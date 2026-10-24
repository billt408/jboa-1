
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
        logging.debug(f" Analyzing: {symbol} ")
        try:
            result = client.daily_monthly_rsi(symbol)
            logging.debug(json.dumps(result, indent=2))
            logging.info(
                "%s-%s - RSI, interval %s for %s days is %.2f",
                result["latest"]["datetime"],
                symbol,
                result["interval"],
                result["period"],
                result["latest"]["rsi"],
            )
            logging.debug("?" * 40) 
        except (requests.RequestException, RuntimeError) as exc:
            logging.error(f"{symbol}: {exc}")
finally:
    client.close()
