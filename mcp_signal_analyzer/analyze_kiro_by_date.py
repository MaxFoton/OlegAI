#!/usr/bin/env python3
"""
Анализ ВСЕХ анализов Kiro за конкретную дату из лога
Парсит kiro_signal_analysis.log и тестирует ВСЕ сигналы
TP/SL 1%, ставка $350

Usage: python analyze_kiro_by_date.py YYYY-MM-DD
Example: python analyze_kiro_by_date.py 2026-08-12
"""

import re
import sys
from pathlib import Path
from datetime import datetime, timedelta
from ccxt import bybit
import time

# Конфигурация
LOG_FILE = Path("/home/max/freqtrade/logs/kiro_signal_analysis.log")
POSITION_SIZE = 350  # $
TP_PERCENT = 1  # 1%
SL_PERCENT = 1  # 1%
CHECK_PERIOD_MINUTES = 60  # Проверяем 1 час

# API
exchange = bybit({'enableRateLimit': True})


def parse_log_signals(log_file: Path, target_date: str):
    """
    Парсит лог и извлекает все анализы за указанную дату
    Формат лога:
    2026-08-14 14:03:13,564 - INFO - 🔍 Analyzing: 2Z LONG score=6
    2026-08-14 14:03:28,155 - INFO - ✅ Sent to ntfy: 📊 2Z LONG | ⏳ ЖДАТЬ ПОДТВЕРЖДЕНИЯ (5/10)
    
    Returns: list of dicts with signal data
    """
    signals = []
    
    with log_file.open('r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Проверяем дату
        if not line.startswith(target_date):
            i += 1
            continue
        
        # Ищем строку "🔍 Analyzing:"
        if "🔍 Analyzing:" not in line:
            i += 1
            continue
        
        # Парсим timestamp
        ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not ts_match:
            i += 1
            continue
        
        timestamp = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
        
        # Парсим: "🔍 Analyzing: SYMBOL DIRECTION score=X"
        analyzing_match = re.search(r'🔍 Analyzing: (\w+) (LONG|SHORT) score=(\d+)', line)
        if not analyzing_match:
            i += 1
            continue
        
        symbol = analyzing_match.group(1)
        direction = analyzing_match.group(2)
        score = int(analyzing_match.group(3))
        
        # Ищем следующую строку "✅ Sent to ntfy:" для вердикта
        verdict = None
        confidence = 0
        entry_price = None
        
        # Смотрим следующие 20 строк
        for j in range(i + 1, min(i + 20, len(lines))):
            next_line = lines[j]
            
            # Парсим ntfy title: "📊 SYMBOL DIRECTION | VERDICT (confidence/10)"
            ntfy_match = re.search(r'✅ Sent to ntfy: 📊 (\w+) (LONG|SHORT) \| (.+?) \((-?\d+)/10\)', next_line)
            if ntfy_match and ntfy_match.group(1) == symbol:
                verdict_str = ntfy_match.group(3)
                confidence = int(ntfy_match.group(4))
                
                # Определяем тип вердикта
                if "✅ ВХОДИТЬ" in verdict_str or "✅ ШОРТИТЬ" in verdict_str:
                    verdict = "ENTER"
                elif "🚫 НЕ ВХОДИТЬ" in verdict_str or "🚫 НЕ ШОРТИТЬ" in verdict_str:
                    verdict = "SKIP"
                elif "⏳ ЖДАТЬ" in verdict_str:
                    verdict = "WAIT"
                
                break
        
        # Ищем entry price в исходной строке queue (ближайшая до Analyzing)
        # Формат: "signal_text": "price=$0.052230"
        for j in range(max(0, i - 30), i):
            prev_line = lines[j]
            if f'"{symbol}/USDT:USDT"' in prev_line or f'"pair": "{symbol}' in prev_line:
                # Ищем price в этой или соседних строках
                for k in range(j, min(j + 10, len(lines))):
                    price_match = re.search(r'price=\$?([0-9.]+)', lines[k])
                    if price_match:
                        entry_price = float(price_match.group(1))
                        break
                if entry_price:
                    break
        
        # Если нашли вердикт и цену - добавляем
        if verdict and entry_price:
            signals.append({
                'timestamp': timestamp,
                'symbol': symbol,
                'direction': direction,
                'score': score,
                'verdict': verdict,
                'confidence': confidence,
                'entry_price': entry_price
            })
        
        i += 1
    
    return signals


def check_signal_result(symbol: str, direction: str, entry_time: datetime, entry_price: float) -> dict:
    """
    Проверяет что случилось с сигналом: TP или SL
    """
    try:
        bybit_symbol = f"{symbol}/USDT:USDT"
        
        since = int(entry_time.timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(bybit_symbol, '1m', since=since, limit=CHECK_PERIOD_MINUTES)
        
        if not ohlcv or len(ohlcv) == 0:
            return {'result': 'NO_DATA', 'pnl': 0, 'minutes': 0}
        
        # Рассчитываем SL и TP
        if direction == "LONG":
            sl_price = entry_price * (1 - SL_PERCENT / 100)
            tp_price = entry_price * (1 + TP_PERCENT / 100)
        else:  # SHORT
            sl_price = entry_price * (1 + SL_PERCENT / 100)
            tp_price = entry_price * (1 - TP_PERCENT / 100)
        
        # Проверяем каждую свечу
        for i, candle in enumerate(ohlcv):
            low = candle[2]
            high = candle[1]
            
            if direction == "LONG":
                if high >= tp_price:
                    pnl = POSITION_SIZE * (TP_PERCENT / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1}
                if low <= sl_price:
                    pnl = -POSITION_SIZE * (SL_PERCENT / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1}
            else:  # SHORT
                if low <= tp_price:
                    pnl = POSITION_SIZE * (TP_PERCENT / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1}
                if high >= sl_price:
                    pnl = -POSITION_SIZE * (SL_PERCENT / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1}
        
        # Не достиг ни TP ни SL
        last_price = ohlcv[-1][4]
        if direction == "LONG":
            unrealized_pnl = POSITION_SIZE * ((last_price - entry_price) / entry_price)
        else:
            unrealized_pnl = POSITION_SIZE * ((entry_price - last_price) / entry_price)
        
        return {'result': 'TIMEOUT', 'pnl': unrealized_pnl, 'minutes': CHECK_PERIOD_MINUTES}
    
    except Exception as e:
        return {'result': 'ERROR', 'pnl': 0, 'minutes': 0, 'error': str(e)}


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} YYYY-MM-DD")
        print(f"Example: python {sys.argv[0]} 2026-08-12")
        return
    
    target_date = sys.argv[1]
    
    # Проверяем формат даты
    try:
        datetime.strptime(target_date, '%Y-%m-%d')
    except ValueError:
        print(f"❌ Неверный формат даты. Используй: YYYY-MM-DD")
        return
    
    print("=" * 80)
    print(f"АНАЛИЗ ВСЕХ АНАЛИЗОВ KIRO ЗА {target_date}")
    print("=" * 80)
    print(f"💰 Position: ${POSITION_SIZE}")
    print(f"📊 SL: {SL_PERCENT}% | TP: {TP_PERCENT}%")
    print(f"⏱️  Проверка: {CHECK_PERIOD_MINUTES} минут")
    print("🎯 Входим во ВСЕ сигналы (даже НЕ ВХОДИТЬ и ЖДАТЬ)")
    print()
    
    # Парсим лог
    print(f"📂 Парсинг лога: {LOG_FILE}")
    signals = parse_log_signals(LOG_FILE, target_date)
    
    if not signals:
        print(f"❌ Не найдено сигналов за {target_date}")
        return
    
    print(f"✅ Найдено {len(signals)} сигналов")
    print()
    
    # Группируем по вердиктам
    enter_signals = [s for s in signals if s['verdict'] == 'ENTER']
    skip_signals = [s for s in signals if s['verdict'] == 'SKIP']
    wait_signals = [s for s in signals if s['verdict'] == 'WAIT']
    
    print(f"📊 Статистика по вердиктам:")
    print(f"   ✅ ENTER (ВХОДИТЬ/ШОРТИТЬ): {len(enter_signals)}")
    print(f"   🚫 SKIP (НЕ ВХОДИТЬ): {len(skip_signals)}")
    print(f"   ⏳ WAIT (ЖДАТЬ): {len(wait_signals)}")
    print()
    
    all_results = []
    
    # Анализируем ВСЕ сигналы
    print("=" * 80)
    print("ТЕСТИРОВАНИЕ ВСЕХ СИГНАЛОВ")
    print("=" * 80)
    print()
    
    for i, sig in enumerate(signals, 1):
        verdict_emoji = {"ENTER": "✅", "SKIP": "🚫", "WAIT": "⏳"}[sig['verdict']]
        
        print(f"[{i}/{len(signals)}] {verdict_emoji} {sig['symbol']} {sig['direction']} @ {sig['timestamp'].strftime('%d.%m %H:%M')}")
        print(f"   Verdict: {sig['verdict']}")
        print(f"   Confidence: {sig['confidence']}/10 | Score: {sig['score']}/15 | Entry: ${sig['entry_price']:.8f}")
        
        result = check_signal_result(sig['symbol'], sig['direction'], sig['timestamp'], sig['entry_price'])
        
        if result['result'] == 'TP':
            print(f"   ✅ TP после {result['minutes']} минут | PnL: +${result['pnl']:.2f}")
        elif result['result'] == 'SL':
            print(f"   ❌ SL после {result['minutes']} минут | PnL: ${result['pnl']:.2f}")
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout после {result['minutes']} минут | Unrealized: ${result['pnl']:.2f}")
        else:
            print(f"   ❓ {result['result']}")
        
        print()
        
        all_results.append({
            'signal': sig,
            'result': result
        })
        
        time.sleep(0.3)
    
    # ИТОГОВАЯ СТАТИСТИКА
    print("=" * 80)
    print("ИТОГИ ПО ВСЕМ СИГНАЛАМ")
    print("=" * 80)
    print()
    
    tp_count = len([r for r in all_results if r['result']['result'] == 'TP'])
    sl_count = len([r for r in all_results if r['result']['result'] == 'SL'])
    timeout_count = len([r for r in all_results if r['result']['result'] == 'TIMEOUT'])
    total_pnl = sum(r['result']['pnl'] for r in all_results)
    
    print(f"📊 Всего сигналов: {len(all_results)}")
    print(f"   ✅ TP: {tp_count} ({tp_count/len(all_results)*100:.1f}%)")
    print(f"   ❌ SL: {sl_count} ({sl_count/len(all_results)*100:.1f}%)")
    print(f"   ⏳ Timeout: {timeout_count}")
    
    if tp_count + sl_count > 0:
        winrate = tp_count / (tp_count + sl_count) * 100
        print(f"\n🎯 Winrate (TP vs SL): {winrate:.1f}%")
    
    print(f"\n💰 Total PnL: ${total_pnl:+.2f}")
    print()
    
    # Статистика по вердиктам
    print("=" * 80)
    print("ДЕТАЛЬНАЯ СТАТИСТИКА ПО ВЕРДИКТАМ")
    print("=" * 80)
    print()
    
    for verdict_type, verdict_name in [("ENTER", "✅ ВХОДИТЬ"), ("SKIP", "🚫 НЕ ВХОДИТЬ"), ("WAIT", "⏳ ЖДАТЬ")]:
        verdict_results = [r for r in all_results if r['signal']['verdict'] == verdict_type]
        
        if not verdict_results:
            continue
        
        tp = len([r for r in verdict_results if r['result']['result'] == 'TP'])
        sl = len([r for r in verdict_results if r['result']['result'] == 'SL'])
        timeout = len([r for r in verdict_results if r['result']['result'] == 'TIMEOUT'])
        pnl = sum(r['result']['pnl'] for r in verdict_results)
        
        print(f"{verdict_name} ({len(verdict_results)} сигналов):")
        print(f"   ✅ TP: {tp} | ❌ SL: {sl} | ⏳ Timeout: {timeout}")
        
        if tp + sl > 0:
            wr = tp / (tp + sl) * 100
            print(f"   🎯 Winrate: {wr:.1f}%")
        
        print(f"   💰 PnL: ${pnl:+.2f}")
        print()
    
    print("=" * 80)


if __name__ == "__main__":
    main()
