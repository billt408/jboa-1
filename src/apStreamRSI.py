import logging
import math
import os
import sys
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from lib.logger_init import initialize_logging
from lib.utils import JsonIniParser

initialize_logging(default_level=logging.INFO)
MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = clock_time(9, 30)
CLOSE_TIME = clock_time(16, 0)

# "since_open": cumulative gains/losses from observed minute closes.
# "wilder": standard Wilder smoothing, reset each session.
MODE = "since_open"
RSI_PERIOD = 14

FEED = DataFeed.IEX

class SessionRSI:
    """Maintain independent RSI state for one symbol."""

    def __init__(self, symbol, mode="since_open", period=14):
        if mode not in {"since_open", "wilder"}:
            raise ValueError(f"Unknown RSI mode: {mode}")

        if (
            isinstance(period, bool)
            or not isinstance(period, int)
            or period < 2
        ):
            raise ValueError("RSI period must be an integer >= 2")

        self.symbol = symbol
        self.mode = mode
        self.period = period

        self.session_date = None
        self.last_timestamp = None

        self.previous_close = None
        self.changes = 0
        self.gain_sum = 0.0
        self.loss_sum = 0.0
        self.average_gain = None
        self.average_loss = None

        # Most recent calculated observation, or None during warm-up.
        self.latest = None

    def reset_session(self, day):
        self.session_date = day
        self.last_timestamp = None

        self.previous_close = None
        self.changes = 0
        self.gain_sum = 0.0
        self.loss_sum = 0.0
        self.average_gain = None
        self.average_loss = None
        self.latest = None

        logging.info("%s starting session %s", self.symbol, day)

    def calculate_rsi(self, close):
        if self.previous_close is None:
            self.previous_close = close
            return None

        difference = close - self.previous_close
        self.previous_close = close

        gain = max(difference, 0.0)
        loss = max(-difference, 0.0)

        self.changes += 1
        self.gain_sum += gain
        self.loss_sum += loss

        if self.mode == "since_open":
            up = self.gain_sum
            down = self.loss_sum

        else:  # wilder
            if self.changes < self.period:
                return None

            if self.changes == self.period:
                self.average_gain = self.gain_sum / self.period
                self.average_loss = self.loss_sum / self.period
            else:
                self.average_gain = (
                    self.average_gain * (self.period - 1) + gain
                ) / self.period

                self.average_loss = (
                    self.average_loss * (self.period - 1) + loss
                ) / self.period

            up = self.average_gain
            down = self.average_loss

        if up == 0.0 and down == 0.0:
            return 50.0

        if down == 0.0:
            return 100.0

        return 100.0 - 100.0 / (1.0 + up / down)

    def process_bar(self, bar):
        """
        Process a new minute bar.

        Return an RSI observation dictionary when available,
        otherwise return None.
        """
        timestamp = bar.timestamp.astimezone(MARKET_TZ)

        if timestamp.weekday() >= 5:
            return None

        if not OPEN_TIME <= timestamp.time() < CLOSE_TIME:
            return None

        close = float(bar.close)
        if not math.isfinite(close) or close <= 0:
            raise ValueError(
                f"Invalid close for {self.symbol}: {bar.close!r}"
            )

        # Ignore delayed bars from a previous session.
        if (
            self.session_date is not None
            and timestamp.date() < self.session_date
        ):
            return None

        if timestamp.date() != self.session_date:
            self.reset_session(timestamp.date())

        # Do not process duplicates or revise already-used bars.
        if (
            self.last_timestamp is not None
            and timestamp <= self.last_timestamp
        ):
            return None

        if self.last_timestamp is not None:
            elapsed = (timestamp - self.last_timestamp).total_seconds()
            if elapsed > 60:
                logging.warning(
                    "%s gap of %.0f minutes between observed bars; "
                    "missing bars are not backfilled",
                    self.symbol,
                    elapsed / 60,
                )

        self.last_timestamp = timestamp
        value = self.calculate_rsi(close)

        if value is None:
            logging.info(
                "%s minute=%s RSI[%s]=warming-up changes=%d",
                self.symbol,
                timestamp.isoformat(),
                self.mode,
                self.changes,
            )
            return None

        self.latest = {
            "symbol": self.symbol,
            "datetime": timestamp.isoformat(),
            "rsi": value,
            "mode": self.mode,
            "period": self.period if self.mode == "wilder" else None,
            "changes": self.changes,
        }

        return self.latest.copy()


