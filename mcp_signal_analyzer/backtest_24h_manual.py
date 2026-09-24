#!/usr/bin/env python3
"""
Backtest за последние 24 часа по анализам из ntfy
Проверяет что было бы если входить НЕМЕДЛЕННО по рекомендации "ВХОДИТЬ/ШОРТИТЬ"
Использует ТОЧНОЕ ВРЕМЯ из лога анализа (когда отправлен в ntfy)
"""

import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from ccxt import bybit
import time

# Конфигурация
LOG_FILE = Path("/home/max/freqtrade/logs/signal_analysis.log")
POSITION_SIZE = 350  # $
RISK_PERCENT = 1  # 1% SL/TP

# API
exchange = bybit({'enableRateLimit': True})

def check_signal_result(symbol: str, direction: str, entry_time: datetime, entry_price: float, sl_percent: float, tp_percent: float) -> dict:
    """
    Проверяет что случилось с сигналом: TP или SL
    """
    try:
        # Конвертируем символ для bybit
        bybit_symbol = f"{symbol}/USDT:USDT"
        
        # Получаем свечи на 30 минут вперед
        since = int(entry_time.timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(bybit_symbol, '1m', since=since, limit=30)
        
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
            ts = datetime.fromtimestamp(candle[0] / 1000)
            low = candle[2]
            high = candle[1]
            
            if direction == "LONG":
                # Проверяем TP (цена выросла)
                if high >= tp_price:
                    pnl = POSITION_SIZE * (tp_percent / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1, 'hit_price': high, 'hit_time': ts}
                
                # Проверяем SL (цена упала)
                if low <= sl_price:
                    pnl = -POSITION_SIZE * (sl_percent / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1, 'hit_price': low, 'hit_time': ts}
            
            else:  # SHORT
                # Проверяем TP (цена упала)
                if low <= tp_price:
                    pnl = POSITION_SIZE * (tp_percent / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1, 'hit_price': low, 'hit_time': ts}
                
                # Проверяем SL (цена выросла)
                if high >= sl_price:
                    pnl = -POSITION_SIZE * (sl_percent / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1, 'hit_price': high, 'hit_time': ts}
        
        # Не достиг ни TP ни SL за 30 минут
        last_price = ohlcv[-1][4]
        if direction == "LONG":
            unrealized_pnl = POSITION_SIZE * ((last_price - entry_price) / entry_price)
        else:
            unrealized_pnl = POSITION_SIZE * ((entry_price - last_price) / entry_price)
        
        return {'result': 'TIMEOUT', 'pnl': unrealized_pnl, 'minutes': 30, 'last_price': last_price}
    
    except Exception as e:
        print(f"  ❌ Ошибка проверки {symbol}: {e}")
        return {'result': 'ERROR', 'pnl': 0, 'minutes': 0}


def parse_log_for_trades():
    """
    Парсит лог анализа и находит все сделки с вердиктом ENTER
    Возвращает список с точным временем анализа (UTC+3), символом, направлением, ценой
    """
    trades = []
    
    with LOG_FILE.open('r') as f:
        lines = f.readlines()
    
    # Ищем паттерн: дата 08.08.2026 + "ANALYZED" + "ВХОДИТЬ/ШОРТИТЬ"
    for i, line in enumerate(lines):
        if "ANALYZED:" not in line:
            continue
        
        # Парсим timestamp (формат: 2026-08-08 HH:MM:SS,mmm)
        timestamp_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not timestamp_match:
            continue
        
        timestamp_str = timestamp_match.group(1)
        timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
        
        # ТОЛЬКО ЗА ДВОЕ СУТОК (07.08 и 08.08)
        if timestamp.date() not in [datetime(2026, 8, 7).date(), datetime(2026, 8, 8).date()]:
            continue
        
        # Парсим: "ANALYZED: SYMBOL DIRECTION | VERDICT (confidence/10)"
        analyzed_match = re.search(r'ANALYZED: (\w+) (LONG|SHORT) \| (.+?) \((-?\d+)/10\)', line)
        if not analyzed_match:
            continue
        
        symbol = analyzed_match.group(1)
        direction = analyzed_match.group(2)
        verdict_str = analyzed_match.group(3)
        confidence = int(analyzed_match.group(4))
        
        # Только если вердикт ENTER
        if "✅ ВХОДИТЬ" not in verdict_str and "✅ ШОРТИТЬ" not in verdict_str:
            continue
        
        # Ищем Entry price в СЛЕДУЮЩЕЙ строке (формат: "Entry: $X.XXXXXX | Stop: ...")
        entry_price = None
        
        if i + 1 < len(lines):
            next_line = lines[i + 1]
            entry_match = re.search(r'Entry: \$([0-9.]+)', next_line)
            if entry_match:
                entry_price = float(entry_match.group(1))
        
        if not entry_price:
            print(f"  ⚠️ Не найдена цена входа для {symbol} {direction} @ {timestamp.strftime('%H:%M:%S')}")
            continue
        
        trades.append({
            'timestamp': timestamp,
            'symbol': symbol,
            'direction': direction,
            'entry_price': entry_price,
            'confidence': confidence
        })
    
    return trades


def main():
    print("=" * 80)
    print("BACKTEST ЗА СЕГОДНЯ 08.08.2026 (ручной вход по анализам)")
    print("=" * 80)
    print(f"Position: ${POSITION_SIZE}")
    print(f"SL: {RISK_PERCENT}% | TP: {RISK_PERCENT}%")
    print(f"Время: UTC+3 (Moscow)")
    print()
    
    # Парсим лог и находим все ENTER сделки
    trades_data = parse_log_for_trades()
    
    if not trades_data:
        print("❌ Не найдено сделок с вердиктом ENTER за последние 24ч")
        return
    
    print(f"Найдено {len(trades_data)} сделок с вердиктом ✅ ENTER")
    print()
    
    trades_results = []
    
    for trade in trades_data:
        symbol = trade['symbol']
        direction = trade['direction']
        entry_price = trade['entry_price']
        confidence = trade['confidence']
        timestamp = trade['timestamp']
        
        print(f"📊 {symbol} {direction} @ {timestamp.strftime('%d.%m %H:%M:%S')}")
        print(f"   Confidence: {confidence}/10")
        print(f"   Entry: ${entry_price:.8f}")
        
        result = check_signal_result(symbol, direction, timestamp, entry_price, RISK_PERCENT, RISK_PERCENT)
        
        if result['result'] == 'TP':
            print(f"   ✅ TP после {result['minutes']} минут @ ${result['hit_price']:.8f}")
            print(f"   💰 PnL: +${result['pnl']:.2f}")
        elif result['result'] == 'SL':
            print(f"   ❌ SL после {result['minutes']} минут @ ${result['hit_price']:.8f}")
            print(f"   💸 PnL: ${result['pnl']:.2f}")
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout после 30 минут @ ${result.get('last_price', 0):.8f}")
            print(f"   📊 Unrealized PnL: ${result['pnl']:.2f}")
        else:
            print(f"   ❓ {result['result']}")
        
        print()
        
        trades_results.append({
            'symbol': symbol,
            'direction': direction,
            'timestamp': timestamp,
            'confidence': confidence,
            'result': result['result'],
            'pnl': result['pnl'],
            'minutes': result.get('minutes', 0)
        })
        
        time.sleep(0.5)  # Rate limit
    
    # Статистика
    print()
    print("=" * 80)
    print("ИТОГИ")
    print("=" * 80)
    
    tp_count = len([t for t in trades_results if t['result'] == 'TP'])
    sl_count = len([t for t in trades_results if t['result'] == 'SL'])
    timeout_count = len([t for t in trades_results if t['result'] == 'TIMEOUT'])
    error_count = len([t for t in trades_results if t['result'] == 'ERROR' or t['result'] == 'NO_DATA'])
    
    total_pnl = sum(t['pnl'] for t in trades_results)
    winrate = (tp_count / len(trades_results) * 100) if trades_results else 0
    
    avg_tp_time = sum(t['minutes'] for t in trades_results if t['result'] == 'TP') / tp_count if tp_count > 0 else 0
    avg_sl_time = sum(t['minutes'] for t in trades_results if t['result'] == 'SL') / sl_count if sl_count > 0 else 0
    
    print(f"Всего сделок: {len(trades_results)}")
    print()
    print(f"Результаты:")
    print(f"  ✅ TP: {tp_count} ({tp_count/len(trades_results)*100:.1f}%)")
    print(f"  ❌ SL: {sl_count} ({sl_count/len(trades_results)*100:.1f}%)")
    print(f"  ⏳ Timeout: {timeout_count} ({timeout_count/len(trades_results)*100:.1f}%)")
    if error_count > 0:
        print(f"  ❓ Errors: {error_count}")
    print()
    print(f"📊 Винрейт (TP vs SL): {winrate:.1f}%")
    print(f"💰 Total PnL: ${total_pnl:+.2f}")
    print(f"💵 Avg PnL per trade: ${total_pnl/len(trades_results):+.2f}")
    print()
    print(f"⏱️ Avg TP time: {avg_tp_time:.1f} минут")
    print(f"⏱️ Avg SL time: {avg_sl_time:.1f} минут")
    print()
    
    # Детали по каждой сделке
    print("Детали сделок:")
    for t in trades_results:
        result_emoji = "✅" if t['result'] == 'TP' else "❌" if t['result'] == 'SL' else "⏳"
        time_str = t['timestamp'].strftime('%d.%m %H:%M')
        print(f"  {result_emoji} {time_str} | {t['symbol']} {t['direction']} | conf={t['confidence']} | PnL=${t['pnl']:+.2f} | {t['minutes']}мин")
    
    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
