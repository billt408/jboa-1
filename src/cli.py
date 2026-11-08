#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.request
import urllib.error

CLI_SERVER_URL = "http://127.0.0.1:5001"


def send_command(endpoint, payload):
    url = f"{CLI_SERVER_URL}{endpoint}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            print(json.dumps(res_data, indent=2))
    except urllib.error.HTTPError as e:
        print(f"Error [{e.code}]: {e.read().decode('utf-8')}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"Failed to connect to CLI server: {e.reason}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Multi-provider RSI CLI Control Panel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Status Command
    subparsers.add_parser("status", help="Get runtime engine status")

    # Force Finalize Command
    subparsers.add_parser(
        "finalize", help="Force evaluate and flush due summary minutes immediately"
    )

    # Manual Metric Inject Command
    inject = subparsers.add_parser(
        "inject", help="Manually inject a custom close metric bar to a provider slot"
    )
    inject.add_argument(
        "--provider", required=True, help="Provider name (e.g. ALPACA, YAHOOFINANCE)"
    )
    inject.add_argument("--symbol", required=True, help="Ticker symbol (e.g. AAPL)")
    inject.add_argument(
        "--price", type=float, required=True, help="Closing price value"
    )
    inject.add_argument(
        "--timestamp", help="ISO Timestamp string (defaults to current time)"
    )

    # Reconnect Command
    reconnect = subparsers.add_parser(
        "reconnect", help="Force restart and reconnect a specific provider stream"
    )
    reconnect.add_argument(
        "--provider",
        required=True,
        help="Provider name to cycle (e.g. ALPACA, TWELVEDATA)",
    )

    args = parser.parse_args()

    if args.command == "status":
        send_command("/cli/status", {})
    elif args.command == "finalize":
        send_command("/cli/finalize", {})
    elif args.command == "inject":
        payload = {
            "provider": args.provider.upper(),
            "symbol": args.symbol.upper(),
            "price": args.price,
            "timestamp": args.timestamp,
        }
        send_command("/cli/inject", payload)
    elif args.command == "reconnect":
        payload = {"provider": args.provider.upper()}
        send_command("/cli/reconnect", payload)


if __name__ == "__main__":
    main()
