#!/usr/bin/env bash
set -euo pipefail

# Default directory (can be overridden by passing a directory path as $1)
LOG_DIR="${1:-/home/billt/jboa-1/src/logs/option_buyer}"

if [[ ! -d "$LOG_DIR" ]]; then
    echo "Error: Directory '$LOG_DIR' does not exist." >&2
    exit 1
fi

# Find the latest *OPTION_BUYER* file in the target directory
LATEST_LOG=$(ls -1t "$LOG_DIR"/*OPTION_BUYER* 2>/dev/null | head -n 1 || true)

if [[ -z "$LATEST_LOG" || ! -f "$LATEST_LOG" ]]; then
    echo "Error: No '*OPTION_BUYER*' files found in '$LOG_DIR'." >&2
    exit 1
fi

echo "Analyzing Log: $LATEST_LOG"
echo "=================================================================="

awk '
{
    # 1. Extract primary RSI value
    # Handles both standalone "rsi=34.68" and "trigger=rsi=34.35<=low..."
    rsi = ""
    for (i = 1; i <= NF; i++) {
        if ($i ~ /^(trigger=)?rsi=[0-9]+(\.[0-9]+)?/) {
            val = $i
            sub(/^(trigger=)?rsi=/, "", val)
            sub(/[^0-9.].*$/, "", val)
            rsi = val + 0
            break
        }
    }

    # Skip lines without an RSI field
    if (rsi == "") next

    # 2. Extract Reason (or fallback to Decision/Event if no reason= tag exists)
    reason = "NO_REASON_TAG"
    if (match($0, /reason=[^ (]+/)) {
        reason = substr($0, RSTART + 7, RLENGTH - 7)
    } else if (match($0, /decision=[^ ]+/)) {
        reason = "DECISION:" substr($0, RSTART + 9, RLENGTH - 9)
    }

    total_lines++

    # 3. Classify > 35 vs <= 35 (catching 33.99 through 35.00 in the <= 35 group)
    if (rsi > 35.00) {
        gt35++
    } else {
        le35++

        # Determine bucket:
        # 34.00 - 35.00 -> bucket_idx = 34 (prints as 33.99 - 35.00)
        # 33.00 - 33.99 -> bucket_idx = 33 (prints as 32.99 - 34.00)
        # ... down to 0.00 - 0.99 -> bucket_idx = 0
        if (rsi == 35.00) {
            b = 34
        } else {
            b = int(rsi)
        }

        bucket_count[b]++
        reason_count[b, reason]++
        all_reasons[reason] = 1
    }
}
END {
    if (total_lines == 0) {
        print "No lines containing RSI data were found."
        exit 0
    }

    printf "Total RSI Lines Analyzed: %d\n\n", total_lines

    printf "OVERALL RSI DISTRIBUTION\n"
    printf "------------------------------------------------------------------\n"
    printf "  RSI > 35.00  : %6d lines (%6.2f%%)\n", gt35, (gt35 / total_lines) * 100
    printf "  RSI <= 35.00 : %6d lines (%6.2f%%)\n\n", le35, (le35 / total_lines) * 100

    if (le35 == 0) exit 0

    printf "BREAKDOWN BELOW RSI 35 (WITH REASONS)\n"
    printf "==================================================================\n"

    # Iterate downward from the 33.99 - 35.00 bucket down to 0
    for (b = 34; b >= 0; b--) {
        if (bucket_count[b] > 0) {
            low_label = (b == 0) ? 0.00 : (b - 0.01)
            high_label = b + 1.00
            b_cnt = bucket_count[b]
            b_pct_total = (b_cnt / total_lines) * 100
            b_pct_le35  = (b_cnt / le35) * 100

            printf "\nRange %5.2f through %5.2f : %5d lines (%5.2f%% of total | %5.2f%% of <=35)\n", \
                   low_label, high_label, b_cnt, b_pct_total, b_pct_le35
            printf "------------------------------------------------------------------\n"

            for (r in all_reasons) {
                rc = reason_count[b, r] + 0
                if (rc > 0) {
                    r_pct = (rc / b_cnt) * 100
                    printf "    %-30s : %5d (%6.2f%%)\n", r, rc, r_pct
                }
            }
        }
    }
}
' "$LATEST_LOG"
