#!/usr/bin/env python3
"""
NTFY Monitor for Signal Analysis
Monitors ntfy server for trading signals and auto-analyzes them
"""

import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from typing import Optional

import httpx

# Import parser and analyzer from server.py
sys.path.insert(0, '/home/max/freqtrade/mcp_signal_analyzer')
from server import SignalParser, SignalAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ntfy-monitor")

# Configuration
NTFY_SERVER = "http://87.121.218.4:8080"
NTFY_SUBSCRIBE_TOPIC = "max-alerts"
NTFY_PUBLISH_TOPIC = "max-analysis"
MIN_SCORE = 7  # Минимальный score для автоматического анализа
CHECK_INTERVAL = 2  # Секунды между проверками


class NtfyMonitor:
    """Monitor ntfy server for signals and auto-analyze"""
    
    def __init__(self):
        self.running = False
        self.last_message_id = None
        
    async def fetch_messages(self) -> list[dict]:
        """Fetch latest messages from ntfy"""
        try:
            url = f"{NTFY_SERVER}/{NTFY_SUBSCRIBE_TOPIC}/json?poll=1"
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                
                if response.status_code == 200:
                    # ntfy returns NDJSON (newline-delimited JSON)
                    messages = []
                    for line in response.text.strip().split('\n'):
                        if line:
                            try:
                                msg = json.loads(line)
                                messages.append(msg)
                            except json.JSONDecodeError:
                                continue
                    return messages
                else:
                    logger.error(f"Failed to fetch messages: {response.status_code}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error fetching messages: {e}")
            return []
    
    async def send_analysis(self, title: str, message: str, priority: int = 3):
        """Send analysis result to ntfy"""
        try:
            url = f"{NTFY_SERVER}/{NTFY_PUBLISH_TOPIC}"
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    data=message.encode('utf-8'),
                    headers={
                        "Title": title,
                        "Priority": str(priority),
                        "Tags": "chart_with_upwards_trend,robot"
                    }
                )
                
                if response.status_code == 200:
                    logger.info(f"Analysis sent to ntfy: {title}")
                else:
                    logger.error(f"Failed to send analysis: {response.status_code}")
                    
        except Exception as e:
            logger.error(f"Error sending analysis: {e}")
    
    def extract_score(self, text: str) -> Optional[int]:
        """Extract score from signal text"""
        match = re.search(r'score=([\d]+)/[\d]+', text)
        if match:
            return int(match.group(1))
        return None
    
    async def process_message(self, message: dict):
        """Process a single message"""
        try:
            # Get message content
            msg_id = message.get('id')
            msg_text = message.get('message', '')
            msg_title = message.get('title', '')
            
            # Check if already processed
            if msg_id == self.last_message_id:
                return
            
            # Check if it's a signal
            if not msg_text or '📢' not in msg_text:
                logger.debug(f"Not a signal message: {msg_title}")
                return
            
            # Extract score
            score = self.extract_score(msg_text)
            if score is None:
                logger.warning(f"Could not extract score from message")
                return
            
            logger.info(f"Received signal with score={score}")
            
            # Check if score is high enough
            if score < MIN_SCORE:
                logger.info(f"Score {score} < {MIN_SCORE}, skipping analysis")
                return
            
            # Parse signal
            signal = SignalParser.parse_signal(msg_text)
            if not signal:
                logger.error("Failed to parse signal")
                await self.send_analysis(
                    "❌ Parse Error",
                    f"Не удалось распарсить сигнал:\n{msg_text[:200]}",
                    priority=1
                )
                return
            
            # Analyze signal
            analysis = SignalAnalyzer.analyze(signal)
            formatted = SignalAnalyzer.format_analysis(signal, analysis)
            
            # Determine priority based on verdict
            priority = 5  # High
            if 'ВХОДИТЬ' in analysis['verdict'] or 'ШОРТИТЬ' in analysis['verdict']:
                priority = 5  # Urgent
            elif 'ЖДАТЬ' in analysis['verdict']:
                priority = 4  # High
            else:
                priority = 3  # Default
            
            # Send analysis
            symbol = signal.get('symbol', 'Unknown')
            signal_type = signal.get('signal_type', 'N/A')
            confidence = analysis.get('confidence', 0)
            
            title = f"📊 {symbol} {signal_type} | {analysis['verdict']} ({confidence}/10)"
            
            await self.send_analysis(title, formatted, priority=priority)
            
            # Update last processed message
            self.last_message_id = msg_id
            
            logger.info(f"✅ Analysis completed for {symbol}")
            
        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
    
    async def run(self):
        """Main monitoring loop"""
        self.running = True
        logger.info(f"🚀 Starting ntfy monitor on {NTFY_SERVER}/{NTFY_SUBSCRIBE_TOPIC}")
        logger.info(f"📊 Min score for auto-analysis: {MIN_SCORE}/15")
        logger.info(f"📤 Results will be sent to: {NTFY_PUBLISH_TOPIC}")
        
        # Send startup notification
        await self.send_analysis(
            "🤖 Signal Analyzer Started",
            f"Мониторинг сигналов запущен\nМин. score: {MIN_SCORE}/15\nКанал: {NTFY_SUBSCRIBE_TOPIC}",
            priority=2
        )
        
        while self.running:
            try:
                # Fetch messages
                messages = await self.fetch_messages()
                
                # Process new messages
                for message in messages:
                    await self.process_message(message)
                
                # Wait before next check
                await asyncio.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                logger.info("Received interrupt signal, stopping...")
                self.running = False
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(CHECK_INTERVAL)
        
        logger.info("Monitor stopped")


async def test_connection():
    """Test ntfy connection"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{NTFY_SERVER}/{NTFY_SUBSCRIBE_TOPIC}/json?poll=1")
            if response.status_code == 200:
                logger.info(f"✅ Connection to ntfy server OK")
                return True
            else:
                logger.error(f"❌ Connection failed: {response.status_code}")
                return False
    except Exception as e:
        logger.error(f"❌ Connection error: {e}")
        return False


async def main():
    """Main entry point"""
    logger.info("Testing connection to ntfy server...")
    
    if not await test_connection():
        logger.error("Failed to connect to ntfy server. Check configuration.")
        return 1
    
    monitor = NtfyMonitor()
    
    try:
        await monitor.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
