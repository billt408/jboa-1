import json
import urllib.request
from pathlib import Path
import sys
from datetime import datetime, timedelta
from lib.parms import JsonIniParser


def get_5day_hold_candidates(underlying_ticker, api_key, api_secret):
    """
    Pulls live data and isolates contracts optimal for a 5-day swing hold.
    """
    # Using the real-time snapshot endpoint provided by Alpaca's Options Data API
    #url = f"https://alpaca.markets{underlying_ticker}?feed=indicative"
    url = f"https://data.alpaca.markets/v1beta1/options/snapshots/{underlying_ticker}?feed=indicative"
    
    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", api_key)
    req.add_header("APCA-API-SECRET-KEY", api_secret)
    req.add_header("accept", "application/json")
    
    try:
        with urllib.request.urlopen(req) as response:
            raw_data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"\n? Network Connection Error: {str(e)}")
        return None

    snapshots = raw_data.get("snapshots", {})
    if not snapshots:
        print("\n?? Connection successful, but Alpaca returned an empty snapshot matrix.")
        return None

    today = datetime.now()
    # Configure Target Expiration Window (7 to 15 days from today)
    min_expiry = today + timedelta(days=7)
    max_expiry = today + timedelta(days=15)
    
    print(f"??? Scanning for expirations between: {min_expiry.strftime('%Y-%m-%d')} and {max_expiry.strftime('%Y-%m-%d')}")
    
    candidates = []
    
    for contract_symbol, details in snapshots.items():
        # 1. Parse out the embedded expiration date string
        try:
            expiry_str = contract_symbol[4:10]
            expiry_date = datetime.strptime(expiry_str, "%y%m%d")
        except (ValueError, IndexError):
            continue
            
        # Enforce our DTE (Days To Expiration) safety window
        if not (min_expiry <= expiry_date <= max_expiry):
            continue

        # 2. Extract pricing metrics and structural Greeks
        greeks = details.get("greeks", {})
        latest_quote = details.get("latestQuote", {})
        
        bid = latest_quote.get("bp", 0)
        ask = latest_quote.get("ap", 0)
        delta = greeks.get("delta")
        theta = greeks.get("theta")
        
        # Skip incomplete data feeds to prevent local LLM logic errors
        if not bid or not ask or delta is None or theta is None:
            continue
            
        spread = ask - bid
        # Enforce tight entry/exit transaction cost metric
        if spread > 0.35: 
            continue
            
        # Target liquid near-the-money options (Delta between 0.30 and 0.50)
        if 0.30 <= abs(float(delta)) <= 0.50:
            candidates.append({
                "contract": contract_symbol,
                "expiration": expiry_date.strftime("%Y-%m-%d"),
                "type": "CALL" if float(delta) > 0 else "PUT",
                "delta": round(float(delta), 2),
                "theta": round(float(theta), 4),
                "bid": bid,
                "ask": ask,
                "spread": round(spread, 2),
                "implied_vol": round(float(details.get("impliedVolatility", 0)), 3)
            })

    # Sort candidates by narrowest bid/ask spread and return the top 5
    candidates = sorted(candidates, key=lambda x: x['spread'])[:5]
    return candidates

if __name__ == "__main__":
    try:
        # Initialize the parser with the file path
        config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))

        # Load the configuration variables into the instance
        config.load_globals()
    except Exception as ce:
        print(f"? Failed to parse configuration file: {ce}")
        sys.exit(1)

    environment = config.ENVIRONMENT
    credentials = config.CREDENTIALS
    nuc_ip = config.NUC_IP

    if not credentials or "api_key" not in credentials or "secret_key" not in credentials:
        print("? Error: Missing credentials dictionary in your config.json.ini file.")
        sys.exit(1)

    
    print("==================================================")
    print("   LAUNCHING LIVE OPTIONS DRY-RUN PIPELINE        ")
    print("==================================================")
    
    # Fetching data for SPY (S&P 500 ETF)
    short_list = get_5day_hold_candidates("SPY", config.ALPACA_KEY, config.ALPACA_SECRET)
    
    print("\n==================================================")
    print("          FINAL PRUNED DATASET FOR LLM            ")
    print("==================================================")
    if short_list:
        print(json.dumps(short_list, indent=2))
        print(f"\n? Dry-run completed. Extracted {len(short_list)} ideal swing-trade contracts.")
    else:
        print("? No contracts met the structural parameters (DTE, Spread, and Delta limits) right now.")
