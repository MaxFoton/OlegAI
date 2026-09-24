#!/usr/bin/env python3
"""
Analyze which indicators correctly predicted direction
"""

# Format: (symbol, direction, result, rsi, phase, lor_dir, lor_str, macd, absorption, vwap, ema)
signals = [
    ("COOKIE", "LONG", "TP", 55, "EARLY_EXPANSION", "NEUTRAL", 54, "bullish", "LONG", "+1.1%", "bullish"),
    ("BEL", "LONG", "SL", 60, "EARLY_EXPANSION", "LONG", 83, "bullish", None, "-1.2%", "bullish"),
    ("KMNO", "LONG", "SL", 88, "EARLY_EXPANSION", "NEUTRAL", 100, "bullish", "LONG", "-2.0%", "bullish"),
    ("CARV", "LONG", "SL", 23, "EXHAUSTION", "NEUTRAL", 20, "bullish", None, "-0.5%", "bearish"),
    ("PUMPFUN", "SHORT", "SL", 58, "LATE_EXPANSION", "LONG", 38, "bearish", None, "+0.5%", "bearish"),
    ("BSB", "LONG", "TP", 60, "EARLY_EXPANSION", "LONG", 100, "bullish", None, "+4.8%", "bullish"),
    ("BIGTIME", "LONG", "SL", 13, "EARLY_EXPANSION", "SHORT", 49, "bearish", "LONG", "-0.9%", "bearish"),
    ("FARTCOIN", "LONG", "SL", 21, "EARLY_EXPANSION", "SHORT", 36, "bearish", "LONG", "-0.7%", "bearish"),
    ("ZORA", "LONG", "SL", 39, "EARLY_EXPANSION", "SHORT", 32, "bearish", "LONG", "-0.9%", "bearish"),
    ("SKL", "SHORT", "SL", 94, "LATE_EXPANSION", "LONG", 29, "bullish", "SHORT", "+1.0%", "bullish"),
    ("ONDO", "LONG", "SL", 15, "EARLY_EXPANSION", "SHORT", 83, "bearish", "LONG", "-1.2%", "bearish"),
]

def check_indicator(name, is_bullish, direction, result):
    """Check if indicator prediction was correct"""
    is_win = (result == "TP")
    
    # Indicator correct if:
    # - bullish & LONG & win OR
    # - bearish & LONG & loss OR  
    # - bullish & SHORT & loss OR
    # - bearish & SHORT & win
    
    if direction == "LONG":
        correct = (is_bullish and is_win) or (not is_bullish and not is_win)
    else:  # SHORT
        correct = (not is_bullish and is_win) or (is_bullish and not is_win)
    
    return correct

print("="*80)
print("АНАЛИЗ ТОЧНОСТИ ИНДИКАТОРОВ")
print("Какие индикаторы правильно предсказывали направление?")
print("="*80)

# Track stats
indicator_stats = {
    'rsi': {'correct': 0, 'wrong': 0, 'details': []},
    'phase': {'correct': 0, 'wrong': 0, 'details': []},
    'lorentzian': {'correct': 0, 'wrong': 0, 'details': []},
    'macd': {'correct': 0, 'wrong': 0, 'details': []},
    'absorption': {'correct': 0, 'wrong': 0, 'details': []},
    'vwap': {'correct': 0, 'wrong': 0, 'details': []},
    'ema': {'correct': 0, 'wrong': 0, 'details': []},
}

