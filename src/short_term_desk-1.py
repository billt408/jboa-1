import json
import urllib.request
from datetime import datetime, timedelta

def get_5day_hold_candidates(underlying_ticker, api_key, api_secret):
    """
    Pulls live data and isolates contracts optimal for a 5-day swing hold.
    """
    #url = f"https://alpaca.markets{underlying_ticker}?feed=indicative"
    url = f"https://data.alpaca.markets/v1beta1/options/snapshots/{underlying_ticker}?feed=indicative"
    
    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", api_key)
    req.add_header("APCA-API-SECRET-KEY", api_secret)
    req.add_header("accept", "application/json")
    
    try:
        with urllib.request.urlopen(req) as response:
            raw_data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"\n? Network Connection Error: {str(e)}")
        return None

    snapshots = raw_data.get("snapshots", {})
    if not snapshots:
        error_msg = f"No snapshot data returned from Alpaca for {underlying_ticker}."
        print(error_msg)
        return None
    else:
        print(f"Successfully retrieved {len(snapshots)} option contracts for {underlying_ticker}.")
    today = datetime.now()
    # Narrow window to contracts expiring in 7 to 15 days
    min_expiry = today + timedelta(days=7)
    max_expiry = today + timedelta(days=15)
    
    candidates = []
    
    for contract_symbol, details in snapshots.items():
        greeks = details.get("greeks", {})
        latest_quote = details.get("latestQuote", {})
        
        # We need options with positive liquidity and available data
        bid = latest_quote.get("bp", 0)
        ask = latest_quote.get("ap", 0)
        delta = greeks.get("delta")
        theta = greeks.get("theta")
        
        if not bid or not ask or delta is None or theta is None:
            continue
            
        spread = ask - bid
        # Ignore illiquid options with massive wide spreads that destroy short-term returns
        if spread > 0.35: 
            continue
            
        # Target high-probability setups (Delta between 0.30 and 0.50)
        if 0.30 <= abs(float(delta)) <= 0.50:
            candidates.append({
                "contract": contract_symbol,
                "type": "CALL" if float(delta) > 0 else "PUT",
                "delta": round(float(delta), 2),
                "theta": round(float(theta), 4),
                "bid": bid,
                "ask": ask,
                "spread": round(spread, 2),
                "implied_vol": round(float(details.get("impliedVolatility", 0)), 3)
            })

    # Sort candidates by lowest spread / optimal premium metrics 
    # to find the top 5 most viable mathematical targets
    candidates = sorted(candidates, key=lambda x: x['spread'])[:5]
    return candidates

def consult_5day_strategist(candidates_json):
    system_prompt = (
        "You are an expert options risk modeling agent specializing in short-term 5-day hold cycles.\n"
        "Your task is to analyze the 5 mathematically pre-filtered option contracts provided and select the SINGLE most profitable candidate.\n\n"
        "EVALUATION CRITERIA FOR A 5-DAY HOLD:\n"
        "1. For Buying Options: Look for a combination of high Delta (directional leverage) and low Ask price relative to the delta.\n"
        "2. For Selling Options: Look for the highest absolute negative Theta value relative to the spread size to capture rapid time decay over the next 5 days.\n"
        "3. Transaction Cost: Favor tight spreads to protect execution value when closing the trade in 5 days.\n\n"
        "CRITICAL RULES:\n"
        "- You must respond ONLY with a single valid JSON object following the schema below.\n"
        "- Do NOT use markdown code blocks or conversational text.\n\n"
        "JSON SCHEMA:\n"
        "{\n"
        "  \"selected_trade\": boolean,\n"
        "  \"target_contract\": \"string (contract name)\",\n"
        "  \"action\": \"string (BUY_TO_OPEN or SELL_TO_OPEN)\",\n"
        "  \"expected_profit_driver\": \"string (e.g., Theta Decay Maximization or Delta Directional Move)\",\n"
        "  \"reasoning\": \"string (one sentence detail why this specific contract beats the other 4 options)\"\n"
        "}"
    )

    payload = {
        "model": "qwen2.5:3b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Analyze these 5-day hold candidate options and select the top contract line: {json.dumps(candidates_json)}"}
        ],
        "stream": False,
        "format": "json"
    }

    data = json.dumps(payload).encode('utf-8')
    
    # Point this to your isolated RHEL NUC IP address
    nuc_ip = "127.0.0.1" # Change to your local network IP if calling from another machine
    url = f"http://{nuc_ip}:11434/api/chat"
    
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    with urllib.request.urlopen(req) as response:
        res_body = response.read().decode('utf-8')
        return json.loads(res_body)['message']['content']

# --- RUNNING THE INTEGRATED LOOP ---
if __name__ == "__main__":
    ALPACA_KEY = "PK3IWXWY7AMSJRBAX3AOGMHNU4"
    ALPACA_SECRET = "AszALSMzQfAXRm1ADpAatfjjmfuC6ULtT9PrASvhT9YP"
    
    print("Executing mathematical pre-filtering for 5-day hold windows...")
    short_list = get_5day_hold_candidates("SPY", ALPACA_KEY, ALPACA_SECRET)
    
    if short_list:
        print("Piping candidates to isolated local LLM strategist...")
        final_decision = consult_5day_strategist(short_list)
        print("\nSelected Multi-Agent Strategy Layout:")
        print(final_decision)
    else:
        print("No liquid short-term contracts matched the 5-day filter rules.")

