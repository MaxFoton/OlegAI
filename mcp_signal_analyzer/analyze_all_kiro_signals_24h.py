#!/usr/bin/env python3
"""
Анализ ВСЕХ сигналов из max-analysis за конкретную дату
Как будто Фунтик вошёл во ВСЕ сигналы сразу (даже "НЕ ВХОДИТЬ")
TP/SL 1%, ставка $350

Usage: python analyze_all_kiro_signals_24h.py [YYYY-MM-DD]
Example: python analyze_all_kiro_signals_24h.py 2026-08-11
"""

import json
import re
import requests
import sys
from datetime import datetime, timedelta
from ccxt import bybit
import time

# Конфигурация
POSITION_SIZE = 350  # $
TP_PERCENT = 1  # 1%
SL_PERCENT = 1  # 1%
CHECK_PERIOD_MINUTES = 60  # Проверяем 1 час

# API
exchange = bybit({'enableRateLimit': True})


def fetch_ntfy_messages(target_date=None):
    """
    Получает сообщения из ntfy за конкретную дату или за последние 24ч
    
    Args:
        target_date: datetime объект для фильтрации (дата в UTC+3, но ищем по UTC!)
    """
    try:
        # Получаем за последние 7 дней чтобы точно захватить нужную дату
        ntfy_url = "http://87.121.218.4:8080/max-analysis/json?poll=1&since=168h"
        response = requests.get(ntfy_url, timeout=30)
        lines = response.text.strip().split('\n')
        
        messages = []
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                
                # Если указана дата - фильтруем
                if target_date:
                    msg_time = datetime.fromtimestamp(msg.get('time', 0))
                    # msg_time уже в UTC
                    
                    # Конвертируем target_date из UTC+3 в UTC для сравнения
                    target_date_utc = target_date - timedelta(hours=3)
                    
                    # Проверяем что сообщение за нужную дату (UTC)
                    if msg_time.date() != target_date_utc.date():
                        continue
                
                messages.append(msg)
            except:
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
    title_match = re.search(r'📊\s+(\w+)\s+(LONG|SHORT)\s+\|\s+(.+?)\s+\((-?\d+)/10\)', title)
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
    elif "🚫 НЕ ВХОДИТЬ" in verdict_str or "🚫 НЕ ШОРТИТЬ" in verdict_str:
        verdict_type = "SKIP"
    elif "⏳ ЖДАТЬ" in verdict_str:
        verdict_type = "WAIT"
    else:
        return None
    
    # Парсим цену входа из message
    price_match = re.search(r'💰 Цена:\s*\$([0-9.]+)', message_text)
    if not price_match:
        return None
    
    entry_price = float(price_match.group(1))
    
    # Парсим score
    score_match = re.search(r'📈 Score:\s*(\d+)/15', message_text)
    score = int(score_match.group(1)) if score_match else None
    
    return {
        'timestamp': timestamp,
        'symbol': symbol,
        'direction': direction,
        'verdict': verdict_type,
        'confidence': confidence,
        'entry_price': entry_price,
        'score': score,
        'verdict_str': verdict_str
    }


