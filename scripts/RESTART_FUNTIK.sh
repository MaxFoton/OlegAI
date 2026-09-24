#!/bin/bash
# Перезапуск Фунтика (PumpDumpReversalStrategyV2) для подхвата новых пар из whitelist

LOG_FILE="/home/max/freqtrade/user_data/pump_dump_strategy/logs/funtik_restart.log"
TMUX_SESSION="1"
TMUX_WINDOW="0"
STRATEGY="PumpDumpReversalStrategyV2"
CONFIG="user_data/pump_dump_strategy/config_pumpdump_reversal.json"
STRATEGY_PATH="user_data/pump_dump_strategy/"

mkdir -p /home/max/freqtrade/user_data/pump_dump_strategy/logs

echo "========================================" >> "$LOG_FILE"
echo "🔄 Starting Funtik restart at $(date)" >> "$LOG_FILE"

# 1. Убиваем процесс Фунтика
echo "🔪 Killing Funtik process ($STRATEGY)..." >> "$LOG_FILE"
pkill -f "freqtrade trade.*$STRATEGY" 2>/dev/null || true

# Ждем завершения процесса (до 15 секунд)
for i in $(seq 1 15); do
    if ! pgrep -f "freqtrade trade.*$STRATEGY" > /dev/null 2>&1; then
        echo "✅ Process stopped after ${i}s" >> "$LOG_FILE"
        break
    fi
    sleep 1
done

# Если всё ещё жив — SIGKILL
if pgrep -f "freqtrade trade.*$STRATEGY" > /dev/null 2>&1; then
    echo "⚠️ Process still alive, sending SIGKILL..." >> "$LOG_FILE"
    pkill -9 -f "freqtrade trade.*$STRATEGY" 2>/dev/null || true
    sleep 2
fi

# 2. Проверяем что tmux сессия существует
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "⚠️ Tmux session $TMUX_SESSION not found, skipping restart" >> "$LOG_FILE"
    exit 1
fi

# 3. Запускаем Фунтика снова
echo "🚀 Starting Funtik in tmux session $TMUX_SESSION..." >> "$LOG_FILE"
tmux send-keys -t "${TMUX_SESSION}:${TMUX_WINDOW}" "" C-c 2>/dev/null || true
sleep 1
tmux send-keys -t "${TMUX_SESSION}:${TMUX_WINDOW}" "cd /home/max/freqtrade && source .venv/bin/activate && python3 -m freqtrade trade --config $CONFIG --strategy $STRATEGY --strategy-path $STRATEGY_PATH" C-m

# 4. Проверяем что запустился
sleep 5
if pgrep -f "freqtrade trade.*$STRATEGY" > /dev/null 2>&1; then
    PID=$(pgrep -f "freqtrade trade.*$STRATEGY")
    echo "✅ Funtik restarted successfully, PID=$PID at $(date)" >> "$LOG_FILE"
else
    echo "❌ Funtik failed to start at $(date)" >> "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"
