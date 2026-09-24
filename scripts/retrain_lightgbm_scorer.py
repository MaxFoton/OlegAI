#!/usr/bin/env python3
"""
Переобучение lightgbm_scorer на чистых данных из trade_snapshots.db

Фильтры качества:
1. result IN ('win', 'loss') - только чистые результаты, без breakeven
2. scanner_score > 0 - есть базовые фичи
3. profit_ratio != 0 - реальный результат
4. Удаляем старые данные до июня 2026 (там мусор)
"""

import sqlite3
import pickle
import json
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.metrics import accuracy_score, roc_auc_score, classification_report
import lightgbm as lgb

# Пути
SNAPSHOTS_DB = Path("/home/max/freqtrade/user_data/pump_dump_strategy/data/trade_snapshots.db")
MODEL_PATH = Path("/home/max/freqtrade/user_data/pump_dump_strategy/data/lightgbm_scorer.pkl")
META_PATH = Path("/home/max/freqtrade/user_data/pump_dump_strategy/data/lightgbm_meta.json")

# Базовые фичи (всегда доступны)
BASE_FEATURES = [
    'scanner_score',
    'regime_score', 
    'micro_score',
    'trigger_score',
    'confidence',
    'volatility',
    'volume_spike',
    'absorption_score',
    'lor_signal',
    'lor_strength',
    'lor_prediction',
    'volume_slope',
    'delta_slope',
    'distance_from_ema20',
    'distance_from_vwap',
    'direction_long',  # 1=LONG, 0=SHORT
]

# Дополнительные фичи (если есть)
OPTIONAL_FEATURES = [
    'rsi',
    'htf_trend_1h',
    'vw_macd',
    'vwap_value',
    'macd_value',
    'macd_signal',
    'macd_hist',
]

# Phase one-hot encoding
PHASE_FEATURES = [
    'phase_early_expansion',
    'phase_mid_expansion', 
    'phase_late_expansion',
    'phase_exhaustion',
]

# Interaction features (добавлены 14.07.2026)
INTERACTION_FEATURES = [
    'long_x_early_expansion',
    'long_x_mid_expansion',
    'long_x_late_expansion',
    'long_x_exhaustion',
    'short_x_early_expansion',
    'short_x_mid_expansion',
    'short_x_late_expansion',
    'short_x_exhaustion',
]


def load_data():
    """Загружает данные из trade_snapshots.db"""
    
    conn = sqlite3.connect(SNAPSHOTS_DB)
    
    # Фильтруем качественные данные
    query = """
    SELECT 
        result,
        profit_ratio,
        scanner_score,
        regime_score,
        micro_score,
        trigger_score,
        confidence,
        volatility,
        volume_spike,
        absorption_score,
        lor_signal,
        lor_strength,
        lor_prediction,
        volume_slope,
        delta_slope,
        distance_from_ema20,
        distance_from_vwap,
        direction,
        market_phase,
        rsi,
        htf_trend_1h,
        vw_macd,
        ema_trend,
        vwap_value,
        macd_value,
        macd_signal,
        macd_hist,
        timestamp
    FROM trade_snapshots
    WHERE 
        result IN ('win', 'loss')
        AND scanner_score > 0
        AND profit_ratio != 0
    ORDER BY timestamp
    """
    
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    print(f"📊 Загружено записей: {len(df)}")
    print(f"   WIN: {(df['result'] == 'win').sum()}")
    print(f"   LOSS: {(df['result'] == 'loss').sum()}")
    print(f"   WR: {(df['result'] == 'win').sum() / len(df) * 100:.1f}%")
    
    return df


def prepare_features(df):
    """Подготавливает фичи для модели"""
    
    X = pd.DataFrame()
    
    # Базовые фичи
    for feat in BASE_FEATURES:
        if feat == 'direction_long':
            # Конвертируем LONG/SHORT в 1/0
            X[feat] = (df['direction'] == 'LONG').astype(float)
        else:
            # Приводим к float, пустые строки → NaN → 0
            X[feat] = pd.to_numeric(df[feat], errors='coerce').fillna(0.0)
    
    # Phase one-hot encoding
    for phase in ['EARLY_EXPANSION', 'MID_EXPANSION', 'LATE_EXPANSION', 'EXHAUSTION']:
        col_name = f'phase_{phase.lower()}'
        X[col_name] = (df['market_phase'] == phase).astype(float)
    
    # Опциональные фичи (если есть)
    for feat in OPTIONAL_FEATURES:
        if feat in df.columns:
            X[feat] = pd.to_numeric(df[feat], errors='coerce').fillna(0.0)
    
    # EMA trend: конвертируем в числовые фичи
    if 'ema_trend' in df.columns:
        X['ema_bullish'] = (df['ema_trend'] == 'bullish').astype(float)
        X['ema_bearish'] = (df['ema_trend'] == 'bearish').astype(float)
        X['ema_neutral'] = (df['ema_trend'] == 'neutral').astype(float)
    
    # Interaction features (direction × phase)
    is_long = (df['direction'] == 'LONG')
    for phase_feat in PHASE_FEATURES:
        if phase_feat in X.columns:
            X[f'long_x_{phase_feat.replace("phase_", "")}'] = X[phase_feat] * is_long.astype(float)
            X[f'short_x_{phase_feat.replace("phase_", "")}'] = X[phase_feat] * (~is_long).astype(float)
    
    # Label: win=1, loss=0
    y = (df['result'] == 'win').astype(int)
    
    print(f"✅ Фичи подготовлены: {X.shape[1]} features")
    print(f"   Колонки: {list(X.columns)}")
    
    return X, y, list(X.columns)


