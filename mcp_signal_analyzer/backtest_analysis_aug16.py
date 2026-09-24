#!/usr/bin/env python3
"""
Backtest всех сигналов из max-analysis за 16 августа 2026
Проверяет что было бы если Фунтик зашел во ВСЕ сигналы
"""

import json
import re
import requests
from datetime import datetime, timedelta
from ccxt import bybit
import time

# Конфигурация
NTFY_URL = "http://87.121.218.4:8080/max-analysis/json?poll=1"
TARGET_DATE = "2026-08-16"  # 16 августа 2026
POSITION_SIZE = 350  # $
RISK_PERCENT = 1  # 1% SL/TP
CHECK_PERIOD_MINUTES = 60  # Проверяем за 1 час

# API
exchange = bybit({'enableRateLimit': True})


def fetch_ntfy_messages_for_date(target_date: str):
    """Получает сообщения из ntfy за конкретную дату"""
    try:
        # Парсим целевую дату
        target_dt = datetime.strptime(target_date, '%Y-%m-%d')
        now = datetime.now()
        
        # Вычисляем сколько часов назад была дата
        hours_ago = int((now - target_dt).total_seconds() / 3600)
        
        # Запрашиваем с запасом (добавляем 48 часов)
        since_param = f"{hours_ago + 48}h"
        
        url = f"{NTFY_URL}&since={since_param}"
        print(f"🔗 Fetching: {url}")
        
        response = requests.get(url, timeout=30)
        lines = response.text.strip().split('\n')
        
        messages = []
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                
                # Фильтруем по дате
                msg_time = datetime.fromtimestamp(msg.get('time', 0))
                msg_date = msg_time.strftime('%Y-%m-%d')
                
                if msg_date == target_date:
                    messages.append(msg)
            except Exception as e:
                continue
        
        return messages
    except Exception as e:
        print(f"❌ Ошибка получения ntfy: {e}")
        return []


def parse_ntfy_message(msg: dict) -> dict:
    """Парсит ntfy сообщение и извлекает данные сигнала"""
    title = msg.get('title', '')
    message_text = msg.get('message', '')
    timestamp = datetime.fromtimestamp(msg.get('time', 0))
    
    # Парсим title: "📊 SYMBOL DIRECTION | VERDICT (confidence/10)"
    title_match = re.search(r'📊 (\w+) (LONG|SHORT) \| (.+?) \((-?\d+)/10\)', title)
    if not title_match:
        return None
    
    symbol = title_match.group(1)
    direction = title_match.group(2)
    verdict_str = title_match.group(3)
    confidence = int(title_match.group(4))
    
    # Определяем вердикт
    verdict_type = None
    if "✅ ВХОДИТЬ" in verdict_str or "✅ ШОРТИТЬ" in verdict_str:
        verdict_type = "ENTER"
    elif "⚠️ НЕ ВХОДИТЬ" in verdict_str or "🚫 НЕ ШОРТИТЬ" in verdict_str:
        verdict_type = "SKIP"
    elif "⏳ ЖДАТЬ" in verdict_str:
        verdict_type = "WAIT"
    else:
        return None
    
    # Парсим цену входа из message
    price_match = re.search(r'Цена: \$([0-9.]+)', message_text)
    if not price_match:
        # Пробуем найти Entry
        price_match = re.search(r'Вход: \$([0-9.]+)', message_text)
    
    if not price_match:
        return None
    
    entry_price = float(price_match.group(1))
    
    # Парсим score
    score_match = re.search(r'Score: (\d+)/15', message_text)
    score = int(score_match.group(1)) if score_match else None
    
    return {
        'timestamp': timestamp,
        'symbol': symbol,
        'direction': direction,
        'verdict': verdict_type,
        'confidence': confidence,
        'entry_price': entry_price,
        'score': score
    }


