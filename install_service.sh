#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="bale-group-manager"
if [ ! -f "$APP_DIR/.env" ]; then
  echo "❌ اول فایل .env را از .env.example بساز و توکن را وارد کن."
  exit 1
fi
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -U pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
cat >/tmp/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Bale Group Manager Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo mv /tmp/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}.service
sudo systemctl daemon-reload
sudo systemctl enable --now ${SERVICE_NAME}
sudo systemctl status ${SERVICE_NAME} --no-pager
