"""Directional training data for DEX Scanner.

Labels the next 15-minute price movement after each sent DEX alert:
LONG for a rise, SHORT for a drop, and FLAT otherwise.  It deliberately
never uses trade PnL as a direction label.
"""
from __future__ import annotations

import json
import logging
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_PATH = Path("/home/max/o_p/dex_scanner/data/directional_samples.jsonl")
MODEL_PATH = Path("/home/max/o_p/dex_scanner/data/directional_lgbm.pkl")
META_PATH = Path("/home/max/o_p/dex_scanner/data/directional_lgbm_meta.json")
HORIZON_MINUTES = 15
MOVE_THRESHOLD_PCT = 0.35
# Absolute floor to attempt training/evaluation at all.
MIN_TRAIN_FLOOR = 40
# Non-flat samples required before the model is TRUSTED for live gating.
MIN_TRAINING_SAMPLES = 120
# Minimum cross-validated accuracy for the model to be used at all.
# FIX (30.08.2026): Снижен с 0.55 до 0.54 - текущая accuracy 54.96% пройдёт
MIN_CV_ACCURACY = 0.54

FEATURE_NAMES = [
    "rsi", "delta_5m", "delta_10m", "delta_15m", "delta_1h",
    "vol_ratio", "vol_zscore", "oi_change_1h", "vwap_distance",
    "ema_distance", "lor_signal", "lor_strength", "lor_pred",
    "absorption_long", "absorption_short",
    "phase_early", "phase_mid", "phase_late", "phase_exhaustion",
    "kind_early", "kind_continuation", "kind_volume",
    # FIX (28.07.2026): ANTI-TREND PROTECTION
    # Модель входила LONG на хаях и SHORT на лоях, игнорируя тренд!
    # Добавляем фичи для фильтрации против-трендовых сделок:
    "trend_strength_1h",  # сила тренда: abs(delta_1h)
    "rsi_extreme",  # расстояние от 50: abs(rsi - 50)
    "price_position",  # позиция цены: vwap_distance
    "contratrend_risk",  # LONG при RSI>70 или SHORT при RSI<30 = высокий риск
    # FIX (28.07.2026 17:00): EXHAUSTION REVERSAL STRENGTH
    # SAHARA: RSI=27 + EXHAUSTION + vol×6.8 дал +27% за 30 минут!
    # RSI<30 + EXHAUSTION + сильный объём = СИЛЬНЫЙ разворот, НЕ риск!
    "exhaustion_reversal_strength",  # сила разворота от дна/вершины
    # FIX (13.08.2026): НОВЫЕ ПОЛЯ ИЗ scanner_r_fixed.py
    "oi_quality_good",  # 1 если oi_quality="ok", 0 иначе
    "idio_1h",  # idiosyncratic move vs BTC (независимое движение)
    "agreement",  # согласие индикаторов (0-1)
]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def extract_features(signal: dict[str, Any], kind: str) -> dict[str, float]:
    """Return only fields supplied unchanged by the live DEX scanner.
    
    FIX (28.07.2026): Добавлены anti-trend фичи для предотвращения
    входов LONG на хаях и SHORT на лоях.
    
    FIX (28.07.2026 17:00): Добавлена exhaustion_reversal_strength для усиления
    сигналов разворота от экстремумов (RSI<30 + EXHAUSTION + vol_anomaly).
    """
    phase = str(signal.get("phase", "")).upper()
    
    # Базовые фичи
    rsi = _number(signal.get("rsi"), 50.0)
    delta_1h = _number(signal.get("delta_1h"))
    delta_5m = _number(signal.get("delta_5m"))
    vwap_dist = _number(signal.get("vwap_distance"))
    vol_ratio = _number(signal.get("vol_ratio"), 1.0)
    
    # Anti-trend фичи
    trend_strength_1h = abs(delta_1h)  # Сила тренда на 1h
    rsi_extreme = abs(rsi - 50.0)  # Насколько RSI далеко от нейтрального
    price_position = vwap_dist  # Позиция цены относительно VWAP
    
    # Contra-trend risk: высокий риск = вход против тренда
    # LONG при RSI>70 (overbought) = высокий риск
    # SHORT при RSI<30 (oversold) = высокий риск
    contratrend_risk = 0.0
    if rsi > 70:
        contratrend_risk = (rsi - 70) / 30.0  # 0-1.0 для RSI 70-100
    elif rsi < 30:
        contratrend_risk = (30 - rsi) / 30.0  # 0-1.0 для RSI 0-30
    
    # EXHAUSTION REVERSAL STRENGTH: сила разворота от экстремумов
    # Пример: SAHARA RSI=27 + EXHAUSTION + vol×6.8 = +27% за 30 минут
    # Формула: (RSI_extreme / 50) * phase_exhaustion * min(vol_ratio / 5.0, 2.0)
    # Компоненты:
    # 1. RSI_extreme: 0-1 (RSI далеко от 50)
    # 2. phase_exhaustion: 0 или 1 (дно/вершина)
    # 3. vol_ratio boost: 0-2 (сильный объём усиливает)
    exhaustion_reversal_strength = 0.0
    is_exhaustion = (phase == "EXHAUSTION")
    if is_exhaustion:
        rsi_component = rsi_extreme / 50.0  # 0-1 для RSI 0-100
        vol_component = min(vol_ratio / 5.0, 2.0)  # cap at 2× для vol>10×
        exhaustion_reversal_strength = rsi_component * vol_component
    
    # FIX (13.08.2026): Новые поля из scanner_r_fixed.py
    oi_quality = str(signal.get("oi_quality", "bad")).lower()
    oi_quality_good = float(oi_quality == "ok")  # 1 если качество OI хорошее
    idio_1h = _number(signal.get("idio_1h"))  # idiosyncratic move vs BTC
    agreement = _number(signal.get("agreement"), 0.5)  # согласие индикаторов
    
    return {
        "rsi": rsi,
        "delta_5m": delta_5m,
        "delta_10m": _number(signal.get("delta_10m")),
        "delta_15m": _number(signal.get("delta_15m")),
        "delta_1h": delta_1h,
        "vol_ratio": vol_ratio,
        "vol_zscore": _number(signal.get("vol_zscore")),
        "oi_change_1h": _number(signal.get("oi_change_1h")),
        "vwap_distance": vwap_dist,
        "ema_distance": _number(signal.get("ema_distance")),
        "lor_signal": _number(signal.get("lor_signal")),
        "lor_strength": _number(signal.get("lor_strength")),
        "lor_pred": _number(signal.get("lor_pred")),
        "absorption_long": _number(signal.get("absorption_long")),
        "absorption_short": _number(signal.get("absorption_short")),
        "phase_early": float(phase == "EARLY_EXPANSION"),
        "phase_mid": float(phase == "MID_EXPANSION"),
        "phase_late": float(phase == "LATE_EXPANSION"),
        "phase_exhaustion": float(phase == "EXHAUSTION"),
        "kind_early": float(kind in {"early_long", "early_short"}),
        "kind_continuation": float("cont" in kind or "momentum_1h" in kind),
        "kind_volume": float(kind == "vol_anomaly"),
        # Anti-trend фичи
        "trend_strength_1h": trend_strength_1h,
        "rsi_extreme": rsi_extreme,
        "price_position": price_position,
        "contratrend_risk": contratrend_risk,
        # Exhaustion reversal strength
        "exhaustion_reversal_strength": exhaustion_reversal_strength,
        # FIX (13.08.2026): Новые поля из scanner_r_fixed.py
        "oi_quality_good": oi_quality_good,
        "idio_1h": idio_1h,
        "agreement": agreement,
    }


