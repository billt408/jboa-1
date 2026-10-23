import time
from lib.indicators import run_rsi_analysis
from lib.utils import JsonIniParser
import sys
from pathlib import Path

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
    print(f"Critical Error loading Alpaca configuration or initializing symbols: {e}")
    sys.exit(2)        

print(f"?? Starting batch RSI analysis for {len(target_symbols)} symbols...\n")

for symbol in target_symbols:
    # Normalize ticker to uppercase for the Alpaca API
    ticker = symbol.upper()
    
    # Visual Header Separator
    print("?" * 40)
    print(f" PROCESSING DATA FOR TICKER: {ticker} ")
    print("?" * 40)
    
    try:
        # Call the wrapped library function
        # verbose=True automatically prints out the hourly, daily, and monthly tables
        hourly, daily, monthly = run_rsi_analysis(ticker, verbose=True)
        
    except Exception as e:
        print(f"? Error processing {ticker}: {e}")
    
    # Clean spacing trailing separator
    print("\n" + "??" * 40 + "\n")
    
    # Optional short delay to avoid overwhelming free tier IEX data feeds
    time.sleep(0.5)

print("? All stock analyses completed successfully.")
