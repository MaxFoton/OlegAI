#!/usr/bin/env python3
"""
Funtik Trades Notifier - отправляет уведомления о входах/выходах в отдельный канал ntfy
Читает funtik_audit.log и отправляет уведомления о ENTRY и EXIT событиях
"""

import time
import subprocess
from pathlib import Path
from datetime import datetime

# Paths
AUDIT_LOG = Path("/home/max/freqtrade/user_data/pump_dump_strategy/logs/funtik_audit.log")
POSITION_FILE = Path("/tmp/funtik_trades_notifier_position.txt")
NTFY_URL = "http://87.121.218.4:8080/funtik-trades"  # Отдельный канал
CHECK_INTERVAL = 2  # секунды

def load_last_position() -> int:
    """Загружает последнюю прочитанную позицию"""
    if POSITION_FILE.exists():
        try:
            return int(POSITION_FILE.read_text().strip())
        except:
            return 0
    return 0

def save_last_position(position: int):
    """Сохраняет текущую позицию"""
    POSITION_FILE.write_text(str(position))

def send_ntfy(title: str, message: str, priority: int = 4):
    """Отправляет уведомление в ntfy"""
    try:
        subprocess.run([
            'curl', '-s',
            '-H', f'Title: {title}',
            '-H', f'Priority: {priority}',
            '-d', message,
            NTFY_URL
        ], check=True, timeout=5)
        return True
    except Exception as e:
        print(f"❌ Failed to send ntfy: {e}")
        return False

def parse_audit_line(line: str) -> dict:
    """Парсит строку из funtik_audit.log"""
    try:
        # Формат: 2026-09-17 10:00:00 | ENTRY_SIGNAL | pair=BTC/USDT:USDT | direction=LONG | ...
        if '|' not in line:
            return None
        
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 2:
            return None
        
        timestamp_str = parts[0].strip()
        event = parts[1].strip()
        
        # Парсим поля key=value
        fields = {'event': event, 'timestamp': timestamp_str}
        for part in parts[2:]:
            if '=' in part:
                key, value = part.split('=', 1)
                fields[key.strip()] = value.strip()
        
        return fields
    except Exception as e:
        print(f"Parse error: {e}")
        return None

def format_entry_message(fields: dict) -> tuple:
    """Форматирует сообщение о входе"""
    pair = fields.get('pair', 'UNKNOWN')
    direction = fields.get('direction', 'UNKNOWN')
    rate = fields.get('rate', 'N/A')
    entry_tag = fields.get('entry_tag', 'N/A')
    mode = fields.get('mode', 'N/A')
    score = fields.get('score', 'N/A')
    htf_1h = fields.get('htf_1h', 'N/A')
    timestamp = fields.get('timestamp', '')
    
    title = f"🟢 ВХОД {pair} {direction}"
    message = f"""
💰 Цена: ${rate}
🏷️ Тег: {entry_tag}
📊 Режим: {mode}
⭐ Скор: {score}
📈 HTF 1h: {htf_1h}
⏰ Время: {timestamp}
    """.strip()
    
    return title, message

def format_exit_message(fields: dict) -> tuple:
    """Форматирует сообщение о выходе"""
    pair = fields.get('pair', 'UNKNOWN')
    direction = fields.get('direction', 'UNKNOWN')
    pnl = fields.get('pnl', 'N/A')  # Изменено: ищем 'pnl' вместо 'profit_ratio'
    exit_reason = fields.get('exit_reason', 'N/A')
    timestamp = fields.get('timestamp', '')
    
    try:
        # pnl уже в формате "-0.98%" - парсим
        if isinstance(pnl, str) and '%' in pnl:
            profit_pct = float(pnl.replace('%', ''))
        else:
            profit_pct = float(pnl) * 100
        emoji = "🟢" if profit_pct > 0 else "🔴"
    except:
        profit_pct = 0
        emoji = "⚪"
    
    title = f"{emoji} ВЫХОД {pair} {direction} | {profit_pct:+.2f}%"
    message = f"""
📈 P&L: {profit_pct:+.2f}%
🚪 Причина: {exit_reason}
⏰ Время: {timestamp}
    """.strip()
    
    return title, message

def monitor_audit_log():
    """Мониторит audit log и отправляет уведомления"""
    print("🚀 Starting Funtik Trades Notifier")
    print(f"📁 Monitoring: {AUDIT_LOG}")
    print(f"📢 Sending to: {NTFY_URL}")
    
    last_position = load_last_position()
    
    # Если первый запуск - начинаем с конца файла
    if last_position == 0 and AUDIT_LOG.exists():
        last_position = AUDIT_LOG.stat().st_size
        save_last_position(last_position)
        print(f"📝 First run - starting from end of file")
    else:
        print(f"📝 Starting from position: {last_position}")
    
    while True:
        try:
            if not AUDIT_LOG.exists():
                print(f"⚠️ Audit log not found: {AUDIT_LOG}")
                time.sleep(CHECK_INTERVAL)
                continue
            
            current_size = AUDIT_LOG.stat().st_size
            
            # Если файл был ротирован/усечён
            if current_size < last_position:
                print("📝 Log file rotated, restarting from beginning")
                last_position = 0
            
            # Читаем новые строки
            if current_size > last_position:
                with AUDIT_LOG.open('r', encoding='utf-8', errors='ignore') as f:
                    f.seek(last_position)
                    new_lines = f.readlines()
                    last_position = f.tell()
                
                save_last_position(last_position)
                
                # Обрабатываем новые строки
                for line in new_lines:
                    fields = parse_audit_line(line)
                    if not fields:
                        continue
                    
                    event = fields.get('event', '')
                    
                    # ENTRY событие (реальный вход, не сигнал)
                    if event == 'ENTRY':
                        title, message = format_entry_message(fields)
                        if send_ntfy(title, message, priority=5):
                            print(f"✅ Sent ENTRY: {fields.get('pair')} {fields.get('direction')} @ {fields.get('rate')}")
                    
                    # EXIT событие
                    elif event == 'EXIT':
                        title, message = format_exit_message(fields)
                        # Парсим pnl из строки вида "-0.98%"
                        try:
                            pnl_str = fields.get('pnl', '0%')
                            profit_pct = float(pnl_str.replace('%', ''))
                        except:
                            profit_pct = 0
                        priority = 5 if profit_pct > 0 else 4
                        if send_ntfy(title, message, priority=priority):
                            print(f"✅ Sent EXIT: {fields.get('pair')} {profit_pct:+.2f}%")
            
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            print("🛑 Stopping...")
            break
        except Exception as e:
            print(f"❌ Error in main loop: {e}")
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    monitor_audit_log()
