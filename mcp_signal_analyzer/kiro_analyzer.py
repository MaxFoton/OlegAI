#!/usr/bin/env python3
"""
Kiro-based Signal Analyzer
Использует Kiro через MCP для анализа сигналов + модель
"""

import json
import subprocess
import logging
import re
import pickle
import os
from pathlib import Path
from datetime import datetime

QUEUE_FILE = Path(os.getenv("SIGNAL_QUEUE_FILE", "/tmp/kiro_signal_queue.json"))
TEST_QUEUE_FILE = Path("/tmp/test_signal_queue.json")  # Для тестов
NTFY_URL = "http://87.121.218.4:8080/max-analysis"
LOG_FILE = Path("/home/max/freqtrade/logs/kiro_signal_analysis.log")
MCP_CONFIG = Path.home() / ".kiro" / "settings" / "mcp.json"

# Путь к модели LightGBM
MODEL_PATH = Path("/home/max/o_p/dex_scanner/data/directional_lgbm.pkl")
MODEL_META_PATH = Path("/home/max/o_p/dex_scanner/data/directional_lgbm_meta.json")

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
logger = logging.getLogger("kiro-analyzer")


# Модель LightGBM теперь работает в сканере и логирует MODEL_VERDICT
# Kiro analyzer читает вердикт из лога вместо вычисления


def check_mcp_configured() -> bool:
    """Проверить что MCP server настроен"""
    if not MCP_CONFIG.exists():
        logger.error(f"MCP config not found: {MCP_CONFIG}")
        return False
    
    try:
        config = json.loads(MCP_CONFIG.read_text())
        if "signal-analyzer" in config.get("mcpServers", {}):
            return True
        logger.error("signal-analyzer not found in MCP config")
        return False
    except Exception as e:
        logger.error(f"Error reading MCP config: {e}")
        return False



# УСТАРЕВШАЯ ФУНКЦИЯ - больше не используется!
# Модель LightGBM теперь работает в сканере и логирует MODEL_VERDICT
# Kiro analyzer читает вердикт из лога
#
# def get_model_prediction(signal_data: dict) -> tuple:
#     """
#     Получить предсказание модели LightGBM для сигнала
#     Возвращает: (predicted_direction, confidence)
#     """
#     ... (см. выше - закомментировано)


def call_kiro_mcp_analysis(signal_data: dict) -> dict:
    """
    Вызвать MCP server для анализа сигнала
    MCP server должен быть запущен в Kiro
    """
    try:
        # Прямой вызов MCP server (server.py)
        # Kiro должен быть открыт и MCP server должен быть подключён
        result = call_mcp_directly(signal_data)
        return result
        
    except Exception as e:
        logger.error(f"Error calling MCP: {e}")
        return None


def call_mcp_directly(signal_data: dict) -> dict:
    """
    Прямой вызов MCP server для анализа
    """
    try:
        # МОДЕЛЬ ОТКЛЮЧЕНА (13.08.2026) - работает только в сканере
        model_direction = None
        model_confidence = 0.0
        
        # Поддерживаем оба формата очереди
        if "signal" in signal_data and isinstance(signal_data["signal"], dict) and signal_data["signal"]:
            sig = signal_data['signal']
        else:
            # Если signal пустой или нет, создаем из верхнего уровня
            full_text = signal_data.get('signal_text', '')
            sig = {
                "symbol": signal_data.get("symbol", "UNKNOWN"),
                "direction": signal_data.get("direction", "UNKNOWN"),
                "score": signal_data.get("score", 0),
                "price": signal_data.get("price", 0),
                "full_text": full_text,
            }
        
        # Обеспечиваем наличие full_text
        if "full_text" not in sig or not sig["full_text"]:
            sig["full_text"] = signal_data.get("signal_text", "")
        
        # Читаем полный текст из signal_text (новый bridge) или full_text (старый monitor)
        full_text = (
            sig.get("full_text")
            or signal_data.get('signal_text')
            or sig.get('signal_text')
            or ''
        )
        
        # Если signal пустой, парсим из signal_text
        symbol = sig.get('symbol')
        direction = sig.get('direction')
        price = sig.get('price', 0)
        
        # Если нет symbol/direction/price, парсим из full_text только direction и price
        # Symbol НЕ парсим из текста - он должен быть в signal
        if not direction or not price:            
            # Парсим direction: "сигнал SHORT | score=7/15"
            if not direction:
                dir_match = re.search(r'сигнал\s+(LONG|SHORT)', full_text)
                if dir_match:
                    direction = dir_match.group(1)
            
            # Парсим price: price=$0.022380
            if not price:
                price_match = re.search(r'price=\$?([\d.]+)', full_text)
                if price_match:
                    price = float(price_match.group(1))
        
        # Парсим MODEL_VERDICT из лога сканера (если есть)
        model_match = re.search(r'MODEL_VERDICT dir=(\w+) confidence=([\d.]+)', full_text)
        if model_match:
            model_direction = model_match.group(1)
            model_confidence = float(model_match.group(2))
            logger.info(f"🤖 LightGBM Model: {model_direction} ({model_confidence:.1%})")
        
        # Импортируем MCP server напрямую
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        
        from server import SignalAnalyzer
        
        analyzer = SignalAnalyzer()
        
        # Формируем уже распарсенный сигнал напрямую
        parsed_signal = {
            'symbol': symbol or 'UNKNOWN',
            'direction': direction or 'UNKNOWN',
            'score': signal_data.get('score', sig.get('score', 0)),
            'price': price,
            'raw_text': full_text,
            'signal_type': direction or 'UNKNOWN',
            # Добавляем предсказание модели из лога
            'model_direction': model_direction,
            'model_confidence': model_confidence
        }
        
        # Парсим индикаторы из full_text
        
        # RSI
        rsi_match = re.search(r'RSI:.*?(?:5m|1h)\s+(\d+)', full_text)
        if rsi_match:
            parsed_signal['rsi'] = float(rsi_match.group(1))
        
        # Phase
        phase_match = re.search(r'фаза\s+(\w+)', full_text)
        if phase_match:
            parsed_signal['phase'] = phase_match.group(1)
        
        # Lorentzian
        lor_match = re.search(r'Lor:\s*([+\-]?\d+)\s*\((\d+)%\)', full_text)
        if lor_match:
            parsed_signal['lorentzian'] = {
                'value': int(lor_match.group(1)),
                'prediction': int(lor_match.group(1)),
                'strength': float(lor_match.group(2))
            }
        
        # VW-MACD
        if 'VW-MACD: bullish' in full_text or 'VW-MACD bullish' in full_text:
            parsed_signal['vw_macd'] = 'bullish'
        elif 'VW-MACD: bearish' in full_text or 'VW-MACD bearish' in full_text:
            parsed_signal['vw_macd'] = 'bearish'
        
        # EMA
        ema_match = re.search(r'EMA9/21\s+(LONG|SHORT)', full_text)
        if ema_match:
            parsed_signal['ema'] = {'trend': 'bullish' if ema_match.group(1) == 'LONG' else 'bearish'}
        
        # VWAP
        vwap_match = re.search(r'VWAP:\s*([+\-][\d.]+)%', full_text)
        if vwap_match:
            parsed_signal['vwap'] = {'deviation': float(vwap_match.group(1))}
        
        # OBV/Absorption
        if 'OBV накопление' in full_text or 'OBV +1' in full_text:
            parsed_signal['absorption'] = 'LONG'
        elif 'OBV распределение' in full_text or 'OBV -1' in full_text:
            parsed_signal['absorption'] = 'SHORT'
        
        logger.info(f"Parsed signal: {parsed_signal.get('symbol')} {parsed_signal.get('direction')}")
        
        # Вызываем анализ
        result = analyzer.analyze(parsed_signal)
        
        return result
        
    except Exception as e:
        logger.error(f"Error in direct MCP call: {e}", exc_info=True)
        return None


