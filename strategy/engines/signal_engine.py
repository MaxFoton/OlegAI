"""
Signal Engine - Multi-level score aggregation

Aggregates 4 score categories:
1. Scanner (external signal quality)
2. Regime (Lorentzian + market trend)
3. Microstructure (absorption + delta + exhaustion)
4. Trigger (velocity + reclaim + S/D zones)
"""

import logging
from typing import Dict

from pandas import DataFrame

logger = logging.getLogger(__name__)


class SignalEngine:
    """
    Aggregates scores from all engines and decides entry quality level.
    
    Output levels:
    - ELITE (score >= 9): high-confidence with full alignment
    - NORMAL (score >= 7): regular setup
    - WEAK: filtered out (was too noisy)
    """
    
    def __init__(self):
        pass
    
    def compute_scores(self, df: DataFrame, signal: dict, direction: str,
                      lorentzian_aligned: bool) -> Dict[str, float]:
        """
        Compute all 4 score components.
        
        Args:
            df: dataframe with all features
            signal: scanner signal dict
            direction: 'LONG' or 'SHORT'
            lorentzian_aligned: regime alignment flag
        
        Returns:
            dict with: scanner_score, regime_score, micro_score, trigger_score, total_score, confidence
        """
        last = df.iloc[-1]
        
        scanner_score = self._scanner_score(signal, last)
        regime_score = self._regime_score(last, lorentzian_aligned)
        micro_score = self._micro_score(last, direction, df)
        trigger_score = self._trigger_score(last, direction, df)
        
        total = scanner_score + regime_score + micro_score + trigger_score
        confidence = min(total / 15, 0.92)  # Clamp at 92%
        
        return {
            'scanner_score': scanner_score,
            'regime_score': regime_score,
            'micro_score': micro_score,
            'trigger_score': trigger_score,
            'total_score': total,
            'confidence': confidence,
        }
    
    # ============================================================
    # SCANNER SCORE (external signal quality)
    # ============================================================
    
    def _scanner_score(self, signal: dict, last) -> float:
        """Score based on external scanner signal quality"""
        score = 0.0
        
        # NEW: Если сигнал от DEX Scanner с новым форматом (score/15)
        if 'score' in signal and 'score_max' in signal:
            scanner_score_raw = signal.get('score', 0)
            scanner_score_max = signal.get('score_max', 15)
            
            # Нормализуем score к 0-4 диапазону (макс для scanner)
            # score >= 10/15 (67%) = 4 балла (ELITE)
            # score >= 7/15 (47%) = 2-3 балла (NORMAL)
            # score < 7/15 = 0-1 балл (REJECT)
            if scanner_score_max == 15:
                if scanner_score_raw >= 10:
                    score += 4  # ELITE signal
                elif scanner_score_raw >= 7:
                    score += 2.5  # NORMAL signal
                elif scanner_score_raw >= 5:
                    score += 1  # WEAK signal
                else:
                    score += 0  # REJECT (score < 5/15)
            elif scanner_score_max == 10:
                # Old format fallback
                if scanner_score_raw >= 7:
                    score += 3
                elif scanner_score_raw >= 5:
                    score += 2
                else:
                    score += 1
            
            # Бонус за очень высокий score
            if scanner_score_raw >= 12 and scanner_score_max == 15:
                score += 1  # extra bonus for 12+/15
        else:
            # OLD: legacy quality_score system
            quality = signal.get("quality_score", 0)
            score += quality * 4
        
        price_move = abs(signal.get("price_change_pct", 0))
        if price_move > 3:
            score += 2
        
        volume_spike = last.get("volume_spike", 1.0) if hasattr(last, 'get') else last["volume_spike"]
        if volume_spike > 1.5:
            score += 1
        
        liq_imbalance = signal.get("liq_imbalance", 0)
        if liq_imbalance > 1.5:
            score += 2
        
        oi_change = signal.get("oi_change_pct", 0)
        if abs(oi_change) > 2:
            score += 1
        
        funding = signal.get("funding_rate", 0)
        if abs(funding) > 0.01:
            score += 1
        
        return score
    
    # ============================================================
    # REGIME SCORE (Lorentzian + market trend)
    # ============================================================
    
    def _regime_score(self, last, lorentzian_aligned: bool) -> float:
        """Score based on regime alignment"""
        score = 0.0
        
        if lorentzian_aligned:
            score += 4
        
        lor_signal = last["lor_signal"] if "lor_signal" in last else 0
        lor_strength = last["lor_strength"] if "lor_strength" in last else 0
        
        if lor_signal == 1:
            score += lor_strength * 2
        
        # Market phase trend
        trend_up = last.get("trend_up", 0) if hasattr(last, 'get') else last["trend_up"]
        trend_down = last.get("trend_down", 0) if hasattr(last, 'get') else last["trend_down"]
        if trend_up or trend_down:
            score += 1
        
        # Volatility regime
        volatility = last["volatility"] if "volatility" in last else 0
        if volatility > 0.015:
            score += 1
        
        return score
    
    # ============================================================
    # MICROSTRUCTURE SCORE (absorption + delta + exhaustion)
    # ============================================================
    
    def _micro_score(self, last, direction: str, df: DataFrame) -> float:
        """Score based on microstructure signals"""
        score = 0.0
        
        absorption_long = last["absorption_long"] if "absorption_long" in last else 0
        absorption_short = last["absorption_short"] if "absorption_short" in last else 0
        exhaustion_long = last["exhaustion_long"] if "exhaustion_long" in last else 0
        exhaustion_short = last["exhaustion_short"] if "exhaustion_short" in last else 0
        
        # Absorption - core signal
        if absorption_long or absorption_short:
            score += 3
        
        # Exhaustion in opposite direction = reversal confirmation
        if direction == "LONG" and exhaustion_short:
            score += 2
        elif direction == "SHORT" and exhaustion_long:
            score += 2
        
        # Delta trap - momentum exhaustion
        delta = last["delta"] if "delta" in last else 0
        if direction == "LONG" and delta < 0 and last["close"] > last["open"]:
            score += 2  # Selling absorbed
        elif direction == "SHORT" and delta > 0 and last["close"] < last["open"]:
            score += 2  # Buying absorbed
        
        # Wick analysis
        candle_range = last["high"] - last["low"]
        if candle_range > 0:
            body = abs(last["close"] - last["open"])
            upper_wick = last["high"] - max(last["close"], last["open"])
            lower_wick = min(last["close"], last["open"]) - last["low"]
            
            if direction == "LONG":
                wick_ratio = lower_wick / candle_range
                if wick_ratio > 0.5:
                    score += 2
                elif wick_ratio > 0.3:
                    score += 1
            elif direction == "SHORT":
                wick_ratio = upper_wick / candle_range
                if wick_ratio > 0.5:
                    score += 2
                elif wick_ratio > 0.3:
                    score += 1
        
        # Heavy volume in body
        if last.get("heavy_vol_body", 0) if hasattr(last, 'get') else last["heavy_vol_body"]:
            score += 1
        
        # Density support/resistance
        if direction == "LONG" and (last["density_below"] if "density_below" in last else 0):
            score += 1
        elif direction == "SHORT" and (last["density_above"] if "density_above" in last else 0):
            score += 1
        
        return score
    
    # ============================================================
    # TRIGGER SCORE (velocity + reclaim + zones + scalper)
    # ============================================================
    
    def _trigger_score(self, last, direction: str, df: DataFrame) -> float:
        """Score based on entry triggers"""
        score = 0.0
        
        velocity_long = last["velocity_long"] if "velocity_long" in last else 0
        velocity_short = last["velocity_short"] if "velocity_short" in last else 0
        reclaim_long = last["reclaim_long"] if "reclaim_long" in last else 0
        reclaim_short = last["reclaim_short"] if "reclaim_short" in last else 0
        scalper_buy = last["scalper_buy"] if "scalper_buy" in last else 0
        scalper_sell = last["scalper_sell"] if "scalper_sell" in last else 0
        
        # Pro Scalper (ITG/TEMA+MACD) - PRIMARY timing trigger
        if direction == "LONG" and scalper_buy:
            score += 3  # Strong timing signal
        elif direction == "SHORT" and scalper_sell:
            score += 3
        
        # Velocity in direction
        if direction == "LONG" and velocity_long:
            score += 2
        elif direction == "SHORT" and velocity_short:
            score += 2
        
        # S/D zone interaction (now tighter)
        if direction == "LONG" and (last["near_demand"] if "near_demand" in last else 0):
            score += 2
        elif direction == "SHORT" and (last["near_supply"] if "near_supply" in last else 0):
            score += 2
        
        # VWAP reclaim
        if direction == "LONG" and reclaim_long:
            score += 1
        elif direction == "SHORT" and reclaim_short:
            score += 1
        
        # Velocity acceleration (multi-candle)
        if len(df) >= 2:
            vel_now = df["velocity"].iloc[-1]
            vel_prev = df["velocity"].iloc[-2]
            
            if direction == "LONG" and vel_now > vel_prev and vel_now > 0:
                score += 2
            elif direction == "SHORT" and vel_now < vel_prev and vel_now < 0:
                score += 2
        
        # Multi-candle confirmation
        if len(df) >= 3:
            closes = df["close"].iloc[-3:]
            opens = df["open"].iloc[-3:]
            
            if direction == "LONG":
                bullish = sum((closes > opens).values)
                if bullish >= 2:
                    score += 1
            elif direction == "SHORT":
                bearish = sum((closes < opens).values)
                if bearish >= 2:
                    score += 1
        
        # EMA20 reclaim
        if len(df) >= 2:
            ema20 = df["ema_fast"].iloc[-1]
            ema20_prev = df["ema_fast"].iloc[-2]
            close_prev = df["close"].iloc[-2]
            last_close = df["close"].iloc[-1]
            
            if direction == "LONG":
                if last_close > ema20 and close_prev <= ema20_prev:
                    score += 2
            elif direction == "SHORT":
                if last_close < ema20 and close_prev >= ema20_prev:
                    score += 2
        
        return score
    
    # ============================================================
    # ENTRY CLASSIFICATION
    # ============================================================
    
    def classify_entry(self, total_score: float, 
                      score_strong: float = 9,
                      score_normal: float = 7) -> str:
        """Classify entry as ELITE/NORMAL/SKIP"""
        if total_score >= score_strong:
            return "ELITE"
        elif total_score >= score_normal:
            return "NORMAL"
        else:
            return "SKIP"
