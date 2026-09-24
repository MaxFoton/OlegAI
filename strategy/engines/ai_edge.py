"""
AI Edge Engine - LightGBM regression model for entry probability

Uses trained model from train_ml_model.py (lightgbm_edge_model.pkl)
Predicts edge_score = future_max_profit - |future_max_drawdown|
"""

import logging
import pickle
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class AIEdgeEngine:
    """
    LightGBM-based edge predictor.
    
    Returns:
        edge_score: predicted asymmetric reward (positive = good entry)
    """
    
    def __init__(self, model_path: Optional[Path] = None):
        if model_path is None:
            model_path = Path(__file__).parent.parent / 'data' / 'lightgbm_edge_model.pkl'
        
        self.model_path = model_path
        self.model = None
        self.feature_cols = []
        self._load()
    
    def _load(self):
        """Load trained model"""
        if not self.model_path.exists():
            logger.warning(f"AI model not found: {self.model_path}")
            return
        
        try:
            with open(self.model_path, 'rb') as f:
                data = pickle.load(f)
            
            self.model = data.get('model')
            self.feature_cols = data.get('feature_cols', [])
            logger.info(f"✅ AI Edge model loaded: {len(self.feature_cols)} features")
        except Exception as e:
            logger.warning(f"AI model load failed: {e}")
            self.model = None
    
    def is_loaded(self) -> bool:
        return self.model is not None
    
    def predict(self, df: pd.DataFrame, signal: Dict, direction: str,
               funding_rate: float = 0.0) -> Dict:
        """
        Predict edge score for current entry candidate.
        
        Args:
            df: dataframe with all FeatureEngine features
            signal: scanner signal dict
            direction: 'LONG' or 'SHORT'
            funding_rate: optional funding
        
        Returns:
            {
                'edge_score': float (predicted edge),
                'confidence': float (0-1, scaled),
                'should_enter': bool (edge_score > threshold),
            }
        """
        if not self.is_loaded():
            return {'edge_score': 0.0, 'confidence': 0.5, 'should_enter': True}
        
        try:
            # Extract features matching trained model
            features = self._extract_features(df, signal, direction, funding_rate)
            
            if features is None:
                return {'edge_score': 0.0, 'confidence': 0.5, 'should_enter': True}
            
            # Reorder to match training feature_cols
            X = np.array([[features.get(col, 0) for col in self.feature_cols]])
            
            # Predict
            edge_score = float(self.model.predict(X)[0])
            
            # Confidence: scale edge_score to 0-1
            # Positive edge = good, negative = bad
            # Use sigmoid-like scaling
            confidence = 1 / (1 + np.exp(-edge_score * 50))
            
            # Should enter if predicted edge > 0
            should_enter = edge_score > 0
            
            return {
                'edge_score': edge_score,
                'confidence': float(confidence),
                'should_enter': should_enter,
            }
        
        except Exception as e:
            logger.debug(f"AI predict error: {e}")
            return {'edge_score': 0.0, 'confidence': 0.5, 'should_enter': True}
    
    def _extract_features(self, df: pd.DataFrame, signal: Dict, direction: str,
                         funding_rate: float = 0.0) -> Optional[Dict]:
        """Extract features matching training schema"""
        try:
            last = df.iloc[-1]
            
            # Match features used in build_ml_dataset.py
            features = {
                # Momentum
                'rsi': float(last.get('rsi', 50)) if not pd.isna(last.get('rsi', 50)) else 50,
                'rsi9': 50,  # may not be calculated
                'mfi': 50,
                'adx': float(last.get('adx', 25)) if not pd.isna(last.get('adx', 25)) else 25,
                'cci': 0,
                'atr': float(last.get('atr', 0)) if not pd.isna(last.get('atr', 0)) else 0,
                'roc_5': 0,
                'roc_10': 0,
                'macd': 0,
                'macd_hist': 0,
                
                # Distance
                'distance_from_ema20': float((last['close'] - last['ema_fast']) / last['ema_fast']) if last.get('ema_fast', 0) > 0 else 0,
                'distance_from_ema50': 0,
                'distance_from_ema200': 0,
                'distance_from_vwap': float((last['close'] - last['vwap']) / last['vwap']) if last.get('vwap', 0) > 0 else 0,
                
                # Price action
                'body_pct': float(abs(last['close'] - last['open']) / last['open']) if last['open'] > 0 else 0,
                'upper_wick_ratio': 0,
                'lower_wick_ratio': 0,
                'range_pct': 0,
                
                # Volume
                'volume_ratio': float(last.get('volume_spike', 1)) if not pd.isna(last.get('volume_spike', 1)) else 1,
                'volume_slope': 0,
                
                # Movement
                'pct_5m': float(last.get('pct_change_1', 0)) if not pd.isna(last.get('pct_change_1', 0)) else 0,
                'pct_15m': float(last.get('pct_change_3', 0)) if not pd.isna(last.get('pct_change_3', 0)) else 0,
                'pct_1h': float(last.get('pct_change_6', 0)) if not pd.isna(last.get('pct_change_6', 0)) else 0,
                
                # Position - need to calc
                'price_position_1h': 0.5,
                
                # Side
                'side': 1 if direction == 'LONG' else -1,
                
                # Microstructure
                'delta_now': float(last.get('delta', 0)) / (last.get('volume', 1) + 0.0001),
                'delta_5_avg': 0,
                'delta_slope': 0,
                'cum_delta_5': 0,
                'absorption': float(last.get('absorption_score', 0)),
                'atr_ratio': 1.0,
                'higher_highs': 0,
                'lower_lows': 0,
                
                # Funding
                'funding_rate': float(funding_rate),
                
                # Regime (placeholder)
                'btc_trend': 0,
                'btc_volatility': 0,
                'btc_pct_1h': 0,
                'eth_trend': 0,
                'eth_pct_1h': 0,
                'sol_trend': 0,
                'sol_pct_1h': 0,
                'market_regime_score': 0,
            }
            
            # Calculate price_position_1h
            if len(df) >= 12:
                recent_high = df['high'].iloc[-12:].max()
                recent_low = df['low'].iloc[-12:].min()
                if recent_high != recent_low:
                    features['price_position_1h'] = (last['close'] - recent_low) / (recent_high - recent_low)
            
            # Calculate wick ratios
            candle_range = last['high'] - last['low']
            if candle_range > 0:
                body = abs(last['close'] - last['open'])
                upper_wick = last['high'] - max(last['close'], last['open'])
                lower_wick = min(last['close'], last['open']) - last['low']
                features['upper_wick_ratio'] = upper_wick / candle_range
                features['lower_wick_ratio'] = lower_wick / candle_range
                features['range_pct'] = candle_range / last['open'] if last['open'] > 0 else 0
            
            return features
        
        except Exception as e:
            logger.debug(f"Feature extraction error: {e}")
            return None
