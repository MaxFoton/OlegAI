#!/usr/bin/env python3
"""
WAIT Queue Monitor - проверяет условия для сигналов в очереди ожидания
Запуск:
python3 wait_queue_monitor.py --once      # Одна проверка
python3 wait_queue_monitor.py --watch     # Непрерывный мониторинг (каждые 15 сек)
"""
import asyncio
import json
import logging
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Timezone MSK (UTC+3)
MSK = timezone(timedelta(hours=3))

import requests

# Paths
WAIT_QUEUE_FILE = Path("/home/max/freqtrade/.kiro/wait_queue.json")
CONFIRMED_SIGNALS_FILE = Path("/home/max/o_p/dex_scanner/data/dex_signals_analysis.json")
LOG_FILE = Path("/home/max/freqtrade/logs/wait_monitor.log")
NTFY_URL = "http://87.121.218.4:8080/max-analysis"

# Bybit API
BYBIT_API_BASE = "https://api.bybit.com"

# BLACKLIST: Проблемные монеты
BYBIT_BLACKLIST = {'BONK', 'WIF', 'POPCAT', 'SHIB1000', 'DOGE1000', 'PYTH'}


def normalize_symbol(symbol: str) -> str:
    """
    Нормализует символ для Bybit API
    Убирает все варианты USDT суффиксов и добавляет один раз
    """
    value = str(symbol).upper()
    value = value.replace("/USDT:USDT", "")
    value = value.replace("/USDT", "")
    value = value.replace(":USDT", "")
    return value if value.endswith("USDT") else value + "USDT"


def parse_iso_datetime(s: str) -> datetime:
    """
    🔥 ROBUST ISO datetime parser - обрабатывает мусорные форматы:
    - '2026-09-28T19:45:54.201197+00:00+00:00' (двойной часовой пояс)
    - '2026-09-28T19:45:54.201197+00:00Z'      (смешанный)
    - '2026-09-28T19:45:54.201197Z'            (только Z)
    - '2026-09-28T19:45:54.201197+00:00'       (правильный)
    - '2026-09-28T19:45:54.201197'             (naive, без TZ)
    
    Returns: aware datetime в UTC
    """
    if not s:
        raise ValueError("Empty datetime string")
    
    # Step 1: Заменяем 'Z' на '+00:00'
    normalized = s.replace('Z', '+00:00')
    
    # Step 2: Убираем дубликаты '+00:00+00:00' → '+00:00'
    while '+00:00+00:00' in normalized:
        normalized = normalized.replace('+00:00+00:00', '+00:00')
    
    # Step 3: Парсим
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as e:
        raise ValueError(f"Cannot parse datetime '{s}' (normalized: '{normalized}'): {e}")
    
    # Step 4: Если naive (без TZ) - считаем UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    
    return dt


# Настройка логирования
LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("wait-monitor")


