#!/bin/bash
# Триггерит проверку сигналов в Kiro через создание файла-триггера

TRIGGER_FILE="/home/max/freqtrade/.kiro/check_signals_trigger"

# Создаём файл-триггер (или обновляем timestamp)
date > "$TRIGGER_FILE"
