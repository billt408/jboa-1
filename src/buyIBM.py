import sys
from pathlib import Path
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass

# Import your custom configuration parser library
from lib.parms import JsonIniParser

try:
    # Initialize the parser with the file path
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))

except Exception as ce:
    print(f"? Failed to parse configuration file: {ce}")
    sys.exit(1)
try:
    trading_client = config.make_alpaca_client()
except Exception as e:
    print(f"Critical Error loading Alpaca configuration or initializing client: {e}")
    sys.exit(2)


# ==========================================
# 3. SUBMIT IBM STOCK MARKET ORDER
# ==========================================
stock_symbol = "IBM"
shares_qty = 10

# Create a market order targeting the US Equity asset class
order_data = MarketOrderRequest(
    symbol=stock_symbol,
    qty=shares_qty,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
    asset_class=AssetClass.US_EQUITY  # Explicitly sets the order type to Standard Stocks
)

print(f"\n?? Executing Buy Market Order for {shares_qty} shares of {stock_symbol}...")

try:
    submitted_order = trading_client.submit_order(order_data=order_data)
    print(f"?? Stock order submitted successfully!")
    print(f"   Order ID: {submitted_order.id}")
    print(f"   Status: {submitted_order.status}")
    print(f"   Symbol: {submitted_order.symbol}")
    print(f"   Quantity: {submitted_order.qty} shares")
except Exception as e:
    print(f"? Failed to execute stock order: {e}")
    sys.exit(1)
