from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus

# 1. Initialize the Trading Client (Using Paper Trading Environment)
API_KEY = "YOUR_API_KEY"
SECRET_KEY = "YOUR_SECRET_KEY"

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# 2. Find a specific Option Contract
# We will look for a Call option on AAPL expiring in the future
underlying_symbol = "AAPL"

req = GetOptionContractsRequest(
    underlying_symbols=[underlying_symbol],
    status=AssetStatus.ACTIVE,
    contract_type=ContractType.CALL,
    expiration_date_gte=datetime(2026, 11, 1).date(), # Looking for Nov 2026 or later
)

# Fetch available contracts from the option chain
contracts = trading_client.get_option_contracts(req)

if not contracts.data:
    raise Exception("No matching option contracts found.")

# Select the first available contract from the list as our target
target_contract = contracts.data[0]
option_symbol = target_contract.symbol

print(f"Found Target Contract: {target_contract.name}")
print(f"OSI Option Symbol: {option_symbol}")
print(f"Strike Price: ${target_contract.strike_price} | Expiration: {target_contract.expiration_date}")

# 3. Create and Submit the Option Order
# Options require exact integer quantities (number of contracts)
order_data = MarketOrderRequest(
    symbol=option_symbol,
    qty=1,                  # 1 contract controls 100 shares
    side=OrderSide.BUY,     # Buy to Open
    time_in_force=TimeInForce.DAY
)

# Submit the order to Alpaca
try:
    submitted_order = trading_client.submit_order(order_data=order_data)
    print(f"\nOrder submitted successfully!")
    print(f"Order ID: {submitted_order.id}")
    print(f"Status: {submitted_order.status}")
except Exception as e:
    print(f"\nFailed to submit option order: {e}")
