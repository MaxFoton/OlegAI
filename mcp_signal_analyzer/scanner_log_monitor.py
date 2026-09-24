#!/usr/bin/env python3
"""
Scanner Log Monitor - reads scanner.log and adds signals to queue for Kiro
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("scanner-log-monitor")

# Configuration
SCANNER_LOG = Path("/home/max/o_p/dex_scanner/logs/scanner.log")
SIGNAL_QUEUE_FILE = Path("/home/max/freqtrade/.kiro/signal_queue.json")
MIN_SCORE = 5  # Все сигналы >=5 идут через lightgbm + Kiro анализ
CHECK_INTERVAL = 5  # секунды

# Track last processed position in log
LAST_POSITION_FILE = Path("/tmp/scanner_log_position.txt")


def load_last_position() -> int:
    """Load last read position in log file"""
    if LAST_POSITION_FILE.exists():
        try:
            return int(LAST_POSITION_FILE.read_text().strip())
        except:
            return 0
    return 0


def save_last_position(position: int):
    """Save current position in log file"""
    LAST_POSITION_FILE.write_text(str(position))


def extract_signal_from_log_line(line: str) -> dict | None:
    """Extract signal data from SIGNAL_DECISION log line"""
    try:
        # Parse: SIGNAL_DECISION base=FLR dir=LONG kind=vol_anomaly score=9 price=0.00603000
        match = re.search(
            r'SIGNAL_DECISION\s+base=(\w+)\s+dir=(\w+)\s+kind=(\w+)\s+score=(\d+)\s+price=([\d.]+)',
            line
        )
        
        if not match:
            return None
        
        symbol, direction, kind, score, price = match.groups()
        
        return {
            'symbol': symbol,
            'direction': direction,
            'kind': kind,
            'score': int(score),
            'price': float(price),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'log_line': line.strip(),
            'details': []  # Будем собирать следующие строки
        }
    except Exception as e:
        logger.error(f"Error parsing line: {e}")
        return None


def parse_signal_details(lines: list[str]) -> str:
    """Parse full signal details from multiple log lines"""
    # Собираем все INFO строки до следующего SIGNAL_DECISION или пустой строки
    details = []
    for line in lines:
        if 'SIGNAL_DECISION' in line:
            break
        if 'INFO' in line:
            # Убираем timestamp и уровень, оставляем только текст
            parts = line.split('INFO', 1)
            if len(parts) > 1:
                text = parts[1].strip()
                if text and not text.startswith('[P'):  # Пропускаем [P3] sent:
                    details.append(text)
    
    return '\n'.join(details)


async def add_to_queue(signal: dict):
    """Add signal to queue for Kiro"""
    # Load existing queue
    if SIGNAL_QUEUE_FILE.exists():
        try:
            queue = json.loads(SIGNAL_QUEUE_FILE.read_text())
        except:
            queue = []
    else:
        queue = []
    
    # 🔥 ДЕДУПЛИКАЦИЯ: не более 2 сигналов по одной монете за последний час
    from datetime import timedelta
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    
    # Считаем сколько сигналов по этой монете было за последний час
    recent_signals = 0
    for item in queue:
        try:
            item_time = datetime.fromisoformat(item['timestamp'])
            item_symbol = item['signal'].get('symbol', '')
            
            if item_time > one_hour_ago and item_symbol == signal['symbol']:
                recent_signals += 1
        except:
            pass
    
    if recent_signals >= 2:
        logger.warning(
            f"🛑 DEDUP: {signal['symbol']} {signal['direction']} - "
            f"уже {recent_signals} сигналов за последний час, пропускаем"
        )
        return
    
    # Add new signal
    queue.append({
        'timestamp': signal['timestamp'],
        'score': signal['score'],
        'signal': signal,
        'status': 'pending'
    })
    
    # Save queue
    SIGNAL_QUEUE_FILE.write_text(json.dumps(queue, indent=2))
    logger.info(f"✅ Signal added: {signal['symbol']} {signal['direction']} score={signal['score']} (recent_count={recent_signals+1})")
    
    # Запускаем авто-обработку
    import subprocess
    try:
        subprocess.Popen([
            'python3',
            '/home/max/freqtrade/mcp_signal_analyzer/auto_process_queue.py'
        ])
        logger.info("🤖 Auto-processor triggered")
    except Exception as e:
        logger.error(f"Failed to trigger processor: {e}")


async def monitor_log():
    """Monitor scanner log for new signals"""
    logger.info("🚀 Starting Scanner Log Monitor")
    logger.info(f"📁 Monitoring: {SCANNER_LOG}")
    logger.info(f"📊 Min score: {MIN_SCORE}/15")
    logger.info(f"💾 Queue file: {SIGNAL_QUEUE_FILE}")
    
    last_position = load_last_position()
    
    # Если первый запуск - читаем только последние 30 минут
    if last_position == 0 and SCANNER_LOG.exists():
        file_size = SCANNER_LOG.stat().st_size
        
        # Читаем последние N строк чтобы захватить ~30 минут
        with SCANNER_LOG.open('r', encoding='utf-8', errors='ignore') as f:
            # Идём с конца, ищем строки за последние 30 мин
            from datetime import timedelta
            thirty_min_ago = datetime.now() - timedelta(minutes=30)
            
            # Читаем последние 5000 строк (примерно 30 мин активности)
            lines = f.readlines()
            if len(lines) > 5000:
                # Берём последние 5000 строк
                skip_lines = len(lines) - 5000
                f.seek(0)
                for _ in range(skip_lines):
                    f.readline()
                last_position = f.tell()
            else:
                last_position = 0
        
        save_last_position(last_position)
        logger.info(f"📝 First run - starting from last 30 minutes (position: {last_position})")
    else:
        logger.info(f"📝 Starting from position: {last_position}")
    
    # Буфер для накопления незавершённого сигнала между итерациями
    pending_signal = None
    pending_signal_buffer = []
    
    while True:
        try:
            if not SCANNER_LOG.exists():
                logger.warning(f"Log file not found: {SCANNER_LOG}")
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            
            # Get current file size
            current_size = SCANNER_LOG.stat().st_size
            
            # If file was rotated/truncated
            if current_size < last_position:
                logger.info("Log file rotated, restarting from beginning")
                last_position = 0
            
            # Read new content
            if current_size > last_position:
                with SCANNER_LOG.open('r', encoding='utf-8', errors='ignore') as f:
                    f.seek(last_position)
                    new_lines = f.readlines()
                    last_position = f.tell()
                
                # Save position
                save_last_position(last_position)
                
                # Продолжаем с предыдущего незавершённого сигнала (если есть)
                signals_in_progress = pending_signal
                signal_lines_buffer = pending_signal_buffer
                
                for line in new_lines:
                    if 'SIGNAL_DECISION' in line:
                        # При появлении нового SIGNAL_DECISION обрабатываем предыдущий
                        # ЕСЛИ он достаточно старый (>=5 сек) - индикаторы уже записались
                        if signals_in_progress:
                            signal_time = datetime.fromisoformat(signals_in_progress['timestamp'])
                            now = datetime.now(timezone.utc)
                            age_seconds = (now - signal_time).total_seconds()
                            
                            # Обрабатываем только если прошло >=5 сек (индикаторы записались)
                            if age_seconds >= 5.0:
                                signals_in_progress['details'] = parse_signal_details(signal_lines_buffer)
                                signals_in_progress['full_text'] = f"{signals_in_progress['log_line']}\n{signals_in_progress['details']}"
                                
                                logger.info(f"📢 Processing signal: {signals_in_progress['symbol']} score={signals_in_progress['score']} (age={age_seconds:.1f}s)")
                                
                                # Check score threshold
                                if signals_in_progress['score'] >= MIN_SCORE:
                                    logger.info(f"✅ Score {signals_in_progress['score']} >= {MIN_SCORE}, adding to queue")
                                    await add_to_queue(signals_in_progress)
                                else:
                                    logger.info(f"⏭️  Score {signals_in_progress['score']} < {MIN_SCORE}, skipping")
                                
                                # Очищаем buffer после обработки
                                signal_lines_buffer = []
                            else:
                                logger.debug(f"⏳ Signal {signals_in_progress['symbol']} too young ({age_seconds:.1f}s), keeping for next iteration")
                                # НЕ очищаем buffer - он нужен для следующей итерации
                        
                        # Начинаем новый сигнал
                        signals_in_progress = extract_signal_from_log_line(line)
                        # НЕ сбрасываем buffer если предыдущий сигнал не обработан
                        if not pending_signal or (pending_signal and pending_signal != signals_in_progress):
                            signal_lines_buffer = []
                    else:
                        # Собираем строки деталей (они могут относиться к текущему или предыдущему сигналу)
                        signal_lines_buffer.append(line)
                
                # Сохраняем незавершённый сигнал для следующей итерации
                # (даём время ~5 сек для записи индикаторов в лог)
                pending_signal = signals_in_progress
                pending_signal_buffer = signal_lines_buffer
            else:
                # Если новых строк нет, но есть незавершённый сигнал старше 10 сек - обрабатываем
                if pending_signal:
                    # Проверяем время: если с момента создания сигнала прошло > 10 сек
                    signal_time = datetime.fromisoformat(pending_signal['timestamp']).replace(tzinfo=None)
                    now = datetime.utcnow()
                    age_seconds = (now - signal_time).total_seconds()
                    
                    if age_seconds > 10:
                        # Индикаторы должны были уже записаться, обрабатываем
                        pending_signal['details'] = parse_signal_details(pending_signal_buffer)
                        pending_signal['full_text'] = f"{pending_signal['log_line']}\n{pending_signal['details']}"
                        
                        logger.info(f"📢 Processing pending signal: {pending_signal['symbol']} score={pending_signal['score']} (age={age_seconds:.1f}s)")
                        
                        if pending_signal['score'] >= MIN_SCORE:
                            logger.info(f"✅ Score {pending_signal['score']} >= {MIN_SCORE}, adding to queue")
                            await add_to_queue(pending_signal)
                        else:
                            logger.info(f"⏭️  Score {pending_signal['score']} < {MIN_SCORE}, skipping")
                        
                        # Очищаем буфер
                        pending_signal = None
                        pending_signal_buffer = []
            
            # Wait before next check
            await asyncio.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("🛑 Stopping...")
            break
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor_log())
