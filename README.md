# FUNTIK - Система автоматической торговли криптовалютами

**Версия:** 2.0  
**Статус:** Production (Dry Run)  
**Дата:** Сентябрь 2026

---

## 📋 Описание

Funtik — полностью автоматизированная торговая система для торговли фьючерсами на Bybit. Система использует машинное обучение (LightGBM), техническийанализ и анализ сигналов DEX для принятия торговых решений.

### Ключевые особенности:

- **Машинное обучение**: LightGBM модель с 54.5% точностью (7485 samples)
- **Мультисигнальная система**: Анализ 36+ индикаторов + DEX сигналы
- **Частичные выходы (TP)**: 30% @ +1.5%, 30% @ +2.5%, 40% runner
- **Трейлинг стоп**: Динамический стоп-лосс с trailing
- **HTF фильтры**: Блокировка входов против тренда (±2.3%)
- **Volume spike фильтры**: Защита от ложных сигналов
- **Wait queue**: Система подтверждения слабых сигналов
- **Киро анализатор**: Автоматический анализ сигналов через MCP

---

## 📁 Структура проекта

```
funtik_git_repo/
├── mcp_signal_analyzer/     # Киро анализатор и парсеры
│   ├── auto_process_queue.py       # Главный анализатор
│   ├── scanner_parser.py           # Парсер фич
│   ├── scanner_log_monitor.py      # Мониторинг scanner.log
│   ├── wait_queue_monitor.py       # Мониторинг wait_queue
│   ├── ntfy_to_funtik.py           # Мост ntfy → Funtik
│   └── run_monitor.sh              # Скрипт запуска мониторов
│
├── strategy/                 # Торговая стратегия
│   ├── PumpDumpReversalStrategy_v2.py  # Главная стратегия (164KB)
│   ├── directional_lgbm.py             # LightGBM scorer
│   ├── lorentzian_classifier.py        # Lorentzian classifier
│   ├── analyze_v2_entries_v2.py        # Анализ сделок
│   └── engines/                        # Модули (data, feature, regime, signal, risk, ai_edge)
│
├── configs/                  # Конфигурация
│   └── config_pumpdump_reversal.json   # Главный конфиг Фунтика
│
├── models/                   # ML модели
│   ├── lightgbm_scorer.pkl             # Обученная модель
│   └── lightgbm_meta.json              # Метаданные модели
│
├── databases/                # Базы данных (не в git)
│   ├── tradesv3_oleg_rl.sqlite         # История сделок (12577+)
│   └── trade_snapshots.db              # Фичи для ML
│
└── scripts/                  # Скрипты управления
    ├── START_FUNTIK.sh                 # Запуск Фунтика
    ├── RESTART_FUNTIK.sh               # Перезапуск
    └── retrain_lightgbm_scorer.py      # Переобучение модели
```

---

## 🚀 Быстрый старт

### 1. Установка зависимостей

```bash
# Python 3.10+
pip install freqtrade
pip install lightgbm scikit-learn pandas numpy
```

### 2. Настройка конфигурации

Отредактируйте `configs/config_pumpdump_reversal.json`:

```json
{
  "exchange": {
    "name": "bybit",
    "key": "YOUR_API_KEY",
    "secret": "YOUR_API_SECRET"
  },
  "api_server": {
    "username": "YOUR_USERNAME",
    "password": "YOUR_PASSWORD"
  }
}
```

### 3. Запуск системы

```bash
# Запуск Фунтика
cd /home/max/freqtrade/user_data/pump_dump_strategy
./START_FUNTIK.sh

# Запуск Киро анализатора
cd /home/max/freqtrade/mcp_signal_analyzer
./run_monitor.sh
```

---

## 📊 Принцип работы

### 1. Получение сигналов

- **Scanner**: Анализирует 332 пары на Bybit каждые 5 минут
- **DEX анализ**: Сигналы от dex_scanner (pump/dump детекция)
- **Wait queue**: Слабые сигналы (5-6/10) ждут подтверждения

### 2. Фильтрация