def check_signal_result(symbol: str, direction: str, entry_time: datetime, entry_price: float) -> dict:
    """
    Проверяет что случилось с сигналом: TP или SL
    """
    try:
        bybit_symbol = f"{symbol}/USDT:USDT"
        
        since = int(entry_time.timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(bybit_symbol, '1m', since=since, limit=CHECK_PERIOD_MINUTES)
        
        if not ohlcv or len(ohlcv) == 0:
            return {'result': 'NO_DATA', 'pnl': 0, 'minutes': 0}
        
        # Рассчитываем SL и TP
        if direction == "LONG":
            sl_price = entry_price * (1 - SL_PERCENT / 100)
            tp_price = entry_price * (1 + TP_PERCENT / 100)
        else:  # SHORT
            sl_price = entry_price * (1 + SL_PERCENT / 100)
            tp_price = entry_price * (1 - TP_PERCENT / 100)
        
        # Проверяем каждую свечу
        for i, candle in enumerate(ohlcv):
            low = candle[2]
            high = candle[1]
            
            if direction == "LONG":
                if high >= tp_price:
                    pnl = POSITION_SIZE * (TP_PERCENT / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1}
                if low <= sl_price:
                    pnl = -POSITION_SIZE * (SL_PERCENT / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1}
            else:  # SHORT
                if low <= tp_price:
                    pnl = POSITION_SIZE * (TP_PERCENT / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1}
                if high >= sl_price:
                    pnl = -POSITION_SIZE * (SL_PERCENT / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1}
        
        # Не достиг ни TP ни SL
        last_price = ohlcv[-1][4]
        if direction == "LONG":
            unrealized_pnl = POSITION_SIZE * ((last_price - entry_price) / entry_price)
        else:
            unrealized_pnl = POSITION_SIZE * ((entry_price - last_price) / entry_price)
        
        return {'result': 'TIMEOUT', 'pnl': unrealized_pnl, 'minutes': CHECK_PERIOD_MINUTES}
    
    except Exception as e:
        return {'result': 'ERROR', 'pnl': 0, 'minutes': 0, 'error': str(e)}


def main():
    # Парсим аргументы командной строки
    target_date = None
    date_str = "последние 24ч"
    
    if len(sys.argv) > 1:
        try:
            target_date = datetime.strptime(sys.argv[1], '%Y-%m-%d')
            date_str = target_date.strftime('%d.%m.%Y')
        except ValueError:
            print(f"❌ Неверный формат даты. Используй: YYYY-MM-DD")
            print(f"Пример: python {sys.argv[0]} 2026-08-11")
            return
    
    print("=" * 80)
    print(f"АНАЛИЗ ВСЕХ СИГНАЛОВ ИЗ MAX-ANALYSIS ЗА {date_str}")
    print("=" * 80)
    print(f"💰 Position: ${POSITION_SIZE}")
    print(f"📊 SL: {SL_PERCENT}% | TP: {TP_PERCENT}%")
    print(f"⏱️  Проверка: {CHECK_PERIOD_MINUTES} минут")
    print("🎯 Входим во ВСЕ сигналы (даже НЕ ВХОДИТЬ)")
    print()
    
    # Получаем сообщения из ntfy
    messages = fetch_ntfy_messages(target_date)
    
    if not messages:
        print(f"❌ Не удалось получить сообщения из ntfy за {date_str}")
        return
    
    print(f"📩 Получено {len(messages)} уведомлений из ntfy за {date_str}")
    print()
    
    # Парсим сообщения
    signals = []
    for msg in messages:
        parsed = parse_ntfy_message(msg)
        if parsed:
            signals.append(parsed)
    
    print(f"✅ Распознано {len(signals)} сигналов")
    print()
    
    if not signals:
        print("❌ Нет сигналов для анализа")
        return
    
    # Группируем по вердиктам
    enter_signals = [s for s in signals if s['verdict'] == 'ENTER']
    skip_signals = [s for s in signals if s['verdict'] == 'SKIP']
    wait_signals = [s for s in signals if s['verdict'] == 'WAIT']
    
    print(f"📊 Статистика по вердиктам:")
    print(f"   ✅ ENTER (ВХОДИТЬ/ШОРТИТЬ): {len(enter_signals)}")
    print(f"   🚫 SKIP (НЕ ВХОДИТЬ): {len(skip_signals)}")
    print(f"   ⏳ WAIT (ЖДАТЬ): {len(wait_signals)}")
    print()
    
    all_results = []
    
    # Анализируем ВСЕ сигналы
    print("=" * 80)
    print("ТЕСТИРОВАНИЕ ВСЕХ СИГНАЛОВ")
    print("=" * 80)
    print()
    
    for i, sig in enumerate(signals, 1):
        verdict_emoji = {"ENTER": "✅", "SKIP": "🚫", "WAIT": "⏳"}[sig['verdict']]
        
        # Время в UTC+3
        ts_moscow = sig['timestamp'] + timedelta(hours=3)
        
        print(f"[{i}/{len(signals)}] {verdict_emoji} {sig['symbol']} {sig['direction']} @ {ts_moscow.strftime('%d.%m %H:%M')}")
        print(f"   Verdict: {sig['verdict_str']}")
        print(f"   Confidence: {sig['confidence']}/10 | Score: {sig['score']}/15 | Entry: ${sig['entry_price']:.8f}")
        
        result = check_signal_result(sig['symbol'], sig['direction'], sig['timestamp'], sig['entry_price'])
        
        if result['result'] == 'TP':
            print(f"   ✅ TP после {result['minutes']} минут | PnL: +${result['pnl']:.2f}")
        elif result['result'] == 'SL':
            print(f"   ❌ SL после {result['minutes']} минут | PnL: ${result['pnl']:.2f}")
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout после {result['minutes']} минут | Unrealized: ${result['pnl']:.2f}")
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
    print("ИТОГИ ПО ВСЕМ СИГНАЛАМ")
    print("=" * 80)
    print()
    
    tp_count = len([r for r in all_results if r['result']['result'] == 'TP'])
    sl_count = len([r for r in all_results if r['result']['result'] == 'SL'])
    timeout_count = len([r for r in all_results if r['result']['result'] == 'TIMEOUT'])
    total_pnl = sum(r['result']['pnl'] for r in all_results)
    
    print(f"📊 Всего сигналов: {len(all_results)}")
    print(f"   ✅ TP: {tp_count} ({tp_count/len(all_results)*100:.1f}%)")
    print(f"   ❌ SL: {sl_count} ({sl_count/len(all_results)*100:.1f}%)")
    print(f"   ⏳ Timeout: {timeout_count}")
    
    if tp_count + sl_count > 0:
        winrate = tp_count / (tp_count + sl_count) * 100
        print(f"\n🎯 Winrate (TP vs SL): {winrate:.1f}%")
    
    print(f"\n💰 Total PnL: ${total_pnl:+.2f}")
    print()
    
    # Статистика по вердиктам
    print("=" * 80)
    print("ДЕТАЛЬНАЯ СТАТИСТИКА ПО ВЕРДИКТАМ")
    print("=" * 80)
    print()
    
    for verdict_type, verdict_name in [("ENTER", "✅ ВХОДИТЬ"), ("SKIP", "🚫 НЕ ВХОДИТЬ"), ("WAIT", "⏳ ЖДАТЬ")]:
        verdict_results = [r for r in all_results if r['signal']['verdict'] == verdict_type]
        
        if not verdict_results:
            continue
        
        tp = len([r for r in verdict_results if r['result']['result'] == 'TP'])
        sl = len([r for r in verdict_results if r['result']['result'] == 'SL'])
        timeout = len([r for r in verdict_results if r['result']['result'] == 'TIMEOUT'])
        pnl = sum(r['result']['pnl'] for r in verdict_results)
        
        print(f"{verdict_name} ({len(verdict_results)} сигналов):")
        print(f"   ✅ TP: {tp} | ❌ SL: {sl} | ⏳ Timeout: {timeout}")
        
        if tp + sl > 0:
            wr = tp / (tp + sl) * 100
            print(f"   🎯 Winrate: {wr:.1f}%")
        
        print(f"   💰 PnL: ${pnl:+.2f}")
        print()
    
    print("=" * 80)


if __name__ == "__main__":
    main()
