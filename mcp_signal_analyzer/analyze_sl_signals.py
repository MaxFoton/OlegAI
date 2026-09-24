#!/usr/bin/env python3
"""
Детальный анализ SL сигналов
Находит что было общего у всех провальных сделок
"""

import json
import re
import requests
from datetime import datetime
from pathlib import Path

# Конфигурация
NTFY_URL = "http://87.121.218.4:8080/max-analysis/json?poll=1&since=48h"
QUEUE_FILE = Path("/home/max/freqtrade/.kiro/signal_queue.json")


def fetch_ntfy_messages():
    """Получает сообщения из ntfy"""
    try:
        response = requests.get(NTFY_URL, timeout=10)
        lines = response.text.strip().split('\n')
        
        messages = []
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                messages.append(msg)
            except:
                continue
        
        return messages
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return []


def parse_ntfy_message(msg: dict, verdict_filter: str = 'ENTER') -> dict:
    """Детально парсит ntfy сообщение"""
    title = msg.get('title', '')
    message_text = msg.get('message', '')
    timestamp = datetime.fromtimestamp(msg.get('time', 0))
    
    # Парсим title
    title_match = re.search(r'📊 (\w+) (LONG|SHORT) \| (.+?) \((-?\d+)/10\)', title)
    if not title_match:
        return None
    
    symbol = title_match.group(1)
    direction = title_match.group(2)
    verdict_str = title_match.group(3)
    confidence = int(title_match.group(4))
    
    # Фильтр по вердикту
    verdict_type = None
    if "✅ ВХОДИТЬ" in verdict_str or "✅ ШОРТИТЬ" in verdict_str:
        verdict_type = "ENTER"
    elif "⚠️ НЕ ВХОДИТЬ" in verdict_str or "🚫 НЕ ШОРТИТЬ" in verdict_str:
        verdict_type = "SKIP"
    elif "⏳ ЖДАТЬ" in verdict_str:
        verdict_type = "WAIT"
    
    if verdict_filter and verdict_type != verdict_filter and verdict_filter != 'ALL':
        return None
    
    # Парсим цену
    price_match = re.search(r'Цена: \$([0-9.]+)', message_text)
    if not price_match:
        price_match = re.search(r'Вход: \$([0-9.]+)', message_text)
    
    if not price_match:
        return None
    
    entry_price = float(price_match.group(1))
    
    # Парсим score
    score_match = re.search(r'Score: (\d+)/15', message_text)
    score = int(score_match.group(1)) if score_match else None
    
    # Парсим индикаторы ЗА
    reasons_for = []
    in_for_section = False
    
    for line in message_text.split('\n'):
        if '✅ ЗА:' in line:
            in_for_section = True
            continue
        
        if in_for_section:
            if '❌ ПРОТИВ:' in line or '🎯' in line:
                break
            
            if line.strip().startswith('•'):
                reason = line.strip()[1:].strip()
                # Извлекаем ключевые индикаторы
                if '1h trend' in reason:
                    trend_match = re.search(r'1h trend=([+-]?\d+\.\d+%)', reason)
                    if trend_match:
                        reasons_for.append(f"1h_trend:{trend_match.group(1)}")
                
                if 'Lorentzian' in reason:
                    lor_match = re.search(r'Lorentzian pred=([+-]?\d+)', reason)
                    if lor_match:
                        reasons_for.append(f"Lorentzian:{lor_match.group(1)}")
                
                if 'EMA' in reason:
                    if 'bullish' in reason:
                        reasons_for.append("EMA:bullish")
                    elif 'bearish' in reason:
                        reasons_for.append("EMA:bearish")
                
                if 'VW-MACD' in reason:
                    if 'bullish' in reason:
                        reasons_for.append("VW-MACD:bullish")
                    elif 'bearish' in reason:
                        reasons_for.append("VW-MACD:bearish")
                
                if 'Phase=' in reason:
                    phase_match = re.search(r'Phase=(\w+)', reason)
                    if phase_match:
                        reasons_for.append(f"Phase:{phase_match.group(1)}")
    
    return {
        'timestamp': timestamp,
        'symbol': symbol,
        'direction': direction,
        'verdict': verdict_type,
        'confidence': confidence,
        'entry_price': entry_price,
        'score': score,
        'indicators_for': reasons_for
    }


