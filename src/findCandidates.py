
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
from lib.getOptionsContracts import get_pruned_option_chain

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
try:
    twelvedata_client = mk_twelvedata_client(api_key)
except Exception as e:
    print(f"Critical Error loading twelvedata configuration or initializing client: {e}")
    sys.exit(2)

try:
    trading_client = config.mk_alpaca_trading_client()
except Exception as e:
    print(f"Critical Error loading Alpaca configuration or initializing client: {e}")
    sys.exit(3)

try:
    for symbol in target_symbols:
        logging.info(f" Analyzing: {symbol} ")
        try:
            #gets the daily RSI value for the last 30 sessions (days) and returns a dictionary with the latest RSI value and other details
            #result = twelvedata_client.daily_rsi(symbol, period=4, sessions=30, interval="1min")
            result = twelvedata_client.daily_rsi(symbol, period=14, sessions=30, interval="1min")
            daily_rsi_value = result["latest"]["rsi"]
            logging.debug(json.dumps(result, indent=2))
            logging.debug("1 day")
            logging.debug(
                "%s-%s - RSI, interval %s for %s periods is %.2f",
                result["latest"]["datetime"],
                symbol,
                result["interval"],
                result["period"],
                result["latest"]["rsi"],
            )
            result = twelvedata_client.daily_rsi(symbol, period=14, sessions=30, interval="1day")
            monthly_rsi_value = result["latest"]["rsi"]
            logging.debug(json.dumps(result, indent=2))
            logging.debug("1 month")
            logging.debug(
                "%s-%s - RSI, interval %s for %s periods is %.2f",
                result["latest"]["datetime"],
                symbol,
                result["interval"],
                result["period"],
                result["latest"]["rsi"],
            )

            #history = twelvedata_client.rsi(symbol=symbol, interval="1min", period=14, outputsize=5000, date="today") 
            #latest = history[-1]
            #logging.info("Latest RSI(14), 1-minute, for %s on %s is %.2f", symbol, latest["datetime"], latest["rsi"])           

            logging.debug("?" * 40)
            
            if monthly_rsi_value <= 35:
                logging.info(
                    "%s-%s - RSI, interval %s for %s days daily is %.2f and monthly is %.2f (BUY CANDIDATE)",
                    result["latest"]["datetime"],
                    symbol,
                    result["interval"],
                    result["period"],
                    daily_rsi_value,
                    monthly_rsi_value,
                )
                try:
                    contract, clean_payload = get_pruned_option_chain(symbol, config.ALPACA_KEY, config.ALPACA_SECRET, twelvedata_client)
                    logging.debug(f"Pruned Option Chain Payload for {symbol}:")
                    logging.debug(clean_payload)
                    logging.info(f"  This is the second OTM contract, stopping further logging.")
                    logging.info(f"Option Symbol: {contract['contract']} | Strike: {contract['strike']} | Bid: {contract['bid']} | Ask: {contract['ask']}")

                except Exception as e:
                    logging.error(f"Error processing symbol {symbol}: {e}")

            elif daily_rsi_value >= 65:
                logging.info(
                    "%s-%s - RSI, interval %s for %s days is %.2f (SELL CANDIDATE)",
                    result["latest"]["datetime"],
                    symbol,
                    result["interval"],
                    result["period"],
                    daily_rsi_value,
                ) 
        except (requests.RequestException, RuntimeError) as exc:
            logging.error(f"{symbol}: {exc}")
finally:
    twelvedata_client.close()
