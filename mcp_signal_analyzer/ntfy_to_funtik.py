#!/usr/bin/env python3
"""
Читает сигналы из ntfy max-analysis и записывает в формат для Фунтика
Только сигналы с вердиктом ВХОДИТЬ/ШОРТИТЬ
"""
import requests
import json
import time
import logging
from pathlib import Path
from datetime import datetime
import re

NTFY_URL = "http://87.121.218.4:8080/max-analysis/json"
OUTPUT_FILE = Path("/home/max/o_p/dex_scanner/data/dex_signals_analysis.json")
WAIT_QUEUE_FILE = Path("/home/max/freqtrade/user_data/pump_dump_strategy/wait_queue.json")
LOG_FILE = Path("/home/max/freqtrade/logs/ntfy_to_funtik.log")

LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("ntfy-to-funtik")


def parse_wait_conditions(recommendations: list, direction: str) -> list:
    """
    Парсит текстовые рекомендации в структурированные conditions для wait_queue
    
    Примеры:
    - "⏳ Ждать: RSI упадёт <50" -> {"type": "rsi_below", "threshold": 50}
    - "1️⃣ MACD развернётся на bullish" -> {"type": "indicator_match", "indicator": "vw_macd", "value": "bullish"}
    """
    conditions = []
    
    for rec in recommendations:
        rec_lower = rec.lower()
        
        # RSI условия
        if 'rsi' in rec_lower:
            import re
            # RSI < X или RSI упадёт < X
            match = re.search(r'rsi.*?[<≤].*?(\d+)', rec_lower)
            if match:
                threshold = int(match.group(1))
                conditions.append({"type": "rsi_below", "threshold": threshold})
                continue
            
            # RSI > X или RSI поднимется > X
            match = re.search(r'rsi.*?[>≥].*?(\d+)', rec_lower)
            if match:
                threshold = int(match.group(1))
                conditions.append({"type": "rsi_above", "threshold": threshold})
                continue
        
        # MACD условия
        if 'macd' in rec_lower:
            if 'bullish' in rec_lower and direction == 'LONG':
                conditions.append({"type": "indicator_match", "indicator": "vw_macd", "value": "bullish"})
            elif 'bearish' in rec_lower and direction == 'SHORT':
                conditions.append({"type": "indicator_match", "indicator": "vw_macd", "value": "bearish"})
        
        # Lorentzian условия
        if 'lorentzian' in rec_lower or 'lor' in rec_lower:
            if direction == 'LONG' and ('+1' in rec or '+3' in rec):
                conditions.append({"type": "indicator_match", "indicator": "lorentzian", "value": "+1"})
            elif direction == 'SHORT' and ('-1' in rec or '-3' in rec):
                conditions.append({"type": "indicator_match", "indicator": "lorentzian", "value": "-1"})
        
        # Volume spike условия
        if 'volume' in rec_lower and 'spike' in rec_lower:
            import re
            match = re.search(r'>.*?(\d+\.?\d*)', rec_lower)
            if match:
                threshold = float(match.group(1))
                conditions.append({"type": "volume_spike_above", "threshold": threshold})
        
        # Свеча bullish/bearish
        if direction == 'LONG' and 'bullish' in rec_lower and 'свеч' in rec_lower:
            conditions.append({"type": "candle_bullish"})
        elif direction == 'SHORT' and 'bearish' in rec_lower and 'свеч' in rec_lower:
            conditions.append({"type": "candle_bearish"})
        
        # "RSI начнёт расти" или "RSI растёт"
        if 'rsi' in rec_lower and ('начнёт расти' in rec_lower or 'растёт' in rec_lower or 'рост' in rec_lower):
            conditions.append({"type": "rsi_rising"})
        
        # "Все индикаторы bullish" или "все индикаторы ЗА"
        if 'все индикаторы' in rec_lower and ('bullish' in rec_lower or 'за' in rec_lower):
            if direction == 'LONG':
                conditions.append({"type": "all_indicators_bullish"})
            elif direction == 'SHORT':
                conditions.append({"type": "all_indicators_bearish"})
    
    return conditions


