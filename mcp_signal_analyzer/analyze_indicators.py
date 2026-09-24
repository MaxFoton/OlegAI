#!/usr/bin/env python3
"""
Глубокий анализ: какие индикаторы показывали правильное направление
"""

# Результаты backtesta (TP = правильно, SL = неправильно)
signals = [
    {
        "symbol": "COOKIE",
        "direction": "LONG",
        "result": "TP",  # ✅ Правильно
        "time": "2026-08-06 19:11",
        "indicators": {
            "Phase": "EARLY_EXPANSION",
            "RSI": 55,
            "Lorentzian": "NEUTRAL (pred:-1)",  # Против
            "Absorption": "LONG",  # ЗА
            "VWAP": "+1.1% above",  # ЗА
            "VW-MACD": "bullish",  # ЗА
            "EMA": "bullish",  # ЗА
            "1h_trend": "+3.19%",  # ЗА
        }
    },
    {
        "symbol": "BEL",
        "direction": "LONG",
        "result": "SL",  # ❌ Неправильно
        "time": "2026-08-06 19:34",
        "indicators": {
            "Phase": "EARLY_EXPANSION",
            "RSI": 60,
            "Lorentzian": "LONG (pred:+1)",  # ЗА
            "Absorption": "нет",  # Нейтрально
            "VWAP": "нет данных",
            "VW-MACD": "нет данных",
            "EMA": "нет данных",
            "1h_trend": "нет данных",
        }
    },
    {
        "symbol": "KMNO",
        "direction": "LONG",
        "result": "SL",  # ❌ Неправильно (на futures)
        "time": "2026-08-07 05:17",
        "indicators": {
            "Phase": "EARLY_EXPANSION",
            "RSI": 88,  # ПЕРЕКУПЛЕН!
            "Lorentzian": "NEUTRAL (pred:-1)",  # ПРОТИВ!
            "Absorption": "LONG",  # ЗА
            "VWAP": "-2.0% below",  # ЗА
            "VW-MACD": "bullish",  # ЗА
            "EMA": "bullish",  # ЗА
            "1h_trend": "+11.95%",  # ЗА (но может быть поздно)
        }
    },
    {
        "symbol": "CARV",
        "direction": "LONG",
        "result": "SL",  # ❌ Неправильно
        "time": "2026-08-07 05:43",
        "indicators": {
            "Phase": "EXHAUSTION",  # Дно
            "RSI": 23,  # Перепродан
            "Lorentzian": "NEUTRAL (pred:+1)",  # ЗА
            "Absorption": "нет",
            "VWAP": "-0.5% below",  # ЗА
            "VW-MACD": "bullish",  # ЗА
            "EMA": "bearish",  # ПРОТИВ!
            "1h_trend": "-0.90%",  # ПРОТИВ!
        }
    },
    {
        "symbol": "PUMPFUN",
        "direction": "SHORT",
        "result": "SL",  # ❌ Неправильно
        "time": "2026-08-07 05:47",
        "indicators": {
            "Phase": "LATE_EXPANSION",  # Вершина
            "RSI": 58,  # Не перекуплен!
            "Lorentzian": "LONG (pred:+3)",  # ПРОТИВ!
            "Absorption": "нет",
            "VWAP": "+0.5% above",  # ЗА
            "VW-MACD": "bearish",  # ЗА
            "EMA": "bearish",  # ЗА
            "1h_trend": "+0.30%",  # Слабый рост
        }
    },
    {
        "symbol": "BSB",
        "direction": "LONG",
        "result": "TP",  # ✅ Правильно
        "time": "2026-08-07 06:04",
        "indicators": {
            "Phase": "EARLY_EXPANSION",
            "RSI": 60,
            "Lorentzian": "LONG (pred:+3)",  # ЗА
            "Absorption": "нет",
            "VWAP": "+4.8% above",  # ЗА (хотя высоко)
            "VW-MACD": "bullish X",  # ЗА (кроссовер!)
            "EMA": "bullish",  # ЗА
            "1h_trend": "+7.14%",  # ЗА
            "OI": "+6.8%",  # ЗА (новые лонги)
        }
    },
    {
        "symbol": "BIGTIME",
        "direction": "LONG",
        "result": "SL",  # ❌ Неправильно
        "time": "2026-08-07 06:43",
        "indicators": {
            "Phase": "EARLY_EXPANSION",
            "RSI": 13,  # ПЕРЕПРОДАН!
            "Lorentzian": "SHORT (pred:-3)",  # ПРОТИВ!
            "Absorption": "LONG",  # ЗА
            "VWAP": "-0.9% below",  # ЗА
            "VW-MACD": "bearish",  # ПРОТИВ!
            "EMA": "bearish",  # ПРОТИВ!
            "1h_trend": "-1.52%",  # ПРОТИВ!
        }
    },
    {
        "symbol": "FARTCOIN",
        "direction": "LONG",
        "result": "SL",  # ❌ Неправильно
        "time": "2026-08-07 06:59",
        "indicators": {
            "Phase": "EARLY_EXPANSION",
            "RSI": 21,  # ПЕРЕПРОДАН!
            "Lorentzian": "SHORT (pred:-1)",  # ПРОТИВ!
            "Absorption": "LONG",  # ЗА
            "VWAP": "-0.7% below",  # ЗА
            "VW-MACD": "bearish",  # ПРОТИВ!
            "EMA": "bearish",  # ПРОТИВ!
            "1h_trend": "-1.20%",  # ПРОТИВ!
        }
    },
    {
        "symbol": "ZORA",
        "direction": "LONG",
        "result": "SL",  # ❌ Неправильно
        "time": "2026-08-07 07:30",
        "indicators": {
            "Phase": "EARLY_EXPANSION",
            "RSI": 39,
            "Lorentzian": "SHORT (pred:-1)",  # ПРОТИВ!
            "Absorption": "LONG",  # ЗА
            "VWAP": "-0.9% below",  # ЗА
            "VW-MACD": "bearish",  # ПРОТИВ!
            "EMA": "bearish",  # ПРОТИВ!
            "1h_trend": "-0.12%",  # Почти нейтрально
        }
    },
    {
        "symbol": "SKL",
        "direction": "SHORT",
        "result": "SL",  # ❌ Неправильно
        "time": "2026-08-07 08:29",
        "indicators": {
            "Phase": "LATE_EXPANSION",  # Вершина
            "RSI": 94,  # ПЕРЕКУПЛЕН!
            "Lorentzian": "LONG (pred:+1)",  # ПРОТИВ!
            "Absorption": "SHORT",  # ЗА
            "VWAP": "+1.0% above",  # ЗА
            "VW-MACD": "bullish",  # ПРОТИВ!
            "EMA": "bullish",  # ПРОТИВ!
            "1h_trend": "+0.99%",  # ПРОТИВ!
            "HTF_filter": "блокировка SHORT - bullish trend",  # ПРОТИВ!
        }
    },
    {
        "symbol": "ONDO",
        "direction": "LONG",
        "result": "SL",  # ❌ Неправильно
        "time": "2026-08-07 08:33",
        "indicators": {
            "Phase": "EARLY_EXPANSION",
            "RSI": 15,  # ПЕРЕПРОДАН!
            "Lorentzian": "SHORT (pred:-3)",  # ПРОТИВ!
            "Absorption": "LONG",  # ЗА
            "VWAP": "-1.2% below",  # ЗА
            "VW-MACD": "bearish",  # ПРОТИВ!
            "EMA": "bearish",  # ПРОТИВ!
            "1h_trend": "-2.38%",  # ПРОТИВ!
            "HTF_filter": "блокировка LONG - bearish trend",  # ПРОТИВ!
        }
    },
]

