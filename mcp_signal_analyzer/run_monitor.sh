#!/bin/bash
# Wrapper script to run ntfy monitor with freqtrade venv

cd /home/max/freqtrade
source .venv/bin/activate
exec python /home/max/freqtrade/mcp_signal_analyzer/ntfy_monitor.py
