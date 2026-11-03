import json
import logging
import os
import threading
import time
from datetime import datetime, time as clock_time
from zoneinfo import ZoneInfo

import websocket

import sys
import inspect
from lib.logger import initialize_logging
initialize_logging(default_level=logging.INFO) # Pass logging.DEBUG here if you want full app debug mode
from pathlib import Path
from lib.getStockRSI import mk_twelvedata_client
from lib.utils import JsonIniParser

try:
    # Initialize the parser with the file path
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
    config.load_globals()
except Exception as ce:
    logging.error(f"? Failed to parse configuration file: {ce}")
    sys.exit(1)

try:
    target_symbols = config.get("symbols", "target_symbols")
except Exception as e:
    logging.error(f"Critical Error initializing symbols: {e}")
    sys.exit(2)        

try:
    api_key = config.get("twelvedata_api", "twelvedata_api_key")
    logging.debug(f"Using API key: {api_key}")
except Exception as e:
    logging.error(f"Critical Error loading twelvedata configuration or initializing twelvedata api: {e}")
    sys.exit(2)        
if not api_key:
    raise SystemExit("Set the TWELVEDATA_api_key environment variable first.")

logging.debug("DEBUG SIGNATURE: %s", inspect.signature(mk_twelvedata_client))
client = mk_twelvedata_client(api_key)


def reset_session(day):
    global session_date, last_finalized
    global previous_close, changes, gain_sum, loss_sum
    global average_gain, average_loss

    session_date = day
    last_finalized = -1
    buckets.clear()

    previous_close = None
    changes = 0
    gain_sum = loss_sum = 0.0
    average_gain = average_loss = None

    logging.info("Starting session %s", day)


def calculate_rsi(close):
    global previous_close, changes, gain_sum, loss_sum
    global average_gain, average_loss

    if previous_close is None:
        previous_close = close
        return None

    difference = close - previous_close
    previous_close = close

    gain = max(difference, 0.0)
    loss = max(-difference, 0.0)

    changes += 1
    gain_sum += gain
    loss_sum += loss

    if MODE == "since_open":
        # All observed minute-close changes since the session started.
        up = gain_sum
        down = loss_sum

    elif MODE == "wilder":
        if changes < RSI_PERIOD:
            return None

        if changes == RSI_PERIOD:
            average_gain = gain_sum / RSI_PERIOD
            average_loss = loss_sum / RSI_PERIOD
        else:
            average_gain = (
                average_gain * (RSI_PERIOD - 1) + gain
            ) / RSI_PERIOD
            average_loss = (
                average_loss * (RSI_PERIOD - 1) + loss
            ) / RSI_PERIOD

        up = average_gain
        down = average_loss

    else:
        raise ValueError(f"Unknown RSI mode: {MODE}")

    if up == 0.0 and down == 0.0:
        return 50.0  # Explicit convention for a flat series.
    if down == 0.0:
        return 100.0

    return 100.0 - 100.0 / (1.0 + up / down)


def on_message(ws, message):
    try:
        event = json.loads(message)
        logging.info("WebSocket got message: %s", message)
        if event.get("event") != "price":
            logging.info("WebSocket status: %s", event)
            return

        if event.get("symbol") != symbol:
            return

        timestamp = float(event["timestamp"])
        price = float(event["price"])
        tick_time = datetime.fromtimestamp(timestamp, MARKET_TZ)

        # Regular-session filtering for U.S. stocks.
        if tick_time.weekday() >= 5:
            return
        if not OPEN_TIME <= tick_time.time() < CLOSE_TIME:
            return

        minute = int(timestamp // 60)

        with lock:
            if session_date is not None and tick_time.date() < session_date:
                return

            if tick_time.date() != session_date:
                reset_session(tick_time.date())

            # Do not revise bars already used in the calculation.
            if minute <= last_finalized:
                return

            existing = buckets.get(minute)
            if existing is None or timestamp >= existing[0]:
                buckets[minute] = (timestamp, price)

    except (ValueError, TypeError, KeyError):
        logging.exception("Invalid WebSocket message")


def finalize_minutes():
    global last_finalized

    while not stop.wait(0.5):
        # Allow two seconds after the minute ends for arriving ticks.
        current_minute = int((time.time() - 2.0) // 60)

        with lock:
            completed = sorted(
                minute for minute in buckets
                if minute < current_minute
            )

            for minute in completed:
                _, close = buckets.pop(minute)
                last_finalized = minute
                value = calculate_rsi(close)

                bar_time = datetime.fromtimestamp(
                    minute * 60, MARKET_TZ
                ).isoformat()

                if value is None:
                    logging.info(
                        "%s minute=%s close=%.4f RSI=warming-up",
                        symbol, bar_time, close,
                    )
                else:
                    logging.info(
                        "%s minute=%s close=%.4f RSI[%s]=%.2f changes=%d",
                        symbol, bar_time, close, MODE, value, changes,
                    )


def on_open(ws):
    logging.info("Connected; subscribing to %s", symbol)
    ws.send(json.dumps({
        "action": "subscribe",
        "params": {"symbols": symbol},
    }))


def main():
    global MODE, RSI_PERIOD, symbol, MARKET_TZ, OPEN_TIME, CLOSE_TIME, lock, stop, buckets, session_date, last_finalized, previous_close, changes, gain_sum, loss_sum, average_gain, average_loss
    symbol = "AAPL"
    MODE = "since_open"  # "since_open" or "wilder"
    RSI_PERIOD = 14      # Used only with MODE="wilder"

    MARKET_TZ = ZoneInfo("America/New_York")
    global OPEN_TIME
    OPEN_TIME = clock_time(9, 30)
    global CLOSE_TIME
    CLOSE_TIME = clock_time(16, 0)

    lock = threading.Lock()
    stop = threading.Event()

    # Minute bucket -> most recent (tick timestamp, price).
    buckets = {}
    session_date = None
    last_finalized = -1

    previous_close = None
    changes = 0
    gain_sum = 0.0
    loss_sum = 0.0
    average_gain = None
    average_loss = None
    logging.info(f" Analyzing: {symbol} ")
    worker = threading.Thread(target=finalize_minutes, daemon=True)
    worker.start()

    try:
        while not stop.is_set():
            ws = websocket.WebSocketApp(
                "wss://ws.twelvedata.com/v1/quotes/price"
                f"?apikey={api_key}",
                on_open=on_open,
                on_message=on_message,
                on_error=lambda ws, error: logging.warning(
                    "WebSocket error: %s", error
                ),
                on_close=lambda ws, code, reason: logging.warning(
                    "WebSocket closed: %s %s", code, reason
                ),
            )

            ws.run_forever(ping_interval=20, ping_timeout=10)

            if not stop.is_set():
                logging.warning(
                    "Reconnecting in 5 seconds; missing prices "
                    "are not backfilled."
                )
                stop.wait(5)

    except KeyboardInterrupt:
        logging.info("Stopping")
    finally:
        stop.set()
        ws.close()
        worker.join(timeout=3)


if __name__ == "__main__":
    main()
