"""
Feature Engine - All technical indicators and price action features
"""

import logging

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

logger = logging.getLogger(__name__)


class FeatureEngine:
    """
    Computes all technical indicators and price action features.
    
    Outputs include:
    - Momentum: RSI, MACD, ADX, ROC
    - Trend: EMA, VWAP
    - Volume: spike, slope, density
    - Price action: body, wicks, range, delta
    - Microstructure: absorption, exhaustion, S/D zones
    """
    
    def __init__(self, velocity_threshold: float = 0.4):
        self.velocity_threshold = velocity_threshold
    
    def compute(self, df: DataFrame) -> DataFrame:
        """Compute all features in correct order"""
        df = self._price_action(df)
        df = self._momentum(df)
        df = self._trend(df)
        df = self._volume(df)
        df = self._delta(df)
        df = self._volatility(df)
        df = self._sd_zones(df)
        df = self._absorption(df)
        df = self._exhaustion(df)
        df = self._velocity(df)
        df = self._reclaim(df)
        df = self._densities(df)
        df = self._pro_scalper(df)
        return df
    
    # ============================================================
    # PRICE ACTION
    # ============================================================
    
    def _price_action(self, df: DataFrame) -> DataFrame:
        df["pct_change_1"] = df["close"].pct_change(1) * 100
        df["pct_change_3"] = df["close"].pct_change(3) * 100
        df["pct_change_6"] = df["close"].pct_change(6) * 100
        df["momentum"] = df["pct_change_1"] - df["pct_change_1"].shift(1)
        return df
    
    # ============================================================
    # MOMENTUM
    # ============================================================
    
    def _momentum(self, df: DataFrame) -> DataFrame:
        df["rsi"] = ta.RSI(df["close"], timeperiod=14)
        return df
    
    # ============================================================
    # TREND (EMA + ADX + VWAP)
    # ============================================================
    
    def _trend(self, df: DataFrame) -> DataFrame:
        df["ema_fast"] = ta.EMA(df["close"], timeperiod=34)
        df["ema_slow"] = ta.EMA(df["close"], timeperiod=89)
        df["adx"] = ta.ADX(df, timeperiod=14)
        
        df["trend_up"] = (
            (df["ema_fast"] > df["ema_slow"]) &
            (df["adx"] >= 18)
        ).astype(int)
        
        df["trend_down"] = (
            (df["ema_fast"] < df["ema_slow"]) &
            (df["adx"] >= 18)
        ).astype(int)
        
        # VWAP (session-anchored)
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        df["vwap"] = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
        
        return df
    
    # ============================================================
    # VOLATILITY
    # ============================================================
    
    def _volatility(self, df: DataFrame) -> DataFrame:
        df["atr"] = ta.ATR(df, timeperiod=14)
        df["volatility"] = df["atr"] / df["close"]
        return df
    
    # ============================================================
    # VOLUME
    # ============================================================
    
    def _volume(self, df: DataFrame) -> DataFrame:
        df["volume_sma"] = df["volume"].rolling(20).mean()
        df["volume_spike"] = df["volume"] / df["volume_sma"]
        return df
    
    # ============================================================
    # DELTA (Range-weighted buy/sell pressure)
    # ============================================================
    
    def _delta(self, df: DataFrame) -> DataFrame:
        rng = df["high"] - df["low"]
        rng = rng.replace(0, 0.0001)
        df["delta"] = df["volume"] * (df["close"] - df["open"]) / rng
        df["abs_delta"] = np.abs(df["delta"])
        return df
    
    # ============================================================
    # SUPPLY / DEMAND ZONES (Pivot-based)
    # ============================================================
    
    def _sd_zones(self, df: DataFrame) -> DataFrame:
        df["pivot_high"] = df["high"].rolling(5, center=True).max()
        df["pivot_low"] = df["low"].rolling(5, center=True).min()
        
        df["supply_zone"] = (
            (df["high"] == df["pivot_high"]) &
            (df["close"] < df["open"])
        ).astype(int)
        
        df["demand_zone"] = (
            (df["low"] == df["pivot_low"]) &
            (df["close"] > df["open"])
        ).astype(int)
        
        df["supply_strength"] = df["supply_zone"] * df["volume_spike"]
        df["demand_strength"] = df["demand_zone"] * df["volume_spike"]
        
        # Near supply/demand (tightened: ATR/2 instead of full ATR)
        atr = df["atr"].fillna(df["close"] * 0.01)
        atr_zone = atr * 0.5  # Tighter zones - was full ATR (75%), now ~30-40%
        
        df["near_supply"] = (
            (df["high"] >= df["pivot_high"].shift(1) - atr_zone) &
            (df["high"] <= df["pivot_high"].shift(1) + atr_zone)
        ).astype(int)
        
        df["near_demand"] = (
            (df["low"] >= df["pivot_low"].shift(1) - atr_zone) &
            (df["low"] <= df["pivot_low"].shift(1) + atr_zone)
        ).astype(int)
        
        return df
    
    # ============================================================
    # ABSORPTION (volume bubble detection)
    # ============================================================
    
    def _absorption(self, df: DataFrame) -> DataFrame:
        # Scaled Volume
        vol_std = df["volume"].rolling(100).std()
        df["scaled_vol"] = df["volume"] / vol_std.replace(0, 0.0001)
        
        # Wick Zones
        mid_price = (df["high"] + df["low"]) / 2
        top_body = np.maximum(df["open"], df["close"])
        low_body = np.minimum(df["open"], df["close"])
        
        df["upper_zone"] = (
            (mid_price >= top_body) & (mid_price <= df["high"])
        ).astype(int)
        
        df["lower_zone"] = (
            (mid_price <= low_body) & (mid_price >= df["low"])
        ).astype(int)
        
        # Absorption levels
        limit_factor = 0.1
        
        df["abs_tiny"] = (
            (df["scaled_vol"] >= limit_factor) &
            (df["scaled_vol"] < limit_factor + 1)
        ).astype(int)
        df["abs_small"] = (
            (df["scaled_vol"] >= limit_factor + 1) &
            (df["scaled_vol"] < limit_factor + 2)
        ).astype(int)
        df["abs_normal"] = (
            (df["scaled_vol"] >= limit_factor + 2) &
            (df["scaled_vol"] < limit_factor + 3)
        ).astype(int)
        df["abs_large"] = (
            (df["scaled_vol"] >= limit_factor + 3) &
            (df["scaled_vol"] < limit_factor + 6)
        ).astype(int)
        df["abs_huge"] = (
            df["scaled_vol"] >= limit_factor + 6
        ).astype(int)
        
        any_absorption = (
            df["abs_tiny"] | df["abs_small"] | df["abs_normal"] |
            df["abs_large"] | df["abs_huge"]
        )
        
        # Buying absorption (lower wick - buyers absorbing)
        df["absorption_long"] = (any_absorption & df["lower_zone"]).astype(int)
        # Selling absorption (upper wick - sellers absorbing)
        df["absorption_short"] = (any_absorption & df["upper_zone"]).astype(int)
        
        # Absorption score (1-5)
        df["absorption_score"] = (
            df["abs_tiny"] * 1 + df["abs_small"] * 2 +
            df["abs_normal"] * 3 + df["abs_large"] * 4 +
            df["abs_huge"] * 5
        )
        
        # Heavy volume in body (inside body absorption)
        inside_body = (mid_price > low_body) & (mid_price < top_body)
        df["heavy_vol_body"] = (
            inside_body & (df["scaled_vol"] > 2.1)
        ).astype(int)
        
        # Heavy volume body separation by direction (matches PineScript barcolor)
        bull_bar = df["close"] > df["open"]
        bear_bar = df["close"] < df["open"]
        df["heavy_vol_bull"] = (df["heavy_vol_body"] & bull_bar).astype(int)
        df["heavy_vol_bear"] = (df["heavy_vol_body"] & bear_bar).astype(int)
        
        return df
    
    # ============================================================
    # EXHAUSTION
    # ============================================================
    
    def _exhaustion(self, df: DataFrame) -> DataFrame:
        move = abs(df["pct_change_3"])
        
        df["exhaustion_long"] = (
            (move > 4) & (df["momentum"] < 0)
        ).astype(int)
        
        df["exhaustion_short"] = (
            (move > 4) & (df["momentum"] > 0)
        ).astype(int)
        
        return df
    
    # ============================================================
    # VELOCITY
    # ============================================================
    
    def _velocity(self, df: DataFrame) -> DataFrame:
        velocity = (df["close"] - df["close"].shift(1)) / df["close"].shift(1)
        df["velocity"] = velocity * 100
        df["velocity_long"] = (df["velocity"] > self.velocity_threshold).astype(int)
        df["velocity_short"] = (df["velocity"] < -self.velocity_threshold).astype(int)
        return df
    
    # ============================================================
    # RECLAIM (VWAP cross)
    # ============================================================
    
    def _reclaim(self, df: DataFrame) -> DataFrame:
        df["reclaim_long"] = (
            (df["close"] > df["vwap"]) &
            (df["close"].shift(1) <= df["vwap"].shift(1))
        ).astype(int)
        
        df["reclaim_short"] = (
            (df["close"] < df["vwap"]) &
            (df["close"].shift(1) >= df["vwap"].shift(1))
        ).astype(int)
        
        return df
    
    # ============================================================
    # DENSITIES (Volume clusters)
    # ============================================================
    
    def _densities(self, df: DataFrame) -> DataFrame:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        df["volume_density"] = df["volume"] * typical_price
        df["density_ma"] = df["volume_density"].rolling(20).mean()
        
        df["density_spike"] = (
            df["volume_density"] > df["density_ma"] * 1.5
        ).astype(int)
        
        df["high_density"] = (
            df["density_spike"].rolling(3).sum() >= 2
        ).astype(int)
        
        df["density_above"] = (
            (df["high_density"] == 1) & (df["high"] > df["close"])
        ).astype(int)
        
        df["density_below"] = (
            (df["high_density"] == 1) & (df["low"] < df["close"])
        ).astype(int)
        
        return df
    
    # ============================================================
    # PRO SCALPER (Session VWAP + Opening Range + Trend Filter)
    # ============================================================
    
    def _pro_scalper(self, df: DataFrame) -> DataFrame:
        """
        PRO Scalper - based on profitprotrading PineScript indicator
        
        Components:
        - Session-anchored VWAP (already calculated in _trend)
        - Opening Range (first N candles of session)
        - Trend Filter: EMA(34)/EMA(89) + ADX(14) >= 18
        """
        # ATR(14) - already calculated in _volatility
        # VWAP - already calculated in _trend
        
        # =====================================================
        # OPENING RANGE - first 30 minutes of session
        # =====================================================
        # For 5m timeframe = 6 candles (30 minutes)
        or_bars = 6
        
        # Detect session start (UTC day change)
        # Use rolling window to find first or_bars of each day
        df['date_only'] = pd.to_datetime(df['date']).dt.date if 'date' in df.columns else None
        
        if df['date_only'] is not None:
            df['session_bar'] = df.groupby('date_only').cumcount()
            
            # OR High/Low: max/min of first or_bars
            df['or_high'] = df.groupby('date_only')['high'].transform(
                lambda x: x.iloc[:or_bars].max() if len(x) >= or_bars else x.max()
            )
            df['or_low'] = df.groupby('date_only')['low'].transform(
                lambda x: x.iloc[:or_bars].min() if len(x) >= or_bars else x.min()
            )
            
            # Above/below OR
            df['above_or_high'] = (df['close'] > df['or_high']).astype(int)
            df['below_or_low'] = (df['close'] < df['or_low']).astype(int)
            
            df = df.drop(columns=['date_only', 'session_bar'])
        else:
            df['or_high'] = df['high']
            df['or_low'] = df['low']
            df['above_or_high'] = 0
            df['below_or_low'] = 0
        
        # =====================================================
        # TREND FILTER (EMA + ADX)
        # =====================================================
        # ema_fast/slow + adx already in df from _trend
        # Trend OK signals (EMA + ADX >= 18)
        
        df['scalper_long_ok'] = (
            (df['ema_fast'] > df['ema_slow']) &
            (df['adx'] >= 18) &
            (df['close'] > df['vwap'])
        ).astype(int)
        
        df['scalper_short_ok'] = (
            (df['ema_fast'] < df['ema_slow']) &
            (df['adx'] >= 18) &
            (df['close'] < df['vwap'])
        ).astype(int)
        
        # =====================================================
        # SCALPER BUY/SELL SIGNALS
        # =====================================================
        # Combined: trend aligned + breakout from OR
        
        # BUY: trend_long + price above OR_high (breakout)
        df['scalper_buy'] = (
            (df['scalper_long_ok'] == 1) &
            (df['above_or_high'] == 1)
        ).astype(int)
        
        # SELL: trend_short + price below OR_low (breakdown)
        df['scalper_sell'] = (
            (df['scalper_short_ok'] == 1) &
            (df['below_or_low'] == 1)
        ).astype(int)
        
        return df
