import json
import sys
from typing import Any, Dict, Optional
from alpaca.trading import TradingClient
from alpaca.data.historical import StockHistoricalDataClient


class JsonIniParser:
    def __init__(self, filepath: str):
        """Initializes the parser with a path to the configuration file."""
        #print(f"--? Initializing JsonIniParser with file: {filepath}")
        self.filepath = filepath
        # Internal structure: { "section_name": { "key": value } }
        self.data: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        """Reads and parses the custom JSON-INI file from disk."""
        self.data = {}
        current_section: Optional[str] = None
        #print(f"!!? Loading configuration from: {self.filepath}")
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                #print(f"? Successfully read {len(lines)} lines from configuration file.")
        except FileNotFoundError:
            # If the file doesn't exist yet, we start with an empty configuration
            return
        #print(f"? Successfully read {len(lines)} lines from configuration file.")
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            #print(f"Line {line_num}: Processing: '{line}'")
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
                #print(f"Line {line_num}: Parsed [{current_section}] {key} = {parsed_value}")
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
        #print(f"? Configuration loaded successfully:\n{json.dumps(self.data, indent=4)}")
        self.ENVIRONMENT = self.get("alpaca_api", "environment", default="paper")
        self.CREDENTIALS = self.get("alpaca_api", "credentials")
        
        if not self.CREDENTIALS or "api_key" not in self.CREDENTIALS or "secret_key" not in self.CREDENTIALS:
            print("? Error: Missing credentials dictionary in your config file.")
            sys.exit(1)
            
        # Bind them directly to the object instance
        self.ALPACA_KEY = self.CREDENTIALS["api_key"]
        self.ALPACA_SECRET = self.CREDENTIALS["secret_key"]
        self.IS_PAPER = (self.ENVIRONMENT == "paper")
        self.NUC_IP = self.get("llm_configuration", "ip_address", default="192.168.0.178")
        #print(f"? Configuration loaded successfully.   {self}")

    def mk_alpaca_trading_client(self) -> 'TradingClient':
        try:
            # Load the configuration variables into the instance
            self.load_globals()
        except Exception as ce:
            print(f"? Failed to parse configuration file: {ce}")
            sys.exit(1)

        print(f"Paper Trading Mode: {self.IS_PAPER}")
        # ==========================================
        # 2. INITIALIZE ALPACA TRADING CLIENT
        # ==========================================
        try:
            trading_client = TradingClient(self.ALPACA_KEY, self.ALPACA_SECRET, paper={self.IS_PAPER})
        except Exception as e:
            print(f"Critical Error loading Alpaca configuration or initializing client: {e}")
            sys.exit(1)

        return trading_client

    def mk_alpaca_historical_client(self) -> 'StockHistoricalDataClient':
        try:
            # Load the configuration variables into the instance
            self.load_globals()
        except Exception as ce:
            print(f"? Failed to parse configuration file: {ce}")
            sys.exit(1)

        print(f"Paper Trading Mode: {self.IS_PAPER}")
        # ==========================================
        # 2. INITIALIZE ALPACA HISTORICAL CLIENT
        # ==========================================
        try:
            stock_historical_data_client = StockHistoricalDataClient(api_key=self.ALPACA_KEY, secret_key=self.ALPACA_SECRET)
        except Exception as e:
            print(f"Critical Error loading Alpaca configuration or initializing client: {e}")
            sys.exit(1)

        return stock_historical_data_client
