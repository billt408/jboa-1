from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed

# Initialize your data stream client
# (Use DataFeed.IEX for free accounts, DataFeed.SIP for paid accounts)
stream_client = StockDataStream("ALPACA_KEY", "ALPACA_SECRET", feed=DataFeed.IEX)

async def check_market_momentum(data, api_key, secret_key):
    # This triggers live whenever SPY or QQQ ticks
    print(f"Ticker: {data.symbol} | Live Price: ${data.price:,.2f}")
    
    # If the market jumps, trigger your SPY option buy order block here
    if data.symbol == "SPY" and data.price > 585.00:
        print("Momentum threshold broken! Submitting options limit order...")
        # execute_options_order()

# Subscribe to both equity benchmarks natively
stream_client.subscribe_trades(check_market_momentum, "SPY", "QQQ")
stream_client.run()
