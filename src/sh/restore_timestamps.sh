#!/usr/bin/env bash
set -euo pipefail

# Default to current directory or pass a directory as the first argument
TARGET_DIR="${1:-.}"

shopt -s nullglob
for filepath in "${TARGET_DIR}"/*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-*.log; do
    filename="$(basename "${filepath}")"

    # Extract YYYYMMDD and HHMMSS from the filename pattern
    if [[ "${filename}" =~ -([0-9]{8})-([0-9]{4})([0-9]{2})- ]]; then
        date_part="${BASH_REMATCH[1]}"   # e.g., 20261118
        hhmm_part="${BASH_REMATCH[2]}"   # e.g., 1122
        ss_part="${BASH_REMATCH[3]}"     # e.g., 18

        touch_ts="${date_part}${hhmm_part}.${ss_part}"
        echo "Updating ${filename} -> ${touch_ts}"
        touch -t "${touch_ts}" "${filepath}"
    fi
done
