import asyncio
import logging
import math
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
        """Replay completed regular-session minute bars for one session.

        Args:
            historical_client:
                StockHistoricalDataClient configured with raw_data=False.

            through:
                Optional timezone-aware datetime used as an exclusive
                cutoff. Rounded down to the start of its minute and
                capped at the current time.

                A bar starting exactly at the cutoff is not replayed.

        Returns:
            Number of newly processed historical bars.

        Behavior:
            - Uses the configured weekday, OPEN_TIME/CLOSE_TIME window.
            - Never rewinds to an older session.
            - Continues after the last accepted bar in the same session.
            - Validates the entire returned batch before replaying it.
            - Deduplicates timestamps and replays in ascending order.
            - Updates tracker state without emitting live observations.

        Limitations:
            - Does not implement a holiday/early-close calendar.
            - Does not prove that historical data is complete.
            - Does not revise bars already accepted by the tracker.
            - Fetching and replay are synchronous; callers must serialize
              access to this tracker.
        """
        minute = timedelta(minutes=1)
        now = datetime.now(MARKET_TZ)

        # Validate and normalize the requested cutoff.
        if through is None:
            through = now
        else:
            if not isinstance(through, datetime):
                raise TypeError("through must be a datetime or None")

            if through.tzinfo is None or through.utcoffset() is None:
                raise ValueError("through must be timezone-aware")

            through = through.astimezone(MARKET_TZ)

        cutoff = min(through, now).replace(
            second=0,
            microsecond=0,
        )
        day = cutoff.date()

        # Never reset a tracker backward to an earlier session.
        if self.session_date is not None and day < self.session_date:
            return 0

        if day.weekday() >= 5:
            return 0

        session_open = datetime.combine(
            day,
            OPEN_TIME,
            tzinfo=MARKET_TZ,
        )
        session_close = datetime.combine(
            day,
            CLOSE_TIME,
            tzinfo=MARKET_TZ,
        )

        end_exclusive = min(cutoff, session_close)

        if end_exclusive <= session_open:
            return 0

        start = session_open

        if self.session_date == day and self.last_timestamp is not None:
            start = max(
                session_open,
                self.last_timestamp + minute,
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

        self.logger.debug(
            "Historical request: symbol=%s start=%s end_exclusive=%s",
            self.symbol,
            start.isoformat(),
            end_exclusive.isoformat(),
        )

        try:
            response = historical_client.get_stock_bars(request)
            returned_bars = response.data.get(self.symbol, [])
        except Exception:
            self.logger.exception(
                "Historical request failed: symbol=%s start=%s "
                "end_exclusive=%s",
                self.symbol,
                start.isoformat(),
                end_exclusive.isoformat(),
            )
            raise

        # Validate the batch before modifying tracker state.
        unique = {}
        duplicate_count = 0
        filtered_count = 0

        for bar in returned_bars:
            if bar.symbol != self.symbol:
                raise ValueError(
                    f"Expected historical symbol {self.symbol}, "
                    f"received {bar.symbol!r}"
                )

            raw_timestamp = bar.timestamp

            if not isinstance(raw_timestamp, datetime):
                raise TypeError(
                    f"Invalid historical timestamp for {self.symbol}: "
                    f"{raw_timestamp!r}"
                )

            if (
                raw_timestamp.tzinfo is None
                or raw_timestamp.utcoffset() is None
            ):
                raise ValueError(
                    f"Historical timestamp must be timezone-aware "
                    f"for {self.symbol}: {raw_timestamp!r}"
                )

            timestamp = raw_timestamp.astimezone(MARKET_TZ)

            # Defensive filtering even if the API returns extra bars.
            if not start <= timestamp < end_exclusive:
                filtered_count += 1
                continue

            if timestamp.second != 0 or timestamp.microsecond != 0:
                raise ValueError(
                    f"Historical minute bar is not minute-aligned "
                    f"for {self.symbol}: {timestamp.isoformat()}"
                )

            # Only completed one-minute bars may be replayed.
            if timestamp + minute > end_exclusive:
                filtered_count += 1
                continue

            try:
                close = float(bar.close)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    f"Invalid historical close for {self.symbol}: "
                    f"{bar.close!r}"
                ) from exc

            if not math.isfinite(close) or close <= 0:
                raise ValueError(
                    f"Invalid historical close for {self.symbol}: "
                    f"{bar.close!r}"
                )

            if timestamp in unique:
                duplicate_count += 1

            # If the response contains duplicates, retain the last entry.
            unique[timestamp] = bar

        if not unique:
            self.logger.warning(
                "No usable historical bars: symbol=%s start=%s "
                "end_exclusive=%s filtered=%d",
                self.symbol,
                start.isoformat(),
                end_exclusive.isoformat(),
                filtered_count,
            )
            return 0

        processed = 0

        for timestamp in sorted(unique):
            previous_timestamp = self.last_timestamp

            # process_bar() remains the final state-update gate.
            self.process_bar(unique[timestamp])

            if self.last_timestamp != previous_timestamp:
                processed += 1

        self.logger.info(
            "Historical replay: symbol=%s processed=%d duplicates=%d "
            "filtered=%d first=%s last=%s cutoff=%s",
            self.symbol,
            processed,
            duplicate_count,
            filtered_count,
            min(unique).isoformat(),
            max(unique).isoformat(),
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
        """Process one valid, new regular-session minute bar.

        Duplicate and out-of-order bars are ignored.
        Invalid input raises before tracker state is changed.
        """
        if bar.symbol != self.symbol:
            raise ValueError(
                f"Expected symbol {self.symbol}, received {bar.symbol}"
            )

        raw_timestamp = bar.timestamp
        if not isinstance(raw_timestamp, datetime):
            raise TypeError(f"Bar timestamp must be a datetime: {raw_timestamp!r}")

        if (
            raw_timestamp.tzinfo is None  or raw_timestamp.utcoffset() is None
        ):
            raise ValueError(
                f"Bar timestamp must be timezone-aware: {raw_timestamp!r}"
            )

        timestamp = raw_timestamp.astimezone(MARKET_TZ)
        if timestamp.second != 0 or timestamp.microsecond != 0:
            raise ValueError(f"Minute bar timestamp is not minute-aligned: " f"{timestamp.isoformat()}")
        day = timestamp.date()

        # Reject older sessions BEFORE considering a session reset.
        if self.session_date is not None and day < self.session_date:
            return None

        # Matches the fixed regular-session window used by warmup_session().
        local_time = timestamp.time()
        if day.weekday() >= 5:
            return None

        if not OPEN_TIME <= local_time < CLOSE_TIME:
            return None

        # Do not apply duplicate bars or rewind within the current session.
        if (
            self.last_timestamp is not None
            and timestamp <= self.last_timestamp
        ):
            return None

        # Validate the close before resetting or updating any state.
        close_val = float(bar.close)
        if not math.isfinite(close_val) or close_val <= 0:
            raise ValueError(
                f"Invalid close for {self.symbol}: {bar.close!r}"
            )

        if self.session_date != day:
            self.reset_session(day)

        rsi_val = self.calculate_rsi(close_val)
        self.last_timestamp = timestamp

        if rsi_val is None:
            return None

        self.latest = {
            "datetime": timestamp.isoformat(),
            "rsi": rsi_val,
            "close": close_val,
        }

        return self.latest

class StreamRouter:
    """Route live bars and backfill missing intervals before processing."""

    def __init__(
        self,
        symbols,
        historical_client,
        mode="wilder",
        period=14,
    ):
        self.symbols = list(symbols)
        self.historical_client = historical_client

        self.trackers = {
            symbol: SessionRSI(
                symbol,
                mode=mode,
                period=period,
            )
            for symbol in self.symbols
        }

        self.queues = {
            symbol: asyncio.Queue()
            for symbol in self.symbols
        }

        # Historical requests share one client; keep access sequential.
        self.history_lock = asyncio.Lock()

    async def warmup_tracker(self, tracker, through=None):
        async with self.history_lock:
            return await asyncio.to_thread(
                tracker.warmup_session,
                self.historical_client,
                through=through,
            )

    async def start(self, stream_client: StockDataStream):
        # Establish initial state before live workers start.
        for symbol, tracker in self.trackers.items():
            logging.info(
                "Warming up historical session for %s...",
                symbol,
            )

            loaded = await self.warmup_tracker(tracker)
            tracker.log_warmup_status(loaded)

        async def handle_stream_bar(bar):
            queue = self.queues.get(bar.symbol)
            if queue is not None:
                await queue.put(bar)

        stream_client.subscribe_bars(
            handle_stream_bar,
            *self.symbols,
        )

        logging.info(
            "Subscribing stream client to symbols: %s",
            self.symbols,
        )

        worker_tasks = [
            asyncio.create_task(
                self.consume_symbol_bars(symbol),
                name=f"rsi-consumer-{symbol}",
            )
            for symbol in self.symbols
        ]

        # Private SDK API: retain compatibility checks when upgrading.
        stream_task = asyncio.create_task(
            stream_client._run_forever(),
            name="alpaca-stock-stream",
        )

        tasks = [stream_task, *worker_tasks]

        try:
            # Exit if a worker raises or the stream runner returns.
            done, _ = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                task.result()

        finally:
            for task in tasks:
                task.cancel()

            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

            await stream_client.close()

    async def consume_symbol_bars(self, symbol):
        queue = self.queues[symbol]
        tracker = self.trackers[symbol]

        while True:
            bar = await queue.get()

            try:
                # Validate incoming input before historical replay or mutation.
                if bar.symbol != symbol:
                    raise ValueError(
                        f"Expected symbol {symbol}, received {bar.symbol}"
                    )

                raw_timestamp = bar.timestamp
                if not isinstance(raw_timestamp, datetime):
                    raise TypeError(f"Bar timestamp must be a datetime: {raw_timestamp!r}")
                if (
                    raw_timestamp.tzinfo is None
                    or raw_timestamp.utcoffset() is None
                ):
                    raise ValueError(
                        f"Naive timestamp for {symbol}: {raw_timestamp!r}"
                    )

                timestamp = raw_timestamp.astimezone(MARKET_TZ)
                if timestamp.second != 0 or timestamp.microsecond != 0:
                    raise ValueError(f"Minute bar timestamp is not minute-aligned: " f"{timestamp.isoformat()}")
                day = timestamp.date()

                close_val = float(bar.close)
                if not math.isfinite(close_val) or close_val <= 0:
                    raise ValueError(
                        f"Invalid live close for {symbol}: {bar.close!r}"
                    )

                if day.weekday() >= 5:
                    continue

                if not OPEN_TIME <= timestamp.time() < CLOSE_TIME:
                    continue

                if (
                    tracker.session_date is not None
                    and day < tracker.session_date
                ):
                    continue

                if (
                    tracker.last_timestamp is not None
                    and timestamp <= tracker.last_timestamp
                ):
                    continue

                needs_backfill = (
                    tracker.session_date != day
                    or tracker.last_timestamp is None
                    or timestamp
                    > tracker.last_timestamp + timedelta(minutes=1)
                )

                if needs_backfill:
                    tracker.logger.info(
                        "Checking historical gap before live bar %s "
                        "(previous=%s)",
                        timestamp.isoformat(),
                        (
                            tracker.last_timestamp.isoformat()
                            if tracker.last_timestamp is not None
                            else None
                        ),
                    )

                    # Exclusive cutoff: replay only bars BEFORE this live bar.
                    loaded = await self.warmup_tracker(
                        tracker,
                        through=timestamp,
                    )

                    tracker.logger.info(
                        "Historical gap replay loaded=%d before=%s",
                        loaded,
                        timestamp.isoformat(),
                    )

                observation = tracker.process_bar(bar)

                if observation is not None:
                    tracker.logger.info(
                        "Real-time RSI update: %s",
                        observation,
                    )

            except Exception:
                tracker.logger.exception(
                    "Stopping after bar/backfill failure for %s",
                    symbol,
                )
                raise

            finally:
                queue.task_done()

def load_settings():
    config_path = (
        Path(__file__).resolve().parent / "lib" / "parms.ini"
    )

    try:
        config = JsonIniParser(str(config_path))
        config.load_globals()
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load configuration: {config_path}"
        ) from exc

    try:
        target_symbols = config.get("symbols", "target_symbols")
    except Exception as exc:
        raise RuntimeError(
            "Failed to read symbols.target_symbols"
        ) from exc

    # Normalize and validate configured symbols.
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
            "target_symbols must be a list, tuple, "
            "or comma-separated string"
        )

    try:
        credentials = config.CREDENTIALS
    except Exception as exc:
        raise RuntimeError(
            "Failed to read CREDENTIALS from configuration"
        ) from exc

    if not isinstance(credentials, dict):
        raise ValueError(
            "CREDENTIALS must be a dictionary containing "
            "'api_key' and 'secret_key'"
        )

    api_key = credentials.get("api_key")
    secret_key = credentials.get("secret_key")

    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(
            "CREDENTIALS.api_key must be a nonempty string"
        )

    if not isinstance(secret_key, str) or not secret_key.strip():
        raise ValueError(
            "CREDENTIALS.secret_key must be a nonempty string"
        )

    return symbols, api_key.strip(), secret_key.strip()

async def main():
    try:
        symbols, api_key, secret_key = load_settings()
    except Exception:
        logging.exception("Failed to load configuration")
        return 2

    historical_client = StockHistoricalDataClient(
        api_key=api_key, secret_key=secret_key
    )
    stream_client = StockDataStream(api_key=api_key, secret_key=secret_key, feed=FEED)

    # Added missing comma between arguments here
    router = StreamRouter(
        symbols=symbols,
        historical_client=historical_client,
        mode=MODE,
        period=RSI_PERIOD,
    )
    await router.start(stream_client)
    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Application interrupted by user.")
        exit_code = 0

    sys.exit(exit_code)