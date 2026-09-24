#!/usr/bin/env python3
"""
Backtest из лог-файла signal_analysis.log
"""

import re
import requests
import time
from pathlib import Path

LOG_FILE = Path("/home/max/freqtrade/logs/signal_analysis.log")
FIXED_RISK = 3.5

def parse_log_file():
    """Парсим лог-файл и извлекаем все анализы"""
    if not LOG_FILE.exists():
        print(f"❌ Log file not found: {LOG_FILE}")
        return []
    
    with LOG_FILE.open('r', encoding='utf-8') as f:
        content = f.read()
    
    # Ищем все записи ANALYZED
    signals = []
    
    # Паттерн: ANALYZED: SYMBOL DIRECTION | VERDICT (X/10)
    pattern = r'ANALYZED: (\w+) (LONG|SHORT) \| ([^(]+) \((\d+)/10\)'
    
    for match in re.finditer(pattern, content):
        symbol = match.group(1)
        direction = match.group(2)
        verdict = match.group(3).strip()
        confidence = int(match.group(4))
        
        # Только сигналы с вердиктом ВХОДИТЬ/ШОРТИТЬ
        if '✅' not in verdict:
            continue
        
        # Ищем детали после этого match
        start_pos = match.end()
        next_section = content.find('=' * 80, start_pos)
        
        if next_section == -1:
            continue
        
        section = content[start_pos:next_section]
        
        # Парсим Entry, Stop, Target
        entry = None
        stop = None
        target = None
        
        entry_match = re.search(r'Entry: \$([0-9.]+)', section)
        if entry_match:
            entry = float(entry_match.group(1))
        
        stop_match = re.search(r'Stop: \$([0-9.]+)', section)
        if stop_match:
            stop = float(stop_match.group(1))
        
        target_match = re.search(r'Target: \$([0-9.]+)', section)
        if target_match:
            target = float(target_match.group(1))
        
        if all([entry, stop, target]):
            signals.append({
                'symbol': symbol,
                'direction': direction,
                'verdict': verdict,
                'confidence': confidence,
                'entry': entry,
                'stop_loss': stop,
                'take_profit': target
            })
    
    return signals


def get_bybit_klines(symbol):
    """Получить последние свечи с Bybit"""
    try:
        url = "https://api.bybit.com/v5/market/kline"
        
        params = {
            'category': 'linear',
            'symbol': f"{symbol}USDT",
            'interval': '5',
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
    print("BACKTEST ИЗ ЛОГ-ФАЙЛА")
    print(f"Фиксированный риск: ${FIXED_RISK} на стоп-лосс")
    print("=" * 80)
    print()
    
    signals = parse_log_file()
    
    if not signals:
        print(f"❌ Не найдено сигналов в {LOG_FILE}")
        print("\nЛог-файл будет создан когда система обработает новый сигнал.")
        return
    
    print(f"Найдено {len(signals)} сигналов с ВХОДИТЬ/ШОРТИТЬ\n")
    
    results = []
    
    for i, signal in enumerate(signals, 1):
        print(f"[{i}/{len(signals)}] {signal['symbol']} {signal['direction']} (confidence={signal['confidence']}/10)")
        print(f"  Entry: ${signal['entry']:.6f} | SL: ${signal['stop_loss']:.6f} | TP: ${signal['take_profit']:.6f}")
        
        klines = get_bybit_klines(signal['symbol'])
        
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
