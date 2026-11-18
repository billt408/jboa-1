#!/usr/bin/env bash
set -euo pipefail

STOP_SERVICE="jboa-rsi.service"
START_SERVICE="jboa-rsi.service"

BASE_DIR="/home/billt/jboa-1/src"
WORK_DIR="/home/billt/jboa-1/OLDLOGS"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="${BASE_DIR}/logs"
BACKUP_DIR="${WORK_DIR}/logs_backup.${TIMESTAMP}"

echo "==> Stopping ${STOP_SERVICE}..."
sudo systemctl stop "${STOP_SERVICE}"

if [[ -d "${LOG_DIR}" ]]; then
    echo "==> Moving logs -> ${BACKUP_DIR}"
    mv "${LOG_DIR}" "${BACKUP_DIR}"

    for subdir in option_buyer option_seller; do
        if [[ -d "${BACKUP_DIR}/${subdir}" ]]; then
            echo "==> Restoring history for ${subdir}..."
            mkdir -p "${LOG_DIR}/${subdir}"
            cp -a "${BACKUP_DIR}/${subdir}/." "${LOG_DIR}/${subdir}/"
        fi
    done
else
    echo "==> No '${LOG_DIR}' directory found; skipping backup."
fi

mv "${BASE_DIR}"/jboa.log "${BACKUP_DIR}"/jboa.log
echo "==> Starting ${START_SERVICE}..."
sudo systemctl start "${START_SERVICE}"

echo "==> Service status:"
sudo systemctl status "${START_SERVICE}" --no-pager -n 10
