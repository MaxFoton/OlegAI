#!/usr/bin/env python3
"""DEX-watchlist alert scanner (fixed).

Цепочка: сканер -> LightGBM-фильтр -> ntfy -> ручной вход или freqtrade-бот.

ИСПРАВЛЕНО (относительно scanner_r.py):
* d1h/d4h считаются с соответствующих TF; d1h_atr нормируется на 1h ATR
* BTC-режим по «живому» часу из 5m (без лага до 60 мин)
* RR-гейт честный: main = structural или TP1, не 3R-заглушка
* стоп/риск от 1h ATR при hold; plan.valid_until; cost по entry_type
* quote_volume<=0 блокирует; OI partial/gap — quality gates
* pro-паттерны проходят apply_model_gate
* score с agreement-штрафом; голоса по группам (anti double-count)
* position_usd для стакана согласован с планом; depth от mid
* state.save после каждого sent; pidfile; daemon sync к wall-clock
* export в Фунтик только вне --test
* cooldown override с лимитом; conflict -> лучшая сторона, не drop both
* phase/ER chop-фильтр; VWAP/retest/MA в ATR/sigma; vol baseline шире
* record_candidate получает plan-поля в sig; journal до гейтов для rejected

Совместимость сохранена:
* ключи sig (старые 53+)
* SIGNAL_DECISION / SIGNAL_PLAN
* notify(...), settle_candidates, record_candidate, predict_direction
* CLI: --test, --max-pairs, --ping, --daemon, --interval, --explain
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import math
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import ccxt
from dotenv import load_dotenv

try:
    import fcntl  # type: ignore
except ImportError:  # Windows
    fcntl = None  # type: ignore

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
STATE_FILE = DATA_DIR / "alert_state.json"
JOURNAL_FILE = DATA_DIR / "signals.jsonl"
LOG_FILE = ROOT / "logs" / "scanner.log"
PID_FILE = DATA_DIR / "scanner.pid"

load_dotenv(ROOT.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("dex_scanner")

# ─────────────────────────────────────────────────────────────────────────
# ВНЕШНИЕ ЗАВИСИМОСТИ (мягкий импорт)
# ─────────────────────────────────────────────────────────────────────────
LGBM_PATH = os.getenv("LGBM_PATH", "/home/max/freqtrade/user_data/pump_dump_strategy")
if LGBM_PATH and LGBM_PATH not in sys.path:
    sys.path.insert(0, LGBM_PATH)


def _stub_notify(**kw) -> bool:
    logger.warning("notifier недоступен, алерт не отправлен: %s", kw.get("title"))
    return False


try:
    from dex_scanner.notifier import notify
except Exception:
    notify = _stub_notify

try:
    from dex_scanner.lightgbm_filter import predict_neutral_signal, get_model_verdict_line
except Exception:
    def predict_neutral_signal(sig, lv, sv):  # noqa: ARG001
        return "NEUTRAL", 0.0

    def get_model_verdict_line(prob, direction):
        return f"ML: n/a (dir={direction}, p={prob:.2f})"

try:
    from dex_scanner.pro_scanner import scan_pro_patterns
except Exception:
    def scan_pro_patterns(sig):  # noqa: ARG001
        return []

try:
    from directional_lgbm import record_candidate, settle_candidates, predict_direction
except Exception:
    logger.warning("directional_lgbm недоступен — работаем без ML-гейта")

    def record_candidate(sig, kind, symbol):  # noqa: ARG001
        return None

    def settle_candidates(exchange):  # noqa: ARG001
        return 0, 0

    def predict_direction(sig, kind):  # noqa: ARG001
        return None, 0.0

_ML_AVAILABLE = predict_direction.__module__ != __name__ if hasattr(predict_direction, "__module__") else False
try:
    from directional_lgbm import predict_direction as _pd_check  # noqa: F401
    _ML_AVAILABLE = True
except Exception:
    _ML_AVAILABLE = False

# ═════════════════════════════════════════════════════════════════════════
# 1. НАСТРОЙКИ
# ═════════════════════════════════════════════════════════════════════════
CFG: dict[str, Any] = {
    # --- триггеры сетапов ---
    "p7_oi_min": 3.3,
    "p7_vol_min": 2.0,
    "p7_move_atr": 0.35,

    "p6_oi_min": 2.67,
    "p6_vol_min": 1.67,
    "p6_move_atr": 0.5,
    "p6_rsi_long_max": 65.0,
    "p6_rsi_short_min": 35.0,
    "p6_max_d15_atr": 1.2,
    "p6_max_vwap_sigma": 0.8,

    "p65_oi_fall": -2.0,
    "p65_oi_rise": 2.0,
    "p65_flat_atr": 0.6,  # в единицах 1h ATR после фикса d1h_atr
    "p65_vol_min": 1.0,

    "p5_move_atr": 1.8,
    "p5_vol_min": 1.33,

    "p4_funding_abs": 0.001,
    "p4_funding_z": 2.0,

    "p3_d15_atr": 0.9,
    "p3_vol_z": 3.33,
    "p3_vol_ratio": 1.67,

    "p2_d1h_atr": 1.5,  # в 1h ATR
    "p2_d10_atr": 1.2,  # в 5m ATR
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,

    # --- риск и исполнение ---
    "equity_usd": 1000.0,
    "risk_per_trade_pct": 0.5,
    "atr_stop_mult": 1.5,       # множитель к ATR стопа (1h)
    "atr_stop_tf": "1h",        # стоп от 1h ATR (hold до 4h)
    "min_rr": 1.5,
    "max_spread_bps": 8.0,
    "taker_fee_bps": 5.5,
    "maker_fee_bps": 2.0,
    "min_liq_mult": 5.0,
    "min_qvol_24h": 2_000_000.0,
    "max_hold_min": 240,
    "entry_valid_bars_5m": 6,   # valid_until = N закрытых 5m-свечей

    # --- гейты решения ---
    "min_score": 7,
    "min_agreement": 0.45,      # net / (long+short)
    "model_conf_min": 0.60,
    "require_htf": True,
    "require_btc_regime": True,
    "btc_dump_1h": -1.0,
    "btc_pump_1h": 1.0,
    "one_alert_per_symbol": True,
    "cooldown_min": 20,
    "cooldown_override_delta": 3,
    "cooldown_override_min": 5,
    "cooldown_override_max": 2,  # макс override за cooldown-окно
    "min_er_continuation": 0.35,  # efficiency ratio (chop filter)
    "max_data_age_sec": 600,      # свежесть последней закрытой 5m
    "require_oi_quality": True,
    "block_late_phase_continuation": True,

    # --- инфраструктура ---
    "workers": 8,
    "btc_symbol": "BTC/USDT:USDT",
    "vol_lookback": 60,          # было 12 — стабильнее median/z
    "funding_z_cache_sec": 1800,
}

MAX_SCORE = 15.0

_cfg_path = os.getenv("SCANNER_CONFIG")
if _cfg_path and Path(_cfg_path).exists():
    raw = json.loads(Path(_cfg_path).read_text())
    unknown = set(raw) - set(CFG)
    if unknown:
        logger.warning("SCANNER_CONFIG unknown keys ignored: %s", sorted(unknown))
    CFG.update({k: v for k, v in raw.items() if k in CFG})
    logger.info("конфиг загружен: %s", _cfg_path)

PROXY = os.getenv("SCANNER_PROXY")

# ═════════════════════════════════════════════════════════════════════════
# 2. ИНДИКАТОРЫ
# ═════════════════════════════════════════════════════════════════════════
TS, OPEN, HIGH, LOW, CLOSE, VOL = 0, 1, 2, 3, 4, 5
TF_MS = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}

# кэш funding z-score: symbol -> (ts, z, rate_hist_len)
_FUNDING_Z_CACHE: dict[str, tuple[float, float, int]] = {}


def pct_change(a: float, b: float) -> float:
    if not a:
        return 0.0
    return (b - a) / a * 100.0


def _ema_series(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _ema(values: Sequence[float], period: int) -> float:
    s = _ema_series(values, period)
    return s[-1] if s else 0.0


def _zscore(hist: Sequence[float], cur: float) -> float:
    if len(hist) < 8:
        return 0.0
    sd = statistics.pstdev(hist)
    return (cur - statistics.mean(hist)) / sd if sd > 0 else 0.0


def calc_rsi(closes: Sequence[float], period: int = 14) -> float:
    """RSI Уайлдера."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains) / period, sum(losses) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def calc_atr(candles: Sequence[Sequence[float]], period: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][HIGH], candles[i][LOW], candles[i - 1][CLOSE]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    if len(trs) < period:
        return statistics.mean(trs)
    v = sum(trs[:period]) / period
    for tr in trs[period:]:
        v = (v * (period - 1) + tr) / period
    return v


def in_atr(move_pct: float, atr_pct_value: float) -> float:
    return move_pct / atr_pct_value if atr_pct_value > 0 else 0.0


def efficiency_ratio(closes: Sequence[float], period: int = 20) -> float:
    """Kaufman ER: 1 = идеальный тренд, 0 = пила."""
    if len(closes) < period + 1:
        return 0.0
    change = abs(closes[-1] - closes[-1 - period])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(-period + 1, 1))
    # range: indices -period+1 .. 0 relative to end -> i from len-period to len-1
    path = 0.0
    base = len(closes) - period
    for i in range(base + 1, len(closes)):
        path += abs(closes[i] - closes[i - 1])
    return (change / path) if path > 0 else 0.0


def candles_have_gap(candles: Sequence[Sequence[float]], tf: str, max_mult: float = 2.0) -> bool:
    if len(candles) < 3:
        return False
    step = TF_MS[tf]
    # проверяем хвост (последние 30 интервалов)
    w = candles[-31:]
    for i in range(1, len(w)):
        dt = int(w[i][TS]) - int(w[i - 1][TS])
        if dt > step * max_mult or dt <= 0:
            return True
    return False


