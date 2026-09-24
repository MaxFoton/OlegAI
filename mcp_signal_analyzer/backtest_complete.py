#!/usr/bin/env python3
"""
ПОЛНЫЙ BACKTEST всех анализов системы
"""

import subprocess
import json
import requests
import time
from datetime import datetime

FIXED_RISK = 3.5  # USD на стоп-лосс (1% от позиции $350)
POSITION_SIZE = 350  # USD размер позиции

def fetch_all_signals():
    """Получить все уникальные сигналы из ntfy"""
    print("Fetching all signals from ntfy...")
    
    result = subprocess.run(
        ['curl', '-s', 'http://87.121.218.4:8080/max-analysis/json?poll=1'],
        capture_output=True,
        text=True,
        timeout=10
    )
    
    if result.returncode != 0:
        print("❌ Failed to fetch from ntfy")
        return []
    
    lines = result.stdout.strip().split('\n')
    
    messages = []
    seen_signals = set()  # Для удаления дубликатов
    
    for line in lines:
        if not line.strip():
            continue
        
        try:
            msg = json.loads(line)
            
            if msg.get('event') != 'message':
                continue
            
            title = msg.get('title', '')
            
            # Только сигналы с ВХОДИТЬ/ШОРТИТЬ
            if not ('✅ ВХОДИТЬ' in title or '✅ ШОРТИТЬ' in title):
                continue
            
            # Создаём уникальный ключ: symbol + direction + timestamp (округлённый до минуты)
            try:
                parts = title.split('|')[0].strip().split()
                symbol = parts[1]
                direction = parts[2]
                timestamp = msg.get('time', 0)
                
                # Округляем до минуты чтобы убрать дубликаты
                minute_timestamp = timestamp - (timestamp % 60)
                
                key = f"{symbol}_{direction}_{minute_timestamp}"
                
                if key not in seen_signals:
                    seen_signals.add(key)
                    messages.append(msg)
            except:
                continue
                
        except:
            continue
    
    print(f"Received {len(messages)} unique signals\n")
    return messages


def parse_signal(msg):
    """Парсим сигнал из ntfy сообщения"""
    title = msg.get('title', '')
    message = msg.get('message', '')
    timestamp = msg.get('time', 0)
    
    try:
        # Символ и направление
        parts = title.split('|')[0].strip().split()
        symbol = parts[1]
        direction = parts[2]
        
        # Парсим Entry, Stop, Target из сообщения
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
            'datetime': datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')
        }
        
    except:
        return None


def get_historical_klines(symbol, start_timestamp):
    """Получить исторические данные с Bybit с момента сигнала"""
    try:
        url = "https://api.bybit.com/v5/market/kline"
        
        # Timestamp в миллисекундах
        start_ms = int(start_timestamp * 1000)
        
        params = {
            'category': 'linear',
            'symbol': f"{symbol}USDT",
            'interval': '5',  # 5-минутные свечи
            'start': start_ms,
            'limit': 200  # До ~16 часов данных
        }
        
        resp = requests.get(url, params=params, timeout=10)
        
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        
        if data.get('retCode') != 0:
            return None
        
        klines = data['result']['list']
        
        # Bybit возвращает в обратном порядке
        klines.reverse()
        
        return klines
        
    except Exception as e:
        return None


def calculate_trade_result(signal, klines):
    """Рассчитать результат сделки"""
    if not klines:
        return None
    
    entry = signal['entry']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    direction = signal['direction']
    
    # Рассчитываем размер позиции исходя из фиксированного риска
    if direction == 'LONG':
        risk_per_coin = abs(entry - stop_loss)
    else:
        risk_per_coin = abs(stop_loss - entry)
    
    # Количество монет = фиксированный риск / риск на монету
    position_size_coins = FIXED_RISK / risk_per_coin
    
    # Проверяем каждую свечу последовательно
    for i, kline in enumerate(klines):
        timestamp_ms = int(kline[0])
        high = float(kline[2])
        low = float(kline[3])
        close = float(kline[4])
        
        if direction == 'LONG':
            # Сначала проверяем стоп-лосс (хуже цена)
            if low <= stop_loss:
                return {
                    'result': 'STOP_LOSS',
                    'pnl': -FIXED_RISK,
                    'exit_price': stop_loss,
                    'candles_count': i + 1
                }
            
            # Потом проверяем тейк-профит
            if high >= take_profit:
                pnl = (take_profit - entry) * position_size_coins
                return {
                    'result': 'TAKE_PROFIT',
                    'pnl': pnl,
                    'exit_price': take_profit,
                    'candles_count': i + 1
                }
        
        else:  # SHORT
            # Сначала проверяем стоп-лосс
            if high >= stop_loss:
                return {
                    'result': 'STOP_LOSS',
                    'pnl': -FIXED_RISK,
                    'exit_price': stop_loss,
                    'candles_count': i + 1
                }
            
            # Потом проверяем тейк-профит
            if low <= take_profit:
                pnl = (entry - take_profit) * position_size_coins
                return {
                    'result': 'TAKE_PROFIT',
                    'pnl': pnl,
                    'exit_price': take_profit,
                    'candles_count': i + 1
                }
    
    # Позиция всё ещё открыта
    current_price = float(klines[-1][4])
    
    if direction == 'LONG':
        unrealized_pnl = (current_price - entry) * position_size_coins
    else:
        unrealized_pnl = (entry - current_price) * position_size_coins
    
    return {
        'result': 'STILL_OPEN',
        'pnl': unrealized_pnl,
        'exit_price': current_price,
        'candles_count': len(klines)
    }


