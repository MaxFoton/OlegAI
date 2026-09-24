#!/usr/bin/env python3
"""
Lorentzian Classification - k-NN с расстоянием Лоренца

Архитектура:
- OFFLINE: обучаемся на исторических данных, сохраняем memory
- ONLINE: только inference - сравниваем текущее состояние с historical states

Pipeline:
  Scanner → Lorentzian (regime filter) → ITG (timing) → Execution

Features:
  f1: RSI(14)
  f2: WT(10, 11)
  f3: CCI(20)
  f4: ADX(20)
  f5: RSI(9)
"""

import json
import logging
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MEMORY_PATH = Path(__file__).parent / "data" / "lorentzian_memory.json"


class LorentzianClassifier:
    """
    k-NN классификатор с расстоянием Лоренца
    
    Offline-trained memory classifier:
    - Загружает исторические features + labels
    - На каждой свече: сравнивает current features с historical
    - Weighted neighbor voting → prediction
    """
    
    def __init__(
        self,
        neighbors_count: int = 8,
        feature_count: int = 5,
    ):
        self.neighbors_count = neighbors_count
        self.feature_count = feature_count
        
        # Historical memory (загружается из файла)
        self._memory_features: List[Dict[str, float]] = []
        self._memory_labels: List[int] = []
        self._loaded = False
        
    def load_memory(self, path: Optional[Path] = None) -> bool:
        """Загружает historical memory из файла"""
        path = path or MEMORY_PATH
        
        if not path.exists():
            logger.warning(f"Lorentzian memory not found: {path}")
            return False
        
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            
            self._memory_features = data.get('features', [])
            self._memory_labels = data.get('labels', [])
            self._loaded = len(self._memory_features) > 0
            
            logger.info(f"Loaded Lorentzian memory: {len(self._memory_features)} samples")
            return self._loaded
            
        except Exception as e:
            logger.error(f"Failed to load Lorentzian memory: {e}")
            return False
    
    def is_loaded(self) -> bool:
        return self._loaded
    
    def calculate_features(self, df: pd.DataFrame) -> Dict[str, float]:
        """
        Рассчитывает 5 фичей для текущего состояния
        
        Returns:
            dict с f1-f5 значениями для последней свечи
        """
        try:
            import talib.abstract as ta
            
            close = df['close'].values
            high = df['high'].values
            low = df['low'].values
            
            # RSI(14)
            rsi14 = ta.RSI(df, timeperiod=14).values[-1]
            
            # WT(10, 11) - Wave Trend
            wt = self._calculate_wt(high, low, close, 10, 11)[-1]
            
            # CCI(20)
            cci = ta.CCI(df, timeperiod=20).values[-1]
            
            # ADX(20)
            adx = ta.ADX(df, timeperiod=20).values[-1]
            
            # RSI(9)
            rsi9 = ta.RSI(df, timeperiod=9).values[-1]
            
            return {
                'f1': float(rsi14) if not np.isnan(rsi14) else 50.0,
                'f2': float(wt) if not np.isnan(wt) else 0.0,
                'f3': float(cci) if not np.isnan(cci) else 0.0,
                'f4': float(adx) if not np.isnan(adx) else 25.0,
                'f5': float(rsi9) if not np.isnan(rsi9) else 50.0,
            }
            
        except Exception as e:
            logger.warning(f"Feature calculation error: {e}")
            return {'f1': 50.0, 'f2': 0.0, 'f3': 0.0, 'f4': 25.0, 'f5': 50.0}
    
    def _calculate_wt(self, high: np.ndarray, low: np.ndarray, close: np.ndarray,
                      period: int = 10, smooth: int = 11) -> np.ndarray:
        """Расчёт Wave Trend"""
        n = len(close)
        wt = np.zeros(n)
        
        if n < period + smooth:
            return wt
        
        hlc3 = (high + low + close) / 3
        
        # ESA - EMA of HLC3
        esa = np.zeros(n)
        multiplier = 2.0 / (period + 1)
        esa[0] = hlc3[0]
        for i in range(1, n):
            esa[i] = (hlc3[i] - esa[i-1]) * multiplier + esa[i-1]
        
        # D - EMA of |HLC3 - ESA|
        d = np.zeros(n)
        abs_diff = np.abs(hlc3 - esa)
        d[0] = abs_diff[0]
        for i in range(1, n):
            d[i] = (abs_diff[i] - d[i-1]) * multiplier + d[i-1]
        
        # CI
        ci = np.zeros(n)
        for i in range(n):
            if d[i] > 0:
                ci[i] = (hlc3[i] - esa[i]) / (0.015 * d[i])
        
        # WT - EMA of CI
        wt_multiplier = 2.0 / (smooth + 1)
        wt[0] = ci[0]
        for i in range(1, n):
            wt[i] = (ci[i] - wt[i-1]) * wt_multiplier + wt[i-1]
        
        return wt
    
    def lorentzian_distance(self, current: Dict[str, float], historical: Dict[str, float]) -> float:
        """
        Расчёт расстояния Лоренца между двумя состояниями
        
        d = Σ log(1 + |fi_current - fi_hist|)
        """
        distance = 0.0
        
        for key in ['f1', 'f2', 'f3', 'f4', 'f5'][:self.feature_count]:
            curr_val = current.get(key, 0.0)
            hist_val = historical.get(key, 0.0)
            distance += math.log(1 + abs(curr_val - hist_val))
        
        return distance
    
    def predict(self, df: pd.DataFrame) -> Dict:
        """
        k-NN prediction - inference only
        
        Returns:
            {
                'signal': int (1=long, -1=short, 0=neutral),
                'strength': float (0-1),
                'neighbors_used': int,
            }
        """
        if not self._loaded:
            return {'signal': 0, 'strength': 0.0, 'neighbors_used': 0}
        
        # Calculate current features
        current_features = self.calculate_features(df)
        
        # Find k nearest neighbors
        distances = []
        for i, hist_features in enumerate(self._memory_features):
            d = self.lorentzian_distance(current_features, hist_features)
            label = self._memory_labels[i]
            distances.append((d, label))
        
        # Sort by distance, take k nearest
        distances.sort(key=lambda x: x[0])
        neighbors = distances[:self.neighbors_count]
        
        if not neighbors:
            return {'signal': 0, 'strength': 0.0, 'neighbors_used': 0}
        
        # Weighted voting (closer neighbors have more weight)
        votes = {1: 0.0, -1: 0.0, 0: 0.0}
        total_weight = 0.0
        
        for d, label in neighbors:
            # Weight = 1 / (1 + distance)
            weight = 1.0 / (1.0 + d)
            votes[label] = votes.get(label, 0.0) + weight
            total_weight += weight
        
        # Normalize
        if total_weight > 0:
            for k in votes:
                votes[k] /= total_weight
        
        # Determine signal
        long_strength = votes.get(1, 0.0)
        short_strength = votes.get(-1, 0.0)
        
        signal = 0
        strength = 0.0
        
        if long_strength > short_strength and long_strength > 0.5:
            signal = 1
            strength = long_strength
        elif short_strength > long_strength and short_strength > 0.5:
            signal = -1
            strength = short_strength
        
        # Calculate prediction = simple count of neighbor votes (-8 to +8)
        # Simple voting: each neighbor = 1 vote, NOT weighted
        prediction = 0
        for d, label in neighbors:
            prediction += label
        
        # prediction is now in range -8 to +8
        # 8 = all neighbors say LONG, -8 = all neighbors say SHORT
        
        return {
            'signal': signal,
            'strength': strength,
            'neighbors_used': len(neighbors),
            'prediction': prediction,
        }


