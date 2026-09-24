#!/usr/bin/env python3
"""
Проверка индикаторов MAV в период 11:54-12:08 11.08.2026
"""
import ccxt
import pandas as pd
from datetime import datetime, timedelta
import numpy as np

# Инициализация Bybit
exchange = ccxt.bybit({
    'enableRateLimit': True,
})

symbol = 'MAV/USDT:USDT'

# Период: 11:54 - 12:08 UTC+3 = 08:54 - 09:08 UTC
start_time = datetime(2026, 8, 11, 8, 30, 0)  # Берем с запасом
end_time = datetime(2026, 8, 11, 9, 15, 0)

print(f"Fetching {symbol} data from {start_time} to {end_time} UTC...")

# Получаем 5m свечи
since = int(start_time.timestamp() * 1000)
ohlcv = exchange.fetch_ohlcv(symbol, '5m', since=since, limit=100)

df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
df['datetime_utc3'] = df['datetime'] + timedelta(hours=3)

# Фильтруем нужный период
df = df[(df['datetime'] >= start_time) & (df['datetime'] <= end_time)]

print(f"\nGot {len(df)} candles")
print(df[['datetime_utc3', 'close', 'volume']].to_string())

# Считаем индикаторы
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# Нужно больше данных для RSI
print("\nFetching more historical data for RSI calculation...")
since_extended = int((start_time - timedelta(hours=2)).timestamp() * 1000)
ohlcv_extended = exchange.fetch_ohlcv(symbol, '5m', since=since_extended, limit=200)
df_extended = pd.DataFrame(ohlcv_extended, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
df_extended['datetime'] = pd.to_datetime(df_extended['timestamp'], unit='ms')
df_extended['datetime_utc3'] = df_extended['datetime'] + timedelta(hours=3)

# RSI
df_extended['rsi'] = calculate_rsi(df_extended['close'], 14)

# EMA 20
df_extended['ema20'] = df_extended['close'].ewm(span=20, adjust=False).mean()

# Фильтруем нужный период
df_result = df_extended[(df_extended['datetime'] >= start_time) & (df_extended['datetime'] <= end_time)].copy()

df_result['price_vs_ema'] = ((df_result['close'] - df_result['ema20']) / df_result['ema20'] * 100).round(2)

print("\n" + "="*80)
print("MAV INDICATORS 11:54-12:08 UTC+3 (08:54-09:08 UTC)")
print("="*80)

for idx, row in df_result.iterrows():
    print(f"\n{row['datetime_utc3'].strftime('%H:%M UTC+3')} | Price: ${row['close']:.6f}")
    print(f"  RSI: {row['rsi']:.1f}")
    print(f"  EMA20: ${row['ema20']:.6f} | Price vs EMA: {row['price_vs_ema']:.2f}%")
    print(f"  Volume: ${row['volume']:.0f}")

# Сигнал был в 11:54, дамп начался в 12:08
signal_time = df_result[df_result['datetime_utc3'].dt.strftime('%H:%M') == '11:54']
dump_time = df_result[df_result['datetime_utc3'].dt.strftime('%H:%M') >= '12:08']

if not signal_time.empty:
    print("\n" + "="*80)
    print("📊 СИГНАЛ В 11:54:")
    print("="*80)
    row = signal_time.iloc[0]
    print(f"Price: ${row['close']:.6f}")
    print(f"RSI: {row['rsi']:.1f}")
    print(f"Price vs EMA20: {row['price_vs_ema']:.2f}%")

if not dump_time.empty:
    print("\n" + "="*80)
    print("📉 ДАМП С 12:08:")
    print("="*80)
    for idx, row in dump_time.iterrows():
        print(f"{row['datetime_utc3'].strftime('%H:%M')} | ${row['close']:.6f} | RSI: {row['rsi']:.1f} | vs EMA: {row['price_vs_ema']:.2f}%")

# Проверяем: были ли моменты когда RSI>50 ДО дампа?
pre_dump = df_result[df_result['datetime_utc3'] < datetime(2026, 8, 11, 12, 8, 0)]
high_rsi = pre_dump[pre_dump['rsi'] >= 50]

print("\n" + "="*80)
print("🔍 МОМЕНТЫ С RSI≥50 ДО 12:08:")
print("="*80)
if high_rsi.empty:
    print("❌ НЕ БЫЛО! RSI не восстановился выше 50")
else:
    for idx, row in high_rsi.iterrows():
        print(f"{row['datetime_utc3'].strftime('%H:%M')} | RSI: {row['rsi']:.1f}")
