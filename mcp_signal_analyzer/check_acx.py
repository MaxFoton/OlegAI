#!/usr/bin/env python3
"""Check ACX signal from 07.08.2026 11:49"""

import requests
from datetime import datetime

# ACX LONG signal at 11:49 (08:49 UTC)
entry_time = datetime(2026, 8, 7, 8, 49)  # UTC
entry_price = 0.04013
sl_price = entry_price * 0.99  # -1%
tp_price = entry_price * 1.01  # +1%

print(f"ACX LONG @ {entry_time.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"Entry: ${entry_price:.5f}")
print(f"SL: ${sl_price:.5f} (-1%)")
print(f"TP: ${tp_price:.5f} (+1%)")
print()

# Get klines from entry time
start_ms = int(entry_time.timestamp() * 1000)
end_ms = start_ms + (3 * 60 * 60 * 1000)  # +3 hours

url = "https://api.bybit.com/v5/market/kline"
params = {
    "category": "linear",
    "symbol": "ACXUSDT",
    "interval": "1",
    "start": start_ms,
    "end": end_ms,
    "limit": 180
}

response = requests.get(url, params=params)
data = response.json()

if data["retCode"] != 0:
    print(f"Error: {data['retMsg']}")
    exit(1)

klines = list(reversed(data["result"]["list"]))

print(f"Got {len(klines)} candles")
print()

for i, kline in enumerate(klines):
    timestamp = int(kline[0])
    high = float(kline[2])
    low = float(kline[3])
    close = float(kline[4])
    
    dt = datetime.fromtimestamp(timestamp / 1000)
    
    # Check SL/TP
    sl_hit = low <= sl_price
    tp_hit = high >= tp_price
    
    if i < 5:
        print(f"{dt.strftime('%H:%M')}: high=${high:.5f}, low=${low:.5f}, close=${close:.5f}")
    
    if sl_hit:
        pnl_pct = (sl_price - entry_price) / entry_price * 100
        print()
        print(f"🛑 SL HIT at {dt.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"   Low: ${low:.5f}")
        print(f"   PnL: {pnl_pct:.2f}% (-$3.50)")
        print()
        print(f"⏱️ Время до SL: {(dt - entry_time).total_seconds() / 60:.0f} минут")
        break
    
    if tp_hit:
        pnl_pct = (tp_price - entry_price) / entry_price * 100
        print()
        print(f"🎯 TP HIT at {dt.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"   High: ${high:.5f}")
        print(f"   PnL: {pnl_pct:.2f}% (+$3.50)")
        break

# Current price
current_response = requests.get("https://api.bybit.com/v5/market/tickers?category=linear&symbol=ACXUSDT")
current_data = current_response.json()
current_price = float(current_data["result"]["list"][0]["lastPrice"])

unrealized_pnl = (current_price - entry_price) / entry_price * 100

print()
print(f"📊 Текущая цена: ${current_price:.5f}")
print(f"📉 От entry: {unrealized_pnl:.2f}%")
print(f"💰 Нереализованный PnL: ${unrealized_pnl * 3.50:.2f}")
