#!/usr/bin/env python3
"""
Backtest слабых сигналов Фунтика (score 5-6) из max-alert
Проверяем: может зря отклоняем эти сигналы?
"""

import json
import re
import requests
from datetime import datetime, timedelta
from pathlib import Path
from pybit.unified_trading import HTTP

# Конфигурация
NTFY_URL = "http://87.121.218.4:8080/max-alerts/json?poll=1&since=48h"
BYBIT_API_KEY = "oFBoV21LzkFzm9Y61g"
BYBIT_API_SECRET = "DJLszHwPfTdAp4k8L5wZGKqwBRo18u4GNCJq"

# Bybit client
session = HTTP(
    testnet=False,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET
)


def fetch_ntfy_messages():
    """Получает сообщения из ntfy max-alerts"""
    try:
        response = requests.get(NTFY_URL, timeout=10)
        lines = response.text.strip().split('\n')
        
        messages = []
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                messages.append(msg)
            except:
                continue
        
        return messages
    except Exception as e:
        print(f"❌ Ошибка получения ntfy: {e}")
        return []


def parse_funtik_alert(msg: dict) -> dict:
    """Парсит сигнал от сканера из max-alerts"""
    title = msg.get('title', '')
    message_text = msg.get('message', '')
    timestamp = datetime.fromtimestamp(msg.get('time', 0))
    
    # Парсим разные форматы title:
    # "TRUST vol×18.3 z=5.9"
    # "BOME 1h +3.83% LONG"
    # "COOKIE vol×2.5 z=3.5"
    
    # Извлекаем symbol из title
    symbol_match = re.match(r'^(\w+)', title)
    if not symbol_match:
        return None
    
    symbol = symbol_match.group(1)
    
    # Парсим score из message
    score_match = re.search(r'score=(\d+)/15', message_text)
    if not score_match:
        return None
    
    score = int(score_match.group(1))
    
    # Фильтр: только слабые сигналы (5-6)
    if score < 5 or score > 6:
        return None
    
    # Парсим цену
    price_match = re.search(r'price=\$([0-9.]+)', message_text)
    if not price_match:
        return None
    
    entry_price = float(price_match.group(1))
    
    # Парсим приоритет из message
    priority_match = re.search(r'(Слабый|Умеренный|Сильный)', message_text)
    priority = priority_match.group(1) if priority_match else 'unknown'
    
    # Парсим направление из message - ищем "LONG" или "SHORT"
    direction = None
    if 'сигнал LONG' in message_text or '| LONG |' in message_text or title.endswith('LONG'):
        direction = 'LONG'
    elif 'сигнал SHORT' in message_text or '| SHORT |' in message_text or title.endswith('SHORT'):
        direction = 'SHORT'
    
    if not direction:
        return None
    
    # Парсим тип сигнала из message
    signal_type = 'unknown'
    if 'vol×' in title:
        signal_type = 'volume_anomaly'
    elif '1h' in title and '%' in title:
        signal_type = 'trend'
    
    return {
        'timestamp': timestamp,
        'symbol': symbol,
        'direction': direction,
        'signal_type': signal_type,
        'score': score,
        'entry_price': entry_price,
        'priority': priority,
        'source': 'dex_scanner'
    }


def get_historical_klines(symbol: str, start_time: datetime, interval: str = '1') -> list:
    """Получает исторические свечи с Bybit"""
    try:
        # Конвертируем в timestamp (миллисекунды)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int((start_time + timedelta(hours=2)).timestamp() * 1000)
        
        response = session.get_kline(
            category="linear",
            symbol=f"{symbol}USDT",
            interval=interval,
            start=start_ms,
            end=end_ms,
            limit=200
        )
        
        if response['retCode'] != 0:
            return []
        
        klines = response['result']['list']
        return klines
    
    except Exception as e:
        print(f"  ⚠️ Ошибка получения klines для {symbol}: {e}")
        return []


