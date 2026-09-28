#!/usr/bin/env python3
"""
Автоматически обрабатывает очередь сигналов напрямую
Вызывается монитором после добавления сигнала
"""

import json
import subprocess
import logging
import pickle
from pathlib import Path
from datetime import datetime

QUEUE_FILE = Path("/home/max/freqtrade/.kiro/signal_queue.json")
WAIT_QUEUE_FILE = Path("/home/max/freqtrade/.kiro/wait_queue.json")
NTFY_URL = "http://87.121.218.4:8080/max-analysis"
LOG_FILE = Path("/home/max/freqtrade/logs/signal_analysis.log")
SCANNER_LOG = Path("/home/max/o_p/dex_scanner/logs/scanner.log")

# Путь к модели lightgbm_scorer (обучена на реальных сделках)
MODEL_PATH = Path("/home/max/freqtrade/user_data/pump_dump_strategy/data/lightgbm_scorer.pkl")
MODEL_META_PATH = Path("/home/max/freqtrade/user_data/pump_dump_strategy/data/lightgbm_meta.json")

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
logger = logging.getLogger("signal-analyzer")


def read_indicators_from_scanner_log(symbol: str, direction: str) -> dict:
    """Читает индикаторы напрямую из scanner.log для символа"""
    try:
        if not SCANNER_LOG.exists():
            logger.warning(f"Scanner log not found: {SCANNER_LOG}")
            return {}
        
        # Читаем последние 1000 строк
        with SCANNER_LOG.open('r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()[-1000:]
        
        # Ищем последний блок для символа
        indicator_block = []
        found_signal = False
        
        for i, line in enumerate(lines):
            if f'SIGNAL_DECISION base={symbol} dir={direction}' in line:
                found_signal = True
                indicator_block = []
                continue
            
            if found_signal:
                if 'SIGNAL_DECISION' in line:
                    # Новый сигнал - останавливаемся
                    break
                if 'progress' in line or 'done. sent=' in line:
                    # Конец блока
                    break
                indicator_block.append(line)
        
        if not indicator_block:
            logger.info(f"⚠️ No indicator block found in scanner.log for {symbol} {direction}")
            return {}
        
        # Парсим индикаторы из блока
        text = '\n'.join(indicator_block)
        logger.info(f"📖 Reading indicators from scanner.log for {symbol} {direction}")
        
        # Парсим как обычно
        parsed = {}
        for line in text.split('\n'):
            # RSI
            if 'RSI:' in line and '/' in line:
                try:
                    # Формат: "RSI: 5m 49 / 1h 65"
                    if '5m' in line:
                        rsi_part = line.split('5m')[1].strip().split()[0]
                        parsed['rsi'] = float(rsi_part)
                    elif '1h' in line:
                        rsi_part = line.split('1h')[1].strip().split()[0]
                        parsed['rsi'] = float(rsi_part)
                except:
                    pass
            
            # Phase
            if 'фаза' in line:
                try:
                    phase = line.split('фаза')[1].strip().split()[0]
                    parsed['phase'] = phase
                except:
                    pass
            
            # VW-MACD
            if 'VW-MACD:' in line or 'MACD:' in line:
                if 'bullish' in line:
                    parsed['vw_macd'] = 'bullish'
                elif 'bearish' in line:
                    parsed['vw_macd'] = 'bearish'
                
                # Парсим силу MACD в ATR - если близко к 0, это свежее пересечение
                if 'сила' in line and 'ATR' in line:
                    try:
                        strength_str = line.split('сила')[1].split('ATR')[0].strip()
                        parsed['macd_strength_atr'] = float(strength_str)
                    except:
                        pass
            
            # VWAP
            if 'VWAP:' in line and '%' in line:
                try:
                    vwap_pct = line.split('%')[0].split()[-1]
                    parsed['vwap'] = float(vwap_pct)
                except:
                    pass
            
            # EMA
            if 'EMA-mom:' in line:
                if '+' in line:
                    parsed['ema'] = 'bullish'
                elif '-' in line:
                    parsed['ema'] = 'bearish'
            
            # Lorentzian: формат "Lor: +1 (6%)" или "Lor: -1 (3%)"
            if 'Lor:' in line:
                try:
                    lor_match = line.split('Lor:')[1].strip()
                    lor_value = lor_match.split()[0]  # +1, -1, 0
                    lor_strength = lor_match.split('(')[1].split('%')[0] if '(' in lor_match else '0'
                    parsed['lorentzian'] = {
                        'prediction': int(lor_value),
                        'strength': float(lor_strength)
                    }
                except:
                    pass
            
            # Volume multiplier: формат "vol×1.8" или "Объём: x1.5"
            if 'vol×' in line or 'vol ×' in line:
                try:
                    vol_str = line.split('×')[1].strip().split()[0]
                    parsed['vol_multiplier'] = float(vol_str)
                except:
                    pass
            elif 'Объём:' in line and 'x' in line:
                try:
                    vol_str = line.split('x')[1].strip().split()[0]
                    parsed['vol_multiplier'] = float(vol_str)
                except:
                    pass
        
        logger.info(f"✅ Parsed from scanner.log: {parsed}")
        return parsed
        
    except Exception as e:
        logger.error(f"Failed to read scanner.log: {e}")
        return {}


def get_model_prediction(symbol: str, direction: str, score: int) -> float:
    """
    Получить вероятность WIN от lightgbm_scorer используя ВСЕ фичи из scanner.log
    Возвращает: prob_win (0.0-1.0) или None если модель недоступна
    """
    try:
        if not MODEL_PATH.exists() or not MODEL_META_PATH.exists():
            logger.info("⚠️ Model files not found")
            return None
        
        # Проверяем метаданные модели
        meta = json.loads(MODEL_META_PATH.read_text())
        
        if not meta.get("used", False):
            logger.info("⚠️ Model 'used' flag is False")
            return None
        
        # КРИТИЧЕСКИ ВАЖНО: попытка импорта lightgbm
        try:
            import lightgbm
        except ImportError:
            logger.info("⚠️ lightgbm not installed")
            return None
        
        # Загружаем модель
        with MODEL_PATH.open('rb') as f:
            model = pickle.load(f)
        
        # Извлекаем ВСЕ фичи из scanner.log
        from scanner_parser import extract_all_features, prepare_model_features
        
        raw_features = extract_all_features(symbol, direction, score)
        
        if not raw_features:
            logger.warning("⚠️ No features extracted from scanner.log")
            return None
        
        # Преобразуем в формат модели
        model_features = prepare_model_features(raw_features, direction)
        
        # Получаем feature_names в правильном порядке
        feature_names = meta.get('feature_names')
        if not feature_names:
            logger.warning("⚠️ No feature_names in meta")
            return None
        
        # Подготавливаем X в порядке модели
        X = [[model_features.get(fname, 0.0) for fname in feature_names]]
        
        # Предсказание: для LGBMClassifier используем predict_proba
        pred_proba = model.predict_proba(X)[0]
        
        # LightGBM возвращает [prob_class_0, prob_class_1]
        prob_win = pred_proba[1] if len(pred_proba) > 1 else float(pred_proba)
        
        logger.info(f"✅ Model prediction: prob_win={prob_win:.2%} (features: {len(model_features)})")
        
        return prob_win
        
    except Exception as e:
        logger.error(f"Model prediction error: {e}", exc_info=True)
        return None
        
        # Если нет критичных фич - модель не работает
        if rsi is None:
            logger.info("⚠️ Model skipped: RSI is None")
            return None
        
        # Конвертируем направление
        is_long = 1.0 if direction == 'LONG' else 0.0
        
        # Конвертируем phase в one-hot
        phase_early = 1.0 if phase == 'EARLY_EXPANSION' else 0.0
        phase_mid = 1.0 if phase == 'MID_EXPANSION' else 0.0
        phase_late = 1.0 if phase == 'LATE_EXPANSION' else 0.0
        phase_exhaustion = 1.0 if phase == 'EXHAUSTION' else 0.0
        
        # Конвертируем VW-MACD в числовое значение (для micro_score)
        vw_macd_val = 1.0 if vw_macd == 'bullish' else (-1.0 if vw_macd == 'bearish' else 0.0)
        
        # Подготавливаем ВСЕ 30 фич в порядке модели
        features = {
            # Numeric features (используем доступные значения или дефолты)
            'scanner_score': float(score) / 15.0,  # Нормализуем score 0-15 -> 0-1
            'regime_score': 0.5,  # Дефолт: нейтральный режим
            'micro_score': vw_macd_val,  # VW-MACD как микротренд
            'trigger_score': float(rsi) / 100.0,  # RSI как триггер
            'confidence': float(score) / 15.0,  # Score как уверенность
            'volatility': 0.5,  # Дефолт: средняя волатильность
            'volume_spike': 0.5,  # Дефолт
            'absorption_score': 0.0,  # Нет данных
            'lor_signal': 0.0,  # Нет Lorentzian данных
            'lor_strength': 0.0,
            'lor_prediction': 0.0,
            'volume_slope': 0.0,  # Нет данных
            'delta_slope': 0.0,  # Нет данных
            'distance_from_ema20': 0.0,  # Дефолт
            'distance_from_vwap': float(vwap) if vwap is not None else 0.0,
            'rsi': float(rsi),
            'htf_trend_1h': 0.0,  # Дефолт: нет HTF данных
            
            # Categorical features
            'direction_long': is_long,
            
            # Phase one-hot
            'phase_early_expansion': phase_early,
            'phase_mid_expansion': phase_mid,
            'phase_late_expansion': phase_late,
            'phase_exhaustion': phase_exhaustion,
            
            # Interaction features (direction x phase)
            'long_x_early_expansion': is_long * phase_early,
            'short_x_early_expansion': (1.0 - is_long) * phase_early,
            'long_x_mid_expansion': is_long * phase_mid,
            'short_x_mid_expansion': (1.0 - is_long) * phase_mid,
            'long_x_late_expansion': is_long * phase_late,
            'short_x_late_expansion': (1.0 - is_long) * phase_late,
            'long_x_exhaustion': is_long * phase_exhaustion,
            'short_x_exhaustion': (1.0 - is_long) * phase_exhaustion,
        }
        
        # Подготавливаем в порядке модели
        feature_names = meta.get('feature_names')
        if not feature_names:
            logger.info("⚠️ No feature_names in meta")
            return None
        
        X = [[features.get(fname, 0.0) for fname in feature_names]]
        
        # Предсказание: класс 1 = WIN
        pred_proba = model.predict_proba(X)[0]
        prob_win = pred_proba[1]
        
        logger.info(f"✅ Model prediction: prob_win={prob_win:.2%}")
        
        return prob_win
        
    except Exception as e:
        logger.error(f"Model prediction error: {e}", exc_info=True)
        return None

def send_to_ntfy(symbol: str, direction: str, score: int, price: float, 
                 verdict: str, reasons_for: list, reasons_against: list, 
                 recommendations: list, confidence: int) -> bool:
    """Отправить результат анализа в ntfy И напрямую в dex_signals_analysis.json (обход ntfy если сервер недоступен)"""
    title = f"📊 {symbol} {direction} | {verdict}"
    
    message_parts = [
        f"💰 Цена: ${price}",
        f"📈 Score: {score}/15",
        "",
        "✅ ЗА:",
    ]
    
    if reasons_for:
        message_parts.extend([f"  • {r}" for r in reasons_for])
    else:
        message_parts.append("  • нет")
    
    if reasons_against:
        message_parts.append("")
        message_parts.append("❌ ПРОТИВ:")
        message_parts.extend([f"  • {r}" for r in reasons_against])
    
    message_parts.append("")
    message_parts.append(f"🎯 {verdict}")
    
    if recommendations:
        message_parts.append("")
        message_parts.extend(recommendations)
    
    message = '\n'.join(message_parts)
    
    # Логируем
    logger.info(f"🔍 FINAL: confidence={confidence} | reasons_for={len(reasons_for)} reasons_against={len(reasons_against)}")
    logger.info(f"=" * 80)
    logger.info(f"ANALYZED: {symbol} {direction} | {verdict} ({confidence}/10)")
    logger.info(f"=" * 80)
    
    # 🔥 FIX (22.09.2026): НЕ ПИШЕМ В JSON ЗДЕСЬ! 
    # Запись должна быть ПОСЛЕ всех фильтров в analyze_and_send()
    
    # Пытаемся отправить в ntfy (может не работать)
    try:
        import subprocess
        subprocess.run([
            'curl', '-s',
            '-H', f'Title: {title}',
            '-H', 'Priority: 4',
            '-d', message,
            NTFY_URL
        ], check=True, timeout=5)
        logger.info(f"✅ Sent to ntfy: {symbol} {direction}")
        print(f"✅ Sent: {symbol} {direction}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to send: {symbol} - {e}")
        print(f"❌ Failed: {symbol}")
        return False


def _return_result(verdict_display: str, confidence: int, reasons_for: list, reasons_against: list, recommendations: list) -> dict:
    """
    Вспомогательная функция для формирования возвращаемого результата
    Определяет машинное значение verdict на основе display значения
    """
    # Определяем машинное значение
    verdict_machine = "BLOCK"  # По умолчанию
    if verdict_display in ["✅ ВХОДИТЬ", "✅ ШОРТИТЬ"]:
        verdict_machine = "ENTRY"
    elif verdict_display == "⏳ ЖДАТЬ":
        verdict_machine = "WAIT"
    
    # Формируем wait_conditions для WAIT
    wait_conditions = []
    if verdict_machine == "WAIT":
        # Извлекаем условия из recommendations
        for rec in recommendations:
            if rec.startswith("⏳ Ждать:") or rec.startswith("1️⃣") or rec.startswith("2️⃣") or rec.startswith("3️⃣") or rec.startswith("4️⃣") or "условия" in rec.lower():
                wait_conditions.append(rec)
    
    return {
        "verdict": verdict_machine,
        "verdict_display": verdict_display,
        "confidence": confidence,
        "reasons_for": reasons_for,
        "reasons_against": reasons_against,
        "wait_conditions": wait_conditions,
        "recommendations": recommendations
    }


def get_pending_signals():
    """Получить pending сигналы"""
    if not QUEUE_FILE.exists():
        return []
    
    with QUEUE_FILE.open('r', encoding='utf-8') as f:
        queue = json.load(f)
    
    return [x for x in queue if x.get('status') == 'pending']


def mark_processed(signal_id: str, status: str) -> None:
    """
    Отметить сигнал обработанным с новым статусом
    
    Args:
        signal_id: Уникальный ID сигнала (format: "SYMBOL_DIRECTION_TIMESTAMP")
        status: Новый статус ("approved", "rejected", "waiting")
    """
    if not QUEUE_FILE.exists():
        logger.warning(f"Queue file not found: {QUEUE_FILE}")
        return
    
    with QUEUE_FILE.open('r', encoding='utf-8') as f:
        queue = json.load(f)
    
    found = False
    for item in queue:
        # Ищем по signal_id (если есть)
        if item.get('signal_id') == signal_id:
            item['status'] = status
            item['processed_at'] = datetime.utcnow().isoformat() + "Z"
            found = True
            break
    
    # FALLBACK: если signal_id нет (старый формат), ищем по symbol+timestamp
    if not found:
        # Парсим signal_id: "SYMBOL_DIRECTION_TIMESTAMP"
        parts = signal_id.split('_')
        if len(parts) >= 3:
            symbol = parts[0]
            timestamp_str = '_'.join(parts[2:])  # На случай если в символе есть _
            
            for item in queue:
                sig = item.get('signal', {})
                item_symbol = sig.get('symbol', '')
                item_ts = item.get('timestamp', '')
                
                # Сравниваем symbol и timestamp
                if item_symbol == symbol and item_ts == timestamp_str and item.get('status') == 'pending':
                    item['status'] = status
                    item['processed_at'] = datetime.utcnow().isoformat() + "Z"
                    item['signal_id'] = signal_id  # Добавляем signal_id для следующего раза
                    found = True
                    logger.info(f"✅ Fallback: found signal by symbol+timestamp: {signal_id}")
                    break
    
    if not found:
        logger.error(f"❌ Signal not found in queue: {signal_id}")
        return
    
    # Атомарная запись через временный файл
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(QUEUE_FILE)
    
    logger.info(f"✅ Signal {signal_id} marked as {status}")


def _parse_structured_conditions(direction: str, recommendations: list, sig: dict) -> list:
    """
    Преобразует текстовые рекомендации в структурированные машинные условия
    
    Args:
        direction: "LONG" или "SHORT"
        recommendations: Список текстовых рекомендаций
        sig: Данные сигнала
        
    Returns:
        list: Структурированные условия в формате:
        [
            {"type": "rsi_below", "threshold": 35},
            {"type": "candle_bullish"},
            {"type": "indicator_match", "indicator": "vw_macd", "value": "bullish"},
            ...
        ]
    """
    conditions = []
    
    for rec in recommendations:
        rec_lower = rec.lower()
        
        # RSI условия
        if "rsi" in rec_lower:
            if direction == "LONG":
                if "< 35" in rec or "<35" in rec or "перепродан" in rec_lower:
                    conditions.append({"type": "rsi_below", "threshold": 35})
                elif "< 50" in rec or "<50" in rec:
                    conditions.append({"type": "rsi_below", "threshold": 50})
                elif "упадёт" in rec_lower and "61.8" in rec:
                    conditions.append({"type": "rsi_below", "threshold": 61.8, "note": "Fibonacci retracement"})
            else:  # SHORT
                if "> 65" in rec or ">65" in rec or "перекуплен" in rec_lower:
                    conditions.append({"type": "rsi_above", "threshold": 65})
                elif "> 50" in rec or ">50" in rec:
                    conditions.append({"type": "rsi_above", "threshold": 50})
                elif "поднимется" in rec_lower and "38.2" in rec:
                    conditions.append({"type": "rsi_above", "threshold": 38.2, "note": "Fibonacci retracement"})
        
        # Свечные паттерны
        if "bullish" in rec_lower and ("свеча" in rec_lower or "candle" in rec_lower):
            conditions.append({"type": "candle_bullish"})
        elif "bearish" in rec_lower and ("свеча" in rec_lower or "candle" in rec_lower):
            conditions.append({"type": "candle_bearish"})
        
        # VW-MACD
        if "vw-macd" in rec_lower or "macd" in rec_lower:
            if "bullish" in rec_lower or "развернётся на bullish" in rec_lower:
                conditions.append({"type": "indicator_match", "indicator": "vw_macd", "value": "bullish"})
            elif "bearish" in rec_lower or "развернётся на bearish" in rec_lower:
                conditions.append({"type": "indicator_match", "indicator": "vw_macd", "value": "bearish"})
            # MACD сила
            if "сила" in rec_lower and "atr" in rec_lower:
                if ">0.2" in rec or "> 0.2" in rec:
                    conditions.append({"type": "macd_strength_above", "threshold": 0.2, "unit": "ATR"})
        
        # VWAP
        if "vwap" in rec_lower:
            if "пробой вверх" in rec_lower or "break up" in rec_lower:
                conditions.append({"type": "vwap_break", "direction": "up"})
            elif "пробой вниз" in rec_lower or "break down" in rec_lower:
                conditions.append({"type": "vwap_break", "direction": "down"})
        
        # EMA
        if "ema" in rec_lower:
            if "bullish" in rec_lower or "станет bullish" in rec_lower:
                conditions.append({"type": "indicator_match", "indicator": "ema", "value": "bullish"})
            elif "bearish" in rec_lower or "станет bearish" in rec_lower:
                conditions.append({"type": "indicator_match", "indicator": "ema", "value": "bearish"})
        
        # Lorentzian
        if "lorentzian" in rec_lower or "lor" in rec_lower:
            if direction == "LONG":
                if "+1" in rec or "+3" in rec or "развернётся на +1" in rec_lower:
                    conditions.append({"type": "indicator_match", "indicator": "lorentzian", "value": "+1"})
            else:  # SHORT
                if "-1" in rec or "-3" in rec or "развернётся на -1" in rec_lower:
                    conditions.append({"type": "indicator_match", "indicator": "lorentzian", "value": "-1"})
        
        # HTF trend
        if "1h trend" in rec_lower or "htf" in rec_lower:
            if direction == "LONG":
                if "положительным" in rec_lower or ">-1.0%" in rec:
                    conditions.append({"type": "htf_trend_above", "threshold": -1.0, "timeframe": "1h"})
            else:  # SHORT
                if "отрицательным" in rec_lower or "<+1.0%" in rec:
                    conditions.append({"type": "htf_trend_below", "threshold": 1.0, "timeframe": "1h"})
        
        # Volume spike
        if "volume spike" in rec_lower or "объём" in rec_lower:
            if ">1.0x" in rec or "> 1.0x" in rec:
                conditions.append({"type": "volume_spike_above", "threshold": 1.0})
            elif ">1.5x" in rec or "> 1.5x" in rec:
                conditions.append({"type": "volume_spike_above", "threshold": 1.5})
        
        # Phase переход
        if "phase" in rec_lower or "фаза" in rec_lower:
            if "mid_expansion" in rec_lower:
                conditions.append({"type": "phase_transition", "target_phase": "MID_EXPANSION"})
    
    # Если ничего не распарсилось, добавляем общее условие "подтверждение"
    if not conditions:
        conditions.append({"type": "manual_confirmation", "note": "Требуется дополнительное подтверждение"})
    
    return conditions


def move_to_wait_queue(signal_id: str, result: dict) -> None:
    """
    Переместить сигнал в очередь ожидания (WAIT) со структурированными условиями
    
    Args:
        signal_id: Уникальный ID сигнала
        result: Результат анализа с verdict="WAIT", conditions, etc.
    """
    if not QUEUE_FILE.exists():
        logger.warning(f"Queue file not found: {QUEUE_FILE}")
        return
    
    # Читаем основную очередь
    with QUEUE_FILE.open('r', encoding='utf-8') as f:
        queue = json.load(f)
    
    # Ищем сигнал
    signal_item = None
    for item in queue:
        if item.get('signal_id') == signal_id or (
            item.get('signal', {}).get('symbol') == signal_id.split('_')[0] and 
            item.get('status') == 'pending'
        ):
            signal_item = item
            break
    
    if not signal_item:
        logger.error(f"❌ Signal not found for wait queue: {signal_id}")
        return
    
    sig = signal_item.get('signal', {})
    symbol = sig.get('symbol', 'UNKNOWN')
    direction = sig.get('direction', 'UNKNOWN')
    score = sig.get('score', 0)
    price = sig.get('price', 0)
    
    # Парсим структурированные условия из рекомендаций
    recommendations = result.get('recommendations', [])
    structured_conditions = _parse_structured_conditions(direction, recommendations, sig)
    
    # Формируем запись для wait_queue в ПРАВИЛЬНОМ формате
    wait_entry = {
        'signal_id': signal_id,
        'status': 'waiting',
        'symbol': symbol,
        'direction': direction,
        'score': score,
        'created_at': datetime.utcnow().isoformat() + "Z",
        'source_price': price,
        'conditions': structured_conditions,  # Структурированные машинные условия
        'conditions_text': result.get('wait_conditions', []),  # Текстовые условия для логов
        'features': {
            # Сохраняем текущие значения индикаторов для сравнения
            'rsi': sig.get('rsi'),
            'phase': sig.get('phase'),
            'vw_macd': sig.get('vw_macd'),
            'lorentzian': sig.get('lorentzian'),
            'volume_multiplier': sig.get('vol_multiplier'),
            'confidence': result.get('confidence', 5),
            'reasons_for': result.get('reasons_for', []),
            'reasons_against': result.get('reasons_against', [])
        },
        'execution_profile': 'FUNTIK_DEFAULT',
        'recheck_count': 0,
        'last_recheck': None
    }
    
    # Читаем wait_queue (или создаём новый)
    if WAIT_QUEUE_FILE.exists():
        with WAIT_QUEUE_FILE.open('r', encoding='utf-8') as f:
            try:
                wait_queue = json.load(f)
            except:
                wait_queue = []
    else:
        wait_queue = []
    
    # Добавляем в wait_queue
    wait_queue.append(wait_entry)
    
    # Записываем wait_queue атомарно
    tmp = WAIT_QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(wait_queue, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(WAIT_QUEUE_FILE)
    
    logger.info(f"✅ Signal {signal_id} moved to wait_queue")
    logger.info(f"   Conditions: {len(structured_conditions)} structured, {len(result.get('wait_conditions', []))} text")
    
    # Помечаем в основной очереди как "waiting"
    mark_processed(signal_id, "waiting")


def analyze_and_send(signal_data: dict) -> dict:
    """
    Детальный анализ и отправка в ntfy - ИСПОЛЬЗУЕМ УМНЫЙ FALLBACK (LightGBM + индикаторы)
    
    Returns:
        dict: {
            "verdict": "ENTRY" | "WAIT" | "BLOCK",
            "verdict_display": "✅ ВХОДИТЬ" | "⏳ ЖДАТЬ" | "⚠️ НЕ ВХОДИТЬ",
            "confidence": int (0-10),
            "reasons_for": list[str],
            "reasons_against": list[str],
            "wait_conditions": list[str],  # Только для WAIT
            "recommendations": list[str]
        }
    """
    sig = signal_data['signal']
    full_text = sig.get('full_text', '')
    
    symbol = sig['symbol']
    direction = sig['direction']
    score = sig['score']
    price = sig['price']
    
    # 🔥 ОТКЛЮЧЕН Claude (26.09.2026) - используем только умный fallback
    # Умный fallback = LightGBM + все индикаторы + проверенная логика
    use_claude_result = False
    logger.info(f"ℹ️ Используем умный fallback (LightGBM + индикаторы)")
    
    # FALLBACK: Умная логика с LightGBM и индикаторами
    # СНАЧАЛА читаем индикаторы напрямую из scanner.log
    scanner_indicators = read_indicators_from_scanner_log(symbol, direction)
    
    # Парсим данные из full_text (старый способ - для совместимости)
    rsi = phase = vw_macd = lor = lor_pred = absorption = vwap = ema = volume = bb_trend = oi = None
    macd_hist = None
    htf_filter_block = False
    
    for line in full_text.split('\n'):
        # RSI: новый формат "RSI: 5m 51 / 1h 35"
        if 'RSI:' in line:
            try:
                # Берём значение 1h если есть, иначе 5m
                if '1h' in line:
                    rsi = int(line.split('1h')[1].strip().split()[0])
                else:
                    rsi = int(line.split('RSI:')[1].strip().split()[0])
            except:
                pass
        
        # Phase: новый формат "фаза EARLY_EXPANSION"
        if 'фаза' in line or 'Phase:' in line:
            try:
                if 'фаза' in line:
                    phase = line.split('фаза')[1].strip().split()[0]
                else:
                    phase = line.split('Phase:')[1].strip().split()[0]
            except:
                pass
        
        # VW-MACD: формат "VW-MACD: bearish (сила 0.01ATR)" или "VW-MACD bearish"
        if 'VW-MACD' in line:
            vw_macd = 'bearish' if 'bearish' in line else 'bullish'
        
        # MACD hist
        if 'MACD:' in line:
            try:
                if 'h=' in line:
                    macd_hist = float(line.split('h=')[1].split(')')[0])
            except:
                pass
        
        # Lorentzian: формат "Lor: +1 (6%)"
        if 'Lor:' in line:
            try:
                lor_text = line.split('Lor:')[1].strip()
                lor = lor_text.split()[0].replace('(', '').replace(')', '')  # +1, -1, 0
                lor_pred = lor  # В новом формате pred = значение
            except:
                pass
        
        # OBV: формат "OBV -1" или "OBV распределение"
        if 'OBV' in line:
            if 'распределение' in line:
                absorption = 'SHORT'  # Распределение = шорты накапливаются
            elif 'накопление' in line:
                absorption = 'LONG'  # Накопление = лонги накапливаются
        
        # VWAP: формат "VWAP: -0.44% (-0.4s)" или "ниже VWAP (-0.44%)"
        if 'VWAP' in line:
            try:
                if '%' in line:
                    vwap_pct = line.split('%')[0].split()[-1]
                    vwap = float(vwap_pct)
            except:
                pass
        
        # EMA: формат "EMA9/21 SHORT (-0.14%)" или "EMA: bearish"
        if 'EMA' in line:
            ema = 'bearish' if ('bearish' in line or 'SHORT' in line) else 'bullish'
        
        # Volume: формат "Объём: x0.8 z=-0.5 ($36,706)"
        if 'Объём:' in line or 'объём $' in line:
            try:
                if '$' in line:
                    volume = line.split('$')[1].split(')')[0].replace(',', '')
            except:
                pass
        
        # 1h trend: формат "Тренд 1h: MA8/18 -1 (-0.44%)" или "1h trend: -0.5%"
        if 'Тренд 1h:' in line or '1h trend:' in line:
            try:
                # Извлекаем процент в скобках
                if '(' in line and '%' in line:
                    bb_trend = line.split('(')[1].split('%')[0] + '%'
            except:
                pass
        
        # OI: формат "OI: +0.6% 1h (bybit)"
        if 'OI:' in line:
            try:
                oi = line.split('OI:')[1].strip().split()[0]
            except:
                pass
        
        # HTF filter / модель: формат без изменений
        if 'HTF FILTER блокировка' in line or 'LGBM: блокировка' in line:
            htf_filter_block = True
    
    # ПРИОРИТЕТ: если индикаторы не найдены в full_text, берём из scanner.log
    if rsi is None and 'rsi' in scanner_indicators:
        rsi = scanner_indicators['rsi']
    if phase is None and 'phase' in scanner_indicators:
        phase = scanner_indicators['phase']
    if vw_macd is None and 'vw_macd' in scanner_indicators:
        vw_macd = scanner_indicators['vw_macd']
    if vwap is None and 'vwap' in scanner_indicators:
        vwap = scanner_indicators['vwap']
    if ema is None and 'ema' in scanner_indicators:
        ema = scanner_indicators['ema']
    
    # MACD strength (если близко к 0 = свежее пересечение)
    macd_strength_atr = scanner_indicators.get('macd_strength_atr') if scanner_indicators else None
    
    # DEBUG: Логируем начальные данные
    logger.info(f"🔍 START ANALYZE: {symbol} {direction} | phase={phase} rsi={rsi} ema={ema} vw_macd={vw_macd}")
    
    # ПОЛУЧАЕМ ПРЕДСКАЗАНИЕ МОДЕЛИ lightgbm_scorer (с ВСЕМИ фичами из scanner.log)
    prob_win = get_model_prediction(symbol, direction, score)
    
    if prob_win is not None:
        logger.info(f"🤖 LightGBM scorer: prob_win={prob_win:.1%}")
    
    # ДЕТАЛЬНЫЙ АНАЛИЗ
    reasons_for = []
    reasons_against = []
    confidence = 5
    verdict = "⏳ ЖДАТЬ"
    recommendations = []
    
    # Модель влияет на confidence
    if prob_win is not None:
        logger.info(f"🤖 Model prediction: prob_win={prob_win:.2%}")
        if prob_win >= 0.55:
            reasons_for.append(f"✅ Модель: {prob_win:.0%} WIN")
            confidence += 2
        elif prob_win < 0.45:
            reasons_against.append(f"⚠️ Модель: {prob_win:.0%} WIN (низкая)")
            confidence -= 3
        else:
            # Нейтральная зона 0.45-0.55 - не влияет на confidence
            reasons_for.append(f"⚪ Модель: {prob_win:.0%} WIN (нейтрально)")
    else:
        logger.info("🤖 Model prediction: unavailable (prob_win=None)")
    
    # ========================================
    # SCORE = 5 → сразу в ЖДАТЬ с условиями
    # ========================================
    if score == 5:
        verdict = "⏳ ЖДАТЬ"
        recommendations.append("⏳ Score=5 - дождись подтверждения:")
        
        if direction == "LONG":
            recommendations.append("1️⃣ RSI < 35 (перепродан)")
            recommendations.append("2️⃣ Bullish свеча (close > prev)")
            recommendations.append("3️⃣ VW-MACD bullish")
            recommendations.append("4️⃣ VWAP пробой вверх")
        else:  # SHORT
            recommendations.append("1️⃣ RSI > 65 (перекуплен)")
            recommendations.append("2️⃣ Bearish свеча (close < prev)")
            recommendations.append("3️⃣ VW-MACD bearish")
            recommendations.append("4️⃣ VWAP пробой вниз")
        
        # Отправляем в ntfy
        send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
        
        # Возвращаем результат
        return {
            "verdict": "WAIT",
            "verdict_display": verdict,
            "confidence": confidence,
            "reasons_for": reasons_for,
            "reasons_against": reasons_against,
            "wait_conditions": recommendations,  # Все recommendations как условия
            "recommendations": recommendations
        }
    
    # ========================================
    # SCORE >= 6 → детальный анализ с моделью
    # ========================================
    
    if direction == "LONG":
        # ========================================
        # КРИТИЧЕСКИЕ БЛОКИРУЮЩИЕ ФИЛЬТРЫ (проверка в начале!)
        # ========================================
        blocked = False
        
        # 1. Phase=LATE_EXPANSION для LONG - это ВЕРШИНА!
        if phase == 'LATE_EXPANSION':
            reasons_against.append(f"⚠️ Phase={phase} - ВЕРШИНА распределения, возможна коррекция")
            confidence -= 3
            verdict = "⏳ ЖДАТЬ"
            recommendations.append("⏳ LATE_EXPANSION - дождись завершения коррекции и разворота вверх:")
            recommendations.append("1️⃣ RSI упадёт <50 (коррекция завершена)")
            recommendations.append("2️⃣ Появится bullish свеча (close выше предыдущей)")
            recommendations.append("3️⃣ RSI начнёт расти (текущий RSI > предыдущий)")
            recommendations.append("4️⃣ Все индикаторы bullish (EMA + VW-MACD + Lorentzian)")
            recommendations.append("✅ Вход: когда все 4 условия выполнены")
            logger.info(f"⏳ Phase=LATE_EXPANSION - ЖДАТЬ разворота для LONG {symbol}")
            blocked = True
        
        # 2. HTF filter блокировка
        if htf_filter_block and 'LONG' in full_text and 'блокировка LONG' in full_text:
            reasons_against.append("🛑 HTF FILTER блокирует LONG")
            confidence = 0
            verdict = "⚠️ НЕ ВХОДИТЬ"
            recommendations.append("❌ Модель блокирует этот вход!")
            logger.info(f"🛑 HTF FILTER блокировка LONG для {symbol}")
            blocked = True
        
        # 3. RSI > 70 - экстремальная перекупленность (блокировка)
        # Для wait_queue будет проверка Fibonacci 61.8
        if rsi and rsi > 70:
            reasons_against.append(f"🛑 RSI={rsi} ПЕРЕКУПЛЕН (>70) - экстремальный риск!")
            confidence = 0
            verdict = "⚠️ НЕ ВХОДИТЬ"
            recommendations.append(f"❌ RSI={rsi} >70 - экстремальная перекупленность!")
            recommendations.append(f"⏳ Ждать: RSI упадёт <61.8 (Fibonacci) и появится bullish разворот")
            logger.info(f"🛑 RSI={rsi} >70 блокировка LONG для {symbol}")
            blocked = True
        
        # 4. Свежий MACD ПРОТИВ направления (только что развернулся bearish)
        # Блокируем LONG если MACD только стал bearish - это разворот ПРОТИВ нас!
        if vw_macd == 'bearish' and macd_strength_atr is not None and macd_strength_atr < 0.15:
            reasons_against.append(f"🛑 MACD свежий ПРОТИВ (bearish, сила {macd_strength_atr:.2f}ATR)")
            confidence -= 3
            verdict = "⏳ ЖДАТЬ"
            recommendations.append(f"⚠️ MACD только развернулся на bearish (сила {macd_strength_atr:.2f}ATR) - ПРОТИВ LONG!")
            recommendations.append(f"⏳ Ждать разворот обратно:")
            recommendations.append(f"  1️⃣ MACD развернётся на bullish")
            recommendations.append(f"  2️⃣ MACD сила >0.2 ATR (устойчивый)")
            logger.info(f"🛑 MACD свежий bearish ({macd_strength_atr:.2f}ATR) ПРОТИВ LONG для {symbol}")
            blocked = True
        
        # Если заблокировано - сразу отправляем и возвращаем результат
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return _return_result(verdict, confidence, reasons_for, reasons_against, recommendations)
        
        # ========================================
        # СТРОГИЕ ФИЛЬТРЫ (анализ backtest 18% winrate)
        # ========================================
        
        # 3. КРИТИЧЕСКИЙ ФИЛЬТР: 1h trend (точность 90%)
        if bb_trend:
            try:
                trend_val = float(bb_trend.replace('%', '').replace('+', ''))
                if trend_val < 0:
                    # 🔥 СМЯГЧЕНИЕ: если Phase=EXHAUSTION - снижаем штраф (фаза сильнее тренда!)
                    penalty = -2 if phase == 'EXHAUSTION' else -5
                    reasons_against.append(f"🛑 1h trend={bb_trend} ПРОТИВ LONG (точность 90%)")
                    confidence += penalty
                elif trend_val > 3.0:
                    # FOMO FILTER: цена уже выросла >3% за 1ч - опасно!
                    reasons_against.append(f"⚠️ 1h trend={bb_trend} - слишком поздно (FOMO)")
                    confidence -= 3
                else:
                    reasons_for.append(f"✅ 1h trend={bb_trend} попутный")
                    confidence += 2
            except:
                pass
        
        # 3. КРИТИЧЕСКИЙ ФИЛЬТР: Lorentzian (точность 87.5%)
        # 🔥 FIX 21.09.2026: ЖЁСТКАЯ БЛОКИРОВКА вместо штрафа!
        if lor_pred:
            if '-1' in lor_pred or '-3' in lor_pred:
                # БЛОКИРОВКА: Lorentzian показывает SHORT, мы пытаемся LONG!
                reasons_against.append(f"🛑 Lorentzian pred={lor_pred} ПРОТИВ LONG (точность 87%)")
                confidence = 0
                verdict = "⚠️ НЕ ВХОДИТЬ"
                recommendations.append(f"❌ Lorentzian показывает SHORT ({lor_pred}) - ПРОТИВ нашего направления!")
                recommendations.append(f"⏳ Ждать: Lorentzian развернётся на +1 или +3 (LONG)")
                logger.info(f"🛑 Lorentzian SHORT ({lor_pred}) блокировка LONG для {symbol}")
                blocked = True
            elif '+1' in lor_pred or '+3' in lor_pred:
                reasons_for.append(f"✅ Lorentzian pred={lor_pred} ЗА LONG")
                confidence += 2
        
        # Если заблокировано - сразу отправляем и возвращаем результат
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return _return_result(verdict, confidence, reasons_for, reasons_against, recommendations)
        
        # 4. КРИТИЧЕСКИЙ ФИЛЬТР: EMA (точность 80%)
        if ema == 'bearish':
            # 🔥 СМЯГЧЕНИЕ: если Phase=EXHAUSTION - снижаем штраф
            penalty = -1 if phase == 'EXHAUSTION' else -3
            reasons_against.append("🛑 EMA bearish ПРОТИВ LONG (точность 80%)")
            confidence += penalty
        elif ema == 'bullish':
            reasons_for.append("✅ EMA bullish ЗА LONG")
            confidence += 2
        
        # 5. КРИТИЧЕСКИЙ ФИЛЬТР: VW-MACD (точность 72.7%)
        if vw_macd == 'bearish':
            # 🔥 СМЯГЧЕНИЕ: если Phase=EXHAUSTION - снижаем штраф
            penalty = -1 if phase == 'EXHAUSTION' else -3
            reasons_against.append("🛑 VW-MACD bearish ПРОТИВ LONG (точность 73%)")
            confidence += penalty
        elif vw_macd == 'bullish':
            reasons_for.append("✅ VW-MACD bullish ЗА LONG")
            confidence += 2
        
        # 6. КРИТИЧЕСКИЙ ФИЛЬТР: Volume Spike > 1.0x
        # 🔥 FIX 21.09.2026: Требуем минимальный объём!
        vol_spike = scanner_indicators.get('volume_spike', 1.0) if scanner_indicators else 1.0
        
        if vol_spike < 1.0:
            reasons_against.append(f"🛑 Volume Spike {vol_spike:.2f}x < 1.0x - слабый объём!")
            confidence -= 3
            verdict = "⏳ ЖДАТЬ"
            recommendations.append(f"⚠️ Низкий volume spike (×{vol_spike:.2f}) - недостаточно силы для входа!")
            recommendations.append("⏳ Ждать: Volume spike вырастет >1.0x")
            logger.info(f"⚠️ LONG {symbol}: vol_spike={vol_spike:.2f} < 1.0x - БЛОКИРОВКА")
            blocked = True
        elif vol_spike >= 1.5:
            reasons_for.append(f"✅ Volume Spike {vol_spike:.2f}x - сильный объём")
            confidence += 2
        
        # Если заблокировано - сразу отправляем и возвращаем результат
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return _return_result(verdict, confidence, reasons_for, reasons_against, recommendations)
        
        # 7. ДОПОЛНИТЕЛЬНЫЕ ФАКТОРЫ (не критичные)
        if absorption == 'LONG':
            reasons_for.append("Абсорбция LONG (крупные игроки)")
            confidence += 1
        
        if vwap and isinstance(vwap, str) and 'below' in vwap:
            reasons_for.append("Цена ниже VWAP (oversold)")
            confidence += 1
        
        # Определяем volume_high ЗАРАНЕЕ для использования в проверках
        volume_high = volume and float(volume) > 1000000
        
        # 🔥 КРИТИЧЕСКИ ВАЖНО: Phase=EXHAUSTION для LONG - это дно после распродажи!
        phase_boost = 0
        rsi_extreme_oversold = rsi and rsi < 25
        
        if phase in ['EXHAUSTION']:
            phase_boost = 4  # Сильный бонус за фазу истощения (дно)
            reasons_for.append(f"🔥 Phase={phase} (ИСТОЩЕНИЕ - ДНО для LONG)")
            confidence += phase_boost
            
            # 🔥🔥 ЭКСТРЕМУМ: RSI<25 + EXHAUSTION + Volume - это СИЛЬНЕЙШИЙ сигнал!
            if rsi_extreme_oversold and volume_high:
                extreme_boost = 2
                reasons_for.append(f"🔥🔥 ЭКСТРЕМ: RSI={rsi} + Phase + Volume - дно!")
                confidence += extreme_boost
        elif phase in ['EARLY_EXPANSION']:
            # 🔥 ФИЛЬТР EARLY_EXPANSION (15.09.2026)
            # ПРОБЛЕМА: 100% убыточных сделок 15.09 были EARLY_EXPANSION
            # EARLY_EXPANSION = ранний рост после падения, но может быть dead cat bounce!
            # Требуем: 1) Volume spike >1.5x  2) HTF тренд в нашу сторону (>-1.0% для LONG)
            
            vol_spike = scanner_indicators.get('volume_spike', 1.0) if scanner_indicators else 1.0
            
            # Проверка HTF тренда из bb_trend
            htf_ok = True
            if bb_trend:
                try:
                    trend_val = float(bb_trend.replace('%', '').replace('+', ''))
                    # LONG: требуем HTF > -3.0% (не сильно bearish) - ОСЛАБЛЕНО 15.09.2026
                    if trend_val < -3.0:
                        htf_ok = False
                        reasons_against.append(f"🛑 EARLY_EXPANSION + HTF={bb_trend} bearish - dead cat bounce!")
                        confidence -= 3
                        verdict = "⏳ ЖДАТЬ"
                        recommendations.append(f"⚠️ EARLY_EXPANSION на bearish HTF - dead cat bounce!")
                        recommendations.append(f"⏳ Ждать: HTF развернётся >-1.0% ИЛИ Phase перейдёт в MID_EXPANSION")
                        logger.info(f"🛑 EARLY_EXPANSION LONG {symbol}: HTF={bb_trend} bearish (dead cat bounce)")
                        blocked = True
                except:
                    pass
            
            # Проверка volume spike
            if not blocked and vol_spike < 1.5:
                reasons_against.append(f"🛑 EARLY_EXPANSION + volume_spike={vol_spike:.2f} <1.5x - слабо!")
                confidence -= 2
                verdict = "⏳ ЖДАТЬ"
                recommendations.append(f"⚠️ EARLY_EXPANSION без volume spike - недостаточно силы!")
                recommendations.append(f"⏳ Ждать: Volume spike >1.5x ИЛИ Phase перейдёт в MID_EXPANSION")
                logger.info(f"🛑 EARLY_EXPANSION LONG {symbol}: vol_spike={vol_spike:.2f} <1.5x")
                blocked = True
            
            if not blocked and htf_ok and vol_spike >= 1.5:
                reasons_for.append(f"✅ Phase={phase} (ранний рост) + vol_spike={vol_spike:.2f}x + HTF OK")
                confidence += 1
        if volume_high:
            reasons_for.append(f"Объём ${volume} >$1M")
            confidence += 1
        
        # 8. ИГНОРИРУЕМ RSI - точность 0%!
        # НЕ ДОБАВЛЯЕМ ЕГО В АНАЛИЗ!
        
        # ========================================
        # ВЕРДИКТ (ГИБКО для Phase!)
        # ========================================
        
        # Калибровка 15.08.2026: снижаем порог с 7/8 до 5 (упущено 28 TP)
        min_confidence = 5
        
        # 🔥 НОВЫЙ ФИЛЬТР СЛАБЫХ СИГНАЛОВ (13.09.2026)
        # Если мало подтверждений (≤2) И модель слабая (<45%), блокируем
        reasons_for_count = len([r for r in reasons_for if not r.startswith('⚪')])
        weak_signal = reasons_for_count <= 2 and prob_win is not None and prob_win < 0.45
        
        if weak_signal:
            verdict = "⏳ ЖДАТЬ"
            reasons_against.append(f"⚠️ Слабый сигнал: только {reasons_for_count} подтверждения + модель {prob_win:.0%}")
            confidence = 3
            recommendations.append("⏳ Недостаточно подтверждений для входа")
            recommendations.append("🔍 Ждать: Lorentzian, Phase или Volume spike")
            logger.info(f"⚠️ Слабый сигнал LONG {symbol}: reasons_for={reasons_for_count} prob_win={prob_win:.2%}")
        elif confidence >= min_confidence:
            verdict = "✅ ВХОДИТЬ"
            
            # 🔥 АНАЛИТИЧЕСКИЕ рекомендации (НЕ торговый план!)
            # Реальный план исполняет PumpDumpReversalStrategy_v2.py
            recommendations.append(f"💰 Вход: ${price:.6f}")
            recommendations.append(f"📊 Confidence: {confidence}/10")
            recommendations.append(f"⚙️ План исполняет: PumpDumpReversalStrategy_v2")
            recommendations.append(f"   └─ TP1: 30% @ +0.3%")
            recommendations.append(f"   └─ TP2: 30% @ +0.8%")
            recommendations.append(f"   └─ Runner: 40% @ +1.3%")
            recommendations.append(f"   └─ SL: -1% (hard)")
            recommendations.append(f"   └─ Runner Protection: активна")
            
        elif confidence >= 5:
            verdict = "⏳ ЖДАТЬ"
            
            wait_conditions = []
            
            if vw_macd == 'bearish':
                wait_conditions.append("VW-MACD развернётся на bullish")
            
            if ema == 'bearish':
                wait_conditions.append("EMA станет bullish")
            
            if bb_trend and float(bb_trend.replace('%', '').replace('+', '')) < 0:
                wait_conditions.append("1h trend станет положительным")
            
            if lor_pred and ('-1' in lor_pred or '-3' in lor_pred):
                wait_conditions.append("Lorentzian развернётся")
            
            if not wait_conditions:
                wait_conditions.append("дополнительное подтверждение")
            
            recommendations.append(f"⏰ Ждать: {' ИЛИ '.join(wait_conditions[:2])}")
            recommendations.append(f"⚠️ Сейчас confidence={confidence}/10 (нужно ≥8)")
            
        else:
            verdict = "⚠️ НЕ ВХОДИТЬ"
            recommendations.append(f"❌ Слишком много против (confidence={confidence}/10)")
            recommendations.append("🛑 Главные индикаторы против входа")
    
    elif direction == "SHORT":
        # ========================================
        # КРИТИЧЕСКИЕ БЛОКИРУЮЩИЕ ФИЛЬТРЫ (проверка в начале!)
        # ========================================
        blocked = False
        
        # 1. Phase=EXHAUSTION для SHORT - это ДНО!
        if phase == 'EXHAUSTION':
            reasons_against.append(f"⚠️ Phase={phase} - ДНО после падения, возможен отскок")
            confidence -= 3
            verdict = "⏳ ЖДАТЬ"
            recommendations.append("⏳ EXHAUSTION - дождись завершения отскока и разворота вниз:")
            recommendations.append("1️⃣ RSI поднимется >50 (отскок завершён)")
            recommendations.append("2️⃣ Появится bearish свеча (close ниже предыдущей)")
            recommendations.append("3️⃣ RSI начнёт падать (текущий RSI < предыдущий)")
            recommendations.append("4️⃣ Все индикаторы bearish (EMA + VW-MACD + Lorentzian)")
            recommendations.append("✅ Вход: когда все 4 условия выполнены")
            logger.info(f"⏳ Phase=EXHAUSTION - ЖДАТЬ разворота для SHORT {symbol}")
            blocked = True
        
        # 2. RSI < 30 - экстремальная перепроданность (блокировка)
        # Для wait_queue будет проверка Fibonacci 38.2
        if rsi and rsi < 30:
            reasons_against.append(f"🛑 RSI={rsi} ПЕРЕПРОДАН (<30) - экстремальный риск отскока!")
            confidence = 0
            verdict = "🚫 НЕ ШОРТИТЬ"
            recommendations.append(f"❌ RSI={rsi} <30 - экстремальная перепроданность!")
            recommendations.append(f"⏳ Ждать: RSI поднимется >38.2 (Fibonacci) и появится bearish разворот")
            logger.info(f"🛑 RSI={rsi} <30 блокировка SHORT для {symbol}")
            blocked = True
        
        # 3. Свежий MACD ПРОТИВ направления (только что развернулся bullish)
        # Блокируем SHORT если MACD только стал bullish - это разворот ПРОТИВ нас!
        if vw_macd == 'bullish' and macd_strength_atr is not None and macd_strength_atr < 0.15:
            reasons_against.append(f"🛑 MACD свежий ПРОТИВ (bullish, сила {macd_strength_atr:.2f}ATR)")
            confidence -= 3
            verdict = "⏳ ЖДАТЬ"
            recommendations.append(f"⚠️ MACD только развернулся на bullish (сила {macd_strength_atr:.2f}ATR) - ПРОТИВ SHORT!")
            recommendations.append(f"⏳ Ждать разворот обратно:")
            recommendations.append(f"  1️⃣ MACD развернётся на bearish")
            recommendations.append(f"  2️⃣ MACD сила >0.2 ATR (устойчивый)")
            logger.info(f"🛑 MACD свежий bullish ({macd_strength_atr:.2f}ATR) ПРОТИВ SHORT для {symbol}")
            blocked = True
        
        # 4. HTF filter блокировка
        if htf_filter_block and 'SHORT' in full_text and 'блокировка SHORT' in full_text:
            reasons_against.append("🛑 HTF FILTER блокирует SHORT")
            confidence = 0
            verdict = "🚫 НЕ ШОРТИТЬ"
            recommendations.append("❌ Модель блокирует этот вход!")
            logger.info(f"🛑 HTF FILTER блокировка SHORT для {symbol}")
            blocked = True
        
        # Если заблокировано - сразу отправляем и возвращаем результат
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return _return_result(verdict, confidence, reasons_for, reasons_against, recommendations)
        
        # ========================================
        # СТРОГИЕ ФИЛЬТРЫ (анализ backtest 18% winrate)
        # ========================================
        
        # 4. КРИТИЧЕСКИЙ ФИЛЬТР: 1h trend (точность 90%)
        if bb_trend:
            try:
                trend_val = float(bb_trend.replace('%', '').replace('+', ''))
                if trend_val > 0:
                    # 🔥 СМЯГЧЕНИЕ: если Phase=LATE_EXPANSION - снижаем штраф (фаза сильнее тренда!)
                    penalty = -2 if phase == 'LATE_EXPANSION' else -5
                    reasons_against.append(f"🛑 1h trend={bb_trend} ПРОТИВ SHORT (точность 90%)")
                    confidence += penalty
                elif trend_val < -3.0:
                    # FOMO FILTER: цена уже упала >3% за 1ч - опасно!
                    reasons_against.append(f"⚠️ 1h trend={bb_trend} - слишком поздно (FOMO)")
                    confidence -= 3
                else:
                    reasons_for.append(f"✅ 1h trend={bb_trend} попутный")
                    confidence += 2
            except:
                pass
        
        # 4. КРИТИЧЕСКИЙ ФИЛЬТР: Lorentzian (точность 87.5%)
        # 🔥 FIX 21.09.2026: ЖЁСТКАЯ БЛОКИРОВКА вместо штрафа!
        if lor_pred:
            if '+1' in lor_pred or '+3' in lor_pred:
                # БЛОКИРОВКА: Lorentzian показывает LONG, мы пытаемся SHORT!
                reasons_against.append(f"🛑 Lorentzian pred={lor_pred} ПРОТИВ SHORT (точность 87%)")
                confidence = 0
                verdict = "⚠️ НЕ ШОРТИТЬ"
                recommendations.append(f"❌ Lorentzian показывает LONG ({lor_pred}) - ПРОТИВ нашего направления!")
                recommendations.append(f"⏳ Ждать: Lorentzian развернётся на -1 или -3 (SHORT)")
                logger.info(f"🛑 Lorentzian LONG ({lor_pred}) блокировка SHORT для {symbol}")
                blocked = True
            elif '-1' in lor_pred or '-3' in lor_pred:
                reasons_for.append(f"✅ Lorentzian pred={lor_pred} ЗА SHORT")
                confidence += 2
        
        # Если заблокировано - сразу отправляем и возвращаем результат
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return _return_result(verdict, confidence, reasons_for, reasons_against, recommendations)
        
        # 5. КРИТИЧЕСКИЙ ФИЛЬТР: EMA (точность 80%)
        if ema == 'bullish':
            # 🔥 СМЯГЧЕНИЕ: если Phase=LATE_EXPANSION - снижаем штраф
            # ⚠️ НО НЕ для EXHAUSTION! EXHAUSTION = дно, возможен отскок!
            if phase == 'EXHAUSTION':
                penalty = -3  # ЖЁСТКИЙ ШТРАФ
                reasons_against.append("🛑 EMA bullish + Phase=EXHAUSTION ПРОТИВ SHORT - отскок!")
            else:
                penalty = -1 if phase == 'LATE_EXPANSION' else -3
                reasons_against.append("🛑 EMA bullish ПРОТИВ SHORT (точность 80%)")
            confidence += penalty
        elif ema == 'bearish':
            reasons_for.append("✅ EMA bearish ЗА SHORT")
            confidence += 2
        
        # 6. КРИТИЧЕСКИЙ ФИЛЬТР: VW-MACD (точность 72.7%)
        if vw_macd == 'bullish':
            # 🔥 СМЯГЧЕНИЕ: если Phase=LATE_EXPANSION - снижаем штраф
            # ⚠️ НО НЕ для EXHAUSTION! EXHAUSTION = дно, возможен отскок!
            if phase == 'EXHAUSTION':
                penalty = -3  # ЖЁСТКИЙ ШТРАФ
                reasons_against.append("🛑 VW-MACD bullish + Phase=EXHAUSTION ПРОТИВ SHORT - отскок!")
            else:
                penalty = -1 if phase == 'LATE_EXPANSION' else -3
                reasons_against.append("🛑 VW-MACD bullish ПРОТИВ SHORT (точность 73%)")
            confidence += penalty
        elif vw_macd == 'bearish':
            reasons_for.append("✅ VW-MACD bearish ЗА SHORT")
            confidence += 2
        
        # 7. КРИТИЧЕСКИЙ ФИЛЬТР: Volume Spike > 1.0x
        # 🔥 FIX 21.09.2026: Требуем минимальный объём!
        vol_spike = scanner_indicators.get('volume_spike', 1.0) if scanner_indicators else 1.0
        
        if vol_spike < 1.0:
            reasons_against.append(f"🛑 Volume Spike {vol_spike:.2f}x < 1.0x - слабый объём!")
            confidence -= 3
            verdict = "⏳ ЖДАТЬ"
            recommendations.append(f"⚠️ Низкий volume spike (×{vol_spike:.2f}) - недостаточно силы для входа!")
            recommendations.append("⏳ Ждать: Volume spike вырастет >1.0x")
            logger.info(f"⚠️ SHORT {symbol}: vol_spike={vol_spike:.2f} < 1.0x - БЛОКИРОВКА")
            blocked = True
        elif vol_spike >= 1.5:
            reasons_for.append(f"✅ Volume Spike {vol_spike:.2f}x - сильный объём")
            confidence += 2
        
        # Если заблокировано - сразу отправляем и возвращаем результат
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return _return_result(verdict, confidence, reasons_for, reasons_against, recommendations)
        
        # 6. VW-MACD SHORT
        if vw_macd == 'bullish':
            if phase == 'EXHAUSTION':
                penalty = -3  # ЖЁСТКИЙ ШТРАФ
                reasons_against.append("🛑 VW-MACD bullish + Phase=EXHAUSTION ПРОТИВ SHORT - отскок!")
            else:
                penalty = -1 if phase == 'LATE_EXPANSION' else -3
                reasons_against.append("🛑 VW-MACD bullish ПРОТИВ SHORT (точность 73%)")
            confidence += penalty
        elif vw_macd == 'bearish':
            reasons_for.append("✅ VW-MACD bearish ЗА SHORT")
            confidence += 2
        
        # 7. МОДЕЛЬ (если есть)
        # ВАЖНО: Модель на 54-56% это почти случайность, НО:
        # - 21.08: 119 одобрено (порог 0.55) → +$139 прибыль
        # - 23.08: 63 одобрено (тот же порог) → -$182 убыток
        
        # 7. ДОПОЛНИТЕЛЬНЫЕ ФАКТОРЫ (не критичные)
        if absorption == 'SHORT':
            reasons_for.append("Абсорбция SHORT (крупные игроки)")
            confidence += 1
        
        if vwap and isinstance(vwap, str) and 'above' in vwap:
            reasons_for.append("Цена выше VWAP (overbought)")
            confidence += 1
        
        # Определяем volume_high ЗАРАНЕЕ для использования в проверках
        volume_high = volume and float(volume) > 1000000
        
        # 🔥 КРИТИЧЕСКИ ВАЖНО: Phase=LATE_EXPANSION для SHORT - это фаза РАСПРЕДЕЛЕНИЯ (TOP)!
        phase_boost = 0
        rsi_extreme_overbought = rsi and rsi > 75
        
        if phase in ['LATE_EXPANSION']:
            phase_boost = 4  # Сильный бонус за фазу распределения
            reasons_for.append(f"🔥 Phase={phase} (РАСПРЕДЕЛЕНИЕ - ТОП для SHORT)")
            confidence += phase_boost
            
            # 🔥🔥 ЭКСТРЕМУМ: RSI>75 + LATE_EXPANSION + Volume - это СИЛЬНЕЙШИЙ сигнал!
            if rsi_extreme_overbought and volume_high:
                extreme_boost = 2
                reasons_for.append(f"🔥🔥 ЭКСТРЕМ: RSI={rsi} + Phase + Volume - вершина!")
                confidence += extreme_boost
        elif phase in ['EARLY_EXPANSION']:
            # 🔥 ФИЛЬТР EARLY_EXPANSION (15.09.2026)
            # ПРОБЛЕМА: 100% убыточных сделок 15.09 были EARLY_EXPANSION
            # Для SHORT: EARLY_EXPANSION = начало падения, но может быть false breakdown!
            # Требуем: 1) Volume spike >1.5x  2) HTF тренд в нашу сторону (<+1.0% для SHORT)
            
            vol_spike = scanner_indicators.get('volume_spike', 1.0) if scanner_indicators else 1.0
            
            # Проверка HTF тренда из bb_trend
            htf_ok = True
            if bb_trend:
                try:
                    trend_val = float(bb_trend.replace('%', '').replace('+', ''))
                    # SHORT: требуем HTF < +3.0% (не сильно bullish) - ОСЛАБЛЕНО 15.09.2026
                    if trend_val > +3.0:
                        htf_ok = False
                        reasons_against.append(f"🛑 EARLY_EXPANSION + HTF={bb_trend} bullish - false breakdown!")
                        confidence -= 3
                        verdict = "⏳ ЖДАТЬ"
                        recommendations.append(f"⚠️ EARLY_EXPANSION на bullish HTF - false breakdown!")
                        recommendations.append(f"⏳ Ждать: HTF развернётся <+1.0% ИЛИ Phase перейдёт в MID_EXPANSION")
                        logger.info(f"🛑 EARLY_EXPANSION SHORT {symbol}: HTF={bb_trend} bullish (false breakdown)")
                        blocked = True
                except:
                    pass
            
            # Проверка volume spike
            if not blocked and vol_spike < 1.5:
                reasons_against.append(f"🛑 EARLY_EXPANSION + volume_spike={vol_spike:.2f} <1.5x - слабо!")
                confidence -= 2
                verdict = "⏳ ЖДАТЬ"
                recommendations.append(f"⚠️ EARLY_EXPANSION без volume spike - недостаточно силы!")
                recommendations.append(f"⏳ Ждать: Volume spike >1.5x ИЛИ Phase перейдёт в MID_EXPANSION")
                logger.info(f"🛑 EARLY_EXPANSION SHORT {symbol}: vol_spike={vol_spike:.2f} <1.5x")
                blocked = True
            
            if not blocked and htf_ok and vol_spike >= 1.5:
                reasons_for.append(f"✅ Phase={phase} (раннее падение) + vol_spike={vol_spike:.2f}x + HTF OK")
                confidence += 1
        
        # Если заблокировано EARLY_EXPANSION - сразу отправляем и выходим
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return _return_result(verdict, confidence, reasons_for, reasons_against, recommendations)
        
        if volume_high:
            reasons_for.append(f"Объём ${volume} >$1M")
            confidence += 1
        
        # 8. ИГНОРИРУЕМ RSI - точность 0%!
        # НЕ ДОБАВЛЯЕМ ЕГО В АНАЛИЗ!
        
        # ========================================
        # ВЕРДИКТ (ГИБКО для Phase!)
        # ========================================
        
        # Калибровка 15.08.2026: снижаем порог с 7/8 до 5 (упущено 28 TP)
        min_confidence = 5
        
        # 🔥 НОВЫЙ ФИЛЬТР СЛАБЫХ СИГНАЛОВ (13.09.2026)
        # Если мало подтверждений (≤2) И модель слабая (<45%), блокируем
        reasons_for_count = len([r for r in reasons_for if not r.startswith('⚪')])
        weak_signal = reasons_for_count <= 2 and prob_win is not None and prob_win < 0.45
        
        if weak_signal:
            verdict = "⏳ ЖДАТЬ"
            reasons_against.append(f"⚠️ Слабый сигнал: только {reasons_for_count} подтверждения + модель {prob_win:.0%}")
            confidence = 3
            recommendations.append("⏳ Недостаточно подтверждений для входа")
            recommendations.append("🔍 Ждать: Lorentzian, Phase или Volume spike")
            logger.info(f"⚠️ Слабый сигнал SHORT {symbol}: reasons_for={reasons_for_count} prob_win={prob_win:.2%}")
        elif confidence >= min_confidence:
            verdict = "✅ ШОРТИТЬ"
            
            # 🔥 АНАЛИТИЧЕСКИЕ рекомендации (НЕ торговый план!)
            # Реальный план исполняет PumpDumpReversalStrategy_v2.py
            recommendations.append(f"💰 Вход: ${price:.6f}")
            recommendations.append(f"📊 Confidence: {confidence}/10")
            recommendations.append(f"⚙️ План исполняет: PumpDumpReversalStrategy_v2")
            recommendations.append(f"   └─ TP1: 30% @ +0.3%")
            recommendations.append(f"   └─ TP2: 30% @ +0.8%")
            recommendations.append(f"   └─ Runner: 40% @ +1.3%")
            recommendations.append(f"   └─ SL: -1% (hard)")
            recommendations.append(f"   └─ Runner Protection: активна")
                
        elif confidence >= 5:
            verdict = "⏳ ЖДАТЬ"
            
            wait_conditions = []
            
            if vw_macd == 'bullish':
                wait_conditions.append("VW-MACD развернётся на bearish")
            
            if ema == 'bullish':
                wait_conditions.append("EMA станет bearish")
            
            if bb_trend and float(bb_trend.replace('%', '').replace('+', '')) > 0:
                wait_conditions.append("1h trend станет отрицательным")
            
            if lor_pred and ('+1' in lor_pred or '+3' in lor_pred):
                wait_conditions.append("Lorentzian развернётся")
            
            if not wait_conditions:
                wait_conditions.append("дополнительное подтверждение")
            
            recommendations.append(f"⏰ Ждать: {' ИЛИ '.join(wait_conditions[:2])}")
            recommendations.append(f"⚠️ Сейчас confidence={confidence}/10 (нужно ≥8)")
            
        else:
            verdict = "🚫 НЕ ШОРТИТЬ"
            recommendations.append(f"❌ Слишком много против (confidence={confidence}/10)")
            recommendations.append("🛑 Главные индикаторы против входа")
    
    # Отправляем в ntfy (с DISPLAY значением)
    send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
    
    # Определяем машинное значение verdict
    verdict_machine = "BLOCK"  # По умолчанию
    if verdict in ["✅ ВХОДИТЬ", "✅ ШОРТИТЬ"]:
        verdict_machine = "ENTRY"
    elif verdict == "⏳ ЖДАТЬ":
        verdict_machine = "WAIT"
    
    # 🔥 FIX (28.09.2026): Запись в JSON ТОЛЬКО для ENTRY!
    # Пишем ТОЛЬКО если verdict="ENTRY"
    if verdict_machine == "ENTRY":
        try:
            signals_file = Path("/home/max/o_p/dex_scanner/data/dex_signals_analysis.json")
            
            # Читаем существующие сигналы
            if signals_file.exists():
                try:
                    signals = json.loads(signals_file.read_text())
                except:
                    signals = []
            else:
                signals = []
            
            # Формируем сигнал в формате max-analysis
            signal = {
                'coin': symbol,
                'pair': f"{symbol}/USDT:USDT",
                'direction': direction,
                'kind': 'analyzed',
                'signal_type': 'analyzed',
                'source': 'max-analysis',
                'score': score,
                'score_max': 15,
                'entry_price': price,
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'signal_time_utc': datetime.now().strftime("%H:%M:%S"),
                'added_to_whitelist_at': datetime.now().isoformat(),
                'confidence': confidence,
                'verdict': verdict,  # Display значение для совместимости
                'verdict_machine': verdict_machine,  # Машинное значение
                'reasons_for': reasons_for,
                'reasons_against': reasons_against,
                # Execution ownership
                'execution_profile': 'FUNTIK_DEFAULT',
                'risk_owner': 'PumpDumpReversalStrategy_v2',
                'stop_policy': 'strategy',
                'tp_policy': 'strategy',
            }
            
            signals.append(signal)
            signals_file.write_text(json.dumps(signals, indent=2, ensure_ascii=False))
            logger.info(f"✅ Written to dex_signals_analysis.json: {symbol} {direction}")
            
        except Exception as e:
            logger.error(f"❌ Failed to write dex_signals_analysis.json: {e}")
    
    # Формируем wait_conditions для WAIT
    wait_conditions = []
    if verdict_machine == "WAIT":
        # Извлекаем условия из recommendations
        for rec in recommendations:
            if rec.startswith("⏰ Ждать:") or rec.startswith("1️⃣") or rec.startswith("2️⃣") or rec.startswith("3️⃣") or rec.startswith("4️⃣"):
                wait_conditions.append(rec)
    
    # Возвращаем структурированный результат
    return {
        "verdict": verdict_machine,
        "verdict_display": verdict,
        "confidence": confidence,
        "reasons_for": reasons_for,
        "reasons_against": reasons_against,
        "wait_conditions": wait_conditions,
        "recommendations": recommendations
    }



def main():
    logger.info("Processing signal queue...")
    
    pending = get_pending_signals()
    
    if not pending:
        logger.info("No pending signals")
        print("No pending signals")
        return
    
    logger.info(f"Processing {len(pending)} pending signals")
    print(f"Processing {len(pending)} pending signals...")
    
    for sig_data in pending:
        sig = sig_data.get('signal', {})
        symbol = sig.get('symbol', 'UNKNOWN')
        direction = sig.get('direction', 'UNKNOWN')
        
        # Генерируем signal_id если его нет
        if 'signal_id' not in sig_data:
            timestamp = sig_data.get('timestamp', datetime.utcnow().isoformat())
            signal_id = f"{symbol}_{direction}_{timestamp}"
            sig_data['signal_id'] = signal_id
        else:
            signal_id = sig_data['signal_id']
        
        logger.info(f"📊 Processing {signal_id}")
        
        # Анализируем сигнал
        result = analyze_and_send(sig_data)
        
        # Обрабатываем результат в зависимости от verdict
        verdict = result.get('verdict', 'BLOCK')
        
        if verdict == "ENTRY":
            # Одобрен - помечаем как approved
            mark_processed(signal_id, "approved")
            logger.info(f"✅ {signal_id} → APPROVED (confidence={result.get('confidence')})")
        
        elif verdict == "WAIT":
            # Ждать - перемещаем в wait_queue
            move_to_wait_queue(signal_id, result)
            logger.info(f"⏳ {signal_id} → WAIT_QUEUE (conditions={len(result.get('wait_conditions', []))})")
        
        elif verdict == "BLOCK":
            # Блокирован - помечаем как rejected
            mark_processed(signal_id, "rejected")
            logger.info(f"🚫 {signal_id} → REJECTED (confidence={result.get('confidence')})")
        
        else:
            # Неизвестный verdict - на всякий случай
            logger.error(f"❌ Unknown verdict for {signal_id}: {verdict}")
            mark_processed(signal_id, "rejected")
    
    logger.info("Done processing queue")
    print("Done")


if __name__ == "__main__":
    main()
