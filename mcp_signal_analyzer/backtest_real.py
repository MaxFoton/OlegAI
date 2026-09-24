#!/usr/bin/env python3
"""
РЕАЛЬНЫЙ BACKTEST: проверка ±1% от цены сигнала
"""

import subprocess
import json
import requests
import time
from datetime import datetime

FIXED_SL_PCT = 1.0  # 1% стоп-лосс
FIXED_TP_PCT = 1.0  # 1% тейк-профит
RISK_AMOUNT = 3.5  # USD риск на сделку

def fetch_signals():
    """Получить уникальные сигналы из ntfy"""
    result = subprocess.run(
        ['curl', '-s', 'http://87.121.218.4:8080/max-analysis/json?poll=1'],
        capture_output=True,
        text=True,
        timeout=10
    )
    
    if result.returncode != 0:
        return []
    
    lines = result.stdout.strip().split('\n')
    
    messages = []
    seen = set()
    
    for line in lines:
        if not line.strip():
            continue
        
        try:
            msg = json.loads(line)
            
            if msg.get('event') != 'message':
                continue
            
            title = msg.get('title', '')
            
            if not ('✅ ВХОДИТЬ' in title or '✅ ШОРТИТЬ' in title):
                continue
            
            # Уникальность по symbol + direction + minute
            parts = title.split('|')[0].strip().split()
            symbol = parts[1]
            direction = parts[2]
            
            # ИСПОЛЬЗУЕМ TIMESTAMP УВЕДОМЛЕНИЯ (когда я отправил в ntfy)
            timestamp = msg.get('time', 0)
            minute_ts = timestamp - (timestamp % 60)
            
            key = f"{symbol}_{direction}_{minute_ts}"
            
            if key not in seen:
                seen.add(key)
                
                # Извлекаем цену из сообщения (это цена сигнала, но время - время уведомления!)
                message_text = msg.get('message', '')
                price = None
                
                for line in message_text.split('\n'):
                    if 'Цена:' in line or '💰 Цена:' in line:
                        try:
                            price = float(line.split('$')[1].split()[0])
                            break
                        except:
                            pass
                
                if price and timestamp:
                    messages.append({
                        'symbol': symbol,
                        'direction': direction,
                        'price': price,
                        'timestamp': timestamp,  # Время уведомления в ntfy!
                        'datetime': datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')
                    })
        except:
            continue
    
    return messages