def find_signal_in_queue(symbol: str, direction: str, timestamp: datetime):
    """Находит сигнал в очереди и возвращает full_text"""
    with QUEUE_FILE.open('r') as f:
        queue = json.load(f)
    
    # Конвертируем timestamp в UTC (из UTC+3)
    from datetime import timedelta
    timestamp_utc = timestamp - timedelta(hours=3)
    
    for item in queue:
        sig = item['signal']
        sig_time = datetime.fromisoformat(item['timestamp'])
        
        if (sig['symbol'] == symbol and 
            sig['direction'] == direction and
            abs((sig_time - timestamp_utc).total_seconds()) < 300):
            return sig.get('full_text', sig.get('details', ''))
    
    return None


def extract_all_indicators(full_text: str) -> dict:
    """Извлекает ВСЕ индикаторы из полного текста сигнала"""
    indicators = {}
    
    for line in full_text.split('\n'):
        # RSI
        if 'RSI:' in line:
            try:
                indicators['rsi'] = int(line.split('RSI:')[1].strip().split()[0])
            except:
                pass
        
        # Phase
        if 'Phase:' in line:
            try:
                indicators['phase'] = line.split('Phase:')[1].strip().split()[0]
            except:
                pass
        
        # 1h trend
        if '1h trend:' in line:
            try:
                indicators['1h_trend'] = line.split('1h trend:')[1].strip().split()[0]
            except:
                pass
        
        # Lorentzian
        if 'Lor:' in line:
            try:
                lor_text = line.split('Lor:')[1].strip()
                # Извлекаем направление
                if '+1' in lor_text or '+3' in lor_text:
                    indicators['lor'] = 'LONG'
                elif '-1' in lor_text or '-3' in lor_text:
                    indicators['lor'] = 'SHORT'
                else:
                    indicators['lor'] = 'NEUTRAL'
                
                # Извлекаем pred
                if 'pred:' in lor_text:
                    pred = lor_text.split('pred:')[1].split()[0]
                    indicators['lor_pred'] = pred
            except:
                pass
        
        # EMA
        if 'EMA:' in line:
            if 'bullish' in line:
                indicators['ema'] = 'bullish'
            elif 'bearish' in line:
                indicators['ema'] = 'bearish'
        
        # VW-MACD
        if 'VW-MACD:' in line:
            if 'bullish' in line:
                indicators['vw_macd'] = 'bullish'
            elif 'bearish' in line:
                indicators['vw_macd'] = 'bearish'
        
        # VWAP
        if 'VWAP:' in line:
            if 'above' in line:
                indicators['vwap'] = 'above'
            elif 'below' in line:
                indicators['vwap'] = 'below'
        
        # Absorption
        if 'Absorption:' in line:
            abs_text = line.split('Absorption:')[1].strip()
            if 'LONG' in abs_text:
                indicators['absorption'] = 'LONG'
            elif 'SHORT' in abs_text:
                indicators['absorption'] = 'SHORT'
    
    return indicators