- **HTF фильтр**: LONG блокируется если HTF < +2.3%, SHORT если HTF > -2.3%
- **Volume spike**: LONG требует vol_spike > 0.5x (SHORT без фильтра)
- **Lorentzian distance**: Проверка похожести на исторические паттерны
- **LightGBM score**: ML оценка вероятности успеха

### 3. Вход в позицию

- **Размер**: 1250 USDT на сделку (с плечом 3x)
- **Тип ордера**: Limit (order_book_top=2)
- **Тайминг**: Unfilled timeout 3 минуты

### 4. Управление позицией

- **Частичные TP**:
  - 30% позиции @ +1.5%
  - 30% позиции @ +2.5%
  - 40% runner (трейлинг)
- **Trailing stop**: +0.3% от цены входа, offset +1.2%
- **Stop-loss**: -1.0% (на бирже)

---

## 🧠 Машинное обучение

### LightGBM Scorer

- **Samples**: 7485 сделок
- **Features**: 36+ (RSI, MACD, Bollinger, Volume, HTF, etc.)
- **Accuracy**: 54.5% (CV)
- **AUC**: 57.47%

### Переобучение модели

```bash
python scripts/retrain_lightgbm_scorer.py
```

Модель обучается на исторических данных из `trade_snapshots.db`.

---

## 📈 Статистика (по состоянию на 2026-09-23)

- **Всего сделок**: 12,577
- **Режим работы**: Dry Run (1 год+)
- **Баланс**: 10M USDT (виртуальный)
- **Пары**: 332 активных
- **Leverage**: 3x
- **Max открытых**: 10 сделок

---

## 🛠 Техническая информация

### Используемые технологии

- **Freqtrade**: Основной движок торговли
- **LightGBM**: Градиентный бустинг для ML
- **Bybit**: Биржа (Futures Linear USDT)
- **SQLite**: Хранение сделок и фич
- **ntfy.sh**: Уведомления о сигналах
- **MCP (Киро)**: Автоанализ сигналов

### Таймзона

Все таймстемпы в **MSK (UTC+3)**. База данных и логи синхронизированы.

### API

Freqtrade API работает на `http://0.0.0.0:8083` (требуется авторизация).

---

## 🔧 Обслуживание

### Мониторинг

```bash
# Проверить статус
ps aux | grep freqtrade

# Проверить логи
tail -f /home/max/freqtrade/user_data/pump_dump_strategy/logs/funtik.log
tail -f /home/max/freqtrade/user_data/pump_dump_strategy/logs/funtik_audit.log
```

### Перезапуск

```bash
# Убить процесс
kill -9 <PID>

# Запустить заново
./RESTART_FUNTIK.sh
```

### Backup базы данных

```bash
# Backup перед изменениями
cp databases/tradesv3_oleg_rl.sqlite databases/tradesv3_backup_$(date +%Y%m%d).sqlite
```

---

## ⚠️ Важные замечания

1. **Dry run**: Система работает в режиме симуляции. Реальные деньги НЕ используются.
2. **API ключи**: Замените placeholder значения в конфиге на свои ключи.
3. **Таймзона**: Система использует MSK (UTC+3). Не меняйте таймзону без миграции БД.
4. **Max adjustments**: `max_entry_position_adjustment: -1` (unlimited) для работы TP.
5. **Whitelist**: 332 пары из Bybit. Не изменяйте без согласования.

---

## 📝 История изменений

### 2026-09-23
- ✅ Исправлена система TP (max_entry_position_adjustment: 0 → -1)
- ✅ HTF пороги изменены: ±3.0% → ±2.3%
- ✅ Volume фильтр для LONG: 1.0 → 0.5
- ✅ Volume фильтр для SHORT: удален
- ✅ Баланс dry_run: 1M → 10M USDT

### 2026-09-22
- ✅ Миграция таймзоны UTC → MSK (128,487 записей)
- ✅ Переобучена LightGBM модель на MSK timestamps

### 2026-07-07
- ✅ Исправлена логика LONG mirror block (HTF фильтр)
- ✅ Исправлен баг с snapshots данными

---

## 📧 Контакты

Для вопросов и поддержки: создавайте issue в репозитории.

---

**Made with 🚀 by Funtik Team**
