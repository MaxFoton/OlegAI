#!/usr/bin/env python3
"""
Backtest ОТКЛОНЁННЫХ сигналов за сегодня
Проверяет что было бы если входить по сигналам с вердиктом "НЕ ВХОДИТЬ" или "ЖДАТЬ"
Цель: проверить не слишком ли строгие фильтры
"""

import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from ccxt import bybit
import time

# Конфигурация
LOG_FILE = Path("/home/max/freqtrade/logs/signal_analysis.log")
POSITION_SIZE = 350  # $
RISK_PERCENT = 1  # 1% SL/TP

# API
exchange = bybit({'enableRateLimit': True})


def check_signal_result(symbol: str, direction: str, entry_time: datetime, entry_price: float, sl_percent: float, tp_percent: float) -> dict:
    """
    Проверяет что случилось с сигналом: TP или SL
    """
    try:
        # Конвертируем символ для bybit
        bybit_symbol = f"{symbol}/USDT:USDT"
        
        # Получаем свечи на 30 минут вперед
        since = int(entry_time.timestamp() * 1000)
        ohlcv = exchange.fetch_ohlcv(bybit_symbol, '1m', since=since, limit=30)
        
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
            ts = datetime.fromtimestamp(candle[0] / 1000)
            low = candle[2]
            high = candle[1]
            
            if direction == "LONG":
                # Проверяем TP (цена выросла)
                if high >= tp_price:
                    pnl = POSITION_SIZE * (tp_percent / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1, 'hit_price': high, 'hit_time': ts}
                
                # Проверяем SL (цена упала)
                if low <= sl_price:
                    pnl = -POSITION_SIZE * (sl_percent / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1, 'hit_price': low, 'hit_time': ts}
            
            else:  # SHORT
                # Проверяем TP (цена упала)
                if low <= tp_price:
                    pnl = POSITION_SIZE * (tp_percent / 100)
                    return {'result': 'TP', 'pnl': pnl, 'minutes': i + 1, 'hit_price': low, 'hit_time': ts}
                
                # Проверяем SL (цена выросла)
                if high >= sl_price:
                    pnl = -POSITION_SIZE * (sl_percent / 100)
                    return {'result': 'SL', 'pnl': pnl, 'minutes': i + 1, 'hit_price': high, 'hit_time': ts}
        
        # Не достиг ни TP ни SL за 30 минут
        last_price = ohlcv[-1][4]
        if direction == "LONG":
            unrealized_pnl = POSITION_SIZE * ((last_price - entry_price) / entry_price)
        else:
            unrealized_pnl = POSITION_SIZE * ((entry_price - last_price) / entry_price)
        
        return {'result': 'TIMEOUT', 'pnl': unrealized_pnl, 'minutes': 30, 'last_price': last_price}
    
    except Exception as e:
        print(f"  ❌ Ошибка проверки {symbol}: {e}")
        return {'result': 'ERROR', 'pnl': 0, 'minutes': 0}


