import json
import urllib.request
import sys
from pathlib import Path
from lib.utils import JsonIniParser


def consult_isolated_analyst(market_data_snapshot, config_obj):
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
    NUC_IP = config_obj.NUC_IP
    
    url = f"http://{NUC_IP}:11434/api/chat"
    
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

# Place this at the absolute bottom of your main execution file:

if __name__ == "__main__":
    test_data = "{'ticker': 'SPY', 'price': 510}"
    # Initialize the parser with the file path
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
    
    try:
        # Load the configuration variables into the instance
        config.load_globals()
    except Exception as ce:
        print(f"? Failed to parse configuration file: {ce}")
        sys.exit(1)
    # ?? THIS IS YOUR NATIVE BREAKPOINT:
    #import pdb; pdb.set_trace() 
    
    #print("Debugger is starting...") 
    result = consult_isolated_analyst(test_data, config)

    print("Model response:", result)
