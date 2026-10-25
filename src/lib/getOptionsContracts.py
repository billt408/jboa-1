import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime
from lib.utils import extract_strike_price, get_current_ask_price, get_three_fridays_away

def get_pruned_option_chain(underlying_ticker, api_key, api_secret, client):
    """
    Fetches the true full option chain using Alpaca's dedicated option-chain snapshot endpoint,
    handles full page token loops, and centers the final output around the spot price.
    """
    if "," in underlying_ticker:
        underlying_ticker = underlying_ticker.split(",")[0].strip()

    ticker = underlying_ticker.upper().strip()

    current_date_var = datetime.today().date()
    target_friday = get_three_fridays_away(start_date=current_date_var)
    expiration_date = target_friday.strftime("%Y-%m-%d")

    logging.debug(f" Target Expiration Date (3 Fridays Away): {expiration_date}")

    # ==========================================================================
    # ONE-STEP: Call the native option-chain snapshot endpoint directly
    # ==========================================================================
    base_url = f"https://data.alpaca.markets/v1beta1/options/snapshots/{ticker}"
    
    all_snapshots = {}
    page_token = None
    
    while True:
        # Build params for clean snapshot filtration
        params = {
            "feed": "indicative",
            "expiration_date": expiration_date,
            "type": "call",
            "limit": 1000
        }
        if page_token:
            params["page_token"] = page_token
            
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        
        req = urllib.request.Request(url)
        req.add_header("APCA-API-KEY-ID", api_key)
        req.add_header("APCA-API-SECRET-KEY", api_secret)
        req.add_header("accept", "application/json")
        
        try:
            with urllib.request.urlopen(req) as response:
                raw_data = json.loads(response.read().decode('utf-8'))
        except Exception as e:
            return f"Error fetching option chain from Alpaca: {str(e)}"
            
        page_snapshots = raw_data.get("snapshots", {})
        all_snapshots.update(page_snapshots)
        
        page_token = raw_data.get("next_page_token")
        if not page_token:
            break

    logging.debug(f" Target Master Contracts returned by Alpaca: {len(all_snapshots)} Call options.")
    if not all_snapshots:
        return f"No snapshot data returned from Alpaca for {ticker}."
    
    # Fetch your shared baseline spot asset ask price
    stk_ask_price = get_current_ask_price(ticker, client)
    logging.debug(f" Current Spot Ask Price for {ticker}: {stk_ask_price}")
    # ==========================================================================
    # PROCESS, CENTER AND PRUNE AROUND SPOT PRICE
    # ==========================================================================
    parsed_contracts = []
    for contract_symbol, details in all_snapshots.items():
        if not details:
            continue
        greeks = details.get("greeks", {}) or {}
        latest_quote = details.get("latestQuote", {}) or {}
        strike_price = extract_strike_price(contract_symbol)
        
        parsed_contracts.append({
            "contract": contract_symbol,
            "strike": strike_price,
            "bid": latest_quote.get("bp", 0),
            "ask": latest_quote.get("ap", 0),
            "gamma": round(float(greeks.get("gamma", 0)), 4) if greeks.get("gamma") else 0,
            "theta": round(float(greeks.get("theta", 0)), 4) if greeks.get("theta") else 0,
            "distance_from_spot": abs(strike_price - stk_ask_price) if stk_ask_price > 0 else strike_price
        })

    # Sort items based on proximity distance to stock spot value
    if stk_ask_price > 0:
        sorted_by_proximity = sorted(parsed_contracts, key=lambda x: x["distance_from_spot"])
        # Take the top 20 closest contracts to the money
        pruned_list = sorted_by_proximity[:1000]
        # Re-sort chronologically by strike for standard user display
        pruned_chain = sorted(pruned_list, key=lambda x: x["strike"])
    else:
        sorted_by_strike = sorted(parsed_contracts, key=lambda x: x["strike"])
        pruned_chain = sorted_by_strike[:20]
    logging.debug(f" pruned chain length: {len(pruned_chain)}")
    otm=0
    # Clear terminal logging format
    for contract in pruned_chain:
        logging.debug("^" * 40)
        logging.debug(f"Option Symbol: {contract['contract']} | Strike: {contract['strike']} | Bid: {contract['bid']} | Ask: {contract['ask']}")
        logging.debug(f"Strike Price: {stk_ask_price} | Spot Price: {stk_ask_price}")
        if stk_ask_price > 0:
            if contract['strike'] <= stk_ask_price:
                logging.debug(f"  IN-THE-MONEY (ITM)")
            else:
                logging.debug(f"  OUT-OF-THE-MONEY (OTM)")
                otm += 1
                if otm == 2:
                    logging.debug(f"  This is the second OTM contract, stopping further logging.")
                    logging.debug(f"Option Symbol: {contract['contract']} | Strike: {contract['strike']} | Bid: {contract['bid']} | Ask: {contract['ask']}")
                    break

    return contract, json.dumps({
        "underlying": ticker,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "chain_snapshot": pruned_chain
    }, indent=2)
