#!/usr/bin/env bash
set -euo pipefail

STOP_SERVICE="jboa-rsi.service"
START_SERVICE="jboa-rsi.service"


WORK_DIR="/home/billt/jboa-1/src"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="${WORK_DIR}/logs"
BACKUP_DIR="${WORK_DIR}/logs_backup.${TIMESTAMP}"

echo "==> Stopping ${STOP_SERVICE}..."
sudo systemctl stop "${STOP_SERVICE}"

if [[ -d "${LOG_DIR}" ]]; then
    echo "==> Moving logs -> ${BACKUP_DIR}"
    mv "${LOG_DIR}" "${BACKUP_DIR}"
else
    echo "==> No '${LOG_DIR}' directory found; skipping backup."
fi

echo "==> Starting ${START_SERVICE}..."
sudo systemctl start "${START_SERVICE}"

echo "==> Service status:"
sudo systemctl status "${START_SERVICE}" --no-pager -n 10