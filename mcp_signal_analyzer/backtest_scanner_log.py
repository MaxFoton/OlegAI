#!/usr/bin/env python3
"""
Backtest сигналов из scanner.log
TP 1% SL 1% ставка $1250
Начиная с 13.08.2026 18:02
"""

import re
from datetime import datetime, timedelta
from ccxt import bybit
import time

# Конфигурация
SCANNER_LOG = "/home/max/o_p/dex_scanner/logs/scanner.log"
POSITION_SIZE = 1250  # $
RISK_PERCENT = 1  # 1% SL/TP
CHECK_PERIOD_MINUTES = 60  # Проверяем за 1 час
START_TIME = datetime(2026, 8, 13, 18, 2, 0)  # 13.08.2026, 18:02

# API
exchange = bybit({'enableRateLimit': True})


def parse_scanner_log():
    """Парсит scanner.log и извлекает сигналы"""
    signals = []
    
    with open(SCANNER_LOG, 'r') as f:
        lines = f.readlines()
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Ищем SIGNAL_DECISION
        if 'SIGNAL_DECISION' in line:
            # Парсим timestamp
            ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
            if not ts_match:
                i += 1
                continue
            
            timestamp = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
            
            # Фильтр по времени
            if timestamp < START_TIME:
                i += 1
                continue
            
            # Парсим SIGNAL_DECISION: base=X dir=Y price=Z
            decision_match = re.search(r'base=(\w+) dir=(LONG|SHORT) kind=(\w+) score=(\d+) price=([0-9.]+)', line)
            if not decision_match:
                i += 1
                continue
            
            symbol = decision_match.group(1)
            direction = decision_match.group(2)
            kind = decision_match.group(3)
            score = int(decision_match.group(4))
            price = float(decision_match.group(5))
            
            # Ищем SIGNAL_PLAN на следующей строке
            if i + 1 < len(lines) and 'SIGNAL_PLAN' in lines[i + 1]:
                plan_match = re.search(r'entry=([0-9.]+)', lines[i + 1])
                if plan_match:
                    entry_price = float(plan_match.group(1))
                else:
                    entry_price = price
            else:
                entry_price = price
            
            signals.append({
                'timestamp': timestamp,
                'symbol': symbol,
                'direction': direction,
                'kind': kind,
                'score': score,
                'entry_price': entry_price
            })
        
        i += 1
    
    return signals


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
    print("BACKTEST ИЗ SCANNER.LOG С 13.08.2026 18:02")
    print("=" * 80)
    print(f"Position: ${POSITION_SIZE}")
    print(f"SL: {RISK_PERCENT}% | TP: {RISK_PERCENT}%")
    print(f"Проверка: {CHECK_PERIOD_MINUTES} минут")
    print()
    
    # Парсим scanner.log
    print("Парсим scanner.log...")
    signals = parse_scanner_log()
    
    print(f"Найдено {len(signals)} сигналов с {START_TIME.strftime('%d.%m %H:%M')}")
    print()
    
    if not signals:
        print("❌ Сигналы не найдены")
        return
    
    # Backtest
    print("=" * 80)
    print("BACKTEST (если бы Фунтик вошел сразу по market)")
    print("=" * 80)
    print()
    
    results = []
    balance = 0
    
    for i, sig in enumerate(signals, 1):
        print(f"{i}. {sig['timestamp'].strftime('%d.%m %H:%M')} | {sig['symbol']} {sig['direction']}")
        print(f"   Kind: {sig['kind']} | Score: {sig['score']}/15 | Entry: ${sig['entry_price']:.6f}")
        
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
        time.sleep(0.5)
    
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
    print(f"✅ TP: {len(tp_results)}" + (f" ({len(tp_results)/closed_trades*100:.1f}%)" if closed_trades > 0 else ""))
    print(f"❌ SL: {len(sl_results)}" + (f" ({len(sl_results)/closed_trades*100:.1f}%)" if closed_trades > 0 else ""))
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
