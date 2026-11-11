"""Multi-provider RSI with a dedicated-thread TRADINGVIEW webhook receiver.

Python 3.12 recommended.
Dependencies: aiohttp, alpaca-py, Flask, yfinance, pandas, pandas-market-calendars,
tzdata, and the existing project-local lib.utils.JsonIniParser.
Uses NYSE calendar holidays and early closes. Summary minute keys use UTC.
One JSON summary per regular-session minute per provider in a dedicated JSONL log.
Each symbol record includes 1-minute velocity and acceleration metrics for both RSI
and Price (rsi_velocity, rsi_acceleration, price_velocity, price_velocity_pct,
price_acceleration, plus multi-period 3m momentum and a kinematics regime classifier:
accelerating_bullish, decelerating_bullish, accelerating_bearish, decelerating_bearish).
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
minute-summary record carries the signal label and kinematics metrics.
An AlpacaPositionMonitor thread polls the account's positions every minute,
writes them to its own log file under logs/positions/ and prints each current
option position.
Deduplication is in-memory only and does not survive process restarts.
Provider startup is controlled exclusively by [providers] NAME.enabled in lib/parms.ini.
load_settings populates module globals selected and required; disabled providers
are not instantiated or included in minute summaries.
"""

import asyncio
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
# Populated by load_settings(); provider startup is controlled only by parms.ini.
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


def classify_kinematic_regime(
    rsi_velocity, rsi_acceleration, price_velocity=None, price_acceleration=None
):
    """Classify kinematics into one of 4 phase regimes or neutral.

    Accelerating Bullish: v > 0, a > 0 (buying thrust expanding)
    Decelerating Bullish: v > 0, a <= 0 (buying waning / topping)
    Accelerating Bearish: v < 0, a < 0 (selling impulse accelerating)
    Decelerating Bearish: v < 0, a >= 0 (selling decelerating / absorption)
    """
    if rsi_velocity is not None:
        if rsi_velocity > 0:
            if rsi_acceleration is not None and rsi_acceleration > 0:
                return "accelerating_bullish"
            return "decelerating_bullish"
        elif rsi_velocity < 0:
            if rsi_acceleration is not None and rsi_acceleration < 0:
                return "accelerating_bearish"
            return "decelerating_bearish"

    if price_velocity is not None:
        if price_velocity > 0:
            if price_acceleration is not None and price_acceleration > 0:
                return "accelerating_bullish"
            return "decelerating_bullish"
        elif price_velocity < 0:
            if price_acceleration is not None and price_acceleration < 0:
                return "accelerating_bearish"
            return "decelerating_bearish"

    return "neutral"


class SessionRSI:
    def __init__(
        self, symbol, provider, mode=MODE, period=RSI_PERIOD, cross_session=True
    ):
        if mode not in {"since_open", "wilder"}:
            raise ValueError("Unknown RSI mode")
        if isinstance(period, bool) or not isinstance(period, int) or period < 2:
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
        self.previous_rsi = None
        self.previous_rsi_velocity = None
        self.previous_price_velocity = None
        self.latest_rsi_velocity = None
        self.latest_rsi_acceleration = None
        self.latest_price_velocity = None
        self.latest_price_acceleration = None
        self.latest = None
        self.provider = provider

    def reset_session(self, day):
        self.session_date = day
        self.last_timestamp = self.previous_close = None
        self.changes = 0
        self.gain_sum = self.loss_sum = 0.0
        self.average_gain = self.average_loss = self.latest = None
        self.previous_rsi = None
        self.previous_rsi_velocity = None
        self.previous_price_velocity = None
        self.latest_rsi_velocity = None
        self.latest_rsi_acceleration = None
        self.latest_price_velocity = None
        self.latest_price_acceleration = None
        self.logger.info("SESSION_START date=%s", day)

    def begin_session(self, day):
        """Enter a new session, keeping converged Wilder state when possible.

        With cross_session enabled and the averages already seeded (at least
        `period` changes observed), the averages, previous_close and change
        counter roll forward so the first bar of the day measures the overnight
        gap from the prior close  the same series TradingView smooths over.
        Otherwise this falls back to a cold per-session reset.
        """
        if self.cross_session and self.average_gain is not None:
            # Keep previous_close, average_gain/average_loss and the change
            # counter exactly as they are: the next bar's change is the
            # overnight gap, and the recursion continues without reseeding.
            self.session_date = day
            self.last_timestamp = None
            self.logger.info(
                "SESSION_ROLLOVER date=%s rsi_state=carried avg_gain=%.6f avg_loss=%.6f prev_rsi=%s",
                day,
                self.average_gain or 0.0,
                self.average_loss or 0.0,
                f"{self.previous_rsi:.2f}" if self.previous_rsi is not None else "n/a",
            )
        else:
            self.reset_session(day)

    def process_bar(self, bar, source, warmup=False):
        """Apply one completed minute close to this provider's RSI.

        warmup=True feeds history into the Wilder state without emitting an
        observation; it is used only for startup seeding, never live minutes.
        """
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

        # Compute 1-minute velocity and acceleration metrics
        price_velocity = None
        price_velocity_pct = None
        price_acceleration = None
        if self.previous_close is not None:
            price_velocity = round(close - self.previous_close, 4)
            if self.previous_close > 0:
                price_velocity_pct = round(
                    (price_velocity / self.previous_close) * 100.0, 4
                )
            if self.previous_price_velocity is not None:
                price_acceleration = round(
                    price_velocity - self.previous_price_velocity, 4
                )

        rsi_velocity = None
        rsi_acceleration = None
        if value is not None:
            if self.previous_rsi is not None:
                rsi_velocity = round(value - self.previous_rsi, 4)
                if self.previous_rsi_velocity is not None:
                    rsi_acceleration = round(
                        rsi_velocity - self.previous_rsi_velocity, 4
                    )

        # Update tracking state
        self.previous_price_velocity = price_velocity
        self.latest_price_velocity = price_velocity
        self.latest_price_acceleration = price_acceleration
        self.previous_close = close
        self.last_timestamp = timestamp

        if value is not None:
            if rsi_velocity is not None:
                self.previous_rsi_velocity = rsi_velocity
            self.latest_rsi_velocity = rsi_velocity
            self.latest_rsi_acceleration = rsi_acceleration
            self.previous_rsi = value

        observation = None

        if value is not None and not warmup:
            observation = {
                "provider": self.provider,
                "symbol": self.symbol,
                "datetime": timestamp.isoformat(),
                "close": close,
                "rsi": value,
                "rsi_velocity": rsi_velocity,
                "rsi_acceleration": rsi_acceleration,
                "price_velocity": price_velocity,
                "price_velocity_pct": price_velocity_pct,
                "price_acceleration": price_acceleration,
            }
            self.latest = observation

        message = (
            "MINUTE provider=%s symbol=%s source=%s timestamp=%s "
            "close=%.8f rsi=%s v_rsi=%s a_rsi=%s v_p=%s changes=%d"
        )

        arguments = (
            self.provider,
            self.symbol,
            source,
            timestamp.isoformat(),
            close,
            "warming-up" if value is None else f"{value:.2f}",
            "n/a" if rsi_velocity is None else f"{rsi_velocity:+.2f}",
            "n/a" if rsi_acceleration is None else f"{rsi_acceleration:+.2f}",
            "n/a" if price_velocity is None else f"{price_velocity:+.2f}",
            self.changes,
        )

        self.logger.debug(message, *arguments)

        if source != "REST_BACKFILL":
            logging.debug(message, *arguments)

        return observation


class FatalProviderError(RuntimeError):
    pass


async def request_json(session, method, url, **kwargs):
    for attempt in range(4):
        try:
            async with session.request(method, url, **kwargs) as response:
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
                    await asyncio.sleep(2**attempt)
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


