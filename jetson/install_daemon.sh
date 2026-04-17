#!/usr/bin/env bash
#
# One-shot installer for the Go2 record daemon as a systemd service.
#
# Run on the Jetson (not on the laptop):
#     sudo bash jetson/install_daemon.sh
#
# What it does (idempotent):
#   1. Ensures /home/unitree/GO2_DATA exists and is writable by `unitree`.
#   2. Copies record_daemon.service to /etc/systemd/system/.
#   3. Reloads systemd, enables + starts the service.
#   4. Prints current status so you can verify it came up cleanly.
#
# Uninstall:
#     sudo systemctl disable --now record_daemon
#     sudo rm /etc/systemd/system/record_daemon.service
#     sudo systemctl daemon-reload

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_SRC="$REPO_DIR/jetson/record_daemon.service"
SERVICE_DST="/etc/systemd/system/record_daemon.service"
DATA_DIR="/home/unitree/GO2_DATA"

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root. Re-run with: sudo bash $0" >&2
    exit 1
fi

if [[ ! -f "$SERVICE_SRC" ]]; then
    echo "Service file not found: $SERVICE_SRC" >&2
    exit 1
fi

echo "[1/4] Ensuring $DATA_DIR exists and is owned by unitree..."
mkdir -p "$DATA_DIR"
chown -R unitree:unitree "$DATA_DIR"

echo "[2/4] Installing unit file → $SERVICE_DST"
cp "$SERVICE_SRC" "$SERVICE_DST"
chmod 644 "$SERVICE_DST"

echo "[3/4] Reloading systemd..."
systemctl daemon-reload

echo "[4/4] Enabling and starting record_daemon..."
systemctl enable --now record_daemon.service

echo
systemctl status record_daemon.service --no-pager || true
echo
echo "Done. Tail the log with:"
echo "    journalctl -u record_daemon -f"
