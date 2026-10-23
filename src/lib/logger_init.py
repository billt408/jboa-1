import logging

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