def main():
    print("=" * 80)
    print("ПОЛНЫЙ BACKTEST ВСЕХ АНАЛИЗОВ")
    print(f"Размер позиции: ${POSITION_SIZE}")
    print(f"Фиксированный риск на SL: ${FIXED_RISK} (1%)")
    print("=" * 80)
    print()
    
    # Получаем все сообщения
    messages = fetch_all_signals()
    
    if not messages:
        print("❌ No signals found!")
        return
    
    # Парсим сигналы
    signals = []
    for msg in messages:
        sig = parse_signal(msg)
        if sig:
            signals.append(sig)
    
    if not signals:
        print("❌ Failed to parse signals!")
        return
    
    print(f"Parsed {len(signals)} unique signals\n")
    print("=" * 80)
    print()
    
    results = []
    
    for i, signal in enumerate(signals, 1):
        print(f"[{i}/{len(signals)}] {signal['symbol']} {signal['direction']} ({signal['datetime']})")
        print(f"    Entry: ${signal['entry']:.6f} | SL: ${signal['stop_loss']:.6f} | TP: ${signal['take_profit']:.6f}")
        
        # Получаем исторические данные с момента сигнала
        klines = get_historical_klines(signal['symbol'], signal['timestamp'])
        
        if not klines:
            print(f"    ⚠️ No historical data available\n")
            continue
        
        # Рассчитываем результат
        result = calculate_trade_result(signal, klines)
        
        if result:
            signal['trade_result'] = result
            results.append(signal)
            
            emoji_map = {
                'TAKE_PROFIT': '✅',
                'STOP_LOSS': '❌',
                'STILL_OPEN': '⏳'
            }
            
            emoji = emoji_map.get(result['result'], '?')
            candles = result['candles_count']
            time_str = f"({candles * 5} min)" if candles < 200 else "(16+ hours)"
            
            print(f"    {emoji} {result['result']}: PnL = ${result['pnl']:+.2f} {time_str}\n")
        
        time.sleep(0.3)  # Не перегружаем API
    
    # ИТОГОВАЯ СТАТИСТИКА
    print("=" * 80)
    print("ИТОГОВАЯ СТАТИСТИКА")
    print("=" * 80)
    
    wins = [r for r in results if r['trade_result']['result'] == 'TAKE_PROFIT']
    losses = [r for r in results if r['trade_result']['result'] == 'STOP_LOSS']
    open_trades = [r for r in results if r['trade_result']['result'] == 'STILL_OPEN']
    
    total_pnl = sum(r['trade_result']['pnl'] for r in results)
    closed_trades = wins + losses
    
    print(f"\nВсего сигналов: {len(results)}")
    print(f"Закрыто сделок: {len(closed_trades)}")
    print(f"  ✅ Wins (TP): {len(wins)}")
    print(f"  ❌ Losses (SL): {len(losses)}")
    print(f"  ⏳ Ещё открыты: {len(open_trades)}")
    
    if closed_trades:
        winrate = (len(wins) / len(closed_trades)) * 100
        print(f"\n🎯 Winrate: {winrate:.1f}%")
    else:
        print(f"\n⚠️ Нет закрытых сделок для расчёта winrate")
    
    print(f"\n💰 Total PnL: ${total_pnl:+.2f}")
    
    if wins:
        avg_win = sum(r['trade_result']['pnl'] for r in wins) / len(wins)
        total_wins_pnl = sum(r['trade_result']['pnl'] for r in wins)
        print(f"   💚 Wins Total: ${total_wins_pnl:+.2f} | Avg: ${avg_win:+.2f}")
    
    if losses:
        avg_loss = sum(r['trade_result']['pnl'] for r in losses) / len(losses)
        total_losses_pnl = sum(r['trade_result']['pnl'] for r in losses)
        print(f"   ❤️ Losses Total: ${total_losses_pnl:+.2f} | Avg: ${avg_loss:+.2f}")
    
    if open_trades:
        unrealized_pnl = sum(r['trade_result']['pnl'] for r in open_trades)
        print(f"   ⏳ Open Unrealized: ${unrealized_pnl:+.2f}")
    
    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
