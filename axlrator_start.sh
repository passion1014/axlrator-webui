#!/bin/bash

LOG_DIR=/app/logs
mkdir -p "$LOG_DIR"

# 날짜 형식
DATE=$(date +"%Y%m%d_%H%M%S")

echo "[Frontend] Starting..."
cd /app/axlrator-webui
nohup npm run preview -- --host 0.0.0.0 > "/app/logs/frontend_$(date +'%Y%m%d_%H%M%S').log" 2>&1 &

echo "[Backend] Starting..."
cd /app/axlrator-webui/backend
./start.sh

