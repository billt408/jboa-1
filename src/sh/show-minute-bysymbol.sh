#!/usr/bin/bash

set -Eeuo pipefail

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") <SYMBOL[,SYMBOL2,...]> [BASE_OR_LOGS_DIR]

Filter the latest minute-summary*.jsonl log for one or more comma-separated symbols.

Examples:
  $(basename "$0") NVDA
  $(basename "$0") NVDA,AAPL,AMD
  $(basename "$0") "NVDA, AAPL" /home/billt/jboa-1
EOF
}

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    [[ $# -ge 1 ]] && exit 0 || exit 1
fi

RAW_SYMBOLS="$1"
CUSTOM_DIR="${2:-}"

if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is not installed or is not in PATH." >&2
    exit 1
fi

# Parse comma-separated symbol list (supports 1 symbol or multiple, trims spaces, uppercases, deduplicates).
SYMBOLS_CLEAN=""
IFS=',' read -ra _raw_parts <<< "$RAW_SYMBOLS"
for part in "${_raw_parts[@]}"; do
    sym="${part#"${part%%[![:space:]]*}"}"
    sym="${sym%"${sym##*[![:space:]]}"}"
    sym="${sym^^}"
    [[ -n "$sym" ]] || continue
    if [[ -z "$SYMBOLS_CLEAN" ]]; then
        SYMBOLS_CLEAN="$sym"
    elif [[ ",$SYMBOLS_CLEAN," != *",$sym,"* ]]; then
        SYMBOLS_CLEAN="$SYMBOLS_CLEAN,$sym"
    fi
done

if [[ -z "$SYMBOLS_CLEAN" ]]; then
    echo "ERROR: No valid symbols provided." >&2
    usage
    exit 1
fi

shopt -s nullglob

find_latest_in_dir() {
    local dir="$1"
    local f latest_file=""
    [[ -d "$dir" ]] || return 1
    local matched=("$dir"/minute-summary*.jsonl)
    (( ${#matched[@]} > 0 )) || return 1
    for f in "${matched[@]}"; do
        [[ -f "$f" ]] || continue
        if [[ -z "$latest_file" || "$f" -nt "$latest_file" ]]; then
            latest_file="$f"
        fi
    done
    [[ -n "$latest_file" ]] || return 1
    printf '%s\n' "$latest_file"
}

latest=""
if [[ -n "$CUSTOM_DIR" ]]; then
    if [[ ! -d "$CUSTOM_DIR" ]]; then
        printf 'ERROR: Directory does not exist: %s\n' "$CUSTOM_DIR" >&2
        exit 1
    fi
    latest="$(
        find_latest_in_dir "$CUSTOM_DIR/src/logs" \
        || find_latest_in_dir "$CUSTOM_DIR" \
        || find_latest_in_dir "$CUSTOM_DIR/logs" \
        || true
    )"
    SEARCH_DESC="$CUSTOM_DIR (checked $CUSTOM_DIR/src/logs, $CUSTOM_DIR, $CUSTOM_DIR/logs)"
else
    SEARCH_DESC="/home/billt/jboa-1/src/logs (fallback: /home/billt/jboa-1/logs)"
    latest="$(
        find_latest_in_dir "/home/billt/jboa-1/src/logs" \
        || find_latest_in_dir "/home/billt/jboa-1/logs" \
        || true
    )"
fi

if [[ -z "$latest" ]]; then
    printf 'ERROR: No minute-summary*.jsonl files found in %s\n' "$SEARCH_DESC" >&2
    exit 1
fi

printf 'Reading: %s\n' "$latest" >&2
printf 'Filtering symbols: %s\n' "$SYMBOLS_CLEAN" >&2

# 1. Keep each provider record intact, with .symbols filtered to only the requested symbol(s).
jq -r --arg syms "$SYMBOLS_CLEAN" '
  ($syms | split(",")) as $wanted
  | del(.kinematics_summary)
  | .symbols |= with_entries(select(.key as $k | ($wanted | index($k)) != null))
  | select((.symbols | length) > 0)
  | ., "=============="
' "$latest"

# 2. Group matching symbol records by [minute_end_utc, symbol] across providers.
jq -rs --arg syms "$SYMBOLS_CLEAN" '
  ($syms | split(",")) as $wanted
  | [
      .[] as $record
      | $record.symbols
      | to_entries[]
      | select(.key as $k | ($wanted | index($k)) != null)
      | {
          provider: $record.provider,
          minute_end_utc: $record.minute_end_utc,
          symbol: .key,
          data: .value
        }
    ]
  | group_by([.minute_end_utc, .symbol])
  | .[]
  | (
      (sort_by(.provider)[] | tojson),
      "=============="
    )
' "$latest"

# 3. Side-by-side RSI / price comparison per minute and requested symbol.
jq -rs --arg syms "$SYMBOLS_CLEAN" '
  ($syms | split(",")) as $wanted
  | [
      .[] as $record
      | $record.symbols
      | to_entries[]
      | select(.key as $k | ($wanted | index($k)) != null)
      | {
          provider: $record.provider,
          minute_start_utc: $record.minute_start_utc,
          minute_end_utc: $record.minute_end_utc,
          symbol: .key,
          data: .value
        }
    ]
  | group_by([.minute_end_utc, .symbol])
  | .[]
  | sort_by(.provider)
  | (
      "Symbol: \(.[0].symbol)",
      "Minute start: \(.[0].minute_start_utc)",
      "Minute end:   \(.[0].minute_end_utc)",
      "",
      (
        .[]
        | "  \(.provider): \(.data | tojson)"
      ),
      "",
      "RSI / price comparison:",
      (
        .[]
        | "  \(.provider): RSI=\(if .data.rsi == null then "N/A" else .data.rsi end)  Price=\(if .data.close == null then "N/A" else .data.close end)"
      ),
      "=============="
    )
' "$latest"