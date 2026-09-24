#!/usr/bin/env python3
"""
Auto Analyzer - анализирует pending сигналы и отправляет в ntfy
Запускается cron каждую минуту
"""

import json
import subprocess
import sys
from pathlib import Path

QUEUE_FILE = Path("/home/max/freqtrade/.kiro/signal_queue.json")
NTFY_URL = "http://87.121.218.4:8080/max-analysis"

def analyze_signal(signal_data: dict) -> str:
    """Простой анализ сигнала на основе данных"""
    sig = signal_data['signal']
    full_text = sig.get('full_text', '')
    
    symbol = sig['symbol']
    direction = sig['direction']
    score = sig['score']
    
    # Парсим данные из full_text
    rsi = None
    phase = None
    vw_macd = None
    
    for line in full_text.split('\n'):
        if 'RSI:' in line:
            try:
                rsi = int(line.split('RSI:')[1].split()[0])
            except:
                pass
        if 'Phase:' in line:
            try:
                phase = line.split('Phase:')[1].strip().split()[0]
            except:
                pass
        if 'VW-MACD:' in line:
            try:
                vw_macd = 'bearish' if 'bearish' in line else 'bullish'
            except:
                pass
    
    # Простая логика анализа
    verdict = "⏳ ЖДАТЬ"
    confidence = 5
    reasons_for = []
    reasons_against = []
    
    if direction == "LONG":
        if rsi and rsi < 35:
            reasons_for.append(f"RSI={rsi} перепродан")
            confidence += 2
        if phase in ['EARLY_EXPANSION', 'ACCUMULATION']:
            reasons_for.append(f"Phase={phase}")
            confidence += 1
        
        if vw_macd == 'bearish':
            reasons_against.append("VW-MACD bearish")
            confidence -= 2
    
    elif direction == "SHORT":
        if rsi and rsi > 70:
            reasons_for.append(f"RSI={rsi} перекуплен")
            confidence += 2
        if phase in ['LATE_EXPANSION', 'DISTRIBUTION']:
            reasons_for.append(f"Phase={phase}")
            confidence += 1
        
        if vw_macd == 'bullish':
            reasons_against.append("VW-MACD bullish")
            confidence -= 2
    
    if confidence >= 7:
        verdict = "✅ ВХОДИТЬ"
    elif confidence <= 3:
        verdict = "🚫 ПРОПУСТИТЬ"
    
    # Формируем сообщение
    msg = f"""🔔 {symbol} {direction}
Score: {score}/15
Вердикт: {verdict} ({confidence}/10)

"""
    
    if reasons_for:
        msg += "✅ ЗА:\n" + "\n".join(f"• {r}" for r in reasons_for) + "\n\n"
    
    if reasons_against:
        msg += "❌ ПРОТИВ:\n" + "\n".join(f"• {r}" for r in reasons_against)
    
    return msg.strip()


def send_to_ntfy(title: str, message: str):
    """Отправка в ntfy"""
    try:
        subprocess.run([
            'curl', '-s',
            '-H', f'Title: {title}',
            '-H', 'Priority: 4',
            '-d', message,
            NTFY_URL
        ], check=True, capture_output=True)
        return True
    except:
        return False


def main():
    if not QUEUE_FILE.exists():
        print("Queue file not found")
        return
    
    # Читаем очередь
    with QUEUE_FILE.open('r') as f:
        queue = json.load(f)
    
    # Обрабатываем pending
    processed_count = 0
    for item in queue:
        if item['status'] != 'pending':
            continue
        
        sig = item['signal']
        print(f"Analyzing: {sig['symbol']} {sig['direction']} score={sig['score']}")
        
        # Анализ
        analysis = analyze_signal(item)
        title = f"📊 {sig['symbol']} {sig['direction']} | score={sig['score']}/15"
        
        # Отправка
        if send_to_ntfy(title, analysis):
            item['status'] = 'processed'
            processed_count += 1
            print(f"✅ Sent to ntfy")
        else:
            print(f"❌ Failed to send")
    
    # Сохраняем
    if processed_count > 0:
        with QUEUE_FILE.open('w') as f:
            json.dump(queue, f, indent=2)
        print(f"Processed {processed_count} signals")


if __name__ == "__main__":
    main()