class MinuteCoordinator:
    """Single event-loop owner of pending closes, RSI state and summary output.

    Finalization is immutable within this process. Late records are rejected,
    including REST backfills for a minute whose summary already exists.
    No network requests or blocking queue drains occur in finalization.
    """

    def __init__(
        self, providers, symbols, start=None, delay=SUMMARY_DELAY_SECONDS, emit=None
    ):
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
        self.kinematics_history = {}
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
        # Retain only current/recent sessions, never arbitrary old warmup data.
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
        """Warm a provider's RSI state with historical bars (startup only).

        Bars are applied oldest-first through the normal SessionRSI recursion
        with warmup=True, so they never emit observations or touch pending
        candidates. Only the local RSI state is affected; TradingView's
        webhook-reported values are never seeded.
        """
        if provider == "TRADINGVIEW" or (provider, symbol) not in self.trackers:
            return 0
        tracker = self.trackers[(provider, symbol)]
        applied = 0
        sorted_bars = sorted(bars, key=lambda item: item.timestamp)
        for bar in sorted_bars:
            before = tracker.changes
            tracker.process_bar(bar, "WARMUP_BACKFILL", warmup=True)
            if tracker.changes != before or tracker.previous_close is not None:
                applied += 1
        # Seed warmup kinematics history so initial live summaries have velocity context
        if sorted_bars:
            history = self.kinematics_history.setdefault((provider, symbol), [])
            for b in sorted_bars[-10:]:
                pv = tracker.latest_price_velocity
                pa = tracker.latest_price_acceleration
                rv = tracker.latest_rsi_velocity
                ra = tracker.latest_rsi_acceleration
                reg = classify_kinematic_regime(rv, ra, pv, pa)
                history.append(
                    {
                        "minute": minute_utc(b.timestamp),
                        "timestamp": (
                            b.timestamp.isoformat()
                            if hasattr(b.timestamp, "isoformat")
                            else str(b.timestamp)
                        ),
                        "close": b.close,
                        "rsi": tracker.latest["rsi"] if tracker.latest else None,
                        "price_velocity": pv,
                        "price_velocity_pct": None,
                        "price_acceleration": pa,
                        "rsi_velocity": rv,
                        "rsi_acceleration": ra,
                        "regime": reg,
                    }
                )
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
            # Bootstrap only with data already present at this deadline.
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
                    # BUY when rsi <= OPTION_CHAIN_RSI_LOW, SELL when
                    # rsi >= OPTION_CHAIN_RSI_HIGH, else null (neutral band).
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

        # Compute 1-minute velocity, acceleration, and multi-period momentum metrics
        history = self.kinematics_history.setdefault((provider, symbol), [])
        curr_close = base.get("close")
        curr_rsi = base.get("rsi")

        price_velocity = None
        price_velocity_pct = None
        price_acceleration = None
        price_acceleration_pct = None
        rsi_velocity = None
        rsi_acceleration = None
        rsi_velocity_3m = None
        price_velocity_3m_pct = None
        regime = "neutral"

        if curr_close is not None:
            # Find the most recent history entry with a valid close
            prev_entry = next(
                (e for e in reversed(history) if e.get("close") is not None), None
            )
            if prev_entry is not None:
                prev_close = prev_entry["close"]
                price_velocity = round(curr_close - prev_close, 4)
                if prev_close > 0:
                    price_velocity_pct = round(
                        ((curr_close - prev_close) / prev_close) * 100.0, 4
                    )
                if prev_entry.get("price_velocity") is not None:
                    price_acceleration = round(
                        price_velocity - prev_entry["price_velocity"], 4
                    )
                if (
                    price_velocity_pct is not None
                    and prev_entry.get("price_velocity_pct") is not None
                ):
                    price_acceleration_pct = round(
                        price_velocity_pct - prev_entry["price_velocity_pct"], 4
                    )

            valid_closes = [e for e in history if e.get("close") is not None]
            if len(valid_closes) >= 3:
                c3m = valid_closes[-3]["close"]
                if c3m > 0:
                    price_velocity_3m_pct = round(((curr_close - c3m) / c3m) * 100.0, 4)

        if curr_rsi is not None:
            # Find the most recent history entry with a valid rsi
            prev_rsi_entry = next(
                (e for e in reversed(history) if e.get("rsi") is not None), None
            )
            if prev_rsi_entry is not None:
                prev_rsi = prev_rsi_entry["rsi"]
                rsi_velocity = round(curr_rsi - prev_rsi, 4)
                if prev_rsi_entry.get("rsi_velocity") is not None:
                    rsi_acceleration = round(
                        rsi_velocity - prev_rsi_entry["rsi_velocity"], 4
                    )

            valid_rsis = [e for e in history if e.get("rsi") is not None]
            if len(valid_rsis) >= 3:
                r3m = valid_rsis[-3]["rsi"]
                rsi_velocity_3m = round(curr_rsi - r3m, 4)

        # Classify kinematics regime
        regime = classify_kinematic_regime(
            rsi_velocity,
            rsi_acceleration,
            price_velocity,
            price_acceleration,
        )

        if curr_close is not None or curr_rsi is not None:
            history.append(
                {
                    "minute": minute,
                    "timestamp": (
                        minute.isoformat()
                        if hasattr(minute, "isoformat")
                        else str(minute)
                    ),
                    "close": curr_close,
                    "rsi": curr_rsi,
                    "price_velocity": price_velocity,
                    "price_velocity_pct": price_velocity_pct,
                    "price_acceleration": price_acceleration,
                    "price_acceleration_pct": price_acceleration_pct,
                    "rsi_velocity": rsi_velocity,
                    "rsi_acceleration": rsi_acceleration,
                    "momentum_3m_pct": price_velocity_3m_pct,
                    "regime": regime,
                }
            )
            if len(history) > 60:
                del history[:-60]

        base.update(
            {
                "price_velocity": price_velocity,
                "price_velocity_pct": price_velocity_pct,
                "price_acceleration": price_acceleration,
                "rsi_velocity": rsi_velocity,
                "rsi_acceleration": rsi_acceleration,
                "kinematics": {
                    "price_velocity_1m": price_velocity,
                    "price_velocity_1m_pct": price_velocity_pct,
                    "price_acceleration_1m": price_acceleration,
                    "price_acceleration_1m_pct": price_acceleration_pct,
                    "rsi_velocity_1m": rsi_velocity,
                    "rsi_acceleration_1m": rsi_acceleration,
                    "rsi_velocity_3m": rsi_velocity_3m,
                    "price_velocity_3m_pct": price_velocity_3m_pct,
                    "regime": regime,
                },
            }
        )

        return base

    def finalize_due(self, now=None):
        now = as_utc(now or datetime.now(UTC))
        emitted = 0
        while now >= self.next_minute + MINUTE + self.delay:
            minute = self.next_minute
            local = minute.astimezone(MARKET_TZ)
            for provider in self.providers:
                symbols_data = {
                    symbol: self._symbol_record(provider, symbol, minute)
                    for symbol in self.symbols
                }

                # Compile kinematics summary across symbols for this provider
                kinematics_summary = {
                    "regimes": {
                        "accelerating_bullish": 0,
                        "decelerating_bullish": 0,
                        "accelerating_bearish": 0,
                        "decelerating_bearish": 0,
                        "neutral": 0,
                    },
                    "max_rsi_velocity": None,
                    "min_rsi_velocity": None,
                    "max_price_velocity_pct": None,
                    "min_price_velocity_pct": None,
                }
                for sym, rec in symbols_data.items():
                    k = rec.get("kinematics") or {}
                    r = k.get("regime")
                    if r in kinematics_summary["regimes"]:
                        kinematics_summary["regimes"][r] += 1

                    rv = rec.get("rsi_velocity")
                    if rv is not None:
                        if (
                            kinematics_summary["max_rsi_velocity"] is None
                            or rv > kinematics_summary["max_rsi_velocity"]["value"]
                        ):
                            kinematics_summary["max_rsi_velocity"] = {
                                "symbol": sym,
                                "value": rv,
                            }
                        if (
                            kinematics_summary["min_rsi_velocity"] is None
                            or rv < kinematics_summary["min_rsi_velocity"]["value"]
                        ):
                            kinematics_summary["min_rsi_velocity"] = {
                                "symbol": sym,
                                "value": rv,
                            }

                    pv_pct = rec.get("price_velocity_pct")
                    if pv_pct is not None:
                        if (
                            kinematics_summary["max_price_velocity_pct"] is None
                            or pv_pct
                            > kinematics_summary["max_price_velocity_pct"]["value"]
                        ):
                            kinematics_summary["max_price_velocity_pct"] = {
                                "symbol": sym,
                                "value": pv_pct,
                            }
                        if (
                            kinematics_summary["min_price_velocity_pct"] is None
                            or pv_pct
                            < kinematics_summary["min_price_velocity_pct"]["value"]
                        ):
                            kinematics_summary["min_price_velocity_pct"] = {
                                "symbol": sym,
                                "value": pv_pct,
                            }

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
                    "symbols": symbols_data,
                    "kinematics_summary": kinematics_summary,
                }
                self.emit(record)
                self.late_counts[provider] = 0
                emitted += 1
            self.last_finalized = minute
            self.next_minute = next_session_minute(minute + MINUTE)
            # Old dates cannot influence new-session RSI or grow memory forever.
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
    def __init__(self, provider, symbols, credentials, coordinator):
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
            # Seed RSI state with prior sessions before any live minute is
            # finalized, so the first summary of the run reports converged
            # Wilder averages instead of a since-open cold start.
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
                "https://api.twelvedata.com/time_series",
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
        """Return the last RSI_WARMUP_SESSIONS completed NYSE sessions.

        Each entry is (date, (open_utc, close_utc)), ordered oldest first.
        Today's session is never included: its minutes are covered by the live
        streams and the existing same-session recovery path.
        """
        if RSI_WARMUP_SESSIONS <= 0:
            return []
        sessions = []
        day = datetime.now(MARKET_TZ).date() - timedelta(days=1)
        # A generous scan window handles holidays and weekends.
        for _ in range(RSI_WARMUP_SESSIONS * 5 + 10):
            if len(sessions) >= RSI_WARMUP_SESSIONS:
                break
            bounds = session_bounds(day)
            if bounds is not None:
                sessions.append((day, bounds))
            day -= timedelta(days=1)
        return list(reversed(sessions))

    async def warmup(self):
        """Seed each symbol's RSI state with prior sessions' 1-minute closes.

        Best effort: a provider that cannot fetch history still runs with a
        cold session reset, exactly as before this warmup existed.
        """
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
                        type(exc).__name__,
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
        """Serialized recovery worker; streaming events bypass this queue."""
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
                    type(exc).__name__,
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
        # Corrections received before the common deadline replace the candidate.
        subscribe_updates = getattr(self.stream, "subscribe_updated_bars", None)
        if subscribe_updates is not None:
            subscribe_updates(callback, *self.symbols)
        await bootstrap()

    async def run_yfinance(self):
        for symbol in self.symbols:
            self.enqueue(symbol, "recover", datetime.now(UTC))
        self.logger.info("POLLING_STARTED symbols=%s", self.symbols)
        while True:
            # A full cycle takes request time plus the configured polling pause.
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
                        type(exc).__name__,
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
                    url = "wss://ws.twelvedata.com/v1/quotes/price"
                    params = {"apikey": self.credentials["twelvedata_key"]}
                    subscription = {
                        "action": "subscribe",
                        "params": {"symbols": ",".join(self.symbols)},
                    }
                else:
                    payload = await request_json(
                        self.session,
                        "POST",
                        "https://api.tradier.com/v1/markets/events/session",
                        headers=self.tradier_headers(),
                    )
                    stream = payload.get("stream", {})
                    session_id = stream.get("sessionid")

                    if not session_id:
                        raise FatalProviderError(
                            "Tradier returned no streaming session ID"
                        )

                    url = "wss://ws.tradier.com/v1/markets/events"
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
            "Tradier token contains surrounding quotes. " "Remove literal quotes."
        )

    if not token or any(character.isspace() for character in token):
        raise ValueError("Tradier token is empty or contains whitespace")

    logging.info(
        "Tradier credential loaded source=%s token_present=true",
        source,
    )

    return token


def parse_provider_enabled(value, provider):
    """Only explicit true enables a provider; quoted true/false are supported."""
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
    """Load enabled providers, symbols and credentials from project configuration.

    selected contains ALL enabled providers, including TRADINGVIEW.
    required maps each provider to its required credential names.
    Missing flags are disabled; invalid flag values are configuration errors.
    """
    global selected, required

    # Reset on every call so repeated loads cannot retain an old selection.
    selected = []
    required = {
        "ALPACA": ["alpaca_key", "alpaca_secret"],
        "TRADIER": ["tradier_token"],
        "TWELVEDATA": ["twelvedata_key"],
        "YAHOOFINANCE": [],
        "TRADINGVIEW": [],
    }

    config_path = Path(__file__).resolve().parent / "lib" / "parms.ini"
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    # Read provider flags with standard INI semantics, independent of how the
    # project's JsonIniParser interprets dotted names or quoted values.
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

    # Keep the existing project-specific parsing for symbols and API credentials.
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
    # TRADINGVIEW and the position monitor both need the Alpaca credentials
    # even without the ALPACA stream, so always load CREDENTIALS when any
    # provider is enabled.
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
                f"Missing credentials for enabled provider {provider}: {', '.join(missing)}"
            )

    logging.info("PROVIDERS_SELECTED providers=%s symbols=%s", selected, symbols)
    return symbols, keys


WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "127.0.0.1")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
WEBHOOK_QUEUE_LIMIT = 2000
WEBHOOK_MAX_BYTES = 16384
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
# Option-chain collection trigger: a TradingView webhook RSI at or below the
# low threshold is the BUY signal (oversold); at or above the high threshold
# is the SELL signal (overbought). Either signal fetches the pruned option
# chain and writes it to logs/optionchains/.
OPTION_CHAIN_RSI_LOW = float(os.getenv("OPTION_CHAIN_RSI_LOW", "35"))
OPTION_CHAIN_RSI_HIGH = float(os.getenv("OPTION_CHAIN_RSI_HIGH", "65"))


def rsi_signal(rsi):
    """Classify a TradingView-reported RSI value as a buy or sell signal.

    Returns "buy" when rsi <= OPTION_CHAIN_RSI_LOW (oversold), "sell" when
    rsi >= OPTION_CHAIN_RSI_HIGH (overbought), and None inside the neutral
    band or when the RSI is unavailable.
    """
    if rsi is None:
        return None
    if rsi <= OPTION_CHAIN_RSI_LOW:
        return "buy"
    if rsi >= OPTION_CHAIN_RSI_HIGH:
        return "sell"
    return None


def validate_tradingview_payload(payload, allowed_symbols):
    from datetime import datetime, UTC
    import math

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

    raw_rsi = payload.get("rsi_value") if "rsi_value" in payload else payload.get("rsi")
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
        # FIX: Keep TradingView's original baseline timestamp (e.g., 18:16:00)
        # Your coordinator handles historical bucket alignment automatically.
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
    """Create the Alpaca client handed to get_pruned_option_chain.

    The option-chain call receives the key/secret alongside this client. If
    your lib.getOptionsContracts implementation expects a different client
    type, adjust only this factory ? the rest of the plumbing is unchanged.
    Returns None when credentials are missing; the trigger then skips with a
    logged warning instead of failing the webhook loop.
    """
    if not keys.get("alpaca_key") or not keys.get("alpaca_secret"):
        logging.warning(
            "OPTION_CHAIN_DISABLED reason=missing_alpaca_credentials "
            "hint=provide api_key/secret_key in parms.ini CREDENTIALS or "
            "ALPACA_API_KEY/ALPACA_SECRET_KEY environment variables"
        )
        return None
    from alpaca.trading.client import TradingClient

    return TradingClient(keys["alpaca_key"], keys["alpaca_secret"])


class TradingviewProvider:
    def __init__(
        self,
        symbols,
        coordinator,
        host=WEBHOOK_HOST,
        port=WEBHOOK_PORT,
        credentials=None,
        option_client=None,
    ):
        import queue
        import threading

        if not WEBHOOK_TOKEN and host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Set WEBHOOK_TOKEN before binding a non-loopback host")
        self.provider = "TRADINGVIEW"
        self.symbols = set(symbols)
        self.coordinator = coordinator
        self.host, self.port = host, port
        self.credentials = dict(credentials or {})
        self.option_client = option_client
        self.option_chain_lock = asyncio.Lock()
        self.queue = queue.Queue(maxsize=WEBHOOK_QUEUE_LIMIT)
        self.stop = threading.Event()
        self.server = None
        self.thread = None
        self.thread_error = None
        self.logger = make_logger(self.provider, "_STREAM")
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

        @app.get("/")
        def index():
            host_ip = request.host.split(":")[0]
            dashboard_url = f"http://{host_ip}:5001/"
            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TradingView Webhook Gateway</title>
    <meta http-equiv="refresh" content="2; url={dashboard_url}">
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex items-center justify-center p-6 antialiased">
    <div class="max-w-md w-full bg-slate-900/90 border border-slate-800 rounded-2xl p-8 text-center shadow-2xl backdrop-blur">
        <div class="w-14 h-14 bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 rounded-2xl flex items-center justify-center mx-auto mb-4 text-2xl font-black">
            K
        </div>
        <h1 class="text-xl font-bold mb-2">TradingView Webhook Gateway</h1>
        <p class="text-xs text-slate-400 leading-relaxed mb-6">
            Webhook ingestion active on port {self.port} (<code class="text-indigo-300">/webhook</code>).<br>
            Redirecting to the <strong>RSI & Price Kinematics Dashboard</strong> on port 5001...
        </p>
        <a href="{dashboard_url}" class="inline-flex items-center gap-2 px-5 py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white font-medium rounded-xl text-sm transition shadow-lg shadow-indigo-600/30">
            Open Kinematics Dashboard &rarr;
        </a>
    </div>
