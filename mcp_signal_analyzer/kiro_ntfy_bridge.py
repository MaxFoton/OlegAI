#!/usr/bin/env python3
"""
Bridge between ntfy and Kiro
Reads ntfy max-alerts and writes analysis instructions for Kiro to pick up
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("kiro-ntfy-bridge")

# Configuration
NTFY_SERVER = "http://87.121.218.4:8080"
NTFY_SUBSCRIBE_TOPIC = "max-alerts"
MIN_SCORE = 5  # Lowered from 7 to 5 (11.08.2026)
CHECK_INTERVAL = 60  # 60 секунд для избежания rate limit ntfy

# Signal queue file for Kiro to read
SIGNAL_QUEUE_FILE = Path("/tmp/kiro_signal_queue.json")
PROCESSED_IDS_FILE = Path("/tmp/kiro_processed_signals.txt")


def load_processed_ids() -> set:
    """Load list of already processed signal IDs"""
    if PROCESSED_IDS_FILE.exists():
        return set(PROCESSED_IDS_FILE.read_text().strip().split('\n'))
    return set()


def save_processed_id(msg_id: str):
    """Mark signal as processed"""
    with PROCESSED_IDS_FILE.open('a') as f:
        f.write(f"{msg_id}\n")


def extract_score(text: str) -> int:
    """Extract score from signal text"""
    match = re.search(r'score=([\d]+)/[\d]+', text)
    if match:
        return int(match.group(1))
    return 0


async def fetch_ntfy_messages(last_id: str = None) -> list[dict]:
    """Fetch latest messages from ntfy"""
    try:
        # Используем since=<id> для получения только новых сообщений после last_id
        if last_id:
            url = f"{NTFY_SERVER}/{NTFY_SUBSCRIBE_TOPIC}/json?since={last_id}"
        else:
            # Первый запуск - получаем все за последнюю минуту
            url = f"{NTFY_SERVER}/{NTFY_SUBSCRIBE_TOPIC}/json?since=1m"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            
            if response.status_code == 200:
                messages = []
                for line in response.text.strip().split('\n'):
                    if line:
                        try:
                            msg = json.loads(line)
                            # Пропускаем служебные сообщения (open/keepalive)
                            if msg.get('event') == 'message':
                                messages.append(msg)
                        except json.JSONDecodeError:
                            continue
                return messages
            else:
                logger.error(f"Failed to fetch: {response.status_code}")
                return []
                
    except httpx.ReadTimeout:
        logger.debug("Read timeout (normal, no new messages)")
        return []
    except Exception as e:
        logger.error(f"Error fetching: {e}")
        return []


async def add_to_queue(signal_text: str, score: int):
    """Add signal to queue for Kiro to analyze"""
    # Load existing queue
    if SIGNAL_QUEUE_FILE.exists():
        try:
            queue = json.loads(SIGNAL_QUEUE_FILE.read_text())
        except:
            queue = []
    else:
        queue = []
    
    # Add new signal
    queue.append({
        'timestamp': datetime.utcnow().isoformat(),
        'score': score,
        'signal_text': signal_text,
        'status': 'pending'
    })
    
    # Save queue
    SIGNAL_QUEUE_FILE.write_text(json.dumps(queue, indent=2))
    logger.info(f"✅ Signal added to queue (score={score})")


async def main():
    """Main loop"""
    logger.info("🚀 Starting Kiro-NTFY bridge")
    logger.info(f"📡 Monitoring: {NTFY_SERVER}/{NTFY_SUBSCRIBE_TOPIC}")
    logger.info(f"📊 Min score: {MIN_SCORE}/15")
    logger.info(f"💾 Queue file: {SIGNAL_QUEUE_FILE}")
    
    processed_ids = load_processed_ids()
    logger.info(f"📝 Already processed: {len(processed_ids)} signals")
    
    last_message_id = None  # Отслеживаем последнее сообщение
    
    while True:
        try:
            # Fetch messages after last_message_id
            messages = await fetch_ntfy_messages(last_message_id)
            
            for msg in messages:
                msg_id = msg.get('id', '')
                msg_text = msg.get('message', '')
                
                # Update last_message_id
                if msg_id:
                    last_message_id = msg_id
                
                # Skip if already processed
                if msg_id in processed_ids:
                    continue
                
                # Check if it's a signal
                if not msg_text or '📢' not in msg_text:
                    continue
                
                # Extract score
                score = extract_score(msg_text)
                if score == 0:
                    logger.debug(f"No score found in message")
                    continue
                
                logger.info(f"📢 New signal: score={score}")
                
                # Check score threshold
                if score >= MIN_SCORE:
                    logger.info(f"✅ Score {score} >= {MIN_SCORE}, adding to queue")
                    await add_to_queue(msg_text, score)
                else:
                    logger.info(f"⏭️  Score {score} < {MIN_SCORE}, skipping")
                
                # Mark as processed
                save_processed_id(msg_id)
                processed_ids.add(msg_id)
            
            # Wait before next check
            await asyncio.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("🛑 Stopping...")
            break
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
