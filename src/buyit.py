import sys
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus

# 1. Initialize the Trading Client (Using Paper Trading Environment)
ALPACA_KEY = "PK3IWXWY7AMSJRBAX3AOGMHNU4"
ALPACA_SECRET = "AszALSMzQfAXRm1ADpAatfjjmfuC6ULtT9PrASvhT9YP"
 
try:
    trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
except Exception as e:
    print(f"? Critical Error initializing Alpaca Client: {e}")
    sys.exit(1)

# 2. Find a specific Option Call Contract
underlying_symbol = "AAPL"

# Query contracts expiring on or after Nov 1, 2026
req = GetOptionContractsRequest(
    underlying_symbols=[underlying_symbol],
    status=AssetStatus.ACTIVE,
    contract_type=ContractType.CALL,
    expiration_date_gte=datetime(2026, 11, 1).date(), 
)

print(f"?? Fetching active option chain for {underlying_symbol}...")

try:
    # Fetch available contracts from the option chain
    response = trading_client.get_option_contracts(req)
    
    # FIX: Alpaca-py groups contracts inside a dictionary-like '.contracts' attribute
    if not response or not hasattr(response, 'contracts') or not response.contracts:
        print(f"?? No active option contracts found for {underlying_symbol} matching criteria.")
        sys.exit(0)
        
    # Get the raw list of contracts securely
    contracts_list = response.contracts
    print(f"? Found {len(contracts_list)} potential option contracts.")

    # Safely select the first available contract
    target_contract = contracts_list[0]
    option_symbol = target_contract.symbol

    print(f"\n?? Selected Target Contract: {target_contract.name}")
    print(f"   OSI Option Symbol: {option_symbol}")
    print(f"   Strike Price: ${target_contract.strike_price} | Expiration: {target_contract.expiration_date}")

except AttributeError as ae:
    print(f"? SDK Attribute Error: The data structure returned by the SDK layout changed. Details: {ae}")
    sys.exit(1)
except Exception as e:
    print(f"? Failed to query option chain: {e}")
    sys.exit(1)


# 3. Create and Submit the Option Order
order_data = MarketOrderRequest(
    symbol=option_symbol,
    qty=1,                  # 1 contract controls 100 shares
    side=OrderSide.BUY,     # Buy to Open
    time_in_force=TimeInForce.DAY
)

print(f"\n?? Executing Buy-To-Open Market Order for 1 contract of {option_symbol}...")

try:
    submitted_order = trading_client.submit_order(order_data=order_data)
    print(f"?? Order submitted successfully!")
    print(f"   Order ID: {submitted_order.id}")
    print(f"   Status: {submitted_order.status}")
    print(f"   Asset Class: {submitted_order.asset_class}")
except Exception as e:
    print(f"? Failed to submit option order to Alpaca: {e}")
    sys.exit(1)
