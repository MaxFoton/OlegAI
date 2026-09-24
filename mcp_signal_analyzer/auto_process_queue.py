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


def get_pending_signals():
    """Получить pending сигналы"""
    if not QUEUE_FILE.exists():
        return []
    
    with QUEUE_FILE.open('r') as f:
        queue = json.load(f)
    
    return [x for x in queue if x['status'] == 'pending']


def mark_processed(symbol: str):
    """Отметить сигнал как обработанный"""
    with QUEUE_FILE.open('r') as f:
        queue = json.load(f)
    
    for item in queue:
        if item['signal']['symbol'] == symbol and item['status'] == 'pending':
            item['status'] = 'processed'
            break
    
    with QUEUE_FILE.open('w') as f:
        json.dump(queue, f, indent=2)


def analyze_and_send(signal_data: dict):
    """Детальный анализ и отправка в ntfy"""
    sig = signal_data['signal']
    full_text = sig.get('full_text', '')
    
    symbol = sig['symbol']
    direction = sig['direction']
    score = sig['score']
    price = sig['price']
    
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
        
        # Отправляем в ntfy и выходим
        send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
        return
    
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
        
        # Если заблокировано - сразу отправляем и выходим
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return
        
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
        
        # Если заблокировано - сразу отправляем и выходим
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return
        
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
        
        # Если заблокировано - сразу отправляем и выходим
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return
        
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
            
            # ЗОЛОТОЕ СЕЧЕНИЕ для TP/SL (Fibonacci levels)
            # PHI = 1.618, PHI_INV = 0.618, 0.382
            # Идеальное R:R = 1.618
            entry_price = price
            stop_loss = price * 0.99382  # -0.618% (Fibonacci retracement)
            take_profit1 = price * 1.00618  # +0.618% (Fibonacci extension)
            take_profit2 = price * 1.01618  # +1.618% (Golden ratio)
            position_size = 1250
            risk_amount = position_size * 0.00618  # 0.618% риск (золотое сечение)
            
            # R:R = (TP - Entry) / (Entry - SL)
            rr_tp1 = (take_profit1 - entry_price) / (entry_price - stop_loss)  # ~1.0
            rr_tp2 = (take_profit2 - entry_price) / (entry_price - stop_loss)  # ~2.618
            
            recommendations.append(f"💰 Вход: ${entry_price:.6f} (немедленно)")
            recommendations.append(f"🛑 Стоп: ${stop_loss:.6f} (-0.618% Fibonacci)")
            recommendations.append(f"🎯 TP1: ${take_profit1:.6f} (+0.618% phi⁻¹ | R:R {rr_tp1:.2f})")
            recommendations.append(f"🎯 TP2: ${take_profit2:.6f} (+1.618% φ | R:R {rr_tp2:.2f})")
            recommendations.append(f"💵 Позиция: ${position_size} | Риск: ${risk_amount:.2f} (φ⁻¹%)")
            
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
        
        # Если заблокировано - сразу отправляем и выходим
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return
        
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
        
        # Если заблокировано - сразу отправляем и выходим
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return
        
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
        
        # Если заблокировано - сразу отправляем и выходим
        if blocked:
            send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
            return
        
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
            return
        
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
            
            # ЗОЛОТОЕ СЕЧЕНИЕ для TP/SL (Fibonacci levels)
            entry_price = price
            stop_loss = price * 1.00618  # +0.618% (Fibonacci retracement)
            take_profit1 = price * 0.99382  # -0.618% (Fibonacci extension)
            take_profit2 = price * 0.98382  # -1.618% (Golden ratio)
            position_size = 1250
            risk_amount = position_size * 0.00618  # 0.618% риск
            
            rr_tp1 = (entry_price - take_profit1) / (stop_loss - entry_price)
            rr_tp2 = (entry_price - take_profit2) / (stop_loss - entry_price)
            
            recommendations.append(f"💰 Вход: ${entry_price:.6f} (немедленно)")
            recommendations.append(f"🛑 Стоп: ${stop_loss:.6f} (+0.618% Fibonacci)")
            recommendations.append(f"🎯 TP1: ${take_profit1:.6f} (-0.618% phi⁻¹ | R:R {rr_tp1:.2f})")
            recommendations.append(f"🎯 TP2: ${take_profit2:.6f} (-1.618% φ | R:R {rr_tp2:.2f})")
            recommendations.append(f"💵 Позиция: ${position_size} | Риск: ${risk_amount:.2f} (φ⁻¹%)")
                
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
    
    # Отправляем в ntfy
    send_to_ntfy(symbol, direction, score, price, verdict, reasons_for, reasons_against, recommendations, confidence)
    
    # 🔥 FIX (22.09.2026): Запись в JSON ПОСЛЕ всех фильтров!
    # Пишем ТОЛЬКО если verdict="ВХОДИТЬ" или "ШОРТИТЬ"
    if verdict in ["✅ ВХОДИТЬ", "✅ ШОРТИТЬ"]:
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
                'verdict': verdict,
                'reasons_for': reasons_for,
                'reasons_against': reasons_against,
            }
            
            signals.append(signal)
            signals_file.write_text(json.dumps(signals, indent=2, ensure_ascii=False))
            logger.info(f"✅ Written to dex_signals_analysis.json: {symbol} {direction}")
            
        except Exception as e:
            logger.error(f"❌ Failed to write dex_signals_analysis.json: {e}")



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
        symbol = sig_data['signal']['symbol']
        
        analyze_and_send(sig_data)
        mark_processed(symbol)
    
    logger.info("Done processing queue")
    print("Done")


if __name__ == "__main__":
    main()