print("="*80)
print("АНАЛИЗ ИНДИКАТОРОВ: Кто был прав, кто врал?")
print("="*80)

# Счетчики
indicator_stats = {
    "Lorentzian_correct": 0,
    "Lorentzian_wrong": 0,
    "VW-MACD_correct": 0,
    "VW-MACD_wrong": 0,
    "EMA_correct": 0,
    "EMA_wrong": 0,
    "1h_trend_correct": 0,
    "1h_trend_wrong": 0,
    "Absorption_correct": 0,
    "Absorption_wrong": 0,
    "VWAP_correct": 0,
    "VWAP_wrong": 0,
    "RSI_extreme_correct": 0,  # RSI >80 для SHORT или <20 для LONG
    "RSI_extreme_wrong": 0,
    "HTF_filter_correct": 0,
    "HTF_filter_wrong": 0,
}

for sig in signals:
    ind = sig["indicators"]
    result = sig["result"]
    direction = sig["direction"]
    
    print(f"\n{sig['symbol']} {direction} @ {sig['time']}")
    print(f"Результат: {'✅ TP' if result == 'TP' else '❌ SL'}")
    print(f"  Phase: {ind['Phase']}, RSI: {ind['RSI']}")
    
    # Lorentzian
    lor = ind.get("Lorentzian", "")
    if "SHORT" in lor and direction == "LONG":
        if result == "SL":
            indicator_stats["Lorentzian_correct"] += 1
            print(f"  ✅ Lorentzian был прав: {lor} ПРОТИВ {direction}")
        else:
            indicator_stats["Lorentzian_wrong"] += 1
            print(f"  ❌ Lorentzian ошибся: {lor} ПРОТИВ {direction}")
    elif "LONG" in lor and direction == "SHORT":
        if result == "SL":
            indicator_stats["Lorentzian_correct"] += 1
            print(f"  ✅ Lorentzian был прав: {lor} ПРОТИВ {direction}")
        else:
            indicator_stats["Lorentzian_wrong"] += 1
            print(f"  ❌ Lorentzian ошибся: {lor} ПРОТИВ {direction}")
    elif "LONG" in lor and direction == "LONG":
        if result == "TP":
            indicator_stats["Lorentzian_correct"] += 1
            print(f"  ✅ Lorentzian был прав: {lor} ЗА {direction}")
        else:
            indicator_stats["Lorentzian_wrong"] += 1
            print(f"  ❌ Lorentzian ошибся: {lor} ЗА {direction}")
    
    # VW-MACD
    macd = ind.get("VW-MACD", "")
    if macd:
        macd_bullish = "bullish" in macd
        if direction == "LONG":
            if (macd_bullish and result == "TP") or (not macd_bullish and result == "SL"):
                indicator_stats["VW-MACD_correct"] += 1
                print(f"  ✅ VW-MACD был прав: {macd}")
            else:
                indicator_stats["VW-MACD_wrong"] += 1
                print(f"  ❌ VW-MACD ошибся: {macd}")
        else:  # SHORT
            if (not macd_bullish and result == "TP") or (macd_bullish and result == "SL"):
                indicator_stats["VW-MACD_correct"] += 1
                print(f"  ✅ VW-MACD был прав: {macd}")
            else:
                indicator_stats["VW-MACD_wrong"] += 1
                print(f"  ❌ VW-MACD ошибся: {macd}")
    
    # EMA
    ema = ind.get("EMA", "")
    if ema and "нет" not in ema:
        ema_bullish = "bullish" in ema
        if direction == "LONG":
            if (ema_bullish and result == "TP") or (not ema_bullish and result == "SL"):
                indicator_stats["EMA_correct"] += 1
                print(f"  ✅ EMA был прав: {ema}")
            else:
                indicator_stats["EMA_wrong"] += 1
                print(f"  ❌ EMA ошибся: {ema}")
        else:  # SHORT
            if (not ema_bullish and result == "TP") or (ema_bullish and result == "SL"):
                indicator_stats["EMA_correct"] += 1
                print(f"  ✅ EMA был прав: {ema}")
            else:
                indicator_stats["EMA_wrong"] += 1
                print(f"  ❌ EMA ошибся: {ema}")
    
    # 1h trend
    trend = ind.get("1h_trend", "")
    if trend and "нет" not in trend:
        trend_val = float(trend.replace("%", "").replace("+", ""))
        if direction == "LONG":
            if (trend_val > 0 and result == "TP") or (trend_val < 0 and result == "SL"):
                indicator_stats["1h_trend_correct"] += 1
                print(f"  ✅ 1h trend был прав: {trend}")
            else:
                indicator_stats["1h_trend_wrong"] += 1
                print(f"  ❌ 1h trend ошибся: {trend}")
        else:  # SHORT
            if (trend_val < 0 and result == "TP") or (trend_val > 0 and result == "SL"):
                indicator_stats["1h_trend_correct"] += 1
                print(f"  ✅ 1h trend был прав: {trend}")
            else:
                indicator_stats["1h_trend_wrong"] += 1
                print(f"  ❌ 1h trend ошибся: {trend}")
    
    # RSI extreme
    rsi = ind.get("RSI", 50)
    if (direction == "LONG" and rsi < 30) or (direction == "SHORT" and rsi > 70):
        if result == "TP":
            indicator_stats["RSI_extreme_correct"] += 1
            print(f"  ✅ RSI extreme был прав: {rsi}")
        else:
            indicator_stats["RSI_extreme_wrong"] += 1
            print(f"  ❌ RSI extreme ошибся: {rsi}")

