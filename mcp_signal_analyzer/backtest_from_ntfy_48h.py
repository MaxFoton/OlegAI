#!/usr/bin/env python3
"""
Backtest за последние 48 часов из ntfy уведомлений
Проверяет ОБА типа: ENTER и REJECTED сигналы
Время проверки: 1 час (не 30 минут)
"""

import json
import re
import requests
from datetime import datetime, timedelta
from ccxt import bybit
import time

# Конфигурация
NTFY_URL = "http://87.121.218.4:8080/max-analysis/json?poll=1&since=48h"
POSITION_SIZE = 350  # $
RISK_PERCENT = 1  # 1% SL/TP
CHECK_PERIOD_MINUTES = 60  # Проверяем за 1 час

# API
exchange = bybit({'enableRateLimit': True})


def fetch_ntfy_messages():
    """Получает сообщения из ntfy за последние 48 часов"""
    try:
        response = requests.get(NTFY_URL, timeout=10)
        lines = response.text.strip().split('\n')
        
        messages = []
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
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
    print("BACKTEST ЗА ПОСЛЕДНИЕ 48 ЧАСОВ ИЗ NTFY")
    print("=" * 80)
    print(f"Position: ${POSITION_SIZE}")
    print(f"SL: {RISK_PERCENT}% | TP: {RISK_PERCENT}%")
    print(f"Проверка: {CHECK_PERIOD_MINUTES} минут")
    print()
    
    # Получаем сообщения из ntfy
    messages = fetch_ntfy_messages()
    
    if not messages:
        print("❌ Не удалось получить сообщения из ntfy")
        return
    
    print(f"Получено {len(messages)} уведомлений из ntfy")
    print()
    
    # Парсим сообщения
    signals = []
    for msg in messages:
        parsed = parse_ntfy_message(msg)
        if parsed:
            signals.append(parsed)
    
    print(f"Распознано {len(signals)} сигналов")
    print()
    
    # ИЗМЕНЕНО: Тестируем ВСЕ сигналы (ENTER + REJECTED)
    enter_signals = [s for s in signals if s['verdict'] == 'ENTER']
    rejected_signals = [s for s in signals if s['verdict'] in ['SKIP', 'WAIT']]
    all_signals = signals  # ВСЕ сигналы!
    
    print(f"✅ ENTER сигналов: {len(enter_signals)}")
    print(f"⚠️ REJECTED сигналов: {len(rejected_signals)}")
    print(f"📊 ВСЕГО сигналов: {len(all_signals)}")
    print()
    
    # Backtest ВСЕХ сигналов
    print("=" * 80)
    print("📊 BACKTEST ВСЕХ СИГНАЛОВ (ENTER + WAIT + SKIP)")
    print("=" * 80)
    print()
    
    all_results = []
    for sig in all_signals:
        verdict_emoji = "✅" if sig['verdict'] == 'ENTER' else "⏳" if sig['verdict'] == 'WAIT' else "⚠️"
        print(f"{verdict_emoji} {sig['symbol']} {sig['direction']} @ {sig['timestamp'].strftime('%d.%m %H:%M')}")
        print(f"   Verdict: {sig['verdict']} | Confidence: {sig['confidence']}/10 | Entry: ${sig['entry_price']:.8f}")
        
        result = check_signal_result(sig['symbol'], sig['direction'], sig['timestamp'], 
                                     sig['entry_price'], RISK_PERCENT, RISK_PERCENT, CHECK_PERIOD_MINUTES)
        
        if result['result'] == 'TP':
            print(f"   ✅ TP после {result['minutes']} минут | PnL: +${result['pnl']:.2f}")
        elif result['result'] == 'SL':
            print(f"   ❌ SL после {result['minutes']} минут | PnL: ${result['pnl']:.2f}")
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout после {result['minutes']} минут | Unrealized: ${result['pnl']:.2f}")
        else:
            print(f"   ❓ {result['result']}")
        
        print()
        all_results.append(result)
        time.sleep(0.3)
    
    # Статистика ВСЕХ
    if all_results:
        tp_count = len([r for r in all_results if r['result'] == 'TP'])
        sl_count = len([r for r in all_results if r['result'] == 'SL'])
        timeout_count = len([r for r in all_results if r['result'] == 'TIMEOUT'])
        total_pnl = sum(r['pnl'] for r in all_results)
        
        print("ИТОГИ ВСЕХ СИГНАЛОВ:")
        print(f"  ✅ TP: {tp_count} ({tp_count/len(all_results)*100:.1f}%)")
        print(f"  ❌ SL: {sl_count} ({sl_count/len(all_results)*100:.1f}%)")
        print(f"  ⏳ Timeout: {timeout_count}")
        if tp_count + sl_count > 0:
            print(f"  📊 Винрейт: {tp_count/(tp_count+sl_count)*100:.1f}%")
        print(f"  💰 Total PnL: ${total_pnl:+.2f}")
        print()
    
    # Backtest ENTER сигналов отдельно
    print("=" * 80)
    print("1️⃣ СТАТИСТИКА ENTER СИГНАЛОВ (✅ ВХОДИТЬ / ✅ ШОРТИТЬ)")
    print("=" * 80)
    print()
    
    enter_results = []
    for sig in enter_signals:
        print(f"✅ {sig['symbol']} {sig['direction']} @ {sig['timestamp'].strftime('%d.%m %H:%M')}")
        print(f"   Confidence: {sig['confidence']}/10 | Entry: ${sig['entry_price']:.8f}")
        
        # Найти результат из all_results
        result = None
        for r, s in zip(all_results, all_signals):
            if s['symbol'] == sig['symbol'] and s['timestamp'] == sig['timestamp']:
                result = r
                break
        
        if result:
            enter_results.append(result)
        print()
    
    time.sleep(0.1)
    
    # Статистика ENTER
    if enter_results:
        tp_count = len([r for r in enter_results if r['result'] == 'TP'])
        sl_count = len([r for r in enter_results if r['result'] == 'SL'])
        timeout_count = len([r for r in enter_results if r['result'] == 'TIMEOUT'])
        total_pnl = sum(r['pnl'] for r in enter_results)
        
        print("ИТОГИ ENTER:")
        print(f"  ✅ TP: {tp_count} ({tp_count/len(enter_results)*100:.1f}%)")
        print(f"  ❌ SL: {sl_count} ({sl_count/len(enter_results)*100:.1f}%)")
        print(f"  ⏳ Timeout: {timeout_count}")
        if tp_count + sl_count > 0:
            print(f"  📊 Винрейт: {tp_count/(tp_count+sl_count)*100:.1f}%")
        print(f"  💰 Total PnL: ${total_pnl:+.2f}")
        print()
    
    # Backtest REJECTED сигналов
    print("=" * 80)
    print("2️⃣ BACKTEST REJECTED СИГНАЛОВ (⚠️ НЕ ВХОДИТЬ / ⏳ ЖДАТЬ)")
    print("=" * 80)
    print()
    
    rejected_results = []
    for sig in rejected_signals[:20]:  # Показываем первые 20
        verdict_emoji = "⚠️" if sig['verdict'] == 'SKIP' else "⏳"
        print(f"{verdict_emoji} {sig['symbol']} {sig['direction']} @ {sig['timestamp'].strftime('%d.%m %H:%M')}")
        print(f"   Confidence: {sig['confidence']}/10 | Entry: ${sig['entry_price']:.8f}")
        
        result = check_signal_result(sig['symbol'], sig['direction'], sig['timestamp'], 
                                     sig['entry_price'], RISK_PERCENT, RISK_PERCENT, CHECK_PERIOD_MINUTES)
        
        if result['result'] == 'TP':
            print(f"   ✅ TP после {result['minutes']} минут | Упущенная прибыль: +${result['pnl']:.2f}")
        elif result['result'] == 'SL':
            print(f"   ❌ SL после {result['minutes']} минут | Правильно отклонён: ${result['pnl']:.2f}")
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout после {result['minutes']} минут | Unrealized: ${result['pnl']:.2f}")
        else:
            print(f"   ❓ {result['result']}")
        
        print()
        rejected_results.append(result)
        time.sleep(0.3)
    
    # Статистика REJECTED
    if rejected_results:
        tp_count = len([r for r in rejected_results if r['result'] == 'TP'])
        sl_count = len([r for r in rejected_results if r['result'] == 'SL'])
        timeout_count = len([r for r in rejected_results if r['result'] == 'TIMEOUT'])
        total_pnl = sum(r['pnl'] for r in rejected_results)
        
        print("ИТОГИ REJECTED (первые 20):")
        print(f"  ✅ TP: {tp_count} ({tp_count/len(rejected_results)*100:.1f}%) - упущенная прибыль")
        print(f"  ❌ SL: {sl_count} ({sl_count/len(rejected_results)*100:.1f}%) - правильно отклонены")
        print(f"  ⏳ Timeout: {timeout_count}")
        if tp_count + sl_count > 0:
            wrong_reject = tp_count / (tp_count + sl_count) * 100
            print(f"  📊 Ошибочных отклонений: {wrong_reject:.1f}%")
        print(f"  💰 Упущенная прибыль: ${total_pnl:+.2f}")
        print()
    
    print("=" * 80)


if __name__ == "__main__":
    main()
