"""
Парсер scanner.log для извлечения ВСЕХ фич для lightgbm модели
"""
import re
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

SCANNER_LOG = Path("/home/max/o_p/dex_scanner/logs/scanner.log")


def extract_all_features(symbol: str, direction: str, score: int) -> dict:
    """
    Извлекает ВСЕ 36 фич из scanner.log для модели lightgbm_scorer
    
    Returns:
        dict с ключами соответствующими feature_names модели
    """
    try:
        if not SCANNER_LOG.exists():
            logger.warning(f"Scanner log not found: {SCANNER_LOG}")
            return {}
        
        # Читаем последние 1000 строк
        with SCANNER_LOG.open('r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()[-1000:]
        
        # Ищем SIGNAL_DECISION для symbol + direction
        signal_idx = None
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if f"SIGNAL_DECISION base={symbol}" in line and f"dir={direction}" in line:
                signal_idx = i
                break
        
        if signal_idx is None:
            logger.info(f"⚠️  No SIGNAL_DECISION found for {symbol} {direction}")
            return {}
        
        # Извлекаем индикаторный блок (20 строк после SIGNAL_DECISION)
        block_lines = lines[signal_idx:signal_idx + 20]
        text = '\n'.join(block_lines)
        
        # Парсим фичи
        features = {}
        
        # scanner_score - приходит как параметр
        features['scanner_score'] = float(score)
        
        # ATR (5m) -> volatility
        match = re.search(r'ATR: 5m ([\d.]+)%', text)
        if match:
            features['volatility'] = float(match.group(1))
        
        # Объём spike
        match = re.search(r'Объём: x([\d.]+)', text)
        if match:
            features['volume_spike'] = float(match.group(1))
        
        # Объём z-score -> volume_slope
        match = re.search(r'z=([-\d.]+)', text)
        if match:
            features['volume_slope'] = float(match.group(1))
        
        # RSI 5m
        match = re.search(r'RSI: 5m (\d+)', text)
        if match:
            features['rsi'] = float(match.group(1))
        
        # RSI 1h -> htf_trend_1h (нормализуем)
        match = re.search(r'RSI: 5m \d+ / 1h (\d+)', text)
        if match:
            features['htf_trend_1h'] = float(match.group(1)) / 100.0
        
        # Phase
        match = re.search(r'фаза (\w+)', text)
        if match:
            phase = match.group(1)
            features['market_phase'] = phase
        else:
            features['market_phase'] = None
        
        # ER (efficiency ratio 5m) -> trigger_score
        match = re.search(r'ER ([\d.]+)/', text)
        if match:
            features['trigger_score'] = float(match.group(1))
        
        # Тренд 1h MA8/18 -> regime_score
        match = re.search(r'Тренд 1h: MA8/18 ([+-]\d+)', text)
        if match:
            features['regime_score'] = float(match.group(1))
        
        # VWAP distance
        match = re.search(r'VWAP: ([-+]?[\d.]+)%', text)
        if match:
            features['distance_from_vwap'] = float(match.group(1))
        
        # MACD сила -> micro_score
        match = re.search(r'MACD: (\w+) \(сила ([\d.]+)ATR\)', text)
        if match:
            features['micro_score'] = float(match.group(2))
        
        # EMA-mom -> distance_from_ema20
        match = re.search(r'EMA-mom: ([+-]\d+) \(([\d.]+)%\)', text)
        if match:
            features['distance_from_ema20'] = float(match.group(2))
        
        # OBV -> absorption_score
        match = re.search(r'OBV ([+-]\d+)', text)
        if match:
            features['absorption_score'] = float(match.group(1))
        
        # OI % -> lor_strength (используем abs значение)
        match = re.search(r'OI: ([-+]?[\d.]+)%', text)
        if match:
            features['lor_strength'] = abs(float(match.group(1)))
        
        # цена/OI -> delta_slope
        match = re.search(r'цена/OI: ([+-]\d+)', text)
        if match:
            features['delta_slope'] = float(match.group(1))
        
        # структура -> lor_signal, lor_prediction
        match = re.search(r'структура ([+-]\d+)', text)
        if match:
            val = int(match.group(1))
            features['lor_signal'] = val
            features['lor_prediction'] = float(val)
        
        # confidence - задаём как score/15
        features['confidence'] = float(score) / 15.0
        
        logger.info(f"✅ Extracted {len(features)} raw features from scanner.log")
        return features
        
    except Exception as e:
        logger.error(f"Failed to extract features: {e}", exc_info=True)
        return {}


def prepare_model_features(raw_features: dict, direction: str) -> dict:
    """
    Преобразует сырые фичи в формат модели (с one-hot encoding и interaction features)
    
    Args:
        raw_features: dict с сырыми фичами из extract_all_features()
        direction: 'LONG' или 'SHORT'
    
    Returns:
        dict с 36 ключами соответствующими feature_names модели
    """
    features = {}
    
    # Базовые numeric фичи (заполняем дефолтами если отсутствуют)
    features['scanner_score'] = raw_features.get('scanner_score', 0.5)
    features['regime_score'] = raw_features.get('regime_score', 0.0)
    features['micro_score'] = raw_features.get('micro_score', 0.0)
    features['trigger_score'] = raw_features.get('trigger_score', 0.5)
    features['confidence'] = raw_features.get('confidence', 0.5)
    features['volatility'] = raw_features.get('volatility', 0.5)
    features['volume_spike'] = raw_features.get('volume_spike', 1.0)
    features['absorption_score'] = raw_features.get('absorption_score', 0.0)
    features['lor_signal'] = raw_features.get('lor_signal', 0.0)
    features['lor_strength'] = raw_features.get('lor_strength', 0.0)
    features['lor_prediction'] = raw_features.get('lor_prediction', 0.0)
    features['volume_slope'] = raw_features.get('volume_slope', 0.0)
    features['delta_slope'] = raw_features.get('delta_slope', 0.0)
    features['distance_from_ema20'] = raw_features.get('distance_from_ema20', 0.0)
    features['distance_from_vwap'] = raw_features.get('distance_from_vwap', 0.0)
    features['rsi'] = raw_features.get('rsi', 50.0)
    features['htf_trend_1h'] = raw_features.get('htf_trend_1h', 0.5)
    
    # Direction
    features['direction_long'] = 1.0 if direction == 'LONG' else 0.0
    
    # Phase one-hot encoding
    phase = raw_features.get('market_phase')
    phases = ['', 'EARLY_EXPANSION', 'EXHAUSTION', 'LATE_EXPANSION', 'MID_EXPANSION', None]
    
    for p in phases:
        phase_key = f'phase_{p if p is not None else "None"}'
        features[phase_key] = 1.0 if phase == p else 0.0
    
    # Interaction features: direction x phase
    is_long = 1.0 if direction == 'LONG' else 0.0
    is_short = 1.0 - is_long
    
    for p in phases:
        phase_key = f'phase_{p if p is not None else "None"}'
        phase_val = features[phase_key]
        
        long_key = f'long_x_{p if p is not None else "None"}'
        short_key = f'short_x_{p if p is not None else "None"}'
        
        features[long_key] = is_long * phase_val
        features[short_key] = is_short * phase_val
    
    return features