print("\n" + "="*80)
print("📊 ИТОГОВАЯ СТАТИСТИКА ИНДИКАТОРОВ")
print("="*80)

for key in ["Lorentzian", "VW-MACD", "EMA", "1h_trend", "RSI_extreme"]:
    correct = indicator_stats.get(f"{key}_correct", 0)
    wrong = indicator_stats.get(f"{key}_wrong", 0)
    total = correct + wrong
    
    if total > 0:
        accuracy = correct / total * 100
        print(f"\n{key}:")
        print(f"  Правильно: {correct}/{total} ({accuracy:.1f}%)")
        print(f"  Ошибок: {wrong}/{total}")

print("\n" + "="*80)
print("🔍 ГЛАВНЫЕ ВЫВОДЫ:")
print("="*80)

print("""
1. **Lorentzian** - когда он ПРОТИВ сигнала, часто оказывается прав
2. **EMA + VW-MACD + 1h trend** - если ВСЕ ТРИ против, НЕ ВХОДИТЬ!
3. **RSI extreme (>80 или <20)** - ЛОВУШКА! Не гарантирует разворот
4. **Phase EXHAUSTION + низкий RSI** - тоже ловушка, падение может продолжиться
5. **HTF filter блокировка** - если модель блокирует, СЛУШАТЬ ЕЁ!

НОВАЯ ЛОГИКА:
- Входить только если Lorentzian НЕ против
- Входить только если EMA + 1h trend попутные
- Входить только если VW-MACD согласен
- Игнорировать экстремальные RSI (<20 или >80) - это FOMO
- Слушать HTF filter блокировки модели
""")