def train_lorentzian_memory(
    df: pd.DataFrame,
    lookahead: int = 4,
    output_path: Optional[Path] = None
) -> bool:
    """
    OFFLINE TRAINING
    
    Создаёт historical memory для Lorentzian classifier
    
    Args:
        df: DataFrame с OHLCV данными
        lookahead: на сколько баров вперёд смотреть для label
        output_path: куда сохранить memory
    
    Returns:
        True если успешно
    """
    output_path = output_path or MEMORY_PATH
    
    try:
        import talib.abstract as ta
        
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        n = len(close)
        
        # Calculate features for all bars
        rsi14 = ta.RSI(df, timeperiod=14).values
        wt = _calc_wt(high, low, close, 10, 11)
        cci = ta.CCI(df, timeperiod=20).values
        adx = ta.ADX(df, timeperiod=20).values
        rsi9 = ta.RSI(df, timeperiod=9).values
        
        features = []
        labels = []
        
        for i in range(n - lookahead):
            # Features at bar i
            feat = {
                'f1': float(rsi14[i]) if not np.isnan(rsi14[i]) else 50.0,
                'f2': float(wt[i]) if not np.isnan(wt[i]) else 0.0,
                'f3': float(cci[i]) if not np.isnan(cci[i]) else 0.0,
                'f4': float(adx[i]) if not np.isnan(adx[i]) else 25.0,
                'f5': float(rsi9[i]) if not np.isnan(rsi9[i]) else 50.0,
            }
            
            # Label: outcome after lookahead bars
            future_close = close[i + lookahead]
            current_close = close[i]
            
            if future_close > current_close * 1.005:  # +0.5%
                label = 1  # long
            elif future_close < current_close * 0.995:  # -0.5%
                label = -1  # short
            else:
                label = 0  # neutral
            
            features.append(feat)
            labels.append(label)
        
        # Save memory
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w') as f:
            json.dump({
                'features': features,
                'labels': labels,
                'lookahead': lookahead,
                'total_samples': len(features),
            }, f)
        
        logger.info(f"Trained Lorentzian memory: {len(features)} samples saved to {output_path}")
        return True
        
    except Exception as e:
        logger.error(f"Training failed: {e}")
        return False


def _calc_wt(high, low, close, period=10, smooth=11):
    n = len(close)
    wt = np.zeros(n)
    if n < period + smooth:
        return wt
    
    hlc3 = (high + low + close) / 3
    esa = np.zeros(n)
    multiplier = 2.0 / (period + 1)
    esa[0] = hlc3[0]
    for i in range(1, n):
        esa[i] = (hlc3[i] - esa[i-1]) * multiplier + esa[i-1]
    
    d = np.zeros(n)
    abs_diff = np.abs(hlc3 - esa)
    d[0] = abs_diff[0]
    for i in range(1, n):
        d[i] = (abs_diff[i] - d[i-1]) * multiplier + d[i-1]
    
    ci = np.zeros(n)
    for i in range(n):
        if d[i] > 0:
            ci[i] = (hlc3[i] - esa[i]) / (0.015 * d[i])
    
    wt_multiplier = 2.0 / (smooth + 1)
    wt[0] = ci[0]
    for i in range(1, n):
        wt[i] = (ci[i] - wt[i-1]) * wt_multiplier + wt[i-1]
    
    return wt
