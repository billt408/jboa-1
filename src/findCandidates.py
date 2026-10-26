
import sys
import json
import logging
import inspect
import requests
from lib.logger_init import initialize_logging
initialize_logging(default_level=logging.INFO) # Pass logging.DEBUG here if you want full app debug mode
from pathlib import Path
from lib.getStockRSI import mk_twelvedata_client
from lib.apGetStockRSI import mk_alpaca_client
from lib.utils import JsonIniParser
from lib.getOptionsContracts import get_pruned_option_chain

def display_results(item, platform, display_now):
    # Pythonic boolean check
    if not display_now:
        return
        
    logging.debug(json.dumps(item, indent=2))
    
    symbol = item.get("symbol", "UNKNOWN")
    
    logging.info(
        "(%s-%s) %s-%s - RSI for %s periods is %.2f",
        item["interval"], 
        platform,
        item["latest"]["datetime"],
        symbol, 
        item["period"], 
        item["latest"].get("rsi", 0.0)
    )
    
    for row in item["history"]:
        logging.debug("%s %s RSI=%.6f", symbol, row["datetime"], row["rsi"])

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

try:
    credentials = config.CREDENTIALS
    if not credentials or "api_key" not in credentials or "secret_key" not in credentials:
        print("? Error: Missing credentials dictionary in your config.json.ini file.")
        sys.exit(1)
    alpaca_api_key = config.CREDENTIALS["api_key"]
    alpaca_secret_key = config.CREDENTIALS["secret_key"]
    logging.debug(f"Using API key: {alpaca_api_key}")
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
    alpaca_client = mk_alpaca_client(api_key=alpaca_api_key, secret_key=alpaca_secret_key)
except Exception as e:
    print(f"Critical Error loading alpaca configuration or initializing client: {e}")
    sys.exit(2)

try:
    for symbol in target_symbols:
        logging.debug(f" Analyzing: {symbol} ")
        try:
            #gets the daily RSI value for the last 30 sessions (days) and returns a dictionary with the latest RSI value and other details
            #twelvedata = twelvedata_client.daily_rsi(symbol, period=4, sessions=30, interval="1min")
            twelvedata_daily = twelvedata_client.daily_rsi(symbol, period=14, sessions=30, interval="1min")
            twelvedata_daily_rsi = twelvedata_daily["latest"]["rsi"]
            display_results(twelvedata_daily, "twelvedata", True)

            alpaca_daily = alpaca_client.daily_rsi(symbol, period=14, sessions=30, interval="1min")
            alpaca_daily_rsi = twelvedata_daily["latest"]["rsi"]
            display_results(alpaca_daily, "alpaca", True)

#           alpaca_history = alpaca_client.rsi(symbol, period=14, outputsize=5, interval="1min",
#               date="2026-10-23T15:59:00-04:00",
#           )
#           for row in alpaca_history:
#               logging.info("%s %s RSI=%.6f", symbol, row["datetime"], row["rsi"])
#           alpaca_latest = alpaca_history[-1]
#           logging.info("alpaca_latest=%s, old_alpaca_history_daily_rsi=%s", alpaca_latest, old_alpaca_daily_rsi)
#            display_results(alpaca_history, "alpaca", True)
            
            twelvedata_monthly = twelvedata_client.daily_rsi(symbol, period=14, sessions=30, interval="1day")
            twelvedata_monthly_rsi = twelvedata_monthly["latest"]["rsi"]
            display_results(twelvedata_monthly, "twelvedata", True)

            alpaca_monthly = alpaca_client.daily_rsi(symbol, period=14, sessions=30, interval="1day")
            alpaca_monthly_rsi = alpaca_monthly["latest"]["rsi"]
            display_results(alpaca_monthly, "alpaca",  True)

            #history = twelvedata_client.rsi(symbol=symbol, interval="1min", period=14, outputsize=5000, date="today") 
            #latest = history[-1]
            #logging.info("Latest RSI(14), 1-minute, for %s on %s is %.2f", symbol, latest["datetime"], latest["rsi"])           

            logging.debug("?" * 40)
            
            if twelvedata_monthly_rsi <= 35:
                logging.info(
                    "%s-%s - RSI, interval %s for %s days daily is %.2f and monthly is %.2f (BUY CANDIDATE)",
                    twelvedata_monthly["latest"]["datetime"],
                    symbol,
                    twelvedata_monthly["interval"],
                    twelvedata_monthly["period"],
                    twelvedata_monthly_rsi,
                    alpaca_monthly_rsi,
                )
                try:
                    contract, clean_payload = get_pruned_option_chain(symbol, config.ALPACA_KEY, config.ALPACA_SECRET, alpaca_client.client)
                    logging.debug(f"Pruned Option Chain Payload for {symbol}:")
                    logging.debug(clean_payload)
                    logging.info(f"  This is the second OTM contract, stopping further logging.")
                    logging.info(f"Option Symbol: {contract['contract']} | Strike: {contract['strike']} | Bid: {contract['bid']} | Ask: {contract['ask']}")

                except Exception as e:
                    logging.error(f"Error processing symbol {symbol}: {e}")

            elif twelvedata_daily_rsi >= 65:
                logging.info(
                    "%s-%s - RSI, interval %s for %s days is %.2f (SELL CANDIDATE)",
                    twelvedata_daily["latest"]["datetime"],
                    symbol,
                    twelvedata_daily["interval"],
                    twelvedata_daily["period"],
                    twelvedata_daily_rsi,
                    alpaca_daily_rsi
                ) 
        except (requests.RequestException, RuntimeError) as exc:
            logging.error(f"{symbol}: {exc}")
finally:
    twelvedata_client.close()
