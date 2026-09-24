#!/usr/bin/env python3
"""
Тест новой логики с Phase весом для NOT SHORT и BIO SHORT
"""

def test_not_short():
    """NOT SHORT 15:10 - должен быть ШОРТИТЬ"""
    print("=" * 80)
    print("NOT SHORT (15:10) - Phase=LATE_EXPANSION, все индикаторы ПРОТИВ")
    print("=" * 80)
    
    direction = "SHORT"
    phase = "LATE_EXPANSION"
    rsi = 79
    volume = 34771510
    bb_trend = "+0.75%"
    lor_pred = "+3"
    ema = "bullish"
    vw_macd = "bullish"
    vwap = "+0.6% (above)"
    absorption = None
    
    confidence = 5
    reasons_for = []
    reasons_against = []
    
    # 1. HTF filter - нет блокировки
    
    # 2. 1h trend (точность 90%)
    trend_val = 0.75
    if trend_val > 0:
        penalty = -2 if phase == 'LATE_EXPANSION' else -5
        reasons_against.append(f"🛑 1h trend={bb_trend} ПРОТИВ SHORT (точность 90%)")
        confidence += penalty
    
    # 3. Lorentzian (точность 87%)
    if '+1' in lor_pred or '+3' in lor_pred:
        penalty = -2 if phase == 'LATE_EXPANSION' else -4
        reasons_against.append(f"🛑 Lorentzian pred={lor_pred} ПРОТИВ SHORT (точность 87%)")
        confidence += penalty
    
    # 4. EMA (точность 80%)
    if ema == 'bullish':
        penalty = -1 if phase == 'LATE_EXPANSION' else -3
        reasons_against.append("🛑 EMA bullish ПРОТИВ SHORT (точность 80%)")
        confidence += penalty
    
    # 5. VW-MACD (точность 73%)
    if vw_macd == 'bullish':
        penalty = -1 if phase == 'LATE_EXPANSION' else -3
        reasons_against.append("🛑 VW-MACD bullish ПРОТИВ SHORT (точность 73%)")
        confidence += penalty
    
    # Модель нет
    
    # 7. ДОПОЛНИТЕЛЬНЫЕ ФАКТОРЫ
    if absorption == 'SHORT':
        reasons_for.append("Абсорбция SHORT (крупные игроки)")
        confidence += 1
    
    if vwap and 'above' in vwap:
        reasons_for.append("Цена выше VWAP (overbought)")
        confidence += 1
    
    # 🔥 НОВАЯ ЛОГИКА: Phase=LATE_EXPANSION для SHORT
    phase_boost = 0
    rsi_extreme_overbought = rsi and rsi > 75
    
    if phase in ['LATE_EXPANSION']:
        phase_boost = 4
        reasons_for.append(f"🔥 Phase={phase} (РАСПРЕДЕЛЕНИЕ - ТОП для SHORT)")
        confidence += phase_boost
        
        # 🔥🔥 ЭКСТРЕМУМ: RSI>75 + LATE_EXPANSION + Volume
        if rsi_extreme_overbought and volume and volume > 1000000:
            extreme_boost = 2
            reasons_for.append(f"🔥🔥 ЭКСТРЕМ: RSI={rsi} + Phase + Volume - вершина!")
            confidence += extreme_boost
    
    volume_high = volume and volume > 1000000
    if volume_high:
        reasons_for.append(f"Объём ${volume} >$1M")
        confidence += 1
    
    # ВЕРДИКТ
    min_confidence = 7 if (phase == 'LATE_EXPANSION' and volume_high) else 8
    
    if confidence >= min_confidence:
        verdict = "✅ ШОРТИТЬ"
    elif confidence >= 5:
        verdict = "⏳ ЖДАТЬ"
    else:
        verdict = "🚫 НЕ ШОРТИТЬ"
    
    print(f"\n✅ ЗА ({len(reasons_for)}):")
    for r in reasons_for:
        print(f"  • {r}")
    
    print(f"\n❌ ПРОТИВ ({len(reasons_against)}):")
    for r in reasons_against:
        print(f"  • {r}")
    
    print(f"\n🎯 ВЕРДИКТ: {verdict}")
    print(f"📊 Confidence: {confidence}/10 (min: {min_confidence})")
    print(f"🔥 Phase boost: +{phase_boost}")
    print()


