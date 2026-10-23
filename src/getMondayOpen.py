from datetime import datetime
import pytz
import logging
import sys
from lib.logger_init import initialize_logging
initialize_logging(default_level=logging.INFO) # Pass logging.DEBUG here if you want full app debug mode

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from pathlib import Path

# ==============================================================================
# ENVIRONMENT PATH PATCH
# Guarantees that 'src' is always in the path so 'from lib.utils' works seamlessly
# regardless of whether this script is called directly or imported.
# ==============================================================================
src_path = str(Path(__file__).resolve().parent.parent)
if src_path not in sys.path:
    sys.path.insert(0, src_path)

# Import your parser from the sibling utils module
from lib.utils import JsonIniParser
try:
    # Initialize the parser with the file path
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
    config.load()
    config.load_globals()
except Exception as ce:
    logging.error(f"? Failed to parse configuration file: {ce}")
    sys.exit(1)

def get_monday_premarket_data(client):
    # 2. Set up the timezone structures explicitly to handle EST vs UTC
    est_tz = pytz.timezone("America/New_York")  # Standard Eastern Time Zone
    now_est = datetime.now(est_tz)
    
    # Define your specific Monday target time window
    # This creates a data window from 4:00 AM EST to 4:00 PM EST
    #start_time = datetime(now_est.year, now_est.month, now_est.day, 4, 0, 0, tzinfo=est_tz)
    #end_time = datetime(now_est.year, now_est.month, now_est.day, 5, 0, 0, tzinfo=est_tz)
    start_time = datetime(2026, 10, 20, 8, 0, 0, tzinfo=est_tz)
    end_time = datetime(2026, 10, 20, 16, 0, 0, tzinfo=est_tz)
    logging.info(f"Querying Alpaca for pre-market data between:")
    logging.info(f"Start: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    logging.info(f"End:   {end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    
    try:
        # 4. Formulate the explicit bar request
        # Note: If you are on a free tier paper account, use feed=DataFeed.IEX
        # Paid accounts should change this parameter to DataFeed.SIP
        request_params = StockBarsRequest(
            symbol_or_symbols=["QQQ", "SPY"],
            timeframe=TimeFrame.Minute,
            start=start_time,
            end=end_time,
            feed=DataFeed.IEX
        )
        
        # 5. Fetch the historical bars array
        bars_data = client.get_stock_bars(request_params)
        logging.info(f"?? Successfully retrieved historical bar data for QQQ and SPY.")
        # Convert the raw data block directly into a pandas DataFrame for simple slicing
        df = bars_data.df
        logging.debug(f"?? DataFrame shape: {df.shape} | Columns: {list(df.columns)}")
    
        if not df.empty:
            for symbol in ["QQQ", "SPY"]:
                if symbol in df.index.levels[0]:
                    symbol_df = df.xs(symbol)
                    logging.info(f"\n=== {symbol} Premarket Data ===")
                    
                    # FIXED: Changed keys to lowercase ('open', 'close', 'high', 'low')
                    open_at_4am = symbol_df['open'].iloc[0]
                    price_at_4pm = symbol_df['close'].iloc[-1]
                    high_premarket = symbol_df['high'].max()
                    low_premarket = symbol_df['low'].min()
                    
                    # Perform structural calculations
                    absolute_change = price_at_4pm - open_at_4am
                    percentage_change = (absolute_change / open_at_4am) * 100
                    direction = "[UP]" if absolute_change >= 0 else "[DOWN]"
                    
                    # Add information block to complete the logic loop
                    logging.info(f"4:00 AM Open:     ${open_at_4am:,.2f}")
                    logging.info(f"4:00 PM Close:  ${price_at_4pm:,.2f}")
                    logging.info(f"Percent Change:   {percentage_change:+.2f}%")   
                    logging.info(f"Absolute Change:  {direction} ${absolute_change:+,.2f}")         
                    logging.info(f"Premarket High:   ${high_premarket:,.2f}")
                    logging.info(f"Premarket Low:    ${low_premarket:,.2f}")
                else:   
                    logging.info(f"No trading data available for symbol {symbol} yet.")
        else:
            logging.info("Alpaca data frame returned empty. Ensure the market is open.")
            
    except Exception as e:
        logging.error(f"An unexpected data client error occurred: {e}")

try:
    client = config.mk_alpaca_historical_client()
except Exception as e:
    logging.error(f"Critical Error loading Alpaca configuration or initializing client: {e}")
    sys.exit(2)        
logging.info(f"?? Alpaca Historical Data Client initialized successfully.")
get_monday_premarket_data(client)    