def main():
    print("=" * 80)
    print("АНАЛИЗ ВСЕХ СИГНАЛОВ: ENTER vs REJECTED")
    print("=" * 80)
    print()
    
    # Получаем сообщения
    messages = fetch_ntfy_messages()
    
    if not messages:
        print("❌ Не удалось получить сообщения")
        return
    
    # Парсим ВСЕ сигналы
    all_signals = []
    for msg in messages:
        parsed = parse_ntfy_message(msg, verdict_filter='ALL')
        if parsed:
            all_signals.append(parsed)
    
    enter_signals = [s for s in all_signals if s['verdict'] == 'ENTER']
    rejected_signals = [s for s in all_signals if s['verdict'] in ['SKIP', 'WAIT']]
    
    print(f"Найдено {len(enter_signals)} ENTER сигналов")
    print(f"Найдено {len(rejected_signals)} REJECTED сигналов")
    print()
    
    # Список SL сигналов из backtest (вручную) - ENTER
    enter_sl_signals = [
        ('AXL', 'SHORT', datetime(2026, 8, 10, 2, 33)),
        ('BROCCOLI', 'SHORT', datetime(2026, 8, 10, 5, 10)),
        ('ICNT', 'SHORT', datetime(2026, 8, 10, 6, 13)),
        ('ENA', 'SHORT', datetime(2026, 8, 10, 8, 24)),
    ]
    
    # Список TP сигналов из REJECTED (вручную) - которые дали бы TP
    rejected_tp_signals = [
        ('ZBCN', 'LONG', datetime(2026, 8, 10, 3, 4)),
        ('BLESS', 'LONG', datetime(2026, 8, 10, 3, 4)),
        ('ZK', 'SHORT', datetime(2026, 8, 10, 4, 51)),
        ('PNUT', 'LONG', datetime(2026, 8, 10, 8, 3)),
        ('SXT', 'SHORT', datetime(2026, 8, 10, 9, 29)),
    ]
    
    print("=" * 80)
    print("1️⃣ АНАЛИЗ ENTER → SL (провалы)")
    print("=" * 80)
    print()
    
    enter_sl_analysis = []
    
    for symbol, direction, timestamp in enter_sl_signals:
        print(f"{'='*80}")
        print(f"❌ {symbol} {direction} @ {timestamp.strftime('%d.%m %H:%M')}")
        print(f"{'='*80}")
        
        # Находим в ntfy
        ntfy_data = None
        for sig in enter_signals:
            if (sig['symbol'] == symbol and 
                sig['direction'] == direction and
                abs((sig['timestamp'] - timestamp).total_seconds()) < 300):
                ntfy_data = sig
                break
        
        if not ntfy_data:
            print("⚠️ Не найдено в ntfy")
            print()
            continue
        
        print(f"Confidence: {ntfy_data['confidence']}/10")
        print(f"Score: {ntfy_data['score']}/15")
        print(f"Entry: ${ntfy_data['entry_price']}")
        print()
        
        # Получаем полный текст из queue
        full_text = find_signal_in_queue(symbol, direction, timestamp)
        
        if not full_text:
            print("⚠️ Не найдено в queue")
            print()
            continue
        
        # Извлекаем все индикаторы
        indicators = extract_all_indicators(full_text)
        
        print("Индикаторы:")
        for key, value in indicators.items():
            print(f"  {key}: {value}")
        print()
        
        # Проверяем что было ПРОТИВ направления
        wrong_indicators = []
        
        if direction == "LONG":
            if indicators.get('1h_trend', '').startswith('-'):
                wrong_indicators.append(f"❌ 1h trend ПРОТИВ ({indicators['1h_trend']})")
            if indicators.get('lor') == 'SHORT':
                wrong_indicators.append(f"❌ Lorentzian ПРОТИВ (SHORT pred:{indicators.get('lor_pred')})")
            if indicators.get('ema') == 'bearish':
                wrong_indicators.append("❌ EMA bearish ПРОТИВ LONG")
            if indicators.get('vw_macd') == 'bearish':
                wrong_indicators.append("❌ VW-MACD bearish ПРОТИВ LONG")
        
        elif direction == "SHORT":
            if indicators.get('1h_trend', '+').replace('+', '').replace('%', '') and float(indicators.get('1h_trend', '0').replace('+', '').replace('%', '')) > 0:
                wrong_indicators.append(f"❌ 1h trend ПРОТИВ ({indicators.get('1h_trend')})")
            if indicators.get('lor') == 'LONG':
                wrong_indicators.append(f"❌ Lorentzian ПРОТИВ (LONG pred:{indicators.get('lor_pred')})")
            if indicators.get('ema') == 'bullish':
                wrong_indicators.append("❌ EMA bullish ПРОТИВ SHORT")
            if indicators.get('vw_macd') == 'bullish':
                wrong_indicators.append("❌ VW-MACD bullish ПРОТИВ SHORT")
        
        if wrong_indicators:
            print("🚨 Индикаторы которые были ПРОТИВ:")
            for w in wrong_indicators:
                print(f"  {w}")
        else:
            print("✅ Все индикаторы были ЗА направление")
        
        print()
        
        enter_sl_analysis.append({
            'symbol': symbol,
            'direction': direction,
            'indicators': indicators,
            'wrong_indicators': wrong_indicators
        })
    
    # Анализ REJECTED → TP
    print("=" * 80)
    print("2️⃣ АНАЛИЗ REJECTED → TP (упущенная прибыль)")
    print("=" * 80)
    print()
    
    rejected_tp_analysis = []
    
    for symbol, direction, timestamp in rejected_tp_signals:
        print(f"{'='*80}")
        print(f"✅ {symbol} {direction} @ {timestamp.strftime('%d.%m %H:%M')} (был отклонён, но дал бы TP)")
        print(f"{'='*80}")
        
        # Находим в rejected
        rejected_data = None
        for sig in rejected_signals:
            if (sig['symbol'] == symbol and 
                sig['direction'] == direction and
                abs((sig['timestamp'] - timestamp).total_seconds()) < 300):
                rejected_data = sig
                break
        
        if not rejected_data:
            print("⚠️ Не найдено в rejected")
            print()
            continue
        
        print(f"Verdict: {rejected_data['verdict']}")
        print(f"Confidence: {rejected_data['confidence']}/10")
        print(f"Entry: ${rejected_data['entry_price']}")
        print()
        
        # Получаем полный текст из queue
        full_text = find_signal_in_queue(symbol, direction, timestamp)
        
        if not full_text:
            print("⚠️ Не найдено в queue")
            print()
            continue
        
        # Извлекаем все индикаторы
        indicators = extract_all_indicators(full_text)
        
        print("Индикаторы:")
        for key, value in indicators.items():
            print(f"  {key}: {value}")
        print()
        
        rejected_tp_analysis.append({
            'symbol': symbol,
            'direction': direction,
            'indicators': indicators
        })
    
    # Статистика ENTER SL
    print("=" * 80)
    print("СТАТИСТИКА ENTER → SL: Какие индикаторы врали?")
    print("=" * 80)
    print()
    
    # Подсчитываем сколько раз каждый индикатор был ПРОТИВ
    ema_wrong_count = sum(1 for s in enter_sl_analysis if any('EMA' in w for w in s['wrong_indicators']))
    lor_wrong_count = sum(1 for s in enter_sl_analysis if any('Lorentzian' in w for w in s['wrong_indicators']))
    trend_wrong_count = sum(1 for s in enter_sl_analysis if any('1h trend' in w for w in s['wrong_indicators']))
    macd_wrong_count = sum(1 for s in enter_sl_analysis if any('VW-MACD' in w for w in s['wrong_indicators']))
    
    enter_total = len(enter_sl_analysis)
    
    print(f"Всего ENTER → SL: {enter_total}")
    print()
    print(f"EMA был ПРОТИВ: {ema_wrong_count}/{enter_total} ({ema_wrong_count/enter_total*100:.1f}%)")
    print(f"Lorentzian был ПРОТИВ: {lor_wrong_count}/{enter_total} ({lor_wrong_count/enter_total*100:.1f}%)")
    print(f"1h trend был ПРОТИВ: {trend_wrong_count}/{enter_total} ({trend_wrong_count/enter_total*100:.1f}%)")
    print(f"VW-MACD был ПРОТИВ: {macd_wrong_count}/{enter_total} ({macd_wrong_count/enter_total*100:.1f}%)")
    print()
    
    # Статистика Phase для ENTER SL
    phase_counts_enter_sl = {}
    for s in enter_sl_analysis:
        phase = s['indicators'].get('phase', 'UNKNOWN')
        phase_counts_enter_sl[phase] = phase_counts_enter_sl.get(phase, 0) + 1
    
    print("Phase в ENTER → SL:")
    for phase, count in phase_counts_enter_sl.items():
        print(f"  {phase}: {count}")
    print()
    
    # Статистика Phase для REJECTED TP
    print("=" * 80)
    print("СТАТИСТИКА REJECTED → TP: Phase анализ")
    print("=" * 80)
    print()
    
    rejected_total = len(rejected_tp_analysis)
    print(f"Всего REJECTED → TP (упущенная прибыль): {rejected_total}")
    print()
    
    phase_counts_rejected_tp = {}
    for s in rejected_tp_analysis:
        phase = s['indicators'].get('phase', 'UNKNOWN')
        phase_counts_rejected_tp[phase] = phase_counts_rejected_tp.get(phase, 0) + 1
    
    print("Phase в REJECTED → TP:")
    for phase, count in phase_counts_rejected_tp.items():
        print(f"  {phase}: {count}")
    print()
    # Вывод
    print("=" * 80)
    print("ВЫВОДЫ")
    print("=" * 80)
    print()
    
    # Сравниваем Phase между ENTER SL и REJECTED TP
    print("Сравнение Phase:")
    print(f"  ENTER → SL чаще всего: {max(phase_counts_enter_sl, key=phase_counts_enter_sl.get) if phase_counts_enter_sl else 'N/A'}")
    print(f"  REJECTED → TP чаще всего: {max(phase_counts_rejected_tp, key=phase_counts_rejected_tp.get) if phase_counts_rejected_tp else 'N/A'}")
    print()
    
    # Проверяем гипотезу про EXHAUSTION
    exhaustion_in_enter_sl = phase_counts_enter_sl.get('EXHAUSTION', 0)
    exhaustion_in_rejected_tp = phase_counts_rejected_tp.get('EXHAUSTION', 0)
    
    if exhaustion_in_enter_sl > 0 and exhaustion_in_rejected_tp == 0:
        print("🚨 КРИТИЧЕСКАЯ НАХОДКА:")
        print(f"  Phase=EXHAUSTION встречается в {exhaustion_in_enter_sl} ENTER → SL")
        print(f"  Но НЕ встречается в REJECTED → TP!")
        print()
        print("  ⚠️ EXHAUSTION для SHORT = ЛОВУШКА!")
        print("  Рекомендация: БЛОКИРОВАТЬ SHORT при Phase=EXHAUSTION")
    elif exhaustion_in_enter_sl == 0 and exhaustion_in_rejected_tp > 0:
        print("✅ Phase=EXHAUSTION работает ХОРОШО!")
        print(f"  Встречается в {exhaustion_in_rejected_tp} упущенных прибылях")
    
    print()
    
    if ema_wrong_count == 0 and lor_wrong_count == 0 and trend_wrong_count == 0 and macd_wrong_count == 0:
        print("🤔 ВСЕ индикаторы были ЗА направление, но сделки проиграли!")
        print("Возможно проблема в:")
        print("  - Timing (слишком поздно вошли)")
        print("  - Volatility (слишком узкий SL 1%)")
        print("  - External factors (новости, манипуляции)")
    else:
        max_wrong = max(ema_wrong_count, lor_wrong_count, trend_wrong_count, macd_wrong_count)
        
        if ema_wrong_count == max_wrong:
            print(f"🚨 EMA - ГЛАВНЫЙ ВИНОВНИК! ({ema_wrong_count}/{enter_total})")
            print("Рекомендация: НЕ снижать штраф для EMA даже при Phase")
        elif lor_wrong_count == max_wrong:
            print(f"🚨 Lorentzian - ГЛАВНЫЙ ВИНОВНИК! ({lor_wrong_count}/{enter_total})")
        elif trend_wrong_count == max_wrong:
            print(f"🚨 1h trend - ГЛАВНЫЙ ВИНОВНИК! ({trend_wrong_count}/{enter_total})")
        elif macd_wrong_count == max_wrong:
            print(f"🚨 VW-MACD - ГЛАВНЫЙ ВИНОВНИК! ({macd_wrong_count}/{enter_total})")
    
    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