def test_bio_short():
    """BIO SHORT 14:28 - должен быть ШОРТИТЬ или хотя бы ВХОДИТЬ"""
    print("=" * 80)
    print("BIO SHORT (14:28) - Phase=LATE_EXPANSION, VW-MACD ЗА")
    print("=" * 80)
    
    direction = "SHORT"
    phase = "LATE_EXPANSION"
    rsi = 67
    volume = 1060070
    bb_trend = "+0.76%"
    lor_pred = "+3"
    ema = "bullish"
    vw_macd = "bearish"
    vwap = "+0.7% (above)"
    absorption = None
    
    confidence = 5
    reasons_for = []
    reasons_against = []
    
    # 1. HTF filter - нет блокировки
    
    # 2. 1h trend (точность 90%)
    trend_val = 0.76
    if trend_val > 0:
        penalty = -2 if phase == 'LATE_EXPANSION' else -5
        reasons_against.append(f"🛑 1h trend={bb_trend} ПРОТИВ SHORT (точность 90%)")
        confidence += penalty
    
    # 3. Lorentzian (точность 87%)
    if '+1' in lor_pred or '+3' in lor_pred:
        penalty = -2 if phase == 'LATE_EXPANSION' else -4
        reasons_against.append(f"🛑 Lorentzian pred={lor_pred} ПРОТИВ SHORT (точность 87%)")
        confidence += penalty
    
    # 4. EMA (точность 80%)
    if ema == 'bullish':
        penalty = -1 if phase == 'LATE_EXPANSION' else -3
        reasons_against.append("🛑 EMA bullish ПРОТИВ SHORT (точность 80%)")
        confidence += penalty
    
    # 5. VW-MACD (точность 73%)
    if vw_macd == 'bearish':
        reasons_for.append("✅ VW-MACD bearish ЗА SHORT")
        confidence += 2
    
    # Модель нет
    
    # 7. ДОПОЛНИТЕЛЬНЫЕ ФАКТОРЫ
    if absorption == 'SHORT':
        reasons_for.append("Абсорбция SHORT (крупные игроки)")
        confidence += 1
    
    if vwap and 'above' in vwap:
        reasons_for.append("Цена выше VWAP (overbought)")
        confidence += 1
    
    # 🔥 НОВАЯ ЛОГИКА: Phase=LATE_EXPANSION для SHORT
    phase_boost = 0
    rsi_extreme_overbought = rsi and rsi > 75
    
    if phase in ['LATE_EXPANSION']:
        phase_boost = 4
        reasons_for.append(f"🔥 Phase={phase} (РАСПРЕДЕЛЕНИЕ - ТОП для SHORT)")
        confidence += phase_boost
        
        # 🔥🔥 ЭКСТРЕМУМ: RSI>75 + LATE_EXPANSION + Volume
        if rsi_extreme_overbought and volume and volume > 1000000:
            extreme_boost = 2
            reasons_for.append(f"🔥🔥 ЭКСТРЕМ: RSI={rsi} + Phase + Volume - вершина!")
            confidence += extreme_boost
    
    volume_high = volume and volume > 1000000
    if volume_high:
        reasons_for.append(f"Объём ${volume} >$1M")
        confidence += 1
    
    # ВЕРДИКТ
    min_confidence = 7 if (phase == 'LATE_EXPANSION' and volume_high) else 8
    
    if confidence >= min_confidence:
        verdict = "✅ ШОРТИТЬ"
    elif confidence >= 5:
        verdict = "⏳ ЖДАТЬ"
    else:
        verdict = "🚫 НЕ ШОРТИТЬ"
    
    print(f"\n✅ ЗА ({len(reasons_for)}):")
    for r in reasons_for:
        print(f"  • {r}")
    
    print(f"\n❌ ПРОТИВ ({len(reasons_against)}):")
    for r in reasons_against:
        print(f"  • {r}")
    
    print(f"\n🎯 ВЕРДИКТ: {verdict}")
    print(f"📊 Confidence: {confidence}/10 (min: {min_confidence})")
    print(f"🔥 Phase boost: +{phase_boost}")
    print()


if __name__ == "__main__":
    print("\n🔥 ТЕСТ НОВОЙ ЛОГИКИ С PHASE ВЕСОМ И RSI ЭКСТРЕМУМАМИ\n")
    test_not_short()
    test_bio_short()
    print("✅ Тест завершен\n")
