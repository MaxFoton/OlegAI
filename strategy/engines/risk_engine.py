"""
Risk Engine - Stop loss, hold zones, fast fail logic
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class RiskEngine:
    """
    Manages risk decisions:
    - Fast fail (cut losses early)
    - Hold zones (don't exit during normal pullback)
    - Absorption rescue (hold if reversal signs)
    """
    
    def __init__(self,
                 fast_fail_duration: int = 30,
                 fast_fail_profit: float = -0.015,
                 fast_fail_velocity: float = 0,
                 hold_zone_lor_strength: float = 0.5,
                 hold_absorption_min: int = 3,
                 hold_lor_pred_min: int = 3):
        self.fast_fail_duration = fast_fail_duration
        self.fast_fail_profit = fast_fail_profit
        self.fast_fail_velocity = fast_fail_velocity
        self.hold_zone_lor_strength = hold_zone_lor_strength
        self.hold_absorption_min = hold_absorption_min
        self.hold_lor_pred_min = hold_lor_pred_min
    
    def in_hold_zone(self, last, is_long: bool) -> bool:
        """Standard hold zone: lorentzian + velocity in our direction"""
        lor_strength = last.get("lor_strength", 0) if hasattr(last, 'get') else last["lor_strength"]
        velocity = last["velocity"] if "velocity" in last else 0
        
        velocity_aligned = (
            (is_long and velocity > self.fast_fail_velocity) or
            (not is_long and velocity < -self.fast_fail_velocity)
        )
        
        return lor_strength > self.hold_zone_lor_strength and velocity_aligned
    
    def absorption_rescue(self, last, is_long: bool, current_profit: float) -> bool:
        """
        Hold even at -1..-2.5% if:
        - absorption_score >= 3 in our direction
        - lorentzian prediction supports
        - velocity reversing back to our side
        """
        if current_profit < -0.025:
            return False  # Too deep
        
        absorption_long = last["absorption_long"] if "absorption_long" in last else 0
        absorption_short = last["absorption_short"] if "absorption_short" in last else 0
        absorption_score = last["absorption_score"] if "absorption_score" in last else 0
        lor_prediction = last["lor_prediction"] if "lor_prediction" in last else 0
        velocity = last["velocity"] if "velocity" in last else 0
        
        absorption_in_dir = (
            (is_long and absorption_long) or
            (not is_long and absorption_short)
        )
        
        lorentzian_supports = (
            (is_long and lor_prediction >= self.hold_lor_pred_min) or
            (not is_long and lor_prediction <= -self.hold_lor_pred_min)
        )
        
        velocity_reversing = (
            (is_long and velocity > 0) or
            (not is_long and velocity < 0)
        )
        
        return (
            absorption_score >= self.hold_absorption_min and
            absorption_in_dir and
            lorentzian_supports and
            velocity_reversing
        )
    
    def should_fast_fail(self, last, is_long: bool, 
                       trade_duration: float, current_profit: float) -> bool:
        """
        Fast fail criteria:
        - Duration > 30 min
        - Profit < -1.5%
        - Velocity going against us
        """
        if trade_duration <= self.fast_fail_duration:
            return False
        
        if current_profit >= self.fast_fail_profit:
            return False
        
        velocity = last["velocity"] if "velocity" in last else 0
        velocity_dead = (
            (is_long and velocity < self.fast_fail_velocity) or
            (not is_long and velocity > -self.fast_fail_velocity)
        )
        
        return velocity_dead