</body>
</html>"""
            return html, 200, {"Content-Type": "text/html"}

        @app.get("/health")
        def health():
            return jsonify(status="ok", provider=self.provider), 200

        @app.post("/webhook")
        def receive():
            import traceback
            import hmac

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
                # Execute your payload validation logic
                event = validate_tradingview_payload(
                    payload,
                    self.symbols,
                )

                # SAFEGUARD: Verify the exact type and key of the returned object
                if not isinstance(event, dict) or "symbol" not in event:
                    print(
                        f"\n[CRITICAL ERROR]: validate_tradingview_payload returned an invalid structure."
                    )
                    print(f"Expected dict with 'symbol' key, but got: {event}\n")
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
                # This block captures exactly what line crashed inside your processing pipeline
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
            self.thread_error = type(exc).__name__
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
            # Use event-loop acceptance time for the strict deadline. The original
            # HTTP receipt time remains in the diagnostic event, not the snapshot.
            reported_rsi=event["rsi_value"],
        )
        self.latest[event["symbol"]] = {**event, "accepted_for_summary": accepted}
        self.logger.debug(
            "WEBHOOK_ACCEPTED symbol=%s accepted=%s", event["symbol"], accepted
        )

    def schedule_option_chain(self, event):
        """Queue option-chain collection on a BUY or SELL RSI signal.

        rsi <= OPTION_CHAIN_RSI_LOW is the BUY signal (oversold);
        rsi >= OPTION_CHAIN_RSI_HIGH is the SELL signal (overbought).
        Called on the event loop right after record(); collection itself runs
        as a background task so a slow Alpaca call never blocks the webhook
        drain loop. Only one collection runs at a time per provider.
        """
        rsi = event.get("rsi_value")
        if rsi is None:
            return
        signal = rsi_signal(rsi)
        if signal is None:
            return
        symbol = event["symbol"]
        self.logger.info(
            "RSI_SIGNAL provider=%s symbol=%s rsi=%.4f signal=%s",
            self.provider,
            symbol,
            rsi,
            signal.upper(),
        )
        # Also surface the signal on the console/root multiStreamRSI log.
        logging.info(
            "RSI_SIGNAL provider=%s symbol=%s rsi=%.4f signal=%s",
            self.provider,
            symbol,
            rsi,
            signal.upper(),
        )
        if not self.credentials.get("alpaca_key") or not self.credentials.get(
            "alpaca_secret"
        ):
            self.logger.warning(
                "OPTION_CHAIN_SKIPPED signal=%s symbol=%s rsi=%.4f reason=missing_alpaca_credentials",
                signal.upper(),
                symbol,
                rsi,
            )
            return
        if self.option_client is None:
            self.logger.warning(
                "OPTION_CHAIN_SKIPPED signal=%s symbol=%s rsi=%.4f reason=no_option_client",
                signal.upper(),
                symbol,
                rsi,
            )
            return
        if self.option_chain_lock.locked():
            self.logger.info(
                "OPTION_CHAIN_SKIPPED signal=%s symbol=%s rsi=%.4f reason=collection_in_progress",
                signal.upper(),
                symbol,
                rsi,
            )
            return
        asyncio.create_task(self.collect_option_chain(event, signal))

    async def collect_option_chain(self, event, signal):
        """Fetch the pruned option chain and persist the full clean payload."""
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
                    "OPTION_CHAIN_FAILED signal=%s symbol=%s rsi=%.4f error_type=%s detail=%s",
                    label,
                    symbol,
                    rsi,
                    type(exc).__name__,
                    exc,
                )
                return
            try:
                path = self.write_option_chain_payload(
                    symbol, rsi, signal, event, clean_payload
                )
            except Exception as exc:
                self.logger.error(
                    "OPTION_CHAIN_WRITE_FAILED signal=%s symbol=%s rsi=%.4f error_type=%s detail=%s",
                    label,
                    symbol,
                    rsi,
                    type(exc).__name__,
                    exc,
                )
                return
            self.logger.info(
                "OPTION_CHAIN_CONTRACT provider=%s signal=%s symbol=%s rsi=%.4f contract=%s file=%s",
                self.provider,
                label,
                symbol,
                rsi,
                contract,
                path.name,
            )

    def write_option_chain_payload(self, symbol, rsi, signal, event, clean_payload):
        """Write the entire clean payload to a uniquely named JSON file."""
        directory = LOG_ROOT / "optionchains"
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in symbol)
        try:
            bar_time = datetime.fromisoformat(event["event_time"])
            stamp = bar_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        except (KeyError, TypeError, ValueError):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        # The short random suffix guarantees uniqueness even if two alerts
        # fire for the same symbol, RSI and minute.
        filename = (
            f"optionchain-{self.provider}-{safe}-{signal.upper()}-rsi{rsi:.2f}-"
            f"{stamp}-{uuid4().hex[:6]}.json"
        )
        path = directory / filename
        path.write_text(
            json.dumps(clean_payload, indent=2, default=str), encoding="utf-8"
        )
        return path

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
        self.coordinator.set_connected(self.provider, True)

        logging.info(
            "TRADINGVIEW listening host=%s port=%d " "route=/webhook authentication=%s",
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
                        # Out-of-band RSI values trigger option-chain
                        # collection as a background task; the shutdown drain
                        # below intentionally calls only record().
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
    """Poll Alpaca account positions every minute and log option holdings."""

    def __init__(self, keys):
        self.provider = "POSITIONS"
        self.credentials = dict(keys or {})
        self.logger = make_logger("POSITIONS", "ALPACA")
        self.client = build_option_chain_client(self.credentials)
        # --- NEW: Status Tracking State Variables ---
        self.last_poll_utc = None
        self.last_status = "uninitialized"
        self.position_count = 0
        self.option_count = 0

    def poll(self):
        """Fetch account + positions and log the snapshot (blocking)."""
        # --- NEW: Set active state flag ---
        self.last_status = "polling"
        account = self.client.get_account()
        positions = list(self.client.get_all_positions() or [])
        options = [p for p in positions if self._is_option(p)]

        self.position_count = len(positions)
        self.option_count = len(options)
        self.last_poll_utc = datetime.now(timezone.utc).isoformat()
        self.last_status = "healthy"

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
            self.last_status = "disabled"
            self.logger.warning(
                "POSITION_MONITOR_DISABLED reason=missing_alpaca_credentials "
                "hint=provide api_key/secret_key in parms.ini CREDENTIALS or "
                "ALPACA_API_KEY/ALPACA_SECRET_KEY environment variables"
            )
            return
        self.logger.info("POSITION_MONITOR_START cadence=every-minute")
        logging.info(
            "POSITION_MONITOR_START provider=POSITIONS log=%s",
            self.logger.handlers[0].baseFilename if self.logger.handlers else "n/a",
        )
        while True:
            try:
                await asyncio.to_thread(self.poll)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_status = f"error: {type(exc).__name__}"
                self.logger.warning(
                    "POSITION_MONITOR_ERROR error_type=%s detail=%s",
                    type(exc).__name__,
                    exc,
                )
            # Sleep until just after the next minute boundary.
            now = datetime.now(UTC)
            target = (
                now.replace(second=0, microsecond=0) + MINUTE + timedelta(seconds=2)
            )
            await asyncio.sleep(max(1.0, (target - now).total_seconds()))


# Create a dictionary to keep track of active provider instances and tasks across the engine
PROVIDER_REGISTRY = {}


DASHBOARD_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RSI & Price Kinematics Engine</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; }
        code, pre, .mono { font-family: 'JetBrains Mono', monospace; }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: rgba(15, 23, 42, 0.6); }
        ::-webkit-scrollbar-thumb { background: rgba(51, 65, 85, 0.8); rounded: 9999px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(99, 102, 241, 0.8); }
    </style>
</head>
<body class="bg-[#080d19] text-slate-100 min-h-screen flex flex-col antialiased selection:bg-indigo-500 selection:text-white">

    <!-- Top Navigation Bar -->
    <header class="border-b border-slate-800/80 bg-slate-900/70 backdrop-blur sticky top-0 z-40">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3 flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-3">
                <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 to-cyan-500 flex items-center justify-center font-black text-white text-lg shadow-md shadow-indigo-500/20">
                    K
                </div>
                <div>
                    <div class="flex items-center gap-2">
                        <h1 class="text-sm sm:text-base font-bold tracking-tight text-white">RSI & Price Kinematics Engine</h1>
                        <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping"></span>
                            ACTIVE
                        </span>
                    </div>
                    <p class="text-[11px] text-slate-400">Multi-Stream Aggregation &bull; 1m Velocities &bull; Accelerations &bull; Dynamic Phase Regimes</p>
                </div>
            </div>

            <!-- Engine Controls -->
            <div class="flex flex-wrap items-center gap-2">
                <div class="hidden lg:flex items-center gap-1.5 px-2.5 py-1 bg-slate-800/70 border border-slate-700/60 rounded-lg text-xs">
                    <span class="text-slate-400">Run:</span>
                    <span id="runIdBadge" class="mono font-semibold text-indigo-300">__RUN_ID__</span>
                </div>

                <div class="flex items-center gap-1.5 px-2.5 py-1 bg-slate-800/70 border border-slate-700/60 rounded-lg text-xs">
                    <span class="text-slate-400">Next Minute:</span>
                    <span id="minuteCountdown" class="mono font-semibold text-amber-300">--s</span>
                </div>

                <button id="btnFlush" onclick="forceFinalize()" class="px-3 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 hover:text-white border border-slate-700 rounded-lg text-xs font-semibold transition flex items-center gap-1">
                    <svg class="w-3.5 h-3.5 text-indigo-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
                    Flush Minute
                </button>

                <button id="btnToggleInject" onclick="toggleInjectModal()" class="px-3 py-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-semibold transition flex items-center gap-1 shadow-md shadow-indigo-600/20">
                    <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" /></svg>
                    Inject Bar
                </button>

                <button id="btnRefresh" onclick="fetchAllData()" class="p-1 bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-slate-200 border border-slate-700 rounded-lg transition" title="Refresh Now">
                    <svg id="refreshIcon" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                </button>
            </div>
        </div>
    </header>

    <!-- Main Content Container -->
    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-5 flex-1 space-y-5">

        <!-- Banner Toast for Actions -->
        <div id="statusToast" class="hidden transition-all duration-300 p-2.5 rounded-xl text-xs font-medium border"></div>

        <!-- KPI Metric Cards Grid -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3.5">
            <!-- Unique Tracked Symbols -->
            <div class="bg-slate-900/50 border border-slate-800/80 rounded-xl p-3.5 flex flex-col justify-between">
                <span class="text-xs font-medium text-slate-400 flex items-center justify-between">
                    Tracked Symbols
                    <span id="activeProvidersCount" class="text-[10px] mono px-1.5 py-0.5 rounded bg-slate-800 text-cyan-300 font-semibold">5 Providers</span>
                </span>
                <div class="mt-1 flex items-baseline gap-2">
                    <span id="uniqueSymbolsCount" class="text-2xl sm:text-3xl font-extrabold text-white mono">0</span>
                    <span class="text-xs text-slate-400">unique symbols</span>
                </div>
                <div class="mt-1.5 flex items-center justify-between text-[11px]">
                    <span class="text-slate-400"><span id="streamFeedsCount" class="text-indigo-300 font-bold mono">0</span> streams</span>
                    <div class="flex items-center gap-1.5 text-[10px]">
                        <span id="bullCount" class="text-emerald-400 font-bold">0 Bull</span> &bull;
                        <span id="bearCount" class="text-rose-400 font-bold">0 Bear</span> &bull;
                        <span id="neutralCount" class="text-slate-400 font-bold">0 Neutral</span>
                    </div>
                </div>
            </div>

            <!-- Peak RSI Velocity -->
            <div class="bg-slate-900/50 border border-slate-800/80 rounded-xl p-3.5 flex flex-col justify-between">
                <span class="text-xs font-medium text-slate-400">Peak RSI Velocity (&Delta;RSI)</span>
                <div class="mt-1 flex items-baseline gap-2">
                    <span id="peakRsiVel" class="text-2xl sm:text-3xl font-extrabold text-emerald-400 mono">--</span>
                    <span class="text-xs text-slate-400">/min</span>
                </div>
                <div class="mt-1.5 text-[11px] text-slate-400 truncate flex items-center justify-between">
                    <span>Symbol: <span id="peakRsiVelSymbol" class="font-semibold text-slate-200 mono">None</span></span>
                    <span id="peakRsiVelRegime" class="text-[10px] px-1.5 py-0.2 rounded bg-emerald-950/60 text-emerald-300 border border-emerald-800/50">Bull</span>
                </div>
            </div>

            <!-- Peak Price Velocity -->
            <div class="bg-slate-900/50 border border-slate-800/80 rounded-xl p-3.5 flex flex-col justify-between">
                <span class="text-xs font-medium text-slate-400">Peak Price Velocity (&Delta;P/P%)</span>
                <div class="mt-1 flex items-baseline gap-2">
                    <span id="peakPriceVel" class="text-2xl sm:text-3xl font-extrabold text-indigo-400 mono">--</span>
                    <span class="text-xs text-slate-400">%/min</span>
                </div>
                <div class="mt-1.5 text-[11px] text-slate-400 truncate flex items-center justify-between">
                    <span>Symbol: <span id="peakPriceVelSymbol" class="font-semibold text-slate-200 mono">None</span></span>
                    <span id="peakPriceVelRegime" class="text-[10px] px-1.5 py-0.2 rounded bg-indigo-950/60 text-indigo-300 border border-indigo-800/50">Active</span>
                </div>
            </div>

            <!-- Peak Acceleration -->
            <div class="bg-slate-900/50 border border-slate-800/80 rounded-xl p-3.5 flex flex-col justify-between">
                <span class="text-xs font-medium text-slate-400">Max Acceleration (a)</span>
                <div class="mt-1 flex items-baseline gap-2">
                    <span id="peakRsiAccel" class="text-2xl sm:text-3xl font-extrabold text-cyan-400 mono">--</span>
                    <span class="text-xs text-slate-400">/min²</span>
                </div>
                <div class="mt-1.5 text-[11px] text-slate-400 truncate flex items-center justify-between">
                    <span>Queue: <span id="pendingQueueSize" class="font-semibold text-slate-200 mono">0</span></span>
                    <span>Late: <span id="lateRecordsCount" class="font-semibold text-slate-200 mono">0</span></span>
                </div>
            </div>
        </div>

        <!-- 4-Quadrant Kinematics Matrix Grid -->
        <div class="bg-slate-900/40 border border-slate-800/80 rounded-2xl p-4">
            <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
                <div>
                    <h2 class="text-xs font-bold text-white uppercase tracking-wider flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-indigo-400"></span>
                        Kinematics 4-Quadrant Phase Matrix
                    </h2>
                    <p class="text-[11px] text-slate-400">Velocity (v) vs Acceleration (a) regime classification. Click any symbol to inspect below.</p>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                <!-- Quadrant II: Decelerating Bullish (Top Left) -->
                <div class="bg-cyan-950/20 border border-cyan-800/40 rounded-xl p-3">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-cyan-300 flex items-center gap-1.5">
                            <span>??</span> Decelerating Bullish
                        </span>
                        <span class="text-[10px] mono px-2 py-0.5 rounded bg-cyan-900/40 text-cyan-200">v &gt; 0, a &le; 0</span>
                    </div>
                    <p class="text-[10px] text-cyan-400/80 mt-0.5">Bullish exhaustion &bull; upward velocity slowing &bull; consolidation / potential top</p>
                    <div id="quadDecBullish" class="mt-2.5 flex flex-wrap gap-1.5 min-h-[28px] items-center">
                        <span class="text-xs text-slate-500 italic">No instruments in this phase</span>
                    </div>
                </div>

                <!-- Quadrant I: Accelerating Bullish (Top Right) -->
                <div class="bg-emerald-950/20 border border-emerald-800/40 rounded-xl p-3">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-emerald-300 flex items-center gap-1.5">
                            <span>??</span> Accelerating Bullish
                        </span>
                        <span class="text-[10px] mono px-2 py-0.5 rounded bg-emerald-900/40 text-emerald-200">v &gt; 0, a &gt; 0</span>
                    </div>
                    <p class="text-[10px] text-emerald-400/80 mt-0.5">Aggressive momentum expansion &bull; buyers dominant &bull; trend thrust</p>
                    <div id="quadAccBullish" class="mt-2.5 flex flex-wrap gap-1.5 min-h-[28px] items-center">
                        <span class="text-xs text-slate-500 italic">No instruments in this phase</span>
                    </div>
                </div>

                <!-- Quadrant III: Accelerating Bearish (Bottom Left) -->
                <div class="bg-rose-950/20 border border-rose-800/40 rounded-xl p-3">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-rose-300 flex items-center gap-1.5">
                            <span>??</span> Accelerating Bearish
                        </span>
                        <span class="text-[10px] mono px-2 py-0.5 rounded bg-rose-900/40 text-rose-200">v &lt; 0, a &lt; 0</span>
                    </div>
                    <p class="text-[10px] text-rose-400/80 mt-0.5">Aggressive selling impulse &bull; downward velocity speeding up &bull; breakdown</p>
                    <div id="quadAccBearish" class="mt-2.5 flex flex-wrap gap-1.5 min-h-[28px] items-center">
                        <span class="text-xs text-slate-500 italic">No instruments in this phase</span>
                    </div>
                </div>

                <!-- Quadrant IV: Decelerating Bearish (Bottom Right) -->
                <div class="bg-amber-950/20 border border-amber-800/40 rounded-xl p-3">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-amber-300 flex items-center gap-1.5">
                            <span>??</span> Decelerating Bearish
                        </span>
                        <span class="text-[10px] mono px-2 py-0.5 rounded bg-amber-900/40 text-amber-200">v &lt; 0, a &ge; 0</span>
                    </div>
                    <p class="text-[10px] text-amber-400/80 mt-0.5">Downside absorption &bull; selling losing speed &bull; basing / potential reversal</p>
                    <div id="quadDecBearish" class="mt-2.5 flex flex-wrap gap-1.5 min-h-[28px] items-center">
                        <span class="text-xs text-slate-500 italic">No instruments in this phase</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Table Toolbar: Search, Mode Switcher & Regime Filters -->
        <div class="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3">
            <div class="flex flex-wrap items-center gap-2 flex-1">
                <div class="relative w-full sm:w-64">
                    <input id="symbolSearch" oninput="onSearchInput()" type="text" placeholder="Filter symbol (e.g. SNDK, NVDA, SPY)..." class="w-full bg-slate-900/80 border border-slate-800 rounded-xl px-3.5 py-1.5 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition">
                    <button onclick="clearSearch()" id="btnClearSearch" class="hidden absolute right-2.5 top-1.5 text-slate-400 hover:text-white text-xs">&times;</button>
                </div>

                <!-- View Switcher: Consolidated Symbols vs All Streams -->
                <div class="inline-flex rounded-lg bg-slate-900 p-0.5 border border-slate-800 text-xs font-medium">
                    <button id="btnViewConsolidated" onclick="setViewMode('consolidated')" class="px-2.5 py-1 rounded-md text-white bg-indigo-600 transition shadow-sm font-semibold">
                        Consolidated Symbols (<span id="tabConsolidatedCount">0</span>)
                    </button>
                    <button id="btnViewStreams" onclick="setViewMode('streams')" class="px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition">
                        All Provider Feeds (<span id="tabStreamsCount">0</span>)
                    </button>
                </div>
            </div>

            <!-- Regime Filter Buttons -->
            <div class="flex flex-wrap items-center gap-1 text-xs">
                <button onclick="setFilter('all')" class="filter-btn px-2.5 py-1 rounded-lg border font-medium transition bg-indigo-600 text-white border-indigo-500" data-regime="all">All</button>
                <button onclick="setFilter('accelerating_bullish')" class="filter-btn px-2 py-1 rounded-lg border font-medium transition bg-slate-800 text-slate-300 border-slate-700 hover:text-white" data-regime="accelerating_bullish">Acc Bull</button>
                <button onclick="setFilter('decelerating_bullish')" class="filter-btn px-2 py-1 rounded-lg border font-medium transition bg-slate-800 text-slate-300 border-slate-700 hover:text-white" data-regime="decelerating_bullish">Dec Bull</button>
                <button onclick="setFilter('accelerating_bearish')" class="filter-btn px-2 py-1 rounded-lg border font-medium transition bg-slate-800 text-slate-300 border-slate-700 hover:text-white" data-regime="accelerating_bearish">Acc Bear</button>
                <button onclick="setFilter('decelerating_bearish')" class="filter-btn px-2 py-1 rounded-lg border font-medium transition bg-slate-800 text-slate-300 border-slate-700 hover:text-white" data-regime="decelerating_bearish">Dec Bear</button>
                <button onclick="setFilter('neutral')" class="filter-btn px-2 py-1 rounded-lg border font-medium transition bg-slate-800 text-slate-300 border-slate-700 hover:text-white" data-regime="neutral">Neutral</button>
            </div>
        </div>

        <!-- Main Instruments Kinematics Table (Scrollable container to protect bottom panel) -->
        <div class="bg-slate-900/50 border border-slate-800/80 rounded-2xl overflow-hidden shadow-xl">
            <div class="max-h-[380px] overflow-y-auto">
                <table class="w-full text-left text-xs">
                    <thead class="sticky top-0 z-20 bg-slate-950/90 backdrop-blur border-b border-slate-800 text-slate-400 font-semibold shadow-sm">
                        <tr>
                            <th class="px-4 py-2.5" id="thSymbolCol">Symbol / Feeds</th>
                            <th class="px-4 py-2.5 text-right">Close ($)</th>
                            <th class="px-4 py-2.5 text-right">Price Velocity (v)</th>
                            <th class="px-4 py-2.5 text-right">Price Accel (a)</th>
                            <th class="px-4 py-2.5 min-w-[130px]">RSI (14)</th>
                            <th class="px-4 py-2.5 text-right">RSI Velocity (v_rsi)</th>
                            <th class="px-4 py-2.5 text-right">RSI Accel (a_rsi)</th>
                            <th class="px-4 py-2.5">Kinematics Regime</th>
                            <th class="px-4 py-2.5 text-right">Momentum (3m)</th>
                            <th class="px-4 py-2.5 text-center">Inspect / Test</th>
                        </tr>
                    </thead>
                    <tbody id="kinematicsTableBody" class="divide-y divide-slate-800/60">
                        <tr>
                            <td colspan="10" class="px-4 py-8 text-center text-slate-500">
                                <div class="flex flex-col items-center justify-center gap-2">
                                    <svg class="w-5 h-5 animate-spin text-indigo-400" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
                                    <span>Connecting to engine telemetry stream...</span>
                                </div>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ========================================================================================= -->
        <!-- BOTTOM PANEL: ALWAYS LOADS WITH OR WITHOUT SYMBOL FILTER -->
        <!-- ========================================================================================= -->
        <div id="bottomInspectorSection" class="space-y-4 pt-2">
            
            <!-- Section 1: Symbol Kinematics Deep-Dive & Multi-Stream Comparison -->
            <div class="bg-slate-900/60 border border-slate-800/90 rounded-2xl p-5 shadow-xl">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3.5 border-b border-slate-800/80">
                    <div class="flex items-center gap-2.5">
                        <div class="w-8 h-8 rounded-lg bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 flex items-center justify-center font-black mono text-sm" id="detailSymbolBadge">
                            --
                        </div>
                        <div>
                            <div class="flex items-center gap-2">
                                <h3 class="text-sm font-bold text-white tracking-tight flex items-center gap-2">
                                    <span id="detailSymbolTitle">Symbol Multi-Stream Inspector</span>
                                    <span id="detailRegimeBadge" class="text-[10px] px-2 py-0.5 rounded-full font-semibold border bg-slate-800 text-slate-300 border-slate-700">Neutral</span>
                                </h3>
                            </div>
                            <p class="text-[11px] text-slate-400" id="detailSymbolSubtitle">Select any symbol in table above or dropdown to inspect cross-provider consensus</p>
                        </div>
                    </div>

                    <!-- Symbol Selector Dropdown directly in bottom panel -->
                    <div class="flex items-center gap-2">
                        <span class="text-xs text-slate-400 font-medium">Inspect Symbol:</span>
                        <select id="bottomSymbolSelect" onchange="selectSymbolFromDropdown(this.value)" class="bg-slate-950 border border-slate-800 rounded-lg px-2.5 py-1 text-xs text-white mono focus:outline-none focus:border-indigo-500">
                            <option value="">Loading symbols...</option>
                        </select>
                    </div>
                </div>

                <!-- Deep-Dive Content Container (Always renders content) -->
                <div id="symbolDeepDiveContent" class="mt-4 space-y-4">
                    <!-- Provider Comparison Table for Selected Symbol -->
                    <div class="overflow-x-auto rounded-xl border border-slate-800/80 bg-slate-950/50">
                        <table class="w-full text-left text-xs">
                            <thead class="bg-slate-950/80 text-slate-400 font-semibold border-b border-slate-800">
                                <tr>
                                    <th class="px-3.5 py-2">Provider Feed</th>
                                    <th class="px-3.5 py-2 text-right">Close ($)</th>
                                    <th class="px-3.5 py-2 text-right">Price Velocity (v)</th>
                                    <th class="px-3.5 py-2 text-right">Price Accel (a)</th>
                                    <th class="px-3.5 py-2 text-right">RSI (14)</th>
                                    <th class="px-3.5 py-2 text-right">RSI Velocity (v_rsi)</th>
                                    <th class="px-3.5 py-2 text-right">RSI Accel (a_rsi)</th>
                                    <th class="px-3.5 py-2">Phase Regime</th>
                                    <th class="px-3.5 py-2 text-center">Status</th>
                                </tr>
                            </thead>
                            <tbody id="detailProvidersTableBody" class="divide-y divide-slate-800/50 font-mono text-[11px]">
                                <tr>
                                    <td colspan="9" class="px-3.5 py-4 text-center text-slate-500">
                                        Select a symbol to view provider stream breakdown.
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>

                    <!-- Phase Explanation Banner -->
                    <div id="detailPhaseExplanation" class="p-3 rounded-xl bg-slate-950/70 border border-slate-800/80 text-xs flex items-center justify-between gap-4">
                        <div class="flex items-center gap-2">
                            <span class="text-indigo-400 font-bold">Kinematic Diagnosis:</span>
                            <span id="detailDiagnosisText" class="text-slate-300">Awaiting stream telemetry...</span>
                        </div>
                        <button onclick="injectForSelectedSymbol()" class="px-2.5 py-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded text-[11px] font-semibold transition shrink-0">
                            Test Inject This Symbol
                        </button>
                    </div>
                </div>
            </div>

            <!-- Section 2: Emitted Minute Summaries Log & Position Monitor (2 cols) -->
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <!-- Minute Summaries Feed (2 cols) -->
                <div class="lg:col-span-2 bg-slate-900/50 border border-slate-800/80 rounded-2xl p-4 space-y-3">
                    <div class="flex items-center justify-between">
                        <div>
                            <h3 class="text-xs font-bold text-white uppercase tracking-wider flex items-center gap-2">
                                <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                                Emitted Minute Summaries Log Feed
                            </h3>
                            <p class="text-[11px] text-slate-400">Records from <code class="mono text-indigo-300">minute-summary-{RUN_ID}.jsonl</code></p>
                        </div>
                        <button onclick="fetchMinuteSummaries()" class="text-xs text-indigo-400 hover:text-indigo-300 font-semibold transition">Refresh Logs</button>
                    </div>

                    <div id="summariesContainer" class="space-y-2.5 max-h-[280px] overflow-y-auto pr-1">
                        <div class="p-4 rounded-xl bg-slate-950/50 border border-slate-800/80 text-center text-slate-400 text-xs space-y-1.5">
                            <div class="font-semibold text-slate-300">Awaiting Minute Finalization</div>
                            <p class="text-[11px] text-slate-500">Summaries are emitted at minute boundary + 45s delay. Live ticks are buffered in memory.</p>
                        </div>
                    </div>
                </div>

                <!-- Position Monitor & Engine Heartbeat (1 col) -->
                <div class="bg-slate-900/50 border border-slate-800/80 rounded-2xl p-4 space-y-3">
                    <div>
                        <h3 class="text-xs font-bold text-white uppercase tracking-wider">Engine Diagnostic Telemetry</h3>
                        <p class="text-[11px] text-slate-400">Positions monitor &amp; feed heartbeat</p>
                    </div>

                    <div class="space-y-2 text-xs">
                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Position Monitor</span>
                            <span id="posMonitorStatus" class="mono font-semibold text-slate-200">not_loaded</span>
                        </div>

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Active Equity Positions</span>
                            <span id="posMonitorEquity" class="mono font-semibold text-slate-200">0</span>
                        </div>

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Active Option Positions</span>
                            <span id="posMonitorOption" class="mono font-semibold text-slate-200">0</span>
                        </div>

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Last Telemetry Poll</span>
                            <span id="posMonitorLastPoll" class="mono text-[11px] text-slate-400">--</span>
                        </div>
                    </div>

                    <div class="pt-2 border-t border-slate-800/80">
                        <h4 class="text-[11px] font-semibold text-slate-300 mb-1.5">Connected Feeds</h4>
                        <div id="connectedFeedsList" class="flex flex-wrap gap-1 text-[11px]">
                            <span class="text-slate-500">Checking feeds...</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </main>

    <!-- Bar Injection Modal -->
    <div id="injectModal" class="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-slate-900 border border-slate-800 rounded-2xl max-w-md w-full p-6 shadow-2xl space-y-4">
            <div class="flex items-center justify-between">
                <h3 class="text-base font-bold text-white flex items-center gap-2">
                    <span class="w-2.5 h-2.5 rounded-full bg-indigo-500"></span>
                    Inject Live Test Bar
                </h3>
                <button onclick="toggleInjectModal()" class="text-slate-400 hover:text-white">&times;</button>
            </div>
            <p class="text-xs text-slate-400">Simulate incoming bar or tick event directly through the MinuteCoordinator pipeline.</p>

            <form id="injectForm" onsubmit="handleInjectSubmit(event)" class="space-y-3 text-xs">
                <div>
                    <label class="block text-slate-300 font-medium mb-1">Provider Feed</label>
                    <select id="injectProvider" class="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-white focus:outline-none focus:border-indigo-500">
                        <option value="TRADINGVIEW">TRADINGVIEW</option>
                        <option value="ALPACA">ALPACA</option>
                        <option value="TRADIER">TRADIER</option>
                        <option value="YAHOOFINANCE">YAHOOFINANCE</option>
                        <option value="TWELVEDATA">TWELVEDATA</option>
                    </select>
                </div>

                <div>
                    <label class="block text-slate-300 font-medium mb-1">Symbol</label>
                    <input id="injectSymbol" type="text" required placeholder="e.g. SNDK" class="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-white uppercase font-mono focus:outline-none focus:border-indigo-500">
                </div>

                <div>
                    <label class="block text-slate-300 font-medium mb-1">Close Price ($)</label>
                    <input id="injectPrice" type="number" step="0.0001" required placeholder="185.50" class="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-white font-mono focus:outline-none focus:border-indigo-500">
                </div>

                <div class="flex justify-end gap-2 pt-2">
                    <button type="button" onclick="toggleInjectModal()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg font-medium transition">Cancel</button>
                    <button type="submit" id="btnSubmitInject" class="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg font-semibold transition flex items-center gap-1.5 shadow-md shadow-indigo-600/30">
                        Inject Bar &rarr;
                    </button>
                </div>
            </form>
        </div>
    </div>

    <!-- JavaScript Application Engine -->
    <script>
        let currentFilter = 'all';
        let currentViewMode = 'consolidated'; // 'consolidated' or 'streams'
        let selectedSymbol = null;
        let latestData = null;
        let nextMinuteUtc = null;
        let countdownTimer = null;
        let expandedSymbols = new Set();

        const REGIME_CONFIG = {
            accelerating_bullish: {
                label: 'Accelerating Bullish',
                badgeClass: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
                icon: '??',
                pillColor: 'text-emerald-400',
                desc: 'Strong buyer thrust & momentum expansion'
            },
            decelerating_bullish: {
                label: 'Decelerating Bullish',
                badgeClass: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/30',
                icon: '??',
                pillColor: 'text-cyan-400',
                desc: 'Upward momentum waning / consolidation'
            },
            accelerating_bearish: {
                label: 'Accelerating Bearish',
                badgeClass: 'bg-rose-500/10 text-rose-400 border-rose-500/30',
                icon: '??',
                pillColor: 'text-rose-400',
                desc: 'Aggressive selling impulse accelerating'
            },
            decelerating_bearish: {
                label: 'Decelerating Bearish',
                badgeClass: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
                icon: '??',
                pillColor: 'text-amber-400',
                desc: 'Downside absorption / selling deceleration'
            },
            neutral: {
                label: 'Neutral',
                badgeClass: 'bg-slate-700/30 text-slate-300 border-slate-700',
                icon: '?',
                pillColor: 'text-slate-400',
                desc: 'Equilibrium / no directional acceleration'
            }
        };

        // Safe format helpers that NEVER throw on null/undefined
        function safeNum(v) {
            if (v === null || v === undefined) return null;
            const n = Number(v);
            return isFinite(n) ? n : null;
        }

        function fmtPrice(v) {
            const n = safeNum(v);
            return n !== null ? '$' + n.toFixed(2) : '--';
        }

        function fmtNum(v, dec = 2, prefix = '') {
            const n = safeNum(v);
            if (n === null) return '--';
            const s = n >= 0 && prefix === '+' ? '+' : '';
            return s + n.toFixed(dec);
        }

        function fmtPct(v, dec = 2, prefix = '') {
            const n = safeNum(v);
            if (n === null) return '--';
            const s = n >= 0 && prefix === '+' ? '+' : '';
            return s + n.toFixed(dec) + '%';
        }

        function getRegime(inst) {
            if (inst.regime && inst.regime !== 'neutral') return inst.regime;
            const rv = safeNum(inst.rsi_velocity);
            const ra = safeNum(inst.rsi_acceleration);
            if (rv !== null) {
                if (rv > 0) return (ra !== null && ra > 0) ? 'accelerating_bullish' : 'decelerating_bullish';
                if (rv < 0) return (ra !== null && ra < 0) ? 'accelerating_bearish' : 'decelerating_bearish';
            }
            const pv = safeNum(inst.price_velocity);
            const pa = safeNum(inst.price_acceleration);
            if (pv !== null) {
                if (pv > 0) return (pa !== null && pa > 0) ? 'accelerating_bullish' : 'decelerating_bullish';
                if (pv < 0) return (pa !== null && pa < 0) ? 'accelerating_bearish' : 'decelerating_bearish';
            }
            return 'neutral';
        }

        function showToast(message, type = 'success') {
            const toast = document.getElementById('statusToast');
            toast.textContent = message;
            toast.className = `p-2.5 rounded-xl text-xs font-medium border block ${
                type === 'success' 
                    ? 'bg-emerald-950/40 text-emerald-300 border-emerald-800' 
                    : 'bg-rose-950/40 text-rose-300 border-rose-800'
            }`;
            setTimeout(() => { toast.classList.add('hidden'); }, 4000);
        }

        function setViewMode(mode) {
            currentViewMode = mode;
            const btnConsolidated = document.getElementById('btnViewConsolidated');
            const btnStreams = document.getElementById('btnViewStreams');
            if (mode === 'consolidated') {
                btnConsolidated.className = 'px-2.5 py-1 rounded-md text-white bg-indigo-600 transition shadow-sm font-semibold';
                btnStreams.className = 'px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition';
                document.getElementById('thSymbolCol').textContent = 'Symbol / Providers';
            } else {
                btnStreams.className = 'px-2.5 py-1 rounded-md text-white bg-indigo-600 transition shadow-sm font-semibold';
                btnConsolidated.className = 'px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition';
                document.getElementById('thSymbolCol').textContent = 'Symbol / Feed';
            }
            applyFilters();
        }

        function setFilter(regime) {
            currentFilter = regime;
            document.querySelectorAll('.filter-btn').forEach(btn => {
                if (btn.dataset.regime === regime) {
                    btn.className = 'filter-btn px-2 py-1 rounded-lg border font-medium transition bg-indigo-600 text-white border-indigo-500';
                } else {
                    btn.className = 'filter-btn px-2 py-1 rounded-lg border font-medium transition bg-slate-800 text-slate-300 border-slate-700 hover:text-white';
                }
            });
            applyFilters();
        }

        function onSearchInput() {
            const val = (document.getElementById('symbolSearch').value || '').trim().toUpperCase();
            document.getElementById('btnClearSearch').classList.toggle('hidden', !val);
            if (val) {
                selectedSymbol = val;
            }
            applyFilters();
            renderSymbolDeepDive();
        }

        function clearSearch() {
            document.getElementById('symbolSearch').value = '';
            document.getElementById('btnClearSearch').classList.add('hidden');
            applyFilters();
        }

        function toggleInjectModal(prefillSymbol = '') {
            const modal = document.getElementById('injectModal');
            modal.classList.toggle('hidden');
            if (prefillSymbol) {
                document.getElementById('injectSymbol').value = prefillSymbol;
                document.getElementById('injectPrice').focus();
            }
        }

        function injectForSelectedSymbol() {
            if (selectedSymbol) {
                toggleInjectModal(selectedSymbol);
            } else {
                toggleInjectModal();
            }
        }

        function selectSymbolFromDropdown(sym) {
            if (!sym) return;
            selectedSymbol = sym;
            renderSymbolDeepDive();
        }

        function selectAndInspectSymbol(sym) {
            selectedSymbol = sym;
            renderSymbolDeepDive();
            const el = document.getElementById('bottomInspectorSection');
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }

        function toggleExpandSymbol(sym) {
            if (expandedSymbols.has(sym)) {
                expandedSymbols.delete(sym);
            } else {
                expandedSymbols.add(sym);
            }
            applyFilters();
        }

        async function handleInjectSubmit(e) {
            e.preventDefault();
            const provider = document.getElementById('injectProvider').value;
            const symbol = document.getElementById('injectSymbol').value.trim().toUpperCase();
            const price = parseFloat(document.getElementById('injectPrice').value);
            const btn = document.getElementById('btnSubmitInject');

            btn.disabled = true;
            btn.textContent = 'Injecting...';

            try {
                const res = await fetch('/cli/inject', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ provider, symbol, price })
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(`Successfully injected bar for ${symbol} @ $${price.toFixed(2)}`);
                    toggleInjectModal();
                    selectedSymbol = symbol;
                    await fetchAllData();
                } else {
                    showToast(`Injection failed: ${data.message || 'Error'}`, 'error');
                }
            } catch (err) {
                showToast(`Network error: ${err.message}`, 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = 'Inject Bar &rarr;';
            }
        }

        async function forceFinalize() {
            const btn = document.getElementById('btnFlush');
            btn.disabled = true;
            btn.textContent = 'Flushing...';

            try {
                const res = await fetch('/cli/finalize', { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message || 'Flushed due minute summaries successfully.');
                    await fetchAllData();
                    await fetchMinuteSummaries();
                } else {
                    showToast('Failed to flush summaries', 'error');
                }
            } catch (err) {
                showToast(`Error: ${err.message}`, 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = `<svg class="w-3.5 h-3.5 text-indigo-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg> Flush Minute`;
            }
        }

        function updateCountdown() {
            if (!nextMinuteUtc) return;
            const now = new Date();
            const diff = Math.max(0, Math.floor((nextMinuteUtc - now) / 1000));
            const el = document.getElementById('minuteCountdown');
            if (el) {
                el.textContent = `${diff}s`;
                el.className = diff <= 5 ? 'mono font-semibold text-rose-400 animate-pulse' : 'mono font-semibold text-amber-300';
            }
        }

        async function fetchAllData() {
            const refreshIcon = document.getElementById('refreshIcon');
            if (refreshIcon) refreshIcon.classList.add('animate-spin');

            try {
                const [statusRes, kinRes] = await Promise.all([
                    fetch('/cli/status').then(r => r.json()).catch(() => null),
                    fetch('/api/kinematics').then(r => r.json()).catch(() => null)
                ]);

                if (statusRes && statusRes.metrics) {
                    renderStatusMetrics(statusRes.metrics);
                }

                if (kinRes) {
                    latestData = kinRes;
                    renderKinematics(kinRes);
                    renderSymbolDeepDive();
                }
            } catch (err) {
                console.error("Telemetry fetch error:", err);
            } finally {
                if (refreshIcon) refreshIcon.classList.remove('animate-spin');
            }
        }

        function renderStatusMetrics(m) {
            document.getElementById('runIdBadge').textContent = m.run_id || '__RUN_ID__';
            document.getElementById('pendingQueueSize').textContent = m.pending_queue_size ?? 0;

            if (m.next_due_minute_utc) {
                nextMinuteUtc = new Date(m.next_due_minute_utc);
                updateCountdown();
            }

            if (m.position_monitor) {
                document.getElementById('posMonitorStatus').textContent = m.position_monitor.status || 'not_loaded';
                document.getElementById('posMonitorEquity').textContent = m.position_monitor.active_equity_positions ?? 0;
                document.getElementById('posMonitorOption').textContent = m.position_monitor.active_option_positions ?? 0;
                document.getElementById('posMonitorLastPoll').textContent = m.position_monitor.last_poll_utc ? new Date(m.position_monitor.last_poll_utc).toLocaleTimeString() : '--';
            }

            // Connected Feeds
            const feedsContainer = document.getElementById('connectedFeedsList');
            if (feedsContainer && m.connected_providers) {
                feedsContainer.innerHTML = '';
                const providers = Object.entries(m.connected_providers);
                document.getElementById('activeProvidersCount').textContent = `${providers.filter(([_, c]) => c).length} Providers Online`;
                providers.forEach(([prov, connected]) => {
                    const badge = document.createElement('span');
                    badge.className = `px-1.5 py-0.5 rounded text-[10px] font-mono font-medium border ${
                        connected 
                            ? 'bg-emerald-950/40 text-emerald-300 border-emerald-800' 
                            : 'bg-rose-950/40 text-rose-300 border-rose-800'
                    }`;
                    badge.textContent = `${prov}: ${connected ? 'ONLINE' : 'OFFLINE'}`;
                    feedsContainer.appendChild(badge);
                });
            }
        }

        function renderKinematics(data) {
            const consolidated = data.consolidated_symbols || [];
            const streams = data.instruments || [];

            // 1. Correct deduplicated symbol counts
            const uniqueCount = data.total_unique_symbols || consolidated.length;
            const streamsCount = data.total_provider_streams || streams.length;
            document.getElementById('uniqueSymbolsCount').textContent = uniqueCount;
            document.getElementById('streamFeedsCount').textContent = streamsCount;
            document.getElementById('tabConsolidatedCount').textContent = uniqueCount;
            document.getElementById('tabStreamsCount').textContent = streamsCount;

            // 2. Regime counts based on unique symbols
            let bull = 0, bear = 0, neutral = 0;
            consolidated.forEach(item => {
                const r = getRegime(item);
                item.regime = r; // ensure normalized
                if (r === 'accelerating_bullish' || r === 'decelerating_bullish') bull++;
                else if (r === 'accelerating_bearish' || r === 'decelerating_bearish') bear++;
                else neutral++;
            });

            document.getElementById('bullCount').textContent = `${bull} Bull`;
            document.getElementById('bearCount').textContent = `${bear} Bear`;
            document.getElementById('neutralCount').textContent = `${neutral} Neutral`;

            // 3. Peak values across instruments
            let maxRsiVel = null, maxRsiVelSym = null, maxRsiRegime = 'Bull';
            let maxPriceVel = null, maxPriceVelSym = null;
            let maxRsiAccel = null;

            consolidated.forEach(inst => {
                const rv = safeNum(inst.rsi_velocity);
                if (rv !== null && (maxRsiVel === null || Math.abs(rv) > Math.abs(maxRsiVel))) {
                    maxRsiVel = rv;
                    maxRsiVelSym = inst.symbol;
                    maxRsiRegime = rv >= 0 ? 'Bull' : 'Bear';
                }
                const pv = safeNum(inst.price_velocity_pct);
                if (pv !== null && (maxPriceVel === null || Math.abs(pv) > Math.abs(maxPriceVel))) {
                    maxPriceVel = pv;
                    maxPriceVelSym = inst.symbol;
                }
                const ra = safeNum(inst.rsi_acceleration);
                if (ra !== null && (maxRsiAccel === null || Math.abs(ra) > Math.abs(maxRsiAccel))) {
                    maxRsiAccel = ra;
                }
            });

            if (maxRsiVel !== null) {
                document.getElementById('peakRsiVel').textContent = fmtNum(maxRsiVel, 2, '+');
                document.getElementById('peakRsiVelSymbol').textContent = maxRsiVelSym || 'None';
                document.getElementById('peakRsiVelRegime').textContent = maxRsiRegime;
            }
            if (maxPriceVel !== null) {
                document.getElementById('peakPriceVel').textContent = fmtNum(maxPriceVel, 2, '+');
                document.getElementById('peakPriceVelSymbol').textContent = maxPriceVelSym || 'None';
            }
            if (maxRsiAccel !== null) {
                document.getElementById('peakRsiAccel').textContent = fmtNum(maxRsiAccel, 2, '+');
            }

            // 4. Update Dropdown Options in bottom panel
            updateBottomSymbolDropdown(consolidated);

            // 5. Render Quadrant Matrix
            renderQuadrantMatrix(consolidated);

            // 6. Render Table
            applyFilters();
        }

        function updateBottomSymbolDropdown(symbolsList) {
            const sel = document.getElementById('bottomSymbolSelect');
            if (!sel || symbolsList.length === 0) return;
            const current = sel.value;
            sel.innerHTML = symbolsList.map(s => `
                <option value="${s.symbol}" ${s.symbol === (selectedSymbol || current) ? 'selected' : ''}>
                    ${s.symbol} (${s.providers_count} feeds &bull; ${s.regime.replace('_', ' ')})
                </option>
            `).join('');

            if (!selectedSymbol && symbolsList.length > 0) {
                selectedSymbol = symbolsList[0].symbol;
            }
        }

        function renderQuadrantMatrix(instruments) {
            const quads = {
                accelerating_bullish: document.getElementById('quadAccBullish'),
                decelerating_bullish: document.getElementById('quadDecBullish'),
                accelerating_bearish: document.getElementById('quadAccBearish'),
                decelerating_bearish: document.getElementById('quadDecBearish')
            };

            Object.values(quads).forEach(el => { el.innerHTML = ''; });

            const grouped = {
                accelerating_bullish: [],
                decelerating_bullish: [],
                accelerating_bearish: [],
                decelerating_bearish: []
            };

            instruments.forEach(inst => {
                const r = getRegime(inst);
                if (grouped[r]) {
                    grouped[r].push(inst);
                }
            });

            Object.entries(grouped).forEach(([regime, items]) => {
                const container = quads[regime];
                if (!container) return;
                if (items.length === 0) {
                    container.innerHTML = `<span class="text-xs text-slate-500 italic">No instruments in this phase</span>`;
                    return;
                }
                items.forEach(item => {
                    const pill = document.createElement('span');
                    const rv = safeNum(item.rsi_velocity);
                    const vText = rv !== null ? ` ${fmtNum(rv, 1, '+')}/m` : '';
                    pill.className = `cursor-pointer px-2 py-0.5 rounded text-[11px] font-mono font-medium border transition hover:scale-105 ${REGIME_CONFIG[regime].badgeClass}`;
                    pill.textContent = `${item.symbol}${vText}`;
                    pill.title = `${item.symbol}\nPrice: ${fmtPrice(item.close)}\nRSI: ${fmtNum(item.rsi, 1)}\nv_rsi: ${fmtNum(item.rsi_velocity, 2, '+')}\na_rsi: ${fmtNum(item.rsi_acceleration, 2, '+')}\nClick to inspect in detail panel`;
                    pill.onclick = () => selectAndInspectSymbol(item.symbol);
                    container.appendChild(pill);
                });
            });
        }

        function applyFilters() {
            if (!latestData) return;
            const search = (document.getElementById('symbolSearch').value || '').trim().toUpperCase();
            const tbody = document.getElementById('kinematicsTableBody');

            let dataset = currentViewMode === 'consolidated' 
                ? (latestData.consolidated_symbols || []) 
                : (latestData.instruments || []);

            // Normalize regimes
            dataset.forEach(item => { item.regime = getRegime(item); });

            // Apply regime filter
            if (currentFilter !== 'all') {
                dataset = dataset.filter(i => i.regime === currentFilter);
            }

            // Apply symbol search
            if (search) {
                dataset = dataset.filter(i => i.symbol.includes(search));
            }

            if (dataset.length === 0) {
                tbody.innerHTML = `<tr><td colspan="10" class="px-4 py-8 text-center text-slate-500">No instruments match current filter criteria.</td></tr>`;
                return;
            }

            if (currentViewMode === 'consolidated') {
                renderConsolidatedTableRows(tbody, dataset);
            } else {
                renderStreamTableRows(tbody, dataset);
            }
        }

        function renderConsolidatedTableRows(tbody, symbolsList) {
            tbody.innerHTML = symbolsList.map(item => {
                const rConf = REGIME_CONFIG[item.regime] || REGIME_CONFIG.neutral;
                const priceFormatted = fmtPrice(item.close);
                
                // Price velocity
                const pv = safeNum(item.price_velocity);
                const pvPct = safeNum(item.price_velocity_pct);
                let pVelText = '--';
                let pVelColor = 'text-slate-400';
                if (pv !== null) {
                    pVelText = `${fmtNum(pv, 2, '+')} (${fmtPct(pvPct, 2, '+')})`;
                    pVelColor = pv > 0 ? 'text-emerald-400' : (pv < 0 ? 'text-rose-400' : 'text-slate-400');
                }

                // Price Accel
                const pa = safeNum(item.price_acceleration);
                const pAccelText = pa !== null ? fmtNum(pa, 2, '+') : '--';

                // RSI
                const rsi = safeNum(item.rsi);
                const rsiVal = rsi !== null ? rsi.toFixed(1) : '--';
                const rsiBarWidth = rsi !== null ? Math.min(100, Math.max(0, rsi)) : 50;
                let rsiBarColor = 'bg-indigo-500';
                if (rsi !== null) {
                    if (rsi >= 70) rsiBarColor = 'bg-rose-500';
                    else if (rsi <= 30) rsiBarColor = 'bg-emerald-500';
                }

                // RSI velocity & accel
                const rv = safeNum(item.rsi_velocity);
                const rvText = rv !== null ? `${fmtNum(rv, 2, '+')}/m` : '--';
                const rvColor = rv !== null ? (rv > 0 ? 'text-emerald-400' : (rv < 0 ? 'text-rose-400' : 'text-slate-400')) : 'text-slate-400';

                const ra = safeNum(item.rsi_acceleration);
                const raText = ra !== null ? `${fmtNum(ra, 2, '+')}/m²` : '--';
                const raColor = ra !== null ? (ra > 0 ? 'text-cyan-400' : (ra < 0 ? 'text-amber-400' : 'text-slate-400')) : 'text-slate-400';

                const mom = safeNum(item.momentum_3m_pct);
                const momText = mom !== null ? fmtPct(mom, 2, '+') : '--';

                const isExpanded = expandedSymbols.has(item.symbol);
                const isSelected = selectedSymbol === item.symbol;

                let rowHtml = `
                    <tr class="hover:bg-slate-800/40 transition cursor-pointer ${isSelected ? 'bg-indigo-950/20 border-l-2 border-indigo-500' : ''}" onclick="selectAndInspectSymbol('${item.symbol}')">
                        <td class="px-4 py-2.5">
                            <div class="flex items-center gap-2">
                                <span class="font-bold text-white mono text-xs">${item.symbol}</span>
                                <button onclick="event.stopPropagation(); toggleExpandSymbol('${item.symbol}')" class="px-1.5 py-0.5 rounded text-[10px] mono bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-slate-700 transition" title="Toggle provider feed breakdown">
                                    ${item.providers_count} feeds ${isExpanded ? '?' : '?'}
                                </button>
                            </div>
                        </td>
                        <td class="px-4 py-2.5 text-right mono font-semibold text-slate-200">${priceFormatted}</td>
                        <td class="px-4 py-2.5 text-right mono font-semibold ${pVelColor}">${pVelText}</td>
                        <td class="px-4 py-2.5 text-right mono text-slate-300">${pAccelText}</td>
                        <td class="px-4 py-2.5">
                            <div class="flex items-center gap-2">
                                <span class="mono font-bold text-slate-200 w-9 text-right">${rsiVal}</span>
                                <div class="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden min-w-[50px]">
                                    <div class="h-full ${rsiBarColor} transition-all duration-300" style="width: ${rsiBarWidth}%"></div>
                                </div>
                            </div>
                        </td>
                        <td class="px-4 py-2.5 text-right mono font-semibold ${rvColor}">${rvText}</td>
                        <td class="px-4 py-2.5 text-right mono font-semibold ${raColor}">${raText}</td>
                        <td class="px-4 py-2.5">
                            <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-semibold border ${rConf.badgeClass}">
                                <span>${rConf.icon}</span>
                                ${rConf.label}
                            </span>
                        </td>
                        <td class="px-4 py-2.5 text-right mono text-slate-300">${momText}</td>
                        <td class="px-4 py-2.5 text-center">
                            <div class="flex items-center justify-center gap-1">
                                <button onclick="event.stopPropagation(); selectAndInspectSymbol('${item.symbol}')" class="px-2 py-0.5 bg-indigo-950/80 hover:bg-indigo-900 text-indigo-300 rounded border border-indigo-800 text-[10px] mono transition">
                                    Inspect
                                </button>
                                <button onclick="event.stopPropagation(); toggleInjectModal('${item.symbol}')" class="px-1.5 py-0.5 bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded border border-slate-700 text-[10px] mono transition" title="Inject Test Bar">
                                    +
                                </button>
                            </div>
                        </td>
                    </tr>
                `;

                // If expanded, show nested rows for each provider
                if (isExpanded && item.provider_entries) {
                    item.provider_entries.forEach(sub => {
                        const sRConf = REGIME_CONFIG[getRegime(sub)] || REGIME_CONFIG.neutral;
                        const sPv = safeNum(sub.price_velocity);
                        const sRv = safeNum(sub.rsi_velocity);
                        const sRa = safeNum(sub.rsi_acceleration);
                        rowHtml += `
                            <tr class="bg-slate-950/60 text-slate-400 text-[11px] border-l-2 border-cyan-500/50">
                                <td class="px-4 py-1.5 pl-8 mono text-cyan-300">? ${sub.provider}</td>
                                <td class="px-4 py-1.5 text-right mono">${fmtPrice(sub.close)}</td>
                                <td class="px-4 py-1.5 text-right mono ${sPv > 0 ? 'text-emerald-400' : (sPv < 0 ? 'text-rose-400' : '')}">${fmtNum(sPv, 2, '+')}</td>
                                <td class="px-4 py-1.5 text-right mono">${fmtNum(sub.price_acceleration, 2, '+')}</td>
                                <td class="px-4 py-1.5 mono">${fmtNum(sub.rsi, 1)}</td>
                                <td class="px-4 py-1.5 text-right mono ${sRv > 0 ? 'text-emerald-400' : (sRv < 0 ? 'text-rose-400' : '')}">${fmtNum(sRv, 2, '+')}</td>
                                <td class="px-4 py-1.5 text-right mono">${fmtNum(sRa, 2, '+')}</td>
                                <td class="px-4 py-1.5">
                                    <span class="text-[9px] px-1.5 py-0.2 rounded border ${sRConf.badgeClass}">${sRConf.label}</span>
                                </td>
                                <td class="px-4 py-1.5 text-right mono">${fmtPct(sub.momentum_3m_pct, 2, '+')}</td>
                                <td class="px-4 py-1.5 text-center mono text-slate-500">feed</td>
                            </tr>
                        `;
                    });
                }

                return rowHtml;
            }).join('');
        }

        function renderStreamTableRows(tbody, streamList) {
            tbody.innerHTML = streamList.map(inst => {
                const rConf = REGIME_CONFIG[getRegime(inst)] || REGIME_CONFIG.neutral;
                const pv = safeNum(inst.price_velocity);
                const pvPct = safeNum(inst.price_velocity_pct);
                const rv = safeNum(inst.rsi_velocity);
                const ra = safeNum(inst.rsi_acceleration);
                const rsi = safeNum(inst.rsi);
                const isSelected = selectedSymbol === inst.symbol;

                return `
                    <tr class="hover:bg-slate-800/40 transition cursor-pointer ${isSelected ? 'bg-indigo-950/20' : ''}" onclick="selectAndInspectSymbol('${inst.symbol}')">
                        <td class="px-4 py-2.5">
                            <div class="font-bold text-white mono">${inst.symbol}</div>
                            <div class="text-[10px] text-slate-500 uppercase">${inst.provider}</div>
                        </td>
                        <td class="px-4 py-2.5 text-right mono font-semibold text-slate-200">${fmtPrice(inst.close)}</td>
                        <td class="px-4 py-2.5 text-right mono font-semibold ${pv > 0 ? 'text-emerald-400' : (pv < 0 ? 'text-rose-400' : 'text-slate-400')}">
                            ${fmtNum(pv, 2, '+')} (${fmtPct(pvPct, 2, '+')})
                        </td>
                        <td class="px-4 py-2.5 text-right mono text-slate-300">${fmtNum(inst.price_acceleration, 2, '+')}</td>
                        <td class="px-4 py-2.5 mono font-semibold text-slate-200">${fmtNum(rsi, 1)}</td>
                        <td class="px-4 py-2.5 text-right mono font-semibold ${rv > 0 ? 'text-emerald-400' : (rv < 0 ? 'text-rose-400' : 'text-slate-400')}">${fmtNum(rv, 2, '+')}/m</td>
                        <td class="px-4 py-2.5 text-right mono font-semibold ${ra > 0 ? 'text-cyan-400' : (ra < 0 ? 'text-amber-400' : 'text-slate-400')}">${fmtNum(ra, 2, '+')}/m²</td>
                        <td class="px-4 py-2.5">
                            <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-semibold border ${rConf.badgeClass}">
                                <span>${rConf.icon}</span>
                                ${rConf.label}
                            </span>
                        </td>
                        <td class="px-4 py-2.5 text-right mono text-slate-300">${fmtPct(inst.momentum_3m_pct, 2, '+')}</td>
                        <td class="px-4 py-2.5 text-center">
                            <button onclick="event.stopPropagation(); toggleInjectModal('${inst.symbol}')" class="px-2 py-0.5 bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded border border-slate-700 text-[10px] mono transition">
                                Inject
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        // =========================================================================================
        // BOTTOM PORTION: ALWAYS RENDERS WITH OR WITHOUT A SYMBOL
        // =========================================================================================
        function renderSymbolDeepDive() {
            if (!latestData) return;
            const consolidated = latestData.consolidated_symbols || [];
            if (consolidated.length === 0) return;

            // If no symbol selected, default to the first or highest velocity symbol
            if (!selectedSymbol) {
                selectedSymbol = consolidated[0].symbol;
            }

            // Find data for selected symbol
            let symbolData = consolidated.find(s => s.symbol === selectedSymbol);
            if (!symbolData && consolidated.length > 0) {
                // If typed search doesn't match exact symbol, find first partial match
                symbolData = consolidated.find(s => s.symbol.startsWith(selectedSymbol)) || consolidated[0];
            }

            const sym = symbolData.symbol;
            const regime = getRegime(symbolData);
            const rConf = REGIME_CONFIG[regime] || REGIME_CONFIG.neutral;

            // Update Header Elements in Deep-Dive Panel
            document.getElementById('detailSymbolBadge').textContent = sym;
            document.getElementById('detailSymbolTitle').textContent = `${sym} Multi-Stream Kinematics Analysis`;
            document.getElementById('detailSymbolSubtitle').textContent = `Synthesized consensus across ${symbolData.providers_count} reporting feeds &bull; Close ${fmtPrice(symbolData.close)} &bull; RSI ${fmtNum(symbolData.rsi, 1)}`;
            
            const badgeEl = document.getElementById('detailRegimeBadge');
            badgeEl.className = `text-[10px] px-2 py-0.5 rounded-full font-semibold border ${rConf.badgeClass}`;
            badgeEl.innerHTML = `${rConf.icon} ${rConf.label}`;

            // Diagnosis sentence
            const rv = safeNum(symbolData.rsi_velocity);
            const ra = safeNum(symbolData.rsi_acceleration);
            let diag = `${rConf.desc}. `;
            if (rv !== null) {
                diag += `RSI velocity is ${fmtNum(rv, 2, '+')}/m. `;
            }
            if (ra !== null) {
                diag += `Acceleration is ${fmtNum(ra, 2, '+')}/m² (${ra > 0 ? 'accelerating upward' : 'decelerating / slowing'}).`;
            }
            document.getElementById('detailDiagnosisText').textContent = diag;

            // Render Providers Comparison Table
            const tbody = document.getElementById('detailProvidersTableBody');
            const entries = symbolData.provider_entries || [];

            if (entries.length === 0) {
                tbody.innerHTML = `<tr><td colspan="9" class="px-3.5 py-4 text-center text-slate-500">No active streams for ${sym}</td></tr>`;
                return;
            }

            tbody.innerHTML = entries.map(e => {
                const pRConf = REGIME_CONFIG[getRegime(e)] || REGIME_CONFIG.neutral;
                const pClose = safeNum(e.close);
                const pRsi = safeNum(e.rsi);
                const pRv = safeNum(e.rsi_velocity);
                const pRa = safeNum(e.rsi_acceleration);

                // Calculate drift vs consensus
                let priceDrift = '--';
                if (pClose !== null && symbolData.close !== null) {
                    const diff = pClose - symbolData.close;
                    priceDrift = `${diff >= 0 ? '+' : ''}$${diff.toFixed(2)}`;
                }

                return `
                    <tr class="hover:bg-slate-900/60 transition">
                        <td class="px-3.5 py-2 font-bold text-white">${e.provider}</td>
                        <td class="px-3.5 py-2 text-right text-slate-200">${fmtPrice(e.close)}</td>
                        <td class="px-3.5 py-2 text-right ${pRv > 0 ? 'text-emerald-400' : (pRv < 0 ? 'text-rose-400' : 'text-slate-400')}">${fmtNum(e.price_velocity, 2, '+')}</td>
                        <td class="px-3.5 py-2 text-right text-slate-300">${fmtNum(e.price_acceleration, 2, '+')}</td>
                        <td class="px-3.5 py-2 text-right text-slate-200">${fmtNum(pRsi, 1)}</td>
                        <td class="px-3.5 py-2 text-right ${pRv > 0 ? 'text-emerald-400' : (pRv < 0 ? 'text-rose-400' : 'text-slate-400')}">${fmtNum(pRv, 2, '+')}</td>
                        <td class="px-3.5 py-2 text-right ${pRa > 0 ? 'text-cyan-400' : (pRa < 0 ? 'text-amber-400' : 'text-slate-400')}">${fmtNum(pRa, 2, '+')}</td>
                        <td class="px-3.5 py-2">
                            <span class="text-[10px] px-2 py-0.5 rounded-full border ${pRConf.badgeClass}">
                                ${pRConf.icon} ${pRConf.label}
                            </span>
                        </td>
                        <td class="px-3.5 py-2 text-center text-emerald-400 font-semibold">LIVE</td>
                    </tr>
                `;
            }).join('');
        }

        async function fetchMinuteSummaries() {
            const container = document.getElementById('summariesContainer');
            try {
                const res = await fetch('/api/minute-summaries');
                const data = await res.json();
                if (!res.ok || !data.summaries || data.summaries.length === 0) {
                    container.innerHTML = `
                        <div class="p-4 rounded-xl bg-slate-950/50 border border-slate-800/80 text-center text-slate-400 text-xs space-y-1.5">
                            <div class="font-semibold text-slate-300">Awaiting Minute Finalization</div>
                            <p class="text-[11px] text-slate-500">
                                Summaries emit to <code class="mono text-indigo-300">minute-summary-${document.getElementById('runIdBadge').textContent}.jsonl</code> upon each NYSE minute boundary + 45s delay.<br>
                                Click <strong>"Flush Minute"</strong> above to force an immediate summary emission.
                            </p>
                        </div>
                    `;
                    return;
                }

                const reversed = [...data.summaries].reverse();
                container.innerHTML = reversed.map((rec, idx) => {
                    const minUtc = rec.minute_start_utc ? new Date(rec.minute_start_utc).toISOString().slice(11, 16) + ' UTC' : '--';
                    const kin = rec.kinematics_summary || {};
                    const regimes = kin.regimes || {};
                    const symbolsCount = rec.symbols ? Object.keys(rec.symbols).length : 0;
                    const maxRsi = kin.max_rsi_velocity;
                    const maxPrice = kin.max_price_velocity_pct;

                    return `
                        <div class="p-2.5 bg-slate-950/60 border border-slate-800/80 rounded-xl space-y-1.5">
                            <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
                                <div class="flex items-center gap-2">
                                    <span class="px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 mono font-bold text-[11px]">${minUtc}</span>
                                    <span class="text-slate-300 font-semibold uppercase text-[11px]">${rec.provider || 'ALL'}</span>
                                    <span class="text-slate-500">&bull;</span>
                                    <span class="text-slate-400 text-[11px]">${symbolsCount} symbols</span>
                                </div>
                                <div class="flex items-center gap-2 text-[11px] mono">
                                    <span class="text-slate-400">Late: <span class="font-bold text-slate-200">${rec.late_records_since_previous_summary ?? 0}</span></span>
                                    <button onclick="copySummaryJson(${idx})" id="copyBtn_${idx}" class="text-indigo-400 hover:text-indigo-300 underline font-sans ml-2">Copy JSON</button>
                                </div>
                            </div>

                            <div class="flex flex-wrap items-center gap-1.5 text-[10px]">
                                <span class="px-1.5 py-0.5 rounded bg-emerald-950/40 text-emerald-300 border border-emerald-900/40">?? Acc Bull: ${regimes.accelerating_bullish || 0}</span>
                                <span class="px-1.5 py-0.5 rounded bg-cyan-950/40 text-cyan-300 border border-cyan-900/40">?? Dec Bull: ${regimes.decelerating_bullish || 0}</span>
                                <span class="px-1.5 py-0.5 rounded bg-rose-950/40 text-rose-300 border border-rose-900/40">?? Acc Bear: ${regimes.accelerating_bearish || 0}</span>
                                <span class="px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-300 border border-amber-900/40">?? Dec Bear: ${regimes.decelerating_bearish || 0}</span>
                            </div>

                            ${maxRsi || maxPrice ? `
                                <div class="text-[10px] mono text-slate-400 flex flex-wrap gap-2.5 pt-1 border-t border-slate-900">
                                    ${maxRsi ? `<span>Max &Delta;RSI: <strong class="text-emerald-400">${maxRsi.symbol} (+${fmtNum(maxRsi.value, 2)})</strong></span>` : ''}
                                    ${maxPrice ? `<span>Max &Delta;P%: <strong class="text-indigo-400">${maxPrice.symbol} (+${fmtNum(maxPrice.value, 2)}%)</strong></span>` : ''}
                                </div>
                            ` : ''}
                            <input type="hidden" id="rawSummary_${idx}" value='${JSON.stringify(rec).replace(/'/g, "&apos;")}'>
                        </div>
                    `;
                }).join('');
            } catch (err) {
                container.innerHTML = `<div class="text-rose-400 text-xs py-4">Error loading minute summaries: ${err.message}</div>`;
            }
        }

        function copySummaryJson(idx) {
            const raw = document.getElementById(`rawSummary_${idx}`).value;
            navigator.clipboard.writeText(raw).then(() => {
                const btn = document.getElementById(`copyBtn_${idx}`);
                btn.textContent = 'Copied!';
                setTimeout(() => { btn.textContent = 'Copy JSON'; }, 2000);
            });
        }

        // Initialize engine telemetry loop
        fetchAllData();
        fetchMinuteSummaries();
        setInterval(fetchAllData, 2500);
        setInterval(fetchMinuteSummaries, 6000);
        countdownTimer = setInterval(updateCountdown, 1000);
    </script>
</body>
</html>
"""


