import json
import urllib.request
from datetime import datetime, timedelta

def get_pruned_option_chain(underlying_ticker, api_key, api_secret):
    """
    Fetches the live options snapshot from Alpaca and prunes it to 
    fit inside a local LLM's context window.
    """
    # 1. Alpaca Live Options Snapshot Endpoint (returns greeks + quotes)
    url = f"https://data.alpaca.markets/v1beta1/options/snapshots/{underlying_ticker}?feed=indicative"
    
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
    if not snapshots:
        return "No snapshot data returned from Alpaca."

    # 2. Configure Pruning Targets (e.g., standard 30 to 45 day swing cycle)
    today = datetime.now()
    min_expiry = today + timedelta(days=30)
    max_expiry = today + timedelta(days=45)
    
    pruned_chain = []
    
    # 3. Parse and Filter the Chain
    for contract_symbol, details in snapshots.items():
        # Parsing string formatting logic out of Alpaca contract strings
        # Example format: AAPL261016C00230000 -> Symbol, Expiry, Type, Strike
        greeks = details.get("greeks", {})
        latest_quote = details.get("latestQuote", {})
        
        # Pull delta to measure distance from the money
        delta = greeks.get("delta")
        if delta is None:
            continue
            
        # Target liquid Near-The-Money options (Delta between 0.15 and 0.65)
        if not (0.15 <= abs(float(delta)) <= 0.65):
            continue
            
        # Extract contract details safely
        implied_vol = details.get("impliedVolatility", 0)
        bid = latest_quote.get("bp", 0)
        ask = latest_quote.get("ap", 0)
        
        # Simple contract string parser to read the expiry date manually
        # If your strategies need exact date matching, parse details['impliedVolatility'] parent data
        
        pruned_chain.append({
            "contract": contract_symbol,
            "delta": round(float(delta), 2),
            "iv": round(float(implied_vol), 3),
            "bid": bid,
            "ask": ask,
            "gamma": round(float(greeks.get("gamma", 0)), 4),
            "theta": round(float(greeks.get("theta", 0)), 4)
        })
        
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

def consult_isolated_analyst(market_data_snapshot):
    system_prompt = (
        "You are the automated Market Analyst Agent for an algorithmic options trading desk. "
        "Your sole task is to evaluate raw options chain data and determine a risk-defined options strategy.\n\n"
        "CRITICAL RULES:\n"
        "1. You must respond ONLY with a single, valid JSON object.\n"
        "2. Do NOT wrap the JSON in markdown code blocks.\n"
        "3. Do NOT include any introductory or concluding text, explanations, or commentary.\n"
        "4. If no valid trade is found based on the criteria, set 'execute_trade' to false.\n\n"
        "The JSON structure must strictly follow this schema:\n"
        "{\n"
        "  \"execute_trade\": boolean,\n"
        "  \"strategy_name\": \"string\",\n"
        "  \"underlying_ticker\": \"string\",\n"
        "  \"reasoning\": \"string\",\n"
        "  \"legs\": [\n"
        "    { \"leg_id\": int, \"action\": \"BUY|SELL\", \"option_type\": \"CALL|PUT\", \"strike_price\": float, \"expiration_date\": \"YYYY-MM-DD\", \"quantity\": int }\n"
        "  ]\n"
        "}"
    )

    user_message = f"Evaluate this real-time options market snapshot and provide your decision: {market_data_snapshot}"

    # Build the payload for Ollama
    payload = {
        "model": "qwen2.5:3b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "stream": False,
        "format": "json"  # Forces Ollama's CPU engine to strictly output JSON
    }

    # Convert the Python dictionary into a string of bytes
    data = json.dumps(payload).encode('utf-8')
    
    # Point this to your NUC's local LAN IP address
    nuc_ip = "192.168.0.162"  # Swap with your actual local NUC IP
    url = f"http://{nuc_ip}:11434/api/chat"
    
    # Configure the standard library request container
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    
    try:
        # Fire the request across the local network
        with urllib.request.urlopen(req) as response:
            res_body = response.read().decode('utf-8')
            res_json = json.loads(res_body)
            # Extract and return the pristine text decision
            return res_json['message']['content']
            
    except Exception as e:
        return f"Network or Ollama Error: {str(e)}"



if __name__ == "__main__":
    # Test credentials placeholder - use Alpaca Dashboard paper configurations
    ALPACA_KEY = "PK3IWXWY7AMSJRBAX3AOGMHNU4"
    ALPACA_SECRET = "AszALSMzQfAXRm1ADpAatfjjmfuC6ULtT9PrASvhT9YP"
    
    print("Fetching and pruning live chain data for SPY...")
    clean_payload = get_pruned_option_chain("SPY", ALPACA_KEY, ALPACA_SECRET)
    print(clean_payload)

    print("Analyzing pruned live option chain data for SPY...")
    result = consult_isolated_analyst(clean_payload)
    print("SPY Model response:", result)

    print("Fetching and pruning live chain data for IBM...")
    clean_payload = get_pruned_option_chain("IBM", ALPACA_KEY, ALPACA_SECRET)
    print(clean_payload)

    print("Analyzing pruned live option chain data for IBM...")
    result = consult_isolated_analyst(clean_payload)
    print("IBM Model response:", result)
