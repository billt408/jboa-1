
import sys
import json
import logging
import inspect
import requests
from lib.logger_init import initialize_logging
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
    logging.error(f"Critical Error loading Alpaca configuration or initializing twelvedata api: {e}")
    sys.exit(2)        
if not api_key:
    raise SystemExit("Set the TWELVEDATA_API_KEY environment variable first.")

logging.debug("DEBUG SIGNATURE: %s", inspect.signature(mk_twelvedata_client))
client = mk_twelvedata_client(api_key)

try:
    for symbol in target_symbols:
        logging.info(f" Analyzing: {symbol} ")
        try:
            #gets the daily RSI value for the last 30 sessions (days) and returns a dictionary with the latest RSI value and other details
            result = client.daily_rsi(symbol, period=4, sessions=1, interval="1month")
            logging.debug(json.dumps(result, indent=2))
            logging.info(
                "%s-%s - RSI, interval %s for %s periods is %.2f",
                result["latest"]["datetime"],
                symbol,
                result["interval"],
                result["period"],
                result["latest"]["rsi"],
            )
            result = client.daily_rsi(symbol, period=5, sessions=1, interval="1min")
            logging.debug(json.dumps(result, indent=2))
            logging.info(
                "%s-%s - RSI, interval %s for %s periods is %.2f",
                result["latest"]["datetime"],
                symbol,
                result["interval"],
                result["period"],
                result["latest"]["rsi"],
            )

            logging.debug("?" * 40)
            rsi_value = result["latest"]["rsi"]
            if rsi_value < 30:
                logging.info(
                    "%s-%s - RSI, interval %s for %s days is %.2f (BUY CANDIDATE)",
                    result["latest"]["datetime"],
                    symbol,
                    result["interval"],
                    result["period"],
                    rsi_value,
                )
            elif rsi_value > 70:
                logging.info(
                    "%s-%s - RSI, interval %s for %s days is %.2f (SELL CANDIDATE)",
                    result["latest"]["datetime"],
                    symbol,
                    result["interval"],
                    result["period"],
                    rsi_value,
                ) 
        except (requests.RequestException, RuntimeError) as exc:
            logging.error(f"{symbol}: {exc}")
finally:
    client.close()
