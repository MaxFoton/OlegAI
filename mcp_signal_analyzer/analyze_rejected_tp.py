#!/usr/bin/env python3
"""
Анализ упущенных прибылей: REJECTED → TP сигналы
Находит почему анализатор/модель были не правы
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


def parse_ntfy_message(msg: dict) -> dict:
    """Парсит ntfy сообщение"""
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
    
    # Фильтр: только REJECTED (не входить/ждать)
    verdict_type = None
    if "⚠️ НЕ ВХОДИТЬ" in verdict_str or "🚫 НЕ ШОРТИТЬ" in verdict_str:
        verdict_type = "SKIP"
    elif "⏳ ЖДАТЬ" in verdict_str:
        verdict_type = "WAIT"
    
    if not verdict_type:
        return None
    
    # Парсим цену
    price_match = re.search(r'💰 Цена: \$([0-9.]+)', message_text)
    if not price_match:
        return None
    
    entry_price = float(price_match.group(1))
    
    # Парсим score
    score_match = re.search(r'📈 Score: (\d+)/15', message_text)
    score = int(score_match.group(1)) if score_match else None
    
    return {
        'timestamp': timestamp,
        'symbol': symbol,
        'direction': direction,
        'verdict': verdict_type,
        'confidence': confidence,
        'entry_price': entry_price,
        'score': score,
        'message': message_text
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


# Список упущенных прибылей из backtest (вручную, из предыдущего результата)
REJECTED_TP_SIGNALS = [
    ('ZBCN', 'LONG', datetime(2026, 8, 10, 3, 4)),
    ('BLESS', 'LONG', datetime(2026, 8, 10, 3, 4)),
    ('ZK', 'SHORT', datetime(2026, 8, 10, 4, 51)),
    ('PNUT', 'LONG', datetime(2026, 8, 10, 8, 3)),
    ('SXT', 'SHORT', datetime(2026, 8, 10, 9, 29)),
]


def main():
    print("=" * 80)
    print("АНАЛИЗ УПУЩЕННЫХ ПРИБЫЛЕЙ: REJECTED → TP")
    print("=" * 80)
    print()
    
    # Получаем сообщения
    messages = fetch_ntfy_messages()
    
    if not messages:
        print("❌ Не удалось получить сообщения")
        return
    
    # Парсим все REJECTED сигналы
    all_rejected = []
    for msg in messages:
        parsed = parse_ntfy_message(msg)
        if parsed:
            all_rejected.append(parsed)
    
    print(f"Найдено {len(all_rejected)} REJECTED сигналов")
    print()
    
    # Анализируем упущенные прибыли
    print("=" * 80)
    print("ДЕТАЛЬНЫЙ АНАЛИЗ УПУЩЕННЫХ ПРИБЫЛЕЙ")
    print("=" * 80)
    print()
    
    for symbol, direction, timestamp in REJECTED_TP_SIGNALS:
        print(f"{'='*80}")
        print(f"💰 {symbol} {direction} @ {timestamp.strftime('%d.%m %H:%M')} (REJECTED, но дал бы TP)")
        print(f"{'='*80}")
        
        # Находим в rejected
        rejected_data = None
        for sig in all_rejected:
            if (sig['symbol'] == symbol and 
                sig['direction'] == direction and
                abs((sig['timestamp'] - timestamp).total_seconds()) < 300):
                rejected_data = sig
                break
        
        if not rejected_data:
            print("⚠️ Не найдено в ntfy")
            print()
            continue
        
        print(f"Verdict: {rejected_data['verdict']}")
        print(f"Confidence: {rejected_data['confidence']}/10")
        print(f"Score: {rejected_data['score']}/15")
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
        
        # Извлекаем причины ПРОТИВ из message
        reasons_against = []
        in_against_section = False
        
        for line in rejected_data['message'].split('\n'):
            if '❌ ПРОТИВ:' in line:
                in_against_section = True
                continue
            
            if in_against_section:
                if '🎯' in line or 'ВХОДИТЬ' in line or 'ШОРТИТЬ' in line or 'ЖДАТЬ' in line:
                    break
                
                if line.strip().startswith('•'):
                    reason = line.strip()[1:].strip()
                    reasons_against.append(reason)
        
        print("🚨 ПРИЧИНЫ ОТКЛОНЕНИЯ (что было ПРОТИВ):")
        if reasons_against:
            for r in reasons_against:
                print(f"  • {r}")
        else:
            print("  (нет явных причин ПРОТИВ)")
        
        print()
        
        # Анализ: почему отклонили?
        print("🔍 АНАЛИЗ ОШИБКИ:")
        
        analysis = []
        
        # Проверяем каждый индикатор
        if direction == "LONG":
            if indicators.get('1h_trend', '').startswith('-'):
                analysis.append(f"  ❌ 1h trend={indicators['1h_trend']} был ПРОТИВ → НО сигнал дал TP!")
            
            if indicators.get('lor') == 'SHORT':
                analysis.append(f"  ❌ Lorentzian={indicators['lor']} pred={indicators.get('lor_pred')} был ПРОТИВ → НО сигнал дал TP!")
            
            if indicators.get('ema') == 'bearish':
                analysis.append(f"  ❌ EMA={indicators['ema']} был ПРОТИВ → НО сигнал дал TP!")
            
            if indicators.get('vw_macd') == 'bearish':
                analysis.append(f"  ❌ VW-MACD={indicators['vw_macd']} был ПРОТИВ → НО сигнал дал TP!")
            
            # Проверяем что было ЗА
            if indicators.get('1h_trend', '+').replace('+', '').replace('%', ''):
                try:
                    trend_val = float(indicators.get('1h_trend', '0').replace('+', '').replace('%', ''))
                    if trend_val > 0:
                        analysis.append(f"  ✅ 1h trend={indicators['1h_trend']} был ЗА LONG")
                except:
                    pass
            
            if indicators.get('lor') == 'LONG':
                analysis.append(f"  ✅ Lorentzian={indicators['lor']} pred={indicators.get('lor_pred')} был ЗА LONG")
            
            if indicators.get('ema') == 'bullish':
                analysis.append(f"  ✅ EMA={indicators['ema']} был ЗА LONG")
            
            if indicators.get('vw_macd') == 'bullish':
                analysis.append(f"  ✅ VW-MACD={indicators['vw_macd']} был ЗА LONG")
            
            if indicators.get('phase') in ['EXHAUSTION', 'EARLY_EXPANSION']:
                analysis.append(f"  ✅ Phase={indicators['phase']} был ХОРОШ для LONG")
        
        elif direction == "SHORT":
            if indicators.get('1h_trend', '+').replace('+', '').replace('%', ''):
                try:
                    trend_val = float(indicators.get('1h_trend', '0').replace('+', '').replace('%', ''))
                    if trend_val > 0:
                        analysis.append(f"  ❌ 1h trend={indicators['1h_trend']} был ПРОТИВ → НО сигнал дал TP!")
                except:
                    pass
            
            if indicators.get('lor') == 'LONG':
                analysis.append(f"  ❌ Lorentzian={indicators['lor']} pred={indicators.get('lor_pred')} был ПРОТИВ → НО сигнал дал TP!")
            
            if indicators.get('ema') == 'bullish':
                analysis.append(f"  ❌ EMA={indicators['ema']} был ПРОТИВ → НО сигнал дал TP!")
            
            if indicators.get('vw_macd') == 'bullish':
                analysis.append(f"  ❌ VW-MACD={indicators['vw_macd']} был ПРОТИВ → НО сигнал дал TP!")
            
            # Проверяем что было ЗА
            if indicators.get('1h_trend', '').startswith('-'):
                analysis.append(f"  ✅ 1h trend={indicators['1h_trend']} был ЗА SHORT")
            
            if indicators.get('lor') == 'SHORT':
                analysis.append(f"  ✅ Lorentzian={indicators['lor']} pred={indicators.get('lor_pred')} был ЗА SHORT")
            
            if indicators.get('ema') == 'bearish':
                analysis.append(f"  ✅ EMA={indicators['ema']} был ЗА SHORT")
            
            if indicators.get('vw_macd') == 'bearish':
                analysis.append(f"  ✅ VW-MACD={indicators['vw_macd']} был ЗА SHORT")
            
            if indicators.get('phase') in ['LATE_EXPANSION']:
                analysis.append(f"  ✅ Phase={indicators['phase']} был ХОРОШ для SHORT")
        
        for a in analysis:
            print(a)
        
        print()
        
        # Вывод
        print("💡 ВЫВОД:")
        
        # Подсчёт индикаторов ЗА vs ПРОТИВ
        for_count = sum(1 for a in analysis if '✅' in a)
        against_count = sum(1 for a in analysis if '❌' in a)
        
        if against_count > for_count:
            print(f"  Индикаторы ПРОТИВ ({against_count}) > ЗА ({for_count})")
            print(f"  → Анализатор был прав в отклонении по индикаторам")
            print(f"  → Но сигнал всё равно дал TP - случайность или что-то упущено?")
        elif for_count > against_count:
            print(f"  Индикаторы ЗА ({for_count}) > ПРОТИВ ({against_count})")
            print(f"  → Анализатор был НЕПРАВ в отклонении!")
            print(f"  → Возможно слишком жёсткие штрафы?")
        else:
            print(f"  Индикаторы 50/50 (ЗА={for_count}, ПРОТИВ={against_count})")
            print(f"  → Неоднозначный сигнал")
        
        print()
        print("=" * 80)
        print()


if __name__ == "__main__":
    main()
