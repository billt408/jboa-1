import pandas as pd
import pandas_ta as ta
import yfinance as yf

# 1. Define the ticker
ticker = "NVDA"

# 2. FIX: Specify an intraday interval (e.g., "1m" for 1-minute, "5m" for 5-minute bars)
# Note: yfinance only allows retrieving the last 7 days of 1-minute data, or 60 days of 5-minute data.
df = yf.download(ticker, period="7d", interval="1m")

# 3. Flatten multi-index columns if they exist
if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.droplevel(1)

# 4. Calculate 14-period RSI automatically
df["RSI"] = ta.rsi(df["Close"], length=14)

# 5. FIX: Clean up the timestamp index so Excel displays the minutes properly
df.index = df.index.tz_localize(None)  # Strips timezone so Excel doesn't scramble it
df.index.name = "Timestamp"            # Explicitly name the index column

# 6. Export to a clean Excel spreadsheet
output_file = f"{ticker}_rsi_intraday_data.xlsx"
df.to_excel(output_file)

print(f"Success! Saved intraday data with correct minutes to {output_file}")
