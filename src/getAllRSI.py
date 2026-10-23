import time
from lib.utils import JsonIniParser
import sys
from pathlib import Path
from lib.getStockRSI import daily_monthly_rsi, make_twelvedata_client

try:
    # Initialize the parser with the file path
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
    config.load()
except Exception as ce:
    print(f"? Failed to parse configuration file: {ce}")
    sys.exit(1)
try:
    target_symbols = config.get("symbols", "target_symbols")
except Exception as e:
    print(f"Critical Error initializing symbols: {e}")
    sys.exit(2)        

    try:
        api_key = config.get("twelvedata_api", "twelvedata_api_key")
    except Exception as e:
        print(f"Critical Error loading Alpaca configuration or initializing twelvedata api: {e}")
        sys.exit(2)        
    if not api_key:
        raise SystemExit("Set the TWELVEDATA_API_KEY environment variable first.")

    client = make_twelvedata_client(api_key)
    
    print("?" * 40) 
    try:
        for symbol in target_symbols:
            print(f" Getting RSI for: {ticker} ")
            try:
                result = client.daily_monthly_rsi(symbol)
                print(json.dumps(result, indent=2))
                print("?" * 40) 
            except (requests.RequestException, RuntimeError) as exc:
                print(f"{symbol}: {exc}")
    finally:
        client.close()
