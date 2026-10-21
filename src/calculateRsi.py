from alpaca.data import DataFeed
import pytz
import pandas as pd
import sys
from datetime import datetime, timedelta
import datetime as dt
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from lib.utils import JsonIniParser
from pathlib import Path

try:
    # Initialize the parser with the file path
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))

except Exception as ce:
    print(f"? Failed to parse configuration file: {ce}")
    sys.exit(1)
try:
    client = config.mk_alpaca_historical_client()
except Exception as e:
    print(f"Critical Error loading Alpaca configuration or initializing client: {e}")
    sys.exit(2)        
print(f"?? Alpaca Historical Data Client initialized successfully.")


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculates RSI using Welles Wilder's smoothing technique."""
    deltas = prices.diff()
    gains = deltas.clip(lower=0)
    losses = -deltas.clip(upper=0)
    
    avg_gain = gains.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = losses.ewm(com=period - 1, min_periods=period).mean()
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def fetch_and_calculate_rsi(symbol: str):
    # FREE TIER FIX: Alpaca Free tier delays data by 15 minutes. 
    # Requesting data newer than 15 minutes ago triggers a 403 error.
    now_utc = dt.datetime.now(pytz.utc)
    end_allowed_utc = now_utc - dt.timedelta(minutes=16) 
    
    eastern = pytz.timezone('US/Eastern')
    now_est = end_allowed_utc.astimezone(eastern)
    
    # Establish market opening time for today
    market_open_est = eastern.localize(dt.datetime.combine(now_est.date(), dt.time(9, 30)))

    # --- HOURLY RSI (14-Period) ---
    hourly_start = market_open_est - dt.timedelta(days=15) 
    
    hourly_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Hour,
        start=hourly_start,
        end=end_allowed_utc,
        feed=DataFeed.IEX  # FREE TIER FIX: Explicitly request IEX data
    )
    hourly_bars = client.get_stock_bars(hourly_request).df.loc[symbol]
    hourly_bars['RSI_14'] = calculate_rsi(hourly_bars['close'], period=14)
    
    today_hourly_rsi = hourly_bars[hourly_bars.index >= market_open_est]['RSI_14']

    # --- DAILY RSI (14-Period) ---
    daily_start = end_allowed_utc - dt.timedelta(days=120)
    daily_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=daily_start,
        end=end_allowed_utc,
        feed=DataFeed.IEX  # FREE TIER FIX: Explicitly request IEX data
    )
    daily_bars = client.get_stock_bars(daily_request).df.loc[symbol]
    daily_bars['RSI_14'] = calculate_rsi(daily_bars['close'], period=14)
    recent_daily_rsi = daily_bars['RSI_14'].dropna().tail(14)

    # --- MONTHLY RSI (30-Period) ---
    monthly_start = end_allowed_utc - dt.timedelta(days=365 * 4)
    monthly_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Month,
        start=monthly_start,
        end=end_allowed_utc,
        feed=DataFeed.IEX  # FREE TIER FIX: Explicitly request IEX data
    )
    monthly_bars = client.get_stock_bars(monthly_request).df.loc[symbol]
    monthly_bars['RSI_30'] = calculate_rsi(monthly_bars['close'], period=30)
    recent_monthly_rsi = monthly_bars['RSI_30'].dropna().tail(30)

    # --- OUTPUT RESULTS ---
    print(f"=== {symbol} HOURLY 14-PERIOD RSI (Today's Market Session) ===")
    if today_hourly_rsi.empty:
        print("No hourly data generated yet for today's session or market hasn't opened.")
    else:
        print(today_hourly_rsi.to_string())
    print("\n" + "="*50 + "\n")
    
    print(f"=== {symbol} DAILY 14-PERIOD RSI (Last 14 Trading Days) ===")
    print(recent_daily_rsi.to_string())
    print("\n" + "="*50 + "\n")
    
    print(f"=== {symbol} MONTHLY 30-PERIOD RSI (Last 30 Trading Months) ===")
    print(recent_monthly_rsi.to_string())

if __name__ == "__main__":
    print("Paper Trading Mode: True")
    fetch_and_calculate_rsi("AAPL")
