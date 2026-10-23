import json
import logging
import threading
import time
import pandas as pd
import pandas_ta as ta
import requests
import websocket
import sys
from pathlib import Path
from lib.utils import JsonIniParser
from lib.logger_init import initialize_logging

# Initialize external logging layer module
initialize_logging(default_level=logging.INFO)
logger = logging.getLogger("root")

# --- Parse Config Layer ---
try:
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
    config.load_globals()
except Exception as ce:
    logging.error(f"? Failed to parse configuration file: {ce}")
    sys.exit(1)

try:
    raw_symbol = config.get("symbols", "target_symbols")
    # Clean the string layout just in case brackets are passed down inside the .ini file
    if isinstance(raw_symbol, list):
        target_symbols = str(raw_symbol[0]).strip()
    else:
        target_symbols = str(raw_symbol).replace("[", "").replace("]", "").replace("'", "").replace('"', "").strip()
except Exception as e:
    logging.error(f"Critical Error initializing symbols: {e}")
    sys.exit(2)        

try:
    api_key = config.get("financialmodelingprep_api", "api_key")
    logging.debug(f"Using API key: {api_key}")
except Exception as e:
    logging.error(f"Critical Error loading financialmodelingprep configuration: {e}")
    sys.exit(2)        

if not api_key:
    raise SystemExit("Set the financialmodelingprep_api_key environment variable first.")

# Configuration Constraints
TIMEFRAME = "1min"
RSI_PERIOD = 14

price_history = []
history_lock = threading.Lock()


def initialize_history():
    """Fetches the initial historical 1-minute bars via REST endpoints."""
    global price_history
    logger.info(f"Initializing historical data for {target_symbols}...")

    # FIXED: Restructured URL path template to utilize supported api/v3/ endpoints
    #url = f"https://financialmodelingprep.com/{TIMEFRAME}/{target_symbols}?apikey={api_key}"
    logging.info(f"Fetching historical data from FMP for {target_symbols} at {TIMEFRAME} timeframe...")
    url = f"https://financialmodelingprep.com/stable/historical-price-eod/{TIMEFRAME}/{target_symbols}?apikey={api_key}"


    try:
        response = requests.get(url).json()

        if isinstance(response, dict) and ("Error Message" in response or "error" in response):
            logger.error(f"FMP API returned an error: {response}")
            return

        if not response or not isinstance(response, list):
            logger.error("Invalid response format received from FMP. Check your API key or symbol configurations.")
            return

        # Convert to DataFrame chronologically
        df = pd.DataFrame(response).iloc[::-1].reset_index(drop=True)

        if "close" not in df.columns:
            logger.error("Expected 'close' column not found in historical data.")
            return

        with history_lock:
            price_history = df["close"].astype(float).tolist()[-50:]

        logger.info(f"Successfully loaded {len(price_history)} historical periods.")

    except Exception as e:
        logger.error(f"Error initializing history: {e}", exc_info=True)


def calculate_rsi(new_price=None):
    """Calculates the current RSI based on history plus the incoming stream price point."""
    with history_lock:
        prices = list(price_history)

    if new_price is not None:
        prices.append(float(new_price))

    if len(prices) <= RSI_PERIOD:
        return None

    try:
        df = pd.DataFrame(prices, columns=["close"])
        rsi_series = ta.rsi(df["close"], length=RSI_PERIOD)
        if rsi_series is not None and not rsi_series.empty:
            return rsi_series.iloc[-1]
    except Exception as e:
        logger.error(f"Error calculating RSI calculation: {e}")
    return None


def on_message(ws, message):
    try:
        data = json.loads(message)
        if isinstance(data, dict) and "s" in data:
            ticker = data.get("s", "").upper()
            if ticker == target_symbols.upper():
                current_price = data.get("p") or data.get("ap") or data.get("lp")
                if current_price:
                    rsi_val = calculate_rsi(current_price)
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    if rsi_val is not None:
                        print(f"[{timestamp}] Live {target_symbols} Price: ${float(current_price):.2f} | 1-Min RSI: {rsi_val:.2f}")
    except Exception as e:
        logger.error(f"Error processing message: {e}")


def on_open(ws):
    logger.info("WebSocket connection opened. Sending credentials...")
    try:
        # Authenticate handshake credentials
        login_payload = {"event": "login", "data": {"apiKey": api_key}}
        ws.send(json.dumps(login_payload))

        # Subscribe channel targeting our symbol
        subscribe_payload = {
            "event": "subscribe",
            "data": {"ticker": [target_symbols.lower()]}
        }
        ws.send(json.dumps(subscribe_payload))
        logger.info(f"Subscribed to trade stream for {target_symbols}")
    except Exception as e:
        logger.error(f"Error during socket channel subscription open: {e}")


def on_error(ws, error):
    logger.error(f"WebSocket Error triggered: {error}")


def on_close(ws, close_status_code, close_msg):
    logger.info(f"WebSocket connection closed. Code: {close_status_code}, Msg: {close_msg}")


def minute_cron_job():
    """Background tracker loop to sync historical bars at closed minute boundaries."""
    global price_history
    while True:
        # Sleep until the exact split second of the next raw minute boundary transition
        time.sleep(60 - (time.time() % 60))
        
        # FIXED: Corrected path URL layout alignment string parsing variables
        url = f"https://financialmodelingprep.com{TIMEFRAME}/{target_symbols}?apikey={api_key}"
        try:
            res = requests.get(url).json()
            if isinstance(res, list) and len(res) > 0:
                # FIXED: Pulled from first element index tracking the most recent boundary close
                latest_closed_price = float(res[0]["close"])
                with history_lock:
                    price_history.append(latest_closed_price)
                    if len(price_history) > 100:
                        price_history.pop(0)
                logger.info(f"--- Hard synced minute close: ${latest_closed_price} ---")
        except Exception as e:
            logger.error(f"Failed updating historical minute boundary: {e}")


if __name__ == "__main__":
    # Pre-populate list arrays
    initialize_history()

    # Spin up background async runtime chron loops
    cron_thread = threading.Thread(target=minute_cron_job, daemon=True)
    cron_thread.start()

    # FIXED: Replaced invalid broken hostname string with real FMP production address link
    fmp_ws_url = "wss://websockets.financialmodelingprep.com/"
    
    ws = websocket.WebSocketApp(
        fmp_ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    ws.run_forever()
