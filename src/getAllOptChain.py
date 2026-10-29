import sys
import logging
from lib.logger import initialize_logging
initialize_logging(default_level=logging.INFO) # Pass logging.DEBUG here if you want full app debug mode
from pathlib import Path
from lib.getOptionsContracts import get_pruned_option_chain

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
    logging.error(f"? Failed to parse configuration file: {ce}")
    sys.exit(1)
try:
    target_symbols = config.get("symbols", "target_symbols")
except Exception as e:
    logging.error(f"Critical Error loading Alpaca configuration or initializing symbols: {e}")
    sys.exit(2)        
logging.info("*" * 40)
logging.info(f" Collect option chain data for {len(target_symbols)} symbols...")

for symbol in target_symbols:
    logging.debug(f"config contents: {config.__dict__}")
    # Normalize ticker to uppercase for the Alpaca API
    ticker = symbol.upper()
    
    # Visual Header Separator
    logging.info(f" PROCESSING DATA FOR TICKER: {ticker} ")
    
    try:
        contract, clean_payload = get_pruned_option_chain(ticker, config.ALPACA_KEY, config.ALPACA_SECRET, client)
        logging.debug(f"Pruned Option Chain Payload for {ticker}:")
        logging.debug(clean_payload)
        logging.debug(f"  This is the second OTM contract, stopping further logging.")
        logging.debug(f"Option Symbol: {contract['contract']} | Strike: {contract['strike']} | Bid: {contract['bid']} | Ask: {contract['ask']}")

    except Exception as e:
        logging.error(f"Error processing symbol {ticker}: {e}")


    #logging.info("Analyzing pruned live option chain data for MU...")
    #result = consult_isolated_analyst(clean_payload)
    #logging.info(f"MU Model response: {result}")


