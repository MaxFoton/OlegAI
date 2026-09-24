#!/usr/bin/env python3
"""
Тест парсера ntfy сообщений
"""
from ntfy_to_funtik import parse_ntfy_message
import json

# Реальные примеры из max-analysis
test_cases = [
    {
        "name": "CARV LONG - ВХОДИТЬ",
        "title": "📊 CARV LONG | ✅ ВХОДИТЬ (18/10)",
        "message": """💰 Цена: $0.029710 📈 Score: 6/15

✅ ЗА:
  • ✅ 1h trend=-0.21% попутный
  • ✅ Lorentzian pred=-3 ЗА SHORT

🎯 ✅ ВХОДИТЬ

💰 Вход: $0.029710 (немедленно)
🛑 Стоп: $0.029413 (-1%)
🎯 Цель: $0.030007 (+1%)"""
    },
    {
        "name": "MAV SHORT - ЖДАТЬ",
        "title": "📊 MAV SHORT | ⏳ ЖДАТЬ (5/10)",
        "message": """💰 Цена: $0.010786 📈 Score: 6/15

⏳ EXHAUSTION - дождись разворота вниз

🎯 ⏳ ЖДАТЬ"""
    },
    {
        "name": "NOT SHORT - НЕ ШОРТИТЬ",
        "title": "📊 NOT SHORT | 🚫 НЕ ШОРТИТЬ (-8/10)",
        "message": """💰 Цена: $0.0003485 📈 Score: 11/15

❌ ПРОТИВ:
  • 🛑 1h trend=+0.75% ПРОТИВ SHORT

🎯 🚫 НЕ ШОРТИТЬ"""
    },
]

print("=" * 80)
print("ТЕСТ ПАРСЕРА NTFY СООБЩЕНИЙ")
print("=" * 80)

for test in test_cases:
    print(f"\n{'='*80}")
    print(f"ТЕСТ: {test['name']}")
    print(f"{'='*80}")
    print(f"Title: {test['title']}")
    print(f"Message: {test['message'][:100]}...")
    
    result = parse_ntfy_message(test['message'], test['title'])
    
    if result:
        print(f"\n✅ Parsed successfully:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"\n❌ Skipped (verdict not ВХОДИТЬ/ШОРТИТЬ or parse failed)")

print("\n" + "=" * 80)
print("ПРОВЕРКА ФОРМАТА ДЛЯ ФУНТИКА")
print("=" * 80)

# Проверяем что формат совпадает с оригиналом
expected_format = {
    "coin": "SYMBOL",
    "pair": "SYMBOL/USDT:USDT",
    "direction": "LONG|SHORT",
    "kind": "analyzed",
    "signal_type": "analyzed",
    "source": "max-analysis",
    "score": 0,
    "score_max": 15,
    "entry_price": 0.0,
    "timestamp": "2026-08-13 11:11:23",
    "signal_time_utc": "11:11:23",
    "added_to_whitelist_at": "2026-08-13T11:11:23",
    "confidence": 0,
    "verdict": "ВХОДИТЬ",
}

print("\nОжидаемый формат:")
print(json.dumps(expected_format, indent=2, ensure_ascii=False))

# Парсим первый валидный пример
result = parse_ntfy_message(test_cases[0]['message'], test_cases[0]['title'])
if result:
    print("\nПолученный формат:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    print("\nСравнение ключей:")
    expected_keys = set(expected_format.keys())
    actual_keys = set(result.keys())
    
    missing = expected_keys - actual_keys
    extra = actual_keys - expected_keys
    
    if not missing and not extra:
        print("✅ Все ключи совпадают!")
    else:
        if missing:
            print(f"❌ Отсутствуют: {missing}")
        if extra:
            print(f"⚠️ Лишние: {extra}")
