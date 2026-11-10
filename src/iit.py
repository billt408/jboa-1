"""Multi-provider RSI with a dedicated-thread TRADINGVIEW webhook receiver.

Python 3.12 recommended.
Dependencies: aiohttp, alpaca-py, Flask, yfinance, pandas, pandas-market-calendars,
tzdata, and the existing project-local lib.utils.JsonIniParser.
Uses NYSE calendar holidays and early closes. Summary minute keys use UTC.
One JSON summary per regular-session minute per provider in a dedicated JSONL log.
Late data cannot change emitted summaries or RSI state. Historical warmup is best effort.
RSI state rolls over across sessions and is seeded at startup with the last
RSI_WARMUP_SESSIONS prior sessions (default 5) so Wilder averages converge like
TradingView's continuous-history RSI. Set RSI_WARMUP_SESSIONS=0 to skip the
startup backfill; per-session cold resets return only if state is not carried.
TradingView requires a 1m closed-bar payload with explicit bar_start.
TradingView webhook RSI values are classified as signals: rsi <=
OPTION_CHAIN_RSI_LOW (default 35) is the BUY signal and rsi >=
OPTION_CHAIN_RSI_HIGH (default 65) is the SELL signal. Each signal triggers
option-chain collection via lib.getOptionsContracts; the full clean payload is
written to logs/optionchains/ with provider, symbol, BUY/SELL signal, RSI and
timestamp in the filename. Every option-chain log line and each TRADINGVIEW
minute-summary record carries the signal label.
An AlpacaPositionMonitor thread polls the account's positions every minute,
writes them to its own log file under logs/positions/ and prints each current
option position.
Deduplication is in-memory only and does not survive process restarts.
Provider startup is controlled exclusively by [providers] NAME.enabled in lib/parms.ini.
load_settings populates module globals selected and required; disabled providers
are not instantiated or included in minute summaries.
"""

import traceback
import queue
from datetime import datetime, UTC
from werkzeug.serving import make_server, WSGIRequestHandler
from flask import Flask, request, jsonify
import threading
from alpaca.trading.client import TradingClient
import asyncio
import collections
import configparser
import json
import logging
import math
import os
import sys

from dataclasses import dataclass
from datetime import datetime, timedelta, time as clock_time, timezone
from functools import lru_cache
import hmac
import pandas_market_calendars as mcal
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
from lib.getOptionsContracts import get_pruned_option_chain

MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = clock_time(9, 30)
CLOSE_TIME = clock_time(16, 0)
MODE = "wilder"
RSI_PERIOD = 14
# Cross-session warmup: at startup each provider's SessionRSI is seeded with
# this many prior NYSE sessions of 1-minute closes so the Wilder averages are
# converged, matching TradingView's continuous-history RSI. 0 disables the
# startup backfill (state still rolls over across sessions once converged).
RSI_WARMUP_SESSIONS = int(os.getenv("RSI_WARMUP_SESSIONS", "5"))
FEED = DataFeed.IEX
# Populated by load_settings(); provider startup is controlled only by
# parms.ini.
selected: list[str] = []
required: dict[str, list[str]] = {}
FINALIZE_GRACE_SECONDS = 2.0
QUEUE_LIMIT = 20000
LOG_RAW_EVENTS = False
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid4().hex[:8]
LOG_ROOT = Path(__file__).resolve().parent / "logs"
MINUTE = timedelta(minutes=1)
UTC = timezone.utc
SUMMARY_DELAY_SECONDS = float(os.getenv("SUMMARY_DELAY_SECONDS", "45"))
SUMMARY_TO_CONSOLE = os.getenv("SUMMARY_TO_CONSOLE", "true").lower() in {
    "1",
    "true",
    "yes",
}
YAHOO_POLL_SECONDS = float(os.getenv("YAHOO_POLL_SECONDS", "30"))
if not math.isfinite(YAHOO_POLL_SECONDS) or YAHOO_POLL_SECONDS <= 0:
    raise ValueError("YAHOO_POLL_SECONDS must be finite and positive")

# Kinematic Math Multipliers & Performance Threshold Matrix Boundaries
MOMENTUM_WINDOW = 4              # Historical window capacity for velocity updates
# Target milestone yield ratio to force an early exit
PROFIT_TAKE_PERCENT = 0.15
# Rate-of-change buffer to halt entries on rapid crashes
VELOCITY_THRESHOLD = 0.05

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
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
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
    if isinstance(value, bool):
        raise ValueError("Price must not be boolean")
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Price must be finite and positive")
    return price


def regular_session(timestamp):
    value = as_utc(timestamp)
    bounds = session_bounds(value.astimezone(MARKET_TZ).date())
    return bounds is not None and bounds[0] <= value < bounds[1]


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
    def __init__(
            self,
            symbol,
            provider,
            mode=MODE,
            period=RSI_PERIOD,
            cross_session=True):
        if mode not in {"since_open", "wilder"}:
            raise ValueError("Unknown RSI mode")
        if isinstance(
                period,
                bool) or not isinstance(
                period,
                int) or period < 2:
            raise ValueError("period must be an integer >= 2")
        self.symbol = symbol
        self.mode = mode
        self.period = period
        # When True, a new session rolls the Wilder averages forward instead of
        # clearing them: the first bar of the day contributes the overnight
        # change from the previous close, like TradingView's bar series.
        self.cross_session = cross_session
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

    def begin_session(self, day):
        """Enter a new session, keeping converged Wilder state when possible."""
        if self.cross_session and self.average_gain is not None:
            self.session_date = day
            self.last_timestamp = None
            self.logger.info(
                "SESSION_ROLLOVER date=%s rsi_state=carried avg_gain=%.6f avg_loss=%.6f",
                day,
                self.average_gain or 0.0,
                self.average_loss or 0.0,
            )
        else:
            self.reset_session(day)

    def process_bar(self, bar, source, warmup=False):
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
            self.begin_session(timestamp.date())

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

            elif self.average_gain is None and self.changes == self.period:
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
                    if value is not None and not warmup:
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
                    self.logger.debug(message, *arguments)
                    if source != "REST_BACKFILL":
                        logging.debug(message, *arguments)
                        return observation
