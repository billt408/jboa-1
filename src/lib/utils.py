import json
import sys
import logging
import datetime 
from pathlib import Path
from typing import Any, Dict, Optional
from alpaca.trading import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

class JsonIniParser:
    def __init__(self, filepath: str):
        """Initializes the parser with a path to the configuration file."""
        logger.debug(f"--? Initializing JsonIniParser with file: {filepath}")
        logging.getLogger("urllib3").setLevel(logging.INFO)
        self.filepath = filepath
        # Internal structure: { "section_name": { "key": value } }
        self.data: Dict[str, Dict[str, Any]] = {}


    def load(self) -> None:
        """Reads and parses the custom JSON-INI file from disk."""
        self.data = {}
        current_section: Optional[str] = None
        logger.debug(f"!!? Loading configuration from: {self.filepath}")
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                logger.debug(f"? Successfully read {len(lines)} lines from configuration file.")
        except FileNotFoundError:
            # If the file doesn't exist yet, we start with an empty configuration
            return
        logger.debug(f"? Successfully read {len(lines)} lines from configuration file.")
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            logger.debug(f"Line {line_num}: Processing: '{line}'")
            # Skip empty lines or lines that are pure comments
            if not line or line.startswith(';') or line.startswith('#'):
                continue
                
            # Match sections like [my_section]
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1].strip()
                if not current_section:
                    raise ValueError(f"Line {line_num}: Empty section name found.")
                if current_section not in self.data:
                    self.data[current_section] = {}
                continue

            # Match key-value pairs separated by '='
            if '=' in line:
                if current_section is None:
                    raise ValueError(f"Line {line_num}: Key-value pair found before any section declaration.")
                
                key, raw_value = line.split('=', 1)
                key = key.strip()
                raw_value = raw_value.strip()
                
                # Parse the value safely using JSON to preserve types (int, string, dict)
                try:
                    parsed_value = json.loads(raw_value)
                except json.JSONDecodeError as e:
                    # If it's a plain string not wrapped in quotes, fallback or handle error
                    # But per JSON strict rules, strings should be wrapped in "quotes"
                    raise ValueError(
                        f"Line {line_num}: Value for '{key}' is not valid JSON. "
                        f"Ensure strings are in double quotes \"value\" and objects are valid JSON. Error: {e}"
                    )
                
                self.data[current_section][key] = parsed_value
                logger.debug(f"Line {line_num}: Parsed [{current_section}] {key} = {parsed_value}")
            else:
                raise ValueError(f"Line {line_num}: Invalid syntax. Expected a [section] or a key=value pair. Got: '{line}'")

    def save(self) -> None:
        """Writes the current internal data structure back to the disk file."""
        with open(self.filepath, 'w', encoding='utf-8') as f:
            for section, keys in self.data.items():
                f.write(f"[{section}]\n")
                for key, value in keys.items():
                    # Serialize the value back to a compact JSON string representation
                    json_value = json.dumps(value)
                    f.write(f"{key} = {json_value}\n")
                f.write("\n")  # Add spacing between sections

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Reads a value programmatically. Returns default if section or key doesn't exist."""
        return self.data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any) -> None:
        """Updates or sets a value programmatically. Supports strings, ints, dicts, lists."""
        if section not in self.data:
            self.data[section] = {}
        self.data[section][key] = value

    def delete(self, section: str, key: str) -> bool:
        """Removes a key from a section. Returns True if found and removed."""
        if section in self.data and key in self.data[section]:
            del self.data[section][key]
            return True
        return False
        
    def load_globals(self) -> None:
        """Loads and binds configuration parameters directly to this instance."""
        # Force a load of the file path passed during initialization
        self.load() 
        if not self.data:
            raise FileNotFoundError(
                logging.error(f"Configuration file at '{self.filepath}' was empty or not found.")
            )

        # 3. Parse and configure the [logging] block
        logging_section = self.data.get("logging", {})

        # Bind log file if configured
        log_file = logging_section.get("log_file")
        if log_file:
            # Resolves log file relative to script parent directory
            log_path = str(Path(self.filepath).parent.parent / log_file)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
                )
            )
            logging.getLogger().addHandler(file_handler)

        # Apply target module logging levels dynamically
        for key, value in logging_section.items():
            if key.endswith(".level"):
                module_name = key.replace(".level", "")
                level_string = str(value).upper()

                # Get the name of the script that is currently running (e.g., 'getAllRSI')
                running_script_stem = Path(sys.argv[0]).stem
                
                # Check if this configuration line belongs to the currently executing file
                if module_name == running_script_stem:
                    logger_target = "__main__"
                    
                elif module_name == "utils":
                    logger_target = "lib.utils"
                    
                elif module_name == "__main__":
                    # Skip generic '__main__' rules if we are explicitly naming files instead
                    continue
                    
                else:
                    logger_target = f"lib.{module_name}"

                try:
                    level_const = getattr(logging, level_string)
                    logging.getLogger(logger_target).setLevel(level_const)
                    
                    # CRITICAL: If this is the current active script, update the root log level 
                    # as well so it doesn't filter out its own lower-level logs
                    if logger_target == "__main__":
                        logging.getLogger().setLevel(level_const)
                        
                    logger.info(f"Dynamically configured level for script context [{module_name}] to {level_string}")
                except AttributeError:
                    logger.warning(f"Warning: Invalid log level identifier string parsed: {level_string}")

        logger.debug(f"? Configuration loaded successfully:\n{json.dumps(self.data, indent=4)}")
         # DEBUG CHECK: If the internal dictionary is totally empty, the file wasn't read or is empty
        if not self.data:
            raise FileNotFoundError(
                f"\n? CRITICAL: The configuration file at '{self.filepath}' was either not found, "
                f"could not be opened, or parsed completely empty. Please verify this path matches your file."
            )
        self.ENVIRONMENT = self.get("alpaca_api", "environment", default="paper")
        self.CREDENTIALS = self.get("alpaca_api", "credentials")
        
        if not self.CREDENTIALS or "api_key" not in self.CREDENTIALS or "secret_key" not in self.CREDENTIALS:
            logger.error(f"? Error: Missing credentials dictionary in your config file.")
            sys.exit(1)
            
        # Bind them directly to the object instance
        self.ALPACA_KEY = self.CREDENTIALS["api_key"]
        self.ALPACA_SECRET = self.CREDENTIALS["secret_key"]
        self.IS_PAPER = (self.ENVIRONMENT == "paper")
        self.NUC_IP = self.get("llm_configuration", "ip_address", default="192.168.0.178")
        self.LOG_LEVEL = self.get("logging", "log_level", default="INFO").upper()
        logger.debug(f"? Configuration loaded successfully.   {self}")

    def mk_alpaca_trading_client(self) -> 'TradingClient':
        try:
            # Load the configuration variables into the instance
            self.load_globals()
        except Exception as ce:
            logger.error(f"? Failed to parse configuration file: {ce}")
            sys.exit(1)

        logger.info(f"Paper Trading Mode: {self.IS_PAPER}")
        # ==========================================
        # 2. INITIALIZE ALPACA TRADING CLIENT
        # ==========================================
        try:
            trading_client = TradingClient(self.ALPACA_KEY, self.ALPACA_SECRET, paper={self.IS_PAPER})
        except Exception as e:
            logger.error(f"Critical Error loading Alpaca configuration or initializing client: {e}")
            sys.exit(1)

        return trading_client

    def mk_alpaca_historical_client(self) -> 'StockHistoricalDataClient':
        try:
            # Load the configuration variables into the instance
            self.load_globals()
        except Exception as ce:
            logger.error(f"? Failed to parse configuration file: {ce}")
            sys.exit(1)

        logger.info(f"Paper Trading Mode: {self.IS_PAPER}")
        # ==========================================
        # 2. INITIALIZE ALPACA HISTORICAL CLIENT
        # ==========================================
        try:
            stock_historical_data_client = StockHistoricalDataClient(api_key=self.ALPACA_KEY, secret_key=self.ALPACA_SECRET, raw_data=False)
        except Exception as e:
            logger.error(f"Critical Error loading Alpaca configuration or initializing client: {e}")
            sys.exit(1)

        return stock_historical_data_client


