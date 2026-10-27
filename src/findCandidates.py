import sys
import json
import logging
import inspect
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from lib.logger_init import initialize_logging
initialize_logging(default_level=logging.INFO) # Pass logging.DEBUG here if you want full app debug mode
from pathlib import Path
from lib.getStockRSI import mk_twelvedata_client
from lib.apGetStockRSI import mk_alpaca_client
from lib.utils import JsonIniParser
from lib.getOptionsContracts import get_pruned_option_chain
#from lib.utils import get_aligned_rsi
from lib.utils import debug_rsi_inputs

def display_results(item, platform, timeframe, display_now):
    if not display_now:
        return
        
    logging.debug(json.dumps(item, indent=2))
    symbol = item.get("symbol", "UNKNOWN")
    
    logging.info(
        "(%s-%s) %s-%s - RSI with interval %s and %s periods is %.2f",
        platform,
        timeframe,
        item["latest"]["datetime"],
        symbol, 
        item["interval"], 
        item["period"], 
        item["latest"].get("rsi", 0.0)
    )
    
    for row in item["history"]:
        logging.debug("%s %s RSI=%.6f", symbol, row["datetime"], row["rsi"])


def display_static_results(history_list, platform, timeframe, display_now):
    if not display_now or not history_list:
        return
        
    # Grab the most recent data point (assumed to be the last item)
    latest_item = history_list[-1]
    symbol = latest_item.get("symbol", "UNKNOWN")
    
    # Safely look for configuration fallback details if missing from individual rows
    interval = latest_item.get("interval", "1min")
    period = latest_item.get("period", "14")
    
    logging.info(
        "(%s-%s) %s-%s - RSI with interval %s and %s periods is %.2f",
        platform,
        timeframe,
        latest_item.get("datetime", "UNKNOWN"),
        symbol,
        interval,
        period,
        latest_item.get("rsi", 0.0)
    )
    
    for row in history_list:
        logging.debug("%s %s RSI=%.6f", symbol, row.get("datetime"), row.get("rsi", 0.0))

try:
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

logging.debug("DEBUG SIGNATURE: %s", inspect.signature(mk_twelvedata_client))

try:
    twelvedata_client = mk_twelvedata_client(api_key)
except Exception as e:
    print(f"Critical Error loading twelvedata configuration or initializing client: {e}")
    sys.exit(2)
try:
    alpaca_client = mk_alpaca_client(api_key=alpaca_api_key, secret_key=alpaca_secret_key, feed="sip",adjustment="split")
    logging.info("Alpaca data configuration: feed=%s, adjustment=%s",alpaca_client.feed.value,alpaca_client.adjustment.value)
except Exception as e:
    print(f"Critical Error loading alpaca configuration or initializing client: {e}")
    sys.exit(2)
NY = ZoneInfo("America/New_York")

# Shared cutoff, safely outside the restricted recent SIP window.
# Round down to a minute so both requests use the same minute boundary.
shared_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=16)).replace(second=0, microsecond=0)
# Twelve Data: local New York timestamp plus explicit timezone
# supplied by the modified rsi() method.
td_end_date = shared_cutoff.astimezone(NY).strftime("%Y-%m-%d %H:%M:%S")

# Alpaca: timezone-aware datetime.
alpaca_end_date = shared_cutoff

logging.info("Shared RSI cutoff: %s", shared_cutoff.astimezone(NY).isoformat())

try:
    for symbol in target_symbols:
        logging.debug(f" Analyzing: {symbol} ")
        try:
#            twelvedata_daily = twelvedata_client.daily_rsi(symbol, period=14, sessions=30, interval="1min")
#            twelvedata_daily_rsi = twelvedata_daily["latest"]["rsi"]
#            display_results(twelvedata_daily, "twelvedata", "1day", True)

#            alpaca_daily = alpaca_client.daily_rsi(symbol, period=14, sessions=30, interval="1min")
#            alpaca_daily_rsi = alpaca_daily["latest"]["rsi"] 
#            display_results(alpaca_daily, "alpaca", "1day", True)

#            alpaca_daily_static = alpaca_client.rsi(symbol, period=14, outputsize=5, interval="1min",
#               date="2026-10-23T15:59:00-04:00",
#            )
#            alpaca_daily_static_rsi = alpaca_daily_static[-1].get("rsi", 0.0)
#            display_static_results(alpaca_daily_static, "alpaca_static", "1day", True)

#            td_history = twelvedata_client.rsi(symbol=symbol,period=14,outputsize=30,interval="1min",end_date=td_end_date)
#            alpaca_history = alpaca_client.rsi(symbol=symbol,period=14,outputsize=30,interval="1min",date=alpaca_end_date)

#            td_latest = td_history[-1]
#            alpaca_latest = alpaca_history[-1]
#            logging.info("twelvedata %s-%s RSI(14), 1min = %.6f",td_latest["datetime"],symbol,td_latest["rsi"])
#            logging.info("alpaca %s-%s RSI(14), 1min = %.6f",alpaca_latest["datetime"],symbol,alpaca_latest["rsi"])

#            try:
#                aligned = get_aligned_rsi(twelvedata_client=twelvedata_client,alpaca_client=alpaca_client,symbol=symbol,cutoff=shared_cutoff,period=14,observations=60)
#            except RuntimeError as exc:
#                logging.error("%s: skipping RSI comparison: %s",symbol,exc,)
#                continue

#            twelvedata_rsi_value = aligned["twelvedata_rsi"]
#            alpaca_rsi_value = aligned["alpaca_rsi"]
#            rsi_timestamp = aligned["datetime"]

#            logging.info("%s-%s RSI(%d), %s: Twelve Data=%.6f / Alpaca=%.6f",rsi_timestamp,symbol,aligned["period"],aligned["interval"],twelvedata_rsi_value,alpaca_rsi_value)

#            if td_latest["datetime"] != alpaca_latest["datetime"]:
#                logging.warning("%s: final candle mismatch: Twelve Data=%s, Alpaca=%s",symbol,td_latest["datetime"],alpaca_latest["datetime"])
##            aligned = get_aligned_rsi(twelvedata_client=twelvedata_client,alpaca_client=alpaca_client,symbol=symbol,cutoff=shared_cutoff,period=14,observations=60)
##
##            # These are now the only one-minute values used below.
##            twelvedata_daily_rsi = aligned["twelvedata_rsi"]
##            alpaca_daily_rsi = aligned["alpaca_rsi"]
##            rsi_timestamp = aligned["datetime"]
##
##            logging.info("%s-%s RSI(%d), %s: ""Twelve Data=%.6f / Alpaca=%.6f / difference=%+.6f",rsi_timestamp,symbol,aligned["period"],
##                aligned["interval"],twelvedata_daily_rsi,alpaca_daily_rsi,alpaca_daily_rsi - twelvedata_daily_rsi,)
            try:
                debug_rsi_inputs(
                    twelvedata_client=twelvedata_client,
                    alpaca_client=alpaca_client,
                    symbol="NVDA",
                    cutoff=shared_cutoff,
                    period=14,
                    bars_requested=1500,
                )
            finally:
                twelvedata_client.close()
                alpaca_client.close()

            sys.exit(0)

            twelvedata_monthly = twelvedata_client.daily_rsi(symbol, period=14, sessions=30, interval="1day")
            twelvedata_monthly_rsi = twelvedata_monthly["latest"]["rsi"]
#            display_results(twelvedata_monthly, "twelvedata", "monthly", True)

            alpaca_monthly = alpaca_client.daily_rsi(symbol, period=14, sessions=30, interval="1day")
            alpaca_monthly_rsi = alpaca_monthly["latest"]["rsi"]
#            display_results(alpaca_monthly, "alpaca",  "monthly", True)

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
                    "%s-%s - RSI, interval %s for %s days is twelvedata: %.2f / alpaca: %.2f (SELL CANDIDATE)",
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
