#!/usr/bin/env python3
"""
Калибровка порогов сканера по историческим анализам
Использует данные за 11-15.08.2026 из kiro_signal_analysis.log
"""

import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from ccxt import bybit
import time

LOG_FILE = Path("/home/max/freqtrade/logs/kiro_signal_analysis.log")
POSITION_SIZE = 1250
TP_PERCENT = 1
SL_PERCENT = 1
CHECK_PERIOD_MINUTES = 60

exchange = bybit({'enableRateLimit': True})

# Даты для анализа
DATES = ["2026-08-11", "2026-08-12", "2026-08-14", "2026-08-15"]


def get_entry_price_from_api(symbol: str, timestamp: datetime) -> float:
    """Получить цену входа = close свечи в момент сигнала"""
    try:
        bybit_symbol = f"{symbol}/USDT:USDT"
        since = int(timestamp.timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(bybit_symbol, '1m', since=since, limit=1)
        
        if ohlcv and len(ohlcv) > 0:
            return float(ohlcv[0][4])
        return None
    except:
        return None


def parse_log_signals(log_file: Path, target_date: str):
    """Парсит лог и извлекает все анализы за дату"""
    signals = []
    
    with log_file.open('r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines):
        if not line.startswith(target_date):
            continue
        
        if "✅ Sent to ntfy: 📊" not in line:
            continue
        
        ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not ts_match:
            continue
        
        timestamp_moscow = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
        timestamp_utc = timestamp_moscow - timedelta(hours=3)
        
        ntfy_match = re.search(r'📊 (\w+) (LONG|SHORT) \| (.+?) \((-?\d+)/10\)', line)
        if not ntfy_match:
            continue
        
        symbol = ntfy_match.group(1)
        direction = ntfy_match.group(2)
        verdict_str = ntfy_match.group(3)
        confidence = int(ntfy_match.group(4))
        
        verdict = None
        if "✅ ВХОДИТЬ" in verdict_str or "✅ ШОРТИТЬ" in verdict_str:
            verdict = "ENTER"
        elif "🚫 НЕ ВХОДИТЬ" in verdict_str or "🚫 НЕ ШОРТИТЬ" in verdict_str:
            verdict = "SKIP"
        elif "⏳ ЖДАТЬ" in verdict_str:
            verdict = "WAIT"
        
        if not verdict:
            continue
        
        # Ищем score в логе (ищем строку с "Analyzing")
        score = None
        for j in range(max(0, i - 10), i):
            if f"🔍 Analyzing: {symbol}" in lines[j]:
                score_match = re.search(r'score=(\d+)', lines[j])
                if score_match:
                    score = int(score_match.group(1))
                break
        
        entry_price = get_entry_price_from_api(symbol, timestamp_utc)
        if not entry_price:
            continue
        
        signals.append({
            'timestamp': timestamp_utc,
            'timestamp_moscow': timestamp_moscow,
            'symbol': symbol,
            'direction': direction,
            'verdict': verdict,
            'confidence': confidence,
            'entry_price': entry_price,
            'score': score,
            'verdict_str': verdict_str,
            'date': target_date
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
        return {'result': 'ERROR', 'pnl': 0, 'minutes': 0}


def main():
    print("=" * 80)
    print("КАЛИБРОВКА ПОРОГОВ СКАНЕРА")
    print("Анализ данных за 11-15.08.2026")
    print("=" * 80)
    print()
    
    all_signals = []
    
    for date in DATES:
        print(f"📅 Загружаю данные за {date}...")
        signals = parse_log_signals(LOG_FILE, date)
        print(f"   Найдено: {len(signals)} сигналов")
        all_signals.extend(signals)
        time.sleep(0.5)
    
    print()
    print(f"📊 Всего сигналов: {len(all_signals)}")
    print()
    
    # Тестируем все сигналы
    results = []
    
    print("🔄 Тестирование сигналов...")
    for i, sig in enumerate(all_signals, 1):
        if i % 20 == 0:
            print(f"   Обработано: {i}/{len(all_signals)}")
        
        result = check_signal_result(sig['symbol'], sig['direction'], sig['timestamp'], sig['entry_price'])
        
        results.append({
            'signal': sig,
            'result': result
        })
        
        time.sleep(0.3)
    
    print()
    print("=" * 80)
    print("РЕЗУЛЬТАТЫ КАЛИБРОВКИ")
    print("=" * 80)
    print()
    
    # Общая статистика
    tp_count = len([r for r in results if r['result']['result'] == 'TP'])
    sl_count = len([r for r in results if r['result']['result'] == 'SL'])
    timeout_count = len([r for r in results if r['result']['result'] == 'TIMEOUT'])
    total_pnl = sum(r['result']['pnl'] for r in results)
    
    print(f"📊 Общая статистика ({len(results)} сигналов):")
    print(f"   ✅ TP: {tp_count} ({tp_count/len(results)*100:.1f}%)")
    print(f"   ❌ SL: {sl_count} ({sl_count/len(results)*100:.1f}%)")
    print(f"   ⏳ Timeout: {timeout_count}")
    
    if tp_count + sl_count > 0:
        winrate = tp_count / (tp_count + sl_count) * 100
        print(f"   🎯 Winrate: {winrate:.1f}%")
    
    print(f"   💰 Total PnL: ${total_pnl:+.2f}")
    print()
    
    # Статистика по вердиктам
    print("=" * 80)
    print("ПО ВЕРДИКТАМ")
    print("=" * 80)
    print()
    
    for verdict_type, verdict_name in [("ENTER", "✅ ВХОДИТЬ"), ("SKIP", "🚫 НЕ ВХОДИТЬ"), ("WAIT", "⏳ ЖДАТЬ")]:
        verdict_results = [r for r in results if r['signal']['verdict'] == verdict_type]
        
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
    
    # Статистика по Score (для ENTER сигналов)
    print("=" * 80)
    print("ПО SCORE СКАНЕРА (только ✅ ВХОДИТЬ)")
    print("=" * 80)
    print()
    
    enter_results = [r for r in results if r['signal']['verdict'] == 'ENTER' and r['signal']['score'] is not None]
    
    if enter_results:
        score_buckets = defaultdict(list)
        
        for r in enter_results:
            score = r['signal']['score']
            if score <= 5:
                bucket = "3-5"
            elif score <= 7:
                bucket = "6-7"
            elif score <= 9:
                bucket = "8-9"
            else:
                bucket = "10+"
            
            score_buckets[bucket].append(r)
        
        for bucket in ["3-5", "6-7", "8-9", "10+"]:
            if bucket not in score_buckets:
                continue
            
            bucket_results = score_buckets[bucket]
            tp = len([r for r in bucket_results if r['result']['result'] == 'TP'])
            sl = len([r for r in bucket_results if r['result']['result'] == 'SL'])
            pnl = sum(r['result']['pnl'] for r in bucket_results)
            
            print(f"Score {bucket} ({len(bucket_results)} сигналов):")
            print(f"   TP: {tp} | SL: {sl}", end="")
            
            if tp + sl > 0:
                wr = tp / (tp + sl) * 100
                print(f" | WR: {wr:.1f}%", end="")
            
            print(f" | PnL: ${pnl:+.2f}")
    
    print()
    
    # Статистика по дням
    print("=" * 80)
    print("ПО ДАТАМ")
    print("=" * 80)
    print()
    
    for date in DATES:
        date_results = [r for r in results if r['signal']['date'] == date]
        
        if not date_results:
            continue
        
        tp = len([r for r in date_results if r['result']['result'] == 'TP'])
        sl = len([r for r in date_results if r['result']['result'] == 'SL'])
        pnl = sum(r['result']['pnl'] for r in date_results)
        
        enter_count = len([r for r in date_results if r['signal']['verdict'] == 'ENTER'])
        
        print(f"{date} ({len(date_results)} сигналов, {enter_count} ENTER):")
        print(f"   TP: {tp} | SL: {sl}", end="")
        
        if tp + sl > 0:
            wr = tp / (tp + sl) * 100
            print(f" | WR: {wr:.1f}%", end="")
        
        print(f" | PnL: ${pnl:+.2f}")
    
    print()
    print("=" * 80)
    print("УПУЩЕННАЯ ПРИБЫЛЬ (ошибки ЖДАТЬ)")
    print("=" * 80)
    print()
    
    # Находим все WAIT сигналы которые дали бы TP
    wait_results = [r for r in results if r['signal']['verdict'] == 'WAIT']
    wait_tp_results = [r for r in wait_results if r['result']['result'] == 'TP']
    wait_sl_results = [r for r in wait_results if r['result']['result'] == 'SL']
    
    missed_profit = sum(r['result']['pnl'] for r in wait_tp_results)
    avoided_loss = sum(r['result']['pnl'] for r in wait_sl_results)
    
    print(f"⏳ ЖДАТЬ сигналов с TP: {len(wait_tp_results)} шт")
    print(f"   💰 Упущенная прибыль: ${missed_profit:+.2f}")
    print()
    print(f"⏳ ЖДАТЬ сигналов с SL: {len(wait_sl_results)} шт")
    print(f"   💸 Избежанные потери: ${avoided_loss:+.2f}")
    print()
    
    net_opportunity = missed_profit + avoided_loss
    print(f"📊 Чистая упущенная прибыль: ${net_opportunity:+.2f}")
    
    if net_opportunity > 50:
        print(f"⚠️  КРИТИЧНО! Kiro слишком осторожен - пропускает {len(wait_tp_results)} прибыльных!")
    elif net_opportunity > 20:
        print(f"⚠️  Kiro немного осторожен - можно ослабить пороги")
    elif net_opportunity < -20:
        print(f"✅ Kiro правильно фильтрует - уберёг от ${-avoided_loss:.2f} потерь")
    else:
        print(f"✅ Баланс в норме")
    
    print()
    
    # ТОП-10 упущенных профитных WAIT - С ДЕТАЛЬНЫМ АНАЛИЗОМ
    if wait_tp_results:
        print("🔝 ТОП-10 УПУЩЕННЫХ ПРОФИТНЫХ СИГНАЛОВ (ЖДАТЬ → должен был ВХОДИТЬ):")
        print()
        
        sorted_wait_tp = sorted(wait_tp_results, key=lambda x: x['result']['pnl'], reverse=True)[:10]
        
        for i, r in enumerate(sorted_wait_tp, 1):
            sig = r['signal']
            res = r['result']
            print(f"{i}. {sig['symbol']} {sig['direction']} @ {sig['timestamp_moscow'].strftime('%d.%m %H:%M')}")
            print(f"   Score: {sig['score']} | Conf: {sig['confidence']}/10")
            print(f"   TP через {res['minutes']}мин | Упущено: ${res['pnl']:+.2f}")
            print(f"   Вердикт: {sig['verdict_str']}")
            print()
    
    print("=" * 80)
    print("РЕКОМЕНДАЦИИ ПО ПОРОГАМ")
    print("=" * 80)
    print()
    
    # Анализ ENTER сигналов
    enter_results = [r for r in results if r['signal']['verdict'] == 'ENTER']
    enter_tp = len([r for r in enter_results if r['result']['result'] == 'TP'])
    enter_sl = len([r for r in enter_results if r['result']['result'] == 'SL'])
    
    if enter_tp + enter_sl > 0:
        enter_wr = enter_tp / (enter_tp + enter_sl) * 100
        
        if enter_wr < 45:
            print("⚠️  Winrate ENTER < 45% - КРИТИЧНО!")
            print("   → Повысить min_score с 5 до 7")
            print("   → Добавить min_agreement: 0.40")
            print("   → Повысить min_rr с 1.0 до 1.3")
        elif enter_wr < 50:
            print("⚠️  Winrate ENTER < 50% - нужна калибровка")
            print("   → Повысить min_score до 6")
            print("   → Добавить min_agreement: 0.35")
        elif enter_wr > 60:
            print("✅ Winrate ENTER > 60% - отлично!")
            if net_opportunity > 50:
                print(f"   ⚠️  НО упущено ${net_opportunity:.2f} - можно ослабить пороги!")
            else:
                print("   → Можно немного ослабить пороги для большего количества")
        else:
            print("✅ Winrate ENTER 50-60% - нормально")
            print("   → Текущие пороги оптимальны")
    
    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