def get_historical_klines(symbol, start_timestamp):
    """Получить исторические свечи с Bybit"""
    try:
        url = "https://api.bybit.com/v5/market/kline"
        
        start_ms = int(start_timestamp * 1000)
        
        params = {
            'category': 'linear',
            'symbol': f"{symbol}USDT",
            'interval': '1',  # 1-минутные свечи для точности
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
        
    except:
        return None


def calculate_result(signal, klines):
    """Рассчитать результат: что быстрее - SL -1% или TP +1%"""
    if not klines:
        return None
    
    price = signal['price']
    direction = signal['direction']
    
    # Рассчитываем SL и TP от цены сигнала
    if direction == 'LONG':
        stop_loss = price * (1 - FIXED_SL_PCT / 100)
        take_profit = price * (1 + FIXED_TP_PCT / 100)
    else:  # SHORT
        stop_loss = price * (1 + FIXED_SL_PCT / 100)
        take_profit = price * (1 - FIXED_TP_PCT / 100)
    
    # Размер позиции
    risk_per_coin = abs(price - stop_loss)
    position_size = RISK_AMOUNT / risk_per_coin
    
    # Проверяем каждую свечу
    for i, kline in enumerate(klines):
        high = float(kline[2])
        low = float(kline[3])
        
        if direction == 'LONG':
            # Сначала проверяем SL (хуже)
            if low <= stop_loss:
                return {
                    'result': 'SL',
                    'pnl': -RISK_AMOUNT,
                    'minutes': i + 1
                }
            
            # Потом TP
            if high >= take_profit:
                pnl = (take_profit - price) * position_size
                return {
                    'result': 'TP',
                    'pnl': pnl,
                    'minutes': i + 1
                }
        
        else:  # SHORT
            # Сначала SL
            if high >= stop_loss:
                return {
                    'result': 'SL',
                    'pnl': -RISK_AMOUNT,
                    'minutes': i + 1
                }
            
            # Потом TP
            if low <= take_profit:
                pnl = (price - take_profit) * position_size
                return {
                    'result': 'TP',
                    'pnl': pnl,
                    'minutes': i + 1
                }
    
    # Ещё открыта
    current = float(klines[-1][4])
    
    if direction == 'LONG':
        pnl = (current - price) * position_size
    else:
        pnl = (price - current) * position_size
    
    return {
        'result': 'OPEN',
        'pnl': pnl,
        'minutes': len(klines)
    }


def main():
    print("=" * 80)
    print("РЕАЛЬНЫЙ BACKTEST: ±1% SL/TP ОТ ЦЕНЫ СИГНАЛА")
    print(f"SL: -{FIXED_SL_PCT}% | TP: +{FIXED_TP_PCT}%")
    print(f"Риск на сделку: ${RISK_AMOUNT}")
    print("=" * 80)
    print()
    
    signals = fetch_signals()
    
    if not signals:
        print("❌ No signals!")
        return
    
    print(f"Found {len(signals)} unique signals\n")
    print("=" * 80)
    print()
    
    results = []
    
    for i, signal in enumerate(signals, 1):
        print(f"[{i}/{len(signals)}] {signal['symbol']} {signal['direction']} ({signal['datetime']})")
        print(f"    Price: ${signal['price']:.6f}")
        
        klines = get_historical_klines(signal['symbol'], signal['timestamp'])
        
        if not klines:
            print(f"    ⚠️ No data\n")
            continue
        
        result = calculate_result(signal, klines)
        
        if result:
            signal['result'] = result
            results.append(signal)
            
            emoji = {'TP': '✅', 'SL': '❌', 'OPEN': '⏳'}[result['result']]
            minutes = result['minutes']
            
            print(f"    {emoji} {result['result']}: ${result['pnl']:+.2f} ({minutes} min)\n")
        
        time.sleep(0.3)
    
    # ИТОГИ
    print("=" * 80)
    print("ИТОГИ")
    print("=" * 80)
    
    wins = [r for r in results if r['result']['result'] == 'TP']
    losses = [r for r in results if r['result']['result'] == 'SL']
    opens = [r for r in results if r['result']['result'] == 'OPEN']
    
    total_pnl = sum(r['result']['pnl'] for r in results)
    closed = wins + losses
    
    print(f"\nВсего сигналов: {len(results)}")
    print(f"Закрыто: {len(closed)}")
    print(f"  ✅ TP: {len(wins)}")
    print(f"  ❌ SL: {len(losses)}")
    print(f"  ⏳ Open: {len(opens)}")
    
    if closed:
        winrate = (len(wins) / len(closed)) * 100
        print(f"\n🎯 Winrate: {winrate:.1f}%")
    
    print(f"\n💰 Total PnL: ${total_pnl:+.2f}")
    
    if wins:
        avg_win = sum(r['result']['pnl'] for r in wins) / len(wins)
        total_wins = sum(r['result']['pnl'] for r in wins)
        print(f"   💚 Wins: ${total_wins:+.2f} | Avg: ${avg_win:+.2f}")
    
    if losses:
        avg_loss = sum(r['result']['pnl'] for r in losses) / len(losses)
        total_losses = sum(r['result']['pnl'] for r in losses)
        print(f"   ❤️ Losses: ${total_losses:+.2f} | Avg: ${avg_loss:+.2f}")
    
    if opens:
        unrealized = sum(r['result']['pnl'] for r in opens)
        print(f"   ⏳ Unrealized: ${unrealized:+.2f}")
    
    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