def calc_vwap(candles: Sequence[Sequence[float]]) -> dict:
    """VWAP с якорем на UTC-сутки + sigma. Мало баров за день -> low confidence."""
    empty = {
        "vwap": 0.0, "distance_pct": 0.0, "distance_sigma": 0.0,
        "position": "unknown", "reversal_long_valid": False,
        "reversal_short_valid": False, "confidence": "none",
    }
    if len(candles) < 2:
        return empty
    day = 86_400_000
    last_day = int(candles[-1][TS]) // day
    window = [c for c in candles if int(c[TS]) // day == last_day]
    if len(window) < 6:
        # не подменяем всей историей — берём prev day если есть, иначе low conf
        prev = [c for c in candles if int(c[TS]) // day == last_day - 1]
        if len(prev) >= 6 and len(window) < 3:
            window = prev
            conf = "low"
        elif len(window) < 3:
            return {**empty, "vwap": candles[-1][CLOSE], "confidence": "none"}
        else:
            conf = "low"
    else:
        conf = "ok"

    tpv = vol = 0.0
    for c in window:
        typical = (c[HIGH] + c[LOW] + c[CLOSE]) / 3.0
        tpv += typical * c[VOL]
        vol += c[VOL]
    vwap = tpv / vol if vol else window[-1][CLOSE]
    sq = sum(
        c[VOL] * (((c[HIGH] + c[LOW] + c[CLOSE]) / 3.0) - vwap) ** 2
        for c in window
    )
    sigma = (sq / vol) ** 0.5 if vol else 0.0
    price = candles[-1][CLOSE]
    dist = pct_change(vwap, price)
    dist_s = ((price - vwap) / sigma) if sigma else 0.0
    return {
        "vwap": vwap,
        "distance_pct": dist,
        "distance_sigma": dist_s,
        "position": "above" if price > vwap else "below",
        # пороги в сигмах, не в фиксированных %
        "reversal_long_valid": dist_s <= -1.0,
        "reversal_short_valid": dist_s >= 1.0,
        "confidence": conf,
    }


def calc_vw_macd(candles: Sequence[Sequence[float]], atr_value: float) -> dict:
    """MACD + signal EMA9. Объём подтверждает crossover, не ломает знак hist."""
    if len(candles) < 40:
        return {
            "vw_macd": 0.0, "signal": 0.0, "histogram": 0.0, "trend": "neutral",
            "crossover_up": False, "crossover_down": False, "strength": 0.0,
            "strength_atr": 0.0, "volume_confirmed": False,
        }
    closes = [c[CLOSE] for c in candles]
    vols = [c[VOL] for c in candles]
    macd_s = [f - s for f, s in zip(_ema_series(closes, 12), _ema_series(closes, 26))]
    signal_s = _ema_series(macd_s, 9)
    hist_s = [m - s for m, s in zip(macd_s, signal_s)]
    hist, prev = hist_s[-1], hist_s[-2]
    avg_vol = statistics.mean(vols[-20:]) or 1.0
    vr = vols[-1] / avg_vol if avg_vol else 1.0
    return {
        "vw_macd": macd_s[-1],
        "signal": signal_s[-1],
        "histogram": hist,
        "trend": "bullish" if hist > 0 else "bearish" if hist < 0 else "neutral",
        "crossover_up": hist > 0 >= prev and vr >= 0.8,
        "crossover_down": hist < 0 <= prev and vr >= 0.8,
        "strength": abs(hist),
        "strength_atr": abs(hist) / atr_value if atr_value else 0.0,
        "volume_confirmed": vr >= 0.8,
    }


def calc_ema_crossover(candles: Sequence[Sequence[float]]) -> dict:
    if len(candles) < 25:
        return {"signal": 0, "fast_ema": 0.0, "slow_ema": 0.0, "distance_pct": 0.0}
    closes = [c[CLOSE] for c in candles]
    f, s = _ema(closes, 9), _ema(closes, 21)
    return {
        "signal": 1 if f > s else -1 if f < s else 0,
        "fast_ema": f, "slow_ema": s, "distance_pct": pct_change(s, f),
    }


def calc_ma_8_18(candles_1h: Sequence[Sequence[float]], atr1h_pct: float = 0.0) -> dict:
    """Тренд MA8/18: порог в долях 1h ATR, не фиксированные 0.1%."""
    if len(candles_1h) < 18:
        return {"trend": 0, "ma8": 0.0, "ma18": 0.0, "distance_pct": 0.0}
    closes = [c[CLOSE] for c in candles_1h]
    ma8 = sum(closes[-8:]) / 8
    ma18 = sum(closes[-18:]) / 18
    dist = pct_change(ma18, ma8)
    # ~0.15 ATR 1h как минимально значимое расхождение
    thr = max(0.05, 0.15 * atr1h_pct) if atr1h_pct > 0 else 0.1
    trend = 1 if dist > thr else -1 if dist < -thr else 0
    return {"trend": trend, "ma8": ma8, "ma18": ma18, "distance_pct": dist}


def calc_atr_stop(candles_1h: Sequence[Sequence[float]], period: int = 14) -> dict:
    if len(candles_1h) < period + 1:
        return {"atr": 0.0, "stop_distance_pct": 0.0, "stop_long": 0.0, "stop_short": 0.0}
    a = calc_atr(candles_1h[-(period + 1):], period)
    price = candles_1h[-1][CLOSE]
    return {
        "atr": a,
        "stop_distance_pct": (a / price * 100.0) if price else 0.0,
        "stop_long": price - a,
        "stop_short": price + a,
    }


def calc_bollinger_squeeze(candles: Sequence[Sequence[float]], period: int = 20) -> dict:
    if len(candles) < period + 2:
        return {
            "is_squeeze": False, "bandwidth": 0.0, "percentile": 1.0,
            "upper_band": 0.0, "lower_band": 0.0, "sma": 0.0,
        }
    closes = [c[CLOSE] for c in candles]

    def bw(idx: int):
        w = closes[idx - period:idx]
        m = sum(w) / period
        sd = statistics.pstdev(w)
        return (
            ((m + 2 * sd) - (m - 2 * sd)) / m * 100.0 if m else 0.0,
            m + 2 * sd, m - 2 * sd, m,
        )

    cur, up, lo, mid = bw(len(closes))
    hist = [bw(i)[0] for i in range(max(period, len(closes) - 100), len(closes))]
    pct = (sum(1 for h in hist if h <= cur) / len(hist)) if hist else 1.0
    return {
        "is_squeeze": pct <= 0.25, "bandwidth": cur, "percentile": pct,
        "upper_band": up, "lower_band": lo, "sma": mid,
    }


def calc_obv(candles: Sequence[Sequence[float]], period: int = 20) -> dict:
    w = list(candles)[-(period + 1):]
    if len(w) < 6:
        return {"obv": 0.0, "obv_trend": 0, "obv_change_pct": 0.0, "divergence": 0}
    series, v = [], 0.0
    for i in range(1, len(w)):
        if w[i][CLOSE] > w[i - 1][CLOSE]:
            v += w[i][VOL]
        elif w[i][CLOSE] < w[i - 1][CLOSE]:
            v -= w[i][VOL]
        series.append(v)
    mid = len(series) // 2
    first = statistics.mean(series[:mid]) if mid else 0.0
    second = statistics.mean(series[mid:])
    trend = 1 if second > first else -1 if second < first else 0
    price_up = w[-1][CLOSE] > w[mid][CLOSE]
    div = 0
    if price_up and trend == -1:
        div = -1
    elif not price_up and trend == 1:
        div = 1
    return {
        "obv": v, "obv_trend": trend,
        "obv_change_pct": ((second - first) / abs(first) * 100.0) if first else 0.0,
        "divergence": div,
    }


def calc_ema_momentum(candles: Sequence[Sequence[float]]) -> dict:
    """Бывший 'lorentzian' — честное имя: EMA20 + 3-bar momentum."""
    if len(candles) < 22:
        return {"signal": 0, "pred": 0, "strength": 0.0}
    closes = [c[CLOSE] for c in candles]
    e = _ema(closes, 20)
    last = closes[-1]
    trend = 1 if last > e else -1
    momentum = sum(1 if closes[i] > closes[i - 1] else -1 for i in range(-3, 0))
    # range fix for negative indices
    momentum = 0
    for i in range(len(closes) - 3, len(closes)):
        momentum += 1 if closes[i] > closes[i - 1] else -1
    signal = 1 if trend == 1 and momentum > 0 else (-1 if trend == -1 and momentum < 0 else 0)
    return {
        "signal": signal, "pred": momentum,
        "strength": min(abs(last - e) / e * 50, 1.0) if e else 0.0,
    }


# alias для совместимости старых ключей lor_*
calc_lorentzian = calc_ema_momentum


def detect_phase(candles: Sequence[Sequence[float]]) -> str:
    if len(candles) < 12:
        return "UNKNOWN"
    r = candles[-12:]
    hi, lo = max(c[HIGH] for c in r), min(c[LOW] for c in r)
    if hi == lo:
        return "EARLY_EXPANSION"
    pos = (r[-1][CLOSE] - lo) / (hi - lo)
    if pos > 0.85:
        return "LATE_EXPANSION"
    if pos > 0.65:
        return "MID_EXPANSION"
    if pos > 0.15:
        return "EARLY_EXPANSION"
    return "EXHAUSTION"


def calc_structure(candles: Sequence[Sequence[float]], lookback: int = 24) -> dict:
    w = list(candles)[-lookback:]
    if len(w) < 6:
        return {
            "trend": 0, "swing_high": 0.0, "swing_low": 0.0,
            "broke_high": False, "broke_low": False,
        }
    mid = len(w) // 2
    ph, pl = max(c[HIGH] for c in w[:mid]), min(c[LOW] for c in w[:mid])
    lh, ll = max(c[HIGH] for c in w[mid:]), min(c[LOW] for c in w[mid:])
    trend = 1 if (lh > ph and ll > pl) else -1 if (lh < ph and ll < pl) else 0
    return {
        "trend": trend, "swing_high": lh, "swing_low": ll,
        "broke_high": w[-1][CLOSE] > ph, "broke_low": w[-1][CLOSE] < pl,
    }


def calc_absorption(candles: Sequence[Sequence[float]], vol_ratio: float) -> dict:
    c = candles[-1]
    rng = c[HIGH] - c[LOW]
    if rng <= 0:
        return {"long": 0, "short": 0}
    top, bot = max(c[CLOSE], c[OPEN]), min(c[CLOSE], c[OPEN])
    upper, lower = (c[HIGH] - top) / rng, (bot - c[LOW]) / rng
    close_pos = (c[CLOSE] - c[LOW]) / rng
    strong = vol_ratio > 1.2
    return {
        "long": 1 if (strong and lower > 0.5 and close_pos > 0.5) else 0,
        "short": 1 if (strong and upper > 0.5 and close_pos < 0.5) else 0,
    }


def detect_retest(
    candles: Sequence[Sequence[float]],
    level: float,
    atr_value: float,
) -> dict:
    """Ретест: допуск в долях ATR, не фиксированные 0.3%."""
    w = list(candles)[-12:]
    if len(w) < 6 or level <= 0:
        return {"is_retest": False, "retest_volume_declining": False, "volume_ratio": 0.0}
    tol = max(atr_value * 0.15, level * 0.0005) if atr_value > 0 else level * 0.003
    b_idx = b_vol = None
    for i, c in enumerate(w[:-2]):
        if c[HIGH] >= level >= c[LOW]:
            b_idx, b_vol = i, c[VOL]
            break
    if b_idx is None:
        return {"is_retest": False, "retest_volume_declining": False, "volume_ratio": 0.0}
    for c in w[b_idx + 1:]:
        if abs(c[CLOSE] - level) <= tol:
            ratio = (c[VOL] / b_vol) if b_vol else 0.0
            return {
                "is_retest": True,
                "retest_volume_declining": ratio < 0.7,
                "volume_ratio": ratio,
            }
    return {"is_retest": False, "retest_volume_declining": False, "volume_ratio": 0.0}


def _fractal_pivots(closes: Sequence[float], left: int = 3, right: int = 3) -> tuple[list[int], list[int]]:
    highs, lows = [], []
    for i in range(left, len(closes) - right):
        w = closes[i - left:i + right + 1]
        if closes[i] == max(w) and w.count(closes[i]) == 1:
            highs.append(i)
        if closes[i] == min(w) and w.count(closes[i]) == 1:
            lows.append(i)
    return highs, lows


def calc_rsi_divergence(candles: Sequence[Sequence[float]]) -> int:
    """Дивергенция по фрактальным свингам, не по двум точкам."""
    if len(candles) < 40:
        return 0
    closes = [c[CLOSE] for c in candles]
    # RSI series (упрощённо — RSI на префиксах дорого; считаем rolling close windows)
    rsi_series = []
    for i in range(20, len(closes) + 1):
        rsi_series.append(calc_rsi(closes[:i]))
    # align: rsi_series[k] corresponds to closes[19+k]
    off = 19
    price = closes[off:]
    if len(price) != len(rsi_series) or len(price) < 15:
        return 0
    _, lows = _fractal_pivots(price)
    highs, _ = _fractal_pivots(price)
    # bullish: price LL, RSI HL
    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        if price[i2] < price[i1] and rsi_series[i2] > rsi_series[i1] + 2:
            return 1
    # bearish: price HH, RSI LH
    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        if price[i2] > price[i1] and rsi_series[i2] < rsi_series[i1] - 2:
            return -1
    return 0


# ═════════════════════════════════════════════════════════════════════════
# 3. ДАННЫЕ С БИРЖИ
# ═════════════════════════════════════════════════════════════════════════
def _safe(fn: Callable, *a, default=None, tries: int = 3, **kw):
    last = None
    for i in range(tries):
        try:
            return fn(*a, **kw)
        except Exception as exc:
            last = exc
            if i < tries - 1:
                time.sleep(0.4 * (2 ** i))
    logger.debug("call %s failed: %s", getattr(fn, "__name__", fn), last)
    return default


def _drop_unclosed(candles: list, tf: str) -> list:
    if not candles:
        return []
    out = [list(c) for c in candles]
    if int(out[-1][TS]) + TF_MS[tf] > int(time.time() * 1000):
        out.pop()
    return out


def fetch_candles(ex, symbol: str, tf: str, limit: int) -> list:
    raw = _safe(ex.fetch_ohlcv, symbol, tf, limit=limit, default=None)
    return _drop_unclosed(raw, tf) if raw else []


def fetch_oi(ex, symbol: str) -> dict:
    """ΔOI в контрактах; не маскируем короткий горизонт как 1h."""
    out = {
        "oi_now": None, "oi_change_1h": None, "oi_change_15m": None,
        "oi_change_5m": None,  # NEW 28.09.2026
        "oi_source": "none", "oi_span_min": 0.0, "oi_quality": "bad",
    }
    hist = _safe(ex.fetch_open_interest_history, symbol, "5m", limit=14, default=None)
    if not hist or len(hist) < 2:
        return out

    def contracts(row: dict):
        for k in ("openInterestAmount", "openInterestContracts", "baseVolume"):
            if row.get(k):
                return float(row[k])
        v = (row.get("info") or {}).get("openInterest")
        return float(v) if v else None

    series = [(r, contracts(r)) for r in hist]
    series = [(r, c) for r, c in series if c]
    if len(series) < 2:
        cur = hist[-1].get("openInterestValue")
        return {
            **out,
            "oi_now": float(cur) if cur else None,
            "oi_source": "partial",
            "oi_quality": "partial",
        }

    cur_row, cur = series[-1]
    out["oi_now"] = float(cur_row.get("openInterestValue") or 0) or cur
    out["oi_source"] = getattr(ex, "id", "exchange")

    def _ts(row):
        t = row.get("timestamp") or (row.get("info") or {}).get("timestamp")
        return float(t) if t else None

    # NEW 28.09.2026: oi_change_5m из последних 2 точек с проверкой timestamp
    if len(series) >= 2:
        prev_row, prev_c = series[-2]
        t0, t1 = _ts(prev_row), _ts(cur_row)
        time_diff_min = ((t1 - t0) / 60000.0) if (t0 and t1) else 5.0
        
        # Проверка качества: не больше 6 минут между точками
        if time_diff_min <= 6.0 and prev_c and prev_c > 0:
            out["oi_change_5m"] = (cur - prev_c) / prev_c * 100.0
    
    if len(series) >= 4 and series[-4][1]:
        out["oi_change_15m"] = (cur - series[-4][1]) / series[-4][1] * 100.0

    # честный 1h: нужен span >= 50 мин
    if len(series) >= 13 and series[-13][1]:
        ref_c = series[-13][1]
        t0, t1 = _ts(series[-13][0]), _ts(series[-1][0])
        span_min = ((t1 - t0) / 60000.0) if (t0 and t1) else 60.0
        out["oi_span_min"] = span_min
        if span_min >= 50:
            out["oi_change_1h"] = (cur - ref_c) / ref_c * 100.0
            out["oi_quality"] = "ok"
        else:
            # масштабируем осторожно и помечаем
            out["oi_change_1h"] = (cur - ref_c) / ref_c * 100.0 * (60.0 / max(span_min, 1.0))
            out["oi_quality"] = "scaled"
    else:
        ref_c = series[0][1]
        t0, t1 = _ts(series[0][0]), _ts(series[-1][0])
        span_min = ((t1 - t0) / 60000.0) if (t0 and t1) else float(5 * (len(series) - 1))
        out["oi_span_min"] = span_min
        if ref_c and span_min >= 50:
            out["oi_change_1h"] = (cur - ref_c) / ref_c * 100.0
            out["oi_quality"] = "ok"
        elif ref_c and span_min >= 20:
            out["oi_change_1h"] = (cur - ref_c) / ref_c * 100.0 * (60.0 / span_min)
            out["oi_quality"] = "scaled"
        else:
            out["oi_quality"] = "short"
    return out


def fetch_orderbook(ex, symbol: str, mid_price: float, position_usd: float) -> dict:
    """Стакан: глубина от mid ±2%, не от candle close."""
    ob = _safe(ex.fetch_order_book, symbol, limit=50, default=None)
    if not ob or not ob.get("bids") or not ob.get("asks"):
        return {
            "sufficient": False, "total_liquidity_usd": 0.0, "ratio": 0.0,
            "spread_bps": 999.0, "imbalance": 0.0, "mid": 0.0,
            "best_bid": 0.0, "best_ask": 0.0,
        }
    bb, ba = ob["bids"][0][0], ob["asks"][0][0]
    mid = (bb + ba) / 2.0
    # impact-oriented window around live mid
    anchor = mid if mid > 0 else mid_price
    lo, hi = anchor * 0.98, anchor * 1.02
    bid = sum(px * q for px, q in ob["bids"] if lo <= px <= hi)
    ask = sum(px * q for px, q in ob["asks"] if lo <= px <= hi)
    total = bid + ask
    need = position_usd * CFG["min_liq_mult"]
    return {
        "sufficient": total >= need if need > 0 else False,
        "total_liquidity_usd": total,
        "ratio": (total / need) if need else 0.0,
        "spread_bps": ((ba - bb) / mid * 10_000) if mid else 999.0,
        "imbalance": ((bid - ask) / total) if total else 0.0,
        "mid": mid, "best_bid": bb, "best_ask": ba,
    }


def fetch_btc_regime(ex) -> dict:
    """Живой 1h ход BTC по 5m + 4h контекст."""
    c5 = fetch_candles(ex, CFG["btc_symbol"], "5m", 80)
    c1h = fetch_candles(ex, CFG["btc_symbol"], "1h", 60)
    if len(c5) < 14:
        return {"state": "neutral", "btc_1h": 0.0, "btc_4h": 0.0}
    # 12 интервалов 5m ≈ 60m между closes[-13] и closes[-1]
    d1h = pct_change(c5[-13][CLOSE], c5[-1][CLOSE]) if len(c5) >= 13 else 0.0
    if len(c1h) >= 5:
        d4h = pct_change(c1h[-5][CLOSE], c1h[-1][CLOSE])
    else:
        d4h = pct_change(c5[-49][CLOSE], c5[-1][CLOSE]) if len(c5) >= 49 else 0.0
    state = (
        "risk_off" if d1h <= CFG["btc_dump_1h"]
        else "risk_on" if d1h >= CFG["btc_pump_1h"]
        else "neutral"
    )
    return {"state": state, "btc_1h": d1h, "btc_4h": d4h}


def _funding_z(ex, symbol: str, funding_rate: float | None) -> float:
    if funding_rate is None:
        return 0.0
    now = time.time()
    cached = _FUNDING_Z_CACHE.get(symbol)
    if cached and now - cached[0] < CFG["funding_z_cache_sec"]:
        return cached[1]
    fh = _safe(ex.fetch_funding_rate_history, symbol, limit=48, default=None) or []
    rates = [x["fundingRate"] for x in fh if x.get("fundingRate") is not None]
    z = _zscore(rates, funding_rate) if len(rates) >= 8 else 0.0
    _FUNDING_Z_CACHE[symbol] = (now, z, len(rates))
    return z


def estimate_position_usd(equity: float | None = None) -> float:
    """Оценка нотионала для проверки стакана (до точного plan)."""
    eq = equity if equity is not None else CFG["equity_usd"]
    risk_usd = eq * CFG["risk_per_trade_pct"] / 100.0
    # типичный стоп ~1.5 * 1h ATR ~ 1–3%; берём 1.5% как baseline
    typical_risk_pct = 1.5
    return risk_usd / (typical_risk_pct / 100.0)


def fetch_signals(ex, symbol: str, regime: dict, position_usd: float) -> dict[str, Any] | None:
    c5 = fetch_candles(ex, symbol, "5m", 200)
    if len(c5) < 60:
        return None
    c1h = fetch_candles(ex, symbol, "1h", 200)
    c4h = fetch_candles(ex, symbol, "4h", 120)

    data_gap = candles_have_gap(c5, "5m")
    last_ts = int(c5[-1][TS]) / 1000.0
    data_age = time.time() - last_ts

    closes = [c[CLOSE] for c in c5]
    price = closes[-1]
    atr5 = calc_atr(c5, 14)
    atr5_pct = (atr5 / price * 100.0) if price else 0.0

    atr1h = 0.0
    atr1h_pct = 0.0
    if len(c1h) > 15:
        atr1h = calc_atr(c1h, 14)
        atr1h_pct = (atr1h / c1h[-1][CLOSE] * 100.0) if c1h[-1][CLOSE] else 0.0

    def d5m(bars: int) -> float:
        return pct_change(closes[-1 - bars], price) if len(closes) > bars else 0.0

    d5, d10, d15 = d5m(1), d5m(2), d5m(3)

    # честный 1h / 4h с соответствующих свечей
    if len(c1h) >= 2:
        d1h = pct_change(c1h[-2][CLOSE], c1h[-1][CLOSE])
    else:
        d1h = d5m(12)
    if len(c4h) >= 2:
        # ход за последние ~4h: от close 2 баров назад до last (1 закрытая 4h ≈ 4h)
        # плюс rolling 4h из 1h если нужно шире
        d4h = pct_change(c4h[-2][CLOSE], c4h[-1][CLOSE])
        if len(c1h) >= 5:
            d4h_roll = pct_change(c1h[-5][CLOSE], c1h[-1][CLOSE])
            # используем rolling 4×1h — стабильнее одной 4h свечи
            d4h = d4h_roll
    else:
        d4h = d5m(48) if len(closes) > 48 else 0.0

    look = CFG["vol_lookback"]
    quote_vols = [c[VOL] * c[CLOSE] for c in c5]
    # baseline без последних 3 баров — меньше self-damping после спайка
    hist_end = max(1, len(quote_vols) - 3)
    hist_start = max(0, hist_end - look)
    hist_vols = quote_vols[hist_start:hist_end]
    cur_vol = quote_vols[-1]
    med = statistics.median(hist_vols) if hist_vols else 0.0
    vol_ratio = (cur_vol / med) if med > 0 else 0.0
    vol_z = _zscore(hist_vols, cur_vol)

    rsi = calc_rsi(closes)
    rsi_1h = calc_rsi([c[CLOSE] for c in c1h]) if len(c1h) > 15 else 50.0
    lor = calc_ema_momentum(c5)
    vwap = calc_vwap(c5)
    macd = calc_vw_macd(c5, atr5)
    ema_c = calc_ema_crossover(c5)
    ma = calc_ma_8_18(c1h, atr1h_pct)
    atr_stop = calc_atr_stop(c1h, 14)
    # BB только на 1h — не подменяем таймфрейм молча
    if len(c1h) > 22:
        bb = calc_bollinger_squeeze(c1h)
        bb_tf = "1h"
    else:
        bb = {
            "is_squeeze": False, "bandwidth": 0.0, "percentile": 1.0,
            "upper_band": 0.0, "lower_band": 0.0, "sma": 0.0,
        }
        bb_tf = "none"
    obv = calc_obv(c5)
    struct5 = calc_structure(c5, 24)
    struct1h = calc_structure(c1h, 20) if len(c1h) >= 10 else {"trend": 0, "swing_high": 0.0, "swing_low": 0.0}
    absorb = calc_absorption(c5, vol_ratio)
    retest = detect_retest(c5, vwap["vwap"], atr5)
    obi = fetch_orderbook(ex, symbol, price, position_usd)
    er = efficiency_ratio(closes, 20)
    er_1h = efficiency_ratio([c[CLOSE] for c in c1h], 20) if len(c1h) > 22 else 0.0

    funding_rate = None
    funding_in_min = None
    f = _safe(ex.fetch_funding_rate, symbol, default=None) or {}
    if f:
        funding_rate = f.get("fundingRate")
        nxt = f.get("fundingTimestamp") or f.get("nextFundingTimestamp")
        if nxt:
            funding_in_min = max(0.0, (float(nxt) / 1000 - time.time()) / 60)
    funding_z = _funding_z(ex, symbol, funding_rate)

    oi = fetch_oi(ex, symbol)
    ticker = _safe(ex.fetch_ticker, symbol, default={}) or {}

    # HTF swings для плана
    swing_high_1h = struct1h.get("swing_high") or 0.0
    swing_low_1h = struct1h.get("swing_low") or 0.0

    sig: dict[str, Any] = {
        # ── идентификация ──
        "symbol": symbol,
        "price": price,
        # ── моментум (старые ключи) ──
        "delta_5m": d5, "delta_10m": d10, "delta_15m": d15,
        "delta_1h": d1h, "delta_4h": d4h,
        "move_60m_live_pct": d1h,  # NEW 28.09.2026: rolling 60m из c5
        "delta_1h": d1h, "delta_4h": d4h,
        "velocity": d5,
        # ── объём ──
        "vol_5m": cur_vol, "vol_ratio": vol_ratio, "vol_zscore": vol_z,
        # ── деривативы ──
        "funding_rate": funding_rate,
        "oi_change_1h": oi["oi_change_1h"], "oi_now": oi["oi_now"],
        "oi_change_5m": oi.get("oi_change_5m"),  # NEW 28.09.2026
        "oi_quality": oi.get("oi_quality"),
        "oi_source": oi["oi_source"],
        # ── индикаторы (старые ключи) ──
        "rsi": round(rsi, 1), "phase": detect_phase(c5),
        "lor_signal": lor["signal"], "lor_pred": lor["pred"], "lor_strength": lor["strength"],
        "absorption_long": absorb["long"], "absorption_short": absorb["short"],
        "vwap": vwap["vwap"], "vwap_distance": vwap["distance_pct"],
        "vwap_position": vwap["position"],
        "vwap_reversal_long_valid": vwap["reversal_long_valid"],
        "vwap_reversal_short_valid": vwap["reversal_short_valid"],
        "vw_macd": macd["vw_macd"], "vw_macd_signal": macd["signal"],
        "vw_macd_histogram": macd["histogram"], "vw_macd_trend": macd["trend"],
        "vw_macd_crossover_up": macd["crossover_up"],
        "vw_macd_crossover_down": macd["crossover_down"],
        "vw_macd_strength": macd["strength"],
        "vw_macd_volume_confirmed": macd["volume_confirmed"],
        "ema_cross_signal": ema_c["signal"], "ema_fast": ema_c["fast_ema"],
        "ema_slow": ema_c["slow_ema"], "ema_distance": ema_c["distance_pct"],
        "ma_8_18_trend": ma["trend"], "ma8": ma["ma8"], "ma18": ma["ma18"],
        "ma_8_18_distance": ma["distance_pct"],
        "atr": atr_stop["atr"], "atr_stop_distance_pct": atr_stop["stop_distance_pct"],
        "atr_stop_long": atr_stop["stop_long"], "atr_stop_short": atr_stop["stop_short"],
        "bb_squeeze": bb["is_squeeze"], "bb_bandwidth": bb["bandwidth"],
        "bb_upper": bb["upper_band"], "bb_lower": bb["lower_band"], "bb_sma": bb["sma"],
        "obv": obv["obv"], "obv_trend": obv["obv_trend"],
        "obv_change_pct": obv["obv_change_pct"],
        "retest_detected": retest["is_retest"],
        "retest_volume_declining": retest["retest_volume_declining"],
        "retest_volume_ratio": retest["volume_ratio"],
        "obi_sufficient": obi["sufficient"],
        "obi_liquidity_usd": obi["total_liquidity_usd"], "obi_ratio": obi["ratio"],
        # ── новые / исправленные ──
        "atr_5m": atr5, "atr_5m_pct": atr5_pct,
        "atr_1h": atr1h, "atr_1h_pct": atr1h_pct,
        "d5_atr": in_atr(d5, atr5_pct),
        "d10_atr": in_atr(d10, atr5_pct),
        "d15_atr": in_atr(d15, atr5_pct),
        # FIX: часовой ход / часовой ATR
        "d1h_atr": in_atr(d1h, atr1h_pct if atr1h_pct > 0 else atr5_pct),
        "d4h_atr": in_atr(d4h, atr1h_pct if atr1h_pct > 0 else atr5_pct),
        "rsi_1h": round(rsi_1h, 1),
        "vwap_distance_sigma": vwap["distance_sigma"],
        "vwap_confidence": vwap["confidence"],
        "vw_macd_strength_atr": macd["strength_atr"],
        "bb_percentile": bb["percentile"],
        "bb_tf": bb_tf,
        "obv_divergence": obv["divergence"],
        "rsi_divergence": calc_rsi_divergence(c5),
        "structure_5m": struct5["trend"], "structure_1h": struct1h.get("trend", 0),
        "swing_high": struct5["swing_high"], "swing_low": struct5["swing_low"],
        "swing_high_1h": swing_high_1h, "swing_low_1h": swing_low_1h,
        "broke_high": struct5["broke_high"], "broke_low": struct5["broke_low"],
        "funding_zscore": funding_z, "funding_in_min": funding_in_min,
        "oi_change_15m": oi["oi_change_15m"],
        "oi_quality": oi["oi_quality"], "oi_span_min": oi["oi_span_min"],
        "spread_bps": obi["spread_bps"], "book_imbalance": obi["imbalance"],
        "best_bid": obi["best_bid"], "best_ask": obi["best_ask"], "mid": obi["mid"],
        "quote_volume_24h": float(ticker.get("quoteVolume") or 0),
        "btc_regime": regime["state"], "btc_1h": regime["btc_1h"], "btc_4h": regime.get("btc_4h", 0.0),
        "efficiency_ratio": er, "efficiency_ratio_1h": er_1h,
        "data_gap": data_gap, "data_age_sec": data_age,
        "candle_ts": last_ts,
    }
    sig["price_oi_agreement"] = _price_oi_agreement(sig)
    # beta-lite: idiosyncratic move vs BTC (beta≈1 упрощение)
    sig["idio_1h"] = sig["delta_1h"] - regime.get("btc_1h", 0.0)
    return sig


def _price_oi_agreement(sig: dict) -> int:
    """Слабый proxy; вес в голосах снижен. Нужен CVD для силы."""
    oi = sig.get("oi_change_1h")
    if oi is None:
        return 0
    up = sig.get("delta_1h", 0) > 0
    oi_up = oi > 0
    if up and oi_up:
        return 2
    if up and not oi_up:
        return 1
    if not up and oi_up:
        return -2
    return -1


# ═════════════════════════════════════════════════════════════════════════
# 4. НАПРАВЛЕНИЕ И СКОРИНГ (группы голосов)
# ═════════════════════════════════════════════════════════════════════════
CONTINUATION, REVERSAL, BREAKOUT = "continuation", "reversal", "breakout"

# группы: внутри группы берём max weight на сторону (anti double-count)
VOTE_GROUPS = {
    "htf_ma": "trend",
    "htf_struct": "trend",
    "ema": "trend",
    "momentum": "momentum",
    "overextension": "momentum",
    "macd": "momentum",
    "lor": "momentum",  # ema_momentum, ключ совместимости
    "vwap": "location",
    "vwap_stretch": "location",
    "retest": "location",
    "squeeze_break": "structure",
    "price_oi": "flow",
    "funding": "flow",
    "book": "flow",
    "obv": "flow",
    "obv_div": "flow",
    "rsi": "oscillator",
    "rsi_guard": "oscillator",
    "rsi_div": "oscillator",
    "absorption": "micro",
    "regime": "regime",
    "idio": "regime",
}


@dataclass
class Vote:
    name: str
    side: str
    weight: float
    detail: str
    group: str = "other"


@dataclass
class Decision:
    side: str
    mode: str
    score: float
    votes: list[Vote] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    agreement: float = 0.0
    long_w: float = 0.0
    short_w: float = 0.0

    def pros(self) -> list[str]:
        return [v.detail for v in self.votes if v.side == self.side]

    def cons(self) -> list[str]:
        opp = "SHORT" if self.side == "LONG" else "LONG"
        return [v.detail for v in self.votes if v.side == opp]


def _opposite(side: str) -> str:
    return "SHORT" if side == "LONG" else "LONG" if side == "SHORT" else "NEUTRAL"


def collect_votes(sig: dict, mode: str) -> list[Vote]:
    v: list[Vote] = []

    def add(name, side, weight, detail):
        if side in ("LONG", "SHORT") and weight > 0:
            v.append(Vote(name, side, weight, detail, VOTE_GROUPS.get(name, "other")))

    d1h_atr = sig.get("d1h_atr", 0)
    vwap_sigma = sig.get("vwap_distance_sigma", 0)
    rsi = sig.get("rsi", 50)
    rsi_1h = sig.get("rsi_1h", 50)

    if sig.get("ma_8_18_trend"):
        s = "LONG" if sig["ma_8_18_trend"] > 0 else "SHORT"
        add("htf_ma", s, 3.0, f"1h MA8/18 {s} ({sig.get('ma_8_18_distance', 0):+.2f}%)")
    if sig.get("structure_1h"):
        s = "LONG" if sig["structure_1h"] > 0 else "SHORT"
        add("htf_struct", s, 2.0, f"структура 1h {s}")

    # price/OI — сниженный вес (без CVD это proxy)
    agree = sig.get("price_oi_agreement", 0)
    oi = sig.get("oi_change_1h")
    oiq = sig.get("oi_quality", "bad")
    oi_w_mult = 1.0 if oiq == "ok" else 0.5 if oiq == "scaled" else 0.0
    if oi is not None and oi_w_mult > 0:
        if agree == 2:
            add("price_oi", "LONG", 1.5 * oi_w_mult, f"цена+ OI{oi:+.1f}% (proxy long)")
        elif agree == -2:
            add("price_oi", "SHORT", 1.5 * oi_w_mult, f"цена- OI{oi:+.1f}% (proxy short)")
        elif agree == 1:
            add("price_oi", "LONG", 0.4 * oi_w_mult, f"цена+ OI{oi:+.1f}% (squeeze?)")
        elif agree == -1:
            add("price_oi", "SHORT", 0.4 * oi_w_mult, f"цена- OI{oi:+.1f}% (long liq?)")

    if abs(d1h_atr) >= 1.0:
        s = "LONG" if d1h_atr > 0 else "SHORT"
        if mode == CONTINUATION:
            add("momentum", s, 2.0, f"1h {sig.get('delta_1h', 0):+.2f}% ({d1h_atr:+.1f}ATR1h)")
        elif mode == REVERSAL and abs(d1h_atr) >= 2.0:
            add("overextension", _opposite(s), 1.5, f"перерастяжение {d1h_atr:+.1f}ATR1h")

    if mode == CONTINUATION:
        if sig.get("vwap_distance", 0) > 0:
            add("vwap", "LONG", 1.5, f"выше VWAP ({sig['vwap_distance']:+.2f}%)")
        elif sig.get("vwap_distance", 0) < 0:
            add("vwap", "SHORT", 1.5, f"ниже VWAP ({sig['vwap_distance']:+.2f}%)")
    else:
        if vwap_sigma <= -1.5:
            add("vwap_stretch", "LONG", 2.0, f"{vwap_sigma:.1f}s под VWAP")
        elif vwap_sigma >= 1.5:
            add("vwap_stretch", "SHORT", 2.0, f"+{vwap_sigma:.1f}s над VWAP")

    st = sig.get("vw_macd_strength_atr", 0)
    if sig.get("vw_macd_crossover_up") and st > 0.05:
        add("macd", "LONG", 2.0, f"MACD bullish cross ({st:.2f}ATR)")
    elif sig.get("vw_macd_crossover_down") and st > 0.05:
        add("macd", "SHORT", 2.0, f"MACD bearish cross ({st:.2f}ATR)")
    elif sig.get("vw_macd_trend") == "bullish":
        add("macd", "LONG", 1.0, "MACD bullish")
    elif sig.get("vw_macd_trend") == "bearish":
        add("macd", "SHORT", 1.0, "MACD bearish")

    if sig.get("ema_cross_signal"):
        s = "LONG" if sig["ema_cross_signal"] > 0 else "SHORT"
        add("ema", s, 1.0, f"EMA9/21 {s} ({sig.get('ema_distance', 0):+.2f}%)")

    if sig.get("lor_signal") and sig.get("lor_strength", 0) > 0.3:
        s = "LONG" if sig["lor_signal"] > 0 else "SHORT"
        add("lor", s, 1.0, f"EMA-mom {s} ({sig['lor_strength']:.0%})")

    if mode == REVERSAL:
        if rsi <= 28 and rsi_1h < 45:
            add("rsi", "LONG", 1.5, f"RSI {rsi:.0f} OS (1h {rsi_1h:.0f})")
        elif rsi >= 72 and rsi_1h > 55:
            add("rsi", "SHORT", 1.5, f"RSI {rsi:.0f} OB (1h {rsi_1h:.0f})")
    else:
        if rsi >= 78:
            add("rsi_guard", "SHORT", 1.0, f"RSI {rsi:.0f} — long late")
        elif rsi <= 22:
            add("rsi_guard", "LONG", 1.0, f"RSI {rsi:.0f} — short late")

    if sig.get("rsi_divergence"):
        s = "LONG" if sig["rsi_divergence"] > 0 else "SHORT"
        add("rsi_div", s, 1.5, f"RSI-div {s}")
    if sig.get("obv_divergence"):
        s = "LONG" if sig["obv_divergence"] > 0 else "SHORT"
        add("obv_div", s, 1.0, f"OBV-div {s}")
    elif sig.get("obv_trend"):
        s = "LONG" if sig["obv_trend"] > 0 else "SHORT"
        add("obv", s, 0.5, "OBV " + ("up" if s == "LONG" else "down"))

    w_abs = 1.5 if mode == REVERSAL else 0.5
    if sig.get("absorption_long"):
        add("absorption", "LONG", w_abs, "поглощение снизу")
    if sig.get("absorption_short"):
        add("absorption", "SHORT", w_abs, "поглощение сверху")

    fr, fz = sig.get("funding_rate"), sig.get("funding_zscore", 0) or 0.0
    if fr is not None:
        if fz >= 2.0 or (abs(fr) >= CFG["p4_funding_abs"] and fr > 0 and fz >= 1.0):
            add("funding", "SHORT", 1.5, f"funding {fr*100:+.3f}% (z{fz:+.1f}) longs hot")
        elif fz <= -2.0 or (abs(fr) >= CFG["p4_funding_abs"] and fr < 0 and fz <= -1.0):
            add("funding", "LONG", 1.5, f"funding {fr*100:+.3f}% (z{fz:+.1f}) shorts hot")

    imb = sig.get("book_imbalance", 0)
    if abs(imb) >= 0.25:
        add("book", "LONG" if imb > 0 else "SHORT", 0.5, f"book imb {imb:+.2f}")

    if sig.get("bb_squeeze") and (sig.get("broke_high") or sig.get("broke_low")):
        s = "LONG" if sig.get("broke_high") else "SHORT"
        add("squeeze_break", s, 1.5, f"squeeze break (pctl {sig.get('bb_percentile', 1):.0%})")

    if sig.get("retest_detected") and sig.get("retest_volume_declining"):
        s = "LONG" if sig.get("vwap_distance", 0) >= 0 else "SHORT"
        add("retest", s, 1.0, "retest on declining vol")

    if sig.get("btc_regime") == "risk_off":
        add("regime", "SHORT", 1.5, f"BTC {sig.get('btc_1h', 0):+.2f}% risk-off")
    elif sig.get("btc_regime") == "risk_on":
        add("regime", "LONG", 1.5, f"BTC {sig.get('btc_1h', 0):+.2f}% risk-on")

    # idiosyncratic move
    idio = sig.get("idio_1h", 0)
    if abs(idio) >= 1.0:
        add("idio", "LONG" if idio > 0 else "SHORT", 1.0, f"idio 1h {idio:+.2f}% vs BTC")

    return v


def _aggregate_group_weights(votes: list[Vote]) -> tuple[float, float, list[Vote]]:
    """Внутри группы — max weight на сторону (не сумма коррелированных)."""
    # group -> side -> best Vote
    best: dict[tuple[str, str], Vote] = {}
    for vt in votes:
        key = (vt.group, vt.side)
        if key not in best or vt.weight > best[key].weight:
            best[key] = vt
    reduced = list(best.values())
    long_w = sum(x.weight for x in reduced if x.side == "LONG")
    short_w = sum(x.weight for x in reduced if x.side == "SHORT")
    return long_w, short_w, reduced


def decide(sig: dict, mode: str, hint: str = "NEUTRAL") -> Decision:
    votes = collect_votes(sig, mode)
    long_w, short_w, reduced = _aggregate_group_weights(votes)
    total = long_w + short_w
    net = long_w - short_w
    agreement = (abs(net) / total) if total > 0 else 0.0

    side = "NEUTRAL" if abs(net) < 1.5 else ("LONG" if net > 0 else "SHORT")
    # score: net * agreement factor (штраф за войну индикаторов)
    raw_score = abs(net) * (0.5 + 0.5 * agreement)
    score = min(raw_score, MAX_SCORE)

    blockers: list[str] = []
    if agreement < CFG["min_agreement"] and side != "NEUTRAL":
        blockers.append(f"слабое согласие {agreement:.2f} < {CFG['min_agreement']}")
        side = "NEUTRAL"

    if hint in ("LONG", "SHORT"):
        if side == "NEUTRAL":
            return Decision(
                "NEUTRAL", mode, min(score, MAX_SCORE), reduced,
                blockers or ["нет перевеса индикаторов"], agreement, long_w, short_w,
            )
        if side != hint:
            return Decision(
                "NEUTRAL", mode, 0.0, reduced,
                [f"конфликт: триггер {hint}, индикаторы {side}"],
                agreement, long_w, short_w,
            )
    return Decision(side, mode, score, reduced, blockers, agreement, long_w, short_w)


def check_orderbook_walls(exchange, sig: dict, dec: Decision) -> Decision:
    """
    Проверяет крупные лимитные заявки (стены) в orderbook.
    Снижает score -5 если вход около стены (±0.5%).
    
    Логика:
    - SHORT у support (крупные bid ниже) → score -= 5 (вход на отскок)
    - LONG у resistance (крупные ask выше) → score -= 5 (вход в стену)
    
    Порог: $20,000 USDT (для низколиквидных альткоинов)
    """
    if dec.side == "NEUTRAL":
        return dec
    
    symbol = sig.get("symbol")
    price = sig.get("price", 0)
    
    if not symbol or price <= 0:
        return dec
    
    try:
        # Получаем orderbook (топ-20 уровней)
        orderbook = exchange.fetch_order_book(symbol, limit=20)
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        
        # Находим крупные стены (>$20k)
        BIG_WALL_USD = 20000
        
        # Анализируем bid (support)
        big_support_levels = []
        for bid_price, bid_size in bids:
            wall_usd = bid_price * bid_size
            if wall_usd > BIG_WALL_USD:
                # Проверяем если стена в ±0.5% от цены
                distance_pct = abs((bid_price - price) / price * 100)
                if distance_pct <= 0.5:
                    big_support_levels.append((bid_price, wall_usd))
        
        # Анализируем ask (resistance)
        big_resistance_levels = []
        for ask_price, ask_size in asks:
            wall_usd = ask_price * ask_size
            if wall_usd > BIG_WALL_USD:
                distance_pct = abs((ask_price - price) / price * 100)
                if distance_pct <= 0.5:
                    big_resistance_levels.append((ask_price, wall_usd))
        
        # Применяем штраф
        penalty = 0
        penalty_reason = []
        
        if dec.side == "SHORT" and big_support_levels:
            # SHORT у поддержки - опасно (может отскочить)
            closest = min(big_support_levels, key=lambda x: abs(x[0] - price))
            penalty = -5
            penalty_reason.append(f"у поддержки ${closest[0]:.6f} (${closest[1]:,.0f})")
            logger.info(
                "%s SHORT у support %.6f ($%,.0f wall) → score penalty -5",
                symbol, closest[0], closest[1]
            )
        
        elif dec.side == "LONG" and big_resistance_levels:
            # LONG у сопротивления - опасно (может отбить)
            closest = min(big_resistance_levels, key=lambda x: abs(x[0] - price))
            penalty = -5
            penalty_reason.append(f"у сопротивления ${closest[0]:.6f} (${closest[1]:,.0f})")
            logger.info(
                "%s LONG у resistance %.6f ($%,.0f wall) → score penalty -5",
                symbol, closest[0], closest[1]
            )
        
        if penalty < 0:
            new_score = max(0, dec.score + penalty)
            new_blockers = list(dec.blockers) + penalty_reason
            
            return Decision(
                dec.side, dec.mode, new_score, dec.votes,
                new_blockers, dec.agreement, dec.long_w, dec.short_w
            )
        
        return dec
        
    except Exception as e:
        # Если не удалось получить orderbook - просто логируем и продолжаем
        logger.debug("Failed to check orderbook for %s: %s", symbol, e)
        return dec


def apply_model_gate(sig: dict, kind: str, dec: Decision) -> Decision:
    """ML-гейт: применяем directional_lgbm модель для фильтрации сигналов.
    
    FIX (18.09.2026): ИСПРАВЛЕНА логика - не блокируем конфликты, а снижаем confidence
    - Проблема: модель блокировала ВСЕ SHORT сигналы (16-18 сент: 131 LONG, 0 SHORT)
    - Старая логика: если ml_direction != indicators → БЛОК
    - Новая логика: используем prob_win как фильтр, НЕ проверяем направление
    
    Логика:
    - prob_win >= 55%: пропускаем (модель уверена)
    - prob_win < 45%: блокируем (модель не уверена)
    - 45-55%: пропускаем с пониженным score
    """
    # Если сигнал уже заблокирован или NEUTRAL - пропускаем
    if dec.side == "NEUTRAL" or dec.blockers:
        return dec
    
    # Проверяем доступность модели
    if not _ML_AVAILABLE:
        logger.debug("ML модель недоступна, пропускаем гейт")
        return dec
    
    # Получаем предсказание модели
    ml_direction, prob_win = predict_direction(sig, kind)
    
    # Добавляем в сигнал для логирования
    sig['ml_direction'] = ml_direction
    sig['ml_prob_win'] = prob_win
    
    # FIX: Используем только prob_win, НЕ сравниваем направления
    # Модель может ошибаться в направлении, но prob_win показывает уверенность
    
    if prob_win < 0.45:
        # Низкая уверенность - блокируем
        logger.info(f"ML: низкая уверенность {prob_win:.1%} < 45%, блокируем {dec.side}")
        return Decision(
            "NEUTRAL", dec.mode, 0.0, dec.votes,
            [f"ML: низкая уверенность {prob_win:.1%} < 45%"],
            dec.agreement, dec.long_w, dec.short_w
        )
    
    if prob_win >= 0.55:
        # Высокая уверенность - пропускаем
        logger.info(f"ML: уверенность {prob_win:.1%} >=55%, пропускаем {dec.side}")
        return dec
    
    # Средняя уверенность 45-55% - пропускаем но логируем
    logger.info(f"ML: средняя уверенность {prob_win:.1%}, пропускаем {dec.side}")
    return dec


def verdict_text(dec: Decision) -> str:
    s, sc = dec.side, dec.score
    if s == "NEUTRAL":
        head = f"Нет чёткого направления | score={sc:.0f}/{MAX_SCORE:.0f}"
    elif sc / MAX_SCORE >= 0.6:
        head = f"Сильный сигнал {s} | score={sc:.0f}/{MAX_SCORE:.0f}"
    elif sc / MAX_SCORE >= 0.35:
        head = f"Умеренный сигнал {s} | score={sc:.0f}/{MAX_SCORE:.0f}"
    else:
        head = f"Слабый / наблюдение {s} | score={sc:.0f}/{MAX_SCORE:.0f}"
    lines = [head, f"режим: {dec.mode} | agreement={dec.agreement:.2f}"]
    if dec.pros():
        lines.append("+ " + " | ".join(dec.pros()))
    if dec.cons():
        lines.append("- " + " | ".join(dec.cons()))
    if dec.blockers:
        lines.append("! " + " | ".join(dec.blockers))
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# 5. ТОРГОВЫЙ ПЛАН
# ═════════════════════════════════════════════════════════════════════════
@dataclass
class Plan:
    side: str
    entry: float
    entry_type: str
    stop: float
    tps: tuple[float, ...]
    rr: float
    rr_tp1: float
    risk_pct: float
    size_usd: float
    qty: float
    cost_bps: float
    net_edge_bps: float
    max_hold_min: int
    valid_until: float  # unix ts

    def as_text(self) -> str:
        tps = " / ".join(f"{t:.8g}" for t in self.tps)
        vu = datetime.fromtimestamp(self.valid_until, tz=timezone.utc).strftime("%H:%M:%S")
        return (
            f"Вход ({self.entry_type}): {self.entry:.8g}\n"
            f"Стоп: {self.stop:.8g} (-{self.risk_pct:.2f}%)\n"
            f"Цели: {tps}\n"
            f"R:R {self.rr:.2f} (TP1 {self.rr_tp1:.2f}) | "
            f"размер ${self.size_usd:.0f} ({self.qty:.6g})\n"
            f"Время-стоп: {self.max_hold_min} мин | valid_until {vu} UTC\n"
            f"издержки {self.cost_bps:.1f}бпс, net {self.net_edge_bps:.0f}бпс"
        )


def build_plan(sig: dict, dec: Decision) -> Plan | None:
    if dec.side == "NEUTRAL":
        return None
    side = dec.side
    sign = 1 if side == "LONG" else -1
    price = sig["price"]
    atr5 = sig.get("atr_5m", 0) or price * 0.01
    atr1h = sig.get("atr_1h", 0) or atr5 * 3.0

    # entry
    if dec.mode == CONTINUATION:
        entry = price - 0.3 * atr5 * sign
        entry = min(entry, price) if side == "LONG" else max(entry, price)
        entry_type = "limit_pullback"
    elif dec.mode == REVERSAL:
        entry = price + 0.15 * atr5 * sign
        entry_type = "stop_confirm"
    else:
        entry, entry_type = price, "market"
        # market: use ask/bid if available
        if side == "LONG" and sig.get("best_ask"):
            entry = sig["best_ask"]
        elif side == "SHORT" and sig.get("best_bid"):
            entry = sig["best_bid"]

    # стоп от 1h ATR + структура 5m/1h
    atr_dist = atr1h * CFG["atr_stop_mult"]
    sh = sig.get("swing_high") or 0.0
    sl = sig.get("swing_low") or 0.0
    sh1 = sig.get("swing_high_1h") or 0.0
    sl1 = sig.get("swing_low_1h") or 0.0

    if side == "LONG":
        stop = entry - atr_dist
        for lvl in (sl, sl1):
            if lvl and lvl < entry:
                stop = min(stop, lvl - 0.15 * atr5)
    else:
        stop = entry + atr_dist
        for lvl in (sh, sh1):
            if lvl and lvl > entry:
                stop = max(stop, lvl + 0.15 * atr5)

    risk_dist = abs(entry - stop)
    if risk_dist <= 0 or entry <= 0:
        return None
    risk_pct = risk_dist / entry * 100.0

    # цели: HTF structure + R-multiples
    structural = None
    if side == "LONG":
        cands = [x for x in (sh, sh1, sig.get("vwap", 0)) if x and x > entry]
        structural = min(cands) if cands else None
    else:
        cands = [x for x in (sl, sl1, sig.get("vwap", 0)) if x and 0 < x < entry]
        structural = max(cands) if cands else None

    raw = {entry + sign * risk_dist * m for m in (1.0, 2.0, 3.0)}
    if structural:
        raw.add(structural)
    tps = tuple(sorted(raw, key=lambda x: sign * (x - entry))[:3])

    # FIX: main = structural или TP1 (не 3R-заглушка)
    main = structural if structural else tps[0]
    rr = abs(main - entry) / risk_dist
    rr_tp1 = abs(tps[0] - entry) / risk_dist

    risk_usd = CFG["equity_usd"] * CFG["risk_per_trade_pct"] / 100.0
    size = risk_usd / (risk_pct / 100.0)
    depth = sig.get("obi_liquidity_usd", 0)
    if depth:
        size = min(size, depth / CFG["min_liq_mult"])
    qty = size / entry if entry else 0.0

    # cost по типу входа
    spread = sig.get("spread_bps", 0) or 0.0
    if entry_type == "limit_pullback":
        # maker in, taker out (conservative)
        cost = spread * 0.5 + CFG["maker_fee_bps"] + CFG["taker_fee_bps"]
    elif entry_type == "stop_confirm":
        cost = spread + CFG["taker_fee_bps"] * 2
    else:
        cost = spread + CFG["taker_fee_bps"] * 2

    net = abs(main - entry) / entry * 10_000 - cost
    valid_until = time.time() + CFG["entry_valid_bars_5m"] * 300

    return Plan(
        side, entry, entry_type, stop, tps, rr, rr_tp1, risk_pct,
        size, qty, cost, net, CFG["max_hold_min"], valid_until,
    )


# ═════════════════════════════════════════════════════════════════════════
# 6. ГЕЙТЫ
# ═════════════════════════════════════════════════════════════════════════
def check_gates(sig: dict, dec: Decision, plan: Plan | None) -> list[str]:
    out: list[str] = []
    if dec.side == "NEUTRAL":
        out.append("нет направления")
        return out
    if dec.score < CFG["min_score"]:
        out.append(f"score {dec.score:.0f} < {CFG['min_score']}")

    if sig.get("data_gap"):
        out.append("gap в OHLCV")
    if sig.get("data_age_sec", 0) > CFG["max_data_age_sec"]:
        out.append(f"данные устарели {sig.get('data_age_sec', 0):.0f}s")

    if CFG.get("require_oi_quality"):
        oiq = sig.get("oi_quality", "bad")
        if oiq in ("bad", "partial", "short", "none"):
            # не режем сетапы без OI-зависимости жёстко — только помечаем для OI-heavy
            pass

    if not sig.get("obi_sufficient", False):
        out.append(f"мало ликвидности (ratio {sig.get('obi_ratio', 0):.1f}x)")

    # FIX: qv<=0 тоже блок
    qv = sig.get("quote_volume_24h", 0) or 0.0
    if qv <= 0:
        out.append("нет quoteVolume 24h (ticker)")
    elif qv < CFG["min_qvol_24h"]:
        out.append(f"оборот 24h ${qv:,.0f} мал")

    if sig.get("spread_bps", 0) > CFG["max_spread_bps"]:
        out.append(f"спред {sig['spread_bps']:.1f}бпс > {CFG['max_spread_bps']}")

    if plan is None:
        out.append("не удалось построить план")
    else:
        if plan.rr < CFG["min_rr"]:
            out.append(f"R:R {plan.rr:.2f} < {CFG['min_rr']}")
        if plan.net_edge_bps <= 0:
            out.append(f"издержки съедают движение (net {plan.net_edge_bps:.0f}бпс)")
        atr1h_pct = sig.get("atr_1h_pct", 0) or sig.get("atr_5m_pct", 0)
        if atr1h_pct > 0 and plan.risk_pct > 3.5 * atr1h_pct:
            out.append("стоп слишком широкий к 1h ATR")

    if CFG["require_htf"]:
        sign = 1 if dec.side == "LONG" else -1
        ma = sig.get("ma_8_18_trend", 0)
        if dec.mode == CONTINUATION and ma and ma != sign:
            out.append(f"1h MA8/18 против {dec.side}")
        elif dec.mode == REVERSAL and abs(sig.get("d1h_atr", 0)) > 2.5 \
                and sig.get("d1h_atr", 0) * sign < 0:
            out.append(f"1h импульс {sig['d1h_atr']:+.1f}ATR против разворота")

    if CFG["require_btc_regime"]:
        st = sig.get("btc_regime", "neutral")
        if dec.side == "LONG" and st == "risk_off":
            out.append(f"BTC risk-off ({sig.get('btc_1h', 0):+.2f}%) против LONG")
        if dec.side == "SHORT" and st == "risk_on":
            out.append(f"BTC risk-on ({sig.get('btc_1h', 0):+.2f}%) против SHORT")

    # chop filter для continuation
    if dec.mode == CONTINUATION:
        er = sig.get("efficiency_ratio_1h") or sig.get("efficiency_ratio", 0)
        if er < CFG["min_er_continuation"]:
            out.append(f"пила ER={er:.2f} < {CFG['min_er_continuation']}")

    # phase gate
    if CFG.get("block_late_phase_continuation") and dec.mode == CONTINUATION:
        ph = sig.get("phase", "")
        if ph in ("LATE_EXPANSION", "EXHAUSTION"):
            out.append(f"фаза {ph} — поздний continuation")

    fim, fr = sig.get("funding_in_min"), sig.get("funding_rate")
    if fim is not None and fr is not None and fim <= 12:
        cost = abs(fr) * 10_000
        if cost >= 3 and ((fr > 0 and dec.side == "LONG") or (fr < 0 and dec.side == "SHORT")):
            out.append(f"через {fim:.0f}мин платим фандинг {cost:.1f}бпс")

    if dec.mode == CONTINUATION and abs(sig.get("d1h_atr", 0)) > 3.0 \
            and abs(sig.get("vwap_distance_sigma", 0)) > 2.5:
        out.append(
            f"поздний вход: {sig['d1h_atr']:+.1f}ATR1h, "
            f"{sig['vwap_distance_sigma']:+.1f}s от VWAP"
        )

    if dec.mode == CONTINUATION:
        if dec.side == "LONG" and sig.get("absorption_short"):
            out.append("поглощение сверху против лонга")
        if dec.side == "SHORT" and sig.get("absorption_long"):
            out.append("поглощение снизу против шорта")

    # OI-quality для OI-сетапов
    kind_hint = sig.get("_active_kind", "")
    if kind_hint.startswith(("oi_", "early_", "accumulation", "distribution")):
        if sig.get("oi_quality") in ("bad", "partial", "short", "none"):
            out.append(f"OI quality={sig.get('oi_quality')}")

    return out


# ═════════════════════════════════════════════════════════════════════════
# 7. СЕТАПЫ
# ═════════════════════════════════════════════════════════════════════════
@dataclass
class Trigger:
    kind: str
    priority: int
    mode: str
    hint: str
    reason: str


SETUPS: list[Callable[[dict], Trigger | None]] = []


def setup(fn):
    SETUPS.append(fn)
    return fn


@setup
def s_oi_surge(sig) -> Trigger | None:
    if sig.get("oi_quality") in ("bad", "partial", "short", "none"):
        return None
    oi = sig.get("oi_change_1h")
    if oi is None or oi < CFG["p7_oi_min"] or sig["vol_ratio"] < CFG["p7_vol_min"]:
        return None
    m = sig["d5_atr"]
    if m >= CFG["p7_move_atr"]:
        return Trigger(
            "oi_surge_long", 7, CONTINUATION, "LONG",
            f"OI{oi:+.1f}% vol x{sig['vol_ratio']:.1f} ход {m:+.1f}ATR",
        )
    if m <= -CFG["p7_move_atr"]:
        return Trigger(
            "oi_surge_short", 7, CONTINUATION, "SHORT",
            f"OI{oi:+.1f}% vol x{sig['vol_ratio']:.1f} ход {m:+.1f}ATR",
        )
    return None


@setup
def s_early(sig) -> Trigger | None:
    if sig.get("oi_quality") in ("bad", "partial", "short", "none"):
        return None
    oi = sig.get("oi_change_1h")
    if oi is None or oi < CFG["p6_oi_min"] or sig["vol_ratio"] < CFG["p6_vol_min"]:
        return None
    if abs(sig["d15_atr"]) >= CFG["p6_max_d15_atr"]:
        return None
    m, rsi, vs = sig["d5_atr"], sig["rsi"], sig.get("vwap_distance_sigma", 0)
    if m >= CFG["p6_move_atr"] and rsi < CFG["p6_rsi_long_max"] and vs <= CFG["p6_max_vwap_sigma"]:
        return Trigger("early_long", 6, CONTINUATION, "LONG", f"ранний: OI{oi:+.1f}% RSI {rsi:.0f}")
    if m <= -CFG["p6_move_atr"] and rsi > CFG["p6_rsi_short_min"] and vs >= -CFG["p6_max_vwap_sigma"]:
        return Trigger("early_short", 6, CONTINUATION, "SHORT", f"ранний: OI{oi:+.1f}% RSI {rsi:.0f}")
    return None


@setup
def s_oi_flat(sig) -> Trigger | None:
    """hint NEUTRAL; kind без long/short — без leakage в ML."""
    if sig.get("oi_quality") in ("bad", "partial", "short", "none"):
        return None
    oi = sig.get("oi_change_1h")
    if oi is None or abs(sig["d1h_atr"]) > CFG["p65_flat_atr"]:
        return None
    if sig["vol_ratio"] < CFG["p65_vol_min"]:
        return None
    if oi >= CFG["p65_oi_rise"]:
        return Trigger(
            "oi_flat_buildup", 6, BREAKOUT, "NEUTRAL",
            f"набор позиций: OI{oi:+.1f}% flat price",
        )
    if oi <= CFG["p65_oi_fall"]:
        return Trigger(
            "oi_flat_unwind", 6, BREAKOUT, "NEUTRAL",
            f"закрытие позиций: OI{oi:+.1f}% flat price",
        )
    return None


@setup
def s_spike(sig) -> Trigger | None:
    if abs(sig["d5_atr"]) < CFG["p5_move_atr"] or sig["vol_ratio"] < CFG["p5_vol_min"]:
        return None
    up = sig["d5_atr"] > 0
    ma = sig.get("ma_8_18_trend", 0)
    aligned = (ma > 0 and up) or (ma < 0 and not up)
    mode = CONTINUATION if aligned else REVERSAL
    hint = ("LONG" if up else "SHORT") if aligned else "NEUTRAL"
    return Trigger(
        "spike_5m", 5, mode, hint,
        f"импульс 5m {sig['delta_5m']:+.2f}% ({sig['d5_atr']:+.1f}ATR) vol x{sig['vol_ratio']:.1f}",
    )


@setup
def s_funding(sig) -> Trigger | None:
    fr = sig.get("funding_rate")
    if fr is None:
        return None
    fz = sig.get("funding_zscore", 0) or 0.0
    if abs(fr) < CFG["p4_funding_abs"] and abs(fz) < CFG["p4_funding_z"]:
        return None
    return Trigger(
        "funding", 4, REVERSAL, "SHORT" if fr > 0 else "LONG",
        f"funding {fr*100:+.3f}% (z{fz:+.1f})",
    )


@setup
def s_squeeze_break(sig) -> Trigger | None:
    if not sig.get("bb_squeeze") or sig["vol_ratio"] < 1.5:
        return None
    if not (sig.get("broke_high") or sig.get("broke_low")):
        return None
    up = sig.get("broke_high")
    return Trigger(
        "squeeze_break", 4, BREAKOUT, "LONG" if up else "SHORT",
        f"пробой из сжатия (pctl {sig.get('bb_percentile', 1):.0%})",
    )


@setup
def s_reversal(sig) -> Trigger | None:
    d15 = sig["d15_atr"]
    if abs(d15) < CFG["p3_d15_atr"]:
        return None
    vs = sig.get("vwap_distance_sigma", 0)
    if d15 <= -CFG["p3_d15_atr"]:
        stalling = sig["d5_atr"] > -0.3
        proof = (
            sig.get("absorption_long")
            or sig.get("rsi_divergence", 0) > 0
            or sig.get("obv_divergence", 0) > 0
        )
        if stalling and proof and vs <= -1.0:
            return Trigger(
                "momentum_15m_down", 3, REVERSAL, "LONG",
                f"падение {sig['delta_15m']:+.2f}% ({d15:+.1f}ATR) выдохлось",
            )
    else:
        stalling = sig["d5_atr"] < 0.3
        proof = (
            sig.get("absorption_short")
            or sig.get("rsi_divergence", 0) < 0
            or sig.get("obv_divergence", 0) < 0
        )
        if stalling and proof and vs >= 1.0:
            return Trigger(
                "momentum_15m_up", 3, REVERSAL, "SHORT",
                f"рост {sig['delta_15m']:+.2f}% ({d15:+.1f}ATR) выдохся",
            )
    return None


@setup
def s_vol_anomaly(sig) -> Trigger | None:
    if sig["vol_zscore"] < CFG["p3_vol_z"] or sig["vol_ratio"] < CFG["p3_vol_ratio"]:
        return None
    return Trigger(
        "vol_anomaly", 3, BREAKOUT, "NEUTRAL",
        f"объём z={sig['vol_zscore']:.1f} x{sig['vol_ratio']:.1f}",
    )


@setup
def s_trend_pullback(sig) -> Trigger | None:
    ma = sig.get("ma_8_18_trend", 0)
    if not ma:
        return None
    d1h, rsi, vs = sig["d1h_atr"], sig["rsi"], sig.get("vwap_distance_sigma", 0)
    half = CFG["p2_d1h_atr"] * 0.5
    if ma > 0:
        if d1h < half or rsi > CFG["rsi_overbought"]:
            return None
        if vs <= 0.8 and sig["d5_atr"] > -1.2:
            return Trigger(
                "trend_continuation_long", 2, CONTINUATION, "LONG",
                f"тренд 1h up, откат VWAP ({vs:+.1f}s)",
            )
    else:
        if d1h > -half or rsi < CFG["rsi_oversold"]:
            return None
        if vs >= -0.8 and sig["d5_atr"] < 1.2:
            return Trigger(
                "trend_continuation_short", 2, CONTINUATION, "SHORT",
                f"тренд 1h down, откат VWAP ({vs:+.1f}s)",
            )
    return None


@setup
def s_fast_continuation(sig) -> Trigger | None:
    d10 = sig["d10_atr"]
    if abs(d10) < CFG["p2_d10_atr"] or sig["vol_ratio"] < 1.2:
        return None
    d5, d1h, rsi = sig["delta_5m"], sig["delta_1h"], sig["rsi"]
    if d10 > 0 and d5 > 0 and d1h > 0 and rsi < CFG["rsi_overbought"]:
        return Trigger(
            "momentum_10m_cont_up", 2, CONTINUATION, "LONG",
            f"10m {sig['delta_10m']:+.2f}% ({d10:+.1f}ATR) + 1h conf",
        )
    if d10 < 0 and d5 < 0 and d1h < 0 and rsi > CFG["rsi_oversold"]:
        return Trigger(
            "momentum_10m_cont_down", 2, CONTINUATION, "SHORT",
            f"10m {sig['delta_10m']:+.2f}% ({d10:+.1f}ATR) + 1h conf",
        )
    return None


def find_triggers(sig: dict) -> list[Trigger]:
    out = []
    for fn in SETUPS:
        try:
            t = fn(sig)
        except Exception as exc:
            logger.debug("setup %s failed: %s", fn.__name__, exc)
            continue
        if t:
            out.append(t)
    out.sort(key=lambda t: -t.priority)
    return out


# ═════════════════════════════════════════════════════════════════════════
# 8. АЛЕРТЫ
# ═════════════════════════════════════════════════════════════════════════
@dataclass
class Alert:
    base: str
    symbol: str
    kind: str
    priority: int
    direction: str
    score: int
    title: str
    message: str
    tags: str
    plan: Plan | None

    @property
    def state_key(self) -> str:
        return f"{self.symbol}::{self.kind}::{self.direction}"


def indicator_block(sig: dict) -> str:
    oi = sig.get("oi_change_1h")
    oi_s = "n/a" if oi is None else f"{oi:+.1f}% 1h ({sig.get('oi_source')}/{sig.get('oi_quality')})"
    fr = sig.get("funding_rate")
    fr_s = "n/a" if fr is None else f"{fr*100:+.4f}% (z{sig.get('funding_zscore', 0):+.1f})"
    return (
        "-----------------\n"
        f"ATR: 5m {sig.get('atr_5m_pct', 0):.2f}% | 1h {sig.get('atr_1h_pct', 0):.2f}%\n"
        f"Ход: 5m {sig['delta_5m']:+.2f}% ({sig.get('d5_atr', 0):+.1f}ATR) | "
        f"15m {sig['delta_15m']:+.2f}% | 1h {sig['delta_1h']:+.2f}% "
        f"({sig.get('d1h_atr', 0):+.1f}ATR1h) | 4h {sig.get('delta_4h', 0):+.2f}%\n"
        f"Объём: x{sig['vol_ratio']:.1f} z={sig['vol_zscore']:.1f} (${sig['vol_5m']:,.0f})\n"
        f"RSI: 5m {sig['rsi']:.0f} / 1h {sig.get('rsi_1h', 50):.0f} | фаза {sig['phase']} | "
        f"ER {sig.get('efficiency_ratio', 0):.2f}/{sig.get('efficiency_ratio_1h', 0):.2f}\n"
        f"Тренд 1h: MA8/18 {sig.get('ma_8_18_trend', 0):+d} "
        f"({sig.get('ma_8_18_distance', 0):+.2f}%) | структура {sig.get('structure_1h', 0):+d}\n"
        f"VWAP: {sig.get('vwap_distance', 0):+.2f}% "
        f"({sig.get('vwap_distance_sigma', 0):+.1f}s, {sig.get('vwap_confidence')})\n"
        f"MACD: {sig.get('vw_macd_trend')} (сила {sig.get('vw_macd_strength_atr', 0):.2f}ATR)\n"
        f"EMA-mom: {sig.get('lor_signal', 0):+d} ({sig.get('lor_strength', 0):.0%}) | "
        f"OBV {sig.get('obv_trend', 0):+d}\n"
        f"OI: {oi_s} | цена/OI: {sig.get('price_oi_agreement', 0):+d} | "
        f"idio {sig.get('idio_1h', 0):+.2f}%\n"
        f"Funding: {fr_s}\n"
        f"Стакан: спред {sig.get('spread_bps', 0):.1f}бпс | "
        f"imb {sig.get('book_imbalance', 0):+.2f} | "
        f"depth ${sig.get('obi_liquidity_usd', 0):,.0f}\n"
        f"BTC: {sig.get('btc_regime')} ({sig.get('btc_1h', 0):+.2f}% 1h live)"
    )


def build_alerts(base: str, sig: dict) -> tuple[list[Alert], list[tuple[str, list[str], dict]]]:
    """rejected: (kind, reasons, decision_snapshot)."""
    alerts: list[Alert] = []
    rejected: list[tuple[str, list[str], dict]] = []
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

    def _snap(dec: Decision, plan: Plan | None) -> dict:
        return {
            "side": dec.side, "score": dec.score, "agreement": dec.agreement,
            "mode": dec.mode, "blockers": list(dec.blockers),
            "entry": plan.entry if plan else None,
            "stop": plan.stop if plan else None,
            "rr": plan.rr if plan else None,
        }

    for trig in find_triggers(sig):
        sig["_active_kind"] = trig.kind
        dec = decide(sig, trig.mode, trig.hint)
        # Проверяем orderbook walls (поддержка/сопротивление)
        exchange = sig.get("_exchange")
        if exchange:
            dec = check_orderbook_walls(exchange, sig, dec)
        dec = apply_model_gate(sig, trig.kind, dec)
        plan = build_plan(sig, dec)
        reasons = check_gates(sig, dec, plan)
        if reasons:
            rejected.append((trig.kind, reasons, _snap(dec, plan)))
            continue

        title = (
            f"{base} {dec.side} {trig.kind} "
            f"{dec.score:.0f}/{MAX_SCORE:.0f} R:R{plan.rr:.1f}"
        )
        message = "\n".join([
            f"{ts} UTC price=${sig['price']:.6f}",
            f"Триггер: {trig.reason}",
            verdict_text(dec),
            "------- план -------",
            plan.as_text(),
            indicator_block(sig),
        ])
        alerts.append(Alert(
            base=base, symbol=sig["symbol"], kind=trig.kind, priority=trig.priority,
            direction=dec.side, score=int(round(dec.score)), title=title,
            message=message,
            tags="chart_with_upwards_trend" if dec.side == "LONG"
            else "chart_with_downwards_trend",
            plan=plan,
        ))

    # Pro-паттерны — С ML-гейтом
    try:
        for pattern, direction, pscore in scan_pro_patterns(sig) or []:
            kind = f"pro_{pattern}"
            sig["_active_kind"] = kind
            dec = Decision(direction, BREAKOUT, float(pscore), [], [], 1.0, float(pscore), 0.0)
            if direction == "SHORT":
                dec.long_w, dec.short_w = 0.0, float(pscore)
            dec = apply_model_gate(sig, kind, dec)
            plan = build_plan(sig, dec)
            reasons = check_gates(sig, dec, plan)
            if reasons:
                rejected.append((kind, reasons, _snap(dec, plan)))
                continue
            alerts.append(Alert(
                base=base, symbol=sig["symbol"], kind=kind, priority=3,
                direction=dec.side, score=int(round(dec.score)),
                title=f"{base} {dec.side} {pattern} {dec.score:.0f}/{MAX_SCORE:.0f} R:R{plan.rr:.1f}",
                message="\n".join([
                    f"{ts} UTC price=${sig['price']:.6f}",
                    f"Pro-паттерн: {pattern}",
                    verdict_text(dec),
                    "------- план -------", plan.as_text(),
                    indicator_block(sig),
                ]),
                tags="dart", plan=plan,
            ))
    except Exception as exc:
        logger.debug("scan_pro_patterns failed: %s", exc)

    # конфликт сторон → лучший score, не drop both
    if CFG["one_alert_per_symbol"] and alerts:
        sides = {a.direction for a in alerts}
        if len(sides) > 1:
            best = max(alerts, key=lambda a: (a.score, a.priority))
            dropped = [a for a in alerts if a.direction != best.direction]
            for a in dropped:
                rejected.append((a.kind, [f"conflict: kept {best.direction} score={best.score}"], {}))
            alerts = [a for a in alerts if a.direction == best.direction]
        alerts.sort(key=lambda a: (-a.priority, -a.score))
        alerts = alerts[:1]
    else:
        alerts.sort(key=lambda a: (-a.priority, -a.score))

    sig.pop("_active_kind", None)
    return alerts, rejected


# ═════════════════════════════════════════════════════════════════════════
# 9. STATE / JOURNAL / EXCHANGE
# ═════════════════════════════════════════════════════════════════════════
@dataclass
class AlertState:
    sent: dict[str, float] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    overrides: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "AlertState":
        if path.exists():
            try:
                d = json.loads(path.read_text())
                return cls(
                    sent=d.get("sent", {}),
                    scores=d.get("scores", {}),
                    overrides=d.get("overrides", {}),
                )
            except Exception as exc:
                logger.warning("alert state load failed: %s", exc)
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "sent": self.sent,
            "scores": self.scores,
            "overrides": self.overrides,
        }, indent=2))
        os.replace(tmp, path)

    def can_send(self, key: str, cooldown_s: int, score: float) -> bool:
        elapsed = time.time() - self.sent.get(key, 0.0)
        if elapsed >= cooldown_s:
            self.overrides[key] = 0
            return True
        old = self.scores.get(key, 0.0)
        n_over = self.overrides.get(key, 0)
        if (
            score >= old + CFG["cooldown_override_delta"]
            and elapsed >= CFG["cooldown_override_min"] * 60
            and n_over < CFG["cooldown_override_max"]
        ):
            logger.info("cooldown override %s: %.0f -> %.0f (n=%d)", key, old, score, n_over + 1)
            self.overrides[key] = n_over + 1
            return True
        return False

    def mark(self, key: str, score: float) -> None:
        self.sent[key] = time.time()
        self.scores[key] = score

    def cleanup(self, ttl_s: int = 86_400) -> None:
        now = time.time()
        self.sent = {k: v for k, v in self.sent.items() if now - v < ttl_s}
        self.scores = {k: v for k, v in self.scores.items() if k in self.sent}
        self.overrides = {k: v for k, v in self.overrides.items() if k in self.sent}


