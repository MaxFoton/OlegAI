#!/usr/bin/env python3
"""
Export Kiro Analysis signals to Funtik
Читает ntfy канал max-analysis и создает файл kiro_signals.json для Фунтика
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kiro-export")

# Config
NTFY_SERVER = "http://87.121.218.4:8080"
NTFY_TOPIC = "max-analysis"
OUTPUT_FILE = Path("/home/max/freqtrade/user_data/pump_dump_strategy/data/kiro_signals.json")
PROCESSED_FILE = Path("/tmp/kiro_analysis_processed.txt")
MAX_AGE_MINUTES = 30  # Максимальный возраст сигнала


def load_processed_ids() -> set:
    """Загрузить ID уже обработанных сигналов"""
    if PROCESSED_FILE.exists():
        return set(PROCESSED_FILE.read_text().strip().split('\n'))
    return set()


def save_processed_id(msg_id: str):
    """Сохранить ID обработанного сигнала"""
    with PROCESSED_FILE.open('a') as f:
        f.write(f"{msg_id}\n")


def parse_analysis(text: str) -> dict | None:
    """
    Парсит анализ от Claude
    
    Формат:
    🔔 SYMBOL DIRECTION Analysis
    📊 Score: X/15
    🎯 Вердикт: ✅ ВХОДИТЬ (8/10) или ⏳ ЖДАТЬ (6/10)
    
    Returns:
        {
            'symbol': str,
            'direction': 'LONG' | 'SHORT',
            'score': int,  # scanner score /15
            'verdict': str,  # 'ВХОДИТЬ' | 'ЖДАТЬ' | 'НЕ ВХОДИТЬ'
            'confidence': int,  # /10
            'wait_conditions': list[str],  # условия для ЖДАТЬ
            'entry_price': float,
            'stop_price': float,
            'target_price': float,
        }
    """
    
    try:
        # Symbol и direction из первой строки
        match = re.search(r'🔔\s+(\w+)\s+(LONG|SHORT)', text)
        if not match:
            return None
        
        symbol = match.group(1)
        direction = match.group(2)
        
        # Scanner score
        score_match = re.search(r'📊 Score:\s*(\d+)/15', text)
        scanner_score = int(score_match.group(1)) if score_match else 0
        
        # Вердикт и confidence
        verdict_match = re.search(r'🎯 Вердикт:\s*(✅|⏳|⚠️|🚫)\s*(.+?)\s*\((\d+)/10\)', text)
        if not verdict_match:
            return None
        
        verdict_emoji = verdict_match.group(1)
        verdict_text = verdict_match.group(2)
        confidence = int(verdict_match.group(3))
        
        # Определяем verdict
        if '✅' in verdict_emoji or 'ВХОДИТЬ' in verdict_text or 'ШОРТИТЬ' in verdict_text:
            verdict = 'ВХОДИТЬ'
        elif '⏳' in verdict_emoji or 'ЖДАТЬ' in verdict_text:
            verdict = 'ЖДАТЬ'
        else:
            verdict = 'НЕ ВХОДИТЬ'
        
        # Wait conditions (для ЖДАТЬ)
        wait_conditions = []
        if verdict == 'ЖДАТЬ':
            wait_section = re.search(r'⏰ Ждать:(.*?)(?=💰|$)', text, re.DOTALL)
            if wait_section:
                for line in wait_section.group(1).split('\n'):
                    line = line.strip()
                    if line and line.startswith('•'):
                        wait_conditions.append(line[1:].strip())
        
        # Entry/Stop/Target prices
        entry_price = None
        stop_price = None
        target_price = None
        
        # Агрессивный entry
        entry_match = re.search(r'Entry\s*\(агр\):\s*\$?([\d.]+)', text)
        if entry_match:
            entry_price = float(entry_match.group(1))
        
        # Stop
        stop_match = re.search(r'Stop:\s*\$?([\d.]+)', text)
        if stop_match:
            stop_price = float(stop_match.group(1))
        
        # Target
        target_match = re.search(r'Target[^:]*:\s*\$?([\d.]+)', text)
        if target_match:
            target_price = float(target_match.group(1))
        
        return {
            'symbol': symbol,
            'direction': direction,
            'score': scanner_score,
            'verdict': verdict,
            'confidence': confidence,
            'wait_conditions': wait_conditions,
            'entry_price': entry_price,
            'stop_price': stop_price,
            'target_price': target_price,
        }
        
    except Exception as e:
        logger.error(f"Error parsing analysis: {e}")
        return None


def fetch_analysis_messages() -> list[dict]:
    """Получить сообщения из ntfy max-analysis"""
    try:
        # Получаем сообщения за последние 30 минут
        url = f"{NTFY_SERVER}/{NTFY_TOPIC}/json?poll=1"
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch: {response.status_code}")
            return []
        
        messages = []
        for line in response.text.strip().split('\n'):
            if line:
                try:
                    msg = json.loads(line)
                    messages.append(msg)
                except json.JSONDecodeError:
                    continue
        
        return messages
        
    except Exception as e:
        logger.error(f"Error fetching: {e}")
        return []


def main():
    """Main export loop"""
    logger.info("🚀 Starting Kiro signals export")
    
    processed_ids = load_processed_ids()
    messages = fetch_analysis_messages()
    
    signals = []
    cutoff_time = datetime.utcnow() - timedelta(minutes=MAX_AGE_MINUTES)
    
    for msg in messages:
        msg_id = msg.get('id', '')
        msg_time = msg.get('time', 0)
        msg_text = msg.get('message', '')
        
        # Skip if already processed
        if msg_id in processed_ids:
            continue
        
        # Skip if too old
        msg_datetime = datetime.utcfromtimestamp(msg_time)
        if msg_datetime < cutoff_time:
            logger.debug(f"Skipping old message {msg_id}")
            continue
        
        # Parse analysis
        analysis = parse_analysis(msg_text)
        if not analysis:
            logger.debug(f"Could not parse message {msg_id}")
            continue
        
        # Only ВХОДИТЬ signals (ЖДАТЬ handled separately)
        if analysis['verdict'] not in ['ВХОДИТЬ', 'ЖДАТЬ']:
            logger.info(f"⏭️  Skipping {analysis['symbol']} {analysis['direction']} - verdict={analysis['verdict']}")
            save_processed_id(msg_id)
            processed_ids.add(msg_id)
            continue
        
        logger.info(f"✅ Exported: {analysis['symbol']} {analysis['direction']} - verdict={analysis['verdict']} conf={analysis['confidence']}/10")
        
        signals.append({
            'coin': analysis['symbol'],
            'direction': analysis['direction'],
            'source': 'kiro_analysis',
            'score': analysis['confidence'],  # Используем confidence как score
            'score_max': 10,
            'scanner_score': analysis['score'],  # Оригинальный scanner score
            'verdict': analysis['verdict'],
            'wait_conditions': analysis['wait_conditions'],
            'entry_price': analysis['entry_price'],
            'stop_price': analysis['stop_price'],
            'target_price': analysis['target_price'],
            'timestamp': msg_datetime.isoformat(),
        })
        
        save_processed_id(msg_id)
        processed_ids.add(msg_id)
    
    # Save to file
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(signals, indent=2))
    
    logger.info(f"📊 Exported {len(signals)} signals to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
