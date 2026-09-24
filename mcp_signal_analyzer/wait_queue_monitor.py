#!/usr/bin/env python3
"""
МОНИТОРИНГ ОЧЕРЕДИ "⏳ ЖДАТЬ"
Следит за сигналами с вердиктом ЖДАТЬ и автоматически переводит их в ВХОДИТЬ
когда выполнены условия
"""
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
import ccxt

# Paths
WAIT_QUEUE_FILE = Path("/home/max/freqtrade/user_data/pump_dump_strategy/wait_queue.json")
SIGNALS_FILE = Path("/home/max/o_p/dex_scanner/data/dex_signals_analysis.json")  # FIX: Фунтик читает отсюда!
LOG_FILE = Path("/home/max/freqtrade/logs/wait_queue_monitor.log")

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
WAIT_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("wait-queue-monitor")

# Инициализация биржи
try:
    exchange = ccxt.bybit({
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    logger.info("✅ Bybit exchange initialized")
except Exception as e:
    logger.error(f"❌ Failed to initialize exchange: {e}")
    exchange = None


def load_wait_queue() -> list:
    """Загружает очередь ЖДАТЬ"""
    if not WAIT_QUEUE_FILE.exists():
        return []
    
    try:
        with open(WAIT_QUEUE_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"❌ Failed to load wait queue: {e}")
        return []


def save_wait_queue(queue: list):
    """Сохраняет очередь ЖДАТЬ"""
    try:
        with open(WAIT_QUEUE_FILE, 'w') as f:
            json.dump(queue, f, indent=2)
    except Exception as e:
        logger.error(f"❌ Failed to save wait queue: {e}")


def add_to_signals(signal: dict):
    """Добавляет сигнал в файл для Фунтика"""
    try:
        signals = []
        if SIGNALS_FILE.exists():
            with open(SIGNALS_FILE, 'r') as f:
                try:
                    signals = json.load(f)
                except:
                    signals = []
        
        # Добавляем новый сигнал
        signals.append(signal)
        
        # Сохраняем
        with open(SIGNALS_FILE, 'w') as f:
            json.dump(signals, f, indent=2)
        
        logger.info(f"✅ Added signal to funtik: {signal['coin']} {signal['direction']}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to add signal: {e}")
        return False


def get_current_market_data(symbol: str) -> dict:
    """Получает текущие рыночные данные + индикаторы + volume spike"""
    if not exchange:
        return None
    
    # BLACKLIST: Монеты недоступные на Bybit
    BYBIT_BLACKLIST = {'PEPE', 'FLOKI', '1000PEPE', '1000FLOKI'}
    if symbol in BYBIT_BLACKLIST:
        logger.warning(f"⚠️ {symbol} в blacklist (недоступен на Bybit)")
        return None
    
    try:
        # Формат для Bybit: SYMBOL/USDT:USDT
        pair = f"{symbol}/USDT:USDT"
        
        # Получаем OHLCV для расчета RSI, EMA и volume spike
        ohlcv_5m = exchange.fetch_ohlcv(pair, '5m', limit=30)
        ohlcv_1h = exchange.fetch_ohlcv(pair, '1h', limit=30)
        
        if not ohlcv_5m or len(ohlcv_5m) < 21:
            return None
        
        # Расчет RSI (упрощенный)
        closes_5m = [x[4] for x in ohlcv_5m]
        closes_1h = [x[4] for x in ohlcv_1h] if ohlcv_1h and len(ohlcv_1h) >= 14 else closes_5m
        
        rsi_5m = calculate_rsi(closes_5m, period=14)
        rsi_1h = calculate_rsi(closes_1h, period=14)
        
        # Текущая цена
        current_price = closes_5m[-1]
        
        # EMA9 и EMA21 (5m)
        ema9 = calculate_ema(closes_5m, period=9)
        ema21 = calculate_ema(closes_5m, period=21)
        ema_bullish = ema9 > ema21
        
        # MACD histogram approximation (12,26,9)
        ema12 = calculate_ema(closes_5m, period=12)
        ema26 = calculate_ema(closes_5m, period=26)
        macd_line = ema12 - ema26
        
        # Signal line (EMA9 of MACD)
        macd_values = []
        for i in range(9, len(closes_5m)):
            e12 = calculate_ema(closes_5m[:i+1], period=12)
            e26 = calculate_ema(closes_5m[:i+1], period=26)
            macd_values.append(e12 - e26)
        
        signal_line = calculate_ema(macd_values, period=9) if len(macd_values) >= 9 else macd_line
        macd_histogram = macd_line - signal_line
        macd_bullish = macd_histogram > 0
        
        # 🔥 VOLUME SPIKE расчёт
        # Берём последние 20 свечей для расчёта среднего объёма
        volumes = [x[5] for x in ohlcv_5m[-20:]]
        current_volume = volumes[-1]
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else current_volume
        volume_spike = current_volume / avg_volume if avg_volume > 0 else 1.0
        
        # Проверка тренда
        prev_close = closes_5m[-2]
        is_bullish = current_price > prev_close
        
        # Получаем последнюю свечу
        last_candle = ohlcv_5m[-1]
        candle_is_bullish = last_candle[4] > last_candle[1]  # close > open
        
        return {
            'rsi': rsi_5m,
            'rsi_1h': rsi_1h,
            'rsi_prev': calculate_rsi(closes_5m[:-1], period=14),
            'price': current_price,
            'is_bullish': is_bullish,
            'candle_bullish': candle_is_bullish,
            'ema_bullish': ema_bullish,
            'macd_bullish': macd_bullish,
            'macd_histogram': macd_histogram,
            'volume_spike': volume_spike,  # ДОБАВЛЕНО
            'updated_at': datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"❌ Failed to get market data for {symbol}: {e}")
        return None


def calculate_rsi(prices: list, period: int = 14) -> float:
    """Расчет RSI"""
    if len(prices) < period + 1:
        return 50.0  # Нейтральное значение
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def calculate_ema(prices: list, period: int) -> float:
    """Расчет EMA"""
    if len(prices) < period:
        return prices[-1]
    
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # SMA для начала
    
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    
    return ema


def check_wait_conditions(wait_signal: dict, market_data: dict) -> tuple:
    """
    Проверяет условия из message сигнала "ЖДАТЬ"
    
    FIX (19.09.2026): 
    1. Парсим ТОЛЬКО секцию "дождись подтверждения:" (не весь message!)
    2. Требуем ВЫПОЛНЕНИЯ ВСЕХ условий (счётчик [met/required])
    3. VWAP учитывается - если требуется, сигнал НЕ активируется (нет данных)
    
    Парсит условия вида:
    - "1️⃣ RSI < 35 (перепродан)" → проверяем RSI < 35
    - "2️⃣ Bullish свеча (close > prev)" → проверяем свеча bullish
    - "3️⃣ VW-MACD bullish" → проверяем MACD bullish
    - "4️⃣ VWAP пробой вверх" → НЕ МОЖЕМ проверить (нет данных) - сигнал блокируется
    - "Volume spike >1.5x" → проверяем volume_spike >= 1.5
    
    ВАЖНО: 
    - Проверяем ТОЛЬКО условия из секции "дождись подтверждения:", НЕ из "✅ ЗА:"
    - Требуются ВСЕ условия одновременно (не ЛЮБОЕ из них)
    - Логика: каждое условие увеличивает required_conditions и проверяется
    
    Returns: (ready: bool, reason: str)
        ready=True - все условия выполнены
        reason - строка вида "✅ LONG READY [3/3]: RSI=32<35 MACD✅ Свеча✅"
                 или "⏳ RSI=45.0 >= 35.0 (ждём < 35.0) [0/1]"
    """
    direction = wait_signal['direction']
    symbol = wait_signal.get('coin') or wait_signal.get('symbol')
    message = wait_signal.get('message', '')
    
    rsi = market_data['rsi']
    rsi_prev = market_data.get('rsi_prev', rsi)
    ema_bullish = market_data.get('ema_bullish', None)
    macd_bullish = market_data.get('macd_bullish', None)
    volume_spike = market_data.get('volume_spike', 1.0)
    is_bullish = market_data.get('is_bullish', None)  # Bullish свеча
    
    reasons = []
    required_conditions = 0  # Сколько условий требуется из message
    met_conditions = 0  # Сколько условий выполнено
    
    # Парсим условия из message (новый формат)
    import re
    
    # КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: парсим ТОЛЬКО условия после "дождись подтверждения:"
    # Игнорируем секцию "✅ ЗА:" которая просто описывает сигнал!
    conditions_section = ""
    if "дождись подтверждения:" in message:
        conditions_section = message.split("дождись подтверждения:")[1]
    elif "Ждать:" in message:
        conditions_section = message.split("Ждать:")[1]
    else:
        # Если нет явной секции условий - используем весь message
        conditions_section = message
    
    # RSI условия: "RSI < 35", "RSI упадёт <61.8", "RSI поднимется >38.2"
    rsi_less_match = re.search(r'RSI\s*<\s*(\d+\.?\d*)', conditions_section)
    rsi_more_match = re.search(r'RSI\s*>\s*(\d+\.?\d*)', conditions_section)
    
    # EMA условия: "EMA bullish", "EMA bearish"
    ema_bullish_required = 'EMA bullish' in conditions_section
    ema_bearish_required = 'EMA bearish' in conditions_section
    
    # MACD условия: "VW-MACD bullish", "VW-MACD bearish", "MACD bullish"
    macd_bullish_required = 'MACD bullish' in conditions_section or 'VW-MACD bullish' in conditions_section
    macd_bearish_required = 'MACD bearish' in conditions_section or 'VW-MACD bearish' in conditions_section
    
    # Volume spike: "Volume spike >1.5x", "volume spike"
    volume_match = re.search(r'[Vv]olume spike\s*>\s*(\d+\.?\d*)x?', conditions_section)
    
    # Свеча: "Bullish свеча", "Bearish свеча"
    bullish_candle_required = 'Bullish свеча' in conditions_section or 'bullish свеча' in conditions_section
    bearish_candle_required = 'Bearish свеча' in conditions_section or 'bearish свеча' in conditions_section
    
    # VWAP: "VWAP пробой вверх", "VWAP пробой вниз"
    # Пока не можем проверить (нет данных), но считаем как required condition
    vwap_up_required = 'VWAP пробой вверх' in conditions_section or 'VWAP вверх' in conditions_section
    vwap_down_required = 'VWAP пробой вниз' in conditions_section or 'VWAP вниз' in conditions_section
    
    # VWAP: "VWAP пробой вверх", "VWAP пробой вниз"
    # Пока не можем проверить (нет данных), пропускаем
    
    # Проверяем условия для LONG
    if direction == "LONG":
        # Проверяем RSI < X
        if rsi_less_match:
            required_conditions += 1
            rsi_threshold = float(rsi_less_match.group(1))
            if rsi >= rsi_threshold:
                return False, f"⏳ RSI={rsi:.1f} >= {rsi_threshold} (ждём < {rsi_threshold}) [{met_conditions}/{required_conditions}]"
            reasons.append(f"RSI={rsi:.1f}<{rsi_threshold}")
            met_conditions += 1
        
        # Проверяем RSI > X
        if rsi_more_match:
            required_conditions += 1
            rsi_threshold = float(rsi_more_match.group(1))
            if rsi <= rsi_threshold:
                return False, f"⏳ RSI={rsi:.1f} <= {rsi_threshold} (ждём > {rsi_threshold}) [{met_conditions}/{required_conditions}]"
            reasons.append(f"RSI={rsi:.1f}>{rsi_threshold}")
            met_conditions += 1
        
        # Проверяем EMA bullish (ТОЛЬКО если в message)
        if ema_bullish_required:
            required_conditions += 1
            if ema_bullish is None or not ema_bullish:
                return False, f"❌ EMA не bullish [{met_conditions}/{required_conditions}]"
            reasons.append("EMA✅")
            met_conditions += 1
        
        # Проверяем MACD bullish (ТОЛЬКО если в message)
        if macd_bullish_required:
            required_conditions += 1
            if macd_bullish is None or not macd_bullish:
                return False, f"❌ MACD не bullish [{met_conditions}/{required_conditions}]"
            reasons.append("MACD✅")
            met_conditions += 1
        
        # Проверяем Bullish свеча (ТОЛЬКО если в message)
        if bullish_candle_required:
            required_conditions += 1
            if is_bullish is None or not is_bullish:
                return False, f"❌ Свеча не bullish [{met_conditions}/{required_conditions}]"
            reasons.append("Свеча✅")
            met_conditions += 1
        
        # Проверяем Volume spike (ТОЛЬКО если в message)
        if volume_match:
            required_conditions += 1
            vol_threshold = float(volume_match.group(1))
            if volume_spike < vol_threshold:
                return False, f"⏳ Volume={volume_spike:.2f}x<{vol_threshold}x [{met_conditions}/{required_conditions}]"
            reasons.append(f"Vol={volume_spike:.2f}x✅")
            met_conditions += 1
        
        # Проверяем VWAP пробой вверх (пока НЕ МОЖЕМ - нет данных!)
        # Если в условиях есть VWAP - НЕ активируем сигнал!
        if vwap_up_required:
            required_conditions += 1
            return False, f"⏳ VWAP условие не может быть проверено (нет данных) [{met_conditions}/{required_conditions}]"
        
        # ВСЕ условия должны быть выполнены!
        if required_conditions == 0:
            return False, f"⏳ Нет условий в message"
        
        if met_conditions < required_conditions:
            return False, f"⏳ Условия не выполнены [{met_conditions}/{required_conditions}]"
        
        return True, f"✅ LONG READY [{met_conditions}/{required_conditions}]: {' '.join(reasons)}"
    
    # Проверяем условия для SHORT
    elif direction == "SHORT":
        # Проверяем RSI > X
        if rsi_more_match:
            required_conditions += 1
            rsi_threshold = float(rsi_more_match.group(1))
            if rsi <= rsi_threshold:
                return False, f"⏳ RSI={rsi:.1f} <= {rsi_threshold} (ждём > {rsi_threshold}) [{met_conditions}/{required_conditions}]"
            reasons.append(f"RSI={rsi:.1f}>{rsi_threshold}")
            met_conditions += 1
        
        # Проверяем RSI < X
        if rsi_less_match:
            required_conditions += 1
            rsi_threshold = float(rsi_less_match.group(1))
            if rsi >= rsi_threshold:
                return False, f"⏳ RSI={rsi:.1f} >= {rsi_threshold} (ждём < {rsi_threshold}) [{met_conditions}/{required_conditions}]"
            reasons.append(f"RSI={rsi:.1f}<{rsi_threshold}")
            met_conditions += 1
        
        # Проверяем EMA bearish (ТОЛЬКО если в message)
        if ema_bearish_required:
            required_conditions += 1
            if ema_bullish is None or ema_bullish:
                return False, f"❌ EMA не bearish [{met_conditions}/{required_conditions}]"
            reasons.append("EMA✅")
            met_conditions += 1
        
        # Проверяем MACD bearish (ТОЛЬКО если в message)
        if macd_bearish_required:
            required_conditions += 1
            if macd_bullish is None or macd_bullish:
                return False, f"❌ MACD не bearish [{met_conditions}/{required_conditions}]"
            reasons.append("MACD✅")
            met_conditions += 1
        
        # Проверяем Bearish свеча (ТОЛЬКО если в message)
        if bearish_candle_required:
            required_conditions += 1
            if is_bullish is None or is_bullish:
                return False, f"❌ Свеча не bearish [{met_conditions}/{required_conditions}]"
            reasons.append("Свеча✅")
            met_conditions += 1
        
        # Проверяем Volume spike (ТОЛЬКО если в message)
        if volume_match:
            required_conditions += 1
            vol_threshold = float(volume_match.group(1))
            if volume_spike < vol_threshold:
                return False, f"⏳ Volume={volume_spike:.2f}x<{vol_threshold}x [{met_conditions}/{required_conditions}]"
            reasons.append(f"Vol={volume_spike:.2f}x✅")
            met_conditions += 1
        
        # Проверяем VWAP пробой вниз (пока НЕ МОЖЕМ - нет данных!)
        # Если в условиях есть VWAP - НЕ активируем сигнал!
        if vwap_down_required:
            required_conditions += 1
            return False, f"⏳ VWAP условие не может быть проверено (нет данных) [{met_conditions}/{required_conditions}]"
        
        # ВСЕ условия должны быть выполнены!
        if required_conditions == 0:
            return False, f"⏳ Нет условий в message"
        
        if met_conditions < required_conditions:
            return False, f"⏳ Условия не выполнены [{met_conditions}/{required_conditions}]"
        
        return True, f"✅ SHORT READY [{met_conditions}/{required_conditions}]: {' '.join(reasons)}"
    
    return False, "Unknown direction"


def process_wait_queue():
    """Обрабатывает очередь ЖДАТЬ"""
    queue = load_wait_queue()
    
    if not queue:
        logger.debug("Wait queue is empty")
        return
    
    logger.info(f"📋 Processing {len(queue)} signals in wait queue")
    
    updated_queue = []
    now = datetime.now()
    
    # BLACKLIST для Bybit
    BYBIT_BLACKLIST = {'PEPE', 'FLOKI', '1000PEPE', '1000FLOKI'}
    
    for wait_signal in queue:
        symbol = wait_signal.get('coin') or wait_signal.get('symbol')  # Поддержка обоих форматов
        direction = wait_signal['direction']
        added_at = datetime.fromisoformat(wait_signal['added_at'])
        age_minutes = (now - added_at).total_seconds() / 60
        
        # Удаляем blacklisted монеты
        if symbol in BYBIT_BLACKLIST:
            logger.info(f"🚫 Removing blacklisted: {symbol} {direction} (недоступен на Bybit)")
            continue
        
        # Удаляем старые сигналы (>30 мин)
        if age_minutes > 30:
            logger.info(f"⏱️ Removing expired signal: {symbol} {direction} (age={age_minutes:.1f}m)")
            continue
        
        # Получаем текущие данные рынка
        market_data = get_current_market_data(symbol)
        
        if not market_data:
            logger.warning(f"⚠️ Failed to get market data for {symbol}, keeping in queue")
            updated_queue.append(wait_signal)
            continue
        
        # Проверяем условия
        ready, reason = check_wait_conditions(wait_signal, market_data)
        
        if ready:
            logger.info(f"✅ {symbol} {direction} is READY: {reason}")
            
            # Создаем сигнал для Фунтика в ПРАВИЛЬНОМ ФОРМАТЕ (как max-analysis)
            signal = {
                'coin': symbol,  # FIX: "coin" вместо "symbol"
                'pair': f"{symbol}/USDT:USDT",  # FIX: добавляем "pair"
                'direction': direction.upper(),  # FIX: uppercase вместо lowercase
                'kind': 'wait_confirmed',
                'signal_type': 'wait_confirmed',
                'source': 'wait_queue',  # МЕТКА: сигнал из очереди ЖДАТЬ
                'score': wait_signal.get('score', 8),
                'score_max': 15,
                'entry_price': market_data['price'],  # FIX: "entry_price" вместо "price"
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # FIX: формат как у max-analysis
                'signal_time_utc': datetime.now().strftime("%H:%M:%S"),
                'added_to_whitelist_at': datetime.now().isoformat(),
                'confidence': wait_signal.get('confidence', 7),
                'verdict': 'ВХОДИТЬ' if direction == 'LONG' else 'ШОРТИТЬ',
                'original_verdict': 'ЖДАТЬ',
                'wait_time_minutes': age_minutes
            }
            
            # Добавляем в очередь Фунтика
            if add_to_signals(signal):
                logger.info(f"🎯 CONVERTED TO ENTRY: {symbol} {direction} after {age_minutes:.1f}m wait")
            else:
                # Если не удалось добавить - оставляем в очереди
                updated_queue.append(wait_signal)
        else:
            logger.debug(f"⏳ {symbol} {direction} NOT READY: {reason}")
            updated_queue.append(wait_signal)
    
    # Сохраняем обновленную очередь
    save_wait_queue(updated_queue)
    
    if len(updated_queue) < len(queue):
        logger.info(f"📊 Queue updated: {len(queue)} → {len(updated_queue)} signals")


def main():
    """Основной цикл мониторинга"""
    logger.info("🚀 Wait Queue Monitor started")
    logger.info(f"📁 Wait queue file: {WAIT_QUEUE_FILE}")
    logger.info(f"📁 Signals file: {SIGNALS_FILE}")
    logger.info(f"⏱️ Check interval: 30 seconds")
    
    while True:
        try:
            process_wait_queue()
            time.sleep(30)  # Проверка каждые 30 секунд
        except KeyboardInterrupt:
            logger.info("👋 Shutting down wait queue monitor")
            break
        except Exception as e:
            logger.error(f"❌ Error in main loop: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
