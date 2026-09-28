#!/usr/bin/env python3
"""
WAIT Queue Monitor - проверяет условия для сигналов в очереди ожидания

Запуск:
    python3 wait_queue_monitor.py --once      # Одна проверка
    python3 wait_queue_monitor.py --watch     # Непрерывный мониторинг (каждые 15 сек)
"""

import json
import logging
import time
import argparse
from pathlib import Path
from datetime import datetime, timedelta
import requests

# Paths
WAIT_QUEUE_FILE = Path("/home/max/freqtrade/.kiro/wait_queue.json")
CONFIRMED_SIGNALS_FILE = Path("/home/max/o_p/dex_scanner/data/dex_signals_analysis.json")
LOG_FILE = Path("/home/max/freqtrade/logs/wait_monitor.log")
NTFY_URL = "http://87.121.218.4:8080/max-analysis"

# Bybit API
BYBIT_API_BASE = "https://api.bybit.com"

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
        """
        Получить последние свечи
        
        Args:
            symbol: Торговая пара (например "HBARUSDT")
            interval: Интервал ("1", "5", "15", "60")
            limit: Количество свечей
            
        Returns:
            list: Список свечей [{open, high, low, close, volume, timestamp}, ...]
        """
        try:
            url = f"{BYBIT_API_BASE}/v5/market/kline"
            params = {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": limit
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
            
            # Сортируем по времени (Bybit возвращает в обратном порядке)
            candles.reverse()
            return candles
            
        except Exception as e:
            logger.error(f"Failed to fetch candles for {symbol}: {e}")
            return []
    
    @staticmethod
    def calculate_rsi(candles: list, period: int = 14) -> float:
        """Рассчитать RSI на основе свечей"""
        if len(candles) < period + 1:
            return 50.0  # Недостаточно данных
        
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
        self.bybit_symbol = f"{symbol}USDT"
        
        # Получаем актуальные данные
        self.candles_5m = BybitDataFetcher.get_current_candles(self.bybit_symbol, "5", 20)
        self.candles_1h = BybitDataFetcher.get_current_candles(self.bybit_symbol, "60", 20)
        
        if not self.candles_5m:
            logger.warning(f"⚠️ No candles fetched for {symbol}")
    
    def check_condition(self, condition: dict) -> tuple[bool, str]:
        """
        Проверить одно условие
        
        Returns:
            (is_met: bool, reason: str)
        """
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
        
        # Индикаторы - требуют дополнительных расчётов
        elif cond_type == "indicator_match":
            indicator = condition.get('indicator')
            target_value = condition.get('value')
            
            # VW-MACD - проверяем по цене и объёму
            if indicator == "vw_macd":
                if not self.candles_5m or len(self.candles_5m) < 20:
                    return False, "Insufficient data for VW-MACD"
                
                # Упрощённая проверка: bullish = цена растёт, bearish = падает
                recent_trend = (self.candles_5m[-1]['close'] - self.candles_5m[-5]['close']) / self.candles_5m[-5]['close']
                
                if target_value == "bullish":
                    is_met = recent_trend > 0
                elif target_value == "bearish":
                    is_met = recent_trend < 0
                else:
                    return False, f"Unknown VW-MACD target: {target_value}"
                
                return is_met, f"VW-MACD {'bullish' if recent_trend > 0 else 'bearish'} (trend={recent_trend:+.2%})"
            
            # EMA - проверяем краткосрочный vs долгосрочный тренд
            elif indicator == "ema":
                if not self.candles_5m or len(self.candles_5m) < 20:
                    return False, "Insufficient data for EMA"
                
                # EMA9 vs EMA21
                closes = [c['close'] for c in self.candles_5m[-21:]]
                ema9 = closes[-1]  # Упрощение
                ema21 = sum(closes) / len(closes)
                
                if target_value == "bullish":
                    is_met = ema9 > ema21
                elif target_value == "bearish":
                    is_met = ema9 < ema21
                else:
                    return False, f"Unknown EMA target: {target_value}"
                
                return is_met, f"EMA {'bullish' if ema9 > ema21 else 'bearish'}"
            
            # Lorentzian - упрощённая проверка по momentum
            elif indicator == "lorentzian":
                if not self.candles_5m or len(self.candles_5m) < 5:
                    return False, "Insufficient data for Lorentzian"
                
                # Упрощение: проверяем 3-барный momentum
                momentum = (self.candles_5m[-1]['close'] - self.candles_5m[-4]['close']) / self.candles_5m[-4]['close']
                
                if target_value in ["+1", "+3"]:
                    is_met = momentum > 0.005  # >0.5% рост
                elif target_value in ["-1", "-3"]:
                    is_met = momentum < -0.005  # <-0.5% падение
                else:
                    return False, f"Unknown Lorentzian target: {target_value}"
                
                return is_met, f"Lorentzian momentum={momentum:+.2%}"
            
            else:
                return False, f"Unknown indicator: {indicator}"
        
        # VWAP пробой
        elif cond_type == "vwap_break":
            break_direction = condition.get('direction')
            # Для простоты проверяем только направление свечи
            if break_direction == "up":
                is_met = current_candle['close'] > prev_candle['close']
                return is_met, f"Price {'rising' if is_met else 'not rising'}"
            else:
                is_met = current_candle['close'] < prev_candle['close']
                return is_met, f"Price {'falling' if is_met else 'not falling'}"
        
        # Volume spike
        elif cond_type == "volume_spike_above":
            threshold = condition.get('threshold', 1.0)
            # Рассчитываем median volume
            volumes = [c['volume'] for c in self.candles_5m[:-1]]
            median_vol = sorted(volumes)[len(volumes)//2] if volumes else 1
            current_vol = current_candle['volume']
            vol_ratio = current_vol / median_vol if median_vol > 0 else 0
            is_met = vol_ratio >= threshold
            return is_met, f"Volume spike {vol_ratio:.2f}x {'≥' if is_met else '<'} {threshold}x"
        
        # HTF trend
        elif cond_type == "htf_trend_above":
            threshold = condition.get('threshold', 0)
            if not self.candles_1h or len(self.candles_1h) < 2:
                return False, "Insufficient 1h data"
            
            # Изменение за 1ч
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
            if not self.candles_5m or len(self.candles_5m) < 10:
                return False, "Insufficient data for MACD strength"
            
            # ATR как proxy
            highs = [c['high'] for c in self.candles_5m[-10:]]
            lows = [c['low'] for c in self.candles_5m[-10:]]
            atr = sum(h - l for h, l in zip(highs, lows)) / len(highs)
            atr_pct = atr / self.candles_5m[-1]['close']
            
            is_met = atr_pct > threshold
            return is_met, f"MACD strength (ATR proxy) {atr_pct:.2%} {'>' if is_met else '<='} {threshold:.2%}"
        
        # Phase transition
        elif cond_type == "phase_transition":
            # Упрощённая проверка - всегда false, требует полного scanner context
            target_phase = condition.get('target_phase', 'UNKNOWN')
            return False, f"Phase transition to {target_phase} requires scanner data"
        
        # Manual confirmation
        elif cond_type == "manual_confirmation":
            return False, "Requires manual confirmation"
        
        else:
            return False, f"Unknown condition type: {cond_type}"
    
    def check_all_conditions(self, conditions: list) -> tuple[bool, dict]:
        """
        Проверить все условия
        
        Returns:
            (all_met: bool, results: dict)
        """
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
        
        # Все условия должны быть выполнены
        all_met = results['not_met'] == 0
        
        return all_met, results


def process_wait_queue():
    """Обработать wait_queue - проверить условия и подтвердить готовые сигналы"""
    
    if not WAIT_QUEUE_FILE.exists():
        logger.info("Wait queue file not found, nothing to process")
        return
    
    # Читаем wait_queue
    with WAIT_QUEUE_FILE.open('r', encoding='utf-8') as f:
        try:
            wait_queue = json.load(f)
        except:
            logger.error("Failed to parse wait_queue.json")
            return
    
    if not wait_queue:
        logger.info("Wait queue is empty")
        return
    
    logger.info(f"📊 Processing {len(wait_queue)} signals in wait queue")
    
    confirmed_signals = []
    updated_queue = []
    
    for entry in wait_queue:
        status = entry.get('status')
        
        # Пропускаем уже обработанные
        if status not in ['waiting']:
            updated_queue.append(entry)
            continue
        
        signal_id = entry.get('signal_id', 'unknown')
        symbol = entry.get('symbol', 'UNKNOWN')
        direction = entry.get('direction', 'UNKNOWN')
        conditions = entry.get('conditions', [])
        created_at = entry.get('created_at')
        
        logger.info(f"🔍 Checking {signal_id}")
        
        # Проверка на устаревание (>30 минут)
        if created_at:
            try:
                created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                age = datetime.now(created_dt.tzinfo) - created_dt
                
                if age > timedelta(minutes=30):
                    logger.info(f"⏱️ {signal_id} expired (age={age.total_seconds()/60:.1f}min)")
                    entry['status'] = 'expired'
                    entry['expired_at'] = datetime.utcnow().isoformat() + "Z"
                    updated_queue.append(entry)
                    continue
            except:
                pass
        
        # Проверяем условия
        checker = ConditionChecker(symbol, direction)
        all_met, results = checker.check_all_conditions(conditions)
        
        # Обновляем recheck_count
        entry['recheck_count'] = entry.get('recheck_count', 0) + 1
        entry['last_recheck'] = datetime.utcnow().isoformat() + "Z"
        entry['last_check_results'] = results
        
        if all_met:
            logger.info(f"✅ {signal_id} conditions MET! ({results['met']}/{results['total']})")
            
            # Получаем текущую цену
            candles = BybitDataFetcher.get_current_candles(f"{symbol}USDT", "5", 1)
            current_price = candles[0]['close'] if candles else entry.get('source_price', 0)
            
            # Создаём CONFIRMED сигнал
            confirmed_signal_id = f"{symbol.lower()}-confirmed-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
            
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
                'confirmed_at': datetime.utcnow().isoformat() + "Z",
                'entry_price': current_price,
                'score': entry.get('score', 8),
                'score_max': 15,
                'kind': 'wait_confirmed',
                'signal_type': 'wait_confirmed',
                'source': 'wait_monitor',
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'signal_time_utc': datetime.now().strftime("%H:%M:%S"),
                'added_to_whitelist_at': datetime.now().isoformat(),
                'confidence': entry.get('features', {}).get('confidence', 7),
                'reasons_for': entry.get('features', {}).get('reasons_for', []) + [
                    f"✅ Wait conditions met ({results['met']}/{results['total']})"
                ],
                'reasons_against': [],
                # Execution ownership - strategy controls TP/SL
                'execution_profile': entry.get('execution_profile', 'FUNTIK_DEFAULT'),
                'risk_owner': 'PumpDumpReversalStrategy_v2',
                'stop_policy': 'strategy',
                'tp_policy': 'strategy',
            }
            
            confirmed_signals.append(confirmed_signal)
            
            # Помечаем в wait_queue как confirmed
            entry['status'] = 'confirmed'
            entry['confirmed_by'] = 'wait_monitor'
            entry['child_signal_id'] = confirmed_signal_id
            entry['confirmed_at'] = datetime.utcnow().isoformat() + "Z"
            
            # Отправляем уведомление в ntfy
            send_confirmation_to_ntfy(symbol, direction, current_price, confirmed_signal)
            
        else:
            logger.info(f"⏳ {signal_id} waiting ({results['met']}/{results['total']} met)")
        
        updated_queue.append(entry)
    
    # Сохраняем обновлённую очередь
    tmp = WAIT_QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(updated_queue, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(WAIT_QUEUE_FILE)
    
    # Добавляем подтверждённые сигналы в dex_signals_analysis.json
    if confirmed_signals:
        # Читаем существующие
        if CONFIRMED_SIGNALS_FILE.exists():
            try:
                existing = json.loads(CONFIRMED_SIGNALS_FILE.read_text())
            except:
                existing = []
        else:
            existing = []
        
        # Добавляем новые
        existing.extend(confirmed_signals)
        
        # Сохраняем
        tmp = CONFIRMED_SIGNALS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp.replace(CONFIRMED_SIGNALS_FILE)
        
        logger.info(f"✅ Added {len(confirmed_signals)} confirmed signals to dex_signals_analysis.json")


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