def check_tp_sl(entry_price: float, direction: str, klines: list, entry_time: datetime) -> tuple:
    """
    Проверяет достигнут ли TP или SL за 1 час после входа
    Возвращает: (result, actual_price, time_to_result, max_profit, max_drawdown)
    """
    if not klines:
        return None, None, None, None, None
    
    # SL/TP: ±1%
    if direction == "LONG":
        stop_loss = entry_price * 0.99
        take_profit = entry_price * 1.01
    else:  # SHORT
        stop_loss = entry_price * 1.01
        take_profit = entry_price * 0.99
    
    # Время входа + 1 час
    exit_time = entry_time + timedelta(hours=1)
    
    max_profit = 0.0
    max_drawdown = 0.0
    
    for kline in reversed(klines):  # Bybit возвращает в обратном порядке
        # kline: [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
        candle_time = datetime.fromtimestamp(int(kline[0]) / 1000)
        
        # Пропускаем свечи до входа
        if candle_time < entry_time:
            continue
        
        # Если прошёл 1 час - выходим
        if candle_time > exit_time:
            break
        
        high = float(kline[2])
        low = float(kline[3])
        close = float(kline[4])
        
        # Проверяем TP/SL
        if direction == "LONG":
            # TP проверка
            if high >= take_profit:
                profit_pct = ((take_profit - entry_price) / entry_price) * 100
                time_minutes = (candle_time - entry_time).total_seconds() / 60
                return "TP", take_profit, time_minutes, profit_pct, max_drawdown
            
            # SL проверка
            if low <= stop_loss:
                loss_pct = ((stop_loss - entry_price) / entry_price) * 100
                time_minutes = (candle_time - entry_time).total_seconds() / 60
                return "SL", stop_loss, time_minutes, max_profit, loss_pct
            
            # MFE/MAE
            profit = ((high - entry_price) / entry_price) * 100
            drawdown = ((low - entry_price) / entry_price) * 100
            max_profit = max(max_profit, profit)
            max_drawdown = min(max_drawdown, drawdown)
        
        else:  # SHORT
            # TP проверка
            if low <= take_profit:
                profit_pct = ((entry_price - take_profit) / entry_price) * 100
                time_minutes = (candle_time - entry_time).total_seconds() / 60
                return "TP", take_profit, time_minutes, profit_pct, max_drawdown
            
            # SL проверка
            if high >= stop_loss:
                loss_pct = ((entry_price - stop_loss) / entry_price) * 100
                time_minutes = (candle_time - entry_time).total_seconds() / 60
                return "SL", stop_loss, time_minutes, max_profit, loss_pct
            
            # MFE/MAE
            profit = ((entry_price - low) / entry_price) * 100
            drawdown = ((entry_price - high) / entry_price) * 100
            max_profit = max(max_profit, profit)
            max_drawdown = min(max_drawdown, drawdown)
    
    # Если за 1 час ничего не случилось - закрываем по последней цене
    if klines:
        last_close = float(klines[0][4])  # Последняя свеча
        if direction == "LONG":
            profit_pct = ((last_close - entry_price) / entry_price) * 100
        else:
            profit_pct = ((entry_price - last_close) / entry_price) * 100
        
        return "TIMEOUT", last_close, 60.0, max_profit, max_drawdown
    
    return None, None, None, None, None


