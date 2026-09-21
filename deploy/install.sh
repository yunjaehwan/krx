#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/krx-bot"
SERVICE="krx-bot.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh"
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip git ca-certificates tzdata

id -u krxbot >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin krxbot

mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/krx_intraday_state" "$APP_DIR/krx_intraday_logs"
cp -r . "$APP_DIR/"

chown -R krxbot:krxbot "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 750 "$APP_DIR/deploy/run_bot.sh"

if [[ ! -d "$APP_DIR/.venv" ]]; then
  sudo -u krxbot python3 -m venv "$APP_DIR/.venv"
fi

sudo -u krxbot "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u krxbot "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/env.example" "$APP_DIR/.env"
  chown krxbot:krxbot "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo
  echo "[ACTION REQUIRED] Edit $APP_DIR/.env and enter your KIS/Telegram values."
fi

install -m 0644 "$APP_DIR/deploy/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE"

echo
echo "Installation complete."
echo "1) Edit: sudo nano $APP_DIR/.env"
echo "2) Start: sudo systemctl start $SERVICE"
echo "3) Status: sudo systemctl status $SERVICE"
echo "4) Logs: sudo tail -f $APP_DIR/logs/bot.log"
