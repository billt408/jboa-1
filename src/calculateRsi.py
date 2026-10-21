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
    
    # Welles Wilder smoothing via Exponential Moving Average
    avg_gain = gains.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = losses.ewm(com=period - 1, min_periods=period).mean()
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def fetch_and_calculate_rsi(symbol: str):
    now_utc = datetime.now(pytz.utc)
    
    # Define US/Eastern time zone for market hours tracking
    eastern = pytz.timezone('US/Eastern')
    now_est = now_utc.astimezone(eastern)
    
    # Establish today's market opening threshold (9:30 AM EST)
    #market_open_est = eastern.localize(datetime.combine(now_est.date(), datetime.time(9, 30)))
    market_open_est = eastern.localize(dt.datetime.combine(now_est.date(), dt.time(9, 30)))
    # --- HOURLY RSI (14-Period) ---
    # Note: To compute a valid 14-period RSI from today's open, 
    # we must fetch historical hours prior to today to seed the initial averages.
    hourly_start = market_open_est - timedelta(days=10) 
    
    hourly_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Hour,
        start=hourly_start,
        end=now_utc
    )
    hourly_bars = client.get_stock_bars(hourly_request).df.loc[symbol]
    hourly_bars['RSI_14'] = calculate_rsi(hourly_bars['close'], period=14)
    
    # Filter the final data to only show frames matching today's session from open onward
    today_hourly_rsi = hourly_bars[hourly_bars.index >= market_open_est]['RSI_14']

    # --- DAILY RSI (14-Period) ---
    daily_start = now_utc - timedelta(days=120)
    daily_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=daily_start,
        end=now_utc
    )
    daily_bars = client.get_stock_bars(daily_request).df.loc[symbol]
    daily_bars['RSI_14'] = calculate_rsi(daily_bars['close'], period=14)
    recent_daily_rsi = daily_bars['RSI_14'].dropna().tail(14)

    # --- MONTHLY RSI (30-Period) ---
    monthly_start = now_utc - timedelta(days=365 * 4)
    monthly_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Month,
        start=monthly_start,
        end=now_utc
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
    fetch_and_calculate_rsi("AAPL")
