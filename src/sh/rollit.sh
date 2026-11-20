#!/usr/bin/env bash
set -euo pipefail

STOP_SERVICE="jboa-rsi.service"
START_SERVICE="jboa-rsi.service"

BASE_DIR="/home/billt/jboa-1/src"
WORK_DIR="/home/billt/jboa-1/OLDLOGS"
TAR_NAME="all_logs.tar"
GZ_NAME="${TAR_NAME}.gz"
TAR_PATH="${WORK_DIR}/${TAR_NAME}"
GZ_PATH="${WORK_DIR}/${GZ_NAME}"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="${BASE_DIR}/logs"
BACKUP_DIR="${WORK_DIR}/logs_backup.${TIMESTAMP}"

# Ensure the destination directory exists
mkdir -p "${WORK_DIR}"

echo "==> Stopping ${STOP_SERVICE}..."
sudo systemctl stop "${STOP_SERVICE}"

if [[ -d "${LOG_DIR}" ]]; then
    echo "==> Moving logs -> ${BACKUP_DIR}"
    mv "${LOG_DIR}" "${BACKUP_DIR}"

    for subdir in option_buyer option_seller optionchains; do
        if [[ -d "${BACKUP_DIR}/${subdir}" ]]; then
            echo "==> Restoring history for ${subdir}..."
            mkdir -p "${LOG_DIR}/${subdir}"
            cp -a "${BACKUP_DIR}/${subdir}/." "${LOG_DIR}/${subdir}/"
        fi
    done
else
    echo "==> No '${LOG_DIR}' directory found; creating empty backup folder."
    mkdir -p "${BACKUP_DIR}"
fi

# Move jboa.log if it exists
if [[ -f "${BASE_DIR}/jboa.log" ]]; then
    mv "${BASE_DIR}/jboa.log" "${BACKUP_DIR}/jboa.log"
fi

# Collect everything in WORK_DIR except the archive files

echo "==> Starting ${START_SERVICE}..."
sudo systemctl start "${START_SERVICE}"

echo "==> Service status:"
sudo systemctl status "${START_SERVICE}" --no-pager -n 10

shopt -s nullglob dotglob
items_to_archive=()
for item in "${WORK_DIR}"/*; do
    base_item="$(basename "${item}")"
    if [[ "${base_item}" != "${GZ_NAME}" && "${base_item}" != "${TAR_NAME}" ]]; then
        items_to_archive+=("${base_item}")
    fi
done
shopt -u nullglob dotglob

if (( ${#items_to_archive[@]} > 0 )); then
    # Decompress existing .tar.gz if present so tar can append to it
    if [[ -f "${GZ_PATH}" ]]; then
        echo "==> Decompressing existing ${GZ_NAME}..."
        gunzip -f "${GZ_PATH}"
    fi

    echo "==> Appending files to ${TAR_NAME}:"
    tar -rvf "${TAR_PATH}" -C "${WORK_DIR}" "${items_to_archive[@]}"

    echo "==> Compressing ${TAR_NAME} -> ${GZ_NAME}..."
    gzip -f "${TAR_PATH}"

    echo "==> Verifying ${GZ_NAME} integrity..."
    if ! tar -tzf "${GZ_PATH}" > /dev/null; then
        echo "ERROR: Archive verification failed! Original files were NOT deleted." >&2
        exit 1
    fi

    echo "==> Verification passed! Cleaning up appended directories and files..."
    for item in "${items_to_archive[@]}"; do
        rm -rf "${WORK_DIR:?}/${item}"
    done
else
    echo "==> Nothing new to append to ${GZ_NAME}."
fi