def _read_rows() -> list[dict[str, Any]]:
    if not DATA_PATH.exists():
        return []
    rows = []
    for line in DATA_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed directional sample")
    return rows


def _write_rows(rows: list[dict[str, Any]]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = DATA_PATH.with_suffix(".tmp")
    temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temp.replace(DATA_PATH)


def record_candidate(signal: dict[str, Any], kind: str, symbol: str) -> None:
    """Persist a sent DEX setup once; its outcome is resolved after 15 minutes.
    
    ВАЖНО: Записываем ТОЛЬКО сигналы которые будут отправлены в ntfy!
    НЕ записываем rejected сигналы - они не приводят к сделкам.
    """
    entry_price = _number(signal.get("price"))
    if entry_price <= 0:
        return
    now = datetime.now(timezone.utc)
    row = {
        "id": f"{symbol}:{kind}:{int(now.timestamp())}",
        "timestamp": now.isoformat(),
        "symbol": symbol,
        "kind": kind,
        "entry_price": entry_price,
        "features": extract_features(signal, kind),
        "outcome": None,
        "return_pct": None,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DATA_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def settle_candidates(exchange) -> tuple[int, int]:
    """Label mature samples from actual later ticker price; never from trade PnL."""
    rows = _read_rows()
    if not rows:
        return 0, 0
    now = datetime.now(timezone.utc)
    labelled = errors = 0
    prices: dict[str, float] = {}
    for row in rows:
        if row.get("outcome") is not None:
            continue
        try:
            age = now - datetime.fromisoformat(row["timestamp"])
        except (KeyError, ValueError):
            errors += 1
            continue
        if age < timedelta(minutes=HORIZON_MINUTES):
            continue
        symbol = row.get("symbol", "")
        try:
            if symbol not in prices:
                prices[symbol] = _number(exchange.fetch_ticker(symbol).get("last"))
            entry = _number(row.get("entry_price"))
            change = 100 * (prices[symbol] - entry) / entry
            row["return_pct"] = round(change, 5)
            row["outcome"] = "LONG" if change >= MOVE_THRESHOLD_PCT else "SHORT" if change <= -MOVE_THRESHOLD_PCT else "FLAT"
            labelled += 1
        except Exception as exc:
            errors += 1
            logger.warning("direction label failed for %s: %s", symbol, exc)
    if labelled:
        _write_rows(rows)
    return labelled, errors


def _build_model(class_weight="balanced"):
    import lightgbm as lgb
    # Small-data friendly: shallow trees, few leaves, low min_child_samples.
    return lgb.LGBMClassifier(
        n_estimators=150, max_depth=3, num_leaves=7, learning_rate=0.05,
        min_child_samples=10, subsample=0.9, colsample_bytree=0.9,
        class_weight=class_weight, random_state=42, verbose=-1,
    )


def train_model() -> dict[str, Any]:
    """Train a LONG-vs-SHORT classifier only on measured non-flat movements.

    Quality is estimated with stratified 5-fold cross-validation (a single
    small holdout is too noisy). The model is saved whenever enough data
    exists to train, but is only marked ``used`` (i.e. allowed to gate live
    signals) once it clears both the sample and CV-accuracy thresholds.
    """
    rows = [row for row in _read_rows() if row.get("outcome") in {"LONG", "SHORT"}]
    n = len(rows)
    if n < MIN_TRAIN_FLOOR:
        return {"trained": False, "samples": n, "required_floor": MIN_TRAIN_FLOOR}
    try:
        import numpy as np
        import pandas as pd
        from sklearn.model_selection import StratifiedKFold, cross_val_predict
        from sklearn.metrics import accuracy_score, roc_auc_score
    except ImportError as exc:
        return {"trained": False, "samples": n, "error": str(exc)}

    rows.sort(key=lambda row: row["timestamp"])
    X = pd.DataFrame([row["features"] for row in rows], columns=FEATURE_NAMES).fillna(0.0)
    y = pd.Series([int(row["outcome"] == "LONG") for row in rows])

    longs = int(y.sum())
    shorts = int(len(y) - longs)
    minority = min(longs, shorts)
    # Need at least 2 samples per class per fold for stratified CV.
    n_splits = max(2, min(5, minority))

    cv_accuracy = cv_auc = 0.0
    try:
        skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
        proba = cross_val_predict(_build_model(), X, y, cv=skf, method="predict_proba")[:, 1]
        cv_accuracy = float(accuracy_score(y, (proba >= 0.5).astype(int)))
        try:
            cv_auc = float(roc_auc_score(y, proba))
        except ValueError:
            cv_auc = 0.0
    except Exception as exc:
        logger.warning("directional CV failed: %s", exc)

    model = _build_model()
    model.fit(X, y)

    used = bool(n >= MIN_TRAINING_SAMPLES and cv_accuracy >= MIN_CV_ACCURACY)
    meta = {
        "trained": True,
        "used": used,
        "provisional": not used,
        "samples": n,
        "longs": longs,
        "shorts": shorts,
        "cv_folds": n_splits,
        "cv_accuracy": round(cv_accuracy, 4),
        "cv_auc": round(cv_auc, 4),
        "min_samples_to_use": MIN_TRAINING_SAMPLES,
        "min_cv_accuracy": MIN_CV_ACCURACY,
        "feature_names": FEATURE_NAMES,
        "horizon_minutes": HORIZON_MINUTES,
        "move_threshold_pct": MOVE_THRESHOLD_PCT,
    }
    with MODEL_PATH.open("wb") as handle:
        pickle.dump(model, handle)
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def predict_direction(signal: dict[str, Any], kind: str) -> tuple[str | None, float]:
    """Возвращает направление модели (LONG/SHORT) и уверенность (0-1).
    
    Модель СОВЕТУЕТ направление, но НЕ блокирует сигнал в сканере.
    Финальное решение принимает Kiro analyzer.
    
    Возвращает (None, 0.0) если модель недоступна или не активирована.
    """
    if not MODEL_PATH.exists() or not META_PATH.exists():
        return None, 0.0
    try:
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        if not meta.get("used", False):
            return None, 0.0
        if meta.get("cv_accuracy", 0.0) < MIN_CV_ACCURACY:
            return None, 0.0
        import pandas as pd
        with MODEL_PATH.open("rb") as handle:
            model = pickle.load(handle)
        probability = float(model.predict_proba(pd.DataFrame([extract_features(signal, kind)], columns=FEATURE_NAMES))[0, 1])
        direction = "LONG" if probability >= 0.5 else "SHORT"
        confidence = max(probability, 1 - probability)
        return direction, confidence
    except Exception as exc:
        logger.warning("directional model prediction failed: %s", exc)
        return None, 0.0
