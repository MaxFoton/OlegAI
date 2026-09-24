"""
Глубокий анализ точек входа V2 — версия 2.
Для пар без локальных 5m данных тянет с Bybit через ccxt.
"""
from __future__ import annotations
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engines.feature_engine import FeatureEngine
from engines.regime_engine import RegimeEngine

import ccxt

DATA_DIR = Path("/home/max/freqtrade/user_data/data/bybit/futures")
TRADES_DB = Path("/home/max/freqtrade/user_data/tradesv3_oleg_rl.sqlite")
OUT_CSV = Path(__file__).resolve().parent / "data" / "v2_entry_features.csv"

ex = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})


def feather_path(pair: str):
    coin = pair.split("/")[0]
    p = DATA_DIR / f"{coin}_USDT_USDT-5m-futures.feather"
    return p if p.exists() else None


def load_or_fetch(pair: str, since_dt: datetime):
    """Возвращает DataFrame со 5m свечами, начиная за 200 свечей до since_dt."""
    fp = feather_path(pair)
    if fp:
        try:
            df = pd.read_feather(fp)
            return df
        except Exception:
            pass
    # Тянем с биржи: 300 свечей до момента входа = 25 часов — хватит для всех расчётов
    try:
        since_ms = int((since_dt - timedelta(hours=25)).timestamp() * 1000)
        ohlcv = ex.fetch_ohlcv(pair, "5m", since=since_ms, limit=300)
        if not ohlcv:
            return None
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"  ! fetch {pair}: {e}")
        return None


# 1. Сделки
conn = sqlite3.connect(TRADES_DB)
cutoff = (datetime.now() - timedelta(hours=72)).isoformat()
trades = pd.read_sql_query(
    """SELECT id, pair, open_date, close_date, open_rate, close_rate,
              max_rate, min_rate, close_profit, is_short, enter_tag, exit_reason
       FROM trades
       WHERE strategy='PumpDumpReversalStrategyV2'
         AND is_open=0 AND open_date >= ?
       ORDER BY open_date""",
    conn, params=(cutoff,))
conn.close()


def outcome(p):
    if p > 0.005: return 'WIN'
    if p < -0.005: return 'LOSS'
    return 'BE'


trades['outcome'] = trades['close_profit'].apply(outcome)
trades['side'] = trades['is_short'].apply(lambda x: 'SHORT' if x else 'LONG')
trades['mode'] = trades['enter_tag'].apply(
    lambda t: 'REVERSAL' if 'reversal' in (t or '') else ('CONT' if 'cont' in (t or '') else '?'))
trades['source'] = trades['enter_tag'].fillna('').str.split('_').str[0]
trades[['mfe_pct', 'mae_pct']] = trades.apply(
    lambda r: pd.Series({
        'mfe_pct': ((r['open_rate']-r['min_rate'])/r['open_rate']*100 if r['is_short']
                    else (r['max_rate']-r['open_rate'])/r['open_rate']*100),
        'mae_pct': ((r['open_rate']-r['max_rate'])/r['open_rate']*100 if r['is_short']
                    else (r['min_rate']-r['open_rate'])/r['open_rate']*100),
    }), axis=1)

print(f'Trades: {len(trades)}')

fe = FeatureEngine()
re = RegimeEngine()
feat_cache: dict[str, pd.DataFrame | None] = {}

# Уникальные пары
unique_pairs = trades['pair'].unique()
print(f'Unique pairs: {len(unique_pairs)}')
have_local = sum(1 for p in unique_pairs if feather_path(p))
print(f'  local 5m: {have_local}, need to fetch: {len(unique_pairs)-have_local}')

# Заранее тянем нужные пары
for i, p in enumerate(unique_pairs, 1):
    if feather_path(p):
        continue  # для локальных тянем по требованию
    pair_trades = trades[trades['pair'] == p]
    earliest = pd.Timestamp(pair_trades['open_date'].min())
    if earliest.tz is None:
        earliest = earliest.tz_localize('UTC')
    print(f'  fetching {p} [{i}/{len(unique_pairs)}]...')
    df = load_or_fetch(p, earliest.to_pydatetime())
    if df is None or len(df) < 100:
        feat_cache[p] = None
        continue
    try:
        df = fe.compute(df.copy())
        df = re.compute_lorentzian(df)
        feat_cache[p] = df
    except Exception as e:
        print(f'  ! engine error {p}: {e}')
        feat_cache[p] = None