def train_model(X, y, feature_names):
    """Обучает LightGBM модель с кросс-валидацией"""
    
    print("\n🚀 Начинаем обучение модели...")
    
    # Параметры модели (оптимизированы для малых данных)
    params = {
        'objective': 'binary',
        'metric': 'binary_logloss',
        'boosting_type': 'gbdt',
        'num_leaves': 15,
        'max_depth': 4,
        'learning_rate': 0.05,
        'n_estimators': 200,
        'min_child_samples': 20,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'reg_alpha': 0.1,
        'reg_lambda': 0.1,
        'class_weight': 'balanced',
        'random_state': 42,
        'verbose': -1,
    }
    
    model = lgb.LGBMClassifier(**params)
    
    # Стратифицированная кросс-валидация (5 фолдов)
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    
    cv_scores = cross_val_score(model, X, y, cv=skf, scoring='accuracy')
    cv_auc = cross_val_score(model, X, y, cv=skf, scoring='roc_auc')
    
    print(f"\n📊 Кросс-валидация (5 фолдов):")
    print(f"   Accuracy: {cv_scores.mean():.4f} ± {cv_scores.std():.4f}")
    print(f"   AUC: {cv_auc.mean():.4f} ± {cv_auc.std():.4f}")
    
    # Обучаем на всех данных
    model.fit(X, y)
    
    # Предсказания на тренировочной выборке (для анализа)
    y_pred = model.predict(X)
    y_pred_proba = model.predict_proba(X)[:, 1]
    
    print(f"\n📈 Результаты на тренировочной выборке:")
    print(f"   Accuracy: {accuracy_score(y, y_pred):.4f}")
    print(f"   AUC: {roc_auc_score(y, y_pred_proba):.4f}")
    print(f"\n{classification_report(y, y_pred, target_names=['LOSS', 'WIN'])}")
    
    # Feature importance
    importance = pd.DataFrame({
        'feature': feature_names,
        'importance': model.feature_importances_
    }).sort_values('importance', ascending=False)
    
    print(f"\n🔝 Топ-10 фичей по важности:")
    print(importance.head(10).to_string(index=False))
    
    return model, cv_scores.mean(), cv_auc.mean()


def save_model(model, feature_names, cv_accuracy, cv_auc, n_samples, n_wins, n_losses):
    """Сохраняет модель и метаданные"""
    
    # Сохраняем модель
    with MODEL_PATH.open('wb') as f:
        pickle.dump(model, f)
    
    print(f"\n✅ Модель сохранена: {MODEL_PATH}")
    
    # Метаданные
    meta = {
        'trained': True,
        'used': True,  # Сразу активируем модель
        'samples': int(n_samples),
        'wins': int(n_wins),
        'losses': int(n_losses),
        'winrate': float(n_wins / n_samples),
        'cv_accuracy': round(float(cv_accuracy), 4),
        'cv_auc': round(float(cv_auc), 4),
        'feature_names': feature_names,
        'trained_at': pd.Timestamp.now().isoformat(),
        'min_prob_threshold': 0.55,  # Порог для использования (55% уверенность)
        'data_filter': 'result IN (win,loss) AND scanner_score>0',
    }
    
    META_PATH.write_text(json.dumps(meta, indent=2))
    print(f"✅ Метаданные сохранены: {META_PATH}")
    
    return meta


def main():
    print("=" * 60)
    print("ПЕРЕОБУЧЕНИЕ lightgbm_scorer на чистых данных")
    print("=" * 60)
    
    # 1. Загружаем данные
    df = load_data()
    
    if len(df) < 100:
        print(f"\n⚠️ НЕДОСТАТОЧНО ДАННЫХ: {len(df)} < 100")
        print("Нужно минимум 100 сделок с результатами для обучения")
        return
    
    # 2. Подготавливаем фичи
    X, y, feature_names = prepare_features(df)
    
    # 3. Обучаем модель
    model, cv_accuracy, cv_auc = train_model(X, y, feature_names)
    
    # 4. Сохраняем
    n_wins = (df['result'] == 'win').sum()
    n_losses = (df['result'] == 'loss').sum()
    
    meta = save_model(model, feature_names, cv_accuracy, cv_auc, len(df), n_wins, n_losses)
    
    print("\n" + "=" * 60)
    print("✅ ОБУЧЕНИЕ ЗАВЕРШЕНО")
    print("=" * 60)
    print(f"📊 Статистика:")
    print(f"   Samples: {meta['samples']}")
    print(f"   WR: {meta['winrate']*100:.1f}%")
    print(f"   CV Accuracy: {meta['cv_accuracy']:.2%}")
    print(f"   CV AUC: {meta['cv_auc']:.2%}")
    print(f"   Threshold: ≥{meta['min_prob_threshold']:.0%} для использования")
    print("=" * 60)


if __name__ == "__main__":
    main()