def parse_log_for_rejected_trades():
    """
    Парсит лог анализа и находит все сделки с вердиктом "НЕ ВХОДИТЬ" или "ЖДАТЬ"
    Затем ищет цену в signal_queue.json
    """
    # Сначала парсим лог
    rejected_from_log = []
    
    with LOG_FILE.open('r') as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines):
        if "ANALYZED:" not in line:
            continue
        
        # Парсим timestamp
        timestamp_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not timestamp_match:
            continue
        
        timestamp_str = timestamp_match.group(1)
        timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
        
        # ТОЛЬКО СЕГОДНЯ (08.08.2026)
        if timestamp.date() != datetime(2026, 8, 8).date():
            continue
        
        # Парсим: "ANALYZED: SYMBOL DIRECTION | VERDICT (confidence/10)"
        analyzed_match = re.search(r'ANALYZED: (\w+) (LONG|SHORT) \| (.+?) \((-?\d+)/10\)', line)
        if not analyzed_match:
            continue
        
        symbol = analyzed_match.group(1)
        direction = analyzed_match.group(2)
        verdict_str = analyzed_match.group(3)
        confidence = int(analyzed_match.group(4))
        
        # Только если вердикт НЕ ВХОДИТЬ или ЖДАТЬ
        is_skip = False
        verdict_type = None
        
        if "⚠️ НЕ ВХОДИТЬ" in verdict_str or "🚫 НЕ ШОРТИТЬ" in verdict_str:
            is_skip = True
            verdict_type = "SKIP"
        elif "⏳ ЖДАТЬ" in verdict_str:
            is_skip = True
            verdict_type = "WAIT"
        
        if not is_skip:
            continue
        
        # Ищем причины против в следующих 20 строках
        reasons_against = []
        for j in range(i, min(i + 20, len(lines))):
            if "❌ ПРОТИВ:" in lines[j]:
                # Читаем следующие строки с причинами
                for k in range(j+1, min(j+10, len(lines))):
                    if lines[k].strip().startswith("•"):
                        reason = lines[k].strip()[1:].strip()
                        if "🛑" in reason:
                            reasons_against.append(reason)
                    elif "🎯" in lines[k]:  # Конец секции ПРОТИВ
                        break
                break
        
        rejected_from_log.append({
            'timestamp': timestamp,
            'symbol': symbol,
            'direction': direction,
            'confidence': confidence,
            'verdict': verdict_type,
            'reasons_against': reasons_against
        })
    
    # Теперь ищем цены в signal_queue.json
    queue_file = Path("/home/max/freqtrade/.kiro/signal_queue.json")
    
    with queue_file.open('r') as f:
        queue = json.load(f)
    
    trades = []
    
    for rejected in rejected_from_log:
        # Ищем в очереди по символу, направлению и примерному времени
        # ВАЖНО: queue в UTC, лог в UTC+3, поэтому конвертируем
        rejected_utc = rejected['timestamp'] - timedelta(hours=3)
        
        for item in queue:
            sig = item['signal']
            sig_time = datetime.fromisoformat(item['timestamp'])
            
            # Совпадение по символу, направлению и времени (в пределах 5 минут)
            if (sig['symbol'] == rejected['symbol'] and 
                sig['direction'] == rejected['direction'] and
                abs((sig_time - rejected_utc).total_seconds()) < 300):
                
                rejected['entry_price'] = sig['price']
                trades.append(rejected)
                break
    
    return trades


