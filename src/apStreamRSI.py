import asyncio
import logging
import math
import os
import sys
from datetime import datetime, timedelta, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from lib.logger_init import initialize_logging
from lib.utils import JsonIniParser

initialize_logging(default_level=logging.INFO)
MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = clock_time(9, 30)
CLOSE_TIME = clock_time(16, 0)

# "since_open": cumulative gains/losses from observed minute closes.
# "wilder": standard Wilder smoothing, reset each session.
#MODE = "since_open"
MODE = "wilder"
RSI_PERIOD = 14

FEED = DataFeed.IEX

class SessionRSI:
    """Maintain independent RSI state for one symbol."""

    @property
    def is_ready(self):
        """True when this tracker has produced an RSI value."""
        return self.latest is not None

    @property
    def warmup_bars_remaining(self):
        """
        Number of additional observed closes required for a first RSI.

        since_open requires 2 closes.
        Wilder requires period + 1 closes.
        """
        closes_seen = (
            self.changes + 1
            if self.previous_close is not None
            else 0
        )

        required = (
            self.period + 1
            if self.mode == "wilder"
            else 2
        )

        return max(0, required - closes_seen)

    def warmup_session(self, historical_client, through=None):
        """
        Replay completed regular-session minute bars for one calendar day.

        through:
            Timezone-aware exclusive cutoff. Only minute bars that have
            finished by this cutoff are used.

            Defaults to the start of the current New York minute.

        Returns:
            Number of newly processed historical bars.

        Historical observations update tracker state but are not passed
        to handle_rsi().
        """
        now = datetime.now(MARKET_TZ)

        if through is None:
            through = now
        elif through.tzinfo is None or through.utcoffset() is None:
            raise ValueError("through must be timezone-aware")

        through = min(through.astimezone(MARKET_TZ), now)
        cutoff = through.replace(second=0, microsecond=0)
        day = cutoff.date()

        # Do not rewind a tracker to an earlier session.
        if self.session_date is not None and day < self.session_date:
            return 0

        if day.weekday() >= 5:
            return 0

        session_open = datetime.combine(
            day, OPEN_TIME, tzinfo=MARKET_TZ
        )
        session_close = datetime.combine(
            day, CLOSE_TIME, tzinfo=MARKET_TZ
        )

        end_exclusive = min(cutoff, session_close)

        if end_exclusive <= session_open:
            return 0

        start = session_open

        if (
            self.session_date == day
            and self.last_timestamp is not None
        ):
            start = max(
                start,
                self.last_timestamp + timedelta(minutes=1),
            )

        if start >= end_exclusive:
            return 0

        request = StockBarsRequest(
            symbol_or_symbols=self.symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end_exclusive - timedelta(microseconds=1),
            feed=FEED,
            adjustment=Adjustment.RAW,
            sort=Sort.ASC,
        )

        response = historical_client.get_stock_bars(request)
        returned_bars = response.data.get(self.symbol, [])

        # Validate/filter/sort before replaying any bars.
        unique = {}

        for bar in returned_bars:
            timestamp = bar.timestamp.astimezone(MARKET_TZ)

            if not start <= timestamp < end_exclusive:
                continue

            if timestamp + timedelta(minutes=1) > end_exclusive:
                continue

            close = float(bar.close)
            if not math.isfinite(close) or close <= 0:
                raise ValueError(
                    f"Invalid historical close for {self.symbol}: "
                    f"{bar.close!r}"
                )

            unique[timestamp] = bar

        processed = 0

        for timestamp in sorted(unique):
            previous_timestamp = self.last_timestamp
            self.process_bar(unique[timestamp])

            if self.last_timestamp != previous_timestamp:
                processed += 1

        if not unique:
            logging.warning(
                "%s no historical bars returned for %s through %s",
                self.symbol,
                start.isoformat(),
                end_exclusive.isoformat(),
            )

        return processed

    def log_warmup_status(self, loaded):
        if self.is_ready:
            logging.info(
                "%s warm-up: loaded=%d changes=%d "
                "latest=%s RSI[%s]=%.2f",
                self.symbol,
                loaded,
                self.changes,
                self.latest["datetime"],
                self.mode,
                self.latest["rsi"],
            )
        else:
            logging.info(
                "%s warm-up: loaded=%d ready=False "
                "additional_closes_required=%d",
                self.symbol,
                loaded,
                self.warmup_bars_remaining,
            )

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

    historical_client = StockHistoricalDataClient(
        api_key=api_key,
        secret_key=secret_key,
    )

    # Stage 1: preload today's completed bars before connecting.
    for symbol, tracker in trackers.items():
        try:
            loaded = tracker.warmup_session(historical_client)
            tracker.log_warmup_status(loaded)
        except Exception:
            logging.exception(
                "%s startup warm-up failed; "
                "will retry when a live bar arrives",
                symbol,
            )

    stream = StockDataStream(
        api_key=api_key,
        secret_key=secret_key,
        feed=FEED,
    )

    # Each symbol needs one catch-up after the stream connects.
    initial_catchup_pending = set(symbols)

    # Serialize access to the shared historical client.
    history_lock = None

    async def on_bar(bar):
        nonlocal history_lock

        tracker = trackers.get(bar.symbol)
        if tracker is None:
            return

        timestamp = bar.timestamp.astimezone(MARKET_TZ)

        if timestamp.weekday() >= 5:
            return

        if not OPEN_TIME <= timestamp.time() < CLOSE_TIME:
            return

        # Ignore old or duplicate live bars before fetching history.
        if tracker.session_date is not None:
            if timestamp.date() < tracker.session_date:
                return

            if (
                timestamp.date() == tracker.session_date
                and tracker.last_timestamp is not None
                and timestamp <= tracker.last_timestamp
            ):
                return

        if history_lock is None:
            history_lock = asyncio.Lock()

        try:
            new_session = tracker.session_date != timestamp.date()

            gap_detected = (
                tracker.last_timestamp is not None
                and timestamp.date() == tracker.session_date
                and timestamp - tracker.last_timestamp
                > timedelta(minutes=1)
            )

            needs_catchup = (
                bar.symbol in initial_catchup_pending
                or new_session
                or gap_detected
            )

            if needs_catchup:
                # Only replay bars BEFORE this live bar.
                # Run blocking HTTP outside the stream's event loop.
                async with history_lock:
                    loaded = await asyncio.to_thread(
                        tracker.warmup_session,
                        historical_client,
                        through=timestamp,
                    )

                initial_catchup_pending.discard(bar.symbol)

                if loaded:
                    logging.info(
                        "%s catch-up loaded %d historical bars",
                        bar.symbol,
                        loaded,
                    )

            observation = tracker.process_bar(bar)

            if observation is not None:
                handle_rsi(observation)

        except Exception:
            # Do not process this live bar after a failed catch-up.
            # The next live bar will retry historical recovery.
            initial_catchup_pending.add(bar.symbol)

            logging.exception(
                "%s warm-up/live processing failed; "
                "will retry catch-up on the next live bar",
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