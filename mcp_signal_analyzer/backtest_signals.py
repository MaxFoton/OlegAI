#!/usr/bin/env python3
"""
Backtest анализов: проверка всех сигналов с вердиктом ВХОДИТЬ/ШОРТИТЬ
"""

import subprocess
import json
from datetime import datetime
import time

FIXED_RISK = 3.5  # USD на стоп-лосс (1% от $350 позиции)

def get_signals_from_ntfy():
    """Получить все сигналы из ntfy с вердиктом ВХОДИТЬ/ШОРТИТЬ"""
    try:
        result = subprocess.run(
            ['curl', '-s', 'http://87.121.218.4:8080/max-analysis/json?since=24h'],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            print("Error fetching ntfy")
            return []
        
        lines = result.stdout.strip().split('\n')
        
        messages = []
        for line in lines:
            if not line.strip():
                continue
            
            try:
                msg = json.loads(line)
                if msg.get('event') == 'message':
                    messages.append(msg)
            except:
                continue
        
        print(f"Received {len(messages)} messages from ntfy")
        
        # Фильтруем только сигналы с вердиктом ВХОДИТЬ/ШОРТИТЬ
        signals = []
        for msg in messages:
            title = msg.get('title', '')
            message = msg.get('message', '')
            
            if '✅ ВХОДИТЬ' in title or '✅ ШОРТИТЬ' in title:
                signal = parse_signal_from_message(title, message, msg.get('time'))
                if signal:
                    signals.append(signal)
        
        print(f"Found {len(signals)} signals with ВХОДИТЬ/ШОРТИТЬ verdict\n")
        return signals
        
    except Exception as e:
        print(f"Error: {e}")
        return []


def parse_signal_from_message(title, message, timestamp):
    """Парсим сигнал из ntfy сообщения"""
    try:
        # Title: 📊 SYMBOL DIRECTION | ✅ ВХОДИТЬ (X/10)
        parts = title.split('|')[0].strip().split()
        symbol = parts[1]
        direction = parts[2]
        
        # Парсим цену, вход, стоп, цель
        lines = message.split('\n')
        price = None
        entry = None
        stop_loss = None
        take_profit = None
        
        for line in lines:
            line = line.strip()
            if line.startswith('💰 Цена:'):
                price = float(line.split('$')[1].strip())
            elif line.startswith('💰 Вход:') or line.startswith('Вход:'):
                entry = float(line.split('$')[1].split()[0])
            elif line.startswith('🛑 Стоп:') or line.startswith('Стоп:'):
                stop_loss = float(line.split('$')[1].split()[0])
            elif line.startswith('🎯 Цель:') or line.startswith('Цель:'):
                take_profit = float(line.split('$')[1].split()[0])
        
        if not all([symbol, direction, entry, stop_loss, take_profit]):
            return None
        
        return {
            'symbol': symbol,
            'direction': direction,
            'price': price or entry,
            'entry': entry,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'timestamp': timestamp
        }
    except Exception as e:
        return None


def get_price_data_from_bybit(symbol, start_time):
    """Получить исторические данные с Bybit"""
    try:
        import requests
        
        url = "https://api.bybit.com/v5/market/kline"
        
        start_ms = int(start_time * 1000)
        
        params = {
            'category': 'linear',
            'symbol': f"{symbol}USDT",
            'interval': '5',
            'start': start_ms,
            'limit': 200
        }
        
        resp = requests.get(url, params=params, timeout=10)
        
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        
        if data.get('retCode') != 0:
            return None
        
        klines = data['result']['list']
        klines.reverse()
        
        return klines
        
    except Exception as e:
        return None


def calculate_pnl(signal, klines):
    """Рассчитать PnL для сигнала"""
    if not klines:
        return None
    
    entry = signal['entry']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    direction = signal['direction']
    
    # Рассчитываем размер позиции
    if direction in ['LONG']:
        risk_per_coin = entry - stop_loss
    else:
        risk_per_coin = stop_loss - entry
    
    position_size = FIXED_RISK / risk_per_coin
    
    # Проверяем каждую свечу
    for kline in klines:
        timestamp = int(kline[0]) / 1000
        high = float(kline[2])
        low = float(kline[3])
        
        if direction in ['LONG']:
            if low <= stop_loss:
                return {'result': 'STOP_LOSS', 'pnl': -FIXED_RISK, 'exit_price': stop_loss, 'position_size': position_size}
            
            if high >= take_profit:
                pnl = (take_profit - entry) * position_size
                return {'result': 'TAKE_PROFIT', 'pnl': pnl, 'exit_price': take_profit, 'position_size': position_size}
        
        else:  # SHORT
            if high >= stop_loss:
                return {'result': 'STOP_LOSS', 'pnl': -FIXED_RISK, 'exit_price': stop_loss, 'position_size': position_size}
            
            if low <= take_profit:
                pnl = (entry - take_profit) * position_size
                return {'result': 'TAKE_PROFIT', 'pnl': pnl, 'exit_price': take_profit, 'position_size': position_size}
    
    # Позиция ещё открыта
    current_price = float(klines[-1][4])
    
    if direction in ['LONG']:
        unrealized_pnl = (current_price - entry) * position_size
    else:
        unrealized_pnl = (entry - current_price) * position_size
    
    return {'result': 'OPEN', 'pnl': unrealized_pnl, 'exit_price': current_price, 'position_size': position_size}


def main():
    print("=" * 80)
    print("BACKTEST АНАЛИЗОВ СИГНАЛОВ")
    print(f"Фиксированный риск: ${FIXED_RISK} на стоп-лосс")
    print("=" * 80)
    print()
    
    signals = get_signals_from_ntfy()
    
    if not signals:
        print("No signals found!")
        return
    
    results = []
    
    for i, signal in enumerate(signals, 1):
        print(f"[{i}/{len(signals)}] {signal['symbol']} {signal['direction']}")
        print(f"  Entry: ${signal['entry']:.6f} | Stop: ${signal['stop_loss']:.6f} | Target: ${signal['take_profit']:.6f}")
        
        klines = get_price_data_from_bybit(signal['symbol'], signal['timestamp'])
        
        if not klines:
            print(f"  ⚠️ No data from Bybit\n")
            continue
        
        result = calculate_pnl(signal, klines)
        
        if result:
            signal['backtest_result'] = result
            results.append(signal)
            
            status = {'TAKE_PROFIT': '✅', 'STOP_LOSS': '❌', 'OPEN': '⏳'}.get(result['result'], '?')
            print(f"  {status} {result['result']}: PnL = ${result['pnl']:+.2f}\n")
        
        time.sleep(0.3)
    
    # Статистика
    print("=" * 80)
    print("ИТОГИ")
    print("=" * 80)
    
    wins = [r for r in results if r['backtest_result']['result'] == 'TAKE_PROFIT']
    losses = [r for r in results if r['backtest_result']['result'] == 'STOP_LOSS']
    open_trades = [r for r in results if r['backtest_result']['result'] == 'OPEN']
    
    total_pnl = sum(r['backtest_result']['pnl'] for r in results)
    closed = wins + losses
    
    print(f"\nВсего сигналов: {len(results)}")
    print(f"Закрыто: {len(closed)} | ✅ Wins: {len(wins)} | ❌ Losses: {len(losses)} | ⏳ Open: {len(open_trades)}")
    
    if closed:
        winrate = len(wins) / len(closed) * 100
        print(f"\n🎯 Winrate: {winrate:.1f}%")
    
    print(f"💰 Total PnL: ${total_pnl:+.2f}")
    
    if wins:
        avg_win = sum(r['backtest_result']['pnl'] for r in wins) / len(wins)
        print(f"   Avg Win: ${avg_win:+.2f}")
    
    if losses:
        avg_loss = sum(r['backtest_result']['pnl'] for r in losses) / len(losses)
        print(f"   Avg Loss: ${avg_loss:+.2f}")
    
    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
