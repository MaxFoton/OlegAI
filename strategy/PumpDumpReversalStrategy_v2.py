"""
Funtik Reversal Strategy V2 - Modular engine-based architecture

Pipeline:
    DataEngine FeatureEngine RegimeEngine SignalEngine
    AIEdgeModel ExecutionEngine RiskEngine TradeManagement FeedbackLoop

This strategy is a thin orchestrator that delegates work to engines.
"""

import logging
import sqlite3
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from freqtrade.strategy import IStrategy, stoploss_from_open
from freqtrade.persistence import Trade
from pandas import DataFrame

# Local engines
sys.path.insert(0, str(Path(__file__).parent))
from engines import (
    DataEngine,
    FeatureEngine,
    RegimeEngine,
    SignalEngine,
)
from engines.risk_engine import RiskEngine
from engines.ai_edge import AIEdgeEngine

logger = logging.getLogger(__name__)


# ============================================================
# AUDIT LOG (12.07.2026)
# Отдельный служебный лог ТОЛЬКО для сигналов и сделок Фунтика.
# ПРОБЛЕМА: funtik.log ротируется на 10MB и забивается heartbeat/debug
# мусором за несколько часов — история входов/сигналов терялась,
# невозможно было посчитать задержку сигналсделка.
# РЕШЕНИЕ: отдельный логгер, свой файл, TimedRotatingFileHandler на
# 3 дня хранения (backupCount=3, ротация раз в сутки), пишет ТОЛЬКО:
# SIGNAL_SEEN, LGBM_DECISION, ENTRY, EXIT
# ============================================================
def _setup_audit_logger() -> logging.Logger:
    audit_logger = logging.getLogger("funtik_audit")
    if audit_logger.handlers:
        return audit_logger # уже настроен (повторный импорт/reload)

    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False # не дублировать в funtik.log

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "funtik_audit.log"

    from logging.handlers import TimedRotatingFileHandler
    handler = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    audit_logger.addHandler(handler)
    return audit_logger


audit_logger = _setup_audit_logger()

_AUDIT_DEDUP_EVENTS = frozenset({"SIGNAL_SEEN", "ENTRY_SIGNAL", "BLOCKED", "LGBM_DECISION"})
_audit_seen: set[tuple[str, ...]] = set()
_audit_signal_context: dict[str, dict[str, str]] = {}


def audit(event: str, pair: str = "", **fields) -> None:
    """Пишет одно уникальное событие сигнала или подтверждённое событие сделки."""
    try:
        if event == "SIGNAL_SEEN":
            _audit_signal_context[pair] = {
                key: str(fields.get(key, ""))
                for key in ("source", "signal_type", "signal_ts")
            }
        elif event in _AUDIT_DEDUP_EVENTS:
            context = _audit_signal_context.get(pair, {})
            for key in ("source", "signal_type", "signal_ts"):
                fields.setdefault(key, context.get(key, ""))
        if event in _AUDIT_DEDUP_EVENTS:
            key = (
                event,
                pair,
                str(fields.get("direction", "")),
                str(fields.get("source", "")),
                str(fields.get("signal_type", fields.get("mode", ""))),
                str(fields.get("reason", fields.get("decision", ""))),
                str(fields.get("signal_ts", "")),
            )
            if key in _audit_seen:
                return
            if len(_audit_seen) >= 10000:
                _audit_seen.clear()
            _audit_seen.add(key)
        extra = " | ".join(f"{k}={v}" for k, v in fields.items())
        audit_logger.info(f"{event:14s} | pair={pair:20s} | {extra}")
    except Exception:
        pass


# ============================================================
# CONFIG
# ============================================================

class Config:
    # Scanner
    #SIGNALS_PATH = Path(__file__).parent / "data" / "telegram_pump_signals.json"
    #DEX_SIGNALS_PATH = Path("/home/max/o_p/dex_scanner/data/dex_signals.json") # DEX сигналы (старое)
    DEX_SIGNALS_PATH = Path("/home/max/o_p/dex_scanner/data/dex_signals_analysis.json") # Сигналы из max-analysis
    SNAPSHOTS_DB = Path(__file__).parent / "data" / "trade_snapshots.db"
    MAX_SIGNAL_AGE_SEC = 1800 # 30 min — ждём подтверждения разворота (01.06: было 900)
    
    # Score thresholds (relaxed - need more trades for ML data)
    ENTRY_SCORE_NORMAL = 5 # was 6 5 (29.06: DEX сигналы 3-7/15)
    ENTRY_SCORE_STRONG = 9 # ELITE stays 9
    
    # Two-Mode setup
    REVERSAL_SCORE_MIN = 5 # threshold for reversal mode (was 6 5)
    CONTINUATION_SCORE_MIN = 5 # threshold for continuation mode (was 7 6)
    
    # AI Edge thresholds (07.07.2026: ВКЛЮЧЁН для фильтрации плохих сигналов)
    AI_EDGE_LOG_ONLY = False # False = blocks trades when edge < 0
    AI_EDGE_THRESHOLD = -0.01 # Block trades with negative predicted edge (было -0.005)
    
    # Risk (FIX 09.09.2026: stoploss -1.8% → -1.0% для R/R баланса)
    HARD_STOPLOSS = -0.010
    # TP restored to analyzer recommendations (09.09.2026):
    # WR 68.49% but avg loss 3.3× bigger than win → negative expectancy
    # Solution: Wider TP (1.5:1 R/R) to compensate, expect WR ~50%
    TAKE_PROFIT_1 = 0.015  # Soft TP at +1.5%
    TAKE_PROFIT_2 = 0.025  # Hard TP at +2.5%
    
    # NEW: Partial profit + breakeven (UPDATED 09.09.2026 for proper R/R)
    PARTIAL_TP_PROFIT = 0.012  # Close 50% at +1.2% to lock early profit
    PARTIAL_TP_RATIO = 0.5     # Close 50% at first target
    BREAKEVEN_TRIGGER = 0.008  # Move to breakeven at +0.8%
    
    # ═══════════════════════════════════════════════════════════════════
    # TWO-STAGE PARTIAL TP (NEW 06.07.2026)
    # Цель: держать позицию дольше, ловить большие движения
    #
    # Стратегия:
    # TP1: Закрыть 30% @ +1.5% (reversal) / +1.8% (continuation)
    # TP2: Закрыть 30% @ +2.5% (reversal) / +3.0% (continuation)
    # Runner: 40% остаётся с плотным trailing (может словить +5-10%)
    #
    # Было: 50% @ +1.0%/+1.2%, остаток 50%
    # Стало: 30% @ +1.5%/+1.8%, 30% @ +2.5%/+3.0%, остаток 40%
    # ═══════════════════════════════════════════════════════════════════
    
    # Mode-specific risk profiles (UPDATED 09.09.2026)
    # Restored wider TP targets from analyzer recommendation
    # Old: TP1 0.51%, TP2 1.25% - too tight, WR 68% but negative expectancy
    # New: TP1 1.5%, TP2 2.5% - proper R/R ratio, expect WR ~50%
    
    # REVERSAL: tight stops, fast TP (mean reversion plays)
    REVERSAL_TP1 = 0.015  # +1.5% close 30% (was 0.51%, too tight)
    REVERSAL_TP1_RATIO = 0.30
    REVERSAL_TP2 = 0.025  # +2.5% close 30% (was 1.25%)
    REVERSAL_TP2_RATIO = 0.30
    REVERSAL_BE = 0.006  # breakeven move at +0.6%
    REVERSAL_FAST_FAIL_DUR = 15
    REVERSAL_FAST_FAIL_PNL = -0.020  # -2.0%
    
    # CONTINUATION: wider stops, larger targets
    CONTINUATION_TP1 = 0.018  # +1.8% close 30%
    CONTINUATION_TP1_RATIO = 0.30
    CONTINUATION_TP2 = 0.030  # +3.0% close 30%
    CONTINUATION_TP2_RATIO = 0.30
    CONTINUATION_BE = 0.008   # breakeven at +0.8%
    CONTINUATION_FAST_FAIL_DUR = 12
    CONTINUATION_FAST_FAIL_PNL = -0.020  # -2.0%
    
    # Trailing - агрессивный для долгого удержания позиций
    # ОТКЛЮЧЕН (30.06.2026): Глобальный trailing срезал профит на +0.6%
    # Используется только умный trailing из custom_stoploss (от +1.5%)
    TRAILING_POSITIVE = None # Было 0.006 - ОТКЛЮЧЕНО
    TRAILING_OFFSET = None # Было 0.012 - ОТКЛЮЧЕНО
    
    # Velocity threshold
    VELOCITY_THRESHOLD = 0.25 # Снижен с 0.4 для спокойного рынка (25.05.2026)
    
    # Fast fail
    FAST_FAIL_DURATION = 15 # 15 min (было 30, 01.06)
    FAST_FAIL_PROFIT = -0.015

    # Early fast fail — если цена сразу пошла против (MFE=0%)
    # 06.06: увеличен таймаут для DEX reversal (reversal паттерны формируются дольше)
    # FIX (03.08.2026): Ослаблен для DEX_EARLY после анализа 24h
    # Проблема: 9 early_fail_dex_early сделок, 0 wins, -30 USDT
    # DEX_EARLY сигналы нужают больше времени развиться (OI spike → price move)
    EARLY_FAIL_DUR_REVERSAL = 45 # мин (было 30, увеличено для DEX_EARLY)
    EARLY_FAIL_DUR_CONTINUATION = 20 # мин (было 15 20)
    EARLY_FAIL_PNL = -0.012 # -1.2% (было -0.8%, больше терпения для early signals)

    # HTF trend filter for continuation
    HTF_CONT_LONG_BLOCK_PCT = -1.5
    HTF_CONT_SHORT_BLOCK_PCT = 1.5
    CONTINUATION_MATURE_LONG_HTF_PCT = 10.0
    CONTINUATION_MATURE_SHORT_HTF_PCT = -10.0

    # LGBM_NEUTRAL (17.07.2026): volume-сигналы по перевесу индикаторов/модели
    # двигаются медленнее настоящего DEX_EARLY (OI+объём спайк), поэтому им
    # нужен отдельный, более терпимый профиль вместо EARLY (20min/-0.8%).
    # Данные 66ч: 71 early_fail_dex_early, avg=-0.88%, итог -219 USDT —
    # выбивало на шуме до того как сигнал успевал проявиться.
    LGBM_NEUTRAL_FAST_FAIL_DUR = 15 # мин
    LGBM_NEUTRAL_FAST_FAIL_PNL = -0.015 # -1.5%
    LGBM_NEUTRAL_EARLY_FAIL_DUR = 25 # мин (было 20 под EARLY)
    LGBM_NEUTRAL_EARLY_FAIL_PNL = -0.010 # -1.0% (было -0.8% под EARLY)

    # DEX_PRO (23.07.2026): PRO patterns (VWAP bounce, EMA cross, RSI div)
    # Качественные паттерны, используем профиль REVERSAL
    DEX_PRO_FAST_FAIL_DUR = 15
    DEX_PRO_FAST_FAIL_PNL = -0.025
    DEX_PRO_EARLY_FAIL_DUR = 30
    DEX_PRO_EARLY_FAIL_PNL = -0.008

    # Trailing после partial TP — смягчён для runner'а (06.07.2026)
    # После закрытия 60% (2x TP) — тянем стоп шире чтобы runner поймал большие движения
    TRAILING_AFTER_PARTIAL_OFFSET = 0.012 # 1.2% отступ (было 0.8%)

    # Hold zone
    HOLD_ZONE_LOR_STRENGTH = 0.5
    HOLD_ABSORPTION_MIN = 3
    HOLD_LOR_PRED_MIN = 3
    
    # RSI overheated/oversold thresholds (14.07.2026, ужесточено с 78/22)
    # ПРОБЛЕМА: модель не выучила значимость rsi (importance=0 - датасет
    # почти весь был с rsi=0/дефолт до недавнего добавления фичи), поэтому
    # реальную защиту от входа на вершине/дне даёт ТОЛЬКО этот хардкод-порог.
    # ICX вошёл LONG при RSI=76 (порог 78 не сработал) прямо на вершине
    # движения. Классический перекуп/перепрод - 70/30, берём чуть шире.
    # FIX (30.07.2026): Ужесточены RSI фильтры после анализа 29-30 июля
    # Проблема: SHORT на RSI 30-39 давали убытки, LONG на RSI 66-71 тоже
    # Было: RSI_OVERHEATED_LONG=72, RSI_OVERSOLD_SHORT=28
    # Стало: RSI_OVERHEATED_LONG=65, RSI_OVERSOLD_SHORT=40
    RSI_OVERHEATED_LONG = 65  # блокировка LONG при RSI >= 65 (было 72)
    RSI_OVERSOLD_SHORT = 40   # блокировка SHORT при RSI <= 40 (было 28)


# ============================================================
# STRATEGY (thin orchestrator)
# ============================================================

