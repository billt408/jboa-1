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

from lib.logger import initialize_logging, setup_symbol_logger
from lib.utils import JsonIniParser

initialize_logging(default_level=logging.INFO)

MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = clock_time(9, 30)
CLOSE_TIME = clock_time(16, 0)

# "since_open": cumulative gains/losses from observed minute closes.
# "wilder": standard Wilder smoothing, reset each session.
# MODE = "since_open"
MODE = "wilder"
RSI_PERIOD = 14
FEED = DataFeed.IEX


class SessionRSI:
    """Maintain independent RSI state for one symbol."""

    def __init__(self, symbol, mode="since_open", period=14):
        if mode not in {"since_open", "wilder"}:
            raise ValueError(f"Unknown RSI mode: {mode}")
        if isinstance(period, bool) or not isinstance(period, int) or period < 2:
            raise ValueError("RSI period must be an integer >= 2")

        self.symbol = symbol
        self.mode = mode
        self.period = period
        self.logger = setup_symbol_logger(symbol)
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

    @property
    def is_ready(self):
        """True when this tracker has produced an RSI value."""
        return self.latest is not None

    @property
    def warmup_bars_remaining(self):
        """Number of additional observed closes required for a first RSI.

        since_open requires 2 closes.
        Wilder requires period + 1 closes.
        """
        closes_seen = self.changes + 1 if self.previous_close is not None else 0
        required = self.period + 1 if self.mode == "wilder" else 2
        return max(0, required - closes_seen)

    def warmup_session(self, historical_client, through=None):
        """Replay completed regular-session minute bars for one calendar day.

        through: Timezone-aware exclusive cutoff. Only minute bars that have
        finished by this cutoff are used. Defaults to the start of the current
        New York minute.

        Returns: Number of newly processed historical bars.
        Historical observations update tracker state but are not passed to handle_rsi().
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

        session_open = datetime.combine(day, OPEN_TIME, tzinfo=MARKET_TZ)
        session_close = datetime.combine(day, CLOSE_TIME, tzinfo=MARKET_TZ)

        end_exclusive = min(cutoff, session_close)
        if end_exclusive <= session_open:
            return 0

        start = session_open
        if self.session_date == day and self.last_timestamp is not None:
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
                    f"Invalid historical close for {self.symbol}: {bar.close!r}"
                )
            unique[timestamp] = bar

        processed = 0
        for timestamp in sorted(unique):
            previous_timestamp = self.last_timestamp
            self.process_bar(unique[timestamp])
            if self.last_timestamp != previous_timestamp:
                processed += 1

        if not unique:
            self.logger.warning(
                "No historical bars returned for %s through %s",
                start.isoformat(),
                end_exclusive.isoformat(),
            )
        return processed

    def log_warmup_status(self, loaded):
        if self.is_ready:
            self.logger.info(
                "Warm-up: loaded=%d changes=%d latest=%s RSI[%s]=%.2f",
                loaded,
                self.changes,
                self.latest["datetime"],
                self.mode,
                self.latest["rsi"],
            )
        else:
            self.logger.info(
                "Warm-up: loaded=%d ready=False additional_closes_required=%d",
                loaded,
                self.warmup_bars_remaining,
            )

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
        self.logger.info("Starting session %s", day)

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
        """Process a new minute bar, updates states, and returns calculated output."""
        timestamp = bar.timestamp.astimezone(MARKET_TZ)
        day = timestamp.date()

        if self.session_date != day:
            self.reset_session(day)

        self.last_timestamp = timestamp
        close_val = float(bar.close)
        rsi_val = self.calculate_rsi(close_val)

        if rsi_val is not None:
            self.latest = {
                "datetime": timestamp.isoformat(),
                "rsi": rsi_val,
                "close": close_val,
            }
            return self.latest
        return None


class StreamRouter:
    """Orchestrates multi-symbol live streaming using async worker tasks."""

    def __init__(self, symbols, historical_client, mode="wilder", period=14):
        self.symbols = symbols
        self.historical_client = historical_client
        self.trackers = {
            s: SessionRSI(s, mode=mode, period=period) for s in symbols
        }
        self.queues = {s: asyncio.Queue() for s in symbols}

    async def start(self, stream_client: StockDataStream):
        # Concurrently perform historical lookup warm-ups without stalling the async event loop
        warmup_tasks = []
        for symbol, tracker in self.trackers.items():
            logging.info("Warming up historical session for %s...", symbol)
            # Offload the blocking SDK request to network-bound thread pools
            task = asyncio.to_thread(tracker.warmup_session, self.historical_client)
            warmup_tasks.append((tracker, task))

        # Await the execution of all background threads concurrently
        for tracker, task in warmup_tasks:
            loaded = await task
            tracker.log_warmup_status(loaded)

        # Spin up concurrent worker loops for each symbol line
        worker_tasks = [
            asyncio.create_task(self.consume_symbol_bars(s)) for s in self.symbols
        ]

        # Define websocket callback to safely sort bars into target async queues
        async def handle_stream_bar(bar):
            symbol = bar.symbol
            if symbol in self.queues:
                await self.queues[symbol].put(bar)

        logging.info("Subscribing stream client to symbols: %s", self.symbols)
        stream_client.subscribe_bars(handle_stream_bar, *self.symbols)

        # Execute streaming task alongside tracking consumers indefinitely
        # Switched from private method ._run_forever() to public .run_forever()
        await asyncio.gather(stream_client.run_forever(), *worker_tasks)

    async def consume_symbol_bars(self, symbol: str):
        queue = self.queues[symbol]
        tracker = self.trackers[symbol]
        while True:
            bar = await queue.get()
            try:
                observation = tracker.process_bar(bar)
                if observation:
                    tracker.logger.info("Real-time RSI update: %s", observation)
            except Exception as e:
                tracker.logger.error(
                    "Exception handling live bar frame: %s", e, exc_info=True
                )
            finally:
                queue.task_done()


def load_settings():
    try:
        config = JsonIniParser(
            str(Path(__file__).resolve().parent / "lib" / "parms.ini")
        )
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
        if (
            not credentials
            or "api_key" not in credentials
            or "secret_key" not in credentials
        ):
            print(
                "? Error: Missing credentials dictionary in your config.json.ini file."
            )
            sys.exit(1)
        alpaca_api_key = config.CREDENTIALS["api_key"]
        alpaca_secret_key = config.CREDENTIALS["secret_key"]
        logging.debug(f"Using API key: {alpaca_api_key}")
    except Exception as e:
        logging.error(
            f"Critical Error loading Alpaca configuration or initializing twelvedata api: {e}"
        )
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
            raise ValueError("target_symbols must contain nonempty symbol strings")
        symbols = [symbol.strip().upper() for symbol in target_symbols]
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


async def main():
    try:
        symbols, api_key, secret_key = load_settings()
    except Exception:
        logging.exception("Failed to load configuration")
        return 2

    historical_client = StockHistoricalDataClient(
        api_key=api_key, secret_key=secret_key
    )
    stream_client = StockDataStream(api_key=api_key, secret_key=secret_key)

    # Added missing comma between arguments here
    router = StreamRouter(
        symbols=symbols,
        historical_client=historical_client,
        mode=MODE,
        period=RSI_PERIOD,
    )
    await router.start(stream_client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Application cleanly interrupted by user.")
        sys.exit(0)
