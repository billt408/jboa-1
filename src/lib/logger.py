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

def setup_symbol_logger(symbol: str) -> logging.Logger:
    """Creates a dedicated, isolated logger for a specific symbol."""
    logger = logging.getLogger(f"rsi_logger_{symbol}")
    logger.setLevel(logging.INFO)
    logger.propagate = False # Prevent dumping to the root terminal logger

    # Clear old handlers if re-initialized
    if logger.handlers:
        logger.handlers.clear()

    # Dynamic filename: AAPL-20261029.log
    timestamp = datetime.now().strftime("%Y%m%d")
    log_file = Path(f"{symbol}-{timestamp}.log")
    
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    logger.addHandler(file_handler)
    return logger
