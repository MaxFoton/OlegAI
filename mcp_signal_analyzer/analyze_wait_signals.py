#!/usr/bin/env python3
"""
Анализ сигналов с вердиктом "⏳ ЖДАТЬ"

Проверяет:
1. Какие условия были даны для входа
2. Выполнились ли эти условия в течение N часов
3. Что было бы если войти после выполнения
4. Входил ли Фунтик в эту же пару и каков результат
"""

import re
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import ccxt
import pandas as pd

LOG_FILE = Path("/home/max/freqtrade/logs/signal_analysis.log")
TRADES_DB = Path("/home/max/freqtrade/user_data/tradesv3_oleg_rl.sqlite")

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})


def parse_wait_signals(log_file: Path, hours_back: int = 24) -> list[dict]:
    """Парсит сигналы ЖДАТЬ из логов"""
    signals = []
    
    # Паттерн для сигналов ЖДАТЬ
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?ANALYZED: (\w+) (LONG|SHORT) \| ⏳ ЖДАТЬ"
    )
    
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            match = pattern.search(line)
            if match:
                ts_str, coin, direction = match.groups()
                signal_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                
                if signal_dt >= cutoff:
                    signals.append({
                        "timestamp": signal_dt,
                        "coin": coin,
                        "direction": direction,
                        "pair": f"{coin}/USDT:USDT",
                    })
    
    return signals


def get_conditions_for_signal(log_file: Path, coin: str, direction: str, timestamp: datetime) -> dict:
    """Извлекает условия входа из логов для конкретного сигнала"""
    conditions = {
        "phase_issue": None,
        "rsi_issue": None,
        "macd_issue": None,
        "weak_signal": False,
        "recommendations": []
    }
    
    # Ищем детали сигнала в логах (в пределах ±10 секунд)
    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    ts_str = timestamp.strftime("%Y-%m-%d %H:%M")
    relevant_lines = []
    capture = False
    
    for line in lines:
        if ts_str in line and f"{coin} {direction}" in line and "ЖДАТЬ" in line:
            capture = True
            relevant_lines = []
        
        if capture:
            relevant_lines.append(line)
            
            if "ANALYZED:" in line and coin not in line:
                break
    
    # Парсим условия из relevant_lines
    text = '\n'.join(relevant_lines)
    
    if "Phase=EXHAUSTION" in text and direction == "SHORT":
        conditions["phase_issue"] = "EXHAUSTION"
        conditions["recommendations"].append("RSI поднимется >50")
        conditions["recommendations"].append("Появится bearish свеча")
    
    if "Phase=LATE_EXPANSION" in text and direction == "LONG":
        conditions["phase_issue"] = "LATE_EXPANSION"
        conditions["recommendations"].append("RSI упадёт <50")
        conditions["recommendations"].append("Появится bullish свеча")
    
    if "RSI=" in text and "ПЕРЕПРОДАН" in text:
        conditions["rsi_issue"] = "oversold"
        conditions["recommendations"].append("RSI поднимется >50")
    
    if "RSI=" in text and "ПЕРЕКУПЛЕН" in text:
        conditions["rsi_issue"] = "overbought"
        conditions["recommendations"].append("RSI упадёт <50")
    
    if "MACD слабый" in text:
        conditions["macd_issue"] = "weak"
        conditions["recommendations"].append("MACD укрепится >0.2 ATR")
    
    if "Слабый сигнал" in text:
        conditions["weak_signal"] = True
        conditions["recommendations"].append("Дополнительные подтверждения")
    
    return conditions