# Локальные тоже подгружаем
for p in unique_pairs:
    if p in feat_cache:
        continue
    fp = feather_path(p)
    if fp is None:
        feat_cache[p] = None
        continue
    try:
        raw = pd.read_feather(fp)
        df = fe.compute(raw.copy())
        df = re.compute_lorentzian(df)
        feat_cache[p] = df
    except Exception as e:
        print(f'  ! local engine error {p}: {e}')
        feat_cache[p] = None

# 2. Считаем фичи на момент входа
rows = []
no_data = 0
for _, t in trades.iterrows():
    df = feat_cache.get(t['pair'])
    if df is None:
        no_data += 1
        continue
    open_dt = pd.Timestamp(t['open_date'])
    if open_dt.tz is None:
        open_dt = open_dt.tz_localize('UTC')
    dates = df['date']
    if dates.dt.tz is None:
        dates = dates.dt.tz_localize('UTC')
    sub = df.loc[dates <= open_dt]
    if len(sub) < 50:
        no_data += 1
        continue

    last = sub.iloc[-1]
    window = sub.tail(50)
    high_1h = window['high'].iloc[-12:].max()
    low_1h = window['low'].iloc[-12:].min()
    pos_1h = ((last['close']-low_1h)/(high_1h-low_1h)) if high_1h > low_1h else 0.5

    body = abs(last['close']-last['open'])
    upper_wick = last['high']-max(last['open'], last['close'])
    lower_wick = min(last['open'], last['close'])-last['low']

    pct_5m = (last['close']-window['close'].iloc[-2])/window['close'].iloc[-2]*100 if len(window) >= 2 else 0
    pct_15m = (last['close']-window['close'].iloc[-4])/window['close'].iloc[-4]*100 if len(window) >= 4 else 0
    pct_1h = (last['close']-window['close'].iloc[-13])/window['close'].iloc[-13]*100 if len(window) >= 13 else 0

    phase = re.detect_phase(window)
    dist_ema = (last['close']-last['ema_fast'])/last['ema_fast']*100 if last['ema_fast'] > 0 else 0
    dist_vwap = (last['close']-last['vwap'])/last['vwap']*100 if last['vwap'] > 0 else 0

    rows.append({
        'id': t['id'],
        'pair': t['pair'],
        'side': t['side'],
        'mode': t['mode'],
        'source': t['source'],
        'outcome': t['outcome'],
        'realized_pct': round(t['close_profit']*100, 2),
        'mfe': round(t['mfe_pct'], 2),
        'mae': round(t['mae_pct'], 2),
        'enter_tag': t['enter_tag'],
        'exit_reason': t['exit_reason'],
        'velocity': round(float(last.get('velocity', 0) or 0), 3),
        'volume_spike': round(float(last.get('volume_spike', 1) or 1), 2),
        'absorption': int(last.get('absorption_score', 0) or 0),
        'lor_signal': int(last.get('lor_signal', 0) or 0),
        'lor_strength': round(float(last.get('lor_strength', 0) or 0), 2),
        'lor_pred': int(last.get('lor_prediction', 0) or 0),
        'rsi': round(float(last.get('rsi', 50) or 50), 1),
        'adx': round(float(last.get('adx', 25) or 25), 1),
        'phase': phase,
        'pos_1h': round(pos_1h, 2),
        'pct_5m': round(pct_5m, 2),
        'pct_15m': round(pct_15m, 2),
        'pct_1h': round(pct_1h, 2),
        'body_pct': round(body/last['open']*100, 2) if last['open'] > 0 else 0,
        'upper_wick_pct': round(upper_wick/last['open']*100 if last['open'] > 0 else 0, 2),
        'lower_wick_pct': round(lower_wick/last['open']*100 if last['open'] > 0 else 0, 2),
        'dist_ema_pct': round(dist_ema, 2),
        'dist_vwap_pct': round(dist_vwap, 2),
    })

print(f'\nProcessed: {len(rows)}, no data: {no_data}')
df = pd.DataFrame(rows)
OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(OUT_CSV, index=False)
print(f'Saved {OUT_CSV}')

