import yfinance as yf

def get_nasdaq_future_details():
    ticker_symbol = "NQ=F"
    
    try:
        nasdaq_future = yf.Ticker(ticker_symbol)
        
        # yfinance stores live metadata metrics inside the fast_info object
        info = nasdaq_future.fast_info
        
        # Extract live price metrics
        current_price = info.last_price
        previous_close = info.previous_close
        
        # Calculate changes safely
        absolute_change = current_price - previous_close
        percentage_change = (absolute_change / previous_close) * 100
        
        # Set up direction indicators
        direction = "? UP" if absolute_change >= 0 else "? DOWN"
        
        print(f"Ticker: {ticker_symbol}")
        print(f"Current Value:    {current_price:,.2f}")
        print(f"Net Change:       {direction} {abs(absolute_change):+,.2f}")
        print(f"Percent Change:   {percentage_change:+.2f}%")
        
        return {
            "price": current_price,
            "absolute_change": absolute_change,
            "percentage_change": percentage_change
        }
            
    except Exception as e:
        print(f"An error occurred while fetching data: {e}")
        return None

if __name__ == "__main__":
    get_nasdaq_future_details()
