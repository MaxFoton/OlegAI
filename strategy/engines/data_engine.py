"""
Data Engine - Signal loading and market data access
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DataEngine:
    """
    Responsible for loading external signals and providing market data context.
    
    Signal sources:
    - telegram_pump_signals.json (Telegram pump/dump alerts)
    - spike_signals.json (Bybit spike scanner)
    - dex_signals.json (DEX Scanner alerts) — NEW 04.06.2026
    """
    
    def __init__(self, signals_path: Path, max_signal_age_sec: int = 900, dex_signals_path: Path = None):
        # Основной файл (telegram)
        self.signals_path = signals_path
        # Дополнительный файл (Bybit Spike Scanner) — лежит рядом
        self.spike_signals_path = signals_path.parent / "spike_signals.json"
        # DEX Scanner сигналы (новое!)
        self.dex_signals_path = dex_signals_path if dex_signals_path else None
        self.max_signal_age_sec = max_signal_age_sec
    
    def _load_file(self, path: Path) -> list:
        """Read a signals JSON file, return list (or [] on error)."""
        if not path.exists():
            return []
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"signals load error {path}: {e}")
            return []
    
    def get_signal(self, pair: str) -> Optional[dict]:
        """
        Load signal for given pair from external scanners.
        Reads telegram_pump_signals.json, spike_signals.json, AND dex_signals.json.
        Returns the freshest signal across all files.
        
        NEW (03.07.2026): Проверка возраста сигнала (max 30 min)
        Проблема: BLESS сигнал в 03:48 UTC, вход в 05:45 UTC (2 часа!)
        За это время рынок изменился (начался LONG тренд)
        """
        ticker = pair.split("/")[0]
        
        # Load all signal sources
        signals = self._load_file(self.signals_path) + self._load_file(self.spike_signals_path)
        
        # Add DEX signals if path provided
        if self.dex_signals_path:
            dex_signals = self._load_file(self.dex_signals_path)
            signals.extend(dex_signals)
        
        if not signals:
            return None
        
        best_signal = None
        best_age = float("inf")
        now = datetime.now()
        
        # NEW: Максимальный возраст сигнала для DEX = 30 минут (1800 sec)
        MAX_SIGNAL_AGE_DEX = 1800  # 30 min для DEX сигналов
        
        for signal in signals:
            if signal.get("coin") != ticker:
                continue
            
            ts = signal.get("timestamp")
            if not ts:
                continue
            
            try:
                # Поддерживаем оба формата timestamp: с tz и без
                ts_clean = ts.replace("+03:00", "").replace("+00:00", "")
                if "T" in ts_clean:
                    signal_time = datetime.fromisoformat(ts_clean)
                else:
                    signal_time = datetime.fromisoformat(ts_clean)
                
                age = (now - signal_time).total_seconds()
                if age < 0:
                    continue
                
                # Проверяем возраст в зависимости от источника
                source = signal.get("source", "telegram")
                if source in ["dex_reversal", "dex_pump", "dex_trend", "dex_volume", "dex_lgbm_neutral", "dex_pro", "dex_early", "wait_queue", "max-analysis"]:
                    # DEX сигналы + wait_queue: max 30 min (рынок быстро меняется)
                    if age > MAX_SIGNAL_AGE_DEX:
                        logger.debug(
                            f"⛔ STALE DEX SIGNAL {ticker} | age={age/60:.1f}m > 30m | "
                            f"source={source} | type={signal.get('signal_type')}"
                        )
                        continue
                else:
                    # Telegram/Spike: используем старый лимит
                    if age > self.max_signal_age_sec:
                        continue
                
                if age < best_age:
                    best_age = age
                    best_signal = signal
            except Exception as e:
                logger.debug(f"Signal parse error: {e}")
        
        return best_signal
    
    def get_signal_age(self, signal: dict) -> Optional[float]:
        """Return signal age in seconds, or None if invalid"""
        if not signal:
            return None
        
        ts = signal.get("timestamp")
        if not ts:
            return None
        
        try:
            signal_time = datetime.fromisoformat(ts.replace("+03:00", ""))
            return (datetime.now() - signal_time).total_seconds()
        except Exception:
            return None
