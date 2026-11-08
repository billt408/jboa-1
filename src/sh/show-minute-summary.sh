#!/usr/bin/bash

set -Eeuo pipefail

# Optional first argument overrides the directory to search.
SEARCH_DIR="${1:-/home/billt/jboa-1}/src/logs"

if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is not installed or is not in PATH." >&2
    exit 1
fi

if [[ ! -d "$SEARCH_DIR" ]]; then
    printf 'ERROR: Directory does not exist: %s\n' "$SEARCH_DIR" >&2
    exit 1
fi

shopt -s nullglob

files=("$SEARCH_DIR"/minute-summary*.jsonl)

# For the default location, fall back to the application's logs directory.
if (( ${#files[@]} == 0 )) && [[ $# -eq 0 ]]; then
    SEARCH_DIR="/home/billt/jboa-1/logs"
    files=("$SEARCH_DIR"/minute-summary*.jsonl)
fi

latest=""

for file in "${files[@]}"; do
    [[ -f "$file" ]] || continue

    if [[ -z "$latest" || "$file" -nt "$latest" ]]; then
        latest="$file"
    fi
done

if [[ -z "$latest" ]]; then
    printf 'ERROR: No minute-summary*.jsonl files found in %s\n' \
        "$SEARCH_DIR" >&2
    exit 1
fi

printf 'Reading: %s\n' "$latest" >&2

# Keep each provider record intact, including every symbol and field.
# Pretty-print the JSON, then print an unquoted separator.
jq -r '., "=============="' "$latest"


jq -rs '                              
  [
    .[] as $record
    | $record.symbols
    | to_entries[]
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

jq -rs '
  [
    .[] as $record
    | $record.symbols
    | to_entries[]
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