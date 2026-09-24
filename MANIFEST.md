# FUNTIK SYSTEM - FILE MANIFEST

**Создано:** 2026-09-23 21:30 MSK  
**Статус:** РАБОЧАЯ ВЕРСИЯ (текущие активные файлы)

---

## 1. MCP Signal Analyzer

| Файл | Назначение |
|------|------------|
| auto_process_queue.py | Главный анализатор Киро |
| scanner_parser.py | Парсер 36 фич из scanner.log |
| scanner_log_monitor.py | Мониторинг scanner.log (PID 2236358) |
| ntfy_to_funtik.py | Мост ntfy → Funtik |
| wait_queue_monitor.py | Мониторинг условий ЖДАТЬ |
| run_monitor.sh | Скрипт запуска мониторов |
| trigger_kiro.sh | Триггер Киро анализа |

## 2. Strategy

| Файл | Назначение |
|------|------------|
| PumpDumpReversalStrategy_v2.py | Главная стратегия (163KB) |
| directional_lgbm.py | LightGBM scorer |
| lorentzian_classifier.py | Lorentzian classifier |
| analyze_v2_entries_v2.py | Анализ сделок |
| engines/*.py | Модули (data, feature, regime, signal, risk, ai_edge) |

## 3. Configs

| Файл | Назначение |
|------|------------|
| config_pumpdump_reversal.json | Конфиг Фунтика (dry_run: true) |

## 4. Databases

| Файл | Размер | Назначение |
|------|--------|------------|
| tradesv3_oleg_rl.sqlite | ~30MB | Все сделки (12577+) |
| trade_snapshots.db | ~1MB | Фичи для ML |

## 5. Models

| Файл | Назначение |
|------|------------|
| lightgbm_scorer.pkl | Модель (7485 samples, CV Acc 54.5%, AUC 57.47%) |
| lightgbm_meta.json | Метаданные модели |

## 6. Scripts

| Файл | Назначение |
|------|------------|
| retrain_lightgbm_scorer.py | Переобучение модели |
| START_FUNTIK.sh | Запуск Фунтика |
| RESTART_FUNTIK.sh | Перезапуск |

## 7. Documentation

| Файл | Назначение |
|------|------------|
| SYSTEM_START_COMMANDS.md | Команды запуска |
| СИСТЕМА_ОПИСАНИЕ.md | Полное описание |

---

## ВОССТАНОВЛЕНИЕ

```bash
# 1. MCP
cp -r mcp_signal_analyzer/* /home/max/freqtrade/mcp_signal_analyzer/

# 2. Strategy
cp -r strategy/* /home/max/freqtrade/user_data/pump_dump_strategy/

# 3. Configs
cp configs/config_pumpdump_reversal.json /home/max/freqtrade/user_data/pump_dump_strategy/

# 4. Models
cp models/* /home/max/freqtrade/user_data/pump_dump_strategy/data/

# 5. Databases (опционально - только для backup)
# cp databases/*.sqlite /home/max/freqtrade/user_data/
```