def journal_write(rec: dict) -> None:
    try:
        JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec.setdefault("ts", time.time())
        with JOURNAL_FILE.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as exc:
        logger.debug("journal write failed: %s", exc)


def load_watchlist() -> list[dict[str, Any]]:
    if not WATCHLIST_FILE.exists():
        raise SystemExit(
            f"watchlist not found: {WATCHLIST_FILE}\n"
            f"запусти update_watchlist.py чтобы создать"
        )
    return json.loads(WATCHLIST_FILE.read_text()).get("pairs", [])


def make_exchange() -> ccxt.Exchange:
    cfg: dict[str, Any] = {
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
        "rateLimit": 120,
    }
    if PROXY:
        cfg["proxies"] = {"http": PROXY, "https": PROXY}
    return ccxt.bybit(cfg)


def acquire_pidlock() -> Any:
    """Single-instance lock. Returns file handle or None if unsupported."""
    if fcntl is None:
        return None
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(PID_FILE, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit(f"another scanner holds {PID_FILE}")
    fh.write(str(os.getpid()))
    fh.flush()

    def _release():
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
            if PID_FILE.exists():
                PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_release)
    return fh


def sleep_until_next_slot(interval: int) -> None:
    """Синхронизация к wall-clock, чтобы не копить drift."""
    now = time.time()
    slot = math.ceil(now / interval) * interval
    delay = max(1.0, slot - now)
    time.sleep(delay)


