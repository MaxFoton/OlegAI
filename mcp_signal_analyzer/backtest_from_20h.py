#!/usr/bin/env python3
"""
Backtest analysis signals from 06.08.2026 20:00 with fixed 1% SL and 1% TP
Checks from notification time, not signal time
"""

import requests
from datetime import datetime, timedelta
import time

# Bybit API
API_URL = "https://api.bybit.com/v5/market/kline"

def get_klines(symbol: str, start_time_ms: int, end_time_ms: int):
    """Get 1-minute klines from Bybit"""
    params = {
        "category": "linear",  # USDT perpetual futures
        "symbol": f"{symbol}USDT",
        "interval": "1",
        "start": start_time_ms,
        "end": end_time_ms,
        "limit": 1000
    }
    
    try:
        response = requests.get(API_URL, params=params)
        data = response.json()
        
        if data["retCode"] != 0:
            print(f"❌ Error for {symbol}: {data['retMsg']}")
            return []
        
        return data["result"]["list"]
    except Exception as e:
        print(f"❌ Exception for {symbol}: {e}")
        return []

def check_signal(symbol: str, direction: str, notification_time: str, entry_price: float):
    """
    Check if SL or TP hit first from notification time
    SL = 1% against direction
    TP = 1% in direction
    """
    
    # Parse notification time
    dt = datetime.strptime(notification_time, "%Y-%m-%d %H:%M")
    start_ms = int(dt.timestamp() * 1000)
    end_ms = start_ms + (24 * 60 * 60 * 1000)  # +24h
    
    # Calculate SL and TP from entry
    if direction == "LONG":
        sl_price = entry_price * 0.99  # -1%
        tp_price = entry_price * 1.01  # +1%
    else:  # SHORT
        sl_price = entry_price * 1.01  # +1%
        tp_price = entry_price * 0.99  # -1%
    
    print(f"\n{'='*80}")
    print(f"📊 {symbol} {direction}")
    print(f"⏰ Notification: {notification_time}")
    print(f"💰 Entry: ${entry_price:.6f}")
    print(f"🛑 SL: ${sl_price:.6f} ({'-1%' if direction == 'LONG' else '+1%'})")
    print(f"🎯 TP: ${tp_price:.6f} ({'+1%' if direction == 'LONG' else '-1%'})")
    
    # Get klines
    klines = get_klines(symbol, start_ms, end_ms)
    
    if not klines:
        print("❌ No data available")
        return None
    
    # Check each candle
    for i, kline in enumerate(reversed(klines)):  # Bybit returns newest first
        timestamp = int(kline[0])
        open_price = float(kline[1])
        high = float(kline[2])
        low = float(kline[3])
        close = float(kline[4])
        
        candle_time = datetime.fromtimestamp(timestamp / 1000)
        
        # Check if SL or TP hit
        if direction == "LONG":
            sl_hit = low <= sl_price
            tp_hit = high >= tp_price
        else:  # SHORT
            sl_hit = high >= sl_price
            tp_hit = low <= tp_price
        
        if sl_hit or tp_hit:
            # Determine which hit first based on candle structure
            if sl_hit and tp_hit:
                # Both hit in same candle - need to determine order
                # Simple heuristic: check which is closer to open
                if direction == "LONG":
                    sl_first = abs(open_price - sl_price) < abs(open_price - tp_price)
                else:
                    sl_first = abs(open_price - sl_price) < abs(open_price - tp_price)
                
                if sl_first:
                    print(f"🛑 SL HIT at {candle_time.strftime('%Y-%m-%d %H:%M')}")
                    print(f"   Low: ${low:.6f}, High: ${high:.6f}")
                    print(f"   PnL: -$3.50 (1% loss)")
                    return "SL"
                else:
                    print(f"🎯 TP HIT at {candle_time.strftime('%Y-%m-%d %H:%M')}")
                    print(f"   Low: ${low:.6f}, High: ${high:.6f}")
                    print(f"   PnL: +$3.50 (1% gain)")
                    return "TP"
            
            elif sl_hit:
                print(f"🛑 SL HIT at {candle_time.strftime('%Y-%m-%d %H:%M')}")
                print(f"   Low: ${low:.6f}, High: ${high:.6f}")
                print(f"   PnL: -$3.50 (1% loss)")
                return "SL"
            
            elif tp_hit:
                print(f"🎯 TP HIT at {candle_time.strftime('%Y-%m-%d %H:%M')}")
                print(f"   Low: ${low:.6f}, High: ${high:.6f}")
                print(f"   PnL: +$3.50 (1% gain)")
                return "TP"
    
    # Still open
    last_kline = klines[0]  # Newest
    last_close = float(last_kline[4])
    pnl_pct = ((last_close - entry_price) / entry_price * 100) if direction == "LONG" else ((entry_price - last_close) / entry_price * 100)
    pnl_usd = pnl_pct * 3.50
    
    print(f"⏳ STILL OPEN")
    print(f"   Last close: ${last_close:.6f}")
    print(f"   Unrealized PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
    return "OPEN"


def main():
    # Signals from 06.08.2026 20:00+ with verdict >= 7
    signals = [
        # Format: (symbol, direction, notification_time, entry_price)
        ("COOKIE", "LONG", "2026-08-06 19:11", 0.010261),
        ("BEL", "LONG", "2026-08-06 19:34", 0.104322),
        ("KMNO", "LONG", "2026-08-07 05:17", 0.020710),
        ("CARV", "LONG", "2026-08-07 05:43", 0.028953),
        ("PUMPFUN", "SHORT", "2026-08-07 05:47", 0.002382),
        ("BSB", "LONG", "2026-08-07 06:04", 0.176538),
        ("BIGTIME", "LONG", "2026-08-07 06:43", 0.005376),
        ("FARTCOIN", "LONG", "2026-08-07 06:59", 0.128925),
        ("ZORA", "LONG", "2026-08-07 07:30", 0.005191),
        ("SKL", "SHORT", "2026-08-07 08:29", 0.003510),
        ("ONDO", "LONG", "2026-08-07 08:33", 0.348776),
        ("BLAST", "SHORT", "2026-08-07 08:53", 0.000242),
    ]
    
    results = {"TP": 0, "SL": 0, "OPEN": 0}
    
    print("="*80)
    print("BACKTEST: Signals from 06.08.2026 20:00+")
    print("Risk: $3.50 per trade (1% of $350 position)")
    print("Method: Fixed 1% SL and 1% TP from notification time")
    print("="*80)
    
    for symbol, direction, notif_time, entry in signals:
        result = check_signal(symbol, direction, notif_time, entry)
        if result:
            results[result] += 1
        time.sleep(0.3)  # Rate limiting
    
    # Summary
    print("\n" + "="*80)
    print("📈 SUMMARY")
    print("="*80)
    print(f"✅ TP: {results['TP']}")
    print(f"❌ SL: {results['SL']}")
    print(f"⏳ Open: {results['OPEN']}")
    print(f"\nTotal signals: {len(signals)}")
    
    if results['TP'] + results['SL'] > 0:
        winrate = results['TP'] / (results['TP'] + results['SL']) * 100
        print(f"Winrate: {winrate:.1f}%")
    
    total_pnl = (results['TP'] * 3.50) - (results['SL'] * 3.50)
    print(f"\nTotal PnL: ${total_pnl:+.2f}")
    print(f"Closed PnL: ${total_pnl:+.2f} ({results['TP']} TP / {results['SL']} SL)")
    print("="*80)

if __name__ == "__main__":
    main()
