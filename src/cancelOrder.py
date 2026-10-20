import sys
from pathlib import Path
from alpaca.trading.client import TradingClient

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
# 3. CANCEL THE SPECIFIED ORDER
# ==========================================
# Replace this string with your target Order ID (e.g., submitted_order.id)
target_order_id = "cf43499b-eefc-4bc8-9bed-17584feeb980" 

print(f"?? Attempting to cancel order ID: {target_order_id}...")

try:
    # Submit the cancellation request
    trading_client.cancel_order_by_id(order_id=target_order_id)
    print(f"? Cancellation request sent successfully for Order ID: {target_order_id}")
    print("   Note: It may take a moment for the status to update to 'canceled' on your dashboard.")
    
except Exception as e:
    print(f"? Failed to cancel order: {e}")
    print("   This usually means the order ID does not exist, or the trade has already been completely filled.")
    sys.exit(1)