class BybitDataFetcher:
    """Получение актуальных данных с Bybit"""
    
    @staticmethod
    def get_current_candles(symbol: str, interval: str = "5", limit: int = 20) -> list:
        """Получить последние ЗАКРЫТЫЕ свечи"""
        try:
            url = f"{BYBIT_API_BASE}/v5/market/kline"
            params = {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": limit + 1
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get('retCode') != 0:
                logger.error(f"Bybit API error: {data.get('retMsg')}")
                return []
            
            candles = []
            for item in data.get('result', {}).get('list', []):
                candles.append({
                    'timestamp': int(item[0]),
                    'open': float(item[1]),
                    'high': float(item[2]),
                    'low': float(item[3]),
                    'close': float(item[4]),
                    'volume': float(item[5])
                })
            
            candles.reverse()
            
            if len(candles) > limit:
                candles = candles[:-1]
            
            logger.debug(f"Dropped last open candle, using {len(candles)} closed candles")
            return candles
        except Exception as e:
            logger.error(f"Failed to fetch candles for {symbol}: {e}")
            return []
    
    @staticmethod
    def calculate_rsi(candles: list, period: int = 14) -> float:
        """Рассчитать RSI на основе свечей"""
        if len(candles) < period + 1:
            return 50.0
        
        closes = [c['close'] for c in candles[-(period+1):]]
        gains = []
        losses = []
        
        for i in range(1, len(closes)):
            change = closes[i] - closes[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)


class ConditionChecker:
    """Проверка структурированных условий"""
    
    def __init__(self, symbol: str, direction: str):
        self.symbol = symbol
        self.direction = direction
        self.bybit_symbol = normalize_symbol(symbol)
        
        # 🔥 FIX (30.09.2026): Увеличен лимит свечей для VW-MACD (нужно 26) и MACD strength (40)
        self.candles_5m = BybitDataFetcher.get_current_candles(self.bybit_symbol, "5", 50)
        self.candles_1h = BybitDataFetcher.get_current_candles(self.bybit_symbol, "60", 30)
        
        if not self.candles_5m:
            logger.warning(f"⚠️ No candles fetched for {symbol}")
    
    def check_condition(self, condition: dict) -> tuple:
        """Проверить одно условие"""
        cond_type = condition.get('type')
        
        if not self.candles_5m:
            return False, "No market data available"
        
        current_candle = self.candles_5m[-1]
        prev_candle = self.candles_5m[-2] if len(self.candles_5m) > 1 else current_candle
        
        # RSI условия
        if cond_type == "rsi_below":
            threshold = condition.get('threshold', 35)
            rsi = BybitDataFetcher.calculate_rsi(self.candles_5m)
            is_met = rsi < threshold
            return is_met, f"RSI={rsi:.1f} {'<' if is_met else '>='} {threshold}"
        
        elif cond_type == "rsi_above":
            threshold = condition.get('threshold', 65)
            rsi = BybitDataFetcher.calculate_rsi(self.candles_5m)
            is_met = rsi > threshold
            return is_met, f"RSI={rsi:.1f} {'>' if is_met else '<='} {threshold}"
        
        # Свечные паттерны
        elif cond_type == "candle_bullish":
            is_met = current_candle['close'] > current_candle['open']
            return is_met, f"Candle {'bullish' if is_met else 'bearish'} (c={current_candle['close']:.6f} vs o={current_candle['open']:.6f})"
        
        elif cond_type == "candle_bearish":
            is_met = current_candle['close'] < current_candle['open']
            return is_met, f"Candle {'bearish' if is_met else 'bullish'} (c={current_candle['close']:.6f} vs o={current_candle['open']:.6f})"
        
        # Индикаторы
        elif cond_type == "indicator_match":
            indicator = condition.get('indicator')
            target_value = condition.get('value')
            
            if indicator == "vw_macd":
                if not self.candles_5m or len(self.candles_5m) < 26:
                    return False, "Insufficient data for VW-MACD (need 26 candles)"
                
                closes = [c['close'] for c in self.candles_5m]
                
                def calculate_ema(prices, period):
                    multiplier = 2 / (period + 1)
                    ema = [sum(prices[:period]) / period]
                    for price in prices[period:]:
                        ema.append((price - ema[-1]) * multiplier + ema[-1])
                    return ema
                
                ema12_series = calculate_ema(closes, 12)
                ema26_series = calculate_ema(closes, 26)
                macd_series = [e12 - e26 for e12, e26 in zip(ema12_series[14:], ema26_series)]
                signal_series = calculate_ema(macd_series, 9)
                histogram = macd_series[-1] - signal_series[-1]
                
                if target_value == "bullish":
                    is_met = histogram > 0
                elif target_value == "bearish":
                    is_met = histogram < 0
                else:
                    return False, f"Unknown VW-MACD target: {target_value}"
                
                return is_met, f"VW-MACD {'bullish' if histogram > 0 else 'bearish'} (histogram={histogram:.6f})"
            
            elif indicator == "ema":
                if not self.candles_5m or len(self.candles_5m) < 21:
                    return False, "Insufficient data for EMA (need 21 candles)"
                
                closes = [c['close'] for c in self.candles_5m]
                
                def calculate_ema(prices, period):
                    multiplier = 2 / (period + 1)
                    ema = [sum(prices[:period]) / period]
                    for price in prices[period:]:
                        ema.append((price - ema[-1]) * multiplier + ema[-1])
                    return ema[-1]
                
                ema9 = calculate_ema(closes, 9)
                ema21 = calculate_ema(closes, 21)
                
                if target_value == "bullish":
                    is_met = ema9 > ema21
                elif target_value == "bearish":
                    is_met = ema9 < ema21
                else:
                    return False, f"Unknown EMA target: {target_value}"
                
                return is_met, f"EMA {'bullish' if ema9 > ema21 else 'bearish'} (EMA9={ema9:.6f} vs EMA21={ema21:.6f})"
            
            elif indicator == "lorentzian":
                if not self.candles_5m or len(self.candles_5m) < 22:
                    return False, "Insufficient data for Lorentzian (need 22 candles)"
                
                closes = [c['close'] for c in self.candles_5m]
                
                def calc_ema(values, period):
                    k = 2.0 / (period + 1)
                    ema = values[0]
                    for v in values[1:]:
                        ema = v * k + ema * (1 - k)
                    return ema
                
                ema20 = calc_ema(closes, 20)
                last_close = closes[-1]
                trend = 1 if last_close > ema20 else -1
                
                momentum = 0
                for i in range(len(closes) - 3, len(closes)):
                    if i > 0:
                        momentum += 1 if closes[i] > closes[i - 1] else -1
                
                lor_signal = 1 if trend == 1 and momentum > 0 else (-1 if trend == -1 and momentum < 0 else 0)
                
                if target_value in ["+1", "+3"]:
                    is_met = lor_signal == 1
                elif target_value in ["-1", "-3"]:
                    is_met = lor_signal == -1
                else:
                    return False, f"Unknown Lorentzian target: {target_value}"
                
                return is_met, f"Lorentzian signal={lor_signal:+d} (trend={trend:+d}, momentum={momentum:+d}, target={target_value})"
            
            else:
                return False, f"Unknown indicator: {indicator}"
        
        # VWAP пробой
        elif cond_type == "vwap_break":
            break_direction = condition.get('direction')
            if break_direction == "up":
                is_met = current_candle['close'] > prev_candle['close']
                return is_met, f"Price {'rising' if is_met else 'not rising'}"
            else:
                is_met = current_candle['close'] < prev_candle['close']
                return is_met, f"Price {'falling' if is_met else 'not falling'}"
        
        # Volume spike - НОВАЯ ЛОГИКА (03.10.2026)
        # LONG: проверяем volume spike + MACD + Lorentzian
        # SHORT: проверяем ТОЛЬКО MACD + Lorentzian (БЕЗ объемов)
        elif cond_type == "volume_spike_above":
            threshold = condition.get('threshold', 1.0)
            
            # Для SHORT: игнорируем условие по объему, проверяем только MACD + Lorentzian
            if self.direction == "SHORT":
                # 1. Проверяем MACD
                if len(self.candles_5m) < 40:
                    return False, "Недостаточно свечей для MACD"
                
                closes = [c['close'] for c in self.candles_5m]
                
                # EMA12 и EMA26
                def ema(values, period):
                    k = 2.0 / (period + 1)
                    result = [values[0]]
                    for v in values[1:]:
                        result.append(v * k + result[-1] * (1 - k))
                    return result
                
                ema12 = ema(closes, 12)
                ema26 = ema(closes, 26)
                macd_line = [f - s for f, s in zip(ema12, ema26)]
                signal_line = ema(macd_line, 9)
                histogram = macd_line[-1] - signal_line[-1]
                
                # 2. Проверяем Lorentzian (EMA20 momentum)
                if len(closes) < 22:
                    return False, "Недостаточно свечей для Lorentzian"
                
                def ema_simple(values, period):
                    k = 2.0 / (period + 1)
                    result = values[0]
                    for v in values[1:]:
                        result = v * k + result * (1 - k)
                    return result
                
                ema20 = ema_simple(closes, 20)
                last_close = closes[-1]
                trend = 1 if last_close > ema20 else -1
                
                # Momentum: сколько из последних 3 свечей падали
                momentum = sum(1 if closes[i] > closes[i-1] else -1 for i in range(-3, 0, 1))
                lor_signal = 1 if trend == 1 and momentum > 0 else (-1 if trend == -1 and momentum < 0 else 0)
                
                macd_ok = histogram < 0
                lor_ok = lor_signal == -1
                
                if not macd_ok:
                    return False, f"SHORT: MACD bullish (hist={histogram:.6f}) - не bearish"
                if not lor_ok:
                    return False, f"SHORT: MACD OK но Lorentzian не bearish (sig={lor_signal})"
                
                return True, f"✅ SHORT confirmed: MACD bearish (hist={histogram:.6f}), Lorentzian=-1"
            
            # Для LONG: проверяем ВСЁ (volume spike + sustained + MACD + Lorentzian)
            else:
                # 1. Проверяем volume spike
                volumes = [c['volume'] for c in self.candles_5m[:-1]]
                median_vol = sorted(volumes)[len(volumes)//2] if volumes else 1
                current_vol = current_candle['volume']
                vol_ratio = current_vol / median_vol if median_vol > 0 else 0
                
                if vol_ratio < threshold:
                    return False, f"Volume spike {vol_ratio:.2f}x < {threshold}x"
                
                # 2. Проверяем что объемы НЕ падают (текущий >= 90% от пика последних 3 свечей)
                recent_volumes = [c['volume'] for c in self.candles_5m[-3:]]
                peak_vol = max(recent_volumes) if recent_volumes else current_vol
                vol_sustained = (current_vol / peak_vol) >= 0.9 if peak_vol > 0 else False
                
                if not vol_sustained:
                    return False, f"Volume spike {vol_ratio:.2f}x OK но падает: {current_vol/peak_vol:.1%} от пика"
                
                # 3. Проверяем MACD
                if len(self.candles_5m) < 40:
                    return False, "Недостаточно свечей для MACD"
                
                closes = [c['close'] for c in self.candles_5m]
                
                # EMA12 и EMA26
                def ema(values, period):
                    k = 2.0 / (period + 1)
                    result = [values[0]]
                    for v in values[1:]:
                        result.append(v * k + result[-1] * (1 - k))
                    return result
                
                ema12 = ema(closes, 12)
                ema26 = ema(closes, 26)
                macd_line = [f - s for f, s in zip(ema12, ema26)]
                signal_line = ema(macd_line, 9)
                histogram = macd_line[-1] - signal_line[-1]
                
                # 4. Проверяем Lorentzian (EMA20 momentum)
                if len(closes) < 22:
                    return False, "Недостаточно свечей для Lorentzian"
                
                def ema_simple(values, period):
                    k = 2.0 / (period + 1)
                    result = values[0]
                    for v in values[1:]:
                        result = v * k + result * (1 - k)
                    return result
                
                ema20 = ema_simple(closes, 20)
                last_close = closes[-1]
                trend = 1 if last_close > ema20 else -1
                
                # Momentum: сколько из последних 3 свечей росли
                momentum = sum(1 if closes[i] > closes[i-1] else -1 for i in range(-3, 0, 1))
                lor_signal = 1 if trend == 1 and momentum > 0 else (-1 if trend == -1 and momentum < 0 else 0)
                
                macd_ok = histogram > 0
                lor_ok = lor_signal == 1
                
                if not macd_ok:
                    return False, f"Volume OK ({vol_ratio:.2f}x) но MACD bearish (hist={histogram:.6f})"
                if not lor_ok:
                    return False, f"Volume OK ({vol_ratio:.2f}x) MACD OK но Lorentzian не bullish (sig={lor_signal})"
                
                return True, f"✅ LONG confirmed: vol={vol_ratio:.2f}x sustained, MACD bullish (hist={histogram:.6f}), Lorentzian=+1"
        
        # HTF trend
        elif cond_type == "htf_trend_above":
            threshold = condition.get('threshold', 0)
            if not self.candles_1h or len(self.candles_1h) < 2:
                return False, "Insufficient 1h data"
            change_pct = ((self.candles_1h[-1]['close'] - self.candles_1h[-2]['close']) / self.candles_1h[-2]['close']) * 100
            is_met = change_pct > threshold
            return is_met, f"1h trend {change_pct:+.2f}% {'>' if is_met else '≤'} {threshold}%"
        
        elif cond_type == "htf_trend_below":
            threshold = condition.get('threshold', 0)
            if not self.candles_1h or len(self.candles_1h) < 2:
                return False, "Insufficient 1h data"
            change_pct = ((self.candles_1h[-1]['close'] - self.candles_1h[-2]['close']) / self.candles_1h[-2]['close']) * 100
            is_met = change_pct < threshold
            return is_met, f"1h trend {change_pct:+.2f}% {'<' if is_met else '≥'} {threshold}%"
        
        # MACD strength
        elif cond_type == "macd_strength_above":
            threshold = condition.get('threshold', 0.2)
            if not self.candles_5m or len(self.candles_5m) < 40:
                return False, "Insufficient data for MACD strength (need 40 candles)"
            
            closes = [c['close'] for c in self.candles_5m]
            
            def calc_ema_series(values, period):
                k = 2.0 / (period + 1)
                ema = [values[0]]
                for v in values[1:]:
                    ema.append(v * k + ema[-1] * (1 - k))
                return ema
            
            ema12 = calc_ema_series(closes, 12)
            ema26 = calc_ema_series(closes, 26)
            macd_series = [e12 - e26 for e12, e26 in zip(ema12[14:], ema26)]
            signal_series = calc_ema_series(macd_series, 9)
            histogram = macd_series[-1] - signal_series[-1]
            
            highs = [c['high'] for c in self.candles_5m]
            lows = [c['low'] for c in self.candles_5m]
            true_ranges = []
            for i in range(1, len(self.candles_5m)):
                h = highs[i]
                l = lows[i]
                prev_close = closes[i-1]
                tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
                true_ranges.append(tr)
            
            period = 14
            if len(true_ranges) >= period:
                atr = sum(true_ranges[-period:]) / period
            else:
                atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
            
            strength_atr = abs(histogram) / atr if atr > 0 else 0.0
            is_met = strength_atr > threshold
            return is_met, f"MACD strength_atr={strength_atr:.2f} {'>' if is_met else '<='} {threshold:.2f}"
        
        # Phase transition
        elif cond_type == "phase_transition":
            target_phase = condition.get('target_phase', 'UNKNOWN')
            if not self.candles_5m or len(self.candles_5m) < 12:
                return False, f"Insufficient data for phase detection (need 12 candles)"
            
            last_12 = self.candles_5m[-12:]
            hi = max(c['high'] for c in last_12)
            lo = min(c['low'] for c in last_12)
            
            if hi == lo:
                current_phase = "EARLY_EXPANSION"
            else:
                pos = (last_12[-1]['close'] - lo) / (hi - lo)
                if pos > 0.85:
                    current_phase = "LATE_EXPANSION"
                elif pos > 0.65:
                    current_phase = "MID_EXPANSION"
                elif pos > 0.15:
                    current_phase = "EARLY_EXPANSION"
                else:
                    current_phase = "EXHAUSTION"
            
            is_met = current_phase == target_phase
            return is_met, f"Phase: {current_phase} {'==' if is_met else '!='} {target_phase}"
        
        elif cond_type == "manual_confirmation":
            return False, "Requires manual confirmation"
        
        else:
            return False, f"Unknown condition type: {cond_type}"
    
    def check_all_conditions(self, conditions: list) -> tuple:
        """Проверить все условия"""
        if not conditions:
            return False, {"error": "No conditions specified"}
        
        results = {
            'total': len(conditions),
            'met': 0,
            'not_met': 0,
            'details': []
        }
        
        for cond in conditions:
            is_met, reason = self.check_condition(cond)
            results['details'].append({
                'condition': cond,
                'is_met': is_met,
                'reason': reason
            })
            if is_met:
                results['met'] += 1
            else:
                results['not_met'] += 1
        
        all_met = results['not_met'] == 0
        return all_met, results


def process_wait_queue():
    """Обработать wait_queue - проверить условия и подтвердить готовые сигналы"""
    import fcntl
    
    if not WAIT_QUEUE_FILE.exists():
        logger.info("Wait queue file not found, nothing to process")
        return
    
    # Читаем существующие confirmed signals
    existing_confirmed = {}
    if CONFIRMED_SIGNALS_FILE.exists():
        try:
            with CONFIRMED_SIGNALS_FILE.open('r', encoding='utf-8') as cf:
                fcntl.flock(cf.fileno(), fcntl.LOCK_SH)
                try:
                    confirmed_data = json.load(cf)
                    for sig in confirmed_data:
                        parent_id = sig.get('parent_signal_id')
                        if parent_id:
                            existing_confirmed[parent_id] = sig.get('signal_id')
                finally:
                    fcntl.flock(cf.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f"Could not read existing confirmed signals: {e}")
    
    try:
        with WAIT_QUEUE_FILE.open('r+', encoding='utf-8') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                wait_queue = json.load(f)
            except:
                logger.error("Failed to parse wait_queue.json")
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return
            
            if not wait_queue:
                logger.info("Wait queue is empty")
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return
            
            logger.info(f"📊 Processing {len(wait_queue)} signals in wait queue")
            
            confirmed_signals = []
            updated_queue = []
            
            for entry in wait_queue:
                status = entry.get('status')
                
                if status not in ['waiting']:
                    updated_queue.append(entry)
                    continue
                
                signal_id = entry.get('signal_id', 'unknown')
                symbol = entry.get('symbol', 'UNKNOWN')
                direction = entry.get('direction', 'UNKNOWN')
                conditions = entry.get('conditions', [])
                created_at = entry.get('created_at')
                source_price = entry.get('source_price', 0)
                
                # 🚫 PRICE FILTER: Пропускаем монеты дороже $0.1
                MAX_PRICE = 0.1
                if source_price > MAX_PRICE:
                    logger.info(f"⛔ {signal_id} PRICE TOO HIGH (symbol={symbol}, price=${source_price:.4f} > ${MAX_PRICE})")
                    entry['status'] = 'expired'
                    entry['expired_at'] = datetime.now(MSK).isoformat()
                    entry['expiry_reason'] = f'price_too_high_{source_price:.4f}'
                    updated_queue.append(entry)
                    continue
                
                # 🚫 BLACKLIST: Пропускаем проблемные монеты
                if symbol in BYBIT_BLACKLIST:
                    logger.info(f"🚫 {signal_id} BLACKLISTED (symbol={symbol})")
                    entry['status'] = 'expired'
                    entry['expired_at'] = datetime.now(MSK).isoformat()
                    entry['expiry_reason'] = f'blacklisted_{symbol}'
                    updated_queue.append(entry)
                    continue
                
                logger.info(f"🔍 Checking {signal_id}")
                
                # 🔥 FIX: Используем robust парсер для valid_until
                valid_until_str = entry.get('valid_until')
                if valid_until_str:
                    try:
                        valid_until = parse_iso_datetime(valid_until_str)
                        now = datetime.now(valid_until.tzinfo)
                        if now > valid_until:
                            logger.info(f"⏱️ {signal_id} expired (valid_until={valid_until_str})")
                            entry['status'] = 'expired'
                            entry['expired_at'] = datetime.now(MSK).isoformat()
                            entry['expiry_reason'] = 'valid_until_reached'
                            updated_queue.append(entry)
                            continue
                    except Exception as e:
                        logger.error(f"Failed to parse valid_until for {signal_id}: {e}")
                else:
                    # FALLBACK: created_at + 30 минут
                    if created_at:
                        try:
                            created_dt = parse_iso_datetime(created_at)
                            age = datetime.now(created_dt.tzinfo) - created_dt
                            if age > timedelta(minutes=30):
                                logger.info(f"⏱️ {signal_id} expired (age={age.total_seconds()/60:.1f}min > 30min)")
                                entry['status'] = 'expired'
                                entry['expired_at'] = datetime.now(MSK).isoformat()
                                entry['expiry_reason'] = 'age_over_30min'
                                updated_queue.append(entry)
                                continue
                        except:
                            pass
                
                # Проверяем условия
                checker = ConditionChecker(symbol, direction)
                all_met, results = checker.check_all_conditions(conditions)
                
                entry['recheck_count'] = entry.get('recheck_count', 0) + 1
                entry['last_recheck'] = datetime.now(MSK).isoformat()
                entry['last_check_results'] = results
                
                if all_met:
                    logger.info(f"✅ {signal_id} conditions MET! ({results['met']}/{results['total']})")
                    
                    candles = BybitDataFetcher.get_current_candles(f"{symbol}USDT", "5", 1)
                    current_price = candles[0]['close'] if candles else entry.get('source_price', 0)
                    
                    if signal_id in existing_confirmed:
                        existing_child = existing_confirmed[signal_id]
                        logger.warning(f"⚠️ {signal_id} already has confirmed child {existing_child}, skipping duplicate")
                        if not entry.get('child_signal_id'):
                            entry['child_signal_id'] = existing_child
                        entry['status'] = 'confirmed'
                        entry['confirmed_at'] = datetime.now(MSK).isoformat()
                        updated_queue.append(entry)
                        continue
                    
                    child_signal_id = entry.get('child_signal_id')
                    if child_signal_id:
                        logger.warning(f"⚠️ {signal_id} already has child signal {child_signal_id}, skipping duplicate")
                        updated_queue.append(entry)
                        continue
                    
                    confirmed_signal_id = f"{symbol.lower()}-confirmed-{datetime.now(MSK).strftime('%Y%m%d-%H%M%S')}"
                    
                    confirmation_time = datetime.now(MSK)
                    confirmed_valid_bars = 3
                    confirmed_valid_until = confirmation_time + timedelta(minutes=confirmed_valid_bars * 5)
                    
                    confirmed_signal = {
                        'signal_id': confirmed_signal_id,
                        'parent_signal_id': signal_id,
                        'status': 'approved',
                        'verdict': 'ENTRY',
                        'verdict_machine': 'ENTRY',
                        'symbol': symbol,
                        'coin': symbol,
                        'pair': f"{symbol}/USDT:USDT",
                        'direction': direction,
                        'confirmed_at': confirmation_time.isoformat(),
                        'entry_price': current_price,
                        'valid_until': confirmed_valid_until.isoformat(),
                        'max_entry_deviation_pct': entry.get('max_entry_deviation_pct', 0.5),
                        'score': entry.get('score', 8),
                        'score_max': 15,
                        'kind': 'wait_confirmed',
                        'signal_type': 'wait_confirmed',
                        'source': 'wait_monitor',
                        'timestamp': datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
                        'signal_time_utc': datetime.now(MSK).strftime("%H:%M:%S"),
                        'added_to_whitelist_at': datetime.now(MSK).isoformat(),
                        'confidence': entry.get('features', {}).get('confidence', 7),
                        'reasons_for': entry.get('features', {}).get('reasons_for', []) + [
                            f"✅ Wait conditions met ({results['met']}/{results['total']})"
                        ],
                        'reasons_against': [],
                        'execution_profile': entry.get('execution_profile', 'FUNTIK_DEFAULT'),
                        'risk_owner': 'PumpDumpReversalStrategy_v2',
                        'stop_policy': 'strategy',
                        'tp_policy': 'strategy',
                    }
                    confirmed_signals.append(confirmed_signal)
                    
                    entry['status'] = 'confirmed'
                    entry['confirmed_by'] = 'wait_monitor'
                    entry['child_signal_id'] = confirmed_signal_id
                    entry['confirmed_at'] = datetime.now(MSK).isoformat()
                    
                    # 🔥 ПРЕДЗАГРУЗКА СВЕЧЕЙ для WAIT CONFIRMED
                    asyncio.run(preload_candles_for_wait_confirmed(symbol, direction))
                    
                    send_confirmation_to_ntfy(symbol, direction, current_price, confirmed_signal)
                else:
                    logger.info(f"⏳ {signal_id} waiting ({results['met']}/{results['total']} met)")
                    updated_queue.append(entry)
            
            # Записываем confirmed signals
            if confirmed_signals:
                try:
                    with open(CONFIRMED_SIGNALS_FILE, 'r+' if CONFIRMED_SIGNALS_FILE.exists() else 'w', encoding='utf-8') as cf:
                        if CONFIRMED_SIGNALS_FILE.exists():
                            fcntl.flock(cf.fileno(), fcntl.LOCK_EX)
                            try:
                                existing = json.load(cf)
                            except:
                                existing = []
                        else:
                            existing = []
                        
                        existing.extend(confirmed_signals)
                        
                        cf.seek(0)
                        cf.truncate()
                        cf.write(json.dumps(existing, indent=2, ensure_ascii=False))
                        
                        if CONFIRMED_SIGNALS_FILE.exists():
                            fcntl.flock(cf.fileno(), fcntl.LOCK_UN)
                    
                    logger.info(f"✅ Added {len(confirmed_signals)} confirmed signals to dex_signals_analysis.json")
                except Exception as e:
                    logger.error(f"Failed to save confirmed signals: {e}", exc_info=True)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    return
            
            # Сохраняем wait_queue
            f.seek(0)
            f.truncate()
            f.write(json.dumps(updated_queue, indent=2, ensure_ascii=False))
            
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    
    except Exception as e:
        logger.error(f"Error processing wait queue: {e}", exc_info=True)
        return


import asyncio

async def preload_candles_for_wait_confirmed(symbol: str, direction: str):
    """
    Предзагрузка свечей для WAIT CONFIRMED сигнала.
    Загружает 100x5m, 50x15m, 50x1h через ccxt и сохраняет в формате Freqtrade.
    """
    try:
        import ccxt.async_support as ccxt
        from pathlib import Path
        
        # Создаём биржу
        exchange = ccxt.bybit({
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        
        # Путь для сохранения
        data_dir = Path("/home/max/freqtrade/user_data/data/bybit")
        data_dir.mkdir(parents=True, exist_ok=True)
        
        # Формат пары для Bybit
        pair = f"{symbol}/USDT:USDT"
        
        # Таймфреймы и количество свечей
        timeframes = {
            '5m': 100,
            '15m': 50,
            '1h': 50
        }
        
        logger.info(f"📥 [WAIT CONFIRMED] Preloading candles for {symbol} ({direction})...")
        
        for timeframe, limit in timeframes.items():
            try:
                # Загружаем свечи
                ohlcv = await exchange.fetch_ohlcv(pair, timeframe, limit=limit)
                
                # Конвертируем в формат Freqtrade: [[timestamp, open, high, low, close, volume], ...]
                freqtrade_data = []
                for candle in ohlcv:
                    freqtrade_data.append([
                        candle[0],  # timestamp (ms)
                        candle[1],  # open
                        candle[2],  # high
                        candle[3],  # low
                        candle[4],  # close
                        candle[5]   # volume
                    ])
                
                # Имя файла: SYMBOL_USDT_USDT-timeframe.json
                filename = f"{symbol}_USDT_USDT-{timeframe}.json"
                filepath = data_dir / filename
                
                # Сохраняем
                with filepath.open('w') as f:
                    json.dump(freqtrade_data, f)
                
                logger.info(f"  ✅ {timeframe}: {len(freqtrade_data)} candles → {filename}")
                
            except Exception as e:
                logger.error(f"  ❌ Failed to load {timeframe}: {e}")
        
        await exchange.close()
        logger.info(f"✅ [WAIT CONFIRMED] Preload complete for {symbol}")
        
    except Exception as e:
        logger.error(f"❌ [WAIT CONFIRMED] Preload failed for {symbol}: {e}")





def send_confirmation_to_ntfy(symbol: str, direction: str, price: float, signal: dict):
    """Отправить уведомление о подтверждении в ntfy"""
    try:
        title = f"✅ {symbol} {direction} | WAIT CONFIRMED"
        message = f"""💰 Entry: ${price:.6f}
📈 Score: {signal.get('score', 0)}/15
📊 Confidence: {signal.get('confidence', 0)}/10
✅ Wait conditions MET!
⚙️ План исполняет: PumpDumpReversalStrategy_v2
   └─ TP1: 30% @ +0.3%
   └─ TP2: 30% @ +0.8%
   └─ Runner: 40% @ +1.3%
   └─ SL: -1% (hard)
   └─ Runner Protection: активна
⏱️ Confirmed by wait_monitor"""
        
        import subprocess
        subprocess.run([
            'curl', '-s',
            '-H', f'Title: {title}',
            '-H', 'Priority: 5',
            '-d', message,
            NTFY_URL
        ], check=True, timeout=5)
        logger.info(f"✅ Sent confirmation to ntfy: {symbol} {direction}")
    except Exception as e:
        logger.error(f"Failed to send ntfy: {e}")


def main():
    parser = argparse.ArgumentParser(description="Wait Queue Monitor")
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--watch', action='store_true', help='Continuous monitoring (every 15 sec)')
    parser.add_argument('--interval', type=int, default=15, help='Check interval in seconds (default: 15)')
    args = parser.parse_args()
    
    if args.watch:
        logger.info(f"🔄 Starting wait_queue monitor (interval={args.interval}s)")
        print(f"🔄 Monitoring wait_queue every {args.interval} seconds...")
        print("Press Ctrl+C to stop")
        try:
            while True:
                process_wait_queue()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            print("\n✋ Stopped")
    else:
        logger.info("🔍 Running single check")
        process_wait_queue()
        logger.info("✅ Done")


if __name__ == "__main__":
    main()
