#!/usr/bin/env python3
import glob
import json
import os
from pathlib import Path
from datetime import datetime

LOG_DIR = Path(__file__).resolve().parent / "logs"


def get_latest_summary_file():
    """Finds the most recently modified minute-summary JSONL file."""
    search_pattern = os.path.join(LOG_DIR, "minute-summary-*.jsonl")
    files = glob.glob(search_pattern)
    if not files:
        return None
    # Sort files by modification time, newest first
    files.sort(key=os.path.getmtime, reverse=True)
    return Path(files[0])


def parse_logs(file_path):
    """Parses the JSONL log file into a structured time-series dictionary per provider and symbol."""
    series_data = {}

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            if record.get("type") != "MINUTE_SUMMARY":
                continue

            provider = record.get("provider")
            timestamp = record.get("minute_start_utc")
            symbols = record.get("symbols", {})

            for symbol, metrics in symbols.items():
                if (
                    metrics.get("status") not in ("ok", "partial")
                    or metrics.get("rsi") is None
                ):
                    continue

                key = (provider, symbol)
                if key not in series_data:
                    series_data[key] = []

                series_data[key].append(
                    {
                        "timestamp": timestamp,
                        "price": metrics["close"],
                        "rsi": metrics["rsi"],
                    }
                )

    # Sort each series chronologically by timestamp
    for key in series_data:
        series_data[key].sort(key=lambda x: x["timestamp"])

    return series_data


def find_local_extrema(data, key_metric="rsi"):
    """Finds mathematical local peaks and troughs using a 3-point comparison window."""
    swings = {"peaks": [], "troughs": []}
    n = len(data)
    if n < 3:
        return swings

    for i in range(1, n - 1):
        prev_val = data[i - 1][key_metric]
        curr_val = data[i][key_metric]
        next_val = data[i + 1][key_metric]

        # Local Peak (Swing High)
        if curr_val > prev_val and curr_val > next_val:
            swings["peaks"].append(data[i])

        # Local Trough (Swing Low)
        elif curr_val < prev_val and curr_val < next_val:
            swings["troughs"].append(data[i])

    return swings


def main():
    target_file = get_latest_summary_file()
    if not target_file:
        print(f"Error: No minute-summary logs found in {LOG_DIR}")
        return

    print(f"Parsing latest log file: {target_file.name}")
    print(
        f"File modified time: {datetime.fromtimestamp(os.path.getmtime(target_file))}"
    )
    print("=" * 70)

    all_series = parse_logs(target_file)

    if not all_series:
        print("No valid 'ok' or 'partial' summaries found in the log file yet.")
        return

    # Process each Provider + Symbol combination found
    for (provider, symbol), data_points in sorted(all_series.items()):
        swings = find_local_extrema(data_points, key_metric="rsi")

        if not swings["peaks"] and not swings["troughs"]:
            continue

        print(
            f"\n?? Provider: {provider} | Symbol: {symbol} ({len(data_points)} total data points)"
        )
        print("-" * 50)

        print("  [SWING HIGHS / PEAKS]")
        for peak in swings["peaks"]:
            print(
                f"    Time: {peak['timestamp']} | Price: {peak['price']:.4f} | RSI Peak: {peak['rsi']:.2f}"
            )

        print("  [SWING LOWS / TROUGHS]")
        for trough in swings["troughs"]:
            print(
                f"    Time: {trough['timestamp']} | Price: {trough['price']:.4f} | RSI Trough: {trough['rsi']:.2f}"
            )


if __name__ == "__main__":
    main()