for symbol, direction, result, rsi, phase, lor_dir, lor_str, macd, absorption, vwap, ema in signals:
    is_win = (result == "TP")
    
    print(f"\n{'='*80}")
    print(f"📊 {symbol} {direction} - {result}")
    print(f"RSI: {rsi} | Phase: {phase} | Lor: {lor_dir}({lor_str}%)")
    print(f"MACD: {macd} | Absorption: {absorption} | VWAP: {vwap} | EMA: {ema}")
    
    # RSI
    if direction == "LONG":
        rsi_bullish = rsi < 40  # Oversold
    else:
        rsi_bullish = rsi > 60  # Overbought
    
    rsi_correct = check_indicator("RSI", rsi_bullish, direction, result)
    
    # Phase
    bullish_phases = ["EARLY_EXPANSION", "MID_EXPANSION", "ACCUMULATION"]
    phase_bullish = phase in bullish_phases
    phase_correct = check_indicator("Phase", phase_bullish, direction, result)
    
    # Lorentzian
    lor_bullish = lor_dir == "LONG"
    lor_correct = check_indicator("Lorentzian", lor_bullish, direction, result)
    
    # MACD
    macd_bullish = "bullish" in macd.lower()
    macd_correct = check_indicator("MACD", macd_bullish, direction, result)
    
    # Absorption (if present)
    if absorption:
        abs_bullish = absorption == "LONG"
        abs_correct = check_indicator("Absorption", abs_bullish, direction, result)
    else:
        abs_correct = None
    
    # VWAP
    vwap_val = float(vwap.replace("%", "").replace("+", ""))
    if direction == "LONG":
        vwap_bullish = vwap_val < 0  # Price below VWAP (oversold for LONG)
    else:
        vwap_bullish = vwap_val > 0  # Price above VWAP (overbought for SHORT)
    vwap_correct = check_indicator("VWAP", vwap_bullish, direction, result)
    
    # EMA
    ema_bullish = "bullish" in ema.lower()
    ema_correct = check_indicator("EMA", ema_bullish, direction, result)
    
    print(f"\n{'✅ Правильно' if is_win else '❌ Неправильно'}:")
    print(f"  RSI: {'✅' if rsi_correct else '❌'} ({rsi}, {'бычий' if rsi_bullish else 'медвежий'})")
    print(f"  Phase: {'✅' if phase_correct else '❌'} ({phase})")
    print(f"  Lorentzian: {'✅' if lor_correct else '❌'} ({lor_dir} {lor_str}%)")
    print(f"  MACD: {'✅' if macd_correct else '❌'} ({macd})")
    if abs_correct is not None:
        print(f"  Absorption: {'✅' if abs_correct else '❌'} ({absorption})")
    print(f"  VWAP: {'✅' if vwap_correct else '❌'} ({vwap})")
    print(f"  EMA: {'✅' if ema_correct else '❌'} ({ema})")
    
    # Update stats
    indicator_stats['rsi']['correct'] += rsi_correct
    indicator_stats['rsi']['wrong'] += not rsi_correct
    indicator_stats['rsi']['details'].append(f"{symbol}: RSI={rsi} {'✅' if rsi_correct else '❌'}")
    
    indicator_stats['phase']['correct'] += phase_correct
    indicator_stats['phase']['wrong'] += not phase_correct
    indicator_stats['phase']['details'].append(f"{symbol}: {phase} {'✅' if phase_correct else '❌'}")
    
    indicator_stats['lorentzian']['correct'] += lor_correct
    indicator_stats['lorentzian']['wrong'] += not lor_correct
    indicator_stats['lorentzian']['details'].append(f"{symbol}: {lor_dir}({lor_str}%) {'✅' if lor_correct else '❌'}")
    
    indicator_stats['macd']['correct'] += macd_correct
    indicator_stats['macd']['wrong'] += not macd_correct
    indicator_stats['macd']['details'].append(f"{symbol}: {macd} {'✅' if macd_correct else '❌'}")
    
    if abs_correct is not None:
        indicator_stats['absorption']['correct'] += abs_correct
        indicator_stats['absorption']['wrong'] += not abs_correct
        indicator_stats['absorption']['details'].append(f"{symbol}: {absorption} {'✅' if abs_correct else '❌'}")
    
    indicator_stats['vwap']['correct'] += vwap_correct
    indicator_stats['vwap']['wrong'] += not vwap_correct
    indicator_stats['vwap']['details'].append(f"{symbol}: {vwap} {'✅' if vwap_correct else '❌'}")
    
    indicator_stats['ema']['correct'] += ema_correct
    indicator_stats['ema']['wrong'] += not ema_correct
    indicator_stats['ema']['details'].append(f"{symbol}: {ema} {'✅' if ema_correct else '❌'}")

# Summary
print(f"\n\n{'='*80}")
print("📊 ИТОГИ: Точность индикаторов (от лучшего к худшему)")
print("="*80)

sorted_indicators = sorted(
    indicator_stats.items(),
    key=lambda x: x[1]['correct']/(x[1]['correct']+x[1]['wrong']) if x[1]['correct']+x[1]['wrong'] > 0 else 0,
    reverse=True
)

for indicator, stats in sorted_indicators:
    total = stats['correct'] + stats['wrong']
    if total > 0:
        accuracy = stats['correct'] / total * 100
        print(f"\n{indicator.upper():15} - {accuracy:5.1f}% ({stats['correct']}/{total})")
        if accuracy >= 60:
            print(f"  ✅ ХОРОШИЙ индикатор - использовать!")
        elif accuracy >= 50:
            print(f"  ⚠️  Средний индикатор")
        else:
            print(f"  ❌ ПЛОХОЙ индикатор - игнорировать!")

print("\n" + "="*80)
print("РЕКОМЕНДАЦИИ:")
print("="*80)

# Find best indicators
best = [(name, stats) for name, stats in sorted_indicators 
        if (stats['correct'] + stats['wrong']) > 0 
        and stats['correct'] / (stats['correct'] + stats['wrong']) >= 0.6]

if best:
    print("\n✅ ИСПОЛЬЗОВАТЬ эти индикаторы (точность ≥60%):")
    for name, stats in best:
        total = stats['correct'] + stats['wrong']
        accuracy = stats['correct'] / total * 100
        print(f"   • {name.upper()}: {accuracy:.1f}%")
else:
    print("\n❌ НЕТ надёжных индикаторов (все <60% точности)")

# Find worst indicators
worst = [(name, stats) for name, stats in sorted_indicators 
         if (stats['correct'] + stats['wrong']) > 0 
         and stats['correct'] / (stats['correct'] + stats['wrong']) < 0.5]

if worst:
    print("\n❌ ИГНОРИРОВАТЬ эти индикаторы (точность <50%):")
    for name, stats in worst:
        total = stats['correct'] + stats['wrong']
        accuracy = stats['correct'] / total * 100
        print(f"   • {name.upper()}: {accuracy:.1f}% - ВРЁТ!")

print("\n" + "="*80)
