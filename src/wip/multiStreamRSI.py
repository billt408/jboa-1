"""Multi-provider session RSI: Alpaca, Tradier, and Twelve Data.

Dependencies: aiohttp, alpaca-py, plus the existing lib.utils.JsonIniParser.
Uses fixed weekday US regular-session hours, not an exchange calendar.
Historical recovery is best effort, not a completeness guarantee.
"""
import asyncio
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, time as clock_time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiohttp
from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from lib.utils import JsonIniParser

MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = clock_time(9, 30)
CLOSE_TIME = clock_time(16, 0)
MODE = "wilder"
RSI_PERIOD = 14
FEED = DataFeed.IEX
ROUTER_SWITCH = "MOST"  # ALPACA, TRADIER, TWELVEDATA, BOTH, ALL, MOST
FINALIZE_GRACE_SECONDS = 2.0
QUEUE_LIMIT = 20000
LOG_RAW_EVENTS = True
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid4().hex[:8]
LOG_ROOT = Path(__file__).resolve().parent / "logs"
MINUTE = timedelta(minutes=1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def make_logger(provider, symbol):
    logger = logging.getLogger(f"rsi.{RUN_ID}.{provider}.{symbol}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in symbol)
    directory = LOG_ROOT / provider.lower()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{safe}-{provider}-{RUN_ID}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logging.info("%s %s log: %s", provider, symbol, path)
    return logger


def normalized_time(value):
    if not isinstance(value, datetime):
        raise TypeError("Timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(MARKET_TZ)


def positive_price(value):
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Price must be finite and positive")
    return price


def regular_session(timestamp):
    return timestamp.weekday() < 5 and OPEN_TIME <= timestamp.time() < CLOSE_TIME


def parse_local(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=MARKET_TZ)
    return normalized_time(result)


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    close: float


class SessionRSI:
    def __init__(self, symbol, provider, mode=MODE, period=RSI_PERIOD):
        if mode not in {"since_open", "wilder"}:
            raise ValueError("Unknown RSI mode")
        if isinstance(period, bool) or not isinstance(period, int) or period < 2:
            raise ValueError("period must be an integer >= 2")
        self.symbol = symbol
        self.mode = mode
        self.period = period
        self.logger = make_logger(provider, symbol)
        self.session_date = None
        self.last_timestamp = None
        self.previous_close = None
        self.changes = 0
        self.gain_sum = self.loss_sum = 0.0
        self.average_gain = self.average_loss = None
        self.latest = None
        self.provider = provider

    def reset_session(self, day):
        self.session_date = day
        self.last_timestamp = self.previous_close = None
        self.changes = 0
        self.gain_sum = self.loss_sum = 0.0
        self.average_gain = self.average_loss = self.latest = None
        self.logger.info("SESSION_START date=%s", day)

    def process_bar(self, bar, source):
        """Apply one completed minute close to this provider's RSI."""
        timestamp = normalized_time(bar.timestamp)
        close = positive_price(bar.close)

        if timestamp.second or timestamp.microsecond:
            raise ValueError("Bar must be minute-aligned")

        if not regular_session(timestamp):
            return None

        if (
            self.session_date is not None
            and timestamp.date() < self.session_date
        ):
            return None

        if (
            self.last_timestamp is not None
            and timestamp <= self.last_timestamp
        ):
            return None

        if self.session_date != timestamp.date():
            self.reset_session(timestamp.date())

        value = None

        if self.previous_close is not None:
            change = close - self.previous_close
            gain = max(change, 0.0)
            loss = max(-change, 0.0)

            self.changes += 1
            self.gain_sum += gain
            self.loss_sum += loss

            up = down = None

            if self.mode == "since_open":
                up = self.gain_sum
                down = self.loss_sum

            elif self.changes == self.period:
                self.average_gain = self.gain_sum / self.period
                self.average_loss = self.loss_sum / self.period
                up = self.average_gain
                down = self.average_loss

            elif self.changes > self.period:
                self.average_gain = (
                    self.average_gain * (self.period - 1) + gain
                ) / self.period

                self.average_loss = (
                    self.average_loss * (self.period - 1) + loss
                ) / self.period

                up = self.average_gain
                down = self.average_loss

            if up is not None:
                if up == 0.0 and down == 0.0:
                    value = 50.0
                elif down == 0.0:
                    value = 100.0
                else:
                    value = 100.0 - 100.0 / (1.0 + up / down)

        self.previous_close = close
        self.last_timestamp = timestamp

        observation = None

        if value is not None:
            observation = {
                "provider": self.provider,
                "symbol": self.symbol,
                "datetime": timestamp.isoformat(),
                "close": close,
                "rsi": value,
            }
            self.latest = observation

        message = (
            "MINUTE provider=%s symbol=%s source=%s timestamp=%s "
            "close=%.8f rsi=%s changes=%d"
        )

        arguments = (
            self.provider,
            self.symbol,
            source,
            timestamp.isoformat(),
            close,
            "warming-up" if value is None else f"{value:.2f}",
            self.changes,
        )

        self.logger.info(message, *arguments)

        if source != "REST_BACKFILL":
            logging.info(message, *arguments)

        return observation

class FatalProviderError(RuntimeError):
    pass


async def request_json(session, method, url, **kwargs):
    # Avoid logging URLs or exception text: query parameters may contain keys.
    for attempt in range(4):
        try:
            async with session.request(method, url, **kwargs) as response:
                if response.status in {401, 403}:
                    raise FatalProviderError(
                        f"HTTP {response.status}: "
                        "authentication/access rejected"
                    )
                if response.status == 429 or response.status >= 500:
                    if attempt == 3:
                        raise RuntimeError("Provider HTTP retries exhausted")
                    await asyncio.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                data = await response.json()
                if not isinstance(data, dict):
                    raise ValueError("Provider returned a non-object response")
                return data
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            if attempt == 3:
                raise RuntimeError("Provider network retries exhausted") from None
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("Provider request failed")

def sanitize_provider_event(value, secrets=()):
    """Redact credentials from provider messages before logging."""
    sensitive_keys = {
        "apikey",
        "api_key",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "authorization",
        "sessionid",
        "session_id",
        "url",
    }

    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if str(key).lower() in sensitive_keys
                else sanitize_provider_event(item, secrets)
            )
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            sanitize_provider_event(item, secrets)
            for item in value
        ]

    if isinstance(value, str):
        for secret in secrets:
            if isinstance(secret, str) and secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    return value

class ProviderRouter:
    def __init__(self, provider, symbols, credentials):
        self.provider = provider
        self.symbols = symbols
        self.credentials = credentials
        self.trackers = {s: SessionRSI(s, provider) for s in symbols}
        self.queues = {s: asyncio.Queue(maxsize=QUEUE_LIMIT) for s in symbols}
        self.logger = make_logger(provider, "_STREAM")
        self.history_lock = asyncio.Lock()
        self.historical = None
        self.stream = None
        if provider == "ALPACA":
            self.historical = StockHistoricalDataClient(
                credentials["alpaca_key"], credentials["alpaca_secret"])
            self.stream = StockDataStream(
                credentials["alpaca_key"], credentials["alpaca_secret"], feed=FEED)

    def enqueue(self, symbol, kind, value):
        # Fail visibly rather than silently dropping ticks/bars or blocking
        # the websocket reader indefinitely when a queue is full.
        try:
            self.queues[symbol].put_nowait((kind, value))
        except asyncio.QueueFull:
            raise RuntimeError(f"{self.provider} queue overflow for {symbol}") from None

    async def run(self):
        """Run this provider and attach its identity to failures."""
        self.logger.info(
            "PROVIDER_START provider=%s symbols=%s",
            self.provider,
            self.symbols,
        )

        try:
            return await self._run()

        except asyncio.CancelledError:
            raise

        except FatalProviderError as exc:
            # FatalProviderError messages are explicitly controlled
            # by our code and must never include keys or tokens.
            message = (
                f"provider={self.provider} "
                f"error_type=FatalProviderError detail={exc}"
            )

            self.logger.error("%s", message)
            raise FatalProviderError(message) from None

        except Exception as exc:
            # Do not include arbitrary exception text here:
            # HTTP exceptions can contain URLs with API keys.
            message = (
                f"provider={self.provider} "
                f"error_type={type(exc).__name__}"
            )

            self.logger.error("%s", message)
            raise RuntimeError(message) from exc

    async def _run(self):
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            tasks = [asyncio.create_task(self.consume(s)) for s in self.symbols]
            tasks.append(asyncio.create_task(
                self.run_alpaca() if self.provider == "ALPACA" else self.run_websocket()))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.stream is not None:
                    await self.stream.close()

    def alpaca_history(self, symbol, start, end):
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=start, end=end - timedelta(microseconds=1),
            feed=FEED, adjustment=Adjustment.RAW, sort=Sort.ASC)
        response = self.historical.get_stock_bars(request)
        return [Bar(b.timestamp, b.close) for b in response.data.get(symbol, [])]

    async def fetch_history(self, symbol, start, end):
        if self.provider == "ALPACA":
            # This thread fetches data only; it never mutates an RSI tracker.
            return await asyncio.to_thread(self.alpaca_history, symbol, start, end)
        if self.provider == "TWELVEDATA":
            payload = await request_json(
                self.session, "GET", "https://api.twelvedata.com/time_series",
                params={"symbol": symbol, "interval": "1min", "order": "asc",
                        "timezone": "America/New_York", "outputsize": 5000,
                        "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_date": (end - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
                        "apikey": self.credentials["twelvedata_key"]})
            if payload.get("status") == "error":
                raise FatalProviderError("Twelve Data historical API returned an error")
            rows = payload.get("values")
            if not isinstance(rows, list):
                raise ValueError("Twelve Data response missing values")
            return [Bar(parse_local(r["datetime"]), r["close"]) for r in rows]
        if self.provider == "TRADIER":
            payload = await request_json(
                self.session,
                "GET",
                "https://api.tradier.com/v1/markets/timesales",
                headers=self.tradier_headers(),
                params={
                    "symbol": symbol,
                    "interval": "1min",
                    "session_filter": "open",
                    "start": start.strftime("%Y-%m-%d %H:%M"),
                    "end": end.strftime("%Y-%m-%d %H:%M"),
                },
            )

            if payload.get("errors"):
                safe_errors = sanitize_provider_event(
                    payload["errors"],
                    secrets=tuple(self.credentials.values()),
                )
                raise FatalProviderError(
                    "Tradier historical request failed: "
                    + json.dumps(safe_errors, default=str)
                )

            if "series" not in payload:
                raise ValueError(
                    "Tradier historical response missing series"
                )

            series = payload["series"]

            if series is None:
                self.logger.warning(
                    "NO_HISTORY provider=TRADIER symbol=%s "
                    "start=%s end=%s",
                    symbol,
                    start.isoformat(),
                    end.isoformat(),
                )
                return []

            if not isinstance(series, dict):
                raise ValueError(
                    "Tradier historical series must be an object"
                )

            if "data" not in series:
                raise ValueError(
                    "Tradier historical response missing series.data"
                )

            rows = series["data"]

            if rows is None:
                return []

            if isinstance(rows, dict):
                rows = [rows]

            if not isinstance(rows, list):
                raise ValueError(
                    "Tradier historical series.data must be a list"
                )

            bars = []

            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(
                        "Tradier historical bar must be an object"
                    )

                # The interval-bar timestamp is expressed in epoch
                # seconds. Stream trade dates use milliseconds instead.
                if row.get("timestamp") is not None:
                    epoch = float(row["timestamp"])

                    if not math.isfinite(epoch):
                        raise ValueError(
                            "Invalid Tradier historical timestamp"
                        )

                    timestamp = datetime.fromtimestamp(
                        epoch,
                        tz=MARKET_TZ,
                    )
                else:
                    timestamp = parse_local(row["time"])

                bars.append(
                    Bar(
                        timestamp=timestamp,
                        close=positive_price(row["close"]),
                    )
                )
            return bars
        raise ValueError(
            f"Unsupported historical provider: {self.provider}"
        )
            
    async def recover(self, symbol, through):
        tracker = self.trackers[symbol]
        end = min(normalized_time(through), datetime.now(MARKET_TZ)).replace(second=0, microsecond=0)
        if end.weekday() >= 5:
            return
        day = end.date()
        if tracker.session_date is not None and day < tracker.session_date:
            return
        start = datetime.combine(day, OPEN_TIME, tzinfo=MARKET_TZ)
        end = min(end, datetime.combine(day, CLOSE_TIME, tzinfo=MARKET_TZ))
        if tracker.session_date == day and tracker.last_timestamp is not None:
            start = max(start, tracker.last_timestamp + MINUTE)
        if start >= end:
            return
        async with self.history_lock:
            bars = await self.fetch_history(symbol, start, end)
        unique = {}
        for bar in bars:
            timestamp = normalized_time(bar.timestamp)
            if not start <= timestamp < end:
                continue
            if timestamp.second or timestamp.microsecond:
                raise ValueError("Historical minute is not aligned")
            unique[timestamp] = Bar(timestamp, positive_price(bar.close))
        for timestamp in sorted(unique):
            tracker.process_bar(unique[timestamp], "REST_BACKFILL")
        tracker.logger.info("BACKFILL loaded=%d start=%s end_exclusive=%s best_effort=true",
                            len(unique), start.isoformat(), end.isoformat())

        ready = tracker.latest is not None

        required_changes = (
            tracker.period
            if tracker.mode == "wilder"
            else 1
        )

        additional_closes = (
            0
            if ready
            else max(
                0,
                required_changes
                - tracker.changes
                + (1 if tracker.previous_close is None else 0),
            )
        )

        logging.info(
            "WARMUP provider=%s symbol=%s loaded=%d "
            "ready=%s additional_closes=%d latest_rsi=%s",
            self.provider,
            symbol,
            len(unique),
            ready,
            additional_closes,
            (
                f"{tracker.latest['rsi']:.2f}"
                if ready
                else "warming-up"
            ),
        )
    async def consume(self, symbol):
        tracker = self.trackers[symbol]
        queue = self.queues[symbol]
        buckets = {}
        tick_count = 0

        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                item = None

            if item is not None:
                kind, value = item

                try:
                    if kind == "recover":
                        buckets.clear()
                        await self.recover(symbol, value)

                    elif kind == "bar":
                        timestamp = normalized_time(value.timestamp)

                        if regular_session(timestamp):
                            await self.accept_bar(
                                symbol,
                                value,
                                "ALPACA_BAR",
                            )

                    elif kind == "tick":
                        timestamp, price = value
                        timestamp = normalized_time(timestamp)
                        price = positive_price(price)

                        if regular_session(timestamp):
                            minute = timestamp.replace(
                                second=0,
                                microsecond=0,
                            )

                            is_new_minute = (
                                tracker.last_timestamp is None
                                or minute > tracker.last_timestamp
                            )

                            if is_new_minute:
                                existing = buckets.get(minute)

                                if (
                                    existing is None
                                    or timestamp >= existing[0]
                                ):
                                    buckets[minute] = (
                                        timestamp,
                                        price,
                                    )

                                tick_count += 1

                                if tick_count == 1:
                                    logging.info(
                                        "FIRST_SESSION_TICK "
                                        "provider=%s symbol=%s "
                                        "timestamp=%s price=%.8f",
                                        self.provider,
                                        symbol,
                                        timestamp.isoformat(),
                                        price,
                                    )

                    else:
                        raise ValueError(
                            f"Unknown queue event kind: {kind!r}"
                        )

                except Exception as exc:
                    # Log execution locations without exception strings
                    # that might contain an authenticated request URL.
                    self.logger.error(
                        "CONSUMER_FAILURE provider=%s symbol=%s "
                        "kind=%s error_type=%s",
                        self.provider,
                        symbol,
                        kind,
                        type(exc).__name__,
                    )
                    raise

                finally:
                    queue.task_done()

            # Do not finalize a minute while received ticks are still
            # waiting to be incorporated into its closing price.
            if not queue.empty():
                continue

            watermark = (
                datetime.now(MARKET_TZ)
                - timedelta(seconds=FINALIZE_GRACE_SECONDS)
            )

            completed = sorted(
                minute
                for minute in buckets
                if minute + MINUTE <= watermark
            )

            for minute in completed:
                _, close = buckets.pop(minute)

                # Historical recovery may already have accepted a
                # bucket's timestamp.
                if (
                    tracker.last_timestamp is not None
                    and minute <= tracker.last_timestamp
                ):
                    continue

                await self.accept_bar(
                    symbol,
                    Bar(minute, close),
                    f"{self.provider}_WS_MINUTE",
                )

    async def accept_bar(self, symbol, bar, source):
        tracker = self.trackers[symbol]
        timestamp = normalized_time(bar.timestamp)
        if tracker.last_timestamp is not None and timestamp <= tracker.last_timestamp:
            return
        if (tracker.session_date != timestamp.date() or tracker.last_timestamp is None
                or timestamp > tracker.last_timestamp + MINUTE):
            await self.recover(symbol, timestamp)
        tracker.process_bar(bar, source)

    async def run_alpaca(self):
        for symbol in self.symbols:
            self.enqueue(symbol, "recover", datetime.now(MARKET_TZ))
        async def callback(bar):
            tracker = self.trackers.get(bar.symbol)

            if tracker is None:
                return

            # The queued bar has not been processed yet. This RSI is
            # therefore the latest previously calculated minute RSI.
            latest = tracker.latest
            bar_timestamp = normalized_time(bar.timestamp)

            current_session_rsi = (
                latest is not None
                and tracker.session_date == bar_timestamp.date()
            )

            if LOG_RAW_EVENTS:
                tracker.logger.info(
                    "WS_BAR provider=ALPACA symbol=%s "
                    "timestamp=%s close=%.8f "
                    "previous_rsi[%s]=%s rsi_bar_timestamp=%s",
                    bar.symbol,
                    bar_timestamp.isoformat(),
                    float(bar.close),
                    tracker.mode,
                    (
                        f"{latest['rsi']:.2f}"
                        if current_session_rsi
                        else "warming-up"
                    ),
                    (
                        latest["datetime"]
                        if current_session_rsi
                        else "none"
                    ),
                )

            self.enqueue(
                bar.symbol,
                "bar",
                Bar(bar.timestamp, bar.close),
            )
            self.stream.subscribe_bars(callback, *self.symbols)
        # Alpaca public run() starts its own event loop. This application
        # already owns one; pin/test alpaca-py because this runner is private.
        await self.stream._run_forever()

    def tradier_headers(self):
        return {"Authorization": f"Bearer {self.credentials['tradier_token']}",
                "Accept": "application/json"}

    async def td_heartbeat(self, websocket):
        while True:
            await asyncio.sleep(10)
            await websocket.send_json({"action": "heartbeat"})

    async def run_websocket(self):
        delay = 1
        while True:
            heartbeat = None
            try:
                if self.provider == "TWELVEDATA":
                    url = "wss://ws.twelvedata.com/v1/quotes/price"
                    params = {"apikey": self.credentials["twelvedata_key"]}
                    subscription = {"action": "subscribe", "params": {"symbols": ",".join(self.symbols)}}
                else:
                    payload = await request_json(
                        self.session, "POST", "https://api.tradier.com/v1/markets/events/session",
                        headers=self.tradier_headers())
                    stream = payload.get("stream", {})
                    session_id = stream.get("sessionid")
                    if not session_id:
                        raise FatalProviderError("Tradier returned no streaming session ID")
                    url = "wss://ws.tradier.com/v1/markets/events"
                    params = None
                    subscription = {"symbols": self.symbols, "filter": ["trade"],
                                    "sessionid": session_id, "linebreak": True, "validOnly": True}
                async with self.session.ws_connect(url, params=params, heartbeat=20) as websocket:
                    await websocket.send_json(subscription)
                    cutoff = datetime.now(MARKET_TZ)
                    for symbol in self.symbols:
                        self.enqueue(symbol, "recover", cutoff)
                    self.logger.info("CONNECTED symbols=%s partial_connection_minute=true", self.symbols)
                    if self.provider == "TWELVEDATA":
                        heartbeat = asyncio.create_task(self.td_heartbeat(websocket))
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError("Websocket transport failed")
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        # Tradier may delimit multiple JSON events with newlines.
                        for line in message.data.splitlines():
                            if not line.strip():
                                continue
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                self.logger.warning("INVALID_JSON")
                                continue
                            self.handle_event(event)
                            delay = 1
                self.logger.warning("DISCONNECTED; recovering on reconnect")
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in {401, 403}:
                    raise FatalProviderError(
                        f"Websocket HTTP {exc.status}: "
                        "authentication/access rejected"
                    ) from None
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
                self.logger.warning("WS_RETRY error_type=%s", type(exc).__name__)
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    def handle_event(self, event):
        if not isinstance(event, dict):
            self.logger.warning("Ignoring non-object event")
            return
        is_tick = (event.get("event") == "price" if self.provider == "TWELVEDATA"
                   else event.get("type") == "trade")
        if not is_tick:
            safe_event = sanitize_provider_event(
                event,
                secrets=tuple(self.credentials.values()),
            )

            details = json.dumps(
                safe_event,
                separators=(",", ":"),
                default=str,
            )

            event_type = str(
                event.get("event", event.get("type", ""))
            ).lower()

            status = str(event.get("status", "")).lower()

            is_error = (
                event_type == "error"
                or status in {"error", "failed", "failure"}
            )

            log_method = (
                self.logger.error if is_error else self.logger.info
            )

            log_method(
                "WS_CONTROL provider=%s payload=%s",
                self.provider,
                details,
            )

            # Also put the event on the console, rather than requiring
            # inspection of the provider's isolated file logger.
            console_method = logging.error if is_error else logging.info

            console_method(
                "WS_CONTROL provider=%s payload=%s",
                self.provider,
                details,
            )

            if is_error:
                raise FatalProviderError(
                    f"Websocket rejected request: {details}"
                )

            return
        symbol = str(event.get("symbol", "")).upper()
        if symbol not in self.queues:
            return
        try:
            if self.provider == "TWELVEDATA":
                epoch, price = float(event["timestamp"]), positive_price(event["price"])
            else:
                epoch, price = float(event["date"]) / 1000.0, positive_price(event["price"])
            if not math.isfinite(epoch):
                raise ValueError("Nonfinite timestamp")
            timestamp = datetime.fromtimestamp(epoch, MARKET_TZ)
            if timestamp > datetime.now(MARKET_TZ) + timedelta(seconds=30):
                raise ValueError("Future timestamp")
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            self.trackers[symbol].logger.warning("INVALID_TICK")
            return
        tracker = self.trackers[symbol]
        latest = tracker.latest

        in_regular_session = regular_session(timestamp)

        same_session = (
            latest is not None
            and tracker.session_date == timestamp.date()
        )

        if not in_regular_session:
            rsi_status = "outside_regular_session"
        elif not same_session:
            rsi_status = "warming_up"
        else:
            rsi_status = "latest_completed_minute"

        rsi_text = (
            f"{latest['rsi']:.2f}"
            if same_session
            else "unavailable"
        )

        rsi_timestamp = (
            latest["datetime"]
            if same_session
            else "none"
        )

        if LOG_RAW_EVENTS:
            safe_event = sanitize_provider_event(
                event,
                secrets=tuple(self.credentials.values()),
            )

            tracker.logger.info(
                "WS_PRICE provider=%s symbol=%s "
                "tick_timestamp=%s price=%.8f "
                "accepted_for_rsi=%s rsi_status=%s "
                "latest_rsi[%s]=%s rsi_bar_timestamp=%s raw=%s",
                self.provider,
                symbol,
                timestamp.isoformat(),
                price,
                in_regular_session,
                rsi_status,
                tracker.mode,
                rsi_text,
                rsi_timestamp,
                json.dumps(
                    safe_event,
                    separators=(",", ":"),
                    default=str,
                ),
            )

        # Outside-session prices may be logged, but do not enter
        # the regular-session RSI calculation.
        if not in_regular_session:
            return

        self.enqueue(
            symbol,
            "tick",
            (timestamp, price),
        )
        
def load_tradier_token(config, credentials):
    """Load a token without exposing it in logs.

    Priority:
        1. TRADIER_TOKEN environment variable
        2. [tradier] tradier_token
        3. CREDENTIALS["tradier_token"]
    """
    value = os.getenv("TRADIER_TOKEN", "")
    source = "environment:TRADIER_TOKEN"

    if not isinstance(value, str) or not value.strip():
        try:
            value = config.get("tradier", "tradier_token")
        except Exception:
            value = ""

        source = "configuration:tradier.tradier_token"

    if not isinstance(value, str) or not value.strip():
        value = credentials.get("tradier_token", "")
        source = "configuration:CREDENTIALS.tradier_token"

    if not isinstance(value, str) or not value.strip():
        raise ValueError("No Tradier token configured")

    token = value.strip()

    # Be tolerant of a copied Authorization header value.
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    # Reject surrounding quotes rather than sending them to the API.
    if token.startswith(("'", '"')) or token.endswith(("'", '"')):
        raise ValueError(
            "Tradier token contains surrounding quotes. "
            "Remove the literal quote characters from its value."
        )

    if not token or any(character.isspace() for character in token):
        raise ValueError(
            "Tradier token is empty or contains whitespace"
        )

    logging.info(
        "Tradier credential loaded source=%s token_present=true",
        source,
    )

    return token

def load_settings():
    config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
    config.load_globals()
    raw = config.get("symbols", "target_symbols")
    if isinstance(raw, str):
        symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, (list, tuple)):
        if any(not isinstance(s, str) or not s.strip() for s in raw):
            raise ValueError("Symbols must be nonempty strings")
        symbols = [s.strip().upper() for s in raw]
    else:
        raise ValueError("target_symbols must be a string, list, or tuple")
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise ValueError("No symbols configured")
    credentials = getattr(config, "CREDENTIALS", {}) or {}
    if not isinstance(credentials, dict):
        raise ValueError("CREDENTIALS must be a dictionary")
    try:
        td_key = config.get("twelvedata_api", "twelvedata_api_key")
    except Exception:
        td_key = ""
    selected_uses_tradier = ROUTER_SWITCH.strip().upper() in {
        "TRADIER",
        "ALL",
        "MOST",
    }

    tradier_token = (load_tradier_token(config, credentials)
        if selected_uses_tradier
        else ""
    )

    keys = {
        "alpaca_key": credentials.get("api_key") or os.getenv("ALPACA_API_KEY", ""),
        "alpaca_secret": credentials.get("secret_key") or os.getenv("ALPACA_SECRET_KEY", ""),
        "tradier_token": tradier_token,
        "twelvedata_key": td_key
    }
    keys = {key: value.strip() if isinstance(value, str) else "" for key, value in keys.items()}
    return symbols, keys


async def main():
    choices = {"ALPACA": ["ALPACA"], "TRADIER": ["TRADIER"],
               "TWELVEDATA": ["TWELVEDATA"], "BOTH": ["ALPACA", "TWELVEDATA"],
               "ALL": ["ALPACA", "TRADIER", "TWELVEDATA"],
               "MOST": ["ALPACA", "TRADIER", ],}
    selected = choices.get(ROUTER_SWITCH.upper())
    if not selected:
        raise ValueError("Invalid ROUTER_SWITCH")
    symbols, keys = load_settings()
    required = {"ALPACA": ["alpaca_key", "alpaca_secret"],
                "TRADIER": ["tradier_token"], "TWELVEDATA": ["twelvedata_key"]}
    for provider in selected:
        if any(not keys[key] for key in required[provider]):
            raise ValueError(f"Missing credentials for {provider}")
    routers = [ProviderRouter(provider, symbols, keys) for provider in selected]
    tasks = [asyncio.create_task(router.run(), name=router.provider) for router in routers]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted")
        code = 0
    except Exception as exc:
        logging.error(
            "Application stopped: %s: %s",
            type(exc).__name__,
            exc,
        )

        code = 1
    finally:
        logging.shutdown()
    sys.exit(code)
