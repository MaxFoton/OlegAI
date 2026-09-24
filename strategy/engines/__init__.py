"""
Funtik Trading Engines - Modular Architecture

Pipeline:
    MARKET
        ↓
    DataEngine        - Signal loading, OHLCV access
        ↓
    FeatureEngine     - All technical indicators
        ↓
    RegimeEngine      - Market regime, Lorentzian, phase detection
        ↓
    SignalEngine      - Score aggregation
        ↓
    AIEdgeModel       - LightGBM probability scorer
        ↓
    ExecutionEngine   - Entry decisions
        ↓
    RiskEngine        - Stop loss, hold zones, drawdown
        ↓
    TradeManagement   - Custom exits, scale-out
        ↓
    FeedbackLoop      - Snapshots for training
"""

from .data_engine import DataEngine
from .feature_engine import FeatureEngine
from .regime_engine import RegimeEngine
from .signal_engine import SignalEngine

__all__ = [
    'DataEngine',
    'FeatureEngine',
    'RegimeEngine',
    'SignalEngine',
]