class PumpDumpReversalStrategyV2(IStrategy):
    
    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "5m"
    startup_candle_count = 50  # Снижено со 100 для экономии RAM (01.09.2026)
    process_only_new_candles = False
    use_exit_signal = True
    position_adjustment_enable = True # Enable partial exits
    use_custom_stoploss = True # Enable breakeven move
    
    # REMOVED: Duplicate protections function - see actual one below (line ~297)
    
    def informative_pairs(self):
        """Регистрируем 1h таймфрейм для всех пар whitelist —
        нужно для HTF-фильтра в populate_entry_trend (_check_htf_trend)."""
        try:
            pairs = self.dp.current_whitelist()
        except Exception:
            return []
        return [(p, "1h") for p in pairs]
    
    minimal_roi = {"0": 100}
    stoploss = Config.HARD_STOPLOSS
    
    # TRAILING ОТКЛЮЧЁН (30.06-02.07.2026): Срезал профит на +0.6%
    # Используется ТОЛЬКО умный trailing из custom_stoploss (от +1.5%)
    trailing_stop = False
    trailing_stop_positive = None
    trailing_stop_positive_offset = None
    trailing_only_offset_is_reached = False
    
    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            # DISABLED 10.09.2026: StoplossGuard блокирует все сделки после 3 стоплоссов
            # {"method": "StoplossGuard", "lookback_period_candles": 24,
            #  "trade_limit": 3, "stop_duration_candles": 12},
            {"method": "LowProfitPairs", "lookback_period_candles": 48,
             "trade_limit": 4, "stop_duration_candles": 24, "required_profit": -0.02},
            {"method": "MaxDrawdown", "lookback_period_candles": 96,
             "trade_limit": 10, "stop_duration_candles": 48, "max_allowed_drawdown": 0.12},
        ]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Initialize engines
        self.data_engine = DataEngine(
            signals_path=Config.DEX_SIGNALS_PATH,
            max_signal_age_sec=Config.MAX_SIGNAL_AGE_SEC,
            dex_signals_path=Config.DEX_SIGNALS_PATH # NEW: DEX сигналы
        )
        self.feature_engine = FeatureEngine(
            velocity_threshold=Config.VELOCITY_THRESHOLD
        )
        self.regime_engine = RegimeEngine()
        self.signal_engine = SignalEngine()
        self.risk_engine = RiskEngine(
            fast_fail_duration=Config.FAST_FAIL_DURATION,
            fast_fail_profit=Config.FAST_FAIL_PROFIT,
            hold_zone_lor_strength=Config.HOLD_ZONE_LOR_STRENGTH,
            hold_absorption_min=Config.HOLD_ABSORPTION_MIN,
            hold_lor_pred_min=Config.HOLD_LOR_PRED_MIN,
        )
        self.ai_edge = AIEdgeEngine()
        
        # Snapshot store for ML feedback
        self._init_snapshots()
        self._current_scores = {}
        
        # Cooldown tracker для проигравших пар (09.06.2026)
        self._pair_cooldowns = {} # {pair: last_loss_timestamp}
    
    def _init_snapshots(self):
        """Initialize SQLite for trade snapshots"""
        Config.SNAPSHOTS_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(Config.SNAPSHOTS_DB)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE,
                timestamp TEXT,
                pair TEXT,
                direction TEXT,
                scanner_score REAL,
                regime_score REAL,
                micro_score REAL,
                trigger_score REAL,
                confidence REAL,
                total_score REAL,
                price_entry REAL,
                volatility REAL,
                volume_spike REAL,
                absorption_score REAL,
                lor_signal INTEGER,
                lor_strength REAL,
                lor_prediction INTEGER,
                market_phase TEXT,
                volume_slope REAL,
                delta_slope REAL,
                distance_from_ema20 REAL,
                distance_from_vwap REAL,
                rsi REAL,
                htf_trend_1h REAL,
                result TEXT,
                max_profit REAL,
                max_drawdown REAL,
                hold_time INTEGER,
                profit_ratio REAL,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
        conn.close()
    
    # ============================================================
    # INDICATORS
    # ============================================================
    
    def bot_loop_start(self, current_time, **kwargs) -> None:
        """
        Запускает active_scanner каждые 5 минут.
        Сканер проверяет 2/3 индикаторов (Lorentzian + Absorption + Scalper)
        для каждой пары whitelist. Результаты кешируются в scanner.top_opportunities.
        """
        try:
            from active_scanner import get_scanner
            scanner = get_scanner()

            if not scanner.should_scan():
                return

            pairlist = self.dp.current_whitelist()
            if not pairlist:
                return

            scanner.scan_all_pairs(strategy_instance=self, pairlist=pairlist)

        except Exception as e:
            logger.debug(f"bot_loop_start scan error: {e}")
    
    def _get_recent_losers(self, hours: int = 3) -> set:
        """
        Возвращает набор пар с убытками за последние N часов.
        Используется для cooldown — не входим повторно в проигравшие пары.
        
        Args:
            hours: количество часов для анализа (по умолчанию 3)
        
        Returns:
            set: набор пар (например {'BTC/USDT:USDT', 'ETH/USDT:USDT'})
        """
        losing_pairs = set()
        
        try:
            # Проверяем trades из DataProvider (если доступно)
            trades = self.dp.get_trades()
            if trades is not None:
                from datetime import timedelta
                cutoff_time = datetime.utcnow() - timedelta(hours=hours)
                
                for trade in trades:
                    # Проверяем закрытые сделки с убытком
                    if (hasattr(trade, 'close_date') and trade.close_date and
                        hasattr(trade, 'close_profit') and trade.close_profit < 0):
                        if trade.close_date >= cutoff_time:
                            losing_pairs.add(trade.pair)
        except Exception as e:
            logger.debug(f"Error getting recent losers: {e}")
        
        return losing_pairs
    
    def _check_pair_cooldown(self, pair: str, cooldown_hours: int = 3) -> bool:
        """
        Проверяет находится ли пара в cooldown после убытка.
        
        Args:
            pair: торговая пара
            cooldown_hours: длительность cooldown в часах
        
        Returns:
            True если пара в cooldown (блокировать вход), False если можно входить
        """
        if pair in self._pair_cooldowns:
            last_loss = self._pair_cooldowns[pair]
            from datetime import timedelta
            cooldown_end = last_loss + timedelta(hours=cooldown_hours)
            
            if datetime.utcnow() < cooldown_end:
                time_left = (cooldown_end - datetime.utcnow()).total_seconds() / 60
                logger.info(
                    f" COOLDOWN {pair.split('/')[0]} | {time_left:.0f} min left "
                    f"(last loss at {last_loss.strftime('%H:%M')})"
                )
                return True
        
        return False
    
    def _update_pair_cooldown(self, pair: str):
        """Обновляет время последнего убытка для пары (вызывается из confirm_trade_exit)"""
        self._pair_cooldowns[pair] = datetime.utcnow()
    
    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        """Compute all features via FeatureEngine + RegimeEngine"""
        df = self.feature_engine.compute(df)
        df = self.regime_engine.compute_lorentzian(df)
        
        # ATR для динамического стоп-лосса (29.06.2026)
        import talib
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
        
        return df
    
    # ============================================================
    # ENTRY
    # ============================================================
    
    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["enter_long"] = 0
        df["enter_short"] = 0
        df["enter_tag"] = ""
        
        if len(df) < 100:
            return df
        
        pair = metadata["pair"]
        ticker = pair.split("/")[0]
        
        # ═══════════════════════════════════════════════════════════════════
        # COOLDOWN CHECK (09.06.2026)
        # Блокируем повторный вход в пары с недавними убытками
        # Данные: 12/19 убытков (63%) - повторные потери на одних парах
        # Cooldown: 3 часа после убытка
        # ═══════════════════════════════════════════════════════════════════
        #if self._check_pair_cooldown(pair, cooldown_hours=3):
            #return df
        
        # 1. Get scanner signal (telegram + bybit_scanner)
        signal = self.data_engine.get_signal(pair)
        direction = None
        entry_source = None
        
        if signal:
            direction = signal.get("direction")
            entry_source = signal.get("source", "telegram")
            
            # NEW: Если есть конкретное направление от DEX scanner - используем его
            if direction and direction in ("LONG", "SHORT"):
                logger.info(
                    f" {ticker} | {direction} signal from {entry_source} "
                    f"score={signal.get('score', 0)}/10"
                )
                # НЕ проверяем отскок для сигналов с явным направлением
                # Scanner уже провёл анализ и определил direction
                
            # ═════════════════════════════════════════════════════════════════
            # NEUTRAL = нет торгового направления. Старый LGBM был обучен на
            # исходах разнородных прошлых сделок, а не на последующем движении
            # цены после DEX-сигнала, поэтому его override отключён: он создавал
            # убыточные LONG/SHORT при собственной оценке 40-45%.
            # Новый directional LGBM будет получать метки LONG/SHORT из
            # фактического движения после DEX-сигналов и отдавать направление
            # уже в scanner, до экспорта в Фунтик.
            elif direction == "NEUTRAL":
                audit("LGBM_DECISION", pair, decision="neutral_skip_legacy_model",
                      source=entry_source, score=signal.get("score", 0),
                      signal_ts=signal.get("timestamp", "?"))
                logger.info(f" NEUTRAL SKIP {ticker} | ожидание направления от DEX directional model")
                return df
                
            elif direction is None:
                logger.info(f" {ticker} | NEUTRAL signal from {entry_source}, checking active_scanner...")
                try:
                    from active_scanner import get_scanner
                    scanner = get_scanner()
                    opp = scanner.get_opportunity_details(pair)
                    # УЖЕСТОЧЕНО (10.06.2026): требуем 3 из 3 индикаторов для reversal
                    # Анализ: 2/5 лоссов = reversal с недостаточным подтверждением
                    required_confirmations = 3 # было 2
                    if opp and opp.get("confirmations", 0) >= required_confirmations:
                        direction = opp["direction"]
                        entry_source = f"{entry_source}+scan"
                        conf = opp.get("confirmations", 2)
                        det = opp.get("details", {})
                        logger.info(
                            f" {ticker} direction={direction} from active_scan [{conf}/3] "
                            f"lor={'' if det.get('lor') else ''} "
                            f"abs={'' if det.get('absorption') else ''} "
                            f"scl={'' if det.get('scalper') else ''}"
                        )
                    else:
                        logger.info(f" {ticker} | No clear direction from active_scan (need 3/3 confirmations)")
                        return df
                except Exception as e:
                    logger.debug(f"active_scan error {ticker}: {e}")
                    return df
        else:
            # Нет внешнего сигнала — ищем паттерн похожий на исторически прибыльный.
            # active_scanner сравнивает текущие фичи пары с WIN-снапшотами через cosine similarity.
            # Это правильная роль ML: не генерировать сигналы из воздуха,
            # а находить повторения уже отработавших прибыльных паттернов.
            try:
                from active_scanner import get_scanner
                scanner = get_scanner()
                opp = scanner.get_opportunity_details(pair)
                # УЖЕСТОЧЕНО (10.06.2026): требуем 3 из 3 для reversal
                required_confirmations = 3 # было 2
                if opp and opp.get("confirmations", 0) >= required_confirmations:
                    direction = opp["direction"]
                    entry_source = "active_scan"
                    conf = opp.get("confirmations", 2)
                    det = opp.get("details", {})
                    signal = {
                        "direction": direction,
                        "quality_score": conf / 3.0,
                        "price_change_pct": 0,
                        "volume_5m": 0,
                        "liq_imbalance": 0,
                        "oi_change_pct": 0,
                        "funding_rate": 0,
                        "source": "active_scan",
                    }
                    logger.info(
                        f" {ticker} active_scan | {direction} [{conf}/3] "
                        f"lor={'' if det.get('lor') else ''} "
                        f"abs={'' if det.get('absorption') else ''} "
                        f"scl={'' if det.get('scalper') else ''}"
                    )
            except Exception as e:
                logger.debug(f"active_scan error {ticker}: {e}")
        
        if not direction:
            return df
        
        # =====================================================
        # DEX MODE - Работа по сигналам Scanner (11.06.2026 + 08.07.2026)
        # =====================================================
        # Scanner уже проверил все условия (velocity, volume, score, timing)
        #
        # Типы сигналов:
        # - dex_early: EARLY LONG/SHORT (P6) - ранний вход, OI+volume spike
        # - dex_reversal: Reversal после pump/dump (P3)
        # - dex_pump: PUMP сигналы (P1)
        # - dex_trend: Continuation 1h momentum (P2)
        # - dex_pro: PRO patterns (VWAP bounce, EMA cross, RSI divergence) (NEW 23.07.2026)
        #
        # EARLY signals: БЕЗ проверок bounce/candles (это ранний вход!)
        # REVERSAL signals: С проверками bounce/velocity/candles
        # PRO signals: С теми же проверками что и REVERSAL
        # =====================================================
        if signal and signal.get("source") in ["dex_reversal", "dex_pump", "dex_trend", "dex_early", "dex_lgbm_neutral", "dex_pro", "max-analysis", "wait_queue"]:
            logger.info(
                f" DEX MODE {ticker} | {direction} | "
                f"source={signal.get('source')} | type={signal.get('signal_type')}"
            )
            
            last = df.iloc[-1]
            
            # Только логирование метрик (БЕЗ проверок!)
            velocity = last["velocity"]
            vol_spike = float(last.get("volume_spike", 1) if hasattr(last, 'get') else last["volume_spike"])
            # FIX (12.07.2026): экспортёр сохраняет score под ключом "score",
            # не "scanner_score" — раньше это всегда давало 0.0 в snapshots
            scanner_score = signal.get("score", signal.get("scanner_score", 0))
            signal_type = signal.get('signal_type', 'reversal')
            signal_source = signal.get('source', 'dex')
            signal_ts = signal.get("timestamp", "?")

            audit(
                "SIGNAL_SEEN", pair, direction=direction, source=signal_source,
                signal_type=signal_type, score=scanner_score, signal_ts=signal_ts,
            )
            
            # ═══════════════════════════════════════════════════════════════════
            # MAX-ANALYSIS СИГНАЛЫ (18.08.2026) - С ФИЛЬТРОМ "КОНЦА ДВИЖЕНИЯ"! 🚀
            # За них уже подумали: СКАНЕР (score 6-15) + МОДЕЛЬ (confidence) + KIRO (ВХОДИТЬ/ШОРТИТЬ)
            # 
            # ПРОБЛЕМА (11.09.2026): Сканер опаздывает! Находит аномалию ПОСЛЕ движения, не ДО!
            # Фунтик входит в КОНЕЦ импульса вместо НАЧАЛА.
            # 
            # РЕШЕНИЕ: Фильтр HTF тренда - НЕ ВХОДИТЬ если движение уже далеко ушло!
            # LONG: блок если HTF > +3% ИЛИ HTF < -3% (движение уже состоялось)
            # SHORT: блок если HTF < -3% (движение уже состоялось)
            # ═══════════════════════════════════════════════════════════════════
            if signal_source == "max-analysis":
                # 🔥 FIX (24.09.2026 00:10): Снижен порог с 0.5 до 0.15 для LONG
                # ПРОБЛЕМА: vol_spike считается из ТЕКУЩИХ данных Bybit, не из сканера!
                #   KMNO показывает 0.16 (низкая активность СЕЙЧАС), но сигнал был на высоком объеме РАНЬШЕ
                # РЕШЕНИЕ: Блокируем только СОВСЕМ мертвые монеты (vol < 0.15)
                # SHORT: фильтр ОТКЛЮЧЕН полностью
                if direction == "LONG" and vol_spike < 0.15:
                    audit("BLOCKED", pair, direction=direction, reason="volume_too_low",
                          vol_spike=f"{vol_spike:.2f}", source=signal_source, score=scanner_score)
                    logger.warning(
                        f"🛑 VOLUME FILTER BLOCK {ticker} | LONG | vol_spike={vol_spike:.2f}x < 0.15 | "
                        f"Недостаточный объём для входа!"
                    )
                    return df
                
                # Проверка "конца движения"
                # 🔥 FIX (24.09.2026 00:15): Ослаблены пороги с ±2.3% до ±6%
                # ПРИЧИНА: Слишком жесткие фильтры блокируют все max-analysis сигналы
                # РЕШЕНИЕ: Блокируем только при ОЧЕНЬ сильном движении (>6%)
                htf_trend_kiro = self._get_htf_trend_1h_pct(pair)
                
                # LONG: блок если движение уже ушло вверх > +6%
                if direction == "LONG" and htf_trend_kiro > 6.0:
                    audit("BLOCKED", pair, direction=direction, reason="late_entry_long",
                          htf_1h=f"{htf_trend_kiro:+.2f}%", source="max-analysis", score=scanner_score)
                    logger.warning(
                        f"🛑 MAX-ANALYSIS LATE ENTRY BLOCK {ticker} | LONG но htf_1h={htf_trend_kiro:+.2f}% > +6.0% | "
                        f"Движение УЖЕ произошло! Не входим в продолжение резкого пампа!"
                    )
                    return df
                
                # SHORT: блок если движение уже ушло вниз < -6%
                if direction == "SHORT" and htf_trend_kiro < -6.0:
                    audit("BLOCKED", pair, direction=direction, reason="late_entry_short",
                          htf_1h=f"{htf_trend_kiro:+.2f}%", source="max-analysis", score=scanner_score)
                    logger.warning(
                        f"🛑 MAX-ANALYSIS LATE ENTRY BLOCK {ticker} | SHORT но htf_1h={htf_trend_kiro:+.2f}% < -6.0% | "
                        f"Движение УЖЕ произошло! Не входим в продолжение дампа!"
                    )
                    return df
                
                logger.info(f"✅ MAX-ANALYSIS ENTRY {ticker} | {direction} | score={scanner_score}/15 | htf_1h={htf_trend_kiro:+.2f}%")
                
                audit("ENTRY_SIGNAL", pair, direction=direction, mode="MAX_ANALYSIS",
                      source=signal_source, signal_type=signal_type, signal_ts=signal_ts,
                      score=scanner_score, htf_1h=f"{htf_trend_kiro:+.2f}%")

                # Немедленный вход
                if direction == "LONG":
                    df.loc[df.index[-1], "enter_long"] = 1
                else:
                    df.loc[df.index[-1], "enter_short"] = 1
                
                entry_tag = f"kiro_{signal_type}_{direction.lower()}"
                df.loc[df.index[-1], "enter_tag"] = entry_tag
                
                # 🔥 FIX (22.09.2026): Сохранить ПОЛНЫЕ scores из JSON для snapshots!
                # ПРОБЛЕМА: Все snapshots имели confidence=0, phase=N/A, rsi=50 (default values)
                # ПРИЧИНА: _current_scores не заполнялся данными из JSON сигнала
                # РЕШЕНИЕ: Берём все поля из signal (который пришёл из dex_signals_analysis.json)
                
                # Базовые поля (как раньше)
                self._current_scores[pair] = {
                    "setup_mode": "MAX_ANALYSIS",
                    "scanner_score": scanner_score,
                    "velocity": velocity,
                    "volume_spike": vol_spike,
                    "signal_type": signal_type,
                    "source": signal_source,
                    "signal_ts": signal_ts,
                    "htf_trend_1h": htf_trend_kiro,
                }
                
                # Дополнительные поля из JSON (для правильных snapshots)
                if signal:
                    self._current_scores[pair].update({
                        "confidence": signal.get("confidence", 0),
                        "verdict": signal.get("verdict", ""),
                        "reasons_for": signal.get("reasons_for", []),
                        "reasons_against": signal.get("reasons_against", []),
                        "market_phase": signal.get("phase", ""),
                        "rsi": signal.get("rsi", 50),
                        "vw_macd": signal.get("vw_macd", 0),
                        "ema_trend": signal.get("ema", "neutral"),
                        "lor_signal": signal.get("lor_signal", 0),
                        "lor_strength": signal.get("lor_strength", 0),
                        "lor_prediction": signal.get("lor_pred", 0),
                    })
                
                return df
            
            # ═══════════════════════════════════════════════════════════════════
            # WAIT_QUEUE СИГНАЛЫ (14.09.2026) - ПОДТВЕРЖДЁННЫЕ "⏳ ЖДАТЬ"! 🎯
            # Эти сигналы УЖЕ прошли полную проверку в wait_queue_monitor:
            # 1. RSI коррекция завершена
            # 2. Bullish/bearish свеча появилась
            # 3. RSI развернулся в нашу сторону
            # 4. ВСЕ индикаторы (EMA + MACD + тренд) подтвердили
            # 
            # НИКАКИХ дополнительных проверок - это лучшие моменты входа!
            # ═══════════════════════════════════════════════════════════════════
            if signal_source == "wait_queue":
                # 🔥 FIX (23.09.2026 вечер): Снижен порог с 1.0 до 0.5 для LONG
                # ПРОБЛЕМА: Слишком много блокировок (vol 0.5-0.9), не хватает сделок
                # РЕШЕНИЕ: Блокируем только совсем мусор (vol < 0.5)
                # SHORT: фильтр ОТКЛЮЧЕН полностью (может работать на слабом объеме)
                if direction == "LONG" and vol_spike < 0.5:
                    audit("BLOCKED", pair, direction=direction, reason="volume_too_low",
                          vol_spike=f"{vol_spike:.2f}", source=signal_source, score=scanner_score)
                    logger.warning(
                        f"🛑 VOLUME FILTER BLOCK {ticker} | LONG | vol_spike={vol_spike:.2f}x < 0.5 | "
                        f"Недостаточный объём для входа!"
                    )
                    return df
                
                # Проверка "конца движения" как у max-analysis
                # 🔥 FIX (24.09.2026 00:15): Ослаблены пороги с ±2.3% до ±6%
                # ПРИЧИНА: Слишком жесткие фильтры блокируют сигналы
                # РЕШЕНИЕ: Блокируем только при ОЧЕНЬ сильном движении (>6%)
                htf_trend_wait = self._get_htf_trend_1h_pct(pair)
                
                # LONG: блок если движение уже ушло вверх > +6%
                if direction == "LONG" and htf_trend_wait > 6.0:
                    audit("BLOCKED", pair, direction=direction, reason="late_entry_long",
                          htf_1h=f"{htf_trend_wait:+.2f}%", source="wait_queue", score=scanner_score)
                    logger.warning(
                        f"🛑 WAIT_QUEUE LATE ENTRY BLOCK {ticker} | LONG но htf_1h={htf_trend_wait:+.2f}% > +6.0% | "
                        f"Движение УЖЕ произошло! Не входим в продолжение резкого пампа!"
                    )
                    return df
                
                # SHORT: блок если движение уже ушло вниз < -6%
                if direction == "SHORT" and htf_trend_wait < -6.0:
                    audit("BLOCKED", pair, direction=direction, reason="late_entry_short",
                          htf_1h=f"{htf_trend_wait:+.2f}%", source="wait_queue", score=scanner_score)
                    logger.warning(
                        f"🛑 WAIT_QUEUE LATE ENTRY BLOCK {ticker} | SHORT но htf_1h={htf_trend_wait:+.2f}% < -6.0% | "
                        f"Движение УЖЕ произошло! Не входим в продолжение дампа!"
                    )
                    return df
                
                logger.info(f"✅ WAIT_QUEUE CONFIRMED {ticker} | {direction} | score={scanner_score}/15 | htf_1h={htf_trend_wait:+.2f}%")
                
                audit("ENTRY_SIGNAL", pair, direction=direction, mode="WAIT_QUEUE",
                      source=signal_source, signal_type=signal_type, signal_ts=signal_ts,
                      score=scanner_score, htf_1h=f"{htf_trend_wait:+.2f}%")

                # Немедленный вход
                if direction == "LONG":
                    df.loc[df.index[-1], "enter_long"] = 1
                else:
                    df.loc[df.index[-1], "enter_short"] = 1
                
                entry_tag = f"wait_confirmed_{direction.lower()}"
                df.loc[df.index[-1], "enter_tag"] = entry_tag
                
                # 🔥 FIX (22.09.2026): Сохранить ПОЛНЫЕ scores из JSON для snapshots!
                # ПРОБЛЕМА: Все snapshots имели confidence=0, phase=N/A, rsi=50 (default values)
                # ПРИЧИНА: _current_scores не заполнялся данными из JSON сигнала
                # РЕШЕНИЕ: Берём все поля из signal (который пришёл из dex_signals_analysis.json)
                
                # Базовые поля (как раньше)
                self._current_scores[pair] = {
                    "setup_mode": "WAIT_QUEUE",
                    "scanner_score": scanner_score,
                    "velocity": velocity,
                    "volume_spike": vol_spike,
                    "signal_type": "wait_confirmed",
                    "source": "wait_queue",
                    "signal_ts": signal_ts,
                    "htf_trend_1h": htf_trend_wait,
                }
                
                # Дополнительные поля из JSON (для правильных snapshots)
                if signal:
                    self._current_scores[pair].update({
                        "confidence": signal.get("confidence", 0),
                        "verdict": signal.get("verdict", ""),
                        "reasons_for": signal.get("reasons_for", []),
                        "reasons_against": signal.get("reasons_against", []),
                        "market_phase": signal.get("phase", ""),
                        "rsi": signal.get("rsi", 50),
                        "vw_macd": signal.get("vw_macd", 0),
                        "ema_trend": signal.get("ema", "neutral"),
                        "lor_signal": signal.get("lor_signal", 0),
                        "lor_strength": signal.get("lor_strength", 0),
                        "lor_prediction": signal.get("lor_pred", 0),
                    })
                
                return df
            
            # ═══════════════════════════════════════════════════════════════════
            # SCANNER SCORE FILTER (05.08.2026) 🎯 УСИЛЕН!
            # Анализ 668 сигналов за сегодня показал:
            # Score 3-5: WR=8.7% ❌ (9W/94L) - МУСОР!
            # Score 5-7: WR=25.0% (42W/126L) - плохо
            # Score 7-10: WR=22.9% (51W/172L) - терпимо ✅
            # Score 10+: WR=61.1% (96W/61L) - отлично! ✅✅
            # 
            # РЕШЕНИЕ: Минимум 7 подтверждений из 15!
            # ═══════════════════════════════════════════════════════════════════
            MIN_SCANNER_SCORE = 7  # было 3, теперь 7 (05.08.2026)
            if scanner_score < MIN_SCANNER_SCORE:
                audit("BLOCKED", pair, direction=direction, reason="score_too_low",
                      score=scanner_score, min_required=MIN_SCANNER_SCORE,
                      source=signal_source, signal_type=signal_type)
                logger.warning(
                    f"🛑 SCORE BLOCK {ticker} | {direction} | score={scanner_score}/15 < {MIN_SCANNER_SCORE} | "
                    f"source={signal_source} | Анализ: WR для score<7 = 8-25% (мусор!)"
                )
                return df
            
            # ═══════════════════════════════════════════════════════════════════
            # VOLUME SPIKE MAX FILTER (05.08.2026) 🚫
            # Анализ 87 сделок за сутки показал:
            # Volume "high" (4-5x spike): WR=11% ❌ (1W/8L) - ХУДШИЙ результат!
            # Volume "elevated" (2-3x): WR=38% ✅
            # 
            # Причина: Экстремальный объём = КОНЕЦ движения, не начало!
            # Это распродажа крупняка на пике или скупка на дне.
            # 
            # РЕШЕНИЕ: Блокировать volume spike > 4.0
            # ═══════════════════════════════════════════════════════════════════
            if vol_spike > 4.0:
                audit("BLOCKED", pair, direction=direction, reason="volume_too_high",
                      vol_spike=f"{vol_spike:.2f}", source=signal_source, score=scanner_score)
                logger.warning(
                    f"🛑 VOLUME TOO HIGH BLOCK {ticker} | {direction} | vol_spike={vol_spike:.2f}x > 4.0 | "
                    f"Экстремальный объём = КОНЕЦ движения! Анализ: vol>4x WR=11%"
                )
                return df
            
            # ═══════════════════════════════════════════════════════════════════
            # LGBM NEUTRAL - БЛОКИРОВКА ОТКЛЮЧЕНА (05.08.2026)
            # Анализ 668 сигналов показал: dex_lgbm_neutral WR=32.7% (134W/276L)
            # Это НЕ плохо! Проблема была в LOW SCORE сигналах (3-5/15).
            # При score≥7 lgbm_neutral показывает приемлемые результаты.
            # 
            # ОТКЛЮЧЕНО: Фильтруем теперь по MIN_SCORE=7, а не по источнику.
            # ═══════════════════════════════════════════════════════════════════
            # if signal_source == "dex_lgbm_neutral":
            #     audit("BLOCKED", pair, direction=direction, reason="lgbm_neutral_disabled",
            #           source="dex_lgbm_neutral", score=scanner_score, signal_ts=signal_ts)
            #     logger.warning(
            #         f"🛑 LGBM NEUTRAL BLOCK {ticker} | score={scanner_score}/15"
            #     )
            #     return df
            
            # ═══════════════════════════════════════════════════════════════════
            # EARLY SIGNALS (08.07.2026) - ПРИОРИТЕТ!
            # EARLY LONG/SHORT = ранний вход при OI spike + volume
            # НЕ ПРОВЕРЯЕМ bounce/candles - это early entry!
            # ═══════════════════════════════════════════════════════════════════
            if signal_source == "dex_early":
                rsi_now_early = float(last.get("rsi", 50) or 50)
                htf_trend_early = self._get_htf_trend_1h_pct(pair)

                # ═══════════════════════════════════════════════════════════════
                # HTF-ТРЕНД ФИЛЬТР ДЛЯ P6 EARLY (12.07.2026, жёсткий блок в коде)
                # ДАННЫЕ (20ч): 7 из 8 EARLY LONG входили ПРОТИВ 1h тренда
                # (BRETT htf=-4.5%, USELESS htf=-2.6%, B3 htf=-4.6%, PORTAL htf=-2.5%)
                # итог тега dex_early_long: -26.0 при WR=13%.
                # РЕШЕНИЕ: реальный блок (не лог модели) — P6 EARLY тоже должен
                # соответствовать старшему тренду, иначе это ловля ножей.
                # ═══════════════════════════════════════════════════════════════
                if direction == "LONG" and htf_trend_early < Config.HTF_CONT_LONG_BLOCK_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="htf_trend_against",
                          htf_1h=f"{htf_trend_early:+.2f}%", rsi=f"{rsi_now_early:.0f}")
                    logger.info(
                        f" EARLY LONG BLOCK {ticker} | htf_1h={htf_trend_early:+.2f}% "
                        f"< {Config.HTF_CONT_LONG_BLOCK_PCT}% (против тренда)"
                    )
                    return df
                if direction == "SHORT" and htf_trend_early > Config.HTF_CONT_SHORT_BLOCK_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="htf_trend_against",
                          htf_1h=f"{htf_trend_early:+.2f}%", rsi=f"{rsi_now_early:.0f}")
                    logger.info(
                        f" EARLY SHORT BLOCK {ticker} | htf_1h={htf_trend_early:+.2f}% "
                        f"> {Config.HTF_CONT_SHORT_BLOCK_PCT}% (против тренда)"
                    )
                    return df
                # RSI-перегрев (вариант B): ENA входил на RSI=82 для LONG
                if direction == "LONG" and rsi_now_early >= Config.RSI_OVERHEATED_LONG:
                    audit("BLOCKED", pair, direction=direction, reason="rsi_overheated",
                          rsi=f"{rsi_now_early:.0f}")
                    logger.info(f" EARLY LONG BLOCK {ticker} | RSI={rsi_now_early:.0f} >= {Config.RSI_OVERHEATED_LONG} (перекуплен)")
                    return df
                if direction == "SHORT" and rsi_now_early <= Config.RSI_OVERSOLD_SHORT:
                    audit("BLOCKED", pair, direction=direction, reason="rsi_overheated",
                          rsi=f"{rsi_now_early:.0f}")
                    logger.info(f" EARLY SHORT BLOCK {ticker} | RSI={rsi_now_early:.0f} <= {Config.RSI_OVERSOLD_SHORT} (перепродан)")
                    return df

                logger.info(
                    f" EARLY ENTRY {ticker} | {direction} | "
                    f"vel={velocity:.2f} vol={vol_spike:.2f} score={scanner_score}/10 | "
                    f"htf_1h={htf_trend_early:+.2f}% rsi={rsi_now_early:.0f} | "
                    f"FAST ENTRY (no bounce/candle checks)"
                )
                audit(
                    "ENTRY_SIGNAL", pair, direction=direction, mode="DEX_EARLY",
                    source=signal_source, signal_type=signal_type, signal_ts=signal_ts,
                    score=scanner_score, rsi=f"{rsi_now_early:.0f}",
                    htf_1h=f"{htf_trend_early:+.2f}%", vol_spike=f"{vol_spike:.2f}",
                )
                # Прямой вход БЕЗ доп проверок (кроме HTF/RSI фильтров выше)!
                if direction == "LONG":
                    df.loc[df.index[-1], "enter_long"] = 1
                else:
                    df.loc[df.index[-1], "enter_short"] = 1
                
                entry_tag = f"dex_early_{direction.lower()}"
                df.loc[df.index[-1], "enter_tag"] = entry_tag
                
                self._current_scores[pair] = {
                    "setup_mode": "DEX_EARLY",
                    "scanner_score": scanner_score,
                    "velocity": velocity,
                    "volume_spike": vol_spike,
                    "signal_type": "early",
                    "source": "dex_early",
                    "rsi": rsi_now_early,
                    "htf_trend_1h": htf_trend_early,
                    "signal_ts": signal_ts,
                }
                
                return df

            # Старый код (отключён):
            """
            if signal_source == "dex_lgbm_neutral":
                rsi_now_lgbm = float(last.get("rsi", 50) or 50)
                htf_trend_lgbm = self._get_htf_trend_1h_pct(pair)

                if direction == "LONG" and htf_trend_lgbm < Config.HTF_CONT_LONG_BLOCK_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="htf_trend_against",
                          htf_1h=f"{htf_trend_lgbm:+.2f}%", source="dex_lgbm_neutral")
                    logger.info(f" LGBM LONG BLOCK {ticker} | htf_1h={htf_trend_lgbm:+.2f}% против тренда")
                    return df
                if direction == "SHORT" and htf_trend_lgbm > Config.HTF_CONT_SHORT_BLOCK_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="htf_trend_against",
                          htf_1h=f"{htf_trend_lgbm:+.2f}%", source="dex_lgbm_neutral")
                    logger.info(f" LGBM SHORT BLOCK {ticker} | htf_1h={htf_trend_lgbm:+.2f}% против тренда")
                    return df
                if direction == "LONG" and htf_trend_lgbm >= Config.CONTINUATION_MATURE_LONG_HTF_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="continuation_mature",
                          htf_1h=f"{htf_trend_lgbm:+.2f}%", source="dex_lgbm_neutral")
                    logger.info(
                        f" LGBM LONG BLOCK {ticker} | htf_1h={htf_trend_lgbm:+.2f}% "
                        f">= {Config.CONTINUATION_MATURE_LONG_HTF_PCT}% (late entry)"
                    )
                    return df
                if direction == "SHORT" and htf_trend_lgbm <= Config.CONTINUATION_MATURE_SHORT_HTF_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="continuation_mature",
                          htf_1h=f"{htf_trend_lgbm:+.2f}%", source="dex_lgbm_neutral")
                    logger.info(
                        f" LGBM SHORT BLOCK {ticker} | htf_1h={htf_trend_lgbm:+.2f}% "
                        f"<= {Config.CONTINUATION_MATURE_SHORT_HTF_PCT}% (late entry)"
                    )
                    return df
                if direction == "LONG" and rsi_now_lgbm >= Config.RSI_OVERHEATED_LONG:
                    audit("BLOCKED", pair, direction=direction, reason="rsi_overheated",
                          rsi=f"{rsi_now_lgbm:.0f}", source="dex_lgbm_neutral")
                    logger.info(f" LGBM LONG BLOCK {ticker} | RSI={rsi_now_lgbm:.0f} перекуплен")
                    return df
                if direction == "SHORT" and rsi_now_lgbm <= Config.RSI_OVERSOLD_SHORT:
                    audit("BLOCKED", pair, direction=direction, reason="rsi_overheated",
                          rsi=f"{rsi_now_lgbm:.0f}", source="dex_lgbm_neutral")
                    logger.info(f" LGBM SHORT BLOCK {ticker} | RSI={rsi_now_lgbm:.0f} перепродан")
                    return df

                audit("ENTRY_SIGNAL", pair, direction=direction, mode="DEX_LGBM_NEUTRAL",
                      source=signal_source, signal_type=signal_type, signal_ts=signal_ts,
                      score=scanner_score, rsi=f"{rsi_now_lgbm:.0f}", htf_1h=f"{htf_trend_lgbm:+.2f}%")
                logger.info(
                    f" LGBM NEUTRAL ENTRY {ticker} | {direction} | "
                    f"htf_1h={htf_trend_lgbm:+.2f}% rsi={rsi_now_lgbm:.0f}"
                )
                if direction == "LONG":
                    df.loc[df.index[-1], "enter_long"] = 1
                else:
                    df.loc[df.index[-1], "enter_short"] = 1

                df.loc[df.index[-1], "enter_tag"] = f"dex_lgbm_neutral_{direction.lower()}"

                self._current_scores[pair] = {
                    "setup_mode": "DEX_LGBM_NEUTRAL", # свой профиль риска (17.07.2026)
                    "scanner_score": scanner_score,
                    "velocity": velocity,
                    "volume_spike": vol_spike,
                    "signal_type": "lgbm_neutral",
                    "source": "dex_lgbm_neutral",
                    "rsi": rsi_now_lgbm,
                    "htf_trend_1h": htf_trend_lgbm,
                    "signal_ts": signal_ts,
                }

                return df
            """
            
            # ═══════════════════════════════════════════════════════════════════
            # PRO PATTERNS (NEW 23.07.2026) - отдельная ветка с проверками
            # VWAP bounce, EMA cross, RSI divergence — качественные паттерны,
            # но требуют дополнительных проверок как REVERSAL
            # ═══════════════════════════════════════════════════════════════════
            if signal_source == "dex_pro":
                rsi_now_pro = float(last.get("rsi", 50) or 50)
                htf_trend_pro = self._get_htf_trend_1h_pct(pair)

                # Применяем те же фильтры что и для LGBM_NEUTRAL
                if direction == "LONG" and htf_trend_pro < Config.HTF_CONT_LONG_BLOCK_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="htf_trend_against",
                          htf_1h=f"{htf_trend_pro:+.2f}%", source="dex_pro")
                    logger.info(f" PRO PATTERN LONG BLOCK {ticker} | htf_1h={htf_trend_pro:+.2f}% против тренда")
                    return df
                if direction == "SHORT" and htf_trend_pro > Config.HTF_CONT_SHORT_BLOCK_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="htf_trend_against",
                          htf_1h=f"{htf_trend_pro:+.2f}%", source="dex_pro")
                    logger.info(f" PRO PATTERN SHORT BLOCK {ticker} | htf_1h={htf_trend_pro:+.2f}% против тренда")
                    return df
                if direction == "LONG" and rsi_now_pro >= Config.RSI_OVERHEATED_LONG:
                    audit("BLOCKED", pair, direction=direction, reason="rsi_overheated",
                          rsi=f"{rsi_now_pro:.0f}", source="dex_pro")
                    logger.info(f" PRO PATTERN LONG BLOCK {ticker} | RSI={rsi_now_pro:.0f} перекуплен")
                    return df
                if direction == "SHORT" and rsi_now_pro <= Config.RSI_OVERSOLD_SHORT:
                    audit("BLOCKED", pair, direction=direction, reason="rsi_overheated",
                          rsi=f"{rsi_now_pro:.0f}", source="dex_pro")
                    logger.info(f" PRO PATTERN SHORT BLOCK {ticker} | RSI={rsi_now_pro:.0f} перепродан")
                    return df

                audit("ENTRY_SIGNAL", pair, direction=direction, mode="DEX_PRO",
                      source=signal_source, signal_type=signal_type, signal_ts=signal_ts,
                      score=scanner_score, rsi=f"{rsi_now_pro:.0f}", htf_1h=f"{htf_trend_pro:+.2f}%")
                logger.info(
                    f" PRO PATTERN ENTRY {ticker} | {direction} | "
                    f"pattern={signal_type} htf_1h={htf_trend_pro:+.2f}% rsi={rsi_now_pro:.0f}"
                )
                if direction == "LONG":
                    df.loc[df.index[-1], "enter_long"] = 1
                else:
                    df.loc[df.index[-1], "enter_short"] = 1

                df.loc[df.index[-1], "enter_tag"] = f"dex_pro_{direction.lower()}"

                self._current_scores[pair] = {
                    "setup_mode": "DEX_PRO",
                    "scanner_score": scanner_score,
                    "velocity": velocity,
                    "volume_spike": vol_spike,
                    "signal_type": signal_type,
                    "source": "dex_pro",
                    "rsi": rsi_now_pro,
                    "htf_trend_1h": htf_trend_pro,
                    "signal_ts": signal_ts,
                }

                return df
            
            # ═══════════════════════════════════════════════════════════════════
            # FLASH CRASH FILTER (03.08.2026) ⚡ КРИТИЧНО!
            # Проблема: ACX SHORT открыт на ОТСКОКЕ после flash crash -19%!
            # 03:30: 0.04066 → 0.03248 (-18.97%) flash crash
            # 03:45: вход SHORT на отскоке +6.6% от дна
            # Результат: -49 USDT (цена восстановилась обратно)
            # 
            # Решение: НЕ ВХОДИТЬ 35 минут после flash crash (свеча >10%)
            # Flash crash = экстремальное событие, нужна стабилизация!
            # ═══════════════════════════════════════════════════════════════════
            if len(df) >= 7:  # 7 свечей = 35 минут на 5m timeframe
                for i in range(1, 8):
                    check_candle = df.iloc[-i]
                    candle_change = abs(check_candle["close"] - check_candle["open"]) / check_candle["open"] if check_candle["open"] > 0 else 0
                    
                    # Flash crash detected: свеча >10% размером
                    if candle_change > 0.10:
                        crash_direction = check_candle["close"] - check_candle["open"]
                        
                        # SHORT вход после DOWN flash crash = ловля отскока!
                        if direction == "SHORT" and crash_direction < 0:
                            logger.warning(
                                f"🚫 FLASH CRASH BLOCK {ticker} | SHORT после DOWN crash -{candle_change:.1%} | "
                                f"{i} свечей назад | это ОТСКОК, не шортить!"
                            )
                            return df
                        
                        # LONG вход после UP flash crash = ловля отката!
                        if direction == "LONG" and crash_direction > 0:
                            logger.warning(
                                f"🚫 FLASH CRASH BLOCK {ticker} | LONG после UP crash +{candle_change:.1%} | "
                                f"{i} свечей назад | это ОТКАТ, не лонговать!"
                            )
                            return df
            
            # ═══════════════════════════════════════════════════════════════════
            # NEW FILTERS (04.08.2026) 🎯 
            # ROLLBACK (04.08.2026 20:00): Анализ 82 сделок показал что фильтры 2-4 ВРЕДНЫ!
            # 
            # Lorentzian AGAINST: WR 36% (лучше чем WITH: 19%!) - фильтр блокировал хорошие сделки
            # Volume <0.7: WR 33% (лучше чем 0.7-1.2: 15%!) - фильтр блокировал хорошие сделки
            # Below EMA: WR 38% (лучше чем just above: 26%!) - фильтр блокировал хорошие сделки
            #
            # ОСТАВЛЕН ТОЛЬКО 1 фильтр: CONT SHORT (WR 12%)
            # ═══════════════════════════════════════════════════════════════════
            
            # FILTER 1: CONTINUATION SHORT (DISABLED 05.08.2026)
            # Анализ 668 сигналов: dex_trend (continuation) WR=30.3% - не так плохо!
            # Проблема была в LOW SCORE, не в типе сигнала
            # ОТКЛЮЧЁН - фильтруем по MIN_SCORE вместо этого
            # ═══════════════════════════════════════════════════════════════════
            # Определяем setup_mode по signal_type для логики ниже
            if signal_type in ["continuation", "trend"]:
                setup_mode = "CONTINUATION"
            elif signal_type == "reversal":
                setup_mode = "REVERSAL"
            else:
                setup_mode = "UNKNOWN"
            
            # if setup_mode == "CONTINUATION" and direction == "SHORT":
            #     audit("BLOCKED", pair, direction=direction, reason="cont_short_disabled",
            #           mode="CONTINUATION", source=signal_source, score=scanner_score)
            #     logger.warning(
            #         f"🛑 CONT SHORT BLOCK {ticker} | score={scanner_score}/15"
            #     )
            #     return df
            
            # ═══════════════════════════════════════════════════════════════════
            # ЖЁСТКИЙ ФИЛЬТР для DEX REVERSAL LONG (16.06.2026)
            # ПРОБЛЕМА: Вход ПОЗДНО после пампа (PUFFER bounce=24.97%, HMSTR уже отскочил)
            # РЕШЕНИЕ: Вход ТОЛЬКО в EARLY bounce (0.5-15%), НЕ после завершённого пампа
            # ═══════════════════════════════════════════════════════════════════
            if signal_type == "reversal" and direction == "LONG":
                # 1. Проверка что цена ОТСКОЧИЛА от дна, но НЕ слишком много
                if len(df) >= 6:
                    recent_low = df["low"].iloc[-6:].min()
                    current_price = last["close"]
                    bounce_pct = (current_price - recent_low) / recent_low * 100
                    
                    # TOO EARLY: bounce < 0.5% = ещё падает
                    if bounce_pct < 0.5:
                        logger.info(f" DEX REVERSAL LONG BLOCK {ticker} | bounce {bounce_pct:.2f}% < 0.5% (too early, still falling)")
                        return df
                    
                    # TOO LATE: bounce > 15% = памп уже закончился, мы опоздали
                    if bounce_pct > 15.0:
                        logger.info(f" DEX REVERSAL LONG BLOCK {ticker} | bounce {bounce_pct:.2f}% > 15% (too late, pump finished)")
                        return df
                
                # 2. Velocity должен быть ПОЛОЖИТЕЛЬНЫЙ, но НЕ слишком сильный (impulse)
                if velocity < 0.2:
                    logger.info(f" DEX REVERSAL LONG BLOCK {ticker} | velocity {velocity:.2f} < 0.2 (still falling)")
                    return df
                
                # NEW: Не входить на IMPULSE candle (velocity > 5 = памп уже идёт)
                if velocity > 5.0:
                    logger.info(f" DEX REVERSAL LONG BLOCK {ticker} | velocity {velocity:.2f} > 5.0 (impulse candle, too late)")
                    return df
                
                # 3. Последние 2 свечи должны быть GREEN (подтверждение роста)
                if len(df) >= 2:
                    candle_1 = df.iloc[-1]["close"] > df.iloc[-1]["open"] # текущая зелёная
                    candle_2 = df.iloc[-2]["close"] > df.iloc[-2]["open"] # предыдущая зелёная
                    
                    if not (candle_1 and candle_2):
                        logger.info(f" DEX REVERSAL LONG BLOCK {ticker} | need 2 green candles confirmation")
                        return df
                    
                    # NEW: Проверка размера текущей свечи (не входить на большой зелёной свече)
                    last_candle_size = abs(last["close"] - last["open"]) / last["open"] * 100
                    if last_candle_size > 3.0: # Свеча больше 3% = памп уже идёт
                        logger.info(f" DEX REVERSAL LONG BLOCK {ticker} | current candle {last_candle_size:.2f}% > 3% (impulse, entering too late)")
                        return df
                
                # 4. Volume должен быть СИЛЬНЫЙ для reversal
                if vol_spike < 1.5:
                    logger.info(f" DEX REVERSAL LONG BLOCK {ticker} | volume {vol_spike:.2f} < 1.5 (weak)")
                    return df
                
                logger.info(
                    f" DEX REVERSAL LONG PASSED {ticker} | "
                    f"bounce={bounce_pct:.2f}% (0.5-15% window) | "
                    f"vel={velocity:.2f} (0.2-5.0 range) | "
                    f"vol={vol_spike:.2f} | "
                    f"candle_size={last_candle_size:.2f}% (<3%)"
                )
            
            # ═══════════════════════════════════════════════════════════════════
            # ЖЁСТКИЙ ФИЛЬТР для DEX REVERSAL SHORT (07.07.2026)
            # ПРОБЛЕМА: Вход SHORT после ЗАВЕРШЁННОГО дампа (AKE -13.43%, RSI=19)
            # РЕШЕНИЕ: Вход ТОЛЬКО в EARLY drop (0.5-10%), НЕ после завершённого дампа
            # ═══════════════════════════════════════════════════════════════════
            if signal_type == "reversal" and direction == "SHORT":
                # 1. Проверка что цена УПАЛА от хая, но НЕ слишком много
                if len(df) >= 6:
                    recent_high = df["high"].iloc[-6:].max()
                    current_price = last["close"]
                    drop_pct = (recent_high - current_price) / recent_high * 100
                    
                    # TOO EARLY: drop < 0.5% = ещё растёт
                    if drop_pct < 0.5:
                        logger.info(f" DEX REVERSAL SHORT BLOCK {ticker} | drop {drop_pct:.2f}% < 0.5% (too early, still rising)")
                        return df
                    
                    # TOO LATE: drop > 10% = дамп уже закончился, мы опоздали
                    if drop_pct > 10.0:
                        logger.info(f" DEX REVERSAL SHORT BLOCK {ticker} | drop {drop_pct:.2f}% > 10% (too late, dump finished, RSI oversold)")
                        return df
                
                # 2. RSI должен быть НЕ OVERSOLD (иначе готов отскочить)
                try:
                    rsi_now = float(df.iloc[-1].get("rsi", 50))
                except:
                    rsi_now = 50.0
                
                if rsi_now < 30:
                    logger.info(f" DEX REVERSAL SHORT BLOCK {ticker} | RSI {rsi_now:.0f} < 30 (oversold, ready for bounce)")
                    return df
                
                # 3. Velocity должен быть ОТРИЦАТЕЛЬНЫЙ (падение продолжается)
                if velocity > -0.2:
                    logger.info(f" DEX REVERSAL SHORT BLOCK {ticker} | velocity {velocity:.2f} > -0.2 (not falling enough)")
                    return df
                
                # NEW: Не входить на IMPULSE candle DOWN (velocity < -5 = дамп уже идёт)
                if velocity < -5.0:
                    logger.info(f" DEX REVERSAL SHORT BLOCK {ticker} | velocity {velocity:.2f} < -5.0 (impulse candle, too late)")
                    return df
                
                # 4. Последние 2 свечи должны быть RED (подтверждение падения)
                if len(df) >= 2:
                    candle_1 = df.iloc[-1]["close"] < df.iloc[-1]["open"] # текущая красная
                    candle_2 = df.iloc[-2]["close"] < df.iloc[-2]["open"] # предыдущая красная
                    
                    if not (candle_1 and candle_2):
                        logger.info(f" DEX REVERSAL SHORT BLOCK {ticker} | need 2 red candles confirmation")
                        return df
                    
                    # NEW: Проверка размера текущей свечи (не входить на большой красной свече)
                    last_candle_size = abs(last["close"] - last["open"]) / last["open"] * 100
                    if last_candle_size > 3.0: # Свеча больше 3% = дамп уже идёт
                        logger.info(f" DEX REVERSAL SHORT BLOCK {ticker} | current candle {last_candle_size:.2f}% > 3% (impulse, entering too late)")
                        return df
                
                # 5. Volume должен быть СИЛЬНЫЙ для reversal
                if vol_spike < 1.5:
                    logger.info(f" DEX REVERSAL SHORT BLOCK {ticker} | volume {vol_spike:.2f} < 1.5 (weak)")
                    return df
                
                logger.info(
                    f" DEX REVERSAL SHORT PASSED {ticker} | "
                    f"drop={drop_pct:.2f}% (0.5-10% window) | "
                    f"RSI={rsi_now:.0f} (>30, not oversold) | "
                    f"vel={velocity:.2f} (-5.0 to -0.2 range) | "
                    f"vol={vol_spike:.2f} | "
                    f"candle_size={last_candle_size:.2f}% (<3%)"
                )
            
            # Другие типы (continuation) прошли все проверки
            logger.info(
                f" DEX ENTRY {ticker} | {direction} | type={signal_type} | "
                f"vel={velocity:.2f} vol={vol_spike:.2f} score={scanner_score}/10 | "
                f" REVERSAL LONG/SHORT have strict filters (bounce/drop, RSI, velocity, candles)"
            )
            
            # ═══════════════════════════════════════════════════════════════════
            # HTF-ТРЕНД + RSI ФИЛЬТР ДЛЯ CONTINUATION (12.07.2026, варианты A+B)
            # ПРОБЛЕМА: dex_continuation_* входили БЕЗ единой проверки, 72% убыточных
            # continuation никогда не были в плюсе (14д: -227.8 из -371 общего минуса).
            # РЕШЕНИЕ: РЕАЛЬНЫЙ блок входа (не только лог модели) — continuation
            # LONG против часового тренда или на RSI-перегреве блокируется сразу.
            # LightGBM дополнительно логирует prob_win для сбора обучающих данных
            # (фичи rsi/htf_trend_1h уже в наборе — после переобучения модель сама
            # научится взвешивать их точнее, чем фиксированный порог).
            # Вход остаётся БЕЗ ЗАДЕРЖКИ (сразу на этой свече) — только сам сигнал
            # должен пройти фильтр, никакого дополнительного ожидания.
            # ═══════════════════════════════════════════════════════════════════
            rsi_now = float(last.get("rsi", 50) or 50)
            htf_trend = self._get_htf_trend_1h_pct(pair)

            if signal_type == "continuation":
                if direction == "LONG" and htf_trend < Config.HTF_CONT_LONG_BLOCK_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="htf_trend_against",
                          htf_1h=f"{htf_trend:+.2f}%", signal_type="continuation")
                    logger.info(
                        f" CONTINUATION LONG BLOCK {ticker} | htf_1h={htf_trend:+.2f}% "
                        f"< {Config.HTF_CONT_LONG_BLOCK_PCT}% (против тренда)"
                    )
                    return df
                if direction == "SHORT" and htf_trend > Config.HTF_CONT_SHORT_BLOCK_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="htf_trend_against",
                          htf_1h=f"{htf_trend:+.2f}%", signal_type="continuation")
                    logger.info(
                        f" CONTINUATION SHORT BLOCK {ticker} | htf_1h={htf_trend:+.2f}% "
                        f"> {Config.HTF_CONT_SHORT_BLOCK_PCT}% (против тренда)"
                    )
                    return df
                if direction == "LONG" and htf_trend >= Config.CONTINUATION_MATURE_LONG_HTF_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="continuation_mature",
                          htf_1h=f"{htf_trend:+.2f}%", signal_type="continuation")
                    logger.info(
                        f" CONTINUATION LONG BLOCK {ticker} | htf_1h={htf_trend:+.2f}% "
                        f">= {Config.CONTINUATION_MATURE_LONG_HTF_PCT}% (late entry)"
                    )
                    return df
                if direction == "SHORT" and htf_trend <= Config.CONTINUATION_MATURE_SHORT_HTF_PCT:
                    audit("BLOCKED", pair, direction=direction, reason="continuation_mature",
                          htf_1h=f"{htf_trend:+.2f}%", signal_type="continuation")
                    logger.info(
                        f" CONTINUATION SHORT BLOCK {ticker} | htf_1h={htf_trend:+.2f}% "
                        f"<= {Config.CONTINUATION_MATURE_SHORT_HTF_PCT}% (late entry)"
                    )
                    return df
                if direction == "LONG" and rsi_now >= Config.RSI_OVERHEATED_LONG:
                    audit("BLOCKED", pair, direction=direction, reason="rsi_overheated",
                          rsi=f"{rsi_now:.0f}", signal_type="continuation")
                    logger.info(f" CONTINUATION LONG BLOCK {ticker} | RSI={rsi_now:.0f} >= {Config.RSI_OVERHEATED_LONG} (перекуплен)")
                    return df
                if direction == "SHORT" and rsi_now <= Config.RSI_OVERSOLD_SHORT:
                    audit("BLOCKED", pair, direction=direction, reason="rsi_overheated",
                          rsi=f"{rsi_now:.0f}", signal_type="continuation")
                    logger.info(f" CONTINUATION SHORT BLOCK {ticker} | RSI={rsi_now:.0f} <= {Config.RSI_OVERSOLD_SHORT} (перепродан)")
                    return df

            audit("ENTRY_SIGNAL", pair, direction=direction, mode="DEX_SCANNER",
                  source=signal_source, signal_type=signal_type, signal_ts=signal_ts,
                  score=scanner_score, rsi=f"{rsi_now:.0f}", htf_1h=f"{htf_trend:+.2f}%")

            # Вход БЕЗ ЗАДЕРЖКИ — быстрая реакция важнее (пожелание: "входить сразу")
            if direction == "LONG":
                df.loc[df.index[-1], "enter_long"] = 1
            else:
                df.loc[df.index[-1], "enter_short"] = 1
            
            entry_tag = f"dex_{signal.get('signal_type', 'reversal')}_{direction.lower()}"
            df.loc[df.index[-1], "enter_tag"] = entry_tag
            
            # Сохранить minimal scores для trade management + обучающие фичи
            self._current_scores[pair] = {
                "setup_mode": "DEX_SCANNER",
                "scanner_score": scanner_score,
                "velocity": velocity,
                "volume_spike": vol_spike,
                "signal_type": signal.get('signal_type', 'unknown'),
                "source": signal.get('source', 'dex'),
                "rsi": rsi_now,
                "htf_trend_1h": htf_trend,
                "signal_ts": signal_ts,
            }
            
            return df
        
        # =====================================================
        # STANDARD MODE - Для Telegram/Bybit/Active Scanner
        # =====================================================
        
        # 2. Check market phase
        phase = self.regime_engine.detect_phase(df)
        can_enter, phase_reason = self.regime_engine.can_enter_phase(phase, direction)
        if not can_enter:
            logger.info(f" PHASE BLOCK {ticker} | {phase_reason}")
            return df
        
        # 2b. EARLY_EXPANSION — ослабленный фильтр RSI
        # CHANGED: убираем жёсткое требование перекупленности/перепроданности
        # Достаточно подтверждения направления от других индикаторов
        if phase == "EARLY_EXPANSION":
            try:
                rsi_now = float(df.iloc[-1].get("rsi", 50) if hasattr(df.iloc[-1], 'get') else df.iloc[-1]["rsi"])
            except Exception:
                rsi_now = 50.0
            # Ослабленные условия: разрешаем торговлю в более широком диапазоне RSI
            if direction == "LONG" and rsi_now >= 70: # было 40, теперь блокируем только при сильной перекупленности
                logger.info(f" PHASE BLOCK {ticker} | EARLY_EXPANSION + RSI={rsi_now:.0f} >= 70 (too overbought for LONG)")
                return df
            if direction == "SHORT" and rsi_now <= 30: # было 60, теперь блокируем только при сильной перепроданности
                logger.info(f" PHASE BLOCK {ticker} | EARLY_EXPANSION + RSI={rsi_now:.0f} <= 30 (too oversold for SHORT)")
                return df
        
        # 2b2. FOMO-блок: lor_signal совпадает с direction + EARLY_EXPANSION
        # Тест 82 сделок: "with Lorentzian + EARLY_EXPANSION" WR=19%, sum=-26.55%
        # Это значит: Lorentzian подтвердил тренд, цена уже ушла, Фунтик заходит в хвост.
        # "against Lorentzian + lor_pred тоже против" WR=65%, sum=+4.87% (контртренд с подтверждением)
        if phase == "EARLY_EXPANSION":
            try:
                lor_sig = int(df.iloc[-1].get("lor_signal", 0) if hasattr(df.iloc[-1], 'get') else df.iloc[-1]["lor_signal"])
                lor_with = (direction == "LONG" and lor_sig == 1) or (direction == "SHORT" and lor_sig == -1)
                if lor_with:
                    logger.info(f" FOMO BLOCK {ticker} | EARLY_EXPANSION + lor_signal WITH direction (WR=19%)")
                    return df
            except Exception:
                pass
        
        # 2c. LATE_EXPANSION + SHORT заблокирован — зеркало LATE_EXPANSION+LONG.
        # Шортить на хаю часа (pos_1h=0.97 как у 1000CAT) = продолжение шорта,
        # не разворот. Данные: 1000CAT SHORT pos_1h=0.97 -2.04%.
        if phase == "LATE_EXPANSION" and direction == "SHORT":
            logger.info(f" PHASE BLOCK {ticker} | LATE_EXPANSION + SHORT blocked")
            return df
        
        # 2d. Volume filter — данные 82 сделок: volume_spike < 0.8 WR=33%.
        # Без объёма нет движения. Минимум normal volume.
        # Порог 0.5 — снижен 18.08.2026 из-за отсутствия сделок (было 0.8)
        if len(df) >= 1:
            try:
                vol_spike = float(df.iloc[-1].get("volume_spike", 1) if hasattr(df.iloc[-1], 'get') else df.iloc[-1]["volume_spike"])
                if vol_spike < 0.5:
                    logger.info(f" VOLUME BLOCK {ticker} | vol_spike={vol_spike:.2f} < 0.5 (18.08: снижен с 0.8)")
                    return df
            except Exception:
                pass
        
        # 2e. FOMO filter — расширен до любой фазы (не только EARLY_EXPANSION).
        # HANA: pct_1h=+2.62%, phase=EARLY_EXPANSION должен был заблокировать,
        # но теперь EARLY_EXPANSION уже заблокирован выше.
        # Оставляем для MID/LATE/EXHAUSTION на случай разогнанных входов.
        if len(df) >= 13:
            try:
                pct_1h_now = (df["close"].iloc[-1] - df["close"].iloc[-13]) / df["close"].iloc[-13] * 100
                if direction == "LONG" and pct_1h_now > 2.0:
                    logger.info(f" FOMO BLOCK {ticker} | LONG + pct_1h={pct_1h_now:+.2f}% > +2%")
                    return df
                if direction == "SHORT" and pct_1h_now < -2.0:
                    logger.info(f" FOMO BLOCK {ticker} | SHORT + pct_1h={pct_1h_now:+.2f}% < -2%")
                    return df
            except Exception:
                pass
        
        # 2c. HTF Trend filter — 26.05.2026 анализ показал что Фунтик
        # лонговал в 1h/4h DOWN-тренды (JCT -3% 1h, MITO -5% 4h, ARPA -3.7% 4h, ...).
        # Все 4 сделки = stop_loss / fast_fail. Не торгуем против старшего тренда.
        try:
            htf_block = self._check_htf_trend(pair, direction)
            if htf_block:
                logger.info(f" HTF BLOCK {ticker} | {htf_block}")
                return df
        except Exception as e:
            logger.debug(f"HTF check error {ticker}: {e}")
        
        # 3. Lorentzian alignment check
        lorentzian_aligned = self.regime_engine.is_lorentzian_aligned(df, direction)
        
        # 3b. Detect setup mode (REVERSAL or CONTINUATION)
        setup_mode = self.regime_engine.detect_setup_mode(df, direction)

        # 3c. HTF continuation filter — мягкий порог уже применён в _check_htf_trend.
        # Дополнительно: если setup_mode=REVERSAL и 1h тренд между -1.5% и -3%
        # — разрешаем (разворот против умеренного тренда — это нормально).
        # Если setup_mode=CONTINUATION и 1h тренд < -1.5% — уже заблокировано выше.
        # Логируем для отладки.
        try:
            df_1h_check = self.dp.get_pair_dataframe(pair=pair, timeframe="1h")
            if df_1h_check is not None and len(df_1h_check) >= 6:
                past_c = df_1h_check["close"].iloc[-6]
                now_c = df_1h_check["close"].iloc[-1]
                pct_1h_c = (now_c - past_c) / past_c * 100 if past_c > 0 else 0
                logger.info(
                    f"{ticker} | setup={setup_mode} | 1h_trend={pct_1h_c:+.1f}% | "
                    f"direction={direction}"
                )
        except Exception:
            pass
        
        # 4. Compute all scores
        scores = self.signal_engine.compute_scores(
            df, signal, direction, lorentzian_aligned
        )
        
        # 5. Pre-filters (basic checks)
        last = df.iloc[-1]
        
        # Velocity check - must have momentum in our direction
        # ОСЛАБЛЕНО (04.06.2026): разрешаем слабый velocity против если есть absorption
        velocity = last["velocity"]
        absorption_score = last.get("absorption_score", 0) if hasattr(last, 'get') else last["absorption_score"]
        absorption_in_dir = (
            (direction == "LONG" and last["absorption_long"]) or
            (direction == "SHORT" and last["absorption_short"])
        )
        
        velocity_ok = (
            (direction == "LONG" and velocity > -0.5) or # было > 0, теперь > -0.5
            (direction == "SHORT" and velocity < 0.5) # было < 0, теперь < 0.5
        )
        
        # Allow reverse velocity if absorption is strong
        velocity_relaxed = (
            absorption_in_dir and absorption_score >= 2 and
            ((direction == "LONG" and velocity > -1.0) or # было -0.3, теперь -1.0
             (direction == "SHORT" and velocity < 1.0)) # было 0.3, теперь 1.0
        )
        
        if not velocity_ok and not velocity_relaxed:
            logger.info(f" VELOCITY BLOCK {ticker} | vel={velocity:.2f}")
            return df
        
        # Wick trap check (relaxed: 2x 3x)
        body = abs(last["close"] - last["open"])
        if body > 0.0001: # avoid div issues
            upper_wick = last["high"] - max(last["close"], last["open"])
            lower_wick = min(last["close"], last["open"]) - last["low"]
            
            # LONG: upper wick = selling pressure on top (rejection)
            # Block only if VERY strong rejection (3x body)
            if direction == "LONG" and upper_wick > body * 3:
                logger.info(f" WICK BLOCK {ticker} | upper_wick > 3x body")
                return df
            # SHORT: lower wick = buying pressure on bottom (rejection)
            if direction == "SHORT" and lower_wick > body * 3:
                logger.info(f" WICK BLOCK {ticker} | lower_wick > 3x body")
                return df
        
        # ═══════════════════════════════════════════════════════════════════
        # OI DIRECTION FILTER (07.06.2026)
        # Вход только при совпадении направления цены и OI:
        # - SHORT: цена падает И OI растёт (новые шорты открываются)
        # - LONG: цена растёт И OI растёт (новые лонги открываются)
        # ═══════════════════════════════════════════════════════════════════
        oi_change = signal.get('oi_change_pct', 0)
        price_change_1h = signal.get('price_change_pct', 0)
        
        if direction == "SHORT":
            # SHORT: нужен падающий рынок (price < 0) И растущий OI (oi > 0)
            if price_change_1h >= 0:
                logger.info(f" OI FILTER {ticker} | SHORT but price rising ({price_change_1h:+.1f}%)")
                return df
            if oi_change <= 0:
                logger.info(f" OI FILTER {ticker} | SHORT but OI falling ({oi_change:+.1f}%)")
                return df
            
            # ═══════════════════════════════════════════════════════════════════
            # OI TRAP FILTER (07.07.2026)
            # Проблема: OI +9% + price -13% = LONGS открываются на дне (bullish!)
            # Фунтик думал это bearish и зашёл в SHORT
            # РЕШЕНИЕ: Если цена сильно упала И OI растёт = longs buying the dip!
            # ═══════════════════════════════════════════════════════════════════
            if price_change_1h < -10.0 and oi_change > 0:
                logger.info(
                    f" OI TRAP {ticker} | SHORT but price={price_change_1h:+.1f}% (big drop) "
                    f"AND OI={oi_change:+.1f}% (LONGS buying the dip!)"
                )
                return df
        
        elif direction == "LONG":
            # LONG: нужен растущий рынок (price > 0) И растущий OI (oi > 0)
            if price_change_1h <= 0:
                logger.info(f" OI FILTER {ticker} | LONG but price falling ({price_change_1h:+.1f}%)")
                return df
            if oi_change <= 0:
                logger.info(f" OI FILTER {ticker} | LONG but OI falling ({oi_change:+.1f}%)")
                return df
        
        # ═══════════════════════════════════════════════════════════════════
        # FUNDING TRAP FILTER (09.06.2026)
        # Цель: определить КТО открывает позиции - лонги или шорты
        # Funding rate показывает доминирование одной стороны:
        # - Отрицательный funding (< -0.01%) = шорты доминируют
        # - Положительный funding (> +0.01%) = лонги доминируют
        #
        # LONG trap: цена растет, OI растет, НО funding отрицательный
        # OI растёт за счёт ШОРТОВ на вершине, не лонгов!
        # Умные деньги шортят хай, не лонгуют
        #
        # SHORT trap: цена падает, OI растет, НО funding положительный
        # OI растёт за счёт ЛОНГОВ на дне, не шортов!
        # Умные деньги лонгуют дно, не шортят
        # ═══════════════════════════════════════════════════════════════════
        funding = signal.get('funding_rate', 0)
        
        if direction == "LONG":
            # LONG вход: нужен положительный или нейтральный funding
            # Если funding < -0.01% много новых ШОРТОВ открывается = bad sign
            if funding < -0.01:
                logger.info(
                    f" FUNDING TRAP {ticker} | LONG but funding={funding:.4f} "
                    f"(shorts dominate, OI={oi_change:+.1f}% from SHORT positions)"
                )
                return df
        
        elif direction == "SHORT":
            # SHORT вход: нужен отрицательный или нейтральный funding
            # Если funding > +0.01% много новых ЛОНГОВ открывается = bad sign
            if funding > 0.01:
                logger.info(
                    f" FUNDING TRAP {ticker} | SHORT but funding={funding:.4f} "
                    f"(longs dominate, OI={oi_change:+.1f}% from LONG positions)"
                )
                return df
        
        # Impulse candle check
        impulse_size = body / last["open"] if last["open"] > 0 else 0
        if impulse_size > 0.025:
            logger.info(f" IMPULSE BLOCK {ticker} | body={impulse_size:.1%}")
            return df
        
        # =====================================================
        # PULLBACK CHECK - don't buy at top / sell at bottom
        # =====================================================
        # If signal direction is LONG, check that we are NOT entering at recent high
        # If signal direction is SHORT, check that we are NOT entering at recent low
        if len(df) >= 6:
            recent_high_5 = df["high"].iloc[-6:-1].max() # high of last 5 candles (excluding current)
            recent_low_5 = df["low"].iloc[-6:-1].min()
            
            if direction == "LONG":
                # Block if current close is at/above recent high (pump exhaustion)
                # Better: enter on pullback from recent high
                distance_from_high = (recent_high_5 - last["close"]) / recent_high_5 if recent_high_5 > 0 else 0
                if distance_from_high < 0.002: # Less than 0.2% pullback from high
                    logger.info(f" NO PULLBACK {ticker} | LONG at recent high (pullback={distance_from_high:.2%})")
                    return df
            
            elif direction == "SHORT":
                # Block if current close is at/below recent low (dump exhaustion)
                distance_from_low = (last["close"] - recent_low_5) / recent_low_5 if recent_low_5 > 0 else 0
                if distance_from_low < 0.002: # Less than 0.2% bounce from low
                    logger.info(f" NO PULLBACK {ticker} | SHORT at recent low (pullback={distance_from_low:.2%})")
                    return df
        
        # 2-candle confirmation
        if not self._has_confirmation(df, direction):
            logger.info(f" NO CONFIRMATION {ticker} | needs 2 candles")
            return df
        
        # 6. Log decision
        ai_pred = self.ai_edge.predict(df, signal, direction)
        edge_score = ai_pred['edge_score']
        ai_conf = ai_pred['confidence']
        
        logger.info(
            f"{ticker} | {direction} | MODE={setup_mode} | PHASE={phase} | "
            f"LOR_pred={int(last['lor_prediction'])} LOR_sig={int(last['lor_signal'])}({last['lor_strength']:.0%}) | "
            f"S={scores['scanner_score']:.1f} R={scores['regime_score']:.1f} "
            f"M={scores['micro_score']:.1f} T={scores['trigger_score']:.1f} | "
            f"TOTAL={scores['total_score']:.1f} CONF={scores['confidence']:.0%} | "
            f"AI_EDGE={edge_score:+.4f}"
        )
        
        # Optional AI Edge filter (disabled by default - model still weak)
        if not Config.AI_EDGE_LOG_ONLY:
            if edge_score < Config.AI_EDGE_THRESHOLD:
                logger.info(f" AI EDGE BLOCK {ticker} | edge={edge_score:.4f}")
                return df
        
        # 7. Classify entry by setup mode
        last_close = last["close"]
        scalper_buy = last.get("scalper_buy", 0) if hasattr(last, 'get') else last["scalper_buy"]
        scalper_sell = last.get("scalper_sell", 0) if hasattr(last, 'get') else last["scalper_sell"]
        absorption_long = last["absorption_long"]
        absorption_short = last["absorption_short"]
        absorption_score = last["absorption_score"]
        velocity = last["velocity"]
        
        # =====================================================
        # MODE A: REVERSAL SETUP
        # =====================================================
        if setup_mode == "REVERSAL":
            # ОСЛАБЛЕННЫЕ ФИЛЬТРЫ для большего количества сделок (04.06.2026)
            # Для DEX сигналов: сам паттерн уже качественный, не требуем жёсткого подтверждения
            
            absorption_in_dir = (
                (direction == "LONG" and absorption_long) or
                (direction == "SHORT" and absorption_short)
            )
            
            # Velocity should be reversing (any positive sign for LONG, negative for SHORT)
            velocity_reversing = (
                (direction == "LONG" and velocity > -0.5) or # было -0.2, теперь -0.5 (мягче)
                (direction == "SHORT" and velocity < 0.5) # было 0.2, теперь 0.5 (мягче)
            )
            
            # ОСЛАБЛЕНО: absorption не обязателен, но даёт бонус к score
            # if not absorption_in_dir:
            # logger.info(f" REVERSAL block {ticker} | no absorption_{direction.lower()}")
            # return df
            
            # ОСЛАБЛЕНО: минимальный absorption score снижен до 1 (было 2)
            if absorption_in_dir and absorption_score < 1:
                logger.info(f" REVERSAL block {ticker} | weak absorption ({absorption_score})")
                return df
            
            # ОСЛАБЛЕНО: velocity может быть против, если есть absorption
            if not velocity_reversing and not absorption_in_dir:
                logger.info(f" REVERSAL block {ticker} | velocity={velocity:.2f} + no absorption")
                return df
            
            # Score check for reversal mode
            # ОСЛАБЛЕНО: score min снижен с 6 до 4 (04.06.2026)
            score_min = 4 # было Config.REVERSAL_SCORE_MIN = 6
            if phase == "EARLY_EXPANSION":
                score_min += 1 # было +2, теперь +1 (мягче)
            if scores['total_score'] < score_min:
                logger.info(
                    f" REVERSAL low score {ticker} | "
                    f"{scores['total_score']:.1f} < {score_min} (phase={phase})"
                )
                return df
            
            entry_class = "REVERSAL"
        
        # =====================================================
        # MODE B: CONTINUATION SETUP
        # =====================================================
        # Требуем 2 из 3 родных индикаторов системы:
        # 1. Pro Scalper (scalper_buy/sell) — ITG/TEMA+MACD триггер
        # 2. Absorption Bubbles (absorption_long/short) — объёмная абсорбция
        # 3. Lorentzian (lor_signal совпадает с direction)
        elif setup_mode == "CONTINUATION":
            scalper_aligned = (
                (direction == "LONG" and scalper_buy) or
                (direction == "SHORT" and scalper_sell)
            )
            absorption_aligned = (
                (direction == "LONG" and absorption_long) or
                (direction == "SHORT" and absorption_short)
            )
            lor_aligned = (
                (direction == "LONG" and int(last.get("lor_signal", 0) if hasattr(last, 'get') else last["lor_signal"]) == 1) or
                (direction == "SHORT" and int(last.get("lor_signal", 0) if hasattr(last, 'get') else last["lor_signal"]) == -1)
            )
            
            confirmations = sum([scalper_aligned, absorption_aligned, lor_aligned])
            
            if confirmations < 2:
                logger.info(
                    f" CONTINUATION block {ticker} | "
                    f"{confirmations}/3 indicators "
                    f"(scalper={'' if scalper_aligned else ''} "
                    f"absorption={'' if absorption_aligned else ''} "
                    f"lor={'' if lor_aligned else ''})"
                )
                return df
            
            # Score check
            score_min = Config.CONTINUATION_SCORE_MIN
            if phase == "EARLY_EXPANSION":
                score_min += 2
            if scores['total_score'] < score_min:
                logger.info(
                    f" CONTINUATION low score {ticker} | "
                    f"{scores['total_score']:.1f} < {score_min} (phase={phase})"
                )
                return df
            
            # Если все 3 индикатора — ELITE, иначе NORMAL
            entry_class = "ELITE_CONT" if confirmations == 3 else "NORMAL_CONT"
        
        else:
            return df
        
        # 8. Set entry
        # Префикс источника: lg=LightGBM, tg=telegram, bs=bybit_scanner
        if entry_source == "lightgbm":
            source_prefix = "lg"
        elif entry_source == "bybit_scanner":
            source_prefix = "bs"
        elif entry_source == "pattern_match":
            source_prefix = "pm"
        else:
            source_prefix = "tg"
        tag = f"{source_prefix}_{entry_class.lower()}_{direction.lower()}_{scores['confidence']:.0%}"
        
        if direction == "LONG":
            df.loc[df.index[-1], "enter_long"] = 1
            df.loc[df.index[-1], "enter_tag"] = tag
            logger.info(f" [{entry_source.upper()}] {entry_class} LONG {ticker} | mode={setup_mode} | score={scores['total_score']:.1f}")
        elif direction == "SHORT":
            df.loc[df.index[-1], "enter_short"] = 1
            df.loc[df.index[-1], "enter_tag"] = tag
            logger.info(f" [{entry_source.upper()}] {entry_class} SHORT {ticker} | mode={setup_mode} | score={scores['total_score']:.1f}")
        
        # 10. Store features for snapshot
        self._current_scores[pair] = {
            **scores,
            'direction': direction,
            'entry_source': entry_source, # telegram / bybit_scanner / lightgbm
            'setup_mode': setup_mode,
            'volatility': last["volatility"],
            'volume_spike': last["volume_spike"],
            'absorption_score': last["absorption_score"],
            'lor_signal': int(last["lor_signal"]),
            'lor_strength': float(last["lor_strength"]),
            'lor_prediction': int(last["lor_prediction"]),
            'market_phase': phase,
            'price_entry': last["close"],
            'volume_slope': self._volume_slope(df),
            'delta_slope': self._delta_slope(df),
            'distance_from_ema20': (last["close"] - last["ema_fast"]) / last["ema_fast"] if last["ema_fast"] > 0 else 0,
            'distance_from_vwap': (last["close"] - last["vwap"]) / last["vwap"] if last["vwap"] > 0 else 0,
            'ai_edge_score': edge_score,
            # Индикаторы для обучения модели (02.09.2026)
            'rsi': float(last.get("rsi_14", 50)) if "rsi_14" in last else 50.0,
            'vw_macd': float(last.get("vw_macd", 0)) if "vw_macd" in last else 0.0,
            'ema_trend': self._get_ema_trend(last),
            'vwap_value': float(last.get("vwap", last["close"])),
            'macd_value': float(last.get("macd", 0)) if "macd" in last else 0.0,
            'macd_signal': float(last.get("macdsignal", 0)) if "macdsignal" in last else 0.0,
            'macd_hist': float(last.get("macdhist", 0)) if "macdhist" in last else 0.0,
        }
        
        return df
    
    def _has_confirmation(self, df: DataFrame, direction: str) -> bool:
        """
        Check confirmation for entry (relaxed):
        - Last candle in direction OR
        - Strong reversal pattern OR
        - Previous + last candle showing momentum
        """
        if len(df) < 3:
            return True # not enough data, allow
        
        curr_close = df["close"].iloc[-1]
        prev_close = df["close"].iloc[-2]
        curr_open = df["open"].iloc[-1]
        prev_open = df["open"].iloc[-2]
        last_low = df["low"].iloc[-1]
        last_high = df["high"].iloc[-1]
        
        if direction == "LONG":
            last_green = curr_close > curr_open
            prev_green = prev_close > prev_open
            
            # Strong reversal: bullish hammer/pinbar
            body_curr = abs(curr_close - curr_open)
            lower_wick = min(curr_close, curr_open) - last_low
            strong_reversal = (
                lower_wick > body_curr * 1.5 # Big lower wick
                and last_green # Closed green
            )
            
            # Higher close than previous = momentum
            higher_close = curr_close > prev_close
            
            # Allow if: 1 green candle + higher close, OR strong reversal, OR 2 green
            return (last_green and higher_close) or strong_reversal or (last_green and prev_green)
        
        elif direction == "SHORT":
            last_red = curr_close < curr_open
            prev_red = prev_close < prev_open
            
            # Strong reversal: bearish shooting star/pinbar
            body_curr = abs(curr_close - curr_open)
            upper_wick = last_high - max(curr_close, curr_open)
            strong_reversal = (
                upper_wick > body_curr * 1.5 # Big upper wick
                and last_red # Closed red
            )
            
            lower_close = curr_close < prev_close
            
            return (last_red and lower_close) or strong_reversal or (last_red and prev_red)
        
        return True
    
    def _volume_slope(self, df: DataFrame) -> float:
        """Volume change over last 5 candles"""
        if len(df) < 5:
            return 0
        v5 = df["volume"].iloc[-5]
        return (df["volume"].iloc[-1] - v5) / v5 if v5 > 0 else 0

    def _get_open_position_direction(self, pair: str) -> Optional[str]:
        """Возвращает 'LONG'/'SHORT' если по паре есть открытая позиция, иначе None."""
        try:
            for t in Trade.get_open_trades():
                if t.pair == pair:
                    return "SHORT" if t.is_short else "LONG"
        except Exception as e:
            logger.debug(f"_get_open_position_direction error {pair}: {e}")
        return None

    def _get_htf_trend_1h_pct(self, pair: str) -> float:
        """
        Числовое значение 1h-тренда (% изменения за последние 6 часовых свечей).
        Используется как ФИЧА для LightGBM (10.07.2026), а не как хардкод-блок —
        модель сама учится взвешивать тренд вместо жёсткого порога.
        """
        try:
            df_1h = self.dp.get_pair_dataframe(pair=pair, timeframe="1h")
            if df_1h is None or len(df_1h) < 6:
                return 0.0
            past = df_1h["close"].iloc[-6]
            now = df_1h["close"].iloc[-1]
            return float((now - past) / past * 100) if past > 0 else 0.0
        except Exception:
            return 0.0
    
    def _check_htf_trend(self, pair: str, direction: str) -> Optional[str]:
        """
        Higher-timeframe trend filter — раздельные пороги для REVERSAL и CONTINUATION.

        CONTINUATION LONG в падающем рынке — главная причина убытков:
          bs_normal_cont_long_92%: WR=38%, avg=-0.90%, total=-81.74 USDT за 7 дней.
          Данные: 28.05.2026 WR=30%, -74.64 USDT — рынок падал, фунтик лонговал.

        Пороги:
          CONTINUATION LONG: блок если 1h тренд < -1.5% (было -3.0%)
          CONTINUATION SHORT: блок если 1h тренд > +1.5%
          REVERSAL: блок только при экстремальных -3% / +3% (разворот допустим)
        """
        try:
            df_1h = self.dp.get_pair_dataframe(pair=pair, timeframe="1h")
        except Exception:
            df_1h = None

        if df_1h is None or len(df_1h) < 6:
            return None

        past = df_1h["close"].iloc[-6]
        now = df_1h["close"].iloc[-1]
        pct_1h_trend = (now - past) / past * 100 if past > 0 else 0

        # Определяем setup_mode из _current_scores (если есть) или из контекста
        # На этапе входа setup_mode ещё не определён — используем сигнал
        # Поэтому проверяем оба порога: мягкий для continuation, жёсткий для reversal
        # Continuation блокируется раньше (слабее против тренда)
        cont_long_block = Config.HTF_CONT_LONG_BLOCK_PCT # -1.5%
        cont_short_block = Config.HTF_CONT_SHORT_BLOCK_PCT # +1.5%
        rev_block = 3.0 # reversal допускает более сильный контртренд

        if direction == "LONG":
            if pct_1h_trend < -rev_block:
                return f"1h trend {pct_1h_trend:+.1f}% (LONG vs strong down, reversal too)"
            if pct_1h_trend < cont_long_block:
                # Блокируем только continuation, reversal пропускаем
                # Но на этапе _check_htf_trend setup_mode ещё неизвестен —
                # блокируем continuation через отдельную проверку в populate_entry_trend
                return f"1h trend {pct_1h_trend:+.1f}% (LONG cont vs down trend)"

        if direction == "SHORT":
            if pct_1h_trend > rev_block:
                return f"1h trend {pct_1h_trend:+.1f}% (SHORT vs strong up, reversal too)"
            if pct_1h_trend > cont_short_block:
                return f"1h trend {pct_1h_trend:+.1f}% (SHORT cont vs up trend)"

        return None
    
    def _delta_slope(self, df: DataFrame) -> float:
        """Delta change over last 5 candles"""
        if len(df) < 5:
            return 0
        d5 = df["delta"].iloc[-5]
        return (df["delta"].iloc[-1] - d5) / (abs(d5) + 0.0001)
    
    def _get_ema_trend(self, last: dict) -> str:
        """Определить EMA тренд: bullish/bearish/neutral"""
        try:
            ema_fast = float(last.get("ema_fast", 0))
            ema_slow = float(last.get("ema_slow", 0))
            
            if ema_fast == 0 or ema_slow == 0:
                return "neutral"
            
            diff_pct = (ema_fast - ema_slow) / ema_slow * 100
            
            if diff_pct > 0.5:
                return "bullish"
            elif diff_pct < -0.5:
                return "bearish"
            else:
                return "neutral"
        except:
            return "neutral"
    
    # ============================================================
    # EXIT
    # ============================================================
    
    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["exit_long"] = 0
        df["exit_short"] = 0
        return df
    
    # ============================================================
    # CONFIRM ENTRY (save snapshot)
    # ============================================================
    
    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                           rate: float, time_in_force: str, current_time,
                           entry_tag: str, side: str, **kwargs) -> bool:
        """Save snapshot when trade is confirmed
        
        FIX (31.07.2026): 🔥 HTF HARD FILTER BACKUP LAYER 🔥
        Защита от scanner bugs - если сканер пропустил сигнал против HTF тренда,
        стратегия заблокирует вход!
        
        Проблема: CTC LONG HTF=-4.34% прошёл сканер (ошибка импорта lightgbm) → убыток!
        Решение: Двойная проверка HTF в стратегии как последняя линия защиты.
        """
        scores = self._current_scores.get(pair, {})
        
        # 🔥 HTF HARD FILTER BACKUP (31.07.2026) 🔥
        # Блокируем вход против сильного HTF тренда
        # 🔥 FIX (24.09.2026 00:15): Ослаблены пороги с ±0.5% до ±6%
        # ПРИЧИНА: Слишком жесткие фильтры блокируют все сигналы
        # РЕШЕНИЕ: Блокируем только при ОЧЕНЬ сильном противотренде
        htf_1h = scores.get("htf_trend_1h", 0)
        direction = side.upper()
        
        # Блокировка LONG против bearish HTF
        if direction == "LONG" and htf_1h < -6.0:
            audit(
                "BLOCK_HTF", pair, direction=direction, entry_tag=entry_tag or "",
                rate=f"{rate:.8f}", htf_1h=f"{htf_1h:.2f}%",
                reason="HTF bearish trend blocks LONG"
            )
            logger.warning(f"🛑 HTF FILTER BLOCK: {pair} LONG против HTF={htf_1h:.2f}% (bearish)")
            return False
        
        # Блокировка SHORT против bullish HTF
        if direction == "SHORT" and htf_1h > +6.0:
            audit(
                "BLOCK_HTF", pair, direction=direction, entry_tag=entry_tag or "",
                rate=f"{rate:.8f}", htf_1h=f"{htf_1h:.2f}%",
                reason="HTF bullish trend blocks SHORT"
            )
            logger.warning(f"🛑 HTF FILTER BLOCK: {pair} SHORT против HTF={htf_1h:.2f}% (bullish)")
            return False
        
        audit(
            "ENTRY", pair, direction=direction, entry_tag=entry_tag or "",
            rate=f"{rate:.8f}", signal_ts=scores.get("signal_ts", "?"),
        )
        if not scores:
            return True
        
        trade_id = f"{pair}_{current_time.timestamp()}"
        scores["trade_id"] = trade_id
        scores["price_entry"] = rate
        
        try:
            conn = sqlite3.connect(Config.SNAPSHOTS_DB)
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO trade_snapshots (
                    trade_id, timestamp, pair, direction,
                    scanner_score, regime_score, micro_score, trigger_score,
                    confidence, total_score, price_entry,
                    volatility, volume_spike, absorption_score,
                    lor_signal, lor_strength, lor_prediction,
                    market_phase, volume_slope, delta_slope,
                    distance_from_ema20, distance_from_vwap,
                    rsi, htf_trend_1h,
                    vw_macd, ema_trend, vwap_value,
                    macd_value, macd_signal, macd_hist,
                    created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade_id, current_time.isoformat(), pair,
                "LONG" if side == "long" else "SHORT",
                scores.get("scanner_score", 0),
                scores.get("regime_score", 0),
                scores.get("micro_score", 0),
                scores.get("trigger_score", 0),
                scores.get("confidence", 0),
                scores.get("total_score", 0),
                rate,
                scores.get("volatility", 0),
                scores.get("volume_spike", 0),
                scores.get("absorption_score", 0),
                scores.get("lor_signal", 0),
                scores.get("lor_strength", 0),
                scores.get("lor_prediction", 0),
                scores.get("market_phase", ""),
                scores.get("volume_slope", 0),
                scores.get("delta_slope", 0),
                scores.get("distance_from_ema20", 0),
                scores.get("distance_from_vwap", 0),
                scores.get("rsi", 50),
                scores.get("htf_trend_1h", 0),
                scores.get("vw_macd", 0),
                scores.get("ema_trend", "neutral"),
                scores.get("vwap_value", rate),
                scores.get("macd_value", 0),
                scores.get("macd_signal", 0),
                scores.get("macd_hist", 0),
                datetime.now().isoformat(),
            ))
            conn.commit()
            conn.close()
            
            logger.info(f" SNAPSHOT SAVED {pair} | total={scores.get('total_score', 0):.1f}")
        except Exception as e:
            logger.error(f"Snapshot save error: {e}")
        
        # ============================================================
        # ntfy уведомление о входе в сделку
        # Только для монет которые есть в DEX watchlist (можно торговать руками)
        # ============================================================
        try:
            self._send_entry_ntfy(pair, side, rate, entry_tag, scores)
        except Exception as e:
            logger.error(f"ntfy entry notification error: {e}")
        
        return True
    
    def _send_entry_ntfy(self, pair: str, side: str, rate: float,
                         entry_tag: str, scores: dict) -> None:
        """
        Отправить ntfy push о входе в сделку.
        Шлём только если монета есть в DEX watchlist (т.е. её можно
        реально торговать руками на DEX).
        """
        import os
        import requests
        
        # 1. Загружаем DEX watchlist
        dex_watchlist_path = Path("/home/max/o_p/dex_scanner/data/watchlist.json")
        if not dex_watchlist_path.exists():
            return
        
        try:
            data = json.loads(dex_watchlist_path.read_text())
            dex_bases = {p["base"] for p in data.get("pairs", [])}
        except Exception:
            return
        
        # 2. Проверяем монету
        # "1000PEPE/USDT:USDT" base "PEPE"
        ticker = pair.split("/")[0]
        base = ticker[4:] if ticker.startswith("1000") else ticker
        if base not in dex_bases:
            logger.info(f" {ticker}: not in DEX watchlist, skip ntfy")
            return
        
        # 3. Загружаем .env с настройками ntfy
        from dotenv import load_dotenv
        load_dotenv("/home/max/o_p/.env")
        ntfy_topic = os.getenv("NTFY_TOPIC", "max-alerts")
        ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh")
        if not ntfy_topic:
            return
        
        # 4. Формируем сообщение
        direction = "LONG" if side == "long" else "SHORT"
        emoji = "" if direction == "LONG" else ""
        entry_source = scores.get("entry_source", "?")
        setup_mode = scores.get("setup_mode", "?")
        phase = scores.get("market_phase", "?")
        total_score = scores.get("total_score", 0)
        confidence = scores.get("confidence", 0)
        
        title = f"{emoji} Funtik {direction} {base} | {entry_source}"
        body = (
            f"price: ${rate:.6f}\n"
            f"tag: {entry_tag}\n"
            f"mode: {setup_mode} | phase: {phase}\n"
            f"score: {total_score:.1f} | conf: {confidence:.0%}\n"
            f" есть на DEX (можно руками)"
        )
        
        # 5. Шлём
        try:
            r = requests.post(
                f"{ntfy_server}/{ntfy_topic}",
                data=body.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8"),
                    "Priority": "4",
                    "Tags": "rocket" if direction == "LONG" else "broken_heart",
                },
                timeout=10,
            )
            if r.status_code == 200:
                logger.info(f" ntfy entry sent: {base} {direction}")
            else:
                logger.warning(f"ntfy http {r.status_code}: {r.text[:100]}")
        except Exception as e:
            logger.error(f"ntfy request failed: {e}")
    
    # ============================================================
    # CONFIRM TRADE EXIT - Update snapshot result for ML training
    # ============================================================
    
    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float,
                          rate: float, time_in_force: str, exit_reason: str,
                          current_time, **kwargs) -> bool:
        """
        Записывает результат сделки в trade_snapshots.
        Это критично для обучения ML — без этого result остаётся NULL,
        и активный сканер не может найти WIN-шаблоны.
        """
        try:
            scores = self._current_scores.get(pair, {})
            trade_id = scores.get("trade_id")
            
            # Если по какой-то причине нет trade_id в памяти — реконструируем
            if not trade_id:
                trade_id = f"{pair}_{trade.open_date_utc.timestamp()}"
            
            # Считаем PnL
            pnl_ratio = trade.calc_profit_ratio(rate) if hasattr(trade, "calc_profit_ratio") else 0.0
            
            # Определяем direction из trade
            direction = "SHORT" if trade.is_short else "LONG"
            
            audit(
                "EXIT", pair, direction=direction, exit_reason=exit_reason, pnl=f"{pnl_ratio:.2%}",
                signal_ts=scores.get("signal_ts", "?"),
            )
            
            # Определяем результат
            if pnl_ratio > 0.005:
                result = "win"
            elif pnl_ratio < -0.005:
                result = "loss"
            else:
                result = "breakeven"
            
            hold_time = int((current_time - trade.open_date_utc).total_seconds() / 60)
            
            # max_profit / max_drawdown из freqtrade trade object
            max_profit = getattr(trade, "max_rate", None)
            if max_profit and trade.open_rate:
                if trade.is_short:
                    max_profit = (trade.open_rate - max_profit) / trade.open_rate
                else:
                    max_profit = (max_profit - trade.open_rate) / trade.open_rate
            else:
                max_profit = pnl_ratio
            
            max_drawdown = getattr(trade, "min_rate", None)
            if max_drawdown and trade.open_rate:
                if trade.is_short:
                    max_drawdown = (trade.open_rate - max_drawdown) / trade.open_rate
                else:
                    max_drawdown = (max_drawdown - trade.open_rate) / trade.open_rate
            else:
                max_drawdown = pnl_ratio
            
            conn = sqlite3.connect(Config.SNAPSHOTS_DB)
            cur = conn.cursor()
            cur.execute("""
                UPDATE trade_snapshots SET
                    result = ?,
                    max_profit = ?,
                    max_drawdown = ?,
                    hold_time = ?,
                    profit_ratio = ?,
                    updated_at = ?
                WHERE trade_id = ?
            """, (result, max_profit, max_drawdown, hold_time, pnl_ratio,
                  datetime.now().isoformat(), trade_id))
            conn.commit()
            conn.close()
            
            logger.info(
                f" SNAPSHOT UPDATED {pair} | result={result} | "
                f"pnl={pnl_ratio:.2%} | mfe={max_profit:.2%} | hold={hold_time}m"
            )
            
            # ═══════════════════════════════════════════════════════════════
            # UPDATE COOLDOWN для убыточных сделок (09.06.2026)
            # При убытке записываем timestamp чтобы заблокировать повторный вход
            # ═══════════════════════════════════════════════════════════════
            if result == "loss":
                self._update_pair_cooldown(pair)
                logger.info(f" COOLDOWN SET {pair.split('/')[0]} | 3 hours after loss")
            
            # Триггерим reload шаблонов в активном сканере при WIN
            if result == "win":
                try:
                    from active_scanner import get_scanner
                    get_scanner().reload_templates()
                except Exception:
                    pass
            
            # Очищаем _current_scores чтобы не утекало
            self._current_scores.pop(pair, None)
            
        except Exception as e:
            logger.error(f"Snapshot update error for {pair}: {e}")
        
        return True
    
    # ============================================================
    # CUSTOM EXIT
    # ============================================================
    
    def custom_stoploss(self, pair: str, trade, current_time, current_rate: float,
                       current_profit: float, after_fill: bool, **kwargs) -> Optional[float]:
        """
        ДИНАМИЧЕСКИЙ МНОГОУРОВНЕВЫЙ СТОП (ПЕРЕРАБОТАН 07.07.2026):
        
         КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: ОТКЛЮЧЕН TRAILING ДО ПЕРВОГО TP
        
        ПРОБЛЕМА: 65.5% сделок закрывались через trailing_stop_loss на +0.5-0.8%,
        не давая дорасти до целевых +1.5-3%.
        
        РЕШЕНИЕ: НИКАКОГО TRAILING до достижения TP1 и выполнения partial exit.
        
        НОВАЯ ЛОГИКА:
        0. profit < 0 AND age < 5min: ATR * 3.0 (защита от фитилей)
        1. profit < TP1 (+1.5%/+1.8%): ТОЛЬКО breakeven или hard stop, БЕЗ TRAILING
        2. profit >= TP1 но partial TP не выполнен: ЖДЁМ adjust_trade_position
        3. profit >= TP1 И partial exit выполнен: ВКЛЮЧАЕМ trailing
           - profit >= +3%: trail 0.8%
           - profit >= +2%: trail 1.5%
           - profit >= TP1: trail 2.5%
        4. После 2х partial TP (runner 40%): tight trail 1.2%
        
        MFE/MAE TRACKING: Логируем максимальный профит для анализа
        """
        scores = self._current_scores.get(pair, {})
        setup_mode = scores.get('setup_mode')
        if not setup_mode:
            tag = (trade.enter_tag or "").lower()
            setup_mode = "REVERSAL" if "reversal" in tag else "CONTINUATION"

        # Определяем целевой TP1 для данного режима
        tp1_target = Config.REVERSAL_TP1 if setup_mode == 'REVERSAL' else Config.CONTINUATION_TP1
        be_trigger = Config.REVERSAL_BE if setup_mode == 'REVERSAL' else Config.CONTINUATION_BE
        
        # Время в сделке
        trade_duration_sec = (current_time - trade.open_date_utc).total_seconds()
        trade_duration_min = trade_duration_sec / 60

        # ═══════════════════════════════════════════════════════════════════
        # MFE/MAE TRACKING (NEW 07.07.2026)
        # Отслеживаем максимальный профит для анализа drawdown
        # ═══════════════════════════════════════════════════════════════════
        if not hasattr(trade, 'max_profit_seen'):
            trade.max_profit_seen = current_profit
        else:
            if current_profit > trade.max_profit_seen:
                trade.max_profit_seen = current_profit
        
        drawdown_from_peak = trade.max_profit_seen - current_profit
        
        # Логируем каждые 2 минуты или при значительных изменениях
        if (int(trade_duration_min) % 2 == 0 or
            drawdown_from_peak > 0.005 or
            current_profit >= tp1_target):
            logger.info(
                f" MFE/MAE {pair.split('/')[0]} | age={trade_duration_min:.0f}m | "
                f"profit={current_profit:.2%} | MFE={trade.max_profit_seen:.2%} | "
                f"DD={drawdown_from_peak:.2%} | mode={setup_mode} | "
                f"exits={trade.nr_of_successful_exits}"
            )

        # Fixed hard stop from entry: never widen the downside limit with ATR.
        if current_profit < 0:
            hard_stop = stoploss_from_open(
                Config.HARD_STOPLOSS,
                current_profit,
                is_short=trade.is_short,
                leverage=getattr(trade, "leverage", 1.0),
            )
            logger.debug(
                f" HARD STOP {pair} | profit={current_profit:.2%} | "
                f"open_stop={Config.HARD_STOPLOSS:.2%}"
            )
            return hard_stop
        # ═══════════════════════════════════════════════════════════════════
        # ГЛАВНОЕ ИЗМЕНЕНИЕ (07.07.2026): NO TRAILING BEFORE TP1
        # ═══════════════════════════════════════════════════════════════════
        
        # ФАЗА 1: Profit < TP1 — АГРЕССИВНЫЙ ТРЕЙЛИНГ (23.07.2026)
        # ПРОБЛЕМА: MFE analysis показывает что сделки доходят до +1.5-3%, но
        # trailing отдаёт прибыль. STG: MFE +1.77%, закрыто -1.07% (слито 2.8%!).
        # РЕШЕНИЕ: Агрессивный трейлинг для малых профитов:
        # +0.5% → SL на breakeven
        # +0.8% → SL на +0.3% (фиксируем что-то)
        # +1.0% → SL на +0.5%
        # TP1 → MFE trail (старая логика)
        if current_profit < tp1_target:
            if current_profit >= 0.010:  # +1.0%
                lock_profit = 0.005  # SL на +0.5%
                distance = max(current_profit - lock_profit, 0.001)
                logger.debug(
                    f" AGGRESSIVE TRAIL {pair} | profit={current_profit:.2%} | "
                    f"lock={lock_profit:.2%} | mode={setup_mode}"
                )
                return distance
            elif current_profit >= 0.008:  # +0.8%
                lock_profit = 0.003  # SL на +0.3%
                distance = max(current_profit - lock_profit, 0.001)
                logger.debug(
                    f" AGGRESSIVE TRAIL {pair} | profit={current_profit:.2%} | "
                    f"lock={lock_profit:.2%} | mode={setup_mode}"
                )
                return distance
            elif current_profit >= 0.005:  # +0.5%
                # Breakeven
                distance = max(current_profit - 0.001, 0.001)
                logger.debug(
                    f" AGGRESSIVE TRAIL {pair} | profit={current_profit:.2%} | "
                    f"lock=breakeven | mode={setup_mode}"
                )
                return distance
            elif current_profit >= be_trigger:
                # Старая логика MFE trail
                mfe = getattr(trade, "max_profit_seen", current_profit)
                lock_buffer = 0.006
                target_lock_profit = max(mfe - lock_buffer, 0.002)
                distance = max(current_profit - target_lock_profit, 0.001)
                logger.debug(
                    f" MFE TRAIL {pair} | profit={current_profit:.2%} | MFE={mfe:.2%} | "
                    f"mode={setup_mode} | lock_target={target_lock_profit:.2%}"
                )
                return distance
            
            # До breakeven - только hard stoploss
            logger.debug(
                f" NO TRAILING {pair} | profit={current_profit:.2%} < TP1 {tp1_target:.2%} | "
                f"using hard stoploss only"
            )
            return None
        
        # ФАЗА 2: TP1 достигнут, но partial exit ещё не выполнен
        # Даём adjust_trade_position() сработать, не мешаем trailing'ом
        if trade.nr_of_successful_exits == 0:
            logger.debug(
                f" WAITING PARTIAL TP1 {pair} | profit={current_profit:.2%} | "
                f"TP1 {tp1_target:.2%} reached, waiting for adjust_trade_position"
            )
            return None
        
        # ═══════════════════════════════════════════════════════════════════
        # ФАЗА 3: Partial TP выполнен ВКЛЮЧАЕМ TRAILING
        # ═══════════════════════════════════════════════════════════════════
        if trade.nr_of_successful_exits >= 2:
            trailing_offset = Config.TRAILING_AFTER_PARTIAL_OFFSET
            new_stoploss = -trailing_offset
            logger.debug(
                f" RUNNER TRAIL {pair} | after {trade.nr_of_successful_exits}x exit | "
                f"profit={current_profit:.2%} | trail={trailing_offset:.1%}"
            )
            return new_stoploss

        # Уровень 1: profit >= +1.68% (было +2.1%, -20% 23.07.2026) tight trail 0.8%
        if current_profit >= 0.0168:
            new_stoploss = -0.008
            logger.debug(
                f" TIGHT TRAIL L1 {pair} | profit={current_profit:.2%} | "
                f"trail=0.8% | exits={trade.nr_of_successful_exits}"
            )
            return new_stoploss
        
        # Уровень 2: profit >= +1.12% (было +1.4%, -20% 23.07.2026) trail 1.5%
        if current_profit >= 0.0112:
            new_stoploss = -0.015
            logger.debug(
                f" TRAIL L2 {pair} | profit={current_profit:.2%} | "
                f"trail=1.5% | exits={trade.nr_of_successful_exits}"
            )
            return new_stoploss
        
        # Уровень 3: profit >= TP1 trail 2.5%
        if current_profit >= tp1_target:
            new_stoploss = -0.025
            logger.debug(
                f" TRAIL L3 {pair} | profit={current_profit:.2%} | "
                f"trail=2.5% | exits={trade.nr_of_successful_exits}"
            )
            return new_stoploss

        # Fallback: hard stoploss
        return None
    
    def adjust_trade_position(self, trade, current_time, current_rate: float,
                              current_profit: float, min_stake, max_stake,
                              current_entry_rate: float, current_exit_rate: float,
                              current_entry_profit: float, current_exit_profit: float,
                              **kwargs) -> Optional[float]:
        """
        TWO-STAGE PARTIAL TP (NEW 06.07.2026):
        
        Цель: Держать позицию дольше, ловить большие движения
        
        Логика:
          TP1: Закрыть 30% при достижении первой цели
          TP2: Закрыть ещё 30% при достижении второй цели
          Runner: Остаток 40% идёт дальше с плотным trailing
        
        REVERSAL:
          TP1: 30% @ +1.5%
          TP2: 30% @ +2.5%
          Runner: 40% может словить +3-5%
        
        CONTINUATION:
          TP1: 30% @ +1.8%
          TP2: 30% @ +3.0%
          Runner: 40% может словить +4-8%
        
        Было: 50% @ +1.0%/+1.2%
        Стало: 30% + 30% + 40% runner
        """
        scores = self._current_scores.get(trade.pair, {})
        setup_mode = scores.get('setup_mode')
        if not setup_mode:
            tag = (trade.enter_tag or "").lower()
            setup_mode = "REVERSAL" if "reversal" in tag else "CONTINUATION"
        
        # Количество уже выполненных частичных выходов
        exits_done = trade.nr_of_successful_exits
        
        # Определяем цели и размеры позиций для каждого режима
        if setup_mode == 'REVERSAL':
            tp1_target = Config.REVERSAL_TP1 # +1.5%
            tp1_ratio = Config.REVERSAL_TP1_RATIO # 30%
            tp2_target = Config.REVERSAL_TP2 # +2.5%
            tp2_ratio = Config.REVERSAL_TP2_RATIO # 30%
        else: # CONTINUATION
            tp1_target = Config.CONTINUATION_TP1 # +1.8%
            tp1_ratio = Config.CONTINUATION_TP1_RATIO # 30%
            tp2_target = Config.CONTINUATION_TP2 # +3.0%
            tp2_ratio = Config.CONTINUATION_TP2_RATIO # 30%
        
        # TP1: Первое частичное закрытие (30% от начальной позиции)
        if exits_done == 0 and current_profit >= tp1_target:
            # Используем текущий stake_amount (который = начальному на первом exit)
            stake_to_close = trade.stake_amount * tp1_ratio
            logger.info(
                f"📊 PARTIAL TP1 {trade.pair} | mode={setup_mode} | "
                f"profit={current_profit:.2%} | target={tp1_target:.1%} | "
                f"closing {tp1_ratio:.0%} of initial = {stake_to_close:.2f} USDT"
            )
            return -stake_to_close
        
        # TP2: Второе частичное закрытие (ещё 30% от начальной, = 42.86% от оставшейся)
        if exits_done == 1 and current_profit >= tp2_target:
            # ИСПРАВЛЕНО (28.07.2026): tp2_ratio нужно пересчитать от оставшейся позиции
            # После TP1 закрыто 30%, осталось 70%.
            # Чтобы закрыть ещё 30% от начальной = закрыть 30/70 = 42.86% от оставшейся
            remaining_after_tp1 = 1.0 - tp1_ratio  # 0.70 (70% осталось)
            tp2_from_remaining = tp2_ratio / remaining_after_tp1  # 0.30 / 0.70 = 0.4286
            stake_to_close = trade.stake_amount * tp2_from_remaining
            logger.info(
                f"📊 PARTIAL TP2 {trade.pair} | mode={setup_mode} | "
                f"profit={current_profit:.2%} | target={tp2_target:.1%} | "
                f"closing {tp2_ratio:.0%} of initial = {tp2_from_remaining:.1%} of remaining = {stake_to_close:.2f} USDT | "
                f"runner {1.0 - tp1_ratio - tp2_ratio:.0%} continues"
            )
            return -stake_to_close
        
        # Runner (40%) продолжает с плотным trailing из custom_stoploss
        return None
    
    def custom_exit(self, pair: str, trade, current_time, current_rate: float,
                   current_profit: float, **kwargs) -> Optional[str]:
        """Exit logic via RiskEngine"""
        if current_profit <= Config.HARD_STOPLOSS:
            logger.warning(
                f"🚨 HARD STOP EXIT {pair} | pnl={current_profit:.2%} | "
                f"limit={Config.HARD_STOPLOSS:.2%}"
            )
            return "hard_stoploss_emergency"

        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        
        if len(df) < 2:
            return None
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        is_long = not trade.is_short
        trade_duration = (current_time - trade.open_date_utc).total_seconds() / 60
        
        # ═══════════════════════════════════════════════════════════════════
        # SPIKE CANDLE EMERGENCY EXIT (03.08.2026)
        # Проблема: ACX SHORT -49 USDT - огромная зелёная свеча вверх
        # Пример: 03:50 свеча +5.98%, 04:00 свеча +13.65%
        # Решение: Проверяем ТЕКУЩУЮ и ПРЕДЫДУЩУЮ свечи на spike против нас
        # ═══════════════════════════════════════════════════════════════════
        
        # Проверяем ТЕКУЩУЮ свечу
        current_candle_size = abs(last["close"] - last["open"]) / last["open"] if last["open"] > 0 else 0
        current_candle_direction = last["close"] - last["open"]
        volume_spike_current = float(last.get("volume_spike", 1))
        
        # Проверяем ПРЕДЫДУЩУЮ свечу (быстрее реагируем!)
        prev_candle_size = abs(prev["close"] - prev["open"]) / prev["open"] if prev["open"] > 0 else 0
        prev_candle_direction = prev["close"] - prev["open"]
        volume_spike_prev = float(prev.get("volume_spike", 1))
        
        # LONG в сделке, но ОГРОМНАЯ красная свеча (>4%) с объёмом
        if is_long:
            # Текущая свеча spike
            if current_candle_direction < 0 and current_candle_size > 0.04 and volume_spike_current > 1.5:
                logger.warning(
                    f"🚨 SPIKE EXIT {pair} | LONG но RED свеча {current_candle_size:.1%} | "
                    f"vol×{volume_spike_current:.1f} | EMERGENCY!"
                )
                return "spike_candle_emergency"
            # Предыдущая свеча spike (реагируем быстрее!)
            if prev_candle_direction < 0 and prev_candle_size > 0.05 and volume_spike_prev > 1.5:
                logger.warning(
                    f"🚨 SPIKE EXIT {pair} | LONG но PREV RED свеча {prev_candle_size:.1%} | "
                    f"vol×{volume_spike_prev:.1f} | EMERGENCY!"
                )
                return "spike_candle_emergency"
        
        # SHORT в сделке, но ОГРОМНАЯ зелёная свеча (>4%) с объёмом
        if not is_long:
            # Текущая свеча spike
            if current_candle_direction > 0 and current_candle_size > 0.04 and volume_spike_current > 1.5:
                logger.warning(
                    f"🚨 SPIKE EXIT {pair} | SHORT но GREEN свеча {current_candle_size:.1%} | "
                    f"vol×{volume_spike_current:.1f} | EMERGENCY!"
                )
                return "spike_candle_emergency"
            # Предыдущая свеча spike (реагируем быстрее!)
            if prev_candle_direction > 0 and prev_candle_size > 0.05 and volume_spike_prev > 1.5:
                logger.warning(
                    f"🚨 SPIKE EXIT {pair} | SHORT но PREV GREEN свеча {prev_candle_size:.1%} | "
                    f"vol×{volume_spike_prev:.1f} | EMERGENCY!"
                )
                return "spike_candle_emergency"
        
        # ═══════════════════════════════════════════════════════════════════
        # MIN HOLD удален (10.06.2026)
        # Проблема: MIN HOLD = 5 минут блокировал early exit при убытках
        # Пример: BLESS -2.26% на 4.9 мин блокирован дошел до -2.92%
        # Решение: Убрать MIN HOLD, позволить protective exits работать
        # Reversal strategy = быстрые входы/выходы, защита капитала важнее
        # ═══════════════════════════════════════════════════════════════════
        
        # Get setup mode for this trade
        scores = self._current_scores.get(pair, {})
        setup_mode = scores.get('setup_mode')
        
        # Если _current_scores потерян (перезапуск бота) — восстановим mode из enter_tag
        # Формат тега: {src}_{class}_{dir}_{conf}, например 'bs_normal_cont_long_92%'
        if not setup_mode:
            tag = (trade.enter_tag or "").lower()
            if "lgbm_neutral" in tag:
                setup_mode = "DEX_LGBM_NEUTRAL"
            elif "dex_pro" in tag:
                setup_mode = "DEX_PRO"
            elif "reversal" in tag:
                setup_mode = "REVERSAL"
            elif "cont" in tag:
                setup_mode = "CONTINUATION"
            else:
                setup_mode = "CONTINUATION" # дефолт безопаснее (длиннее терпеть)
            logger.debug(
                f"setup_mode восстановлен из enter_tag '{trade.enter_tag}' → {setup_mode}"
            )
        
        # ═══════════════════════════════════════════════════════════════════
        # MAX_ANALYSIS: ОТКЛЮЧЁН EARLY_FAIL (21.08.2026)
        # Проблема: 24 LOSS сделок за последние 100, 0% WR, -30.67% PnL
        # За эти сигналы уже подумали: СКАНЕР + МОДЕЛЬ + KIRO
        # Даём им больше времени развиться - используем ТОЛЬКО hard stoploss
        # ═══════════════════════════════════════════════════════════════════
        if setup_mode == 'MAX_ANALYSIS':
            # Для max-analysis: НЕТ early_fail, только hard stoploss
            # FIX (30.08.2026): инициализируем переменные чтобы не было UnboundLocalError
            fast_fail_dur = 9999  # отключено
            fast_fail_pnl = -1.0  # отключено
            early_fail_dur = 9999  # отключено
            early_fail_pnl = -1.0  # отключено
            
            logger.debug(
                f"💎 MAX-ANALYSIS MODE {pair} | NO early_fail | "
                f"duration={trade_duration:.0f}min | pnl={current_profit:.2%}"
            )
            # Переходим к hold zones и другим проверкам ниже
            pass
        else:
            # Mode-aware fast fail params для обычных сигналов
            if setup_mode == 'REVERSAL':
                fast_fail_dur = Config.REVERSAL_FAST_FAIL_DUR
                fast_fail_pnl = Config.REVERSAL_FAST_FAIL_PNL
                early_fail_dur = Config.EARLY_FAIL_DUR_REVERSAL
                early_fail_pnl = Config.EARLY_FAIL_PNL
            elif setup_mode == 'DEX_LGBM_NEUTRAL':
                fast_fail_dur = Config.LGBM_NEUTRAL_FAST_FAIL_DUR
                fast_fail_pnl = Config.LGBM_NEUTRAL_FAST_FAIL_PNL
                early_fail_dur = Config.LGBM_NEUTRAL_EARLY_FAIL_DUR
                early_fail_pnl = Config.LGBM_NEUTRAL_EARLY_FAIL_PNL
            elif setup_mode == 'DEX_PRO':
                fast_fail_dur = Config.DEX_PRO_FAST_FAIL_DUR
                fast_fail_pnl = Config.DEX_PRO_FAST_FAIL_PNL
                early_fail_dur = Config.DEX_PRO_EARLY_FAIL_DUR
                early_fail_pnl = Config.DEX_PRO_EARLY_FAIL_PNL
            else:
                fast_fail_dur = Config.CONTINUATION_FAST_FAIL_DUR
                fast_fail_pnl = Config.CONTINUATION_FAST_FAIL_PNL
                early_fail_dur = Config.EARLY_FAIL_DUR_CONTINUATION
                early_fail_pnl = Config.EARLY_FAIL_PNL

            # ── РАННИЙ FAST FAIL (новый) ──────────────────────────────────────
            # Данные: 38 fast_fail_reversal — у большинства MFE=0%, сделка никогда
            # не была в плюсе. Если за 10-15 мин цена не двинулась в нашу сторону
            # и уже -0.8% — выходим сразу, не ждём полного fast_fail.
            if trade_duration > early_fail_dur and current_profit < early_fail_pnl:
                velocity = last["velocity"]
                # Цена идёт против нас и нет признаков разворота
                velocity_against = (
                    (is_long and velocity < 0) or
                    (not is_long and velocity > 0)
                )
                # Нет абсорбции в нашу сторону
                no_rescue = not self.risk_engine.absorption_rescue(last, is_long, current_profit)
                if velocity_against and no_rescue:
                    logger.info(
                        f" EARLY FAIL {pair} | mode={setup_mode} | "
                        f"{trade_duration:.0f}min | pnl={current_profit:.2%} | vel={velocity:.2f}"
                    )
                    return f"early_fail_{setup_mode.lower()}"
        
        # Hold zones - don't exit
        if self.risk_engine.absorption_rescue(last, is_long, current_profit):
            logger.info(
                f" ABSORPTION HOLD {pair} | pnl={current_profit:.2%}"
            )
            return None
        
        if self.risk_engine.in_hold_zone(last, is_long):
            return None
        
        # ═══════════════════════════════════════════════════════════════════
        # ШТРАФ ЗА РАННИЙ ВЫХОД ИЗ ПРОФИТНОЙ СДЕЛКИ (08.06.2026)
        # Проблема: Фунтик выходит на первом профите по soft_exit
        # Решение: Блокируем soft exit если:
        # - duration < 10 минут И profit < +2%
        # - Цель: дать профиту вырасти, не срезать цветы рано
        # ═══════════════════════════════════════════════════════════════════
        EARLY_EXIT_PENALTY_TIME = 10 # минут
        EARLY_EXIT_PENALTY_PROFIT = 0.02 # +2%
        
        if (trade_duration < EARLY_EXIT_PENALTY_TIME and
            0 < current_profit < EARLY_EXIT_PENALTY_PROFIT):
            # Профитная сделка но слишком рано выходить
            logger.info(
                f" EARLY EXIT PENALTY {pair} | duration={trade_duration:.1f}m < {EARLY_EXIT_PENALTY_TIME}m | "
                f"profit={current_profit:.2%} < {EARLY_EXIT_PENALTY_PROFIT:.1%} | ДЕРЖИМ!"
            )
            return None # НЕ ВЫХОДИМ
        
        # ═══════════════════════════════════════════════════════════════════
        # RUNNER HARD TP (28.07.2026)
        # Проблема: после TP1+TP2 runner может висеть часами при +10-15%
        # Решение: жёсткий выход при +5-8% если было 2+ partial exits
        # ═══════════════════════════════════════════════════════════════════
        RUNNER_HARD_TP_MIN = 0.05  # +5% минимум для закрытия runner
        RUNNER_HARD_TP_MAX = 0.08  # +8% максимум, дальше не держим
        
        if trade.nr_of_successful_exits >= 2:
            # Было 2+ partial exits (TP1 + TP2), остался runner
            if current_profit >= RUNNER_HARD_TP_MAX:
                logger.info(
                    f"🎯 RUNNER HARD TP MAX {pair} | profit={current_profit:.2%} >= {RUNNER_HARD_TP_MAX:.1%} | "
                    f"exits={trade.nr_of_successful_exits} | ЗАКРЫВАЕМ ПОЛНОСТЬЮ"
                )
                return "runner_hard_tp_max"
            elif current_profit >= RUNNER_HARD_TP_MIN:
                # От +5% до +8% - проверяем признаки разворота
                momentum = last["momentum"]
                velocity = last["velocity"]
                reversal_signs = (
                    (is_long and momentum < -0.3 and velocity < -0.3) or
                    (not is_long and momentum > 0.3 and velocity > 0.3)
                )
                if reversal_signs:
                    logger.info(
                        f"🎯 RUNNER HARD TP {pair} | profit={current_profit:.2%} | "
                        f"mom={momentum:.2f} vel={velocity:.2f} | разворот, ЗАКРЫВАЕМ"
                    )
                    return "runner_hard_tp_reversal"
        elif trade.nr_of_successful_exits >= 1 and current_profit >= 0.10:
            # Был только TP1, но профит уже +10% - закрываем полностью
            logger.info(
                f"🎯 MEGA PROFIT EXIT {pair} | profit={current_profit:.2%} >= +10% | "
                f"exits={trade.nr_of_successful_exits} | ЗАКРЫВАЕМ ВСЁ"
            )
            return "mega_profit_exit"
        
        # Take profits - ОТКЛЮЧЕНО (21.09.2026)
        # ПРОБЛЕМА: custom_exit() вызывается РАНЬШЕ adjust_trade_position()
        # Если вернуть строку здесь - сделка ПОЛНОСТЬЮ закроется, 
        # блокируя partial TP (30% @ 1.5%, 30% @ 2.5%)
        # РЕШЕНИЕ: Используем ТОЛЬКО adjust_trade_position() для TP
        # custom_exit() только для emergency exits (spike candle, mega profit)
        
        # СТАРЫЙ КОД (конфликтует с partial TP):
        # if current_profit >= Config.TAKE_PROFIT_2:
        #     return "tp2_1pct"
        
        if current_profit >= Config.TAKE_PROFIT_1:
            momentum = last["momentum"]
            velocity = last["velocity"]

            # После partial TP (50% закрыто) — держим остаток если тренд подтверждает
            # Цель: дать позиции расти к TP2 (+3%) пока тренд в нашу сторону
            if trade.nr_of_successful_exits > 0:
                # 1. Проверяем momentum/velocity на 5m (быстрый тренд)
                momentum_strong = (
                    (is_long and momentum > 0.3 and velocity > 0.5) or
                    (not is_long and momentum < -0.3 and velocity < -0.5)
                )
                if momentum_strong and current_profit < Config.TAKE_PROFIT_2:
                    logger.info(
                        f" HOLD MOMENTUM {pair} | mom={momentum:.2f} vel={velocity:.2f} | "
                        f"profit={current_profit:.2%} waiting for TP2"
                    )
                    return None # держим из-за сильного momentum
                
                # 2. Проверяем тренд на 1h — если попутный, держим
                try:
                    df_1h = self.dp.get_pair_dataframe(pair=pair, timeframe="1h")
                    if df_1h is not None and len(df_1h) >= 6:
                        past_1h = df_1h["close"].iloc[-6]
                        now_1h = df_1h["close"].iloc[-1]
                        pct_1h = (now_1h - past_1h) / past_1h * 100 if past_1h > 0 else 0
                        trend_confirms = (
                            (is_long and pct_1h > 0.5) or
                            (not is_long and pct_1h < -0.5)
                        )
                        if trend_confirms and current_profit < Config.TAKE_PROFIT_2:
                            logger.info(
                                f" HOLD TREND {pair} | 1h_trend={pct_1h:+.1f}% confirms | "
                                f"profit={current_profit:.2%} waiting for TP2"
                            )
                            return None # держим остаток
                except Exception:
                    pass

            # ═══════════════════════════════════════════════════════════════
            # SOFT EXIT - ТОЛЬКО при СИЛЬНОМ развороте (08.06.2026)
            # Проблема: выходили при первом же тике против
            # Решение: требуем СИЛЬНЫЙ разворот (momentum И velocity И absorption)
            # ═══════════════════════════════════════════════════════════════
            if is_long:
                # Для LONG exit нужны ВСЕ признаки разворота вниз:
                strong_reversal = (
                    momentum < -0.5 and # было < 0, теперь < -0.5
                    velocity < -0.5 and # было < 0, теперь < -0.5
                    (last["absorption_short"] or last["reclaim_short"])
                )
                if strong_reversal:
                    logger.info(
                        f" STRONG REVERSAL {pair} | mom={momentum:.2f} vel={velocity:.2f} | "
                        f"profit={current_profit:.2%} soft exit"
                    )
                    return "long_soft_exit"
                else:
                    logger.info(
                        f" WEAK SIGNAL {pair} | mom={momentum:.2f} vel={velocity:.2f} | "
                        f"не хватает для soft exit, ДЕРЖИМ"
                    )
            else:
                # Для SHORT exit нужны ВСЕ признаки разворота вверх:
                strong_reversal = (
                    momentum > 0.5 and # было > 0, теперь > 0.5
                    velocity > 0.5 and # было > 0, теперь > 0.5
                    (last["absorption_long"] or last["reclaim_long"])
                )
                if strong_reversal:
                    logger.info(
                        f" STRONG REVERSAL {pair} | mom={momentum:.2f} vel={velocity:.2f} | "
                        f"profit={current_profit:.2%} soft exit"
                    )
                    return "short_soft_exit"
                else:
                    logger.info(
                        f" WEAK SIGNAL {pair} | mom={momentum:.2f} vel={velocity:.2f} | "
                        f"не хватает для soft exit, ДЕРЖИМ"
                    )
        
        # Mode-aware fast fail
        if trade_duration > fast_fail_dur and current_profit < fast_fail_pnl:
            velocity = last["velocity"]
            velocity_dead = (
                (is_long and velocity < 0) or
                (not is_long and velocity > 0)
            )
            if velocity_dead:
                logger.info(
                    f" FAST FAIL {pair} | mode={setup_mode} | "
                    f"{trade_duration:.0f}min | pnl={current_profit:.2%}"
                )
                return f"fast_fail_{setup_mode.lower()}"
        
        # Early reversal exit (small profit + reversal signs)
        if current_profit > 0.003:
            momentum = last["momentum"]
            rsi = last["rsi"]
            
            if is_long and momentum < -0.8 and rsi < 40:
                return "long_reversal"
            elif not is_long and momentum > 0.8 and rsi > 60:
                return "short_reversal"
        
        return None

    # ============================================================

