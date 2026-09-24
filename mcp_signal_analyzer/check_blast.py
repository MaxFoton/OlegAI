#!/usr/bin/env python3
"""Check BLAST SHORT full history"""

import requests
from datetime import datetime

API_URL = "https://api.bybit.com/v5/market/kline"

# BLAST SHORT at 2026-08-07 08:53
entry_price = 0.000242
sl_price = 0.000244  # +1%
tp_price = 0.000240  # -1%

# Start from 08:53
dt = datetime(2026, 8, 7, 8, 53)
start_ms = int(dt.timestamp() * 1000)

# Check до сейчас (больше 24ч)
now_ms = int(datetime.now().timestamp() * 1000)

print(f"Checking BLAST SHORT from {dt} to now")
print(f"Entry: ${entry_price:.7f}")
print(f"SL: ${sl_price:.7f} (+1%)")
print(f"TP: ${tp_price:.7f} (-1%)")
print()

params = {
    "category": "linear",
    "symbol": "BLASTUSDT",
    "interval": "1",
    "start": start_ms,
    "end": now_ms,
    "limit": 1000
}

response = requests.get(API_URL, params=params)
data = response.json()

if data["retCode"] != 0:
    print(f"Error: {data['retMsg']}")
    exit(1)

klines = list(reversed(data["result"]["list"]))

print(f"Got {len(klines)} candles")

tp_hit = False
sl_hit = False

for kline in klines:
    timestamp = int(kline[0])
    high = float(kline[2])
    low = float(kline[3])
    
    candle_time = datetime.fromtimestamp(timestamp / 1000)
    
    # For SHORT: SL hits if high >= sl_price, TP hits if low <= tp_price
    if high >= sl_price:
        print(f"🛑 SL HIT at {candle_time}")
        print(f"   High: ${high:.7f}")
        sl_hit = True
        break
    
    if low <= tp_price:
        print(f"🎯 TP HIT at {candle_time}")
        print(f"   Low: ${low:.7f}")
        tp_hit = True
        break

if not tp_hit and not sl_hit:
    last = klines[-1]
    last_close = float(last[4])
    last_time = datetime.fromtimestamp(int(last[0]) / 1000)
    
    pnl_pct = (entry_price - last_close) / entry_price * 100
    pnl_usd = pnl_pct * 3.50
    
    print(f"⏳ STILL OPEN")
    print(f"   Last candle: {last_time}")
    print(f"   Last close: ${last_close:.7f}")
    print(f"   Unrealized PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
    
    # Show high/low range
    all_highs = [float(k[2]) for k in klines]
    all_lows = [float(k[3]) for k in klines]
    
    print(f"\n   Range since entry:")
    print(f"   Highest: ${max(all_highs):.7f}")
    print(f"   Lowest: ${min(all_lows):.7f}")