def send_to_ntfy(title: str, message: str):
    """Отправить результат в ntfy"""
    try:
        proc = subprocess.run(
            [
                "curl", "-fsS",
                "-H", f"Title: {title}",
                "-H", "Content-Type: text/plain; charset=utf-8",
                "--data-binary", f"@-",
                NTFY_URL
            ],
            input=message.encode('utf-8'),
            capture_output=True,
            timeout=10
        )
        
        if proc.returncode != 0:
            logger.error(f"ntfy failed rc={proc.returncode} stderr={proc.stderr.decode('utf-8', errors='replace')[-500:]}")
            return False
        
        logger.info(f"✅ Sent to ntfy: {title}")
        return True
    except Exception as e:
        logger.error(f"Failed to send to ntfy: {e}")
        return False


def process_queue():
    """Обработать очередь сигналов"""
    # Вызываем auto_process_queue.py который делает всю работу
    try:
        result = subprocess.run(
            ["python3", str(Path(__file__).parent / "auto_process_queue.py")],
            capture_output=True,
            timeout=60
        )
        
        if result.returncode == 0:
            logger.info(f"✅ auto_process_queue.py completed successfully")
            if result.stdout:
                logger.info(f"Output: {result.stdout.decode('utf-8')}")
        else:
            logger.error(f"❌ auto_process_queue.py failed: {result.stderr.decode('utf-8')}")
        
        return
        
    except Exception as e:
        logger.error(f"Error calling auto_process_queue.py: {e}")
        return
    
    # СТАРЫЙ КОД УДАЛЁН - всё делает auto_process_queue.py
    return


def process_queue_OLD_BROKEN():
    """Обработать очередь сигналов - СТАРАЯ ВЕРСИЯ НЕ РАБОТАЕТ!"""
    if not QUEUE_FILE.exists():
        logger.info("No queue file found")
        return
    
    try:
        queue = json.loads(QUEUE_FILE.read_text())
    except Exception as e:
        logger.error(f"Error reading queue: {e}")
        return
    
    if not queue:
        logger.info("Queue is empty")
        return
    
    # Обрабатываем pending сигналы
    for item in queue:
        if item.get('status') != 'pending':
            continue
        
        # Поддерживаем оба формата
        if "signal" in item and isinstance(item["signal"], dict):
            sig = item['signal']
        else:
            sig = item


if __name__ == "__main__":
    import sys
    import atexit
    
    # Lock file
    ANALYZER_LOCK = Path(os.getenv("KIRO_ANALYZER_LOCK", "/tmp/kiro_analyzer.lock"))
    
    # Создаем lock
    try:
        ANALYZER_LOCK.write_text(str(os.getpid()))
        atexit.register(lambda: ANALYZER_LOCK.unlink(missing_ok=True))
    except Exception as e:
        logger.error(f"Failed to create lock: {e}")
        sys.exit(1)
    
    # Если передан аргумент --test, используем тестовый файл
    if "--test" in sys.argv:
        logger.info("🧪 TEST MODE: Using test queue file")
        QUEUE_FILE = TEST_QUEUE_FILE
    
    logger.info("🚀 Starting Kiro-based Signal Analyzer")
    logger.info(f"📁 Queue file: {QUEUE_FILE}")
    
    try:
        process_queue()
    finally:
        ANALYZER_LOCK.unlink(missing_ok=True)
    
    logger.info("✅ Done")
