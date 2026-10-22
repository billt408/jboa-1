import json
import urllib.request
from datetime import datetime, timedelta
from lib.utils import get_three_fridays_away
from lib.utils import extract_strike_price
from lib.utils import get_current_ask_price

# ==============================================================================
# ENVIRONMENT PATH PATCH
# Guarantees that 'src' is always in the path so 'from lib.utils' works seamlessly
# regardless of whether this script is called directly or imported.
# ==============================================================================
#src_path = str(Path(__file__).resolve().parent.parent)
#if src_path not in sys.path:
#    sys.path.insert(0, src_path)#

# Import your parser from the sibling utils module
#from lib.utils import JsonIniParser

def get_pruned_option_chain(underlying_ticker, api_key, api_secret, client):
    """
    Fetches the live options snapshot from Alpaca and prunes it to 
    fit inside a local LLM's context window.
    """
    current_date_var = datetime.today().date()
    target_friday = get_three_fridays_away(start_date=current_date_var)
    target_friday_str = target_friday.strftime("%Y-%m-%d")    
    print(f"Passed variable: {current_date_var}")
    print(f"Result (3 Fridays away): {target_friday}")
    print(f"Target Friday (3 Fridays from today): {target_friday_str}")
    expiration_date = target_friday_str  # Must use YYYY-MM-DD format
#==========================
    all_snapshots = {}
    page_token = None
    base_url = f"https://data.alpaca.markets/v1beta1/options/snapshots/{underlying_ticker}?feed=indicative&expiration_date={expiration_date}&type=call&limit=1000"
    while True:
        # Append the pagination token to the URL if it exists
        url = base_url if not page_token else f"{base_url}&page_token={page_token}"
        
        req = urllib.request.Request(url)
        req.add_header("APCA-API-KEY-ID", api_key)
        req.add_header("APCA-API-SECRET-KEY", api_secret)
        req.add_header("accept", "application/json")
        
        try:
            with urllib.request.urlopen(req) as response:
                raw_data = json.loads(response.read().decode('utf-8'))
        except Exception as e:
            return f"Error fetching from Alpaca: {str(e)}"
        
        # Merge this page's snapshots into our master dictionary
        page_snapshots = raw_data.get("snapshots", {})
        all_snapshots.update(page_snapshots)
        
        # Look for the pagination token at the root of the response payload
        page_token = raw_data.get("next_page_token")
        
        # If next_page_token is null or missing, we have collected every page
        if not page_token:
            break

    print(f"?? Total contracts collected after pagination: {len(all_snapshots)} for {underlying_ticker}.")
    if not all_snapshots:
        return "No snapshot data returned from Alpaca."
    
    today = datetime.now()
    min_expiry = today + timedelta(days=30)
    max_expiry = today + timedelta(days=45)
    sorted_snapshots = sorted(
        all_snapshots.items(), 
        key=lambda item: int(item[0][-8:])
    )        
    stk_ask_price = get_current_ask_price(underlying_ticker,client)
    pruned_chain = []
    # 3. Parse and Filter the Chain
    for contract_symbol, details in sorted_snapshots:
        # Parsing string formatting logic out of Alpaca contract strings
        # Example format: AAPL261016C00230000 -> Symbol, Expiry, Type, Strike
        greeks = details.get("greeks", {})
        latest_quote = details.get("latestQuote", {})
        
        # Pull delta to measure distance from the money
        #delta = greeks.get("delta")
        #if delta is None:
        #    continue
            
        # Target liquid Near-The-Money options (Delta between 0.15 and 0.65)
        #if not (0.15 <= abs(float(delta)) <= 0.65):
        #    continue
            
        # Extract contract details safely
        #implied_vol = details.get("impliedVolatility", 0)
        bid = latest_quote.get("bp", 0)
        ask = latest_quote.get("ap", 0)
        if stk_ask_price > 0:
            print(f"? Real-time Ask Price for {underlying_ticker}: ${stk_ask_price:.2f} -- ${ask:.2f}")
        else:
            print(f"? Could not fetch data for {underlying_ticker}.")        
        strike_price = extract_strike_price(contract_symbol)
        # Output will be exactly: 342.5
        print(f"Option Symbol: {contract_symbol}")
        print(f"Extracted Strike Price: {strike_price}")
        if (stk_ask_price > 0 and (strike_price  <= stk_ask_price)):
            print(f"?? {contract_symbol} is IN-THE-MONEY (ITM).")
        else:
            print(f"?? {contract_symbol} is out-of-the-money (OTM).")
        # Simple contract string parser to read the expiry date manually
        # If your strategies need exact date matching, parse details['impliedVolatility'] parent data
        pruned_chain.append({
            "contract": contract_symbol,
        #    "delta": round(float(delta), 2),
        #    "iv": round(float(implied_vol), 3),
            "bid": bid,
            "ask": ask,
            "gamma": round(float(greeks.get("gamma", 0)), 4),
            "theta": round(float(greeks.get("theta", 0)), 4)
        })
        print("?" * 40)
        
        # Cap the payload list to the top 20 most liquid contracts 
        # to ensure it strictly respects the local 3B model's context threshold
        if len(pruned_chain) >= 20:
            break

    # Wrap the extracted results into a clean dictionary payload
    return json.dumps({
        "underlying": underlying_ticker,
        "timestamp": today.strftime("%Y-%m-%d %H:%M:%S"),
        "chain_snapshot": pruned_chain
    }, indent=2)


""" url = f"https://data.alpaca.markets/v1beta1/options/snapshots/{underlying_ticker}?feed=indicative&expiration_date={expiration_date}"

    #expiration_date = "2026-11-06"
    #url = f"https://alpaca.markets{underlying_ticker}&expiration_dates={expiration_date}"
    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", api_key)
    req.add_header("APCA-API-SECRET-KEY", api_secret)
    req.add_header("accept", "application/json")
    
    try:
        with urllib.request.urlopen(req) as response:
            raw_data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        return f"Error fetching from Alpaca: {str(e)}"
    
    snapshots = raw_data.get("snapshots", {})
    print(f"?? Retrieved {len(snapshots)} contracts from Alpaca for {underlying_ticker}.")
    if not snapshots:
        return "No snapshot data returned from Alpaca."
 """
