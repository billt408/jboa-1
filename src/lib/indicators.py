import datetime as dt
import sys
from pathlib import Path
from typing import Tuple

import pandas as pd
import pytz
from alpaca.data import DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# Import your parser from the sibling utils module
from lib.utils import JsonIniParser


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


def format_with_slope(rsi_series: pd.Series) -> pd.DataFrame:
    """Calculates the slope (change) and formats the output with visual direction cues."""
    slope = rsi_series.diff()
    df_output = pd.DataFrame(
        {"RSI": rsi_series.round(2), "Slope": slope.round(3)}
    )

    # Generate human-readable string indicators based on the mathematical slope
    direction = []
    for val in slope:
        if pd.isna(val):
            direction.append("- N/A")
        elif val > 0:
            direction.append(f"? UP (+{round(val, 2)})")
        elif val < 0:
            direction.append(f"? DOWN ({round(val, 2)})")
        else:
            direction.append("- FLAT (0.0)")
    df_output["Direction"] = direction
    return df_output


def run_rsi_analysis(
    symbol: str, verbose: bool = True
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetches stock historical data from Alpaca, calculates RSI and slope

    intervals across Hourly, Daily, and Monthly metrics, and returns the DataFrames.
    """
    # 1. DYNAMIC CONFIGURATION & CLIENT INITIALIZATION
    # Resolves paths safely regardless of whether script runs standalone or as an imported module
    config_path = str(Path(__file__).resolve().parent / "parms.ini")
    try:
        config = JsonIniParser(config_path)
        client = config.mk_alpaca_historical_client()
        if verbose:
            print("? Alpaca Historical Data Client initialized successfully.")
    except Exception as ce:
        print(f"? Failed to initialize Alpaca Client via config: {ce}")
        sys.exit(1)

    # 2. TIME RANGE CONFIGURATION (Free tier accommodation)
    now_utc = dt.datetime.now(pytz.utc)
    end_allowed_utc = now_utc - dt.timedelta(minutes=16)
    eastern = pytz.timezone("US/Eastern")
    now_est = end_allowed_utc.astimezone(eastern)
    market_open_est = eastern.localize(
        dt.datetime.combine(now_est.date(), dt.time(9, 30))
    )

    # --- HOURLY RSI & SLOPE ---
    hourly_start = market_open_est - dt.timedelta(days=15)
    hourly_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Hour,
        start=hourly_start,
        end=end_allowed_utc,
        feed=DataFeed.IEX,
    )
    hourly_bars = client.get_stock_bars(hourly_request).df.loc[symbol]
    hourly_bars["RSI_14"] = calculate_rsi(hourly_bars["close"], period=14)
    hourly_formatted = format_with_slope(hourly_bars["RSI_14"])
    today_hourly_output = hourly_formatted[
        hourly_formatted.index >= market_open_est
    ]

    # --- DAILY RSI & SLOPE ---
    daily_start = end_allowed_utc - dt.timedelta(days=120)
    daily_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=daily_start,
        end=end_allowed_utc,
        feed=DataFeed.IEX,
    )
    daily_bars = client.get_stock_bars(daily_request).df.loc[symbol]
    daily_bars["RSI_14"] = calculate_rsi(daily_bars["close"], period=14)
    daily_formatted = format_with_slope(daily_bars["RSI_14"])
    recent_daily_output = daily_formatted.dropna(subset=["RSI"]).tail(14)

    # --- MONTHLY RSI & SLOPE ---
    monthly_start = end_allowed_utc - dt.timedelta(days=365 * 4)
    monthly_request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Month,
        start=monthly_start,
        end=end_allowed_utc,
        feed=DataFeed.IEX,
    )
    monthly_bars = client.get_stock_bars(monthly_request).df.loc[symbol]
    monthly_bars["RSI_30"] = calculate_rsi(monthly_bars["close"], period=30)
    monthly_formatted = format_with_slope(monthly_bars["RSI_30"])
    recent_monthly_output = monthly_formatted.dropna(subset=["RSI"]).tail(30)

    # 3. OUTPUT PRINTS (If verbose logging is explicitly enabled)
    if verbose:
        print(
            f"\n=== {symbol} HOURLY 14-PERIOD RSI & SLOPE (Today's Market Session) ==="
        )
        if today_hourly_output.empty:
            print(
                "No hourly data generated yet for today's session or market hasn't opened."
            )
        else:
            print(today_hourly_output[["RSI", "Direction"]].to_string())

        print("\n" + "=" * 60 + "\n")
        print(
            f"=== {symbol} DAILY 14-PERIOD RSI & SLOPE (Last 14 Trading Days) ==="
        )
        print(recent_daily_output[["RSI", "Direction"]].to_string())

        print("\n" + "=" * 60 + "\n")
        print(
            f"=== {symbol} MONTHLY 30-PERIOD RSI & SLOPE (Last 30 Trading Months) ==="
        )
        print(recent_monthly_output[["RSI", "Direction"]].to_string())

    return today_hourly_output, recent_daily_output, recent_monthly_output


if __name__ == "__main__":
    # Standard script direct-execution test case
    run_rsi_analysis("AAPL", verbose=True)
