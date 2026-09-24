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
    # Формат: "📊 SYMBOL DIRECTION | VERDICT" или "📊 SYMBOL DIRECTION | VERDICT (confidence/10)"
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
        "message": message
    }
    
    # Сигналы "ЖДАТЬ" → в wait_queue
    if "ЖДАТЬ" in verdict or verdict_emoji == "⏳":
        logger.info(f"⏳ WAIT signal: {symbol} {direction} → wait_queue")
        return {"type": "wait", "data": signal_base}
    
    # FIX (16.09.2026): Confidence < 6 = ЖДАТЬ, не ВХОДИТЬ!
    # Баг: сигналы с confidence=5 попадали в Фунтик с verdict="ВХОДИТЬ"
    if confidence < 6:
        logger.info(f"⏳ LOW confidence {confidence}/10: {symbol} {direction} → wait_queue (автоперевод в ЖДАТЬ)")
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
    existing = existing[-50:]
    
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
    
    # Добавляем новые сигналы с полем added_at
    for signal in wait_signals:
        signal['added_at'] = datetime.now().isoformat()
    
    # Объединяем и удаляем дубликаты (по symbol+direction), оставляем САМЫЙ СВЕЖИЙ
    combined = existing + wait_signals
    
    # Сортируем по timestamp (самые новые в конце)
    combined.sort(key=lambda x: x.get('timestamp', ''))
    
    # Дедупликация: оставляем ПОСЛЕДНИЙ (самый свежий) сигнал для каждой пары symbol+direction
    seen = {}
    for sig in combined:
        key = (sig['symbol'], sig['direction'])
        seen[key] = sig  # перезаписываем старые новыми
    
    unique = list(seen.values())
    
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
