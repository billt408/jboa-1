import sys
from pathlib import Path
from lib.getOptChain import get_pruned_option_chain



# ==============================================================================
# ENVIRONMENT PATH PATCH
# Guarantees that 'src' is always in the path so 'from lib.utils' works seamlessly
# regardless of whether this script is called directly or imported.
# ==============================================================================
src_path = str(Path(__file__).resolve().parent.parent)
if src_path not in sys.path:
    sys.path.insert(0, src_path)

# Import your parser from the sibling utils module
from lib.utils import JsonIniParser
try:
    # Initialize the parser with the file path
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
    config.load()
    config.load_globals()
    environment = config.ENVIRONMENT
    credentials = config.CREDENTIALS
    nuc_ip = config.NUC_IP
    client = config.mk_alpaca_historical_client()
except Exception as ce:
    print(f"? Failed to parse configuration file: {ce}")
    sys.exit(1)
try:
    target_symbols = config.get("symbols", "target_symbols")
except Exception as e:
    print(f"Critical Error loading Alpaca configuration or initializing symbols: {e}")
    sys.exit(2)        

print(f"?? Collect option chain data for {len(target_symbols)} symbols...\n")

for symbol in target_symbols:
    #print(f"config contents: {config.__dict__}")
    # Normalize ticker to uppercase for the Alpaca API
    ticker = symbol.upper()
    
    # Visual Header Separator
    print("?" * 40)
    print(f" PROCESSING DATA FOR TICKER: {ticker} ")
    
    try:
        clean_payload = get_pruned_option_chain(ticker, config.ALPACA_KEY, config.ALPACA_SECRET, client)
        print(f"Pruned Option Chain Payload for {ticker}:")
        print(clean_payload)
    except Exception as e:
        print(f"Error processing symbol {ticker}: {e}")


    #print("Analyzing pruned live option chain data for MU...")
    #result = consult_isolated_analyst(clean_payload)
    #print("MU Model response:", result)


