# ТЕКУЩАЯ ТОРГОВАЯ ЛОГИКА - 06.10.2026

## ОБЩАЯ АРХИТЕКТУРА

```
Scanner.log → scanner_log_monitor.py → preload_candles → signal_queue.json
                                                              ↓
                                                         Kiro Analysis
                                                              ↓
                                                    dex_signals_analysis.json
                                                              ↓
                                                      Фунтик (Freqtrade)
                                                              ↓
                                                      PumpDumpReversalStrategy_v2
```

## ИСТОЧНИКИ СИГНАЛОВ

### 1. Scanner.log (основной источник)
- **Где**: `/home/max/o_p/dex_scanner/logs/scanner.log`
- **Мониторинг**: `scanner_log_monitor.py` (каждые 5 сек)
- **Минимальный score**: 5/15
- **Формат**: `SIGNAL_ID=SYMBOL_DIR_TIMESTAMP SIGNAL_DECISION base=X dir=LONG/SHORT kind=vol_anomaly score=9`

### 2. Предзагрузка свечей (NEW 05.10.2026)
**ДО отправки сигнала в Kiro:**
```python
await preload_candles_for_funtik(symbol, direction)
```
- Загружает: 100x5m, 50x15m, 50x1h
- Сохраняет в: `/home/max/freqtrade/user_data/data/bybit/{SYMBOL}_USDT_USDT-{tf}.json`
- Формат: Freqtrade JSON `[[timestamp, open, high, low, close, volume], ...]`

### 3. Wait Queue Monitor
**При подтверждении Wait сигнала:**
```python
asyncio.run(preload_candles_for_wait_confirmed(symbol, direction))
send_confirmation_to_ntfy(symbol, direction, price, signal)
```
- Обновляет свечи ПЕРЕД отправкой ntfy
- Гарантирует свежие данные после ожидания 15+ минут

### 4. Kiro Analysis
- Анализирует через LightGBM + директоры
- Решение: ВХОДИТЬ / ЖДАТЬ / НЕ ВХОДИТЬ
- Confidence: 0-10
- Минимум для входа: confidence >= 6

## ENTRY LOGIC (PumpDumpReversalStrategy_v2)

### Режимы работы
1. **REVERSAL** - mean reversion, tight stops
2. **CONTINUATION** - momentum, wider stops

### Определение режима
```python
setup_mode = "REVERSAL" if "reversal" in enter_tag.lower() else "CONTINUATION"
```

### Фильтры HTF (1h таймфрейм)
**ОТКЛЮЧЕНЫ 05.10.2026** для max-analysis сигналов:
```python
# Все 4 блока закомментированы:
# if is_long and htf_change < -0.04: return False  # LONG + HTF падение >4%
# if is_long and htf_change > 0.05: return False   # LONG + HTF рост >5%
# if is_short and htf_change > 0.04: return False  # SHORT + HTF рост >4%
# if is_short and htf_change < -0.06: return False # SHORT + HTF падение >6%
```

## EXIT LOGIC

### Настройки TP/SL (Config)
```python
# HARD STOPLOSS
HARD_STOPLOSS = -0.010  # -1.0%

# REVERSAL
REVERSAL_TP1 = 0.003     # +0.3% close 30%
REVERSAL_TP1_RATIO = 0.30
REVERSAL_TP2 = 0.008     # +0.8% close 30%
REVERSAL_TP2_RATIO = 0.30
REVERSAL_BE = 0.002      # breakeven at +0.2%

# CONTINUATION
CONTINUATION_TP1 = 0.003  # +0.3% close 30%
CONTINUATION_TP1_RATIO = 0.30
CONTINUATION_TP2 = 0.008  # +0.8% close 30%
CONTINUATION_TP2_RATIO = 0.30
CONTINUATION_BE = 0.002   # breakeven at +0.2%

# TRAILING (после TP)
TRAILING_AFTER_PARTIAL_OFFSET = 0.012  # 1.2% after 2x partial exits
```

### custom_stoploss() - Динамический стоп

**ЛОГИКА:**
1. **profit < 0** → HARD_STOPLOSS (-1%)
2. **profit < TP1 (0.3%)** → НЕТ TRAILING (return None)
   - Защита от преждевременного закрытия
   - Ждём достижения TP1
3. **profit >= TP1 но exits=0** → ЖДЁМ adjust_trade_position
4. **exits >= 1** → ВКЛЮЧАЕМ TRAILING:
   - profit >= 1.68%: trail 0.8%
   - profit >= 1.12%: trail 1.5%
   - profit >= TP1: trail 2.5%
5. **exits >= 2** → RUNNER TRAIL 1.2%

**FIX 26.09.2026:** Отключён aggressive trailing до TP1
- Проблема: trailing_stop_loss срабатывал на +0.5-1.0% ДО TP1
- Решение: НЕТ TRAILING до первого partial exit

### adjust_trade_position() - Частичные продажи

**TP1: 30% @ +0.3%**
```python
if exits_done == 0 and current_profit >= tp1_target:
    stake_to_close = trade.stake_amount * 0.30
    return -stake_to_close
```

**TP2: 30% @ +0.8%** (от начальной = 42.86% от оставшейся)
```python
if exits_done == 1 and current_profit >= tp2_target:
    remaining_after_tp1 = 0.70
    tp2_from_remaining = 0.30 / 0.70  # = 0.4286
    stake_to_close = trade.stake_amount * tp2_from_remaining
    return -stake_to_close
```

**Runner: 40%** продолжает с tight trailing

### order_filled() - Подтверждение TP

**🔥 FIX 28.09.2026:**
- TP state устанавливается ТОЛЬКО после фактического исполнения ордера
- НЕ в adjust_trade_position (там только запрос на выход)

## ПРОБЛЕМА (06.10.2026)

### Симптомы
- Сделки закрываются с мизерным профитом: 0.09%, 0.23%, 0.80%
- Причина выхода: `trailing_stop_loss` или `roi`
- TP1/TP2 **НЕ срабатывают** (нет частичных выходов)

### Примеры
```
#13058 AKE SHORT:  0.23% через trailing_stop_loss
#13057 PHA SHORT:  0.09% через trailing_stop_loss  
#13056 B3 SHORT:   0.80% через roi
```

### Гипотезы
1. **TP слишком далеко** (0.3% / 0.8% не достигаются)
2. **Trailing включается раньше TP1** (баг в логике?)
3. **roi перехватывает** до достижения TP1
4. **adjust_trade_position не вызывается** (почему?)

### Нужно проверить
- [ ] Логи custom_stoploss для этих сделок
- [ ] Логи adjust_trade_position 
- [ ] Значение `minimal_roi` в стратегии
- [ ] Порядок вызова функций Freqtrade
- [ ] MFE/MAE tracking показывает реальный максимальный профит

## ДАННЫЕ ДЛЯ АНАЛИЗА

### Базы данных
- `/home/max/freqtrade/tradesv3.sqlite` - история сделок
- `/home/max/freqtrade/trade_snapshots.db` - фичи + результаты

### Логи
- `/home/max/freqtrade/logs/funtik.log` - основной лог стратегии
- MFE/MAE логи каждые 2 минуты

### JSON очереди
- `/home/max/freqtrade/.kiro/signal_queue.json`
- `/home/max/freqtrade/.kiro/wait_queue.json`
- `/home/max/o_p/dex_scanner/data/dex_signals_analysis.json`

## НАСТРОЙКИ FREQTRADE

```python
# Стратегия
startup_candle_count = 10  # минимум (свечи предзагружены)
position_adjustment_enable = True  # для partial exits
use_exit_signal = True
use_custom_stoploss = True

# informative_pairs восстановлена (05.10.2026)
# Freqtrade читает предзагруженные 1h файлы
def informative_pairs(self):
    pairs = self.dp.current_whitelist()
    return [(p, "1h") for p in pairs]

# minimal_roi
minimal_roi = {"0": 100}  # практически отключен
```

## ИСТОРИЯ ИЗМЕНЕНИЙ

### 05.10.2026
- ✅ Добавлена предзагрузка свечей в scanner_log_monitor.py
- ✅ Добавлена предзагрузка свечей в wait_queue_monitor.py
- ✅ Восстановлена informative_pairs для чтения 1h
- ✅ Отключены HTF блоки для max-analysis
- ✅ startup_candle_count: 50 → 10

### 25.09.2026
- TP снижены: 0.5%/1.0% → 0.3%/0.8%
- Причина: старые уровни слишком далеко

### 26.09.2026
- Отключён aggressive trailing до TP1
- Причина: trailing_stop_loss срабатывал преждевременно

### 09.09.2026
- HARD_STOPLOSS: -1.8% → -1.0%
- Причина: R/R баланс

### 28.09.2026
- TP state теперь устанавливается в order_filled()
- Не в adjust_trade_position

