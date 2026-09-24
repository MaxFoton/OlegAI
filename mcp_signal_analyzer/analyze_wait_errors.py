#!/usr/bin/env python3
"""
Детальный анализ ошибок ЖДАТЬ - какие индикаторы были ЗА вход, но Kiro сказал ждать
"""

import re
from pathlib import Path
from datetime import datetime, timedelta

LOG_FILE = Path("/home/max/freqtrade/logs/kiro_signal_analysis.log")


def parse_wait_errors(log_file: Path, target_date: str):
    """Парсит лог и находит все ЖДАТЬ сигналы с детальной инфой"""
    errors = []
    
    with log_file.open('r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        if not line.startswith(target_date):
            i += 1
            continue
        
        # Ищем "✅ Sent to ntfy: 📊 SYMBOL DIRECTION | ⏳ ЖДАТЬ"
        if "✅ Sent to ntfy: 📊" not in line or "⏳ ЖДАТЬ" not in line:
            i += 1
            continue
        
        # Парсим timestamp
        ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not ts_match:
            i += 1
            continue
        
        timestamp_moscow = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
        
        # Парсим символ и направление
        ntfy_match = re.search(r'📊 (\w+) (LONG|SHORT) \| (.+?) \((-?\d+)/10\)', line)
        if not ntfy_match:
            i += 1
            continue
        
        symbol = ntfy_match.group(1)
        direction = ntfy_match.group(2)
        verdict_str = ntfy_match.group(3)
        confidence = int(ntfy_match.group(4))
        
        # Теперь ищем в обратном порядке детальную инфу
        # Ищем "🔍 LONG/SHORT reasons_for=X reasons_against=Y"
        reasons_for = []
        reasons_against = []
        
        for j in range(i - 1, max(0, i - 30), -1):
            prev_line = lines[j]
            
            # Находим reasons
            if f"🔍 {direction} reasons_for=" in prev_line:
                reasons_match = re.search(r'reasons_for=(\d+) reasons_against=(\d+)', prev_line)
                if reasons_match:
                    num_for = int(reasons_match.group(1))
                    num_against = int(reasons_match.group(2))
            
            # Находим список ЗА
            if "🔍 ЗА:" in prev_line:
                za_match = re.search(r"🔍 ЗА: (\[.+?\])", prev_line)
                if za_match:
                    try:
                        reasons_for = eval(za_match.group(1))
                    except:
                        pass
            
            # Находим список ПРОТИВ
            if "🔍 ПРОТИВ:" in prev_line:
                protiv_match = re.search(r"🔍 ПРОТИВ: (\[.+?\])", prev_line)
                if protiv_match:
                    try:
                        reasons_against = eval(protiv_match.group(1))
                    except:
                        pass
            
            # Если нашли "🔍 Analyzing:" значит закончился блок этого сигнала
            if "🔍 Analyzing:" in prev_line and symbol in prev_line:
                break
        
        errors.append({
            'timestamp': timestamp_moscow,
            'symbol': symbol,
            'direction': direction,
            'confidence': confidence,
            'reasons_for': reasons_for,
            'reasons_against': reasons_against,
            'verdict_str': verdict_str
        })
        
        i += 1
    
    return errors


def main():
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python analyze_wait_errors.py YYYY-MM-DD")
        return
    
    target_date = sys.argv[1]
    
    print("=" * 80)
    print(f"АНАЛИЗ ОШИБОК ЖДАТЬ за {target_date}")
    print("=" * 80)
    print()
    
    errors = parse_wait_errors(LOG_FILE, target_date)
    
    if not errors:
        print(f"❌ Нет сигналов ЖДАТЬ за {target_date}")
        return
    
    print(f"Найдено {len(errors)} сигналов ЖДАТЬ")
    print()
    
    # Группируем по confidence
    by_confidence = {}
    for err in errors:
        conf = err['confidence']
        if conf not in by_confidence:
            by_confidence[conf] = []
        by_confidence[conf].append(err)
    
    print("=" * 80)
    print("РАСПРЕДЕЛЕНИЕ ПО CONFIDENCE")
    print("=" * 80)
    print()
    
    for conf in sorted(by_confidence.keys()):
        print(f"Confidence {conf}/10: {len(by_confidence[conf])} сигналов")
    
    print()
    print("=" * 80)
    print("ДЕТАЛЬНЫЙ АНАЛИЗ СИГНАЛОВ ЖДАТЬ")
    print("=" * 80)
    print()
    
    for i, err in enumerate(errors[:20], 1):  # Показываем первые 20
        print(f"{i}. {err['symbol']} {err['direction']} @ {err['timestamp'].strftime('%d.%m %H:%M')}")
        print(f"   Confidence: {err['confidence']}/10")
        print(f"   ЗА вход ({len(err['reasons_for'])}):")
        for reason in err['reasons_for']:
            print(f"      ✅ {reason}")
        
        print(f"   ПРОТИВ входа ({len(err['reasons_against'])}):")
        if err['reasons_against']:
            for reason in err['reasons_against']:
                print(f"      ❌ {reason}")
        else:
            print(f"      (нет)")
        
        print(f"   Вердикт: {err['verdict_str']}")
        print()
    
    if len(errors) > 20:
        print(f"... и ещё {len(errors) - 20} сигналов")
        print()
    
    print("=" * 80)
    print("СТАТИСТИКА ПРИЧИН")
    print("=" * 80)
    print()
    
    # Считаем частоту каждой причины ЗА
    reason_counts_for = {}
    for err in errors:
        for reason in err['reasons_for']:
            reason_counts_for[reason] = reason_counts_for.get(reason, 0) + 1
    
    print("ТОП причин ЗА вход:")
    for reason, count in sorted(reason_counts_for.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"   {count:3d}x - {reason}")
    
    print()
    
    # Считаем частоту каждой причины ПРОТИВ
    reason_counts_against = {}
    for err in errors:
        for reason in err['reasons_against']:
            reason_counts_against[reason] = reason_counts_against.get(reason, 0) + 1
    
    if reason_counts_against:
        print("ТОП причин ПРОТИВ входа:")
        for reason, count in sorted(reason_counts_against.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"   {count:3d}x - {reason}")
    else:
        print("Причин ПРОТИВ: нет")
    
    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
