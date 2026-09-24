#!/usr/bin/env python3
"""
Backtest сигналов из queue файла
"""

import json
import requests
import time
from pathlib import Path

QUEUE_FILE = Path("/home/max/freqtrade/.kiro/signal_queue.json")
FIXED_RISK = 3.5  # USD на стоп-лосс (1% от $350)

def load_signals():
    """Загрузить все обработанные сигналы из очереди"""
    with QUEUE_FILE.open('r') as f:
        queue = json.load(f)
    
    # Только те где был вердикт ВХОДИТЬ/ШОРТИТЬ (confidence >= 7)
    return [item for item in queue if item['status'] == 'processed' and item['score'] >= 7]


def parse_signal_for_backtest(queue_item):
    """Извлечь данные для бэктеста"""
    sig = queue_item['signal']
    full_text = sig.get('full_text', '')
    
    symbol = sig['symbol']
    direction = sig['direction']
    price = sig['price']
    timestamp = queue_item['timestamp']
    
    # Парсим RSI, Phase и другие параметры
    rsi = phase = vw_macd = None
    
    for line in full_text.split('\n'):
        if 'RSI:' in line:
            try:
                rsi = int(line.split('RSI:')[1].split()[0])
            except:
                pass
        if 'Phase:' in line:
            phase = line.split('Phase:')[1].strip().split()[0]
        if 'VW-MACD:' in line:
            vw_macd = 'bearish' if 'bearish' in line else 'bullish'
    
    # Определяем вердикт (имитируем логику из auto_process_queue.py)
    confidence = 5
    
    if direction == "LONG":
        if rsi and rsi < 30:
            confidence += 2
        if phase in ['EARLY_EXPANSION', 'EXHAUSTION']:
            confidence += 2
        if vw_macd == 'bearish':
            confidence -= 2
    
    elif direction == "SHORT":
        if rsi and rsi > 70:
            confidence += 2
        if phase in ['LATE_EXPANSION']:
            confidence += 2
        if vw_macd == 'bullish':
            confidence -= 2
    
    # Только сигналы где confidence >= 7
    if confidence < 7:
        return None
    
    # Рассчитываем точки входа/SL/TP (упрощённая версия)
    if direction == "LONG":
        entry = price * 1.002
        stop_loss = price * 0.97
        take_profit = price * 1.08
    else:
        entry = price * 0.998
        stop_loss = price * 1.03
        take_profit = price * 0.92
    
    return {
        'symbol': symbol,
        'direction': direction,
        'price': price,
        'entry': entry,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'timestamp': timestamp,
        'rsi': rsi,
        'phase': phase
    }


def get_bybit_klines(symbol, start_timestamp):
    """Получить свечи с Bybit"""
    try:
        from datetime import datetime
        
        # Конвертируем ISO timestamp в unix milliseconds
        dt = datetime.fromisoformat(start_timestamp.replace('Z', '+00:00'))
        start_ms = int(dt.timestamp() * 1000)
        
        url = "https://api.bybit.com/v5/market/kline"
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
        print(f"    Error fetching data: {e}")
        return None


def calculate_pnl(signal, klines):
    """Рассчитать PnL"""
    if not klines:
        return None
    
    entry = signal['entry']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    direction = signal['direction']
    
    # Размер позиции
    if direction == "LONG":
        risk_per_coin = entry - stop_loss
    else:
        risk_per_coin = stop_loss - entry
    
    position_size = FIXED_RISK / abs(risk_per_coin)
    
    # Проверяем свечи
    for kline in klines:
        high = float(kline[2])
        low = float(kline[3])
        
        if direction == "LONG":
            if low <= stop_loss:
                return {'result': 'SL', 'pnl': -FIXED_RISK, 'exit': stop_loss}
            if high >= take_profit:
                pnl = (take_profit - entry) * position_size
                return {'result': 'TP', 'pnl': pnl, 'exit': take_profit}
        else:
            if high >= stop_loss:
                return {'result': 'SL', 'pnl': -FIXED_RISK, 'exit': stop_loss}
            if low <= take_profit:
                pnl = (entry - take_profit) * position_size
                return {'result': 'TP', 'pnl': pnl, 'exit': take_profit}
    
    # Открыта
    current = float(klines[-1][4])
    
    if direction == "LONG":
        pnl = (current - entry) * position_size
    else:
        pnl = (entry - current) * position_size
    
    return {'result': 'OPEN', 'pnl': pnl, 'exit': current}


def main():
    print("=" * 80)
    print("BACKTEST АНАЛИЗОВ СИГНАЛОВ")
    print(f"Фиксированный риск: ${FIXED_RISK} на стоп-лосс")
    print("=" * 80)
    print()
    
    queue_items = load_signals()
    print(f"Loaded {len(queue_items)} processed signals from queue\n")
    
    signals = []
    for item in queue_items:
        sig = parse_signal_for_backtest(item)
        if sig:
            signals.append(sig)
    
    print(f"Filtered to {len(signals)} signals with confidence >= 7\n")
    
    if not signals:
        print("No actionable signals found!")
        return
    
    results = []
    
    for i, signal in enumerate(signals, 1):
        print(f"[{i}/{len(signals)}] {signal['symbol']} {signal['direction']} (RSI={signal['rsi']}, Phase={signal['phase']})")
        print(f"    Entry: ${signal['entry']:.6f} | SL: ${signal['stop_loss']:.6f} | TP: ${signal['take_profit']:.6f}")
        
        klines = get_bybit_klines(signal['symbol'], signal['timestamp'])
        
        if not klines:
            print(f"    ⚠️ No data\n")
            continue
        
        result = calculate_pnl(signal, klines)
        
        if result:
            signal['result'] = result
            results.append(signal)
            
            emoji = {'TP': '✅', 'SL': '❌', 'OPEN': '⏳'}[result['result']]
            print(f"    {emoji} {result['result']}: ${result['pnl']:+.2f}\n")
        
        time.sleep(0.3)
    
    # Итоги
    print("=" * 80)
    print("ИТОГИ")
    print("=" * 80)
    
    wins = [r for r in results if r['result']['result'] == 'TP']
    losses = [r for r in results if r['result']['result'] == 'SL']
    opens = [r for r in results if r['result']['result'] == 'OPEN']
    
    total_pnl = sum(r['result']['pnl'] for r in results)
    closed = wins + losses
    
    print(f"\nВсего: {len(results)}")
    print(f"Закрыто: {len(closed)} | ✅ Wins: {len(wins)} | ❌ Losses: {len(losses)} | ⏳ Open: {len(opens)}")
    
    if closed:
        winrate = len(wins) / len(closed) * 100
        print(f"\n🎯 Winrate: {winrate:.1f}%")
    
    print(f"💰 Total PnL: ${total_pnl:+.2f}")
    
    if wins:
        avg_win = sum(r['result']['pnl'] for r in wins) / len(wins)
        print(f"   Avg Win: ${avg_win:+.2f}")
    
    if losses:
        avg_loss = sum(r['result']['pnl'] for r in losses) / len(losses)
        print(f"   Avg Loss: ${avg_loss:+.2f}")


if __name__ == "__main__":
    main()
