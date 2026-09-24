"""
Regime Engine - Market regime detection (Lorentzian + market phase)
"""

import logging
import sys
from pathlib import Path
from typing import Tuple

from pandas import DataFrame

logger = logging.getLogger(__name__)


# Singleton classifier (loaded once)
_CLASSIFIER = None


def get_classifier():
    """Lazy-load Lorentzian classifier"""
    global _CLASSIFIER
    if _CLASSIFIER is None:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from lorentzian_classifier import LorentzianClassifier
        _CLASSIFIER = LorentzianClassifier()
        try:
            _CLASSIFIER.load_memory()
            logger.info("Lorentzian memory loaded")
        except Exception as e:
            logger.warning(f"Failed loading Lorentzian: {e}")
    return _CLASSIFIER


class RegimeEngine:
    """
    Detects market regime, direction bias, and expansion phase.
    
    Outputs:
    - Lorentzian signal/strength/prediction
    - Market phase (EARLY/MID/LATE_EXPANSION, EXHAUSTION)
    """
    
    def __init__(self):
        self._classifier = get_classifier()
    
    def compute_lorentzian(self, df: DataFrame) -> DataFrame:
        """Add Lorentzian columns to dataframe"""
        df["lor_signal"] = 0
        df["lor_strength"] = 0.0
        df["lor_prediction"] = 0
        
        try:
            prediction = self._classifier.predict(df)
            df["lor_signal"] = prediction["signal"]
            df["lor_strength"] = prediction["strength"]
            df["lor_prediction"] = prediction.get("prediction", 0)
        except Exception as e:
            logger.warning(f"Lorentzian error: {e}")
        
        return df
    
    def detect_phase(self, df: DataFrame, lookback: int = 12) -> str:
        """
        Detect market expansion phase based on price position
        in recent range.
        
        Returns:
            EARLY_EXPANSION - 15-65% range position
            MID_EXPANSION - 65-85% range position
            LATE_EXPANSION - >85% (near recent high)
            EXHAUSTION - <15% (near recent low)
        """
        if len(df) < lookback:
            return "UNKNOWN"
        
        recent_high = df["high"].iloc[-lookback:].max()
        recent_low = df["low"].iloc[-lookback:].min()
        last_close = df["close"].iloc[-1]
        
        if recent_high == recent_low:
            return "EARLY_EXPANSION"
        
        position = (last_close - recent_low) / (recent_high - recent_low)
        
        if position > 0.85:
            return "LATE_EXPANSION"
        elif position > 0.65:
            return "MID_EXPANSION"
        elif position > 0.15:
            return "EARLY_EXPANSION"
        else:
            return "EXHAUSTION"
    
    def is_lorentzian_aligned(self, df: DataFrame, direction: str, 
                              min_pred: int = 3) -> bool:
        """Check if Lorentzian regime aligns with trade direction"""
        lor_signal = df["lor_signal"].iloc[-1]
        lor_prediction = df["lor_prediction"].iloc[-1]
        
        if direction == "LONG":
            return lor_signal == 1 or lor_prediction >= min_pred
        elif direction == "SHORT":
            return lor_signal == -1 or lor_prediction <= -min_pred
        return False
    
    def can_enter_phase(self, phase: str, direction: str) -> Tuple[bool, str]:
        """
        Check if entry is allowed given current phase
        
        Returns:
            (allowed, reason)
        """
        # Block buying tops
        if phase == "LATE_EXPANSION" and direction == "LONG":
            return False, "LATE_EXPANSION - buying top"
        
        # Block selling bottoms
        if phase == "EXHAUSTION" and direction == "SHORT":
            return False, "EXHAUSTION - selling bottom"
        
        return True, "OK"
    
    def detect_setup_mode(self, df, direction: str) -> str:
        """
        Determine setup mode: REVERSAL or CONTINUATION
        
        Based on Lorentzian vs scanner direction alignment:
        - CONTINUATION: Lorentzian aligned with direction (trend continues)
        - REVERSAL: Lorentzian opposite or neutral (mean reversion play)
        
        Returns:
            'CONTINUATION' or 'REVERSAL'
        """
        if len(df) < 1:
            return "REVERSAL"
        
        last = df.iloc[-1]
        lor_signal = last["lor_signal"]
        lor_prediction = last["lor_prediction"]
        lor_strength = last["lor_strength"]
        
        # Strong Lorentzian alignment with direction = CONTINUATION
        if direction == "LONG":
            if lor_signal == 1 and lor_strength >= 0.6 and lor_prediction >= 4:
                return "CONTINUATION"
        elif direction == "SHORT":
            if lor_signal == -1 and lor_strength >= 0.6 and lor_prediction <= -4:
                return "CONTINUATION"
        
        # Otherwise it's a REVERSAL play
        return "REVERSAL"