class AdminCliProvider:
    def __init__(self, coordinator, host="127.0.0.1", port=5001):
        import threading

        self.coordinator = coordinator
        self.host = host
        self.port = port
        self.loop = asyncio.get_running_loop()
        self.stop = threading.Event()
        self.server = None
        self.thread = None
        self.app = self.create_app()

    def create_app(self):
        from flask import Flask, request, jsonify

        app = Flask("admin_cli_interface")
        app.config["MAX_CONTENT_LENGTH"] = 16384

        @app.route("/", methods=["GET"])
        @app.route("/dashboard", methods=["GET"])
        @app.route("/kinematics", methods=["GET"])
        def index():
            html = DASHBOARD_HTML_TEMPLATE.replace("__RUN_ID__", RUN_ID)
            return html, 200, {"Content-Type": "text/html; charset=utf-8"}

        @app.get("/api/kinematics")
        def get_kinematics():
            latest = {}
            records_flat = []
            symbols_map = {}

            for (provider, symbol), hist in self.coordinator.kinematics_history.items():
                if hist:
                    cur = hist[-1]
                    regime = cur.get("regime")
                    if not regime or regime == "neutral":
                        regime = classify_kinematic_regime(
                            cur.get("rsi_velocity"),
                            cur.get("rsi_acceleration"),
                            cur.get("price_velocity"),
                            cur.get("price_acceleration"),
                        )

                    entry = {
                        "provider": provider,
                        "symbol": symbol,
                        "close": cur.get("close"),
                        "rsi": cur.get("rsi"),
                        "price_velocity": cur.get("price_velocity"),
                        "price_velocity_pct": cur.get("price_velocity_pct"),
                        "price_acceleration": cur.get("price_acceleration"),
                        "rsi_velocity": cur.get("rsi_velocity"),
                        "rsi_acceleration": cur.get("rsi_acceleration"),
                        "momentum_3m_pct": cur.get("momentum_3m_pct"),
                        "ema_slope": cur.get("ema_slope"),
                        "regime": regime,
                        "timestamp": cur.get("timestamp"),
                    }
                    latest.setdefault(provider, {})[symbol] = entry
                    records_flat.append(entry)

                    # Group by unique ticker for consolidated view
                    sym_group = symbols_map.setdefault(
                        symbol,
                        {
                            "symbol": symbol,
                            "providers": [],
                            "entries": [],
                            "closes": [],
                            "rsis": [],
                            "price_velocities": [],
                            "price_velocity_pcts": [],
                            "price_accelerations": [],
                            "rsi_velocities": [],
                            "rsi_accelerations": [],
                            "momentums": [],
                            "regimes": [],
                        },
                    )
                    sym_group["providers"].append(provider)
                    sym_group["entries"].append(entry)
                    if entry["close"] is not None:
                        sym_group["closes"].append(entry["close"])
                    if entry["rsi"] is not None:
                        sym_group["rsis"].append(entry["rsi"])
                    if entry["price_velocity"] is not None:
                        sym_group["price_velocities"].append(entry["price_velocity"])
                    if entry["price_velocity_pct"] is not None:
                        sym_group["price_velocity_pcts"].append(
                            entry["price_velocity_pct"]
                        )
                    if entry["price_acceleration"] is not None:
                        sym_group["price_accelerations"].append(
                            entry["price_acceleration"]
                        )
                    if entry["rsi_velocity"] is not None:
                        sym_group["rsi_velocities"].append(entry["rsi_velocity"])
                    if entry["rsi_acceleration"] is not None:
                        sym_group["rsi_accelerations"].append(entry["rsi_acceleration"])
                    if entry["momentum_3m_pct"] is not None:
                        sym_group["momentums"].append(entry["momentum_3m_pct"])
                    sym_group["regimes"].append(regime)

            # Build consolidated unique symbols list
            consolidated_symbols = []
            regime_counts = {
                "accelerating_bullish": 0,
                "decelerating_bullish": 0,
                "accelerating_bearish": 0,
                "decelerating_bearish": 0,
                "neutral": 0,
            }

            def median_or_none(vals):
                if not vals:
                    return None
                s = sorted(vals)
                mid = len(s) // 2
                return s[mid] if len(s) % 2 != 0 else (s[mid - 1] + s[mid]) / 2

            for sym, g in sorted(symbols_map.items()):
                c_close = median_or_none(g["closes"])
                c_rsi = median_or_none(g["rsis"])
                c_pv = median_or_none(g["price_velocities"])
                c_pv_pct = median_or_none(g["price_velocity_pcts"])
                c_pa = median_or_none(g["price_accelerations"])
                c_rv = median_or_none(g["rsi_velocities"])
                c_ra = median_or_none(g["rsi_accelerations"])
                c_mom = median_or_none(g["momentums"])

                c_regime = classify_kinematic_regime(c_rv, c_ra, c_pv, c_pa)
                if c_regime in regime_counts:
                    regime_counts[c_regime] += 1

                consolidated_symbols.append(
                    {
                        "symbol": sym,
                        "providers_count": len(g["providers"]),
                        "providers": g["providers"],
                        "close": round(c_close, 2) if c_close is not None else None,
                        "rsi": round(c_rsi, 2) if c_rsi is not None else None,
                        "price_velocity": round(c_pv, 2) if c_pv is not None else None,
                        "price_velocity_pct": (
                            round(c_pv_pct, 4) if c_pv_pct is not None else None
                        ),
                        "price_acceleration": (
                            round(c_pa, 2) if c_pa is not None else None
                        ),
                        "rsi_velocity": round(c_rv, 2) if c_rv is not None else None,
                        "rsi_acceleration": (
                            round(c_ra, 2) if c_ra is not None else None
                        ),
                        "momentum_3m_pct": (
                            round(c_mom, 2) if c_mom is not None else None
                        ),
                        "regime": c_regime,
                        "provider_entries": g["entries"],
                    }
                )

            return (
                jsonify(
                    status="ok",
                    run_id=RUN_ID,
                    total_unique_symbols=len(consolidated_symbols),
                    total_provider_streams=len(records_flat),
                    configured_symbols=list(self.coordinator.symbols),
                    regime_counts=regime_counts,
                    latest_by_provider=latest,
                    consolidated_symbols=consolidated_symbols,
                    instruments=records_flat,
                ),
                200,
            )

        @app.get("/api/minute-summaries")
        def get_minute_summaries():
            path = LOG_ROOT / f"minute-summary-{RUN_ID}.jsonl"
            if not path.exists():
                return jsonify(status="ok", summaries=[]), 200
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
                records = []
                for line in lines[-30:]:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
                return jsonify(status="ok", summaries=records), 200
            except Exception as exc:
                return jsonify(status="error", message=str(exc)), 500

        @app.route("/cli/status", methods=["GET", "POST"])
        def get_status():
            pm_entry = PROVIDER_REGISTRY.get("POSITIONS_MONITOR", {})
            pm = pm_entry.get("router")

            position_monitor_report = {
                "status": pm.last_status if pm else "not_loaded",
                "last_poll_utc": pm.last_poll_utc if pm else None,
                "active_equity_positions": pm.position_count if pm else 0,
                "active_option_positions": pm.option_count if pm else 0,
            }
            latest_kinematics = {}
            for (provider, symbol), hist in self.coordinator.kinematics_history.items():
                if hist:
                    latest = hist[-1]
                    reg = latest.get("regime")
                    if not reg or reg == "neutral":
                        reg = classify_kinematic_regime(
                            latest.get("rsi_velocity"),
                            latest.get("rsi_acceleration"),
                            latest.get("price_velocity"),
                            latest.get("price_acceleration"),
                        )
                    latest_kinematics.setdefault(provider, {})[symbol] = {
                        "close": latest.get("close"),
                        "rsi": latest.get("rsi"),
                        "price_velocity": latest.get("price_velocity"),
                        "price_velocity_pct": latest.get("price_velocity_pct"),
                        "price_acceleration": latest.get("price_acceleration"),
                        "rsi_velocity": latest.get("rsi_velocity"),
                        "rsi_acceleration": latest.get("rsi_acceleration"),
                        "momentum_3m_pct": latest.get("momentum_3m_pct"),
                        "ema_slope": latest.get("ema_slope"),
                        "regime": reg,
                        "timestamp": latest.get("timestamp"),
                    }

            status_report = {
                "run_id": RUN_ID,
                "connected_providers": self.coordinator.connected,
                "tracked_instruments": self.coordinator.symbols,
                "unique_symbols_count": len(self.coordinator.symbols),
                "total_stream_feeds": len(self.coordinator.kinematics_history),
                "next_due_minute_utc": self.coordinator.next_minute.isoformat(),
                "pending_queue_size": len(self.coordinator.pending),
                "loaded_providers": [
                    k for k in PROVIDER_REGISTRY.keys() if k != "POSITIONS_MONITOR"
                ],
                "position_monitor": position_monitor_report,
                "latest_kinematics": latest_kinematics,
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
            symbol = payload.get("symbol")
            price = payload.get("price")
            raw_time = payload.get("timestamp") or datetime.now(UTC).isoformat()

            if not provider or not symbol or price is None:
                return jsonify(status="error", message="Missing parameters"), 400

            try:
                target_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))

                def process_submission():
                    return self.coordinator.submit(
                        provider=provider,
                        symbol=symbol,
                        timestamp=target_time,
                        close=price,
                        source="CLI_MANUAL_INJECT",
                        kind="bar",
                    )

                future = asyncio.run_coroutine_threadsafe(
                    asyncio.to_thread(process_submission), self.loop
                )
                accepted = future.result(timeout=5.0)
                return (
                    jsonify(status="ok", processed=True, summary_accepted=accepted),
                    200,
                )
            except Exception as e:
                return jsonify(status="error", message=str(e)), 400

        # --- NEW: Reconnect endpoint ---
        @app.post("/cli/reconnect")
        def reconnect_provider():
            payload = request.get_json(force=True, silent=True) or {}
            provider = (payload.get("provider") or "").upper()

            if provider not in PROVIDER_REGISTRY:
                return (
                    jsonify(
                        status="error",
                        message=f"Provider '{provider}' is not configured or enabled.",
                    ),
                    400,
                )

            async def trigger_reconnect():
                entry = PROVIDER_REGISTRY[provider]
                old_task = entry["task"]
                router = entry["router"]

                # Cancel running task loop gracefully
                if old_task and not old_task.done():
                    old_task.cancel()
                    try:
                        await old_task
                    except asyncio.CancelledError:
                        pass

                # Reset connection state in case it was stuck on True
                self.coordinator.set_connected(provider, False)

                # Spawn clean instance run loop inside our main event loop
                new_task = asyncio.create_task(
                    run_provider_isolated(router, self.coordinator), name=provider
                )
                entry["task"] = new_task
                return True

            future = asyncio.run_coroutine_threadsafe(trigger_reconnect(), self.loop)
            future.result(
                timeout=10.0
            )  # Blocks thread cleanly until replacement task is mounted

            return (
                jsonify(
                    status="ok",
                    message=f"Reconnection cycle triggered for provider: {provider}",
                ),
                200,
            )

        return app

    def serve(self):
        while not self.stop.is_set():
            self.server.handle_request()
        self.server.server_close()

    async def run(self):
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
            target=self.serve, name="CLI-Admin-Server", daemon=True
        )
        self.thread.start()

        logging.info(
            "CLI Engine interface running on http://%s:%d", self.host, self.port
        )
        try:
            while not self.stop.is_set():
                await asyncio.sleep(1.0)
        finally:
            self.stop.set()
            await asyncio.to_thread(self.thread.join, 2.0)


