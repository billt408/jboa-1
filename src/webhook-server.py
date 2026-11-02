from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# Decorative terminal header
print("=" * 60)
print(" ?? LOCAL TRADINGVIEW WEBHOOK RECEIVER INITIALIZED")
print("=" * 60)

@app.route('/webhook', methods=['POST'])
def handle_tradingview_webhook():           
    # 1. Verify incoming content type is JSON
    if not request.is_json:
        return jsonify({"status": "error", "message": "Payload must be JSON"}), 400

    # 2. Extract the data packet sent by TradingView
    data = request.json
    
    ticker = data.get('ticker', 'UNKNOWN')
    price = data.get('price', '0.00')
    rsi_value = data.get('rsi_value', 'N/A')
    tv_time = data.get('time', '')
    
    # 3. Create a local timestamp of exactly when your machine received it
    local_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 4. Format the output based on whether it is Overbought or Oversold
    try:
        rsi_float = float(rsi_value) if rsi_value != 'N/A' else 50.0
        status_label = "?? OVERBOUGHT" if rsi_float >= 70.0 else "?? OVERSOLD" if rsi_float <= 35.0 else "?? TRIPPED"
    except ValueError:
        status_label = "? ALERT"

    # 5. Print a clean, readable dashboard panel to your terminal screen
    print(f"\n[ {local_now} ] {status_label} EVENT DETECTED")
    print("-" * 50)
    print(f"  ?? Symbol:    {ticker}")
    print(f"  ?? Price:     ${price}")
    print(f"  ?? 1m RSI:    {rsi_value}")
    print(f"  ?? TV Time:   {tv_time}")
    print("-" * 50)

    # 6. Return a clean HTTP 200 OK success status back to TradingView's cloud servers
    return jsonify({"status": "success", "received": ticker}), 200

if __name__ == '__main__':
    # Runs locally on port 5000
    app.run(host='0.0.0.0', port=5000, debug=False)
    