if len(df) < 10:
    print(f'\n⚠️ Слишком мало данных ({len(df)}) — анализ нестабилен')

print('\n' + '='*70)
print('# 1. Outcome')
print('='*70)
print(df['outcome'].value_counts().to_string())
print(f'win_rate: {(df["outcome"] == "WIN").sum()/len(df)*100:.0f}%')

print('\n' + '='*70)
print('# 2. Медианы фич: WIN vs LOSS')
print('='*70)
numeric = ['velocity', 'volume_spike', 'absorption', 'lor_signal', 'lor_strength',
           'lor_pred', 'rsi', 'adx', 'pos_1h', 'pct_5m', 'pct_15m', 'pct_1h',
           'body_pct', 'upper_wick_pct', 'lower_wick_pct',
           'dist_ema_pct', 'dist_vwap_pct']
medians = df.groupby('outcome')[numeric].median().T
if 'WIN' in medians.columns and 'LOSS' in medians.columns:
    medians['WIN-LOSS'] = (medians['WIN'] - medians['LOSS']).round(3)
medians = medians.round(3)
print(medians.to_string())

print('\n' + '='*70)
print('# 3. Phase × outcome')
print('='*70)
ct = pd.crosstab(df['phase'], df['outcome'])
ct['win_rate%'] = (ct.get('WIN', 0) / ct.sum(axis=1) * 100).round(0)
print(ct.to_string())

print('\n' + '='*70)
print('# 4. pos_1h: где в часовом диапазоне открываемся (1.0=топ часа)')
print('='*70)


def pos_bucket(p):
    if p < 0.3: return 'LOW (0-30%)'
    if p < 0.7: return 'MID (30-70%)'
    return 'HIGH (70-100%)'
df['pos_bucket'] = df['pos_1h'].apply(pos_bucket)
ct = pd.crosstab([df['side'], df['pos_bucket']], df['outcome'])
ct['win_rate%'] = (ct.get('WIN', 0) / ct.sum(axis=1) * 100).round(0)
print(ct.to_string())

print('\n' + '='*70)
print('# 5. pct_1h — насколько импульс уже разогнан')
print('='*70)


def pct1h_bucket(side, p):
    if side == 'LONG':
        if p < -2: return '5_dipping (<-2%)'
        if p < 0: return '4_mild_dip (-2..0)'
        if p < 1: return '3_flat (0..1%)'
        if p < 3: return '2_rising (1..3%)'
        return '1_overbought (>3%)'
    else:
        if p > 2: return '5_climbing (>2%)'
        if p > 0: return '4_mild_up (0..2)'
        if p > -1: return '3_flat (-1..0)'
        if p > -3: return '2_falling (-3..-1)'
        return '1_oversold (<-3%)'
df['pct1h_bucket'] = df.apply(lambda r: pct1h_bucket(r['side'], r['pct_1h']), axis=1)
ct = pd.crosstab([df['side'], df['pct1h_bucket']], df['outcome'])
ct['win_rate%'] = (ct.get('WIN', 0) / ct.sum(axis=1) * 100).round(0)
print(ct.to_string())

print('\n' + '='*70)
print('# 6. Lorentzian alignment (signal vs side)')
print('='*70)


def lor_align(r):
    if r['side'] == 'LONG':
        return 'with' if r['lor_signal'] == 1 else ('against' if r['lor_signal'] == -1 else 'neutral')
    return 'with' if r['lor_signal'] == -1 else ('against' if r['lor_signal'] == 1 else 'neutral')
df['lor_align'] = df.apply(lor_align, axis=1)
ct = pd.crosstab(df['lor_align'], df['outcome'])
ct['win_rate%'] = (ct.get('WIN', 0) / ct.sum(axis=1) * 100).round(0)
print(ct.to_string())

print('\n' + '='*70)
print('# 7. Distance from EMA20 — куплено далеко от средней?')
print('='*70)


def dist_bucket(d, side):
    a = d if side == 'LONG' else -d  # для шорта нормализуем
    if a < -1: return '5_far_against'
    if a < 0: return '4_below'
    if a < 1: return '3_just_above'
    if a < 3: return '2_extended'
    return '1_very_extended'
df['dist_bucket'] = df.apply(lambda r: dist_bucket(r['dist_ema_pct'], r['side']), axis=1)
ct = pd.crosstab(df['dist_bucket'], df['outcome'])
ct['win_rate%'] = (ct.get('WIN', 0) / ct.sum(axis=1) * 100).round(0)
print(ct.to_string())

print('\n' + '='*70)
print('# 8. Volume spike at entry')
print('='*70)


def vol_bucket(v):
    if v < 0.7: return '1_low (<0.7)'
    if v < 1.2: return '2_normal'
    if v < 2: return '3_elevated'
    if v < 4: return '4_high'
    return '5_extreme'
df['vol_bucket'] = df['volume_spike'].apply(vol_bucket)
ct = pd.crosstab(df['vol_bucket'], df['outcome'])
ct['win_rate%'] = (ct.get('WIN', 0) / ct.sum(axis=1) * 100).round(0)
print(ct.to_string())

print('\n' + '='*70)
print('# 9. Mode × outcome')
print('='*70)
ct = pd.crosstab([df['mode'], df['side']], df['outcome'])
ct['win_rate%'] = (ct.get('WIN', 0) / ct.sum(axis=1) * 100).round(0)
print(ct.to_string())

print('\n' + '='*70)
print('# 10. Source × outcome')
print('='*70)
ct = pd.crosstab(df['source'], df['outcome'])
ct['win_rate%'] = (ct.get('WIN', 0) / ct.sum(axis=1) * 100).round(0)
print(ct.to_string())

print('\n' + '='*70)
print('# 11. Худшие LOSS (top-15 по убытку)')
print('='*70)
worst = df[df['outcome'] == 'LOSS'].sort_values('realized_pct').head(15)
cols = ['pair', 'side', 'mode', 'realized_pct', 'mfe', 'pos_1h', 'pct_1h',
        'velocity', 'rsi', 'phase', 'dist_ema_pct', 'lor_align']
print(worst[cols].to_string(index=False))

print('\n' + '='*70)
print('# 12. Лучшие WIN (top-10 по profit)')
print('='*70)
best = df[df['outcome'] == 'WIN'].sort_values('realized_pct', ascending=False).head(10)
print(best[cols].to_string(index=False))

# === УВЕДОМЛЕНИЕ В NTFY ===
print('\n' + '='*70)
print('# Отправка уведомления...')
print('='*70)
try:
    import requests
    
    win_count = (df["outcome"] == "WIN").sum()
    loss_count = (df["outcome"] == "LOSS").sum()
    be_count = (df["outcome"] == "BE").sum()
    total = len(df)
    win_rate = (win_count / total * 100) if total > 0 else 0
    
    # Топ источников по WR
    source_wr = df.groupby('source').apply(
        lambda g: (g['outcome'] == 'WIN').sum() / len(g) * 100 if len(g) > 5 else 0
    ).sort_values(ascending=False)
    
    # Формируем сообщение (только ASCII)
    message = f"""Signal Analysis 72h
    
Total trades: {total}
WIN: {win_count} ({win_rate:.1f}%)
LOSS: {loss_count}
BE: {be_count}

Sources (WR > 5 trades):
{chr(10).join(f"  {src}: {wr:.0f}%" for src, wr in source_wr.head(5).items() if wr > 0)}

Worst LOSS:
{chr(10).join(f"  {row['pair']} {row['side']} {row['realized_pct']:.1f}%" for _, row in worst.head(3).iterrows())}

Details: {OUT_CSV}"""
    
    # Отправляем в ntfy
    url = "http://87.121.218.4:8080/max-analize"
    priority = "default" if win_rate >= 50 else "high"
    tags = "chart_with_upwards_trend" if win_rate >= 50 else "chart_with_downwards_trend"
    
    response = requests.post(
        url,
        data=message,
        headers={
            "Title": f"Kiro Analysis: WR={win_rate:.0f}%",
            "Priority": priority,
            "Tags": tags
        }
    )
    
    if response.status_code == 200:
        print(f"✅ Уведомление отправлено в max-analize")
    else:
        print(f"❌ Ошибка отправки: {response.status_code}")
        
except Exception as e:
    print(f"❌ Не удалось отправить уведомление: {e}")