def main():
    print("=" * 80)
    print("BACKTEST ОТКЛОНЁННЫХ СИГНАЛОВ ЗА СЕГОДНЯ 08.08.2026")
    print("=" * 80)
    print(f"Position: ${POSITION_SIZE}")
    print(f"SL: {RISK_PERCENT}% | TP: {RISK_PERCENT}%")
    print(f"Цель: проверить не слишком ли строгие фильтры")
    print()
    
    # Парсим лог и находим все REJECTED сделки
    trades_data = parse_log_for_rejected_trades()
    
    if not trades_data:
        print("❌ Не найдено отклонённых сигналов за сегодня")
        return
    
    print(f"Найдено {len(trades_data)} отклонённых сигналов (НЕ ВХОДИТЬ/ЖДАТЬ)")
    print()
    
    trades_results = []
    
    for trade in trades_data:
        symbol = trade['symbol']
        direction = trade['direction']
        entry_price = trade['entry_price']
        confidence = trade['confidence']
        timestamp = trade['timestamp']
        verdict = trade['verdict']
        reasons = trade['reasons_against']
        
        verdict_emoji = "⚠️" if verdict == "SKIP" else "⏳"
        print(f"{verdict_emoji} {symbol} {direction} @ {timestamp.strftime('%d.%m %H:%M:%S')}")
        print(f"   Confidence: {confidence}/10 | Verdict: {verdict}")
        print(f"   Entry: ${entry_price:.8f}")
        
        # Показываем главные причины отклонения
        if reasons:
            print(f"   Причины против:")
            for r in reasons[:3]:  # Показываем топ-3
                # Убираем эмодзи для краткости
                clean_reason = r.replace("🛑", "").strip()
                if "EMA" in clean_reason:
                    print(f"     • {clean_reason}")
                elif "Lorentzian" in clean_reason:
                    print(f"     • {clean_reason}")
                elif "1h trend" in clean_reason:
                    print(f"     • {clean_reason}")
                elif "VW-MACD" in clean_reason:
                    print(f"     • {clean_reason}")
        
        result = check_signal_result(symbol, direction, timestamp, entry_price, RISK_PERCENT, RISK_PERCENT)
        
        if result['result'] == 'TP':
            print(f"   ✅ TP после {result['minutes']} минут")
            print(f"   💰 Упущенная прибыль: +${result['pnl']:.2f}")
        elif result['result'] == 'SL':
            print(f"   ❌ SL после {result['minutes']} минут")
            print(f"   ✅ Правильно отклонён! (убыток ${result['pnl']:.2f})")
        elif result['result'] == 'TIMEOUT':
            print(f"   ⏳ Timeout после 30 минут")
            print(f"   📊 Unrealized: ${result['pnl']:.2f}")
        else:
            print(f"   ❓ {result['result']}")
        
        print()
        
        trades_results.append({
            'symbol': symbol,
            'direction': direction,
            'timestamp': timestamp,
            'confidence': confidence,
            'verdict': verdict,
            'result': result['result'],
            'pnl': result['pnl'],
            'minutes': result.get('minutes', 0),
            'reasons': reasons
        })
        
        time.sleep(0.5)  # Rate limit
    
    # Статистика
    print()
    print("=" * 80)
    print("ИТОГИ")
    print("=" * 80)
    
    tp_count = len([t for t in trades_results if t['result'] == 'TP'])
    sl_count = len([t for t in trades_results if t['result'] == 'SL'])
    timeout_count = len([t for t in trades_results if t['result'] == 'TIMEOUT'])
    
    total_pnl = sum(t['pnl'] for t in trades_results)
    
    print(f"Всего отклонённых: {len(trades_results)}")
    print()
    print(f"Если бы вошли:")
    print(f"  ✅ TP: {tp_count} ({tp_count/len(trades_results)*100:.1f}%) - упущенная прибыль")
    print(f"  ❌ SL: {sl_count} ({sl_count/len(trades_results)*100:.1f}%) - правильно отклонены")
    print(f"  ⏳ Timeout: {timeout_count} ({timeout_count/len(trades_results)*100:.1f}%)")
    print()
    
    if tp_count + sl_count > 0:
        wrong_reject_rate = (tp_count / (tp_count + sl_count)) * 100
        print(f"📊 Ошибочные отклонения: {wrong_reject_rate:.1f}% (TP из TP+SL)")
        print(f"📊 Правильные отклонения: {100-wrong_reject_rate:.1f}% (SL из TP+SL)")
    
    print(f"💰 Упущенная прибыль: ${total_pnl:+.2f}")
    print()
    
    # Анализ индикаторов
    print("=" * 80)
    print("АНАЛИЗ: Какие индикаторы чаще всего блокируют сигналы?")
    print("=" * 80)
    
    ema_against = [t for t in trades_results if any("EMA" in r for r in t['reasons'])]
    lor_against = [t for t in trades_results if any("Lorentzian" in r for r in t['reasons'])]
    trend_against = [t for t in trades_results if any("1h trend" in r for r in t['reasons'])]
    macd_against = [t for t in trades_results if any("VW-MACD" in r for r in t['reasons'])]
    
    print(f"EMA против: {len(ema_against)} сигналов")
    if ema_against:
        ema_tp = len([t for t in ema_against if t['result'] == 'TP'])
        ema_sl = len([t for t in ema_against if t['result'] == 'SL'])
        if ema_tp + ema_sl > 0:
            ema_wrong = (ema_tp / (ema_tp + ema_sl)) * 100
            print(f"  → Ошибочно блокировал: {ema_wrong:.1f}% (TP из TP+SL)")
            print(f"  → Правильно блокировал: {100-ema_wrong:.1f}% (SL из TP+SL)")
    
    print()
    print(f"Lorentzian против: {len(lor_against)} сигналов")
    if lor_against:
        lor_tp = len([t for t in lor_against if t['result'] == 'TP'])
        lor_sl = len([t for t in lor_against if t['result'] == 'SL'])
        if lor_tp + lor_sl > 0:
            lor_wrong = (lor_tp / (lor_tp + lor_sl)) * 100
            print(f"  → Ошибочно блокировал: {lor_wrong:.1f}%")
            print(f"  → Правильно блокировал: {100-lor_wrong:.1f}%")
    
    print()
    print(f"1h trend против: {len(trend_against)} сигналов")
    if trend_against:
        trend_tp = len([t for t in trend_against if t['result'] == 'TP'])
        trend_sl = len([t for t in trend_against if t['result'] == 'SL'])
        if trend_tp + trend_sl > 0:
            trend_wrong = (trend_tp / (trend_tp + trend_sl)) * 100
            print(f"  → Ошибочно блокировал: {trend_wrong:.1f}%")
            print(f"  → Правильно блокировал: {100-trend_wrong:.1f}%")
    
    print()
    print(f"VW-MACD против: {len(macd_against)} сигналов")
    if macd_against:
        macd_tp = len([t for t in macd_against if t['result'] == 'TP'])
        macd_sl = len([t for t in macd_against if t['result'] == 'SL'])
        if macd_tp + macd_sl > 0:
            macd_wrong = (macd_tp / (macd_tp + macd_sl)) * 100
            print(f"  → Ошибочно блокировал: {macd_wrong:.1f}%")
            print(f"  → Правильно блокировал: {100-macd_wrong:.1f}%")
    
    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
