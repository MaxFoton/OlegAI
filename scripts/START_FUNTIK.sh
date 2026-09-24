#!/bin/bash
# Запуск Фунтик (SHORT) для tmux 1

cd /home/max/freqtrade

# Активируем venv
source .venv/bin/activate

# Запускаем Фунтик
python3 -m freqtrade trade \
  --config user_data/pump_dump_strategy/config_okx_freqai_rl_v19.json \
  --strategy FuntikRuleBasedStrategy \
  --strategy-path user_data/pump_dump_strategy/

echo "✅ Фунтик запущен в tmux 1"
