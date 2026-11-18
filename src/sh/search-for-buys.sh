#!/usr/bin/env bash
set -euo pipefail

SEARCH_DIR="/home/billt/jboa-1/OLDLOGS"
BUYER_DEST="/home/billt/jboa-1/option_buyer"
SELLER_DEST="/home/billt/jboa-1/option_seller"

if [[ ! -d "${SEARCH_DIR}" ]]; then
    echo "Error: Directory '${SEARCH_DIR}' does not exist." >&2
    exit 1
fi

mkdir -p "${BUYER_DEST}" "${SELLER_DEST}"

find "${SEARCH_DIR}" -type f -print0 | while IFS= read -r -d '' file; do
    if grep -i "2026-11-18" "${file}" | grep -i "submit" | grep -iq "AMZN"; then
        echo "MATCH: ${file}"

        if [[ "${file}" == *"/option_buyer/"* ]]; then
            cp -a "${file}" "${BUYER_DEST}/"
            echo "  -> Copied to ${BUYER_DEST}/"
        elif [[ "${file}" == *"/option_seller/"* ]]; then
            cp -a "${file}" "${SELLER_DEST}/"
            echo "  -> Copied to ${SELLER_DEST}/"
        else
            echo "  -> Warning: Could not determine buyer/seller destination for ${file}" >&2
        fi
    fi
done
