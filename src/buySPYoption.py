import sys
from pathlib import Path
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass

# Import your custom configuration parser library
from lib.utils import JsonIniParser

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

option_symbol = "MU261106C00910000" 
contracts_qty = 10  # 1 contract controls 100 shares

# Create a market order targeting the Options asset class
order_data = MarketOrderRequest(
    symbol=option_symbol,
    qty=contracts_qty,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY
)
print(f"\n?? Executing Buy Market Order for {contracts_qty} contracts of {option_symbol}...")

try:
    submitted_order = trading_client.submit_order(order_data=order_data)
    print(f"?? Option order submitted successfully!")
    print(f"   Order ID: {submitted_order.id}")
    print(f"   Status: {submitted_order.status}")
    print(f"   Symbol: {submitted_order.symbol}")
    print(f"   Quantity: {submitted_order.qty} contracts")
except Exception as e:
    print(f"? Failed to execute option order: {e}")
    sys.exit(1)
