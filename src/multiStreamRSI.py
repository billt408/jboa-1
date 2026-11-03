"""Multi-provider RSI with a dedicated-thread TRADINGVIEW webhook receiver.

Dependencies: aiohttp, alpaca-py, Flask (includes Werkzeug), yfinance, pandas, pandas_ta, and lib.utils.JsonIniParser.
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
import pandas as pd
import yfinance as yf
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
ROUTER_SWITCH = "ALL"  # ALPACA, TRADIER, TWELVEDATA, YAHOOFINANCE, BOTH, ALL, MOST
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

        if self.session_date is not None and timestamp.date() < self.session_date:
            return None

        if self.last_timestamp is not None and timestamp <= self.last_timestamp:
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
    for attempt in range(4):
        try:
            async with session.request(method, url, **kwargs) as response:
                if response.status in {401, 403}:
                    raise FatalProviderError(
                        f"HTTP {response.status}: " "authentication/access rejected"
                    )
                if response.status == 429 or response.status >= 500:
                    if attempt == 3:
                        raise RuntimeError("Provider HTTP retries exhausted")
                    await asyncio.sleep(2**attempt)
                    continue
                response.raise_for_status()
                data = await response.json()
                if not isinstance(data, dict):
                    raise ValueError("Provider returned a non-object response")
                return data
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            if attempt == 3:
                raise RuntimeError("Provider network retries exhausted") from None
            await asyncio.sleep(2**attempt)
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
        return [sanitize_provider_event(item, secrets) for item in value]

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
                credentials["alpaca_key"],
                credentials["alpaca_secret"],
            )
            self.stream = StockDataStream(
                credentials["alpaca_key"],
                credentials["alpaca_secret"],
                feed=FEED,
            )

    def enqueue(self, symbol, kind, value):
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
            message = (
                f"provider={self.provider} "
                f"error_type=FatalProviderError detail={exc}"
            )
            self.logger.error("%s", message)
            raise FatalProviderError(message) from None
        except Exception as exc:
            message = f"provider={self.provider} " f"error_type={type(exc).__name__}"
            self.logger.error("%s", message)
            raise RuntimeError(message) from exc

    async def _run(self):
        timeout = aiohttp.ClientTimeout(total=30, connect=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            tasks = [asyncio.create_task(self.consume(s)) for s in self.symbols]

            if self.provider == "ALPACA":
                tasks.append(asyncio.create_task(self.run_alpaca()))
            elif self.provider == "YAHOOFINANCE":
                tasks.append(asyncio.create_task(self.run_yfinance()))
            else:
                tasks.append(asyncio.create_task(self.run_websocket()))

            try:
                done, _ = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
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
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end - timedelta(microseconds=1),
            feed=FEED,
            adjustment=Adjustment.RAW,
            sort=Sort.ASC,
        )
        response = self.historical.get_stock_bars(request)

        return [Bar(b.timestamp, b.close) for b in response.data.get(symbol, [])]

    def _yf_history(self, symbol, start, end):
        """Retrieve one-minute bars using timezone-aware datetime bounds."""
        start = normalized_time(start)
        end = normalized_time(end)

        if start >= end:
            return []

        df = yf.download(
            symbol,
            start=start,
            end=end,
            interval="1m",
            auto_adjust=False,
            prepost=False,
            threads=False,
            progress=False,
            timeout=20,
        )

        if df is None or df.empty:
            self.logger.warning(
                "YAHOOFINANCE_NO_DATA symbol=%s start=%s end=%s",
                symbol,
                start.isoformat(),
                end.isoformat(),
            )
            return []

        # A single-symbol download can still have MultiIndex columns.
        if isinstance(df.columns, pd.MultiIndex):
            close_columns = [
                column
                for column in df.columns
                if any(str(part).strip().lower() == "close" for part in column)
            ]
        else:
            close_columns = [
                column
                for column in df.columns
                if str(column).strip().lower() == "close"
            ]

        if len(close_columns) != 1:
            raise ValueError(
                f"Yahoo Finance returned ambiguous or missing "
                f"Close columns for {symbol}"
            )

        closes = df[close_columns[0]]

        if not isinstance(closes, pd.Series):
            raise ValueError(f"Yahoo Finance Close data is not a Series for {symbol}")

        # Only accept minutes that have finished, including a grace period.
        watermark = datetime.now(MARKET_TZ) - timedelta(seconds=FINALIZE_GRACE_SECONDS)
        unique = {}

        for ts, raw_close in closes.items():
            if pd.isna(raw_close):
                continue

            timestamp = pd.Timestamp(ts)

            if timestamp.tzinfo is None:
                raise ValueError(
                    f"Yahoo Finance returned a timezone-naive "
                    f"intraday timestamp for {symbol}: {timestamp}"
                )

            timestamp = normalized_time(timestamp.to_pydatetime())

            if not start <= timestamp < end:
                continue

            if timestamp.second or timestamp.microsecond:
                raise ValueError(
                    f"Yahoo Finance returned a non-minute-aligned "
                    f"timestamp for {symbol}: {timestamp.isoformat()}"
                )

            if not regular_session(timestamp):
                continue

            if timestamp + MINUTE > watermark:
                continue

            unique[timestamp] = Bar(
                timestamp=timestamp,
                close=positive_price(raw_close),
            )

        return [unique[timestamp] for timestamp in sorted(unique)]

    async def fetch_history(self, symbol, start, end):
        if self.provider == "ALPACA":
            return await asyncio.to_thread(
                self.alpaca_history,
                symbol,
                start,
                end,
            )

        if self.provider == "YAHOOFINANCE":
            return await asyncio.to_thread(
                self._yf_history,
                symbol,
                start,
                end,
            )

        if self.provider == "TWELVEDATA":
            payload = await request_json(
                self.session,
                "GET",
                "twelvedata.com",
                params={
                    "symbol": symbol,
                    "interval": "1min",
                    "order": "asc",
                    "timezone": "America/New_York",
                    "outputsize": 5000,
                    "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_date": (end - timedelta(seconds=1)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "apikey": self.credentials["twelvedata_key"],
                },
            )

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
                "tradier.com",
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
                raise ValueError("Tradier historical response missing series")

            series = payload["series"]

            if series is None:
                self.logger.warning(
                    "NO_HISTORY provider=TRADIER symbol=%s start=%s end=%s",
                    symbol,
                    start.isoformat(),
                    end.isoformat(),
                )
                return []

            if not isinstance(series, dict):
                raise ValueError("Tradier historical series must be an object")

            if "data" not in series:
                raise ValueError("Tradier historical response missing series.data")

            rows = series["data"]

            if rows is None:
                return []

            if isinstance(rows, dict):
                rows = [rows]

            if not isinstance(rows, list):
                raise ValueError("Tradier historical series.data must be a list")

            bars = []

            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Tradier historical bar must be an object")

                if row.get("timestamp") is not None:
                    epoch = float(row["timestamp"])

                    if not math.isfinite(epoch):
                        raise ValueError("Invalid Tradier historical timestamp")

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

        raise ValueError(f"Unsupported historical provider: {self.provider}")

    async def recover(self, symbol, through):
        tracker = self.trackers[symbol]
        end = min(
            normalized_time(through),
            datetime.now(MARKET_TZ),
        ).replace(second=0, microsecond=0)

        if end.weekday() >= 5:
            return

        day = end.date()

        if tracker.session_date is not None and day < tracker.session_date:
            return

        start = datetime.combine(day, OPEN_TIME, tzinfo=MARKET_TZ)
        end = min(
            end,
            datetime.combine(day, CLOSE_TIME, tzinfo=MARKET_TZ),
        )

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

            unique[timestamp] = Bar(
                timestamp,
                positive_price(bar.close),
            )

        for timestamp in sorted(unique):
            tracker.process_bar(unique[timestamp], "REST_BACKFILL")

        tracker.logger.info(
            "BACKFILL loaded=%d start=%s end_exclusive=%s best_effort=true",
            len(unique),
            start.isoformat(),
            end.isoformat(),
        )

        ready = tracker.latest is not None
        required_changes = tracker.period if tracker.mode == "wilder" else 1
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
            "WARMUP provider=%s symbol=%s loaded=%d ready=%s "
            "additional_closes=%d latest_rsi=%s",
            self.provider,
            symbol,
            len(unique),
            ready,
            additional_closes,
            (f"{tracker.latest['rsi']:.2f}" if ready else "warming-up"),
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
                                f"{self.provider}_BAR",
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

                                if existing is None or timestamp >= existing:
                                    buckets[minute] = (timestamp, price)

                                tick_count += 1

                                if tick_count == 1:
                                    logging.info(
                                        "FIRST_SESSION_TICK provider=%s "
                                        "symbol=%s timestamp=%s price=%.8f",
                                        self.provider,
                                        symbol,
                                        timestamp.isoformat(),
                                        price,
                                    )
                    else:
                        raise ValueError(f"Unknown queue event kind: {kind!r}")
                except Exception as exc:
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

            if not queue.empty():
                continue

            watermark = datetime.now(MARKET_TZ) - timedelta(
                seconds=FINALIZE_GRACE_SECONDS
            )
            completed = sorted(
                minute for minute in buckets if minute + MINUTE <= watermark
            )

            for minute in completed:
                _, close = buckets.pop(minute)

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

        if (
            tracker.session_date != timestamp.date()
            or tracker.last_timestamp is None
            or timestamp > tracker.last_timestamp + MINUTE
        ):
            await self.recover(symbol, timestamp)

        tracker.process_bar(bar, source)

    async def run_alpaca(self):
        for symbol in self.symbols:
            self.enqueue(symbol, "recover", datetime.now(MARKET_TZ))

        async def callback(bar):
            tracker = self.trackers.get(bar.symbol)

            if tracker is None:
                return

            latest = tracker.latest
            bar_timestamp = normalized_time(bar.timestamp)
            current_session_rsi = (
                latest is not None and tracker.session_date == bar_timestamp.date()
            )

            if LOG_RAW_EVENTS:
                tracker.logger.info(
                    "WS_BAR provider=ALPACA symbol=%s timestamp=%s "
                    "close=%.8f previous_rsi[%s]=%s rsi_bar_timestamp=%s",
                    bar.symbol,
                    bar_timestamp.isoformat(),
                    float(bar.close),
                    tracker.mode,
                    (f"{latest['rsi']:.2f}" if current_session_rsi else "warming-up"),
                    (latest["datetime"] if current_session_rsi else "none"),
                )

            self.enqueue(
                bar.symbol,
                "bar",
                Bar(bar.timestamp, bar.close),
            )

        self.stream.subscribe_bars(callback, *self.symbols)
        await self.stream._run_forever()

    async def run_yfinance(self):
        """Simulate a streaming data feed by polling Yahoo Finance intraday bars."""
        for symbol in self.symbols:
            self.enqueue(symbol, "recover", datetime.now(MARKET_TZ))

        self.logger.info(
            "CONNECTED polling engine active for symbols=%s",
            self.symbols,
        )

        while True:
            try:
                now = datetime.now(MARKET_TZ)

                # Look back 5 minutes to sweep past bars and guarantee
                # capture across boundary transitions.
                start_time = now - timedelta(minutes=5)

                for symbol in self.symbols:
                    bars = await self.fetch_history(
                        symbol,
                        start_time,
                        now,
                    )

                    for bar in bars:
                        self.enqueue(symbol, "bar", bar)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    "YAHOOFINANCE_POLL_ERROR error_type=%s",
                    type(exc).__name__,
                )

            await asyncio.sleep(30)

    def tradier_headers(self):
        return {
            "Authorization": (f"Bearer {self.credentials['tradier_token']}"),
            "Accept": "application/json",
        }

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
                    url = "wss://twelvedata.com"
                    params = {"apikey": self.credentials["twelvedata_key"]}
                    subscription = {
                        "action": "subscribe",
                        "params": {"symbols": ",".join(self.symbols)},
                    }
                else:
                    payload = await request_json(
                        self.session,
                        "POST",
                        "tradier.com",
                        headers=self.tradier_headers(),
                    )
                    stream = payload.get("stream", {})
                    session_id = stream.get("sessionid")

                    if not session_id:
                        raise FatalProviderError(
                            "Tradier returned no streaming session ID"
                        )

                    url = "wss://tradier.com"
                    params = None
                    subscription = {
                        "symbols": self.symbols,
                        "filter": ["trade"],
                        "sessionid": session_id,
                        "linebreak": True,
                        "validOnly": True,
                    }

                async with self.session.ws_connect(
                    url,
                    params=params,
                    heartbeat=20,
                ) as websocket:
                    await websocket.send_json(subscription)
                    cutoff = datetime.now(MARKET_TZ)

                    for symbol in self.symbols:
                        self.enqueue(symbol, "recover", cutoff)

                    self.logger.info(
                        "CONNECTED symbols=%s " "partial_connection_minute=true",
                        self.symbols,
                    )

                    if self.provider == "TWELVEDATA":
                        heartbeat = asyncio.create_task(self.td_heartbeat(websocket))

                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError("Websocket transport failed")

                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue

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
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ConnectionError,
            ) as exc:
                self.logger.warning(
                    "WS_RETRY error_type=%s",
                    type(exc).__name__,
                )
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(
                        heartbeat,
                        return_exceptions=True,
                    )

            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    def handle_event(self, event):
        if not isinstance(event, dict):
            self.logger.warning("Ignoring non-object event")
            return

        is_tick = (
            event.get("event") == "price"
            if self.provider == "TWELVEDATA"
            else event.get("type") == "trade"
        )

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
            event_type = str(event.get("event", event.get("type", ""))).lower()
            status = str(event.get("status", "")).lower()
            is_error = event_type == "error" or status in {"error", "failed", "failure"}
            log_method = self.logger.error if is_error else self.logger.info
            log_method(
                "WS_CONTROL provider=%s payload=%s",
                self.provider,
                details,
            )

            console_method = logging.error if is_error else logging.info
            console_method(
                "WS_CONTROL provider=%s payload=%s",
                self.provider,
                details,
            )

            if is_error:
                raise FatalProviderError(f"Websocket rejected request: {details}")

            return

        symbol = str(event.get("symbol", "")).upper()

        if symbol not in self.queues:
            return

        try:
            if self.provider == "TWELVEDATA":
                epoch, price = (
                    float(event["timestamp"]),
                    positive_price(event["price"]),
                )
            else:
                epoch, price = (
                    float(event["date"]) / 1000.0,
                    positive_price(event["price"]),
                )

            if not math.isfinite(epoch):
                raise ValueError("Nonfinite timestamp")

            timestamp = datetime.fromtimestamp(epoch, MARKET_TZ)

            if timestamp > datetime.now(MARKET_TZ) + timedelta(seconds=30):
                raise ValueError("Future timestamp")
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            OSError,
        ):
            self.trackers[symbol].logger.warning("INVALID_TICK")
            return

        tracker = self.trackers[symbol]
        latest = tracker.latest
        in_regular_session = regular_session(timestamp)
        same_session = latest is not None and tracker.session_date == timestamp.date()

        if not in_regular_session:
            rsi_status = "outside_regular_session"
        elif not same_session:
            rsi_status = "warming_up"
        else:
            rsi_status = "latest_completed_minute"

        rsi_text = f"{latest['rsi']:.2f}" if same_session else "unavailable"
        rsi_timestamp = latest["datetime"] if same_session else "none"

        if LOG_RAW_EVENTS:
            safe_event = sanitize_provider_event(
                event,
                secrets=tuple(self.credentials.values()),
            )
            tracker.logger.info(
                "WS_PRICE provider=%s symbol=%s tick_timestamp=%s "
                "price=%.8f accepted_for_rsi=%s rsi_status=%s "
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

        if not in_regular_session:
            return

        self.enqueue(symbol, "tick", (timestamp, price))


def load_tradier_token(config, credentials):
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

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if token.startswith(("'", '"')) or token.endswith(("'", '"')):
        raise ValueError(
            "Tradier token contains surrounding quotes. " "Remove literal quotes."
        )

    if not token or any(character.isspace() for character in token):
        raise ValueError("Tradier token is empty or contains whitespace")

    logging.info(
        "Tradier credential loaded source=%s token_present=true",
        source,
    )

    return token


def load_settings():
    config_path = Path(__file__).resolve().parent / "lib" / "parms.ini"

    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    config = JsonIniParser(str(config_path))
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
        td_key = config.get(
            "twelvedata_api",
            "twelvedata_api_key",
        )
    except Exception:
        td_key = ""

    selected_uses_tradier = ROUTER_SWITCH.strip().upper() in {
        "TRADIER",
        "ALL",
        "MOST",
    }
    tradier_token = (
        load_tradier_token(config, credentials) if selected_uses_tradier else ""
    )

    keys = {
        "alpaca_key": (credentials.get("api_key") or os.getenv("ALPACA_API_KEY", "")),
        "alpaca_secret": (
            credentials.get("secret_key") or os.getenv("ALPACA_SECRET_KEY", "")
        ),
        "tradier_token": tradier_token,
        "twelvedata_key": td_key,
    }
    keys = {
        key: value.strip() if isinstance(value, str) else ""
        for key, value in keys.items()
    }

    return symbols, keys


WEBHOOK_ENABLED = True
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "127.0.0.1")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
WEBHOOK_QUEUE_LIMIT = 2000
WEBHOOK_MAX_BYTES = 16384


def validate_tradingview_payload(payload, allowed_symbols):
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")

    ticker = payload.get("ticker")

    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("ticker must be a nonempty string")

    ticker = ticker.strip().upper()

    if len(ticker) > 100:
        raise ValueError("ticker is too long")

    symbol = ticker if ticker in allowed_symbols else ticker.rsplit(":", 1)[-1]

    if symbol not in allowed_symbols:
        raise ValueError("ticker is not in configured target_symbols")

    try:
        if isinstance(payload.get("price"), bool):
            raise ValueError()

        price = positive_price(payload.get("price"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("price must be finite and positive") from None

    raw_rsi = payload.get("rsi_value")

    if raw_rsi is None or raw_rsi == "" or raw_rsi == "N/A":
        rsi = None
    else:
        try:
            if isinstance(raw_rsi, bool):
                raise ValueError()

            rsi = float(raw_rsi)

            if not math.isfinite(rsi) or not 0 <= rsi <= 100:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "rsi_value must be between 0 and 100, or omitted"
            ) from None

    raw_time = payload.get("time", "")
    event_time = None

    if raw_time:
        if not isinstance(raw_time, str) or len(raw_time) > 100:
            raise ValueError("time must be a timezone-aware ISO-8601 string")

        try:
            event_time = normalized_time(
                datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            )
        except (TypeError, ValueError, OverflowError):
            raise ValueError("time must be a timezone-aware ISO-8601 string") from None

    return {
        "symbol": symbol,
        "ticker": ticker,
        "price": price,
        "rsi_value": rsi,
        "event_time": (event_time.isoformat() if event_time else None),
        "received_at": datetime.now(MARKET_TZ).isoformat(),
    }


class TradingviewProvider:
    def __init__(self, symbols, host=WEBHOOK_HOST, port=WEBHOOK_PORT):
        import queue
        import threading

        self.provider = "TRADINGVIEW"
        self.symbols = set(symbols)
        self.host = host
        self.port = port
        self.queue = queue.Queue(maxsize=WEBHOOK_QUEUE_LIMIT)
        self.stop = threading.Event()
        self.server = None
        self.thread = None
        self.thread_error = None
        self.logger = make_logger(self.provider, "_STREAM")
        self.symbol_loggers = {
            symbol: make_logger(self.provider, symbol) for symbol in symbols
        }
        self.latest = {}
        self.app = self.create_app()

    def create_app(self):
        import queue

        from flask import Flask, request, jsonify

        app = Flask("tradingview_webhook")
        app.config["MAX_CONTENT_LENGTH"] = WEBHOOK_MAX_BYTES

        @app.errorhandler(413)
        def too_large(_error):
            return (
                jsonify(
                    status="error",
                    message="Payload too large",
                ),
                413,
            )

        @app.get("/health")
        def health():
            return jsonify(status="ok", provider=self.provider), 200

        @app.post("/webhook")
        def receive():
            if not request.is_json:
                return (
                    jsonify(
                        status="error",
                        message="Payload must be JSON",
                    ),
                    415,
                )

            payload = request.get_json(silent=True)

            if not isinstance(payload, dict):
                return (
                    jsonify(
                        status="error",
                        message="JSON payload must be an object",
                    ),
                    400,
                )

            try:
                event = validate_tradingview_payload(
                    payload,
                    self.symbols,
                )
            except ValueError as exc:
                return jsonify(status="error", message=str(exc)), 400

            try:
                self.queue.put_nowait(event)
            except queue.Full:
                return (
                    jsonify(
                        status="error",
                        message="Receiver busy; alert not queued",
                    ),
                    503,
                )

            return (
                jsonify(
                    status="queued",
                    provider=self.provider,
                    received=event["symbol"],
                ),
                200,
            )

        return app

    def serve(self):
        try:
            while not self.stop.is_set():
                self.server.handle_request()
        except Exception as exc:
            self.thread_error = type(exc).__name__
        finally:
            self.server.server_close()

    def record(self, event):
        rsi = event["rsi_value"]
        label = (
            "ALERT"
            if rsi is None
            else "OVERBOUGHT" if rsi >= 70 else "OVERSOLD" if rsi <= 35 else "TRIPPED"
        )
        event = {
            **event,
            "provider": self.provider,
            "status_label": label,
            "rsi_source": "webhook_reported",
        }

        self.latest[event["symbol"]] = event
        text = json.dumps(event, separators=(",", ":"))

        self.symbol_loggers[event["symbol"]].info(
            "WEBHOOK_ALERT %s",
            text,
        )
        logging.info("WEBHOOK_ALERT %s", text)

    async def run(self):
        import queue
        import threading

        from werkzeug.serving import make_server, WSGIRequestHandler

        class BoundedRequestHandler(WSGIRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2.0)

        self.server = make_server(
            self.host,
            self.port,
            self.app,
            threaded=False,
            request_handler=BoundedRequestHandler,
        )
        self.server.timeout = 0.5
        self.thread = threading.Thread(
            target=self.serve,
            name="TRADINGVIEW-webhook",
            daemon=True,
        )
        self.thread.start()

        logging.info(
            "TRADINGVIEW listening host=%s port=%d "
            "route=/webhook authentication=none",
            self.host,
            self.port,
        )

        try:
            while True:
                for _ in range(100):
                    try:
                        event = self.queue.get_nowait()
                    except queue.Empty:
                        break

                    try:
                        self.record(event)
                    finally:
                        self.queue.task_done()

                if not self.thread.is_alive():
                    raise RuntimeError(
                        "TRADINGVIEW server thread stopped: " + str(self.thread_error)
                    )

                await asyncio.sleep(0.05)
        finally:
            self.stop.set()
            await asyncio.to_thread(self.thread.join, 3.0)

            if self.thread.is_alive():
                logging.error("TRADINGVIEW thread did not stop within timeout")

            while True:
                try:
                    event = self.queue.get_nowait()
                except queue.Empty:
                    break

                try:
                    self.record(event)
                finally:
                    self.queue.task_done()


async def main():
    logging.info("ENTERED main()  separate webhook routing")

    switch = ROUTER_SWITCH.strip().upper()

    # These lists contain ONLY ProviderRouter-compatible providers.
    choices = {
        "ALPACA": ["ALPACA"],
        "TRADIER": ["TRADIER"],
        "TWELVEDATA": ["TWELVEDATA"],
        "YAHOOFINANCE": ["YAHOOFINANCE"],
        "BOTH": ["ALPACA", "TWELVEDATA"],
        "ALL": [
            "ALPACA",
            "TRADIER",
            "TWELVEDATA",
            "YAHOOFINANCE",
        ],
        "MOST": ["ALPACA", "TRADIER"],
        # Webhook-only modes: no market-data routers.
        "TRADINGVIEW": [],
    }

    if switch not in choices:
        raise ValueError(
            f"Invalid ROUTER_SWITCH: {ROUTER_SWITCH!r}. "
            f"Valid choices: {', '.join(choices)}"
        )

    selected = choices[switch]

    logging.info(
        "ROUTER_SELECTION switch=%s market_data_providers=%s",
        switch,
        selected,
    )

    symbols, keys = load_settings()

    required = {
        "ALPACA": ["alpaca_key", "alpaca_secret"],
        "TRADIER": ["tradier_token"],
        "TWELVEDATA": ["twelvedata_key"],
        "YAHOOFINANCE": [],
    }

    # Validate credentials only for market-data providers.
    for provider in selected:
        if provider not in required:
            raise ValueError(f"Unexpected market-data provider: {provider!r}")

        missing = [key for key in required[provider] if not keys.get(key)]

        if missing:
            raise ValueError(
                f"Missing credentials for {provider}: " + ", ".join(missing)
            )

    routers = [ProviderRouter(provider, symbols, keys) for provider in selected]

    # The webhook uses its own class, not ProviderRouter.
    enable_webhook = WEBHOOK_ENABLED or switch in {"TRADINGVIEW"}

    if enable_webhook:
        routers.append(TradingviewProvider(symbols=symbols))

    if not routers:
        raise ValueError("No market-data providers or webhook receiver enabled")

    logging.info(
        "STARTUP symbols=%s providers=%s",
        symbols,
        [router.provider for router in routers],
    )

    tasks = [
        asyncio.create_task(
            router.run(),
            name=router.provider,
        )
        for router in routers
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()

        await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

    return 0


logging.info(
    f"Reached entry point: "
    f"file={globals().get('__file__', '<interactive>')} "
    f"name={__name__!r}"
)

if __name__ == "__main__":
    exit_code = 1

    try:
        logging.info("Starting asyncio.run(main())")
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted")
        exit_code = 0
    except Exception:
        logging.exception("Application startup or runtime failure")
        exit_code = 1
    finally:
        logging.shutdown()

    sys.exit(exit_code)