def main():
    print("=" * 80)
    print("BACKTEST СЛАБЫХ СИГНАЛОВ ФУНТИКА (SCORE 5-6)")
    print("Источник: max-alert за 48 часов")
    print("=" * 80)
    print()
    
    # Получаем сообщения
    messages = fetch_ntfy_messages()
    
    if not messages:
        print("❌ Не удалось получить сообщения")
        return
    
    print(f"Получено {len(messages)} сообщений из ntfy")
    print()
    
    # Парсим сигналы
    signals = []
    for msg in messages:
        parsed = parse_funtik_alert(msg)
        if parsed:
            signals.append(parsed)
    
    print(f"Найдено {len(signals)} слабых сигналов (score 5-6)")
    print()
    
    if not signals:
        print("❌ Нет слабых сигналов для анализа")
        return
    
    # Группируем по приоритету
    priority_counts = {}
    for sig in signals:
        priority_counts[sig['priority']] = priority_counts.get(sig['priority'], 0) + 1
    
    print("Распределение по приоритетам:")
    for priority, count in priority_counts.items():
        print(f"  {priority}: {count}")
    print()
    
    # Backtest
    print("=" * 80)
    print("BACKTEST РЕЗУЛЬТАТЫ")
    print("=" * 80)
    print()
    
    results = []
    
    for i, sig in enumerate(signals, 1):
        print(f"{i}/{len(signals)} {sig['symbol']} {sig['direction']} @ {sig['timestamp'].strftime('%d.%m %H:%M')} | score={sig['score']} | priority={sig['priority']}")
        
        # Получаем исторические данные
        klines = get_historical_klines(sig['symbol'], sig['timestamp'])
        
        if not klines:
            print(f"  ❌ Нет данных")
            print()
            continue
        
        # Проверяем TP/SL
        result, exit_price, time_minutes, max_profit, max_drawdown = check_tp_sl(
            sig['entry_price'],
            sig['direction'],
            klines,
            sig['timestamp']
        )
        
        if result:
            results.append({
                'signal': sig,
                'result': result,
                'exit_price': exit_price,
                'time_minutes': time_minutes,
                'max_profit': max_profit,
                'max_drawdown': max_drawdown
            })
            
            if result == "TP":
                pnl = 3.50  # +1%
                print(f"  ✅ TP за {time_minutes:.1f} мин | MFE: {max_profit:.2f}% | MAE: {max_drawdown:.2f}% | PnL: +${pnl:.2f}")
            elif result == "SL":
                pnl = -3.50  # -1%
                print(f"  ❌ SL за {time_minutes:.1f} мин | MFE: {max_profit:.2f}% | MAE: {max_drawdown:.2f}% | PnL: -${pnl:.2f}")
            else:
                if sig['direction'] == 'LONG':
                    profit_pct = ((exit_price - sig['entry_price']) / sig['entry_price']) * 100
                else:
                    profit_pct = ((sig['entry_price'] - exit_price) / sig['entry_price']) * 100
                pnl = 350 * (profit_pct / 100)
                print(f"  ⏱️ TIMEOUT (1h) | MFE: {max_profit:.2f}% | MAE: {max_drawdown:.2f}% | PnL: ${pnl:+.2f}")
        else:
            print(f"  ❌ Ошибка проверки")
        
        print()
    
    # Статистика
    print("=" * 80)
    print("СТАТИСТИКА")
    print("=" * 80)
    print()
    
    if not results:
        print("❌ Нет результатов для анализа")
        return
    
    tp_count = sum(1 for r in results if r['result'] == 'TP')
    sl_count = sum(1 for r in results if r['result'] == 'SL')
    timeout_count = sum(1 for r in results if r['result'] == 'TIMEOUT')
    
    total_pnl = 0.0
    for r in results:
        if r['result'] == 'TP':
            total_pnl += 3.50
        elif r['result'] == 'SL':
            total_pnl -= 3.50
        else:  # TIMEOUT
            sig = r['signal']
            if sig['direction'] == 'LONG':
                profit_pct = ((r['exit_price'] - sig['entry_price']) / sig['entry_price']) * 100
            else:
                profit_pct = ((sig['entry_price'] - r['exit_price']) / sig['entry_price']) * 100
            total_pnl += 350 * (profit_pct / 100)
    
    winrate = (tp_count / len(results)) * 100 if results else 0
    
    print(f"Всего сигналов: {len(results)}")
    print(f"✅ TP: {tp_count} ({tp_count/len(results)*100:.1f}%)")
    print(f"❌ SL: {sl_count} ({sl_count/len(results)*100:.1f}%)")
    print(f"⏱️ TIMEOUT: {timeout_count} ({timeout_count/len(results)*100:.1f}%)")
    print()
    print(f"💰 ВИНРЕЙТ: {winrate:.1f}%")
    print(f"💵 TOTAL PnL: ${total_pnl:+.2f}")
    print()
    
    # Статистика по приоритетам
    print("=" * 80)
    print("СТАТИСТИКА ПО ПРИОРИТЕТАМ")
    print("=" * 80)
    print()
    
    for priority in set(sig['priority'] for sig in signals):
        priority_results = [r for r in results if r['signal']['priority'] == priority]
        
        if not priority_results:
            continue
        
        tp = sum(1 for r in priority_results if r['result'] == 'TP')
        sl = sum(1 for r in priority_results if r['result'] == 'SL')
        
        pnl = 0.0
        for r in priority_results:
            if r['result'] == 'TP':
                pnl += 3.50
            elif r['result'] == 'SL':
                pnl -= 3.50
            else:
                sig = r['signal']
                if sig['direction'] == 'LONG':
                    profit_pct = ((r['exit_price'] - sig['entry_price']) / sig['entry_price']) * 100
                else:
                    profit_pct = ((sig['entry_price'] - r['exit_price']) / sig['entry_price']) * 100
                pnl += 350 * (profit_pct / 100)
        
        wr = (tp / len(priority_results)) * 100
        
        print(f"Приоритет: {priority}")
        print(f"  Сигналов: {len(priority_results)}")
        print(f"  TP: {tp} | SL: {sl}")
        print(f"  Винрейт: {wr:.1f}%")
        print(f"  PnL: ${pnl:+.2f}")
        print()
    
    # Статистика по score
    print("=" * 80)
    print("СТАТИСТИКА ПО SCORE")
    print("=" * 80)
    print()
    
    for score in [5, 6]:
        score_results = [r for r in results if r['signal']['score'] == score]
        
        if not score_results:
            continue
        
        tp = sum(1 for r in score_results if r['result'] == 'TP')
        sl = sum(1 for r in score_results if r['result'] == 'SL')
        
        pnl = 0.0
        for r in score_results:
            if r['result'] == 'TP':
                pnl += 3.50
            elif r['result'] == 'SL':
                pnl -= 3.50
            else:
                sig = r['signal']
                if sig['direction'] == 'LONG':
                    profit_pct = ((r['exit_price'] - sig['entry_price']) / sig['entry_price']) * 100
                else:
                    profit_pct = ((sig['entry_price'] - r['exit_price']) / sig['entry_price']) * 100
                pnl += 350 * (profit_pct / 100)
        
        wr = (tp / len(score_results)) * 100
        
        print(f"Score {score}/15:")
        print(f"  Сигналов: {len(score_results)}")
        print(f"  TP: {tp} | SL: {sl}")
        print(f"  Винрейт: {wr:.1f}%")
        print(f"  PnL: ${pnl:+.2f}")
        print()
    
    # Детальный анализ SL сигналов
    print("=" * 80)
    print("ДЕТАЛЬНЫЙ АНАЛИЗ SL СИГНАЛОВ")
    print("=" * 80)
    print()
    
    sl_results = [r for r in results if r['result'] == 'SL']
    
    if sl_results:
        print(f"Найдено {len(sl_results)} SL сигналов:")
        print()
        
        for r in sl_results:
            sig = r['signal']
            print(f"❌ {sig['symbol']} {sig['direction']} @ {sig['timestamp'].strftime('%d.%m %H:%M')}")
            print(f"   Score: {sig['score']}/15 | Priority: {sig['priority']} | Source: {sig['source']}")
            print(f"   Entry: ${sig['entry_price']:.6f} | Exit: ${r['exit_price']:.6f}")
            print(f"   Time to SL: {r['time_minutes']:.1f} min")
            print(f"   MFE: {r['max_profit']:.2f}% | MAE: {r['max_drawdown']:.2f}%")
            print()
    else:
        print("✅ Нет SL сигналов!")
        print()
    
    print("=" * 80)


if __name__ == "__main__":
    main()