def get_three_fridays_away(start_date=None) -> datetime:
    """Accurately calculates the date exactly 3 Fridays away from the start date."""
    if start_date is None:
        start_date = datetime.datetime.today().date()
    
    current_weekday = start_date.weekday() # 0=Mon, 4=Fri, 6=Sun
    target_weekday = 4
    
    # Calculate days to the very next Friday
    days_until_next_friday = (target_weekday - current_weekday) % 7
    if days_until_next_friday == 0:
        days_until_next_friday = 7
        
    # Push out an additional 14 days to make it exactly 3 Fridays away
    return start_date + datetime.timedelta(days=days_until_next_friday + 14)


def extract_strike_price(option_symbol: str) -> float:
    """Extracts the float strike price from a standard OCC option symbol string."""
    # Slice out the final 8 characters of the string
    raw_strike = option_symbol[-8:]

    # Convert to a float and shift the decimal place 3 spots to the left
    strike_price = float(raw_strike) / 1000.0

    return strike_price

def get_current_ask_price(symbol: str, client=None) -> float:
    """Fetches the real-time ask price for a given stock symbol using the Alpaca API.

    If no pre-initialized client is passed, it loads one via parms.ini.
    """
    ticker = symbol.upper()
    if client is None:
        logger.debug(f"The client is not initialized")

    try:
            request_params = StockSnapshotRequest(symbol_or_symbols=ticker)
            snapshot = client.get_stock_snapshot(request_params)

            if ticker not in snapshot:
                logger.info(f"? No snapshot data found for symbol: {ticker}")
                return 0.0

            ticker_snapshot = snapshot[ticker]
            
            # 1. Gather our potential price anchors from the snapshot object wrapper
            latest_quote = getattr(ticker_snapshot, 'latest_quote', None)
            latest_trade = getattr(ticker_snapshot, 'latest_trade', None)
            daily_bar = getattr(ticker_snapshot, 'daily_bar', None)
            
            ask_price = 0.0
            
            # 2. Strategy Plan A: Try extracting the standard live inside Ask price
            if latest_quote and hasattr(latest_quote, 'ask_price') and latest_quote.ask_price is not None:
                ask_price = float(latest_quote.ask_price)

            # 3. Strategy Plan B (THE FIX): If the IEX quote returned 0, fallback to the latest real trade execution price
            if ask_price == 0.0 and latest_trade and hasattr(latest_trade, 'price') and latest_trade.price is not None:
                ask_price = float(latest_trade.price)
                logger.debug(f"? Free-tier quote was thin (0.0). Successfully fell back to Latest Trade Price: ${ask_price:.2f}")

            # 4. Strategy Plan C: Absolute baseline fallback to the last daily candle close if everything else is dry
            if ask_price == 0.0 and daily_bar and hasattr(daily_bar, 'close') and daily_bar.close is not None:
                ask_price = float(daily_bar.close)
                logger.debug(f"? Quote and trade data empty. Falling back to Daily Candle Close: ${ask_price:.2f}")

            logger.debug(f"?? Reference Price Used for {ticker}: ${ask_price:.2f}")
            return ask_price

    except Exception as e:
        logger.error(f"? Error retrieving baseline market valuation for {ticker}: {e}")
        return 0.0