def load_settings():
    try:
        config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
        config.load_globals()
    except Exception as ce:
        logging.error(f"? Failed to parse configuration file: {ce}")
        sys.exit(1)

    try:
        target_symbols = config.get("symbols", "target_symbols")
    except Exception as e:
        logging.error(f"Critical Error initializing symbols: {e}")
        sys.exit(2)        

    try:
        credentials = config.CREDENTIALS
        if not credentials or "api_key" not in credentials or "secret_key" not in credentials:
            print("? Error: Missing credentials dictionary in your config.json.ini file.")
            sys.exit(1)
        alpaca_api_key = config.CREDENTIALS["api_key"]
        alpaca_secret_key = config.CREDENTIALS["secret_key"]
        logging.debug(f"Using API key: {alpaca_api_key}")
    except Exception as e:
        logging.error(f"Critical Error loading Alpaca configuration or initializing twelvedata api: {e}")
        sys.exit(2)        

    if isinstance(target_symbols, str):
        symbols = [
            symbol.strip().upper()
            for symbol in target_symbols.split(",")
            if symbol.strip()
        ]
    elif isinstance(target_symbols, (list, tuple)):
        if any(
            not isinstance(symbol, str) or not symbol.strip()
            for symbol in target_symbols
        ):
            raise ValueError(
                "target_symbols must contain nonempty symbol strings"
            )
        symbols = [
            symbol.strip().upper()
            for symbol in target_symbols
        ]
    else:
        raise ValueError(
            "target_symbols must be a list or comma-separated string"
        )

    symbols = list(dict.fromkeys(symbols))

    if not symbols:
        raise ValueError("No target symbols configured")


    if not alpaca_api_key or not alpaca_secret_key:
        raise ValueError("Alpaca API key and secret key are required")

    return symbols, alpaca_api_key, alpaca_secret_key


def handle_rsi(observation):
    """
    Consume each calculated RSI observation.

    Add MQTT publishing, database writes, or application callbacks here.
    """
    logging.info(
        "%s minute=%s RSI[%s]=%.2f changes=%d",
        observation["symbol"],
        observation["datetime"],
        observation["mode"],
        observation["rsi"],
        observation["changes"],
    )


def main():
    try:
        symbols, api_key, secret_key = load_settings()
    except Exception:
        logging.exception("Failed to load configuration")
        return 2

    trackers = {
        symbol: SessionRSI(
            symbol=symbol,
            mode=MODE,
            period=RSI_PERIOD,
        )
        for symbol in symbols
    }

    stream = StockDataStream(
        api_key=api_key,
        secret_key=secret_key,
        feed=FEED,
    )

    async def on_bar(bar):
        tracker = trackers.get(bar.symbol)
        if tracker is None:
            return

        try:
            observation = tracker.process_bar(bar)

            if observation is not None:
                handle_rsi(observation)

        except Exception:
            logging.exception(
                "Failed to process minute bar for %s",
                bar.symbol,
            )

    stream.subscribe_bars(on_bar, *symbols)

    logging.info(
        "Starting Alpaca stream: symbols=%s feed=%s mode=%s period=%d",
        ", ".join(symbols),
        FEED.value,
        MODE,
        RSI_PERIOD,
    )
    logging.info(
        "No historical backfill: calculations begin with the first "
        "received regular-session minute bar."
    )

    try:
        stream.run()
    except KeyboardInterrupt:
        logging.info("Stopping")
    except Exception:
        logging.exception("Alpaca stream stopped unexpectedly")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())