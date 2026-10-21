#from lib.indicators import run_rsi_analysis

# This executes the calculation, outputs the charts to terminal, and unpacks the raw underlying dataframes
#hourly, daily, monthly = run_rsi_analysis("MSFT", verbose=True)

# You can now cleanly inspect values programmatically in downstream services:
# current_daily_rsi = daily['RSI'].iloc[-1]


import time
from lib.indicators import run_rsi_analysis

# Your target tickers
target_symbols = ["nvda", "mu", "crwv", "amzn", "amd", "tsla", "klac", "dell", "intc", "spcx", "AAPL", "MSFT", "GOOG"]

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