async def run_provider_isolated(router, coordinator):
    """Keep other providers running after a fatal provider rejection."""
    try:
        await router.run()
    except asyncio.CancelledError:
        logging.info(
            "Provider task run loop canceled for allocation update: %s", router.provider
        )
        raise
    except FatalProviderError as exc:
        coordinator.set_connected(router.provider, False)
        logging.error(
            "PROVIDER_DISABLED provider=%s reason=%s "
            "other_providers_continue=true "
            "restart or CLI reconnect required to retry",
            router.provider,
            exc,
        )
        # Keep task alive but sleeping so execution structure is healthy until a restart/reconnect request
        await asyncio.Event().wait()
    except Exception as exc:
        coordinator.set_connected(router.provider, False)
        logging.error(
            "PROVIDER_CRASHED provider=%s error=%s", router.provider, type(exc).__name__
        )
        await asyncio.Event().wait()


async def main():
    # 1. Load system settings
    symbols, keys = load_settings()
    if not selected:
        return 0

    coordinator = MinuteCoordinator(selected, symbols)

    # 2. START THE ADMIN CLI SERVER FIRST
    # We explicitly launch this on local interfaces to ensure the port binds immediately
    cli_admin = AdminCliProvider(coordinator, host="127.0.0.1", port=5001)

    logging.info(
        "STARTUP providers=%s symbols=%s first_minute=%s delay_seconds=%s",
        selected,
        symbols,
        coordinator.report_start.isoformat(),
        SUMMARY_DELAY_SECONDS,
    )

    # 3. Initialize background providers
    for provider in selected:
        if provider == "TRADINGVIEW":
            router_instance = TradingviewProvider(
                symbols,
                coordinator,
                credentials=keys,
                option_client=build_option_chain_client(keys),
            )
        else:
            router_instance = ProviderRouter(provider, symbols, keys, coordinator)

        PROVIDER_REGISTRY[provider] = {"router": router_instance, "task": None}

    # 4. Gather up all execution tasks securely
    tasks = [
        asyncio.create_task(coordinator.run(), name="MINUTE_COORDINATOR"),
        asyncio.create_task(
            cli_admin.run(), name="CLI_ADMIN_PANEL"
        ),  # Mount server task
    ]

    # Alpaca position monitor: independent of the RSI providers, polls the
    # account every minute and prints current option positions to its own log.
    # Starts whenever Alpaca credentials are available; otherwise it logs a
    # single disabled warning and exits.
    position_monitor = AlpacaPositionMonitor(keys)
    tasks.append(
        asyncio.create_task(
            run_provider_isolated(position_monitor, coordinator),
            name="ALPACA_POSITION_MONITOR",
        )
    )

    for provider, entry in PROVIDER_REGISTRY.items():
        task = asyncio.create_task(
            run_provider_isolated(entry["router"], coordinator), name=provider
        )
        entry["task"] = task
        tasks.append(task)

    # 5. Execute engine orchestration
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        for entry in PROVIDER_REGISTRY.values():
            if entry["task"] and not entry["task"].done():
                entry["task"].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
