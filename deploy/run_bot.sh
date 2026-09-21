#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/krx-bot"
ENV_FILE="$APP_DIR/.env"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
LOG_DIR="/opt/krx-bot/logs"

mkdir -p "$LOG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[ERROR] $ENV_FILE not found"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

cd "$APP_DIR"
export PYTHONUNBUFFERED=1
export TZ=Asia/Seoul

while true; do
  DOW="$(TZ=Asia/Seoul date +%u)"
  HM="$(TZ=Asia/Seoul date +%H:%M)"

  # 정규장(09:00~15:30)만 감시합니다. 애프터마켓/NXT 확장은 별도 검증 후 진행.
  if [[ "$DOW" -le 5 && "$HM" > "08:55" && "$HM" < "15:31" ]]; then
    echo "[$(TZ=Asia/Seoul date '+%F %T')] starting trading process"
    "$PY" krx_realtime_autotrader.py >> "$LOG_DIR/bot.log" 2>&1 || true
    echo "[$(TZ=Asia/Seoul date '+%F %T')] trading process exited; retrying after 15s"
    sleep 15
  else
    sleep 60
  fi
done