class FatalProviderError(RuntimeError):
    pass
                    async def request_json(session, method, url, kwargs):
                    for attempt in range(4):
                    try:
                    async with session.request(method, url, kwargs) as response:
                    if response.status in {401, 403}:
                    raise FatalProviderError(
                        f"HTTP {response.status}: authentication/access rejected"
                    )
                    text = await response.text()
                    retry = (
                        response.status == 429
                        or response.status >= 500
                        or (response.status == 400 and "quota violation" in text.lower())
                    )
                    if retry:
                    if attempt == 3:
                    raise RuntimeError("Provider HTTP retries exhausted")
                    await asyncio.sleep(2attempt)
                    continue
                    if response.status >= 400:
                    raise RuntimeError(f"Provider HTTP {response.status}")
                    data = json.loads(text)
                    if not isinstance(data, dict):
                    raise ValueError("Provider returned a non-object response")
                    return data
                    except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                    if attempt == 3:
                    raise RuntimeError("Provider network retries exhausted") from None
                    await asyncio.sleep(2attempt)
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
                    @dataclass(frozen=True)
                    class MinuteCandidate:


                    close: float
                    event_time: datetime
                    received_at: datetime
                    source: str
                    kind: str
                    reported_rsi: float | None = None
                    def as_utc(value):


                    return normalized_time(value).astimezone(UTC)
                    def minute_utc(value):


                    return as_utc(value).replace(second=0, microsecond=0)
                    MARKET_CALENDAR = mcal.get_calendar("NYSE")
                    @lru_cache(maxsize=512)
                    def session_bounds(day):


                    schedule = MARKET_CALENDAR.schedule(start_date=day, end_date=day)
                    if schedule.empty:
                    return None
                    row = schedule.iloc[0]
                    return (
                        as_utc(row["market_open"].to_pydatetime()),
                        as_utc(row["market_close"].to_pydatetime()),
                    )
                    def next_session_minute(value):


                    value = minute_utc(value)
                    day = value.astimezone(MARKET_TZ).date()
                    for _ in range(370):
                    bounds = session_bounds(day)
                    if bounds is not None:
                    opening, closing = bounds
                    if value < closing:
                    return max(opening, value)
                    day += timedelta(days=1)
                    value = as_utc(datetime.combine(day, clock_time(0), tzinfo=MARKET_TZ))
                    raise RuntimeError("Could not find the next NYSE session")
                    def make_summary_logger():


                    logger = logging.getLogger(f"rsi.{RUN_ID}.minute_summary")
                    logger.setLevel(logging.INFO)
                    logger.propagate = False
                    if not logger.handlers:
                    LOG_ROOT.mkdir(parents=True, exist_ok=True)
                    path = LOG_ROOT / f"minute-summary-{RUN_ID}.jsonl"
                    handler = logging.FileHandler(path, encoding="utf-8")
                    handler.setFormatter(logging.Formatter("%(message)s"))
                    logger.addHandler(handler)
                    if SUMMARY_TO_CONSOLE:
                    console = logging.StreamHandler()
                    console.setFormatter(logging.Formatter("%(message)s"))
                    logger.addHandler(console)
                    logging.info("MINUTE_SUMMARY_FILE %s", path)
                    return logger
                    class SymbolKinematics:


                    """Tracks chronological RSI streams to isolate velocity and acceleration vectors."""
                    def init(self, symbol, window_size=MOMENTUM_WINDOW):


                    self.symbol = symbol
                    self.history = collections.deque(maxlen=window_size)
                    self.timestamps = collections.deque(maxlen=window_size)
                    def push(self, rsi, timestamp_iso):


                    try:
                    ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
                    except ValueError:
                    ts = datetime.now(timezone.utc)
                    self.history.append(rsi)
                    self.timestamps.append(ts)
                    @property
                    def calculate_vectors(self):


                    """Computes rate-of-change (velocity) and momentum change (acceleration)."""
                    if len(self.history) < 2:
                    return 0.0, 0.0
                    dt1 = (self.timestamps[-1] - self.timestamps[-2]).total_seconds() or 1.0
                    velocity = (self.history[-1] - self.history[-2]) / dt1
                    acceleration = 0.0
                    if len(self.history) >= 3:
                    dt2 = (self.timestamps[-2] - self.timestamps[-3]).total_seconds() or 1.0
                    prev_velocity = (self.history[-2] - self.history[-3]) / dt2
                    acceleration = (velocity - prev_velocity) / dt1
                    return velocity, acceleration
                    class MinuteCoordinator:


                    """Single event-loop owner of pending closes, RSI state and summary output."""


                    def init(
                            self,
                            providers,
                            symbols,
                            start=None,
                            delay=SUMMARY_DELAY_SECONDS,
                            emit=None):


                    if not math.isfinite(delay) or not 0 <= delay <= 3600:
                    raise ValueError("SUMMARY_DELAY_SECONDS must be between 0 and 3600")
                    self.providers = tuple(dict.fromkeys(providers))
                    self.symbols = tuple(dict.fromkeys(symbols))
                    if not self.providers or not self.symbols:
                    raise ValueError("Coordinator requires providers and symbols")
                    self.delay = timedelta(seconds=delay)
                    self.started_at = as_utc(start or datetime.now(UTC))
                    self.report_start = next_session_minute(self.started_at)
                    self.next_minute = self.report_start
                    self.last_finalized = None
                    self.pending = {}
                    self.trackers = {
                        (provider, symbol): SessionRSI(symbol, provider)
                        for provider in self.providers
                        if provider != "TRADINGVIEW"
                        for symbol in self.symbols
                    }
                    self.counts = {}
                    self.connected = {provider: False for provider in self.providers}
                    self.outages = {
                        provider: [(self.started_at, None)] for provider in self.providers
                    }
                    self.late_counts = {provider: 0 for provider in self.providers}
                    logger = None if emit is not None else make_summary_logger()
                    self.emit = emit or (
                        lambda record: logger.info(
                            "%s", json.dumps(record, separators=(",", ":"), allow_nan=False)
                        )
                    )
                    def set_connected(self, provider, connected, at=None):


                    if self.connected[provider] == connected:
                    return
                    at = as_utc(at or datetime.now(UTC))
                    self.connected[provider] = connected
                    if connected:
                    start, _ = self.outages[provider][-1]
                    self.outages[provider][-1] = (start, at)
                    else:
                    self.outages[provider].append((at, None))


                    def submit(
                        self,
                        provider,
                        symbol,
                        timestamp,
                        close,
                        *,
                        source,
                        kind="bar",
                        received_at=None,
                        reported_rsi=None,
                    ):


                    if provider not in self.providers or symbol not in self.symbols:
                    raise ValueError("Unconfigured provider or symbol")
                    if kind not in {"bar", "tick", "webhook"}:
                    raise ValueError("Unknown candidate kind")
                    received_at = as_utc(received_at or datetime.now(UTC))
                    event_time = as_utc(timestamp)
                    minute = minute_utc(event_time)
                    if kind != "tick" and minute != event_time:
                    raise ValueError("Bar timestamps must identify the exact minute start")
                    if event_time > received_at + timedelta(seconds=30):
                    raise ValueError("Future event timestamp")
                    if not regular_session(minute):
                    return False
                    price = positive_price(close)
                    if provider == "TRADINGVIEW" and kind != "webhook":
                    raise ValueError("TradingView requires aligned webhook records")
                    if reported_rsi is not None:
                    if isinstance(reported_rsi, bool):
                    raise ValueError("Invalid RSI")
                    reported_rsi = float(reported_rsi)
                    if not math.isfinite(reported_rsi) or not 0 <= reported_rsi <= 100:
                    raise ValueError("Invalid RSI")
                    warmup = minute < self.report_start
                    first_deadline = self.report_start + MINUTE + self.delay
                    deadline = first_deadline if warmup else minute + MINUTE + self.delay
                    if (
                        self.last_finalized is not None and minute <= self.last_finalized
                    ) or received_at > deadline:
                    self.late_counts[provider] += 1
                    return False
                    if kind == "webhook" and received_at < minute + MINUTE:
                    raise ValueError("bar_closed=true cannot precede the minute end")
                    if (
                        warmup
                        and minute.astimezone(MARKET_TZ).date()
                        != self.report_start.astimezone(MARKET_TZ).date()
                    ):
                    return False
                    key = (provider, symbol, minute)
                    old = self.pending.get(key)
                    priority = 3 if source == "REST_BACKFILL" else (1 if kind == "tick" else 2)
                    old_priority = (
                        (3 if old.source == "REST_BACKFILL" else (1 if old.kind == "tick" else 2))
                        if old
                        else -1
                    )
                    if old and (
                        priority < old_priority
                        or (priority == old_priority and event_time < old.event_time)
                    ):
                    return False
                    self.pending[key] = MinuteCandidate(
                        price, event_time, received_at, source, kind, reported_rsi
                    )
                    return True
                    def seed_history(self, provider, symbol, bars):


                    """Warm a provider's RSI state with historical bars (startup only)."""
                    if provider == "TRADINGVIEW" or (provider, symbol) not in self.trackers:
                    return 0
                    tracker = self.trackers[(provider, symbol)]
                    applied = 0
                    for bar in sorted(bars, key=lambda item: item.timestamp):
                    before = tracker.changes
                    tracker.process_bar(bar, "WARMUP_BACKFILL", warmup=True)
                    if tracker.changes != before or tracker.previous_close is not None:
                    applied += 1
                    logging.info(
                        "RSI_WARMUP provider=%s symbol=%s bars=%d applied=%d changes=%d "
                        "converged=%s",
                        provider,
                        symbol,
                        len(bars),
                        applied,
                        tracker.changes,
                        tracker.average_gain is not None,
                    )
                    return applied
                    def _apply(self, provider, symbol, minute, candidate):


                    tracker = self.trackers[(provider, symbol)]
                    result = tracker.process_bar(Bar(minute, candidate.close), candidate.source)
                    day = minute.astimezone(MARKET_TZ).date()
                    count_key = (provider, symbol, day)
                    self.counts[count_key] = self.counts.get(count_key, 0) + 1
                    return result
                    def _symbol_record(self, provider, symbol, minute):


                    key = (provider, symbol, minute)
                    candidate = self.pending.pop(key, None)
                    base = {"close": None, "rsi": None, "status": "missing"}
                    if provider != "TRADINGVIEW":
                    tracker = self.trackers[(provider, symbol)]
                    day = minute.astimezone(MARKET_TZ).date()
                    if tracker.session_date != day:
                    tracker.begin_session(day)
                    warmup = sorted(
                        other
                        for other in self.pending
                        if other[0] == provider
                        and other[1] == symbol
                        and other[2] < minute
                        and other[2].astimezone(MARKET_TZ).date() == day
                    )
                    for other in warmup:
                    self._apply(provider, symbol, other[2], self.pending.pop(other))
                    observation = None
                    if candidate:
                    observation = self._apply(provider, symbol, minute, candidate)
                    opening, _ = session_bounds(day)
                    expected = int((minute - opening) / MINUTE) + 1
                    observed = self.counts.get((provider, symbol, day), 0)
                    base.update(
                        {
                            "rsi_source": "local_session_rsi",
                            "observed_closes": observed,
                            "expected_minutes": expected,
                            "missing_minutes": max(0, expected - observed),
                        }
                    )
                    if observation:
                    base["rsi"] = observation["rsi"]
                    if candidate:
                    base["status"] = "ok" if observation else "warming_up"
                    elif candidate:
                    signal = rsi_signal(candidate.reported_rsi)
                    base.update(
                        {
                            "rsi": candidate.reported_rsi,
                            "rsi_source": "webhook_reported",
                            "signal": signal,
                            "status": (
                                "ok"
                                if candidate.reported_rsi is not None
                                else "rsi_unavailable"
                            ),
                        }
                    )
                    if candidate:
                    base.update(
                        {
                            "close": candidate.close,
                            "source": candidate.source,
                            "event_time_utc": candidate.event_time.isoformat(),
                            "received_at_utc": candidate.received_at.isoformat(),
                            "aggregation": (
                                "last_received_event"
                                if candidate.kind == "tick"
                                else "source_bar"
                            ),
                        }
                    )
                    if candidate.kind == "tick":
                    partial = self.started_at > minute or any(
                        start < minute + MINUTE and (end is None or end > minute)
                        for start, end in self.outages[provider]
                    )
                    base["partial"] = partial
                    if partial:
                    base["status"] = "partial"
                    return base
                    def finalize_due(self, now=None):


                    now = as_utc(now or datetime.now(UTC))
                    emitted = 0
                    while now >= self.next_minute + MINUTE + self.delay:
                    minute = self.next_minute
                    local = minute.astimezone(MARKET_TZ)
                    for provider in self.providers:
                    record = {
                        "type": "MINUTE_SUMMARY",
                        "run_id": RUN_ID,
                        "provider": provider,
                        "minute_start_utc": minute.isoformat(),
                        "minute_start": local.isoformat(),
                        "minute_end_utc": (minute + MINUTE).isoformat(),
                        "deadline_utc": (minute + MINUTE + self.delay).isoformat(),
                        "emitted_at_utc": now.isoformat(),
                        "connection_at_emission": (
                            "connected" if self.connected[provider] else "disconnected"
                        ),
                        "late_records_since_previous_summary": self.late_counts[provider],
                        "symbols": {
                            symbol: self._symbol_record(provider, symbol, minute)
                            for symbol in self.symbols
                        },
                    }
                    self.emit(record)
                    self.late_counts[provider] = 0
                    emitted += 1
                    self.last_finalized = minute
                    self.next_minute = next_session_minute(minute + MINUTE)
                    next_day = self.next_minute.astimezone(MARKET_TZ).date()
                    self.pending = {
                        key: value
                        for key, value in self.pending.items()
                        if key[2].astimezone(MARKET_TZ).date() >= next_day
                    }
                    self.counts = {
                        key: value for key, value in self.counts.items() if key[2] >= next_day
                    }
                    for provider in self.providers:
                    self.outages[provider] = [
                        (a, b) for a, b in self.outages[provider] if b is None or b > minute
                    ]
                    return emitted
                    async def run(self):
                    while True:
                    self.finalize_due()
                    await asyncio.sleep(0.25)
                    class ProviderRouter:
                    def init(self, provider, symbols, credentials, coordinator):


                    self.provider = provider
                    self.symbols = tuple(symbols)
                    self.credentials = credentials
                    self.coordinator = coordinator
                    self.trackers = {s: coordinator.trackers[(provider, s)] for s in self.symbols}
                    self.logger = make_logger(provider, "_STREAM")
                    self.history_lock = asyncio.Lock()
                    self.history_requests = asyncio.Queue(maxsize=1000)
                    self.recovery_pending = set()
                    self.historical = None
                    self.stream = None
                    if provider == "ALPACA":
                    self.historical = StockHistoricalDataClient(
                        credentials["alpaca_key"], credentials["alpaca_secret"]
                    )
                    self.stream = StockDataStream(
                        credentials["alpaca_key"], credentials["alpaca_secret"], feed=FEED
                    )
                    def enqueue(self, symbol, kind, value):


                    if symbol not in self.symbols:
                    return
                    if kind == "recover":
                    through = minute_utc(value)
                    key = (symbol, through)
                    if key not in self.recovery_pending:
                    self.history_requests.put_nowait(key)
                    self.recovery_pending.add(key)
                    elif kind == "bar":
                    self.coordinator.submit(
                        self.provider,
                        symbol,
                        value.timestamp,
                        value.close,
                        source=f"{self.provider}_BAR",
                    )
                    elif kind == "tick":
                    timestamp, price = value
                    self.coordinator.submit(
                        self.provider,
                        symbol,
                        timestamp,
                        price,
                        source=f"{self.provider}_WS_MINUTE",
                        kind="tick",
                    )
                    else:
                    raise ValueError(f"Unknown event kind: {kind!r}")
                    async def run(self):
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
                    message = f"provider={self.provider} " f"error_type={type(exc).name}"
                    self.logger.error("%s", message)
                    raise RuntimeError(message) from exc
                    async def _run(self):
                    timeout = aiohttp.ClientTimeout(total=30, connect=10)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                    self.session = session
                    await self.warmup()
                    collector = {
                        "ALPACA": self.run_alpaca,
                        "YAHOOFINANCE": self.run_yfinance,
                        "TWELVEDATA": self.run_websocket,
                        "TRADIER": self.run_websocket,
                    }[self.provider]
                    tasks = [
                        asyncio.create_task(self.consume()),
                        asyncio.create_task(collector()),
                    ]
                    try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                    task.result()
                    raise RuntimeError("Provider worker exited unexpectedly")
                    finally:
                    self.coordinator.set_connected(self.provider, False)
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
                    start, end = normalized_time(start), normalized_time(end)
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
                    def prior_sessions(self):


                    if RSI_WARMUP_SESSIONS <= 0:
                    return []
                    sessions = []
                    day = datetime.now(MARKET_TZ).date() - timedelta(days=1)
                    for _ in range(RSI_WARMUP_SESSIONS * 5 + 10):
                    if len(sessions) >= RSI_WARMUP_SESSIONS:
                    break
                    bounds = session_bounds(day)
                    if bounds is not None:
                    sessions.append((day, bounds))
                    day -= timedelta(days=1)
                    return list(reversed(sessions))
                    async def warmup(self):
                    sessions = self.prior_sessions()
                    if not sessions:
                    return
                    for symbol in self.symbols:
                    applied_total = 0
                    for day, (opening, closing) in sessions:
                    try:
                    async with self.history_lock:
                    bars = await self.fetch_history(symbol, opening, closing)
                    except FatalProviderError:
                    raise
                    except Exception as exc:
                    self.logger.warning(
                        "RSI_WARMUP_SKIP provider=%s symbol=%s date=%s "
                        "error_type=%s",
                        self.provider,
                        symbol,
                        day,
                        type(exc).name,
                    )
                    continue
                    applied_total += self.coordinator.seed_history(
                        self.provider, symbol, bars
                    )
                    self.logger.info(
                        "RSI_WARMUP_DONE provider=%s symbol=%s sessions=%d bars_applied=%d",
                        self.provider,
                        symbol,
                        len(sessions),
                        applied_total,
                    )
                    async def recover(self, symbol, through):
                    through = minute_utc(through)
                    day = through.astimezone(MARKET_TZ).date()
                    bounds = session_bounds(day)
                    if bounds is None:
                    return
                    start, closing = bounds
                    end = min(through, closing, minute_utc(datetime.now(UTC)))
                    if self.coordinator.last_finalized is not None:
                    start = max(start, self.coordinator.last_finalized + MINUTE)
                    if start >= end:
                    return
                    async with self.history_lock:
                    bars = await self.fetch_history(symbol, start, end)
                    accepted = 0
                    for bar in sorted(bars, key=lambda item: item.timestamp):
                    timestamp = as_utc(bar.timestamp)
                    if start <= timestamp < end:
                    accepted += self.coordinator.submit(
                        self.provider, symbol, timestamp, bar.close, source="REST_BACKFILL"
                    )
                    self.logger.info(
                        "BACKFILL symbol=%s received=%d accepted=%d start=%s end=%s",
                        symbol,
                        len(bars),
                        accepted,
                        start.isoformat(),
                        end.isoformat(),
                    )
                    async def consume(self):
                    while True:
                    symbol, through = await self.history_requests.get()
                    try:
                    await self.recover(symbol, through)
                    except FatalProviderError:
                    raise
                    except asyncio.CancelledError:
                    raise
                    except Exception as exc:
                    self.logger.warning(
                        "RECOVERY_FAILED symbol=%s error_type=%s",
                        symbol,
                        type(exc).name,
                    )
                    finally:
                    self.recovery_pending.discard((symbol, through))
                    self.history_requests.task_done()
                    async def accept_bar(self, symbol, bar, source):
                    return self.coordinator.submit(
                        self.provider, symbol, bar.timestamp, bar.close, source=source
                    )
                    async def run_alpaca(self):
                    async def callback(bar):
                    if bar.symbol not in self.symbols:
                    return
                    self.coordinator.set_connected(self.provider, True)
                    self.enqueue(bar.symbol, "bar", Bar(bar.timestamp, bar.close))
                    async def bootstrap():
                    for symbol in self.symbols:
                    self.enqueue(symbol, "recover", datetime.now(UTC))
                    await self.stream._run_forever()
                    self.stream.subscribe_bars(callback, *self.symbols)
                    subscribe_updates = getattr(self.stream, "subscribe_updated_bars", None)
                    if subscribe_updates is not None:
                    subscribe_updates(callback, *self.symbols)
                    await bootstrap()
                    async def run_yfinance(self):
                    for symbol in self.symbols:
                    self.enqueue(symbol, "recover", datetime.now(UTC))
                    self.logger.info("POLLING_STARTED symbols=%s", self.symbols)
                    while True:
                    for symbol in self.symbols:
                    try:
                    now = datetime.now(UTC)
                    async with self.history_lock:
                    bars = await self.fetch_history(
                        symbol, now - timedelta(minutes=5), now
                    )
                    self.coordinator.set_connected(self.provider, True)
                    for bar in bars:
                    self.enqueue(symbol, "bar", bar)
                    except asyncio.CancelledError:
                    raise
                    except FatalProviderError:
                    raise
                    except Exception as exc:
                    self.coordinator.set_connected(self.provider, False)
                    self.logger.warning(
                        "YAHOOFINANCE_POLL_ERROR symbol=%s error_type=%s",
                        symbol,
                        type(exc).name,
                    )
                    await asyncio.sleep(YAHOO_POLL_SECONDS)
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
                    self.coordinator.set_connected(self.provider, True)
                    cutoff = datetime.now(MARKET_TZ)
                    for symbol in self.symbols:
                    self.enqueue(symbol, "recover", cutoff)
                    self.logger.info(
                        "CONNECTED symbols=%s partial_connection_minute=true",
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
                        type(exc).name,
                    )
                    finally:
                    self.coordinator.set_connected(self.provider, False)
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
                    self.logger.warning("INVALID_EVENT non_object=true")
                    return
                    is_tick = (
                        event.get("event") == "price"
                        if self.provider == "TWELVEDATA"
                        else event.get("type") == "trade"
                    )
                    if not is_tick:
                    safe = sanitize_provider_event(
                        event, secrets=tuple(self.credentials.values())
                    )
                    details = json.dumps(safe, separators=(",", ":"), default=str)
                    event_type = str(event.get("event", event.get("type", ""))).lower()
                    status = str(event.get("status", "")).lower()
                    code = event.get("code")
                    is_error = (
                        event_type == "error"
                        or status in {"error", "failed", "failure"}
                        or (isinstance(code, int) and code >= 400)
                    )
                    self.logger.log(
                        logging.ERROR if is_error else logging.INFO,
                        "WS_CONTROL provider=%s payload=%s",
                        self.provider,
                        details,
                    )
                    if is_error:
                    raise FatalProviderError(f"WebSocket rejected request: {details}")
                    return
                    symbol = str(event.get("symbol", "")).upper()
                    if symbol not in self.symbols:
                    return
                    try:
                    raw = event["timestamp"] if self.provider == "TWELVEDATA" else event["date"]
                    epoch = float(raw) / (1 if self.provider == "TWELVEDATA" else 1000)
                    if not math.isfinite(epoch):
                    raise ValueError("Nonfinite timestamp")
                    timestamp = datetime.fromtimestamp(epoch, UTC)
                    price = positive_price(event["price"])
                    self.enqueue(symbol, "tick", (timestamp, price))
                    except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    self.logger.warning("INVALID_TICK symbol=%s", symbol)
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
                        "Tradier token contains surrounding quotes. Remove literal quotes."
                    )
                    if not token or any(character.isspace() for character in token):
                    raise ValueError("Tradier token is empty or contains whitespace")
                    logging.info(
                        "Tradier credential loaded source=%s token_present=true",
                        source,
                    )
                    return token
                    def parse_provider_enabled(value, provider):


                    if value is None:
                    return False
                    if isinstance(value, bool):
                    return value
                    if not isinstance(value, str):
                    raise ValueError(f"[providers] {provider}.enabled must be True or False")
                    flag = value.strip()
                    if len(flag) >= 2 and flag[0] == flag[-1] and flag[0] in {"'", '"'}:
                    flag = flag[1:-1].strip()
                    flag = flag.casefold()
                    if flag == "true":
                    return True
                    if flag == "false":
                    return False
                    raise ValueError(
                        f"[providers] {provider}.enabled must be True or False; got {value!r}"
                    )
                    def load_settings():


                    global selected, required
                    selected = []
                    required = {
                        "ALPACA": ["alpaca_key", "alpaca_secret"],
                        "TRADIER": ["tradier_token"],
                        "TWELVEDATA": ["twelvedata_key"],
                        "YAHOOFINANCE": [],
                        "TRADINGVIEW": [],
                    }
                    config_path = Path(file).resolve().parent / "lib" / "parms.ini"
                    if not config_path.is_file():
                    raise FileNotFoundError(f"Configuration file not found: {config_path}")
                    provider_config = configparser.ConfigParser(interpolation=None)
                    with config_path.open("r", encoding="utf-8-sig") as handle:
                    provider_config.read_file(handle)
                    if not provider_config.has_section("providers"):
                    raise ValueError("Configuration is missing the [providers] section")
                    recognized = {f"{provider}.enabled".lower() for provider in required}
                    for option in provider_config.options("providers"):
                    if option.lower().endswith(".enabled") and option.lower() not in recognized:
                    raise ValueError(f"Unknown provider flag in [providers]: {option}")
                    for provider in required:
                    option = f"{provider}.enabled"
                    raw_flag = provider_config.get("providers", option, fallback=None)
                    enabled = parse_provider_enabled(raw_flag, provider)
                    logging.info("PROVIDER_CONFIG provider=%s enabled=%s", provider, enabled)
                    if enabled:
                    selected.append(provider)
                    if not selected:
                    logging.warning(
                        "NO_PROVIDERS_ENABLED: no collectors, webhook or summaries will start"
                    )
                    return [], {}
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
                    credentials = {}
                    if selected:
                    credentials = getattr(config, "CREDENTIALS", {}) or {}
                    if not isinstance(credentials, dict):
                    raise ValueError("CREDENTIALS must be a dictionary")
                    keys = {
                        "alpaca_key": "",
                        "alpaca_secret": "",
                        "tradier_token": "",
                        "twelvedata_key": "",
                    }
                    if "ALPACA" in selected or "TRADINGVIEW" in selected:
                    keys["alpaca_key"] = credentials.get("api_key") or os.getenv(
                        "ALPACA_API_KEY", ""
                    )
                    keys["alpaca_secret"] = credentials.get("secret_key") or os.getenv(
                        "ALPACA_SECRET_KEY", ""
                    )
                    if "TRADIER" in selected:
                    keys["tradier_token"] = load_tradier_token(config, credentials)
                    if "TWELVEDATA" in selected:
                    td_key = os.getenv("TWELVEDATA_API_KEY", "").strip()
                    if not td_key:
                    try:
                    td_key = config.get("twelvedata_api", "twelvedata_api_key")
                    except Exception:
                    td_key = ""
                    keys["twelvedata_key"] = td_key
                    keys = {
                        key: value.strip() if isinstance(value, str) else ""
                        for key, value in keys.items()
                    }
                    for provider in selected:
                    missing = [key for key in required[provider] if not keys.get(key)]
                    if missing:
                    raise ValueError(
                        f"Missing credentials for enabled provider {provider}: {
                            ', '.join(missing)}")
                    logging.info("PROVIDERS_SELECTED providers=%s symbols=%s", selected, symbols)
                    return symbols, keys
                    WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "127.0.0.1")
                    WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
                    WEBHOOK_QUEUE_LIMIT = 2000
                    WEBHOOK_MAX_BYTES = 16384
                    WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
                    OPTION_CHAIN_RSI_LOW = float(os.getenv("OPTION_CHAIN_RSI_LOW", "35"))
                    OPTION_CHAIN_RSI_HIGH = float(os.getenv("OPTION_CHAIN_RSI_HIGH", "65"))
                    def rsi_signal(rsi):


                    if rsi is None:
                    return None
                    if rsi <= OPTION_CHAIN_RSI_LOW:
                    return "buy"
                    if rsi >= OPTION_CHAIN_RSI_HIGH:
                    return "sell"
                    return None
                    def validate_tradingview_payload(payload, allowed_symbols):


                    if not isinstance(payload, dict):
                    raise ValueError("JSON payload must be an object")
                    ticker = payload.get("ticker")
                    if not isinstance(ticker, str) or not ticker.strip() or len(ticker) > 100:
                    raise ValueError("ticker must be a nonempty string of at most 100 characters")
                    ticker = ticker.strip().upper()
                    symbol = ticker if ticker in allowed_symbols else ticker.rsplit(":", 1)[-1]
                    if symbol not in allowed_symbols:
                    raise ValueError("ticker is not in configured target_symbols")
                    try:
                    price = positive_price(payload.get("price"))
                    except (TypeError, ValueError, OverflowError):
                    raise ValueError("price must be finite and positive") from None
                    raw_rsi = payload.get(
                        "rsi_value") if "rsi_value" in payload else payload.get("rsi")
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
                    if payload.get("timeframe") != "1m":
                    raise ValueError('timeframe must be "1m"')
                    if payload.get("bar_closed") is not True:
                    raise ValueError("bar_closed must be JSON true")
                    raw_time = payload.get("bar_start")
                    if not isinstance(raw_time, str) or not raw_time or len(raw_time) > 100:
                    raise ValueError(
                        "bar_start must be a timezone-aware ISO-8601 minute-start string"
                    )
                    try:
                    timestamp = as_utc(datetime.fromisoformat(raw_time.replace("Z", "+00:00")))
                    if timestamp != minute_utc(timestamp):
                    raise ValueError()
                    now = datetime.now(UTC)
                    if now < timestamp:
                    raise ValueError()
                    if not regular_session(timestamp):
                    raise ValueError()
                    except (TypeError, ValueError, OverflowError):
                    raise ValueError(
                        "bar_start must identify a completed regular-session minute"
                    ) from None
                    return {
                        "symbol": symbol,
                        "ticker": ticker,
                        "price": price,
                        "rsi_value": rsi,
                        "event_time": timestamp.isoformat(),
                        "received_at": now.isoformat(),
                        "timeframe": "1m",
                        "bar_closed": True,
                    }
                    def build_option_chain_client(keys):


                    if not keys.get("alpaca_key") or not keys.get("alpaca_secret"):
                    logging.warning(
                        "OPTION_CHAIN_DISABLED reason=missing_alpaca_credentials "
                        "hint=provide api_key/secret_key in parms.ini CREDENTIALS or "
                        "ALPACA_API_KEY/ALPACA_SECRET_KEY environment variables"
                    )
                    return None
                    return TradingClient(keys["alpaca_key"], keys["alpaca_secret"])
                    class TradingviewProvider:


                    def init(
                        self,
                        symbols,
                        coordinator,
                        host=WEBHOOK_HOST,
                        port=WEBHOOK_PORT,
                        credentials=None,
                        option_client=None,
                    ):


                    if not WEBHOOK_TOKEN and host not in {"127.0.0.1", "localhost", "::1"}:
                    raise ValueError("Set WEBHOOK_TOKEN before binding a non-loopback host")
                    self.provider = "TRADINGVIEW"
                    self.symbols = set(symbols)
                    self.coordinator = coordinator
                    self.host, self.port = host, port
                    self.credentials = dict(credentials or {})
                    self.option_client = option_client
                    self.option_chain_lock = asyncio.Lock()
                    self.kinematics_registry = {}
                    self.queue = queue.Queue(maxsize=WEBHOOK_QUEUE_LIMIT)
                    self.stop = threading.Event()
                    self.server = None
                    self.thread = None
                    self.thread_error = None
                    self.logger = make_logger(self.provider, "_STREAM")
                    self.latest = {}
                    self.app = self.create_app()
                    def create_app(self):


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


                    payload = request.get_json(force=True, silent=True)
                    if not isinstance(payload, dict):
                    return (
                        jsonify(status="error", message="JSON payload must be an object"),
                        400,
                    )
                    if WEBHOOK_TOKEN:
                    supplied = request.headers.get("X-Webhook-Token")
                    if supplied is None:
                    supplied = payload.get("webhook_token", "")
                    if not isinstance(supplied, str) or not hmac.compare_digest(
                        supplied.encode("utf-8"), WEBHOOK_TOKEN.encode("utf-8")
                    ):
                    return jsonify(status="error", message="Unauthorized"), 401
                    try:
                    event = validate_tradingview_payload(
                        payload,
                        self.symbols,
                    )
                    if not isinstance(event, dict) or "symbol" not in event:
                    print(
                        f"\n[CRITICAL ERROR]: validate_tradingview_payload returned an invalid structure."
                    )
                    return (
                        jsonify(
                            status="error",
                            message="Internal payload structural mismatch",
                        ),
                        500,
                    )
                    self.queue.put_nowait(event)
                    return (
                        jsonify(
                            status="queued",
                            provider=self.provider,
                            received=event["symbol"],
                        ),
                        200,
                    )
                    except ValueError as exc:
                    print(f"[VALIDATION FAILED]: {exc}")
                    return jsonify(status="error", message=str(exc)), 400
                    except queue.Full:
                    return (
                        jsonify(status="error", message="Receiver busy; alert not queued"),
                        503,
                    )
                    except Exception as system_err:
                    print(
                        "\n=================== CRITICAL SERVER TRACEBACK (500) ==================="
                    )
                    traceback.print_exc()
                    print(
                        "=======================================================================\n"
                    )
                    return (
                        jsonify(status="error", message="Internal Server Processing Error"),
                        500,
                    )
                    return app
                    def serve(self):


                    try:
                    while not self.stop.is_set():
                    self.server.handle_request()
                    except Exception as exc:
                    self.thread_error = type(exc).name
                    finally:
                    self.server.server_close()
                    def record(self, event):


                    accepted = self.coordinator.submit(
                        self.provider,
                        event["symbol"],
                        datetime.fromisoformat(event["event_time"]),
                        event["price"],
                        source="TRADINGVIEW_WEBHOOK",
                        kind="webhook",
                        reported_rsi=event["rsi_value"],
                    )
                    self.latest[event["symbol"]] = {**event, "accepted_for_summary": accepted}
                    self.logger.debug(
                        "WEBHOOK_ACCEPTED symbol=%s accepted=%s", event["symbol"], accepted
                    )
                    def schedule_option_chain(self, event):


                    """Evaluates entry thresholds against velocity metrics and active position matrices."""
                    rsi = event.get("rsi_value")
                    if rsi is None:
                    return
                    symbol = event["symbol"]
                    if symbol not in self.kinematics_registry:
                    self.kinematics_registry[symbol] = SymbolKinematics(symbol)
                    tracker = self.kinematics_registry[symbol]
                    tracker.push(rsi, event["event_time"])
                    velocity, acceleration = tracker.calculate_vectors
                    signal = rsi_signal(rsi)
                    current_profit_pc = 0.0
                    has_open_position = False
                    if self.option_client:
                    try:
                    positions = self.option_client.get_all_positions() or []
                    for p in positions:
                    pos_symbol = getattr(p, 'symbol', '')
                    if pos_symbol.startswith(symbol) or symbol in pos_symbol:
                    has_open_position = True
                    cost_basis = float(getattr(p, 'avg_entry_price', 1.0) or 1.0)
                    cur_price = float(getattr(p, 'current_price', 0.0) or 0.0)
                    if cost_basis > 0:
                    current_profit_pc = (cur_price - cost_basis) / cost_basis
                    except Exception as e:
                    self.logger.warning(f"Portfolio lookup bypass for matrix execution: {e}")
                    if signal == "buy" and not has_open_position:
                    if velocity < -VELOCITY_THRESHOLD:
                    self.logger.info(
                        f"BUY_SIGNAL_DEFERRED symbol={symbol} rsi={rsi:.2f} "
                        f"Reason=Falling Knife (Velocity={velocity:.4f} is too deeply negative)"
                    )
                    return
                    self.logger.info(
                        f"MUTATED_EXECUTION_TRIGGER ACTION=BUY symbol={symbol} rsi={rsi:.2f} "
                        f"V={velocity:.4f} A={acceleration:.4f}"
                    )
                    logging.info(
                        f"MUTATED_EXECUTION_TRIGGER ACTION=BUY symbol={symbol} rsi={
                            rsi:.2f}")
                    asyncio.create_task(self.collect_option_chain(event, "buy"))
                    elif has_open_position:
                    met_rsi_target = (rsi >= OPTION_CHAIN_RSI_HIGH)
                    met_alpha_take = (current_profit_pc >=
                                    PROFIT_TAKE_PERCENT and acceleration <= 0.0)
                    met_exhaustion = (rsi > 60.0 and velocity < -VELOCITY_THRESHOLD)
                    if met_rsi_target or met_alpha_take or met_exhaustion:
                    reason_label = (
                        "RSI_HIGH" if met_rsi_target else
                        ("ALPHA_PROFIT_TAKE" if met_alpha_take else "MOMENTUM_EXHAUSTION")
                    )
                    self.logger.info(
                        f"MUTATED_EXECUTION_TRIGGER ACTION=SELL symbol={symbol} rsi={
                            rsi:.2f} " f"Reason={reason_label} PnL={
                                current_profit_pc *
                                100:.2f}% V={
                                    velocity:.4f}")
                    logging.info(
                        f"MUTATED_EXECUTION_TRIGGER ACTION=SELL symbol={symbol} reason={reason_label}")
                    asyncio.create_task(self.collect_option_chain(event, "sell"))
                    async def collect_option_chain(self, event, signal):
                    symbol = event["symbol"]
                    rsi = event["rsi_value"]
                    label = signal.upper()
                    async with self.option_chain_lock:
                    try:
                    contract, clean_payload = await asyncio.to_thread(
                        get_pruned_option_chain,
                        symbol,
                        self.credentials["alpaca_key"],
                        self.credentials["alpaca_secret"],
                        self.option_client,
                    )
                    except Exception as exc:
                    self.logger.error(
                        "OPTION_CHAIN_FAILED signal=%s symbol=%s rsi=%s error_type=%s detail=%s",
                        label,
                        symbol,
                        "warming-up" if rsi is None else f"{rsi:.2f}",
                        type(exc).name,
                        exc,
                    )
                    return
                    try:
                    path = self.write_option_chain_payload(
                        symbol, rsi, signal, event, clean_payload
                    )
                    except Exception as exc:
                    self.logger.error(
                        "OPTION_CHAIN_WRITE_FAILED signal=%s symbol=%s rsi=%s error_type=%s detail=%s",
                        label,
                        symbol,
                        "warming-up" if rsi is None else f"{rsi:.2f}",
                        type(exc).name,
                        exc,
                    )
                    return
                    self.logger.info(
                        "OPTION_CHAIN_CONTRACT provider=%s signal=%s symbol=%s rsi=%s contract=%s file=%s",
                        self.provider,
                        label,
                        symbol,
                        "warming-up" if rsi is None else f"{rsi:.2f}",
                        contract,
                        path.name,
                    )


                    def write_option_chain_payload(
                            self,
                            symbol,
                            rsi,
                            signal,
                            event,
                            clean_payload):


                    directory = LOG_ROOT / "optionchains"
                    directory.mkdir(parents=True, exist_ok=True)
                    safe = "".join(c if c.isalnum() or c in "-." else "" for c in symbol)
                    try:
                    bar_time = datetime.fromisoformat(event["event_time"])
                    stamp = bar_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
                    except (KeyError, TypeError, ValueError):
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                    rsi_str = "warmup" if rsi is None else f"{rsi:.2f}"
                    filename = (
                        f"optionchain-{self.provider}-{safe}-{signal.upper()}-rsi{rsi_str}-"
                        f"{stamp}-{uuid4().hex[:6]}.json"
                    )
                    path = directory / filename
                    path.write_text(
                        json.dumps(clean_payload, indent=2, default=str), encoding="utf-8"
                    )
                    return path
                    async def run(self):
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
                    self.coordinator.set_connected(self.provider, True)
                    logging.info(
                        "TRADINGVIEW listening host=%s port=%d route=/webhook authentication=%s",
                        self.host,
                        self.port,
                        "token" if WEBHOOK_TOKEN else "none-loopback-only",
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
                    self.schedule_option_chain(event)
                    finally:
                    self.queue.task_done()
                    if not self.thread.is_alive():
                    raise RuntimeError(
                        "TRADINGVIEW server thread stopped: " + str(self.thread_error)
                    )
                    await asyncio.sleep(0.05)
                    finally:
                    self.coordinator.set_connected(self.provider, False)
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
                    class AlpacaPositionMonitor:
                    def init(self, keys):


                    self.provider = "POSITIONS"
                    self.credentials = dict(keys or {})
                    self.logger = make_logger("POSITIONS", "ALPACA")
                    self.client = build_option_chain_client(self.credentials)
                    @staticmethod
                    def _is_option(position):


                    raw = getattr(position, "asset_class", None)
                    value = str(getattr(raw, "value", raw) or "").lower()
                    return "option" in value
                    @staticmethod
                    def _num(position, field):


                    try:
                    value = float(getattr(position, field) or 0.0)
                    return value if math.isfinite(value) else 0.0
                    except (TypeError, ValueError, OverflowError):
                    return 0.0
                    def _format_option(self, position):


                    return (
                        f"symbol={getattr(position, 'symbol', '?')} "
                        f"qty={self._num(position, 'qty'):.4g} "
                        f"side={getattr(position, 'side', '?')} "
                        f"avg_entry={self._num(position, 'avg_entry_price'):.4f} "
                        f"current_price={self._num(position, 'current_price'):.4f} "
                        f"market_value={self._num(position, 'market_value'):.2f} "
                        f"unrealized_pl={self._num(position, 'unrealized_pl'):.2f} "
                        f"unrealized_plpc={self._num(position, 'unrealized_plpc'):.4f}"
                    )
                    def poll(self):


                    account = self.client.get_account()
                    positions = list(self.client.get_all_positions() or [])
                    options = [p for p in positions if self._is_option(p)]
                    self.logger.info(
                        "POSITIONS_SNAPSHOT equity=%.2f cash=%.2f buying_power=%.2f "
                        "long_market_value=%.2f short_market_value=%.2f positions=%d options=%d",
                        float(getattr(account, "equity", 0.0) or 0.0),
                        float(getattr(account, "cash", 0.0) or 0.0),
                        float(getattr(account, "buying_power", 0.0) or 0.0),
                        float(getattr(account, "long_market_value", 0.0) or 0.0),
                        float(getattr(account, "short_market_value", 0.0) or 0.0),
                        len(positions),
                        len(options),
                    )
                    for position in positions:
                    if self._is_option(position):
                    self.logger.info("OPTION_POSITION %s", self._format_option(position))
                    else:
                    self.logger.info(
                        "HOLDING type=equity symbol=%s qty=%.4g avg_entry=%.4f "
                        "current_price=%.4f market_value=%.2f unrealized_pl=%.2f",
                        getattr(position, "symbol", "?"),
                        self._num(position, "qty"),
                        self._num(position, "avg_entry_price"),
                        self._num(position, "current_price"),
                        self._num(position, "market_value"),
                        self._num(position, "unrealized_pl"),
                    )
                    if not options:
                    self.logger.info("OPTIONS_POSITIONS none")
                    async def run(self):
                    if self.client is None:
                    self.logger.warning(
                        "POSITION_MONITOR_DISABLED reason=missing_alpaca_credentials "
                        "hint=provide api_key/secret_key in parms.ini CREDENTIALS or "
                        "ALPACA_API_KEY/ALPACA_SECRET_KEY environment variables"
                    )
                    return
                    self.logger.info("POSITION_MONITOR_START cadence=every-minute")
                    logging.info(
                        "POSITION_MONITOR_START provider=POSITIONS log=%s",
                        self.logger.handlers[0].baseFilename if self.logger.handlers else "n/a")
                    while True:
                    try:
                    await asyncio.to_thread(self.poll)
                    except asyncio.CancelledError:
                    raise
                    except Exception as exc:
                    self.logger.warning(
                        "POSITION_MONITOR_ERROR error_type=%s detail=%s",
                        type(exc).name,
                        exc,
                    )
                    now = datetime.now(UTC)
                    target = now.replace(second=0, microsecond=0) + MINUTE + timedelta(seconds=2)
                    await asyncio.sleep(max(1.0, (target - now).total_seconds()))
                    PROVIDER_REGISTRY = {}
                    class AdminCliProvider:
                    def init(self, coordinator, host="127.0.0.1", port=5001):


                    self.coordinator = coordinator
                    self.host = host
                    self.port = port
                    self.loop = asyncio.get_running_loop()
                    self.stop = threading.Event()
                    self.server = None
                    self.thread = None
                    self.app = self.create_app()
                    def create_app(self):


                    app = Flask("admin_cli_interface")
                    app.config["MAX_CONTENT_LENGTH"] = 16384
                    @app.route("/cli/status", methods=["GET", "POST"])
                    def get_status():


                    status_report = {
                        "run_id": RUN_ID,
                        "connected_providers": self.coordinator.connected,
                        "tracked_instruments": self.coordinator.symbols,
                        "next_due_minute_utc": self.coordinator.next_minute.isoformat(),
                        "pending_queue_size": len(self.coordinator.pending),
                        "loaded_providers": list(PROVIDER_REGISTRY.keys()),
                    }
                    return jsonify(status="ok", metrics=status_report), 200
                    @app.post("/cli/finalize")
                    def force_finalize():
                    def run_finalize():


                    return self.coordinator.finalize_due()
                    future = asyncio.run_coroutine_threadsafe(
                        asyncio.to_thread(run_finalize), self.loop
                    )
                    emitted = future.result(timeout=5.0)
                    return (
                        jsonify(
                            status="ok", message=f"Flush complete. Emitted {emitted} summaries."
                        ),
                        200,
                    )
                    @app.post("/cli/inject")
                    def inject_bar():


                    payload = request.get_json(force=True, silent=True) or {}
                    provider = payload.get("provider")
                    "other_providers_continue=true "
                    "restart or CLI reconnect required to retry",
                    router.provider,
                    exc,
                    )
                        await asyncio.Event().wait()
                        except Exception as exc:
                        coordinator.set_connected(router.provider, False)
                        logging.error(
                    "PROVIDER_CRASHED provider=%s error=%s", router.provider, type(exc))
