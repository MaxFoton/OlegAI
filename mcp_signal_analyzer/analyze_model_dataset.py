#!/usr/bin/env python3
"""
Анализ датасета модели: какие сигналы работают лучше
"""

import json
from collections import defaultdict
from pathlib import Path

DATASET_FILE = Path("/home/max/o_p/dex_scanner/data/directional_samples.jsonl")

def load_dataset():
    """Загрузить датасет"""
    samples = []
    
    with DATASET_FILE.open('r') as f:
        for line in f:
            if line.strip():
                try:
                    sample = json.loads(line)
                    samples.append(sample)
                except:
                    pass
    
    return samples


def analyze_by_features(samples):
    """Анализ успешности по различным признакам"""
    
    # Группировка по Phase
    by_phase = defaultdict(lambda: {'total': 0, 'success': 0})
    
    # Группировка по RSI ranges
    by_rsi = defaultdict(lambda: {'total': 0, 'success': 0})
    
    # Группировка по Lorentzian направлению
    by_lor = defaultdict(lambda: {'total': 0, 'success': 0})
    
    # Группировка по Absorption
    by_abs = defaultdict(lambda: {'total': 0, 'success': 0})
    
    # Противотрендовый риск
    by_contratrend = defaultdict(lambda: {'total': 0, 'success': 0})
    
    for sample in samples:
        features = sample.get('features', {})
        outcome = sample.get('outcome')  # LONG или SHORT
        return_pct = sample.get('return_pct', 0)
        
        if not outcome:
            continue
        
        # Success если return > 0.35% (порог модели)
        success = return_pct > 0.35
        
        # Phase
        if features.get('phase_early'):
            phase = 'EARLY_EXPANSION'
        elif features.get('phase_mid'):
            phase = 'MID_EXPANSION'
        elif features.get('phase_late'):
            phase = 'LATE_EXPANSION'
        elif features.get('phase_exhaustion'):
            phase = 'EXHAUSTION'
        else:
            phase = 'UNKNOWN'
        
        by_phase[f"{phase}_{outcome}"]['total'] += 1
        if success:
            by_phase[f"{phase}_{outcome}"]['success'] += 1
        
        # RSI
        rsi = features.get('rsi', 50)
        if rsi < 30:
            rsi_range = '<30_oversold'
        elif rsi < 40:
            rsi_range = '30-40'
        elif rsi < 60:
            rsi_range = '40-60_neutral'
        elif rsi < 70:
            rsi_range = '60-70'
        else:
            rsi_range = '>70_overbought'
        
        by_rsi[f"{rsi_range}_{outcome}"]['total'] += 1
        if success:
            by_rsi[f"{rsi_range}_{outcome}"]['success'] += 1
        
        # Lorentzian
        lor_signal = features.get('lor_signal', 0)
        lor_str = features.get('lor_strength', 0)
        
        if lor_signal > 0:
            lor = f"LONG_str{int(lor_str*100)}"
        elif lor_signal < 0:
            lor = f"SHORT_str{int(abs(lor_str)*100)}"
        else:
            lor = "NEUTRAL"
        
        by_lor[f"{lor}_{outcome}"]['total'] += 1
        if success:
            by_lor[f"{lor}_{outcome}"]['success'] += 1
        
        # Absorption
        abs_long = features.get('absorption_long', False)
        abs_short = features.get('absorption_short', False)
        
        if abs_long:
            absorption = "LONG"
        elif abs_short:
            absorption = "SHORT"
        else:
            absorption = "NONE"
        
        by_abs[f"{absorption}_{outcome}"]['total'] += 1
        if success:
            by_abs[f"{absorption}_{outcome}"]['success'] += 1
        
        # Contratrend risk
        contratrend = features.get('contratrend_risk', 0)
        
        if contratrend > 0.7:
            risk = "HIGH"
        elif contratrend > 0.4:
            risk = "MEDIUM"
        else:
            risk = "LOW"
        
        by_contratrend[f"{risk}_{outcome}"]['total'] += 1
        if success:
            by_contratrend[f"{risk}_{outcome}"]['success'] += 1
    
    return {
        'phase': by_phase,
        'rsi': by_rsi,
        'lorentzian': by_lor,
        'absorption': by_abs,
        'contratrend_risk': by_contratrend
    }


def print_analysis(analysis):
    """Вывести анализ"""
    
    for category, data in analysis.items():
        print(f"\n{'=' * 80}")
        print(f"АНАЛИЗ ПО: {category.upper()}")
        print(f"{'=' * 80}")
        
        # Сортируем по winrate
        items = []
        for key, stats in data.items():
            if stats['total'] >= 20:  # Минимум 20 сэмплов
                winrate = (stats['success'] / stats['total']) * 100
                items.append((key, stats['total'], winrate))
        
        items.sort(key=lambda x: x[2], reverse=True)
        
        print(f"\n{'Условие':<40} {'Сэмплов':<10} {'Winrate':<10}")
        print(f"{'-' * 60}")
        
        for key, total, winrate in items[:15]:  # Топ 15
            emoji = "✅" if winrate > 55 else "❌" if winrate < 50 else "⚠️"
            print(f"{emoji} {key:<38} {total:<10} {winrate:>6.1f}%")


def main():
    print("=" * 80)
    print("АНАЛИЗ ДАТАСЕТА МОДЕЛИ")
    print("=" * 80)
    print()
    
    print("Загрузка датасета...")
    samples = load_dataset()
    
    print(f"Загружено {len(samples)} сэмплов\n")
    
    # Общая статистика
    total_success = sum(1 for s in samples if s.get('return_pct', 0) > 0.35)
    overall_winrate = (total_success / len(samples)) * 100
    
    longs = [s for s in samples if s.get('outcome') == 'LONG']
    shorts = [s for s in samples if s.get('outcome') == 'SHORT']
    
    long_wins = sum(1 for s in longs if s.get('return_pct', 0) > 0.35)
    short_wins = sum(1 for s in shorts if s.get('return_pct', 0) > 0.35)
    
    print(f"Общий winrate: {overall_winrate:.1f}%")
    print(f"  LONG: {len(longs)} сэмплов, winrate: {(long_wins/len(longs)*100) if longs else 0:.1f}%")
    print(f"  SHORT: {len(shorts)} сэмплов, winrate: {(short_wins/len(shorts)*100) if shorts else 0:.1f}%")
    print()
    
    # Анализ по признакам
    analysis = analyze_by_features(samples)
    print_analysis(analysis)
    
    print("\n" + "=" * 80)
    print("РЕКОМЕНДАЦИИ:")
    print("=" * 80)
    print()
    print("На основе анализа, входить стоит когда:")
    print("  ✅ Низкий contratrend_risk")
    print("  ✅ RSI в экстремальных зонах для соответствующего направления")
    print("  ✅ Phase совпадает с направлением (EARLY для LONG, LATE для SHORT)")
    print("  ✅ Есть absorption в направлении сигнала")
    print()
    print("Избегать когда:")
    print("  ❌ Высокий contratrend_risk")
    print("  ❌ Lorentzian против направления с высокой силой")
    print("  ❌ Phase против направления")
    print()


if __name__ == "__main__":
    main()
