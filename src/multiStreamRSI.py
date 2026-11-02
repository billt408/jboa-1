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
ROUTER_SWITCH = "BOTH"  # ALPACA, TRADIER, TWELVEDATA, BOTH, ALL
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

    def reset_session(self, day):
        self.session_date = day
        self.last_timestamp = self.previous_close = None
        self.changes = 0
        self.gain_sum = self.loss_sum = 0.0
        self.average_gain = self.average_loss = self.latest = None
        self.logger.info("SESSION_START date=%s", day)

    def process_bar(self, bar, source):
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
            gain, loss = max(change, 0.0), max(-change, 0.0)
            self.changes += 1
            self.gain_sum += gain
            self.loss_sum += loss
            if self.mode == "since_open":
                up, down = self.gain_sum, self.loss_sum
            elif self.changes < self.period:
                up = down = None
            elif self.changes == self.period:
                self.average_gain = self.gain_sum / self.period
                self.average_loss = self.loss_sum / self.period
                up, down = self.average_gain, self.average_loss
            else:
                self.average_gain = (self.average_gain * (self.period - 1) + gain) / self.period
                self.average_loss = (self.average_loss * (self.period - 1) + loss) / self.period
                up, down = self.average_gain, self.average_loss
            if up is not None:
                value = 50.0 if up == down == 0 else (
                    100.0 if down == 0 else 100.0 - 100.0 / (1.0 + up / down)
                )
        self.previous_close = close
        self.last_timestamp = timestamp
        self.logger.info("MINUTE source=%s timestamp=%s close=%.8f rsi=%s changes=%d",
                         source, timestamp.isoformat(), close, value, self.changes)
        if value is not None:
            self.latest = {"datetime": timestamp.isoformat(), "close": close, "rsi": value}
        return self.latest if value is not None else None


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
        payload = await request_json(
            self.session, "GET", "https://api.tradier.com/v1/markets/timesales",
            headers=self.tradier_headers(),
            params={"symbol": symbol, "interval": "1min", "session_filter": "open",
                    "start": start.strftime("%Y-%m-%d %H:%M"),
                    "end": end.strftime("%Y-%m-%d %H:%M")})
        series = payload.get("series")
        if series is None:
            return []
        if not isinstance(series, dict) or "data" not in series:
            raise ValueError("Tradier response missing series.data")
        rows = series["data"] or []
        if isinstance(rows, dict):
            rows = [rows]
        return [Bar(parse_local(r["time"]), r["close"]) for r in rows]

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

    async def consume(self, symbol):
        tracker = self.trackers[symbol]
        queue = self.queues[symbol]
        buckets = {}
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                kind = None
            if kind is not None:
                try:
                    if kind == "recover":
                        # Disconnected/partial buckets are discarded; REST is
                        # asked for completed observations before live replay.
                        buckets.clear()
                        await self.recover(symbol, value)
                    elif kind == "bar":
                        timestamp = normalized_time(value.timestamp)
                        if regular_session(timestamp):
                            await self.accept_bar(symbol, value, "ALPACA_BAR")
                    elif kind == "tick":
                        timestamp, price = value
                        timestamp = normalized_time(timestamp)
                        price = positive_price(price)
                        if regular_session(timestamp):
                            minute = timestamp.replace(second=0, microsecond=0)
                            if tracker.last_timestamp is None or minute > tracker.last_timestamp:
                                existing = buckets.get(minute)
                                if existing is None or timestamp >= existing[0]:
                                    buckets[minute] = (timestamp, price)
                finally:
                    queue.task_done()
            watermark = datetime.now(MARKET_TZ) - timedelta(seconds=FINALIZE_GRACE_SECONDS)
            for minute in sorted(list(buckets)):
                if minute + MINUTE <= watermark:
                    _, close = buckets.pop(minute)
                    await self.accept_bar(symbol, Bar(minute, close), "WS_MINUTE_CLOSE")

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
            if bar.symbol in self.queues:
                if LOG_RAW_EVENTS:
                    self.trackers[bar.symbol].logger.info("WS_BAR timestamp=%s close=%s", bar.timestamp, bar.close)
                self.enqueue(bar.symbol, "bar", Bar(bar.timestamp, bar.close))
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
        if LOG_RAW_EVENTS:
            self.trackers[symbol].logger.info("WS_PRICE %s", json.dumps(event, separators=(",", ":")))
        self.enqueue(symbol, "tick", (timestamp, price))


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
    td_key = credentials.get("twelvedata_api_key") or os.getenv("TWELVEDATA_API_KEY", "")
    if not td_key:
        try:
            td_key = config.get("twelvedata_api", "twelvedata_api_key")
        except Exception:
            td_key = ""
    keys = {
        "alpaca_key": credentials.get("api_key") or os.getenv("ALPACA_API_KEY", ""),
        "alpaca_secret": credentials.get("secret_key") or os.getenv("ALPACA_SECRET_KEY", ""),
        "tradier_token": config.get("tradier", "tradier_token"),
        "twelvedata_key": td_key
    }
    keys = {key: value.strip() if isinstance(value, str) else "" for key, value in keys.items()}
    return symbols, keys


async def main():
    choices = {"ALPACA": ["ALPACA"], "TRADIER": ["TRADIER"],
               "TWELVEDATA": ["TWELVEDATA"], "BOTH": ["ALPACA", "TWELVEDATA"],
               "ALL": ["ALPACA", "TRADIER", "TWELVEDATA"]}
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
