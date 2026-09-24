#!/usr/bin/env python3
"""
Backtest сигналов с 13.08.2026 18:02
TP 1% SL 1% ставка $1250
"""

import json
import re
import requests
from datetime import datetime
from ccxt import bybit
import time

# Конфигурация
NTFY_URL = "http://87.121.218.4:8080/max-analysis/json?poll=1&since=48h"
POSITION_SIZE = 1250  # $
RISK_PERCENT = 1  # 1% SL/TP
CHECK_PERIOD_MINUTES = 60  # Проверяем за 1 час
START_TIME = datetime(2026, 8, 13, 18, 2, 0)  # 13.08.2026, 18:02

# API
exchange = bybit({'enableRateLimit': True})


def fetch_ntfy_messages():
    """Получает сообщения из ntfy"""
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
    
    # Фильтр по времени
    if timestamp < START_TIME:
        return None
    
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
    """Проверяет результат сигнала: TP или SL"""
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
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1, 'exit_price': tp_price}
                if low <= sl_price:
                    pnl = -POSITION_SIZE * (sl_percent / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1, 'exit_price': sl_price}
            else:  # SHORT
                if low <= tp_price:
                    pnl = POSITION_SIZE * (tp_percent / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1, 'exit_price': tp_price}
                if high >= sl_price:
                    pnl = -POSITION_SIZE * (sl_percent / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1, 'exit_price': sl_price}
        
        # Не достиг ни TP ни SL
        last_price = ohlcv[-1][4]
        if direction == "LONG":
            unrealized_pnl = POSITION_SIZE * ((last_price - entry_price) / entry_price)
        else:
            unrealized_pnl = POSITION_SIZE * ((entry_price - last_price) / entry_price)
        
        return {'result': 'TIMEOUT', 'pnl': unrealized_pnl, 'minutes': check_minutes, 'exit_price': last_price}
    
    except Exception as e:
        return {'result': 'ERROR', 'pnl': 0, 'minutes': 0, 'error': str(e)}


def main():
    print("=" * 80)
    print("BACKTEST С 13.08.2026 18:02")
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
    
    # Парсим сообщения
    signals = []
    for msg in messages:
        parsed = parse_ntfy_message(msg)
        if parsed:
            signals.append(parsed)
    
    # Сортируем по времени
    signals.sort(key=lambda x: x['timestamp'])
    
    print(f"Распознано {len(signals)} сигналов с {START_TIME.strftime('%d.%m %H:%M')}")
    print()
    
    # Все сигналы (независимо от вердикта)
    print("=" * 80)
    print("ВСЕ СИГНАЛЫ (если бы Фунтик вошел сразу)")
    print("=" * 80)
    print()
    
    results = []
    balance = 0
    
    for i, sig in enumerate(signals, 1):
        verdict_emoji = {
            'ENTER': '✅',
            'SKIP': '⚠️',
            'WAIT': '⏳'
        }.get(sig['verdict'], '❓')
        
        print(f"{i}. {verdict_emoji} {sig['timestamp'].strftime('%d.%m %H:%M')} | {sig['symbol']} {sig['direction']}")
        print(f"   Verdict: {sig['verdict']} ({sig['confidence']}/10) | Entry: ${sig['entry_price']:.6f}")
        
        result = check_signal_result(sig['symbol'], sig['direction'], sig['timestamp'], 
                                     sig['entry_price'], RISK_PERCENT, RISK_PERCENT, CHECK_PERIOD_MINUTES)
        
        if result['result'] == 'TP':
            print(f"   ✅ TP после {result['minutes']} мин | Exit: ${result.get('exit_price', 0):.6f} | PnL: +${result['pnl']:.2f}")
            balance += result['pnl']
        elif result['result'] == 'SL':
            print(f"   ❌ SL после {result['minutes']} мин | Exit: ${result.get('exit_price', 0):.6f} | PnL: ${result['pnl']:.2f}")
            balance += result['pnl']
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout ({result['minutes']} мин) | Exit: ${result.get('exit_price', 0):.6f} | Unrealized: ${result['pnl']:.2f}")
        elif result['result'] == 'NO_DATA':
            print(f"   ❓ Нет данных")
        else:
            print(f"   ❌ Ошибка: {result.get('error', 'Unknown')}")
        
        print(f"   💰 Баланс: ${balance:+.2f}")
        print()
        
        results.append(result)
        time.sleep(0.3)
    
    # Финальная статистика
    print("=" * 80)
    print("ИТОГИ")
    print("=" * 80)
    
    tp_results = [r for r in results if r['result'] == 'TP']
    sl_results = [r for r in results if r['result'] == 'SL']
    timeout_results = [r for r in results if r['result'] == 'TIMEOUT']
    error_results = [r for r in results if r['result'] in ['NO_DATA', 'ERROR']]
    
    closed_trades = len(tp_results) + len(sl_results)
    
    print(f"Всего сигналов: {len(signals)}")
    print(f"✅ TP: {len(tp_results)} ({len(tp_results)/closed_trades*100:.1f}%)" if closed_trades > 0 else "✅ TP: 0")
    print(f"❌ SL: {len(sl_results)} ({len(sl_results)/closed_trades*100:.1f}%)" if closed_trades > 0 else "❌ SL: 0")
    print(f"⏳ Timeout: {len(timeout_results)}")
    print(f"❓ Ошибки/Нет данных: {len(error_results)}")
    
    if closed_trades > 0:
        winrate = len(tp_results) / closed_trades * 100
        print(f"\n📊 Винрейт: {winrate:.1f}%")
    
    total_pnl = sum(r['pnl'] for r in tp_results + sl_results)
    print(f"💰 Total PnL: ${total_pnl:+.2f}")
    
    if closed_trades > 0:
        avg_pnl = total_pnl / closed_trades
        print(f"📈 Средний PnL на трейд: ${avg_pnl:+.2f}")
    
    print()


if __name__ == "__main__":
    main()