def get_logger(name: str, level=logging.INFO) -> logging.Logger:
    """Creates and returns a logger with the specified name and level."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Create console handler with the specified log level
    ch = logging.StreamHandler()
    ch.setLevel(level)
    
    # Create formatter and add it to the handler
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    
    # Add the handler to the logger
    if not logger.handlers:
        logger.addHandler(ch)
    
    return logger

NY = ZoneInfo("America/New_York")


def get_aligned_rsi(twelvedata_client,alpaca_client,symbol,cutoff,period=14,observations=60):
    """
    Fetch one-minute RSI histories using a shared cutoff.

    Select the latest timestamp present in BOTH histories.
    Never compare observations with different timestamps.

    Assumes both histories use America/New_York timestamps.
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")

    cutoff_ny = cutoff.astimezone(NY)
    cutoff_text = cutoff_ny.strftime("%Y-%m-%d %H:%M:%S")

    td_history = twelvedata_client.rsi(
        symbol=symbol,
        period=period,
        outputsize=observations,
        interval="1min",
        end_date=cutoff_text,
    )

    ap_history = alpaca_client.rsi(
        symbol=symbol,
        period=period,
        outputsize=observations,
        interval="1min",
        date=cutoff,
    )

    def index_history(history):
        indexed = {}

        for row in history:
            # Your clients return intraday timestamps in this format.
            timestamp = datetime.datetime.strptime(
                row["datetime"],
                "%Y-%m-%d %H:%M:%S",
            )

            # Compare local wall-clock timestamps consistently.
            if timestamp <= cutoff_ny.replace(tzinfo=None):
                indexed[timestamp] = row

        return indexed

    td_by_time = index_history(td_history)
    ap_by_time = index_history(ap_history)

    common_times = td_by_time.keys() & ap_by_time.keys()

    if not common_times:
        raise RuntimeError(
            f"{symbol}: no common RSI candle at or before "
            f"{cutoff_text}; refusing to compare mismatched candles"
        )

    common_time = max(common_times)
    common_text = common_time.strftime("%Y-%m-%d %H:%M:%S")

    td_latest_time = max(td_by_time)
    ap_latest_time = max(ap_by_time)

    if common_time != td_latest_time or common_time != ap_latest_time:
        logging.warning(
            "%s: aligning to %s; latest available: "
            "Twelve Data=%s, Alpaca=%s",
            symbol,
            common_text,
            td_latest_time.strftime("%Y-%m-%d %H:%M:%S"),
            ap_latest_time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    return {
        "symbol": symbol,
        "interval": "1min",
        "period": period,
        "requested_cutoff": cutoff_text,
        "datetime": common_text,
        "twelvedata_rsi": float(td_by_time[common_time]["rsi"]),
        "alpaca_rsi": float(ap_by_time[common_time]["rsi"]),
    }

def debug_rsi_inputs(
    twelvedata_client,
    alpaca_client,
    symbol,
    cutoff,
    period=14,
    bars_requested=1500,
    output_dir="rsi_debug",
):
    import csv
    import datetime as dt
    import math
    from pathlib import Path
    from types import SimpleNamespace

    from alpaca.common.enums import Sort
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")

    symbol = symbol.strip().upper()
    cutoff_ny = cutoff.astimezone(NY)
    end_text = cutoff_ny.strftime("%Y-%m-%d %H:%M:%S")

    # Fetch the actual Twelve Data RSI at this cutoff.
    td_api_history = twelvedata_client.rsi(
        symbol=symbol,
        interval="1min",
        period=period,
        outputsize=60,
        end_date=end_text,
    )

    # Fetch Twelve Data's raw input prices independently.
    td_response = twelvedata_client._get(
        "time_series",
        symbol=symbol,
        interval="1min",
        outputsize=bars_requested,
        end_date=end_text,
        timezone="America/New_York",
        order="asc",
        format="JSON",
    )

    td_rows = td_response.get("values")

    if not isinstance(td_rows, list) or not td_rows:
        raise RuntimeError(
            f"{symbol}: no Twelve Data time-series values returned"
        )

    td_prices = {}

    for row in td_rows:
        timestamp = dt.datetime.strptime(
            row["datetime"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=NY)

        close = float(row["close"])

        if not math.isfinite(close):
            raise RuntimeError("Non-finite Twelve Data close")

        if timestamp <= cutoff:
            td_prices[timestamp] = close

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=cutoff - dt.timedelta(days=30),
        end=cutoff,
        limit=bars_requested,
        sort=Sort.DESC,
        feed=alpaca_client.feed,
        adjustment=alpaca_client.adjustment,
    )

    response = alpaca_client._get_bars(request)

    ap_prices = {
        bar.timestamp.astimezone(NY): float(bar.close)
        for bar in response.data.get(symbol, [])
        if bar.timestamp <= cutoff
    }

    if not td_prices or not ap_prices:
        raise RuntimeError(f"{symbol}: one price history is empty")

    # Use the same calculation implementation on each native history.
    def calculate(prices):
        bars = [
            SimpleNamespace(timestamp=timestamp, close=close)
            for timestamp, close in sorted(prices.items())
        ]

        return {
            row["datetime"]: row["rsi"]
            for row in alpaca_client._calculate_rsi(
                bars, period, "1min"
            )
        }

    td_local = calculate(td_prices)
    ap_local = calculate(ap_prices)

    td_api = {
        row["datetime"]: float(row["rsi"])
        for row in td_api_history
    }

    comparable = td_api.keys() & td_local.keys() & ap_local.keys()

    if not comparable:
        raise RuntimeError(f"{symbol}: no common diagnostic timestamp")

    target = max(comparable)

    logging.info(
        "%s diagnostic at %s: "
        "TD API=%.6f | local(TD closes)=%.6f | "
        "local(Alpaca closes)=%.6f",
        symbol,
        target,
        td_api[target],
        td_local[target],
        ap_local[target],
    )

    logging.info(
        "%s price histories: TD=%d bars [%s through %s]; "
        "Alpaca=%d bars [%s through %s]",
        symbol,
        len(td_prices),
        min(td_prices).isoformat(),
        max(td_prices).isoformat(),
        len(ap_prices),
        min(ap_prices).isoformat(),
        max(ap_prices).isoformat(),
    )

    # Test initialization sensitivity independently for each provider.
    for label, prices in (
        ("Twelve Data", td_prices),
        ("Alpaca", ap_prices),
    ):
        items = sorted(prices.items())

        for count in (300, 600, 1200):
            if len(items) < count:
                continue

            values = calculate(dict(items[-count:]))

            if target in values:
                logging.info(
                    "%s %s initialization test: "
                    "%d input bars -> RSI at %s = %.6f",
                    symbol,
                    label,
                    count,
                    target,
                    values[target],
                )

    # Compare the last 120 minutes through the common target.
    target_dt = dt.datetime.strptime(
        target, "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=NY)

    window_start = target_dt - dt.timedelta(minutes=120)

    timestamps = sorted(
        timestamp
        for timestamp in td_prices.keys() | ap_prices.keys()
        if window_start <= timestamp <= target_dt
    )

    records = []

    for timestamp in timestamps:
        td_close = td_prices.get(timestamp)
        ap_close = ap_prices.get(timestamp)

        delta = (
            ap_close - td_close
            if td_close is not None and ap_close is not None
            else None
        )

        records.append({
            "datetime": timestamp.isoformat(),
            "td_close": td_close,
            "alpaca_close": ap_close,
            "alpaca_minus_td": delta,
        })

    missing_td = sum(row["td_close"] is None for row in records)
    missing_ap = sum(row["alpaca_close"] is None for row in records)
    different = [
        row for row in records
        if row["alpaca_minus_td"] is not None
        and abs(row["alpaca_minus_td"]) > 1e-8
    ]

    logging.info(
        "%s last 120 minutes: missing TD=%d, missing Alpaca=%d, "
        "different closes=%d",
        symbol,
        missing_td,
        missing_ap,
        len(different),
    )

    for row in different[-10:]:
        logging.info(
            "%s close comparison %s: "
            "TD=%.8f Alpaca=%.8f difference=%+.8f",
            symbol,
            row["datetime"],
            row["td_close"],
            row["alpaca_close"],
            row["alpaca_minus_td"],
        )

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{symbol}_minute_closes.csv"

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "datetime",
                "td_close",
                "alpaca_close",
                "alpaca_minus_td",
            ],
        )
        writer.writeheader()
        writer.writerows(records)

    logging.info("Saved RSI price diagnostic: %s", path.resolve())

    return {
        "datetime": target,
        "td_api_rsi": td_api[target],
        "td_local_rsi": td_local[target],
        "alpaca_local_rsi": ap_local[target],
    }