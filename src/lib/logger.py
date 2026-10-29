import logging
from datetime import datetime
from pathlib import Path

def initialize_logging(default_level=logging.INFO):
    """
    Configures root logging formats and silences noisy library submodules.
    Should be called immediately upon application startup.
    """
    logging.basicConfig(
        level=default_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    # Silence specific noisy utility modules immediately
    logging.getLogger("lib.utils").setLevel(logging.WARNING)
    logging.getLogger("alpaca").setLevel(logging.WARNING)
    logging.getLogger("utils").setLevel(logging.WARNING)

import sys  # Make sure sys is imported at the top of lib/logger.py

import sys
from pathlib import Path
from datetime import datetime
import logging

def setup_symbol_logger(symbol: str) -> logging.Logger:
    """Creates a dedicated, isolated logger for a specific symbol that logs 
    to a subfolder disk path and the console terminal window.
    """
    logger = logging.getLogger(f"rsi_logger_{symbol}")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Prevent dumping to the root terminal logger

    # Clear old handlers if re-initialized
    if logger.handlers:
        logger.handlers.clear()

    # Define and build the target directory path safely
    log_dir = Path("./logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # Dynamic filename locked inside the target path: ./log/AAPL-20261029.log
    timestamp = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"{symbol}-{timestamp}.log"
    
    # 1. FILE HANDLER (Writes to disk inside the log directory)
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    logger.addHandler(file_handler)

    # 2. CONSOLE HANDLER (Outputs directly to terminal screen)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        f'%(asctime)s - [{symbol}] - %(levelname)s - %(message)s'
    ))
    logger.addHandler(console_handler)

    return logger