def parse_ntfy_message(message: str, title: str) -> dict:
    """
    Парсит сообщение из ntfy и извлекает данные сигнала
    
    Пример title: "📊 BIO SHORT | ✅ ШОРТИТЬ (8/10)"
    Пример message содержит:
        💰 Цена: $0.02384
        📈 Score: 9/15
        ...
    """
    # Парсим title
    # Формат 1: "📊 SYMBOL DIRECTION | VERDICT" или "📊 SYMBOL DIRECTION | VERDICT (confidence/10)"
    # Формат 2: "✅ SYMBOL DIRECTION | WAIT CONFIRMED" (подтверждение из wait_queue)
    
    # Пробуем формат WAIT CONFIRMED
    wait_confirmed_match = re.search(r'✅\s+(\w+)\s+(LONG|SHORT)\s+\|\s+WAIT\s+CONFIRMED', title)
    if wait_confirmed_match:
        symbol = wait_confirmed_match.group(1)
        direction = wait_confirmed_match.group(2)
        
        # Парсим entry price и confidence из message
        price_match = re.search(r'💰 Entry:\s*\$([0-9.]+)', message)
        confidence_match = re.search(r'📊 Confidence:\s*([0-9]+)/10', message)
        score_match = re.search(r'📈 Score:\s*([0-9]+)/15', message)
        
        if not price_match:
            logger.warning(f"Could not parse entry price from WAIT CONFIRMED: {message[:100]}")
            return None
        
        price = float(price_match.group(1))
        confidence = int(confidence_match.group(1)) if confidence_match else 7
        score = int(score_match.group(1)) if score_match else 0
        
        # WAIT CONFIRMED всегда идет на исполнение
        signal = {
            "coin": symbol,
            "pair": f"{symbol}/USDT:USDT",
            "direction": direction,
            "kind": "analyzed",
            "signal_type": "wait_confirmed",
            "source": "wait_monitor",
            "score": score,
            "score_max": 15,
            "entry_price": price,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_time_utc": datetime.now().strftime("%H:%M:%S"),
            "added_to_whitelist_at": datetime.now().isoformat(),
            "confidence": confidence,
            "verdict": "WAIT_CONFIRMED",
        }
        logger.info(f"✅ WAIT CONFIRMED: {symbol} {direction} @ ${price} (confidence={confidence}/10)")
        return {"type": "entry", "data": signal}
    
    # Обычный формат
    title_match = re.search(r'📊\s+(\w+)\s+(LONG|SHORT)\s+\|\s+(✅|⏳|🚫)\s*(\w+)(?:\s*\(([0-9-]+)/10\))?', title)
    if not title_match:
        return None
    
    symbol = title_match.group(1)
    direction = title_match.group(2)
    verdict_emoji = title_match.group(3)
    verdict = title_match.group(4)
    confidence_str = title_match.group(5)
    # FIX (22.09.2026): Если confidence НЕ указан в title - берём из вердикта!
    # ВХОДИТЬ/ШОРТИТЬ без confidence = минимальный порог для входа (6/10)
    # ЖДАТЬ без confidence = ниже порога (5/10)
    if confidence_str:
        confidence = int(confidence_str)
    elif verdict in ['ВХОДИТЬ', 'ШОРТИТЬ']:
        confidence = 6  # default для ВХОДИТЬ/ШОРТИТЬ = 6/10 (минимальный порог)
    else:
        confidence = 5  # default для ЖДАТЬ = 5/10 (ниже порога)
    
    # Парсим message
    price_match = re.search(r'💰 Цена:\s*\$([0-9.]+)', message)
    score_match = re.search(r'📈 Score:\s*([0-9]+)/15', message)
    phase_match = re.search(r'Phase:\s*(\w+)', message)
    
    if not price_match or not score_match:
        logger.warning(f"Could not parse price/score from message: {message[:100]}")
        return None
    
    price = float(price_match.group(1))
    score = int(score_match.group(1))
    phase = phase_match.group(1) if phase_match else None
    
    # Извлекаем рекомендации из message (для парсинга условий)
    lines = message.split('\n')
    recommendations = []
    for line in lines:
        line = line.strip()
        if line.startswith('⏳') or line.startswith('1️⃣') or line.startswith('2️⃣') or line.startswith('3️⃣') or line.startswith('4️⃣'):
            recommendations.append(line)
    
    # Создаём базовый объект сигнала
    signal_base = {
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "score": score,
        "confidence": confidence,
        "verdict": verdict,
        "phase": phase,
        "timestamp": datetime.now().isoformat(),
        "message": message,
        "recommendations": recommendations  # Для справки
    }
    
    # Сигналы "ЖДАТЬ" → в wait_queue
    if "ЖДАТЬ" in verdict or verdict_emoji == "⏳":
        # Парсим условия из рекомендаций
        conditions = parse_wait_conditions(recommendations, direction)
        signal_base['conditions'] = conditions
        logger.info(f"⏳ WAIT signal: {symbol} {direction} → wait_queue (conditions={len(conditions)})")
        return {"type": "wait", "data": signal_base}
    
    # FIX (16.09.2026): Confidence < 6 = ЖДАТЬ, не ВХОДИТЬ!
    # Баг: сигналы с confidence=5 попадали в Фунтик с verdict="ВХОДИТЬ"
    if confidence < 6:
        # Парсим условия из рекомендаций
        conditions = parse_wait_conditions(recommendations, direction)
        signal_base['conditions'] = conditions
        logger.info(f"⏳ LOW confidence {confidence}/10: {symbol} {direction} → wait_queue (автоперевод в ЖДАТЬ, conditions={len(conditions)})")
        signal_base['verdict'] = 'ЖДАТЬ'
        signal_base['original_verdict'] = verdict
        signal_base['wait_reason'] = f'Низкая уверенность {confidence}/10 < 6'
        return {"type": "wait", "data": signal_base}
    
    # Сигналы НЕ ВХОДИТЬ → игнорируем
    if "НЕ" in verdict or verdict_emoji == "🚫":
        logger.debug(f"Skipping {symbol} {direction} - verdict={verdict}")
        return None
    
    # Сигналы ВХОДИТЬ/ШОРТИТЬ → в основную очередь
    if verdict in ['ВХОДИТЬ', 'ШОРТИТЬ']:
        signal = {
            "coin": symbol,
            "pair": f"{symbol}/USDT:USDT",
            "direction": direction,
            "kind": "analyzed",
            "signal_type": "analyzed",
            "source": "max-analysis",
            "score": score,
            "score_max": 15,
            "entry_price": price,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "signal_time_utc": datetime.now().strftime("%H:%M:%S"),
            "added_to_whitelist_at": datetime.now().isoformat(),
            "confidence": confidence,
            "verdict": verdict,
        }
        return {"type": "entry", "data": signal}
    
    return None


def fetch_latest_signals(since_id: int = 0) -> list:
    """Получает последние сообщения из ntfy"""
    try:
        params = {'poll': '1', 'since': str(since_id)}
        response = requests.get(NTFY_URL, params=params, timeout=10)
        response.raise_for_status()
        
        messages = []
        for line in response.text.strip().split('\n'):
            if not line:
                continue
            try:
                msg = json.loads(line)
                messages.append(msg)
            except json.JSONDecodeError:
                pass
        
        return messages
    except Exception as e:
        logger.error(f"Failed to fetch from ntfy: {e}")
        return []


def update_signals_file(new_signals: list):
    """Обновляет файл сигналов для Фунтика"""
    # Читаем существующие сигналы
    existing = []
    if OUTPUT_FILE.exists():
        try:
            with OUTPUT_FILE.open('r') as f:
                existing = json.load(f)
        except:
            existing = []
    
    # Фильтруем дубликаты в new_signals по symbol+direction
    seen = set()
    unique_new = []
    for sig in new_signals:
        key = (sig.get('coin'), sig.get('direction'))
        if key not in seen:
            seen.add(key)
            unique_new.append(sig)
    
    # Добавляем только уникальные новые сигналы
    existing.extend(unique_new)
    
    # Оставляем только последние 50 сигналов
    existing = existing[-200:]
    
    # Записываем обратно
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open('w') as f:
        json.dump(existing, f, indent=2)
    
    logger.info(f"Updated signals file with {len(unique_new)} new signals (filtered {len(new_signals) - len(unique_new)} duplicates)")


def update_wait_queue(wait_signals: list):
    """Обновляет очередь ЖДАТЬ"""
    # Читаем существующую очередь
    existing = []
    if WAIT_QUEUE_FILE.exists():
        try:
            with WAIT_QUEUE_FILE.open('r') as f:
                existing = json.load(f)
        except:
            existing = []
    
    # Добавляем новые сигналы с полем added_at и status
    for signal in wait_signals:
        signal['added_at'] = datetime.now().isoformat()
        # 🔥 FIX: Добавляем обязательные поля для wait_queue_monitor
        signal['status'] = 'waiting'
        if 'signal_id' not in signal:
            # Генерируем signal_id из данных
            timestamp = signal.get('timestamp', datetime.now().isoformat()).replace(':', '').replace('-', '').replace('+', '').replace('.', '')[:14]
            signal['signal_id'] = f"{signal['symbol']}_{signal['direction']}_{timestamp}"
    
    # Объединяем и удаляем дубликаты (по symbol+direction), оставляем САМЫЙ СВЕЖИЙ
    combined = existing + wait_signals
    
    # Сортируем по timestamp (самые новые в конце)
    combined.sort(key=lambda x: x.get('timestamp', ''))
    
    # Дедупликация: оставляем ПОСЛЕДНИЙ (самый свежий) сигнал для каждой пары symbol+direction
    # 🔥 FIX: НЕ удаляем старые сигналы с status != 'waiting' (confirmed, expired)
    seen = {}
    keep_old = []  # Старые сигналы НЕ в статусе waiting
    
    for sig in combined:
        status = sig.get('status', 'waiting')
        
        # Если сигнал НЕ waiting - сохраняем как есть (не дедуплицируем)
        if status != 'waiting':
            keep_old.append(sig)
            continue
        
        # Для waiting сигналов - дедупликация по symbol+direction
        key = (sig['symbol'], sig['direction'])
        seen[key] = sig  # перезаписываем старые новыми
    
    unique = keep_old + list(seen.values())
    
    # Сохраняем
    WAIT_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with WAIT_QUEUE_FILE.open('w') as f:
        json.dump(unique, f, indent=2)
    
    logger.info(f"Updated wait queue with {len(wait_signals)} new signals (total={len(unique)})")


def main():
    logger.info("Starting ntfy-to-funtik bridge...")
    
    last_id = 0
    seen_ids = set()
    
    while True:
        try:
            messages = fetch_latest_signals(since_id=last_id)
            
            if not messages:
                time.sleep(5)
                continue
            
            # Фильтруем только новые сообщения
            new_messages = []
            for msg in messages:
                msg_id = msg.get('id', '')
                if msg_id and msg_id not in seen_ids:
                    new_messages.append(msg)
                    seen_ids.add(msg_id)
                    # Обновляем last_id для API
                    if isinstance(msg_id, str):
                        try:
                            last_id = max(last_id, int(msg_id))
                        except:
                            pass
            
            if not new_messages:
                time.sleep(5)
                continue
            
            logger.info(f"Received {len(new_messages)} NEW messages from ntfy (total fetched: {len(messages)})")
            
            new_signals = []
            wait_signals = []
            for msg in new_messages:
                # FIX: msg['id'] может быть string, приводим к int
                msg_id = msg.get('id', 0)
                if isinstance(msg_id, str):
                    try:
                        msg_id = int(msg_id)
                    except (ValueError, TypeError):
                        msg_id = 0
                
                last_id = max(last_id, msg_id)
                
                title = msg.get('title', '')
                message = msg.get('message', '')
                
                if not title or not message:
                    continue
                
                result = parse_ntfy_message(message, title)
                if result:
                    if result['type'] == 'entry':
                        signal = result['data']
                        new_signals.append(signal)
                        logger.info(f"✅ Parsed signal: {signal['coin']} {signal['direction']} @ ${signal['entry_price']} (confidence={signal['confidence']}/10)")
                    elif result['type'] == 'wait':
                        wait_signal = result['data']
                        wait_signals.append(wait_signal)
                        logger.info(f"⏳ Parsed WAIT signal: {wait_signal['symbol']} {wait_signal['direction']} @ ${wait_signal['price']} (confidence={wait_signal['confidence']}/10)")
            
            if new_signals:
                update_signals_file(new_signals)
            
            if wait_signals:
                update_wait_queue(wait_signals)
            
            time.sleep(2)
            
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