def check_signal_result(symbol: str, direction: str, entry_time: datetime, entry_price: float, sl_percent: float, tp_percent: float, check_minutes: int = 60) -> dict:
    """
    Проверяет что случилось с сигналом: TP или SL
    """
    try:
        bybit_symbol = f"{symbol}/USDT:USDT"
        
        since = int(entry_time.timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(bybit_symbol, '1m', since=since, limit=check_minutes)
        
        if not ohlcv or len(ohlcv) == 0:
            return {'result': 'NO_DATA', 'pnl': 0, 'minutes': 0}
        
        # Рассчитываем SL и TP
        if direction == "LONG":
            sl_price = entry_price * (1 - sl_percent / 100)
            tp_price = entry_price * (1 + tp_percent / 100)
        else:  # SHORT
            sl_price = entry_price * (1 + sl_percent / 100)
            tp_price = entry_price * (1 - tp_percent / 100)
        
        # Проверяем каждую свечу
        for i, candle in enumerate(ohlcv):
            low = candle[2]
            high = candle[1]
            
            if direction == "LONG":
                if high >= tp_price:
                    pnl = POSITION_SIZE * (tp_percent / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1}
                if low <= sl_price:
                    pnl = -POSITION_SIZE * (sl_percent / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1}
            else:  # SHORT
                if low <= tp_price:
                    pnl = POSITION_SIZE * (tp_percent / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1}
                if high >= sl_price:
                    pnl = -POSITION_SIZE * (sl_percent / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1}
        
        # Не достиг ни TP ни SL
        last_price = ohlcv[-1][4]
        if direction == "LONG":
            unrealized_pnl = POSITION_SIZE * ((last_price - entry_price) / entry_price)
        else:
            unrealized_pnl = POSITION_SIZE * ((entry_price - last_price) / entry_price)
        
        return {'result': 'TIMEOUT', 'pnl': unrealized_pnl, 'minutes': check_minutes}
    
    except Exception as e:
        return {'result': 'ERROR', 'pnl': 0, 'minutes': 0, 'error': str(e)}


def main():
    print("=" * 80)
    print(f"BACKTEST: ВСЕ СИГНАЛЫ ИЗ max-analysis ЗА {TARGET_DATE}")
    print("=" * 80)
    print(f"💰 Position: ${POSITION_SIZE}")
    print(f"📊 SL: {RISK_PERCENT}% | TP: {RISK_PERCENT}%")
    print(f"⏱️  Проверка: {CHECK_PERIOD_MINUTES} минут")
    print(f"🎯 Входим во ВСЕ сигналы (включая ЖДАТЬ и НЕ ВХОДИТЬ)")
    print()
    
    # Получаем сообщения из ntfy за 16 августа
    print(f"📡 Загружаю сообщения из ntfy за {TARGET_DATE}...")
    messages = fetch_ntfy_messages_for_date(TARGET_DATE)
    
    if not messages:
        print(f"❌ Не найдено сообщений за {TARGET_DATE}")
        return
    
    print(f"✅ Получено {len(messages)} уведомлений")
    print()
    
    # Парсим сообщения
    signals = []
    for msg in messages:
        parsed = parse_ntfy_message(msg)
        if parsed:
            signals.append(parsed)
    
    if not signals:
        print(f"❌ Не удалось распознать сигналы")
        return
    
    print(f"✅ Распознано {len(signals)} сигналов")
    print()
    
    # Группируем по вердиктам
    enter_signals = [s for s in signals if s['verdict'] == 'ENTER']
    wait_signals = [s for s in signals if s['verdict'] == 'WAIT']
    skip_signals = [s for s in signals if s['verdict'] == 'SKIP']
    
    print(f"📊 Статистика по вердиктам:")
    print(f"   ✅ ENTER (ВХОДИТЬ): {len(enter_signals)}")
    print(f"   ⏳ WAIT (ЖДАТЬ): {len(wait_signals)}")
    print(f"   🚫 SKIP (НЕ ВХОДИТЬ): {len(skip_signals)}")
    print()
    
    # Backtest ВСЕХ сигналов
    print("=" * 80)
    print("📊 ТЕСТИРОВАНИЕ ВСЕХ СИГНАЛОВ")
    print("=" * 80)
    print()
    
    all_results = []
    for i, sig in enumerate(signals, 1):
        verdict_emoji = {"ENTER": "✅", "WAIT": "⏳", "SKIP": "🚫"}[sig['verdict']]
        
        print(f"[{i}/{len(signals)}] {verdict_emoji} {sig['symbol']} {sig['direction']}")
        print(f"   Время: {sig['timestamp'].strftime('%d.%m.%Y %H:%M:%S')}")
        print(f"   Verdict: {sig['verdict']} | Confidence: {sig['confidence']}/10 | Score: {sig['score']}/15")
        print(f"   Entry: ${sig['entry_price']:.8f}")
        
        result = check_signal_result(sig['symbol'], sig['direction'], sig['timestamp'], 
                                     sig['entry_price'], RISK_PERCENT, RISK_PERCENT, CHECK_PERIOD_MINUTES)
        
        if result['result'] == 'TP':
            print(f"   ✅ TP после {result['minutes']} минут | PnL: +${result['pnl']:.2f}")
        elif result['result'] == 'SL':
            print(f"   ❌ SL после {result['minutes']} минут | PnL: ${result['pnl']:.2f}")
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout {result['minutes']} мин | Unrealized: ${result['pnl']:.2f}")
        elif result['result'] == 'ERROR':
            print(f"   ❓ ERROR: {result.get('error', 'Unknown')}")
        else:
            print(f"   ❓ {result['result']}")
        
        print()
        all_results.append({
            'signal': sig,
            'result': result
        })
        time.sleep(0.3)
    
    # ИТОГОВАЯ СТАТИСТИКА
    print("=" * 80)
    print("📊 ИТОГОВАЯ СТАТИСТИКА")
    print("=" * 80)
    print()
    
    valid_results = [r for r in all_results if r['result']['result'] in ['TP', 'SL', 'TIMEOUT']]
    
    if not valid_results:
        print("❌ Нет валидных результатов")
        return
    
    tp_count = len([r for r in valid_results if r['result']['result'] == 'TP'])
    sl_count = len([r for r in valid_results if r['result']['result'] == 'SL'])
    timeout_count = len([r for r in valid_results if r['result']['result'] == 'TIMEOUT'])
    total_pnl = sum(r['result']['pnl'] for r in valid_results)
    
    print(f"📊 Всего сигналов: {len(valid_results)}")
    print(f"   ✅ TP: {tp_count} ({tp_count/len(valid_results)*100:.1f}%)")
    print(f"   ❌ SL: {sl_count} ({sl_count/len(valid_results)*100:.1f}%)")
    print(f"   ⏳ Timeout: {timeout_count} ({timeout_count/len(valid_results)*100:.1f}%)")
    print()
    
    if tp_count + sl_count > 0:
        winrate = tp_count / (tp_count + sl_count) * 100
        print(f"🎯 Winrate (TP vs SL): {winrate:.1f}%")
    
    print(f"\n💰 Total PnL: ${total_pnl:+.2f}")
    
    if len(valid_results) > 0:
        avg_pnl = total_pnl / len(valid_results)
        print(f"📈 Средний PnL на сделку: ${avg_pnl:+.2f}")
    
    print()
    
    # Детальная статистика по вердиктам
    print("=" * 80)
    print("📋 ДЕТАЛЬНАЯ СТАТИСТИКА ПО ВЕРДИКТАМ")
    print("=" * 80)
    print()
    
    for verdict_type, verdict_name, emoji in [
        ("ENTER", "ВХОДИТЬ", "✅"),
        ("WAIT", "ЖДАТЬ", "⏳"),
        ("SKIP", "НЕ ВХОДИТЬ", "🚫")
    ]:
        verdict_results = [r for r in all_results if r['signal']['verdict'] == verdict_type and r['result']['result'] in ['TP', 'SL', 'TIMEOUT']]
        
        if not verdict_results:
            continue
        
        tp = len([r for r in verdict_results if r['result']['result'] == 'TP'])
        sl = len([r for r in verdict_results if r['result']['result'] == 'SL'])
        timeout = len([r for r in verdict_results if r['result']['result'] == 'TIMEOUT'])
        pnl = sum(r['result']['pnl'] for r in verdict_results)
        
        print(f"{emoji} {verdict_name} ({len(verdict_results)} сигналов):")
        print(f"   ✅ TP: {tp} | ❌ SL: {sl} | ⏳ Timeout: {timeout}")
        
        if tp + sl > 0:
            wr = tp / (tp + sl) * 100
            print(f"   🎯 Winrate: {wr:.1f}%")
        
        print(f"   💰 PnL: ${pnl:+.2f}")
        
        if len(verdict_results) > 0:
            avg = pnl / len(verdict_results)
            print(f"   📈 Средний PnL: ${avg:+.2f}")
        
        print()
    
    print("=" * 80)
    print("✅ BACKTEST ЗАВЕРШЕН")
    print("=" * 80)


if __name__ == "__main__":
    main()