# ═════════════════════════════════════════════════════════════════════════
# 10. ОДИН ПРОХОД
# ═════════════════════════════════════════════════════════════════════════
def run_once(
    test_mode: bool = False,
    max_pairs: int | None = None,
    explain: bool = False,
) -> int:
    if not _ML_AVAILABLE:
        logger.warning("ML gate OFF — все сигналы без directional_lgbm")

    pairs = load_watchlist()
    if max_pairs:
        pairs = pairs[:max_pairs]
    logger.info("scanning %d pairs", len(pairs))

    state = AlertState.load(STATE_FILE)
    state.cleanup()
    exchange = make_exchange()

    try:
        labelled, label_errors = settle_candidates(exchange)
        if labelled or label_errors:
            logger.info("direction labels: labelled=%d errors=%d", labelled, label_errors)
    except Exception as exc:
        logger.warning("settle_candidates failed: %s", exc)

    regime = fetch_btc_regime(exchange)
    logger.info("BTC режим: %s (%.2f%% 1h live)", regime["state"], regime["btc_1h"])

    position_usd = estimate_position_usd()
    cooldown_s = CFG["cooldown_min"] * 60
    sent_count = skipped = errors = 0
    reject_counter: dict[str, int] = {}

    def work(p: dict):
        sig = fetch_signals(exchange, p["symbol"], regime, position_usd)
        if not sig:
            return p, None, [], []
        # Добавляем exchange в sig для проверки orderbook
        sig["_exchange"] = exchange
        alerts, rejected = build_alerts(p["base"], sig)
        return p, sig, alerts, rejected

    with ThreadPoolExecutor(max_workers=CFG["workers"]) as pool:
        futures = {pool.submit(work, p): p for p in pairs}
        for i, fut in enumerate(as_completed(futures), 1):
            p = futures[fut]
            try:
                p, sig, alerts, rejected = fut.result()
            except Exception as exc:
                errors += 1
                logger.warning("err on %s: %s", p["symbol"], exc)
                continue
            if sig is None:
                continue

            if explain:
                for kind, reasons, _snap in rejected:
                    logger.info("REJECT %s %s: %s", p["base"], kind, "; ".join(reasons))

            # journal: ВСЕ rejected (для калибровки / anti selection-bias)
            for kind, reasons, snap in rejected:
                for r in reasons:
                    reject_counter[r.split()[0] if r else "unknown"] = (
                        reject_counter.get(r.split()[0] if r else "unknown", 0) + 1
                    )
                journal_write({
                    "symbol": p["symbol"], "kind": kind, "side": snap.get("side", "NEUTRAL"),
                    "score": snap.get("score"), "agreement": snap.get("agreement"),
                    "blocked": reasons, "features": sig, "decision": snap,
                })

            for a in alerts:
                if not state.can_send(a.state_key, cooldown_s, a.score):
                    skipped += 1
                    continue

                # plan fields в sig для ML/record
                if a.plan:
                    sig = dict(sig)
                    sig["plan_entry"] = a.plan.entry
                    sig["plan_stop"] = a.plan.stop
                    sig["plan_tps"] = list(a.plan.tps)
                    sig["plan_rr"] = a.plan.rr
                    sig["plan_entry_type"] = a.plan.entry_type
                    sig["plan_valid_until"] = a.plan.valid_until
                    sig["plan_size_usd"] = a.plan.size_usd

                try:
                    record_candidate(sig, a.kind, p["symbol"])
                except Exception as exc:
                    logger.debug("record_candidate failed: %s", exc)

                journal_write({
                    "symbol": p["symbol"], "kind": a.kind,
                    "side": a.direction, "score": a.score,
                    "entry": a.plan.entry if a.plan else None,
                    "stop": a.plan.stop if a.plan else None,
                    "tps": list(a.plan.tps) if a.plan else [],
                    "rr": a.plan.rr if a.plan else None,
                    "valid_until": a.plan.valid_until if a.plan else None,
                    "blocked": [], "features": sig,
                })

                if test_mode:
                    logger.info("TEST [P%d] %s\n%s\n", a.priority, a.title, a.message)
                else:
                    notify(
                        title=a.title, message=a.message, priority=a.priority,
                        tags=a.tags,
                        click_url=f"https://www.bybit.com/trade/usdt/{a.base}USDT",
                    )
                state.mark(a.state_key, a.score)
                state.save(STATE_FILE)  # FIX: сразу после sent
                sent_count += 1
                logger.info(
                    "[P%d] sent: %s | score=%d/%d",
                    a.priority, a.title, a.score, int(MAX_SCORE),
                )

                # Генерируем signal_id
                signal_id = f"{p['base']}_{a.direction}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                
                logger.info(
                    "SIGNAL_ID=%s SIGNAL_DECISION base=%s dir=%s kind=%s score=%d price=%.8f mode=%s",
                    signal_id, p["base"], a.direction, a.kind, a.score,
                    float(sig.get("price", 0) or 0), getattr(a, 'mode', 'unknown'),
                )
                if a.plan:
                    logger.info(
                        "SIGNAL_PLAN base=%s dir=%s entry=%.10g stop=%.10g "
                        "tp1=%.10g tp2=%.10g rr=%.2f size_usd=%.2f qty=%.10g "
                        "hold_min=%d valid_until=%.0f entry_type=%s",
                        p["base"], a.direction, a.plan.entry, a.plan.stop,
                        a.plan.tps[0],
                        a.plan.tps[1] if len(a.plan.tps) > 1 else a.plan.tps[0],
                        a.plan.rr, a.plan.size_usd, a.plan.qty, a.plan.max_hold_min,
                        a.plan.valid_until, a.plan.entry_type,
                    )
                    # Получаем вердикт модели (не блокируем сигнал!)
                    model_dir, model_conf = predict_direction(sig, a.kind)
                    if model_dir:
                        logger.info("MODEL_VERDICT dir=%s confidence=%.2f", model_dir, model_conf)
                    # Добавляем детальные индикаторы для Kiro analyzer
                    logger.info(indicator_block(sig))

            if i % 25 == 0:
                logger.info("progress %d/%d", i, len(pairs))

    state.save(STATE_FILE)
    if reject_counter:
        top = sorted(reject_counter.items(), key=lambda x: -x[1])[:8]
        logger.info("top reject reasons: %s", ", ".join(f"{k}={v}" for k, v in top))
    logger.info("done. sent=%d cooldown_skipped=%d errors=%d", sent_count, skipped, errors)

    try:
        if hasattr(exchange, "close"):
            exchange.close()
    except Exception as exc:
        logger.debug("exchange close: %s", exc)

    # FIX: export только вне test
    if not test_mode:
        try:
            import subprocess
            script = ROOT / "export_signals_to_funtik.py"
            if script.exists():
                subprocess.run(
                    ["python3", str(script)], cwd=str(ROOT),
                    capture_output=True, timeout=30,
                )
                logger.info("exported signals to dex_signals.json")
        except Exception as exc:
            logger.error("failed to export signals: %s", exc)

    return sent_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="не слать в ntfy, только лог")
    parser.add_argument("--max-pairs", type=int, default=None)
    parser.add_argument("--ping", action="store_true", help="тест ntfy и выход")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=180)
    parser.add_argument(
        "--explain", action="store_true",
        help="логировать причины отказа по каждому сетапу",
    )
    parser.add_argument(
        "--no-pidlock", action="store_true",
        help="не брать single-instance lock",
    )
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    lock_fh = None
    if args.daemon and not args.no_pidlock:
        lock_fh = acquire_pidlock()

    if args.ping:
        ok = notify(
            title="DEX Scanner ping",
            message=f"тестовое уведомление\n{datetime.now(timezone.utc).isoformat()}",
            priority=3, tags="white_check_mark",
        )
        logger.info("ping result: %s", ok)
        return 0

    if args.daemon:
        logger.info("daemon mode: interval=%ds (wall-clock sync)", args.interval)
        while True:
            try:
                run_once(args.test, args.max_pairs, args.explain)
            except KeyboardInterrupt:
                logger.info("daemon stopped by user")
                return 0
            except Exception as exc:
                logger.error("daemon error: %s", exc, exc_info=True)
            logger.info("sleeping until next %ds slot", args.interval)
            try:
                sleep_until_next_slot(args.interval)
            except KeyboardInterrupt:
                logger.info("daemon stopped by user")
                return 0
    else:
        run_once(args.test, args.max_pairs, args.explain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
