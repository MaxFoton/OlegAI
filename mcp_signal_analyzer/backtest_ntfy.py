#!/usr/bin/env python3
"""
Backtest сигналов напрямую из ntfy
"""

import requests
import json
import time
from datetime import datetime

FIXED_RISK = 3.5  # USD на стоп-лосс

def fetch_ntfy_messages():
    """Получить все сообщения из ntfy"""
    print("Fetching messages from ntfy (может занять время)...")
    
    try:
        # Попробуем через streaming API
        url = "http://87.121.218.4:8080/max-analysis/json?poll=1"
        
        # Сначала получим последнее
        resp = requests.get(url, timeout=5)
        
        if resp.status_code != 200:
            print(f"Error: {resp.status_code}")
            return []
        
        # Теперь попробуем получить больше через since
        messages = []
        
        # Попробуем разные периоды
        for hours in [1, 2, 4, 8, 12, 24]:
            try:
                url = f"http://87.121.218.4:8080/max-analysis/json?since={hours}h"
                print(f"  Trying since={hours}h...")
                
                resp = requests.get(url, timeout=15, stream=True)
                
                if resp.status_code == 200:
                    # Читаем построчно
                    for line in resp.iter_lines():
                        if line:
                            try:
                                msg = json.loads(line)
                                if msg.get('event') == 'message':
                                    messages.append(msg)
                            except:
                                pass
                    
                    print(f"  Got {len(messages)} messages")
                    
                    if len(messages) > 10:
                        break
                        
            except requests.Timeout:
                print(f"  Timeout for {hours}h")
                continue
            except Exception as e:
                print(f"  Error: {e}")
                continue
        
        return messages
        
    except Exception as e:
        print(f"Error: {e}")
        return []


def parse_signal(msg):
    """Парсим сигнал из сообщения"""
    title = msg.get('title', '')
    message = msg.get('message', '')
    timestamp = msg.get('time', 0)
    
    # Только сигналы с вердиктом ВХОДИТЬ/ШОРТИТЬ
    if not ('✅ ВХОДИТЬ' in title or '✅ ШОРТИТЬ' in title):
        return None
    
    try:
        # Парсим заголовок: 📊 SYMBOL DIRECTION | ✅ ВХОДИТЬ (X/10)
        parts = title.split('|')[0].strip().split()
        symbol = parts[1]
        direction = parts[2]
        
        # Парсим сообщение
        lines = message.split('\n')
        entry = None
        stop_loss = None
        take_profit = None
        
        for line in lines:
            line = line.strip()
            
            if 'Вход:' in line or '💰 Вход:' in line:
                try:
                    entry = float(line.split('$')[1].split()[0])
                except:
                    pass
            
            if 'Стоп:' in line or '🛑 Стоп:' in line:
                try:
                    stop_loss = float(line.split('$')[1].split()[0])
                except:
                    pass
            
            if 'Цель:' in line or '🎯 Цель:' in line:
                try:
                    take_profit = float(line.split('$')[1].split()[0])
                except:
                    pass
        
        if not all([symbol, direction, entry, stop_loss, take_profit]):
            return None
        
        return {
            'symbol': symbol,
            'direction': direction,
            'entry': entry,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'timestamp': timestamp,
            'title': title
        }
        
    except Exception as e:
        return None


def get_bybit_klines(symbol, start_time):
    """Получить свечи с Bybit"""
    try:
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
        
    except:
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
        risk = entry - stop_loss
    else:
        risk = stop_loss - entry
    
    position_size = FIXED_RISK / abs(risk)
    
    # Проверяем свечи
    for kline in klines:
        high = float(kline[2])
        low = float(kline[3])
        
        if direction == "LONG":
            if low <= stop_loss:
                return {'result': 'SL', 'pnl': -FIXED_RISK}
            if high >= take_profit:
                pnl = (take_profit - entry) * position_size
                return {'result': 'TP', 'pnl': pnl}
        else:
            if high >= stop_loss:
                return {'result': 'SL', 'pnl': -FIXED_RISK}
            if low <= take_profit:
                pnl = (entry - take_profit) * position_size
                return {'result': 'TP', 'pnl': pnl}
    
    # Открыта
    current = float(klines[-1][4])
    
    if direction == "LONG":
        pnl = (current - entry) * position_size
    else:
        pnl = (entry - current) * position_size
    
    return {'result': 'OPEN', 'pnl': pnl}


def main():
    print("=" * 80)
    print("BACKTEST АНАЛИЗОВ")
    print(f"Фиксированный риск: ${FIXED_RISK} на стоп-лосс")
    print("=" * 80)
    print()
    
    messages = fetch_ntfy_messages()
    
    if not messages:
        print("\n❌ Не удалось получить сообщения из ntfy!")
        print("Попробуй вручную: curl 'http://87.121.218.4:8080/max-analysis/json?since=24h'")
        return
    
    print(f"\nПарсим {len(messages)} сообщений...")
    
    signals = []
    for msg in messages:
        sig = parse_signal(msg)
        if sig:
            signals.append(sig)
    
    print(f"Найдено {len(signals)} сигналов с ВХОДИТЬ/ШОРТИТЬ\n")
    
    if not signals:
        print("❌ Нет сигналов для бэктеста!")
        return
    
    results = []
    
    for i, signal in enumerate(signals, 1):
        print(f"[{i}/{len(signals)}] {signal['symbol']} {signal['direction']}")
        print(f"  Entry: ${signal['entry']:.6f} | SL: ${signal['stop_loss']:.6f} | TP: ${signal['take_profit']:.6f}")
        
        klines = get_bybit_klines(signal['symbol'], signal['timestamp'])
        
        if not klines:
            print(f"  ⚠️ No data\n")
            continue
        
        result = calculate_pnl(signal, klines)
        
        if result:
            signal['result'] = result
            results.append(signal)
            
            emoji = {'TP': '✅', 'SL': '❌', 'OPEN': '⏳'}[result['result']]
            print(f"  {emoji} {result['result']}: ${result['pnl']:+.2f}\n")
        
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
    
    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
