#!/usr/bin/env bash

# Usage: ./search.sh "search_term" /path/to/directory

SEARCH_TERM="$1"
TARGET_DIR="${2:-.}" # Defaults to the current directory if none is provided

if [[ -z "$SEARCH_TERM" ]]; then
    echo "Usage: $0 <search_string> [directory]"
    exit 1
fi

if [[ ! -d "$TARGET_DIR" ]]; then
    echo "Error: Directory '$TARGET_DIR' not found."
    exit 1
fi

# -r: recursive
# -n: show line numbers
# -i: case-insensitive
# -I: ignore binary files
grep -rniI -- "$SEARCH_TERM" "$TARGET_DIR"
