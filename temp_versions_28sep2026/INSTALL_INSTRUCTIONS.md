# KIRO FIXES - 28 SEPTEMBER 2026

## 📦 Содержимое архива

```
kiro_fixes_28sep2026.zip
├── auto_process_queue.py              # Анализатор сигналов с умным fallback
├── wait_queue_monitor.py              # Монитор WAIT очереди
├── scanner.py                         # DEX Scanner с signal_id
├── PumpDumpReversalStrategy_v2.py     # Стратегия Фунтика с валидацией
└── wait-queue-monitor.service         # Systemd service для монитора
```

---

## 🔧 ЧТО ИСПРАВЛЕНО

### 1. **auto_process_queue.py**
- ✅ Машинные значения verdict: `ENTRY` / `WAIT` / `BLOCK`
- ✅ Правильный lifecycle сигнала:
  - `ENTRY` → `approved` (пишется в dex_signals_analysis.json)
  - `WAIT` → `waiting` (перемещается в wait_queue.json)
  - `BLOCK` → `rejected` (отклонён)
- ✅ `mark_processed(signal_id, status)` - по signal_id с fallback на symbol+timestamp
- ✅ `move_to_wait_queue()` - структурированные условия (JSON, не текст)
- ✅ Убраны Fibonacci планы TP/SL (стратегия сама управляет)
- ✅ Добавлены execution поля: `risk_owner`, `stop_policy`, `tp_policy`

### 2. **wait_queue_monitor.py** (НОВЫЙ)
- ✅ Мониторинг wait_queue каждые 15 секунд
- ✅ Проверка структурированных условий (RSI, свечи, индикаторы)
- ✅ Получение актуальных данных с Bybit API
- ✅ Создание confirmed сигналов с `parent_signal_id`
- ✅ Отправка в ntfy + запись в dex_signals_analysis.json
- ✅ Автоматический expire через 30 минут

### 3. **scanner.py**
- ✅ Добавлен `SIGNAL_ID` в лог: `SIGNAL_ID=HBAR_LONG_20260928123456`
- ✅ Баги `dec.long_weight` → `dec.long_w` исправлены
- ✅ Добавлен `mode` в лог

### 4. **PumpDumpReversalStrategy_v2.py**
- ✅ **confirm_trade_entry()** - полная валидация сигнала:
  - Проверка `verdict = ENTRY`
  - Проверка `status in {approved, confirmed}`
  - Проверка `direction` совпадает с side
  - Проверка отклонения цены < 0.5%
- ✅ **Runner Protection ИСПРАВЛЕН**:
  - Отслеживание пикового profit после TP1
  - Выход если откат ниже 50% от пика
  - Убрана неправильная логика с `min_acceptable`
- ✅ Реальные TP уровни стратегии в логах (30%@+0.3%, 30%@+0.8%, 40%runner)

### 5. **wait-queue-monitor.service**
- ✅ Systemd unit для автозапуска монитора
- ✅ Автоперезапуск при падении
- ✅ Логирование в `/home/max/freqtrade/logs/wait_monitor.log`

---

## 📥 УСТАНОВКА

### Шаг 1: Бэкап текущих файлов

```bash
cd /home/max/freqtrade
mkdir -p backups/2026-09-28_kiro_fixes
cp mcp_signal_analyzer/auto_process_queue.py backups/2026-09-28_kiro_fixes/
cp user_data/pump_dump_strategy/PumpDumpReversalStrategy_v2.py backups/2026-09-28_kiro_fixes/

cd /home/max/o_p/dex_scanner
cp scanner.py /home/max/freqtrade/backups/2026-09-28_kiro_fixes/
```

### Шаг 2: Распаковать архив

```bash
cd /home/max/freqtrade
unzip -o kiro_fixes_28sep2026.zip

# Переместить файлы на места
mv auto_process_queue.py mcp_signal_analyzer/
mv wait_queue_monitor.py mcp_signal_analyzer/
mv PumpDumpReversalStrategy_v2.py user_data/pump_dump_strategy/

cd /home/max/o_p/dex_scanner
mv /home/max/freqtrade/scanner.py .
```

### Шаг 3: Установить wait-queue-monitor service

```bash
cd /home/max/freqtrade
sudo cp wait-queue-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wait-queue-monitor.service
sudo systemctl start wait-queue-monitor.service
sudo systemctl status wait-queue-monitor.service
```

### Шаг 4: Проверить логи монитора

```bash
tail -f /home/max/freqtrade/logs/wait_monitor.log
```

### Шаг 5: Перезапустить Фунтика (если работает)

```bash
# Остановить
sudo systemctl stop freqtrade

# Проверить синтаксис стратегии
cd /home/max/freqtrade
source .venv/bin/activate
python3 -m py_compile user_data/pump_dump_strategy/PumpDumpReversalStrategy_v2.py

# Запустить
sudo systemctl start freqtrade
sudo systemctl status freqtrade
```

---

## 🔍 ПРОВЕРКА РАБОТЫ

### 1. Проверка анализатора

```bash
# Ручной запуск анализа pending сигналов
cd /home/max/freqtrade
source .venv/bin/activate
python3 mcp_signal_analyzer/auto_process_queue.py
```

**Ожидаемый результат:**
- Сигналы с verdict=ENTRY → пишутся в `dex_signals_analysis.json`
- Сигналы с verdict=WAIT → перемещаются в `wait_queue.json`
- Сигналы с verdict=BLOCK → помечаются rejected

### 2. Проверка wait_monitor

```bash
# Ручной запуск одной проверки
cd /home/max/freqtrade
source .venv/bin/activate
python3 mcp_signal_analyzer/wait_queue_monitor.py --once
```

**Ожидаемый результат:**
- Проверяет условия для сигналов в `wait_queue.json`
- Создаёт confirmed сигналы когда условия выполнены
- Отправляет в ntfy уведомление

### 3. Проверка файлов очередей

```bash
# Основная очередь
cat /home/max/freqtrade/.kiro/signal_queue.json | jq '.[] | {signal_id, status}'

# WAIT очередь
cat /home/max/freqtrade/.kiro/wait_queue.json | jq '.[] | {signal_id, status, symbol, conditions}'

# Одобренные сигналы
cat /home/max/o_p/dex_scanner/data/dex_signals_analysis.json | jq '.[] | {symbol, verdict_machine, status}'
```

---

## 📊 НОВЫЕ ПОЛЯ В СИГНАЛАХ

### В dex_signals_analysis.json:

```json
{
  "signal_id": "HBAR_LONG_20260928123456",
  "verdict": "✅ ВХОДИТЬ",
  "verdict_machine": "ENTRY",
  "status": "approved",
  "execution_profile": "FUNTIK_DEFAULT",
  "risk_owner": "PumpDumpReversalStrategy_v2",
  "stop_policy": "strategy",
  "tp_policy": "strategy"
}
```

### В wait_queue.json:

```json
{
  "signal_id": "HBAR_LONG_20260928123456",
  "status": "waiting",
  "symbol": "HBAR",
  "direction": "LONG",
  "conditions": [
    {"type": "rsi_below", "threshold": 35},
    {"type": "candle_bullish"},
    {"type": "indicator_match", "indicator": "vw_macd", "value": "bullish"}
  ],
  "created_at": "2026-09-28T10:30:00Z",
  "recheck_count": 5,
  "last_recheck": "2026-09-28T10:35:00Z"
}
```

---

## ⚠️ ВАЖНО

1. **Не смешивать планы**: Анализатор только решает ENTRY/WAIT/BLOCK, стратегия управляет TP/SL
2. **Runner Protection**: Теперь отслеживает пик и закрывает при откате >50%
3. **Signal validation**: Стратегия проверяет свежесть, направление и цену перед входом
4. **Wait queue**: Автоматически подтверждает сигналы когда условия выполнены

---

## 📝 СТРУКТУРА СИСТЕМЫ

```
┌─────────────────────────────────────┐
│ 1. DEX Scanner (scanner.py)        │
│    • Анализирует рынок              │
│    • Формирует signal_id            │
│    • Отправляет в ntfy "dex-signals"│
└──────────────┬──────────────────────┘
               ↓
┌─────────────────────────────────────┐
│ 2. Queue Monitor (listener)         │
│    • Слушает ntfy                   │
│    • Добавляет в signal_queue.json  │
│    • Запускает auto_process_queue   │
└──────────────┬──────────────────────┘
               ↓
┌─────────────────────────────────────┐
│ 3. Auto Analyzer                    │
│    (auto_process_queue.py)          │
│    • LightGBM + фильтры             │
│    • Решает: ENTRY / WAIT / BLOCK   │
│    ├─ ENTRY → dex_signals_analysis  │
│    ├─ WAIT → wait_queue.json        │
│    └─ BLOCK → rejected              │
└─────────────┬───────────────────────┘
              ↓
┌─────────────────────────────────────┐
│ 4. Wait Monitor                     │
│    (wait_queue_monitor.py)          │
│    • Проверяет условия каждые 15с   │
│    • Получает данные с Bybit        │
│    • Создаёт confirmed → analysis   │
└──────────────┬──────────────────────┘
               ↓
┌─────────────────────────────────────┐
│ 5. Freqtrade (Фунтик)              │
│    (PumpDumpReversalStrategy_v2)    │
│    • Читает dex_signals_analysis    │
│    • Валидирует сигнал              │
│    • Открывает позицию              │
│    • TP: 30%@+0.3%, 30%@+0.8%,     │
│      40%runner@+1.3%                │
│    • Runner Protection активна      │
└─────────────────────────────────────┘
```

---

## 🐛 TROUBLESHOOTING

### Wait monitor не запускается

```bash
sudo systemctl status wait-queue-monitor.service
sudo journalctl -u wait-queue-monitor.service -f
```

### Сигналы не попадают в dex_signals_analysis.json

1. Проверить verdict в логах: `grep "SIGNAL_ID" /home/max/freqtrade/logs/signal_analysis.log`
2. Проверить что verdict=ENTRY для сигналов
3. Проверить файл существует и доступен на запись

### Фунтик не входит по сигналам

1. Проверить логи стратегии: `grep "ENTRY BLOCK" /home/max/freqtrade/logs/freqtrade.log`
2. Проверить файл: `/home/max/o_p/dex_scanner/data/dex_signals_analysis.json`
3. Проверить поля: `verdict_machine`, `status`, `direction`

---

**Версия:** 28.09.2026  
**Автор:** Kiro AI Assistant