def check_conditions_met(pair: str, signal_time: datetime, direction: str, conditions: dict, hours_ahead: int = 6) -> Optional[dict]:
    """
    Проверяет выполнились ли условия в течение hours_ahead часов
    Возвращает время и цену когда условия выполнились, или None
    """
    try:
        since_ms = int(signal_time.timestamp() * 1000)
        # Загружаем свечи на hours_ahead вперёд (5m свечи)
        limit = (hours_ahead * 60 // 5) + 10
        candles = exchange.fetch_ohlcv(pair, "5m", since=since_ms, limit=limit)
        
        if not candles or len(candles) < 2:
            return None
        
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        
        # Вычисляем RSI
        closes = df["close"].values
        if len(closes) < 15:
            return None
        
        delta = pd.Series(closes).diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        df["rsi"] = rsi
        
        # Проверяем условия на каждой свече после сигнала
        for idx in range(1, len(df)):
            row = df.iloc[idx]
            prev_row = df.iloc[idx - 1]
            
            conditions_met = True
            
            # Проверка RSI
            if conditions.get("rsi_issue") == "oversold":
                # Для SHORT: ждём RSI >50 после перепроданности
                if row["rsi"] < 50:
                    conditions_met = False
            
            if conditions.get("rsi_issue") == "overbought":
                # Для LONG: ждём RSI <50 после перекупленности
                if row["rsi"] > 50:
                    conditions_met = False
            
            # Проверка Phase (через RSI как прокси)
            if conditions.get("phase_issue") == "EXHAUSTION" and direction == "SHORT":
                # EXHAUSTION для SHORT: ждём RSI >50 (отскок завершён) + bearish свеча
                if row["rsi"] < 50:
                    conditions_met = False
                if row["close"] >= prev_row["close"]:  # не bearish
                    conditions_met = False
            
            if conditions.get("phase_issue") == "LATE_EXPANSION" and direction == "LONG":
                # LATE_EXPANSION для LONG: ждём RSI <50 (коррекция) + bullish свеча
                if row["rsi"] > 50:
                    conditions_met = False
                if row["close"] <= prev_row["close"]:  # не bullish
                    conditions_met = False
            
            if conditions_met:
                return {
                    "entry_time": row["dt"],
                    "entry_price": float(row["close"]),
                    "candles_waited": idx,
                    "minutes_waited": idx * 5,
                }
        
        return None  # Условия не выполнились за hours_ahead часов
        
    except Exception as e:
        print(f"  ! Error checking {pair}: {e}")
        return None


def simulate_trade(pair: str, entry_time: datetime, entry_price: float, direction: str, hours_ahead: int = 4) -> dict:
    """
    Симулирует сделку: что было бы если войти по entry_price
    Возвращает результат через hours_ahead часов
    """
    try:
        since_ms = int(entry_time.timestamp() * 1000)
        limit = (hours_ahead * 60 // 5) + 10
        candles = exchange.fetch_ohlcv(pair, "5m", since=since_ms, limit=limit)
        
        if not candles:
            return {"result": "NO_DATA", "pnl": 0, "exit_reason": "no_data"}
        
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
        
        # TP/SL из стратегии
        if direction == "LONG":
            tp1 = entry_price * 1.01  # +1%
            sl = entry_price * 0.99   # -1%
        else:  # SHORT
            tp1 = entry_price * 0.99  # -1%
            sl = entry_price * 1.01   # +1%
        
        # Проверяем каждую свечу
        for idx, row in df.iterrows():
            high, low = row["high"], row["low"]
            
            if direction == "LONG":
                if low <= sl:
                    pnl = (sl - entry_price) / entry_price * 100
                    return {"result": "LOSS", "pnl": pnl, "exit_reason": "stoploss", "candles": idx}
                if high >= tp1:
                    pnl = (tp1 - entry_price) / entry_price * 100
                    return {"result": "WIN", "pnl": pnl, "exit_reason": "tp1", "candles": idx}
            else:  # SHORT
                if high >= sl:
                    pnl = (entry_price - sl) / entry_price * 100
                    return {"result": "LOSS", "pnl": pnl, "exit_reason": "stoploss", "candles": idx}
                if low <= tp1:
                    pnl = (entry_price - tp1) / entry_price * 100
                    return {"result": "WIN", "pnl": pnl, "exit_reason": "tp1", "candles": idx}
        
        # Не закрыта за hours_ahead
        final_price = float(df.iloc[-1]["close"])
        if direction == "LONG":
            pnl = (final_price - entry_price) / entry_price * 100
        else:
            pnl = (entry_price - final_price) / entry_price * 100
        
        result = "WIN" if pnl > 0.5 else ("LOSS" if pnl < -0.5 else "BE")
        return {"result": result, "pnl": pnl, "exit_reason": "running", "candles": len(df)}
        
    except Exception as e:
        print(f"  ! Error simulating {pair}: {e}")
        return {"result": "ERROR", "pnl": 0, "exit_reason": "error"}


def check_funtik_trades(pair: str, signal_time: datetime, hours_window: int = 6) -> Optional[dict]:
    """Проверяет входил ли Фунтик в эту пару в пределах hours_window часов после сигнала"""
    conn = sqlite3.connect(TRADES_DB)
    
    start_time = signal_time.isoformat()
    end_time = (signal_time + timedelta(hours=hours_window)).isoformat()
    
    query = """
    SELECT id, pair, open_date, close_date, open_rate, close_rate, 
           close_profit, is_short, exit_reason
    FROM trades
    WHERE pair = ?
      AND open_date >= ?
      AND open_date <= ?
    ORDER BY open_date
    LIMIT 1
    """
    
    result = conn.execute(query, (pair, start_time, end_time)).fetchone()
    conn.close()
    
    if result:
        trade_id, pair, open_dt, close_dt, open_rate, close_rate, profit, is_short, exit_reason = result
        direction = "SHORT" if is_short else "LONG"
        
        return {
            "trade_id": trade_id,
            "direction": direction,
            "open_time": open_dt,
            "close_time": close_dt,
            "profit": float(profit) * 100 if profit else None,
            "exit_reason": exit_reason,
        }
    
    return None


def main():
    print("=" * 80)
    print("АНАЛИЗ СИГНАЛОВ 'ЖДАТЬ' ЗА ПОСЛЕДНИЕ 24 ЧАСА")
    print("=" * 80)
    
    # 1. Парсим сигналы ЖДАТЬ
    signals = parse_wait_signals(LOG_FILE, hours_back=24)
    print(f"\n📊 Найдено сигналов ЖДАТЬ: {len(signals)}")
    
    if not signals:
        print("❌ Нет сигналов ЖДАТЬ за последние 24 часа")
        return
    
    # Дедупликация
    seen = set()
    unique_signals = []
    for sig in signals:
        key = (sig["coin"], sig["direction"], sig["timestamp"].strftime("%Y-%m-%d %H"))
        if key not in seen:
            seen.add(key)
            unique_signals.append(sig)
    
    print(f"📊 Уникальных: {len(unique_signals)}")
    
    results = []
    
    for i, sig in enumerate(unique_signals, 1):
        coin = sig["coin"]
        direction = sig["direction"]
        pair = sig["pair"]
        signal_time = sig["timestamp"]
        
        print(f"\n[{i}/{len(unique_signals)}] {coin} {direction} @ {signal_time.strftime('%H:%M')}")
        
        # 2. Извлекаем условия
        conditions = get_conditions_for_signal(LOG_FILE, coin, direction, signal_time)
        print(f"  📋 Условия: phase={conditions.get('phase_issue')} rsi={conditions.get('rsi_issue')} macd={conditions.get('macd_issue')} weak={conditions.get('weak_signal')}")
        
        # 3. Проверяем выполнились ли условия
        conditions_met = check_conditions_met(pair, signal_time, direction, conditions, hours_ahead=6)
        
        if conditions_met:
            minutes = conditions_met["minutes_waited"]
            print(f"  ✅ Условия выполнились через {minutes} минут")
            
            # 4. Симулируем что было бы
            sim = simulate_trade(pair, conditions_met["entry_time"], conditions_met["entry_price"], direction, hours_ahead=4)
            print(f"  💰 Результат симуляции: {sim['result']} ({sim['pnl']:+.2f}%) reason={sim['exit_reason']}")
        else:
            print(f"  ❌ Условия НЕ выполнились за 6 часов")
            sim = None
        
        # 5. Проверяем что сделал Фунтик
        funtik_trade = check_funtik_trades(pair, signal_time, hours_window=6)
        
        if funtik_trade:
            print(f"  🤖 Фунтик ВОШЁЛ: {funtik_trade['direction']} profit={funtik_trade['profit']:+.2f}% exit={funtik_trade['exit_reason']}")
        else:
            print(f"  🤖 Фунтик НЕ входил")
        
        results.append({
            "coin": coin,
            "direction": direction,
            "signal_time": signal_time,
            "conditions": conditions,
            "conditions_met": conditions_met is not None,
            "simulation": sim,
            "funtik_trade": funtik_trade,
        })
    
    # Статистика
    print("\n" + "=" * 80)
    print("СТАТИСТИКА")
    print("=" * 80)
    
    total = len(results)
    conditions_met_count = sum(1 for r in results if r["conditions_met"])
    
    print(f"\n📊 Всего сигналов ЖДАТЬ: {total}")
    print(f"✅ Условия выполнились: {conditions_met_count} ({conditions_met_count/total*100:.1f}%)")
    print(f"❌ Условия НЕ выполнились: {total - conditions_met_count} ({(total-conditions_met_count)/total*100:.1f}%)")
    
    # Статистика симуляций
    sims = [r for r in results if r["simulation"]]
    if sims:
        wins = sum(1 for r in sims if r["simulation"]["result"] == "WIN")
        losses = sum(1 for r in sims if r["simulation"]["result"] == "LOSS")
        avg_pnl = sum(r["simulation"]["pnl"] for r in sims) / len(sims)
        
        print(f"\n💰 Если бы входили после выполнения условий:")
        print(f"  WIN: {wins} ({wins/len(sims)*100:.1f}%)")
        print(f"  LOSS: {losses} ({losses/len(sims)*100:.1f}%)")
        print(f"  Средний PnL: {avg_pnl:+.2f}%")
    
    # Статистика Фунтика
    funtik_trades = [r for r in results if r["funtik_trade"]]
    if funtik_trades:
        funtik_wins = sum(1 for r in funtik_trades if r["funtik_trade"]["profit"] and r["funtik_trade"]["profit"] > 0.5)
        funtik_avg = sum(r["funtik_trade"]["profit"] for r in funtik_trades if r["funtik_trade"]["profit"]) / len(funtik_trades)
        
        print(f"\n🤖 Фунтик вошёл в {len(funtik_trades)} из {total} сигналов ({len(funtik_trades)/total*100:.1f}%)")
        print(f"  WIN: {funtik_wins} ({funtik_wins/len(funtik_trades)*100:.1f}%)")
        print(f"  Средний PnL: {funtik_avg:+.2f}%")


if __name__ == "__main__":
    main()
