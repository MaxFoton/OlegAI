#!/usr/bin/env python3
"""
Анализ всех анализов Kiro за конкретную дату из signal_analysis.log
TP/SL 1%, ставка $350
"""

import re
import sys
from pathlib import Path
from datetime import datetime
from ccxt import bybit
import time

LOG_FILE = Path("/home/max/freqtrade/logs/kiro_signal_analysis.log")  # ИЗМЕНЕНО
POSITION_SIZE = 1250  # ИЗМЕНЕНО с 350 на 1250
TP_PERCENT = 1
SL_PERCENT = 1
CHECK_PERIOD_MINUTES = 60

exchange = bybit({'enableRateLimit': True})


def get_entry_price_from_api(symbol: str, timestamp: datetime) -> float:
    """Получить цену входа = close свечи в момент сигнала"""
    try:
        bybit_symbol = f"{symbol}/USDT:USDT"
        # Получаем свечу в момент сигнала
        since = int(timestamp.timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(bybit_symbol, '1m', since=since, limit=1)
        
        if ohlcv and len(ohlcv) > 0:
            return float(ohlcv[0][4])  # close price
        return None
    except:
        return None


def parse_log_for_date(log_file: Path, target_date: str):
    """Парсит лог и находит все анализы за дату"""
    signals = []
    
    with log_file.open('r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines):
        if not line.startswith(target_date):
            continue
        
        # Ищем "✅ Sent to ntfy: 📊 SYMBOL DIRECTION | VERDICT (confidence/10)"
        if "✅ Sent to ntfy: 📊" not in line:
            continue
        
        # Парсим timestamp (UTC+3 в логе, конвертируем в UTC для API)
        ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not ts_match:
            continue
        
        # Время в UTC+3 (Moscow)
        timestamp_moscow = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
        # Конвертируем в UTC для API
        from datetime import timedelta
        timestamp_utc = timestamp_moscow - timedelta(hours=3)
        
        # Парсим: "✅ Sent to ntfy: 📊 SYMBOL DIRECTION | VERDICT (confidence/10)"
        ntfy_match = re.search(r'📊 (\w+) (LONG|SHORT) \| (.+?) \((-?\d+)/10\)', line)
        if not ntfy_match:
            continue
        
        symbol = ntfy_match.group(1)
        direction = ntfy_match.group(2)
        verdict_str = ntfy_match.group(3)
        confidence = int(ntfy_match.group(4))
        
        # Определяем вердикт (БЕРЁМ ВСЕ!)
        verdict = None
        if "✅ ВХОДИТЬ" in verdict_str or "✅ ШОРТИТЬ" in verdict_str:
            verdict = "ENTER"
        elif "🚫 НЕ ВХОДИТЬ" in verdict_str or "🚫 НЕ ШОРТИТЬ" in verdict_str:
            verdict = "SKIP"
        elif "⏳ ЖДАТЬ" in verdict_str:
            verdict = "WAIT"
        
        # Пропускаем только если не смогли определить вердикт
        if not verdict:
            continue
        
        # Берём цену из API (цена закрытия свечи в момент сигнала)
        entry_price = get_entry_price_from_api(symbol, timestamp_utc)
        if not entry_price:
            continue
        
        signals.append({
            'timestamp': timestamp_utc,  # UTC для API
            'timestamp_moscow': timestamp_moscow,  # Moscow для отображения
            'symbol': symbol,
            'direction': direction,
            'verdict': verdict,
            'confidence': confidence,
            'entry_price': entry_price,
            'verdict_str': verdict_str
        })
    
    return signals


def check_signal_result(symbol: str, direction: str, entry_time: datetime, entry_price: float) -> dict:
    try:
        bybit_symbol = f"{symbol}/USDT:USDT"
        since = int(entry_time.timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(bybit_symbol, '1m', since=since, limit=CHECK_PERIOD_MINUTES)
        
        if not ohlcv or len(ohlcv) == 0:
            return {'result': 'NO_DATA', 'pnl': 0, 'minutes': 0}
        
        if direction == "LONG":
            sl_price = entry_price * (1 - SL_PERCENT / 100)
            tp_price = entry_price * (1 + TP_PERCENT / 100)
        else:
            sl_price = entry_price * (1 + SL_PERCENT / 100)
            tp_price = entry_price * (1 - TP_PERCENT / 100)
        
        for i, candle in enumerate(ohlcv):
            low = candle[2]
            high = candle[1]
            
            if direction == "LONG":
                if high >= tp_price:
                    return {'result': 'TP', 'pnl': POSITION_SIZE * (TP_PERCENT / 100), 'minutes': i + 1}
                if low <= sl_price:
                    return {'result': 'SL', 'pnl': -POSITION_SIZE * (SL_PERCENT / 100), 'minutes': i + 1}
            else:
                if low <= tp_price:
                    return {'result': 'TP', 'pnl': POSITION_SIZE * (TP_PERCENT / 100), 'minutes': i + 1}
                if high >= sl_price:
                    return {'result': 'SL', 'pnl': -POSITION_SIZE * (SL_PERCENT / 100), 'minutes': i + 1}
        
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
        return
    
    target_date = sys.argv[1]
    
    print("=" * 80)
    print(f"АНАЛИЗ ВСЕХ АНАЛИЗОВ KIRO ЗА {target_date}")
    print("=" * 80)
    print(f"💰 Position: ${POSITION_SIZE} | 📊 SL/TP: {SL_PERCENT}%/{TP_PERCENT}%")
    print()
    
    signals = parse_log_for_date(LOG_FILE, target_date)
    
    if not signals:
        print(f"❌ Нет анализов за {target_date}")
        return
    
    print(f"✅ Найдено {len(signals)} анализов")
    
    enter_signals = [s for s in signals if s['verdict'] == 'ENTER']
    skip_signals = [s for s in signals if s['verdict'] == 'SKIP']
    wait_signals = [s for s in signals if s['verdict'] == 'WAIT']
    
    print(f"   ✅ ENTER: {len(enter_signals)}")
    print(f"   🚫 SKIP: {len(skip_signals)}")
    print(f"   ⏳ WAIT: {len(wait_signals)}")
    print()
    
    all_results = []
    
    print("=" * 80)
    print("ТЕСТИРОВАНИЕ ВСЕХ СИГНАЛОВ")
    print("=" * 80)
    print()
    
    for i, sig in enumerate(signals, 1):
        verdict_emoji = {"ENTER": "✅", "SKIP": "🚫", "WAIT": "⏳"}[sig['verdict']]
        
        print(f"[{i}/{len(signals)}] {verdict_emoji} {sig['symbol']} {sig['direction']} @ {sig['timestamp_moscow'].strftime('%H:%M')}")
        print(f"   {sig['verdict_str']} | Conf: {sig['confidence']}/10 | Entry: ${sig['entry_price']:.8f}")
        
        result = check_signal_result(sig['symbol'], sig['direction'], sig['timestamp'], sig['entry_price'])
        
        if result['result'] == 'TP':
            print(f"   ✅ TP {result['minutes']}мин | +${result['pnl']:.2f}")
        elif result['result'] == 'SL':
            print(f"   ❌ SL {result['minutes']}мин | ${result['pnl']:.2f}")
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout | ${result['pnl']:.2f}")
        else:
            print(f"   ❓ {result['result']}")
        
        print()
        all_results.append({'signal': sig, 'result': result})
        time.sleep(0.3)
    
    print("=" * 80)
    print("ИТОГИ")
    print("=" * 80)
    print()
    
    tp_count = len([r for r in all_results if r['result']['result'] == 'TP'])
    sl_count = len([r for r in all_results if r['result']['result'] == 'SL'])
    timeout_count = len([r for r in all_results if r['result']['result'] == 'TIMEOUT'])
    total_pnl = sum(r['result']['pnl'] for r in all_results)
    
    print(f"📊 Всего: {len(all_results)} | ✅ TP: {tp_count} | ❌ SL: {sl_count} | ⏳ Timeout: {timeout_count}")
    
    if tp_count + sl_count > 0:
        winrate = tp_count / (tp_count + sl_count) * 100
        print(f"🎯 Winrate: {winrate:.1f}%")
    
    print(f"💰 Total PnL: ${total_pnl:+.2f}")
    print()
    
    # По вердиктам
    for verdict_type, verdict_name in [("ENTER", "✅ ВХОДИТЬ"), ("SKIP", "🚫 НЕ ВХОДИТЬ"), ("WAIT", "⏳ ЖДАТЬ")]:
        verdict_results = [r for r in all_results if r['signal']['verdict'] == verdict_type]
        
        if not verdict_results:
            continue
        
        tp = len([r for r in verdict_results if r['result']['result'] == 'TP'])
        sl = len([r for r in verdict_results if r['result']['result'] == 'SL'])
        pnl = sum(r['result']['pnl'] for r in verdict_results)
        
        print(f"{verdict_name} ({len(verdict_results)}шт): TP={tp} SL={sl}", end="")
        
        if tp + sl > 0:
            wr = tp / (tp + sl) * 100
            print(f" WR={wr:.1f}%", end="")
        
        print(f" PnL=${pnl:+.2f}")
    
    print("=" * 80)


if __name__ == "__main__":
    main()
