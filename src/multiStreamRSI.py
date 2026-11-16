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
TradingView sends a consolidated 1m closed-bar multi-symbol batch payload
(`rsi_batch`) once per minute with explicit bar_start and per-symbol RSI/price.
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


KINEMATICS_SMOOTH_METHOD = os.getenv("KINEMATICS_SMOOTH_METHOD", "hma").strip().lower()
if KINEMATICS_SMOOTH_METHOD not in {"hma", "ema", "none"}:
    KINEMATICS_SMOOTH_METHOD = "hma"
KINEMATICS_SMOOTH_PERIOD = max(2, int(os.getenv("KINEMATICS_SMOOTH_PERIOD", str(RSI_PERIOD))))
KINEMATICS_RSI_OVERBOUGHT = float(os.getenv("KINEMATICS_RSI_OVERBOUGHT", "70.0"))
KINEMATICS_RSI_OVERSOLD = float(os.getenv("KINEMATICS_RSI_OVERSOLD", "30.0"))


def compute_wma(values, period):
    """Compute Weighted Moving Average series over values."""
    if not values:
        return []
    n = max(1, int(period))
    out = []
    for i in range(len(values)):
        window = values[max(0, i - n + 1) : i + 1]
        k = len(window)
        denom = k * (k + 1) / 2.0
        weighted = sum(val * (idx + 1) for idx, val in enumerate(window))
        out.append(weighted / denom)
    return out


def compute_ema(values, period):
    """Compute Exponential Moving Average series over values."""
    if not values:
        return []
    n = max(1, int(period))
    alpha = 2.0 / (n + 1.0)
    out = [float(values[0])]
    for val in values[1:]:
        out.append(alpha * float(val) + (1.0 - alpha) * out[-1])
    return out


def compute_hma(values, period):
    """Compute Hull Moving Average (HMA) series: WMA(2*WMA(n/2) - WMA(n), sqrt(n))."""
    if not values:
        return []
    n = max(2, int(period))
    half_n = max(1, n // 2)
    sqrt_n = max(1, int(round(math.sqrt(n))))
    wma_half = compute_wma(values, half_n)
    wma_full = compute_wma(values, n)
    raw_hull = [2.0 * h - f for h, f in zip(wma_half, wma_full)]
    return compute_wma(raw_hull, sqrt_n)


def compute_smoothed_series(values, period=None, method=None):
    """Return smoothed price series aligned to the given period (HMA, EMA, or raw)."""
    if period is None:
        period = KINEMATICS_SMOOTH_PERIOD
    if method is None:
        method = KINEMATICS_SMOOTH_METHOD
    clean = [float(v) for v in values]
    if not clean or method == "none":
        return clean
    if method == "ema":
        return compute_ema(clean, period)
    return compute_hma(clean, period)


def classify_rsi_zone(rsi_val, overbought=None, oversold=None):
    """Classify RSI into boundary friction zones."""
    if overbought is None:
        overbought = KINEMATICS_RSI_OVERBOUGHT
    if oversold is None:
        oversold = KINEMATICS_RSI_OVERSOLD
    if rsi_val is None or not math.isfinite(rsi_val):
        return "unknown"
    if rsi_val >= overbought:
        return "overbought"
    if rsi_val <= oversold:
        return "oversold"
    if 40.0 <= rsi_val <= 60.0:
        return "neutral_band"
    if rsi_val > 60.0:
        return "upper_expansion"
    return "lower_expansion"


def compute_kinematic_forecast(
    close,
    velocity,
    acceleration,
    rsi,
    prev_velocity=None,
    prev_acceleration=None,
    overbought=None,
    oversold=None,
):
    """Compute next-step kinematic projection P_hat_{t+1} = P_t + v_t + 0.5*a_t,
    leading/confirmed crossover signals, and 4-state RSI-filtered trading action."""
    if overbought is None:
        overbought = KINEMATICS_RSI_OVERBOUGHT
    if oversold is None:
        oversold = KINEMATICS_RSI_OVERSOLD
    projected_price = None
    projected_delta = None
    projected_delta_pct = None

    if close is not None and math.isfinite(close) and velocity is not None and math.isfinite(velocity):
        acc = float(acceleration) if (acceleration is not None and math.isfinite(acceleration)) else 0.0
        delta = float(velocity) + 0.5 * acc
        projected_delta = round(delta, 4)
        projected_price = round(float(close) + delta, 4)
        if close > 0:
            projected_delta_pct = round((delta / float(close)) * 100.0, 4)

    crossover_signal = "none"
    if velocity is not None and prev_velocity is not None:
        if prev_velocity >= 0 and velocity < 0:
            crossover_signal = "confirmed_bearish"
        elif prev_velocity <= 0 and velocity > 0:
            crossover_signal = "confirmed_bullish"
    if crossover_signal == "none" and acceleration is not None and prev_acceleration is not None:
        if prev_acceleration >= 0 and acceleration < 0 and (velocity is None or velocity > 0):
            crossover_signal = "leading_bearish"
        elif prev_acceleration <= 0 and acceleration > 0 and (velocity is None or velocity < 0):
            crossover_signal = "leading_bullish"

    rsi_zone = classify_rsi_zone(rsi, overbought=overbought, oversold=oversold)
    forecast_direction = "neutral"
    trading_action = "Hold / Monitor"

    if velocity is not None and acceleration is not None and rsi is not None:
        if velocity > 0 and acceleration < 0 and rsi >= overbought:
            forecast_direction = "bearish_reversal"
            trading_action = "Prepare to Short / Exit Longs"
        elif velocity < 0 and acceleration > 0 and rsi <= oversold:
            forecast_direction = "bullish_reversal"
            trading_action = "Prepare to Buy / Exit Shorts"
        elif velocity > 0 and acceleration > 0 and 40.0 <= rsi <= 60.0:
            forecast_direction = "bullish_continuation"
            trading_action = "Hold Long / Trend Follow"
        elif velocity < 0 and acceleration < 0 and 40.0 <= rsi <= 60.0:
            forecast_direction = "bearish_continuation"
            trading_action = "Hold Short / Trend Follow"
        elif velocity > 0 and acceleration > 0 and rsi > 60.0 and rsi < overbought:
            forecast_direction = "bullish_momentum"
            trading_action = "Trail Long / Watch 70 Friction"
        elif velocity < 0 and acceleration < 0 and rsi < 40.0 and rsi > oversold:
            forecast_direction = "bearish_momentum"
            trading_action = "Trail Short / Watch 30 Support"
        elif velocity > 0 and acceleration < 0:
            forecast_direction = "bullish_decelerating"
            trading_action = "Tighten Stops / Watch Crossover"
        elif velocity < 0 and acceleration > 0:
            forecast_direction = "bearish_decelerating"
            trading_action = "Watch for Bottom Crossover"

    return {
        "projected_price_next": projected_price,
        "projected_delta": projected_delta,
        "projected_delta_pct": projected_delta_pct,
        "rsi_zone": rsi_zone,
        "forecast_direction": forecast_direction,
        "trading_action": trading_action,
        "crossover_signal": crossover_signal,
    }


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
        self.option_chain_cache = {}
        self.option_buy_events = {}
        self.option_sell_events = {}
        self.event_loop = None
        self.minute_finalized_event = None
        self.minute_finalized_listeners = []
        logger = None if emit is not None else make_summary_logger()
        self.emit = emit or (
            lambda record: logger.info(
                "%s", json.dumps(record, separators=(",", ":"), allow_nan=False)
            )
        )

    def attach_event_loop(self, loop):
        self.event_loop = loop
        if self.minute_finalized_event is None:
            self.minute_finalized_event = asyncio.Event()

    def subscribe_minute_finalized(self):
        """Return a dedicated asyncio.Event signaled on each minute finalization."""
        evt = asyncio.Event()
        self.minute_finalized_listeners.append(evt)
        return evt

    def notify_minute_finalized(self):
        def _set_all():
            if self.minute_finalized_event is not None:
                self.minute_finalized_event.set()
            for evt in list(self.minute_finalized_listeners):
                evt.set()

        if self.minute_finalized_event is None and not self.minute_finalized_listeners:
            return
        try:
            running = asyncio.get_running_loop()
            if running is self.event_loop:
                _set_all()
                return
        except RuntimeError:
            pass
        if self.event_loop is not None and not self.event_loop.is_closed():
            self.event_loop.call_soon_threadsafe(_set_all)

    def record_option_contract(
        self,
        symbol,
        contract,
        signal,
        event_time=None,
        clean_payload=None,
        source="collect_option_chain",
    ):
        """Cache the latest contract returned by collect_option_chain per symbol."""
        sym = str(symbol).strip().upper()
        self.option_chain_cache[sym] = {
            "symbol": sym,
            "contract": contract,
            "signal": signal,
            "event_time": event_time,
            "clean_payload": clean_payload,
            "source": source,
            "collected_at_utc": datetime.now(UTC).isoformat(),
        }

    def record_option_buy_event(
        self,
        symbol,
        executed_at,
        contract,
        qty=1,
        order_id=None,
        trigger_minute=None,
        reason=None,
        rsi=None,
        dry_run=False,
    ):
        """Record an executed option buy order aligned to its exact UTC minute."""
        sym = str(symbol).strip().upper()
        if isinstance(executed_at, str):
            exec_dt = as_utc(
                datetime.fromisoformat(executed_at.strip().replace("Z", "+00:00"))
            )
        else:
            exec_dt = as_utc(executed_at or datetime.now(UTC))
        min_dt = minute_utc(exec_dt)

        trig_iso = None
        if isinstance(trigger_minute, datetime):
            trig_iso = minute_utc(trigger_minute).isoformat()
        elif isinstance(trigger_minute, str) and trigger_minute.strip():
            try:
                trig_dt = datetime.fromisoformat(
                    trigger_minute.strip().replace("Z", "+00:00")
                )
                trig_iso = (
                    minute_utc(trig_dt).isoformat()
                    if trig_dt.tzinfo is not None
                    else trigger_minute.strip()
                )
            except ValueError:
                trig_iso = trigger_minute.strip()

        event = {
            "symbol": sym,
            "minute_utc": min_dt.isoformat(),
            "executed_at_utc": exec_dt.isoformat(),
            "trigger_minute_utc": trig_iso,
            "contract": str(contract) if contract is not None else None,
            "qty": int(qty or 1),
            "order_id": str(order_id) if order_id is not None else None,
            "reason": reason,
            "rsi": round(float(rsi), 2) if rsi is not None else None,
            "dry_run": bool(dry_run),
        }
        events = self.option_buy_events.setdefault(sym, [])
        events.append(event)
        if len(events) > 100:
            del events[:-100]
        return event

    def record_option_sell_event(
        self,
        symbol,
        executed_at,
        contract,
        qty=1,
        order_id=None,
        trigger_minute=None,
        reason=None,
        rsi=None,
        dry_run=False,
    ):
        """Record an executed sell order aligned to its exact UTC minute."""
        sym = str(symbol).strip().upper()
        if isinstance(executed_at, str):
            exec_dt = as_utc(
                datetime.fromisoformat(executed_at.strip().replace("Z", "+00:00"))
            )
        else:
            exec_dt = as_utc(executed_at or datetime.now(UTC))
        min_dt = minute_utc(exec_dt)

        trig_iso = None
        if isinstance(trigger_minute, datetime):
            trig_iso = minute_utc(trigger_minute).isoformat()
        elif isinstance(trigger_minute, str) and trigger_minute.strip():
            try:
                trig_dt = datetime.fromisoformat(
                    trigger_minute.strip().replace("Z", "+00:00")
                )
                trig_iso = (
                    minute_utc(trig_dt).isoformat()
                    if trig_dt.tzinfo is not None
                    else trigger_minute.strip()
                )
            except ValueError:
                trig_iso = trigger_minute.strip()

        clean_qty = (
            int(round(qty))
            if isinstance(qty, (int, float)) and abs(qty - round(qty)) < 1e-9
            else qty
        )
        event = {
            "symbol": sym,
            "minute_utc": min_dt.isoformat(),
            "executed_at_utc": exec_dt.isoformat(),
            "trigger_minute_utc": trig_iso,
            "contract": str(contract) if contract is not None else None,
            "qty": clean_qty,
            "order_id": str(order_id) if order_id is not None else None,
            "reason": reason,
            "rsi": round(float(rsi), 2) if rsi is not None else None,
            "dry_run": bool(dry_run),
        }
        events = self.option_sell_events.setdefault(sym, [])
        events.append(event)
        if len(events) > 100:
            del events[:-100]
        return event

    def latest_option_contract(self, symbol, max_age_seconds=None):
        """Return cached collect_option_chain entry for symbol if fresh enough."""
        sym = str(symbol).strip().upper()
        entry = self.option_chain_cache.get(sym)
        if not entry:
            return None
        if max_age_seconds is not None:
            try:
                collected = datetime.fromisoformat(entry["collected_at_utc"])
                age = (datetime.now(UTC) - as_utc(collected)).total_seconds()
                if age > max_age_seconds:
                    return None
            except Exception:
                return None
        return entry

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
        # Seed warmup kinematics history so initial live summaries have velocity & smoothed context
        if sorted_bars:
            history = self.kinematics_history.setdefault((provider, symbol), [])
            seed_slice = sorted_bars[-60:]
            seed_closes = [b.close for b in seed_slice]
            smoothed_seed = compute_smoothed_series(seed_closes)
            prev_sv = None
            prev_sa = None
            for idx, b in enumerate(seed_slice):
                sc = round(smoothed_seed[idx], 4)
                sv = round(sc - smoothed_seed[idx - 1], 4) if idx >= 1 else None
                sa = round(sv - prev_sv, 4) if (sv is not None and prev_sv is not None) else None
                pv = tracker.latest_price_velocity if idx == len(seed_slice) - 1 else (
                    round(b.close - seed_slice[idx - 1].close, 4) if idx >= 1 else None
                )
                pa = tracker.latest_price_acceleration if idx == len(seed_slice) - 1 else None
                rv = tracker.latest_rsi_velocity if idx == len(seed_slice) - 1 else None
                ra = tracker.latest_rsi_acceleration if idx == len(seed_slice) - 1 else None
                rsi_val = tracker.latest["rsi"] if (tracker.latest and idx == len(seed_slice) - 1) else None
                reg = classify_kinematic_regime(rv, ra, sv if sv is not None else pv, sa if sa is not None else pa)
                fc = compute_kinematic_forecast(
                    b.close,
                    sv if sv is not None else pv,
                    sa if sa is not None else pa,
                    rsi_val,
                    prev_velocity=prev_sv,
                    prev_acceleration=prev_sa,
                )
                if idx >= len(seed_slice) - 20:
                    history.append(
                        {
                            "minute": minute_utc(b.timestamp),
                            "timestamp": (
                                b.timestamp.isoformat()
                                if hasattr(b.timestamp, "isoformat")
                                else str(b.timestamp)
                            ),
                            "close": b.close,
                            "smoothed_close": sc,
                            "rsi": rsi_val,
                            "price_velocity": pv,
                            "price_velocity_pct": None,
                            "price_acceleration": pa,
                            "smoothed_velocity": sv,
                            "smoothed_acceleration": sa,
                            "rsi_velocity": rv,
                            "rsi_acceleration": ra,
                            "projected_price_next": fc["projected_price_next"],
                            "projected_delta_pct": fc["projected_delta_pct"],
                            "rsi_zone": fc["rsi_zone"],
                            "forecast_direction": fc["forecast_direction"],
                            "trading_action": fc["trading_action"],
                            "crossover_signal": fc["crossover_signal"],
                            "regime": reg,
                        }
                    )
                prev_sv = sv
                prev_sa = sa
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

        smoothed_close = None
        smoothed_velocity = None
        smoothed_acceleration = None
        if curr_close is not None:
            close_series = [e["close"] for e in history if e.get("close") is not None] + [curr_close]
            smoothed_vals = compute_smoothed_series(close_series)
            if smoothed_vals:
                smoothed_close = round(smoothed_vals[-1], 4)
                if len(smoothed_vals) >= 2:
                    smoothed_velocity = round(smoothed_vals[-1] - smoothed_vals[-2], 4)
                if len(smoothed_vals) >= 3:
                    prev_smoothed_vel = smoothed_vals[-2] - smoothed_vals[-3]
                    smoothed_acceleration = round(smoothed_velocity - prev_smoothed_vel, 4)

        prev_vel_for_cross = None
        prev_acc_for_cross = None
        if history:
            prev_hist = next(
                (e for e in reversed(history) if e.get("smoothed_velocity") is not None or e.get("price_velocity") is not None),
                None,
            )
            if prev_hist is not None:
                prev_vel_for_cross = (
                    prev_hist.get("smoothed_velocity")
                    if prev_hist.get("smoothed_velocity") is not None
                    else prev_hist.get("price_velocity")
                )
                prev_acc_for_cross = (
                    prev_hist.get("smoothed_acceleration")
                    if prev_hist.get("smoothed_acceleration") is not None
                    else prev_hist.get("price_acceleration")
                )

        active_pv = smoothed_velocity if smoothed_velocity is not None else price_velocity
        active_pa = smoothed_acceleration if smoothed_acceleration is not None else price_acceleration
        forecast = compute_kinematic_forecast(
            curr_close,
            active_pv,
            active_pa,
            curr_rsi,
            prev_velocity=prev_vel_for_cross,
            prev_acceleration=prev_acc_for_cross,
        )

        # Classify kinematics regime
        regime = classify_kinematic_regime(
            rsi_velocity,
            rsi_acceleration,
            active_pv,
            active_pa,
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
                    "smoothed_close": smoothed_close,
                    "rsi": curr_rsi,
                    "price_velocity": price_velocity,
                    "price_velocity_pct": price_velocity_pct,
                    "price_acceleration": price_acceleration,
                    "price_acceleration_pct": price_acceleration_pct,
                    "smoothed_velocity": smoothed_velocity,
                    "smoothed_acceleration": smoothed_acceleration,
                    "rsi_velocity": rsi_velocity,
                    "rsi_acceleration": rsi_acceleration,
                    "momentum_3m_pct": price_velocity_3m_pct,
                    "projected_price_next": forecast["projected_price_next"],
                    "projected_delta": forecast["projected_delta"],
                    "projected_delta_pct": forecast["projected_delta_pct"],
                    "rsi_zone": forecast["rsi_zone"],
                    "forecast_direction": forecast["forecast_direction"],
                    "trading_action": forecast["trading_action"],
                    "crossover_signal": forecast["crossover_signal"],
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
                "smoothed_close": smoothed_close,
                "smoothed_velocity": smoothed_velocity,
                "smoothed_acceleration": smoothed_acceleration,
                "rsi_velocity": rsi_velocity,
                "rsi_acceleration": rsi_acceleration,
                "projected_price_next": forecast["projected_price_next"],
                "forecast_direction": forecast["forecast_direction"],
                "kinematics": {
                    "price_velocity_1m": price_velocity,
                    "price_velocity_1m_pct": price_velocity_pct,
                    "price_acceleration_1m": price_acceleration,
                    "price_acceleration_1m_pct": price_acceleration_pct,
                    "smooth_method": KINEMATICS_SMOOTH_METHOD,
                    "smooth_period": KINEMATICS_SMOOTH_PERIOD,
                    "smoothed_close": smoothed_close,
                    "smoothed_velocity": smoothed_velocity,
                    "smoothed_acceleration": smoothed_acceleration,
                    "rsi_velocity_1m": rsi_velocity,
                    "rsi_acceleration_1m": rsi_acceleration,
                    "rsi_velocity_3m": rsi_velocity_3m,
                    "price_velocity_3m_pct": price_velocity_3m_pct,
                    "projected_price_next": forecast["projected_price_next"],
                    "projected_delta": forecast["projected_delta"],
                    "projected_delta_pct": forecast["projected_delta_pct"],
                    "rsi_zone": forecast["rsi_zone"],
                    "forecast_direction": forecast["forecast_direction"],
                    "trading_action": forecast["trading_action"],
                    "crossover_signal": forecast["crossover_signal"],
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
                    "forecasts": {
                        "bearish_reversal": 0,
                        "bullish_reversal": 0,
                        "bullish_continuation": 0,
                        "bearish_continuation": 0,
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
                    fc_dir = k.get("forecast_direction")
                    if fc_dir in kinematics_summary["forecasts"]:
                        kinematics_summary["forecasts"][fc_dir] += 1

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
        if emitted > 0:
            self.notify_minute_finalized()
        return emitted

    async def run(self):
        self.attach_event_loop(asyncio.get_running_loop())
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

            orig_subscribe_all = getattr(self.stream, "_subscribe_all", None)
            if callable(orig_subscribe_all):
                async def _wrapped_subscribe_all(*args, **kwargs):
                    res = await orig_subscribe_all(*args, **kwargs)
                    self.coordinator.set_connected(self.provider, True)
                    return res

                self.stream._subscribe_all = _wrapped_subscribe_all

            async def watch_connection():
                # StockDataStream._run_forever() keeps the WebSocket open internally,
                # whereas callback(bar) only fires when a live 1m bar trades. Track
                # underlying WebSocket state so ALPACA reports ONLINE outside market
                # hours / before the first bar as well as OFFLINE on disconnects.
                while True:
                    ws = getattr(self.stream, "_ws", None)
                    state = getattr(ws, "state", None) if ws is not None else None
                    state_name = (
                        getattr(state, "name", str(state or "")).upper()
                        if state is not None
                        else ""
                    )
                    ws_closed = (
                        ws is None
                        or bool(getattr(ws, "closed", False))
                        or state_name in {"CLOSED", "CLOSING"}
                    )
                    if not ws_closed and getattr(self.stream, "_running", True):
                        self.coordinator.set_connected(self.provider, True)
                    elif self.coordinator.connected.get(self.provider):
                        self.coordinator.set_connected(self.provider, False)
                    await asyncio.sleep(1.0)

            watcher = asyncio.create_task(watch_connection())
            try:
                await self.stream._run_forever()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                self.coordinator.set_connected(self.provider, False)

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


def _strip_ini_value(value):
    if not isinstance(value, str):
        return value
    cleaned = value.split(";", 1)[0].strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _ini_str(cfg, section, option, env_name, default):
    if cfg.has_option(section, option):
        raw = _strip_ini_value(cfg.get(section, option, fallback=""))
        if raw != "":
            return str(raw)
    return os.getenv(env_name, default)


def _ini_bool(cfg, section, option, env_name, default):
    if cfg.has_option(section, option):
        raw = str(_strip_ini_value(cfg.get(section, option, fallback=""))).strip().lower()
        if raw in {"1", "true", "yes"}:
            return True
        if raw in {"0", "false", "no"}:
            return False
        raise ValueError(f"[{section}] {option} must be true/false; got {raw!r}")
    return os.getenv(env_name, "true" if default else "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _ini_int(cfg, section, option, env_name, default, minimum=None):
    if cfg.has_option(section, option):
        raw = _strip_ini_value(cfg.get(section, option, fallback=""))
        val = int(raw)
    else:
        val = int(os.getenv(env_name, str(default)))
    return max(minimum, val) if minimum is not None else val


def _ini_float(cfg, section, option, env_name, default):
    if cfg.has_option(section, option):
        raw = _strip_ini_value(cfg.get(section, option, fallback=""))
        return float(raw)
    return float(os.getenv(env_name, str(default)))


def _apply_ini_feature_settings(cfg):
    """Load [kinematics], [option_buyer], and [option_seller] overrides from lib/parms.ini."""
    global KINEMATICS_SMOOTH_METHOD, KINEMATICS_SMOOTH_PERIOD
    global KINEMATICS_RSI_OVERBOUGHT, KINEMATICS_RSI_OVERSOLD
    global OPTION_CHAIN_RSI_LOW, OPTION_CHAIN_RSI_HIGH
    global ALPACA_PAPER, OPTION_BUY_ENABLED, OPTION_BUY_DRY_RUN
    global OPTION_BUY_QTY, OPTION_BUY_ORDER_TYPE, OPTION_BUY_COOLDOWN_MINUTES
    global OPTION_BUY_MAX_OPEN_CONTRACTS, OPTION_BUY_MAX_AGE_SECONDS
    global OPTION_BUY_FETCH_IF_MISSING, OPTION_BUY_ALLOW_DUPLICATE_UNDERLYING
    global OPTION_BUY_REQUIRE_PROJECTION_ALIGNMENT, OPTION_BUY_REQUIRE_RIGHT_MATCH
    global OPTION_BUY_ALLOWED_FORECASTS
    global OPTION_SELL_ENABLED, OPTION_SELL_DRY_RUN, OPTION_SELL_PAPER
    global OPTION_SELL_QTY, OPTION_SELL_CLOSE_FULL_POSITION
    global OPTION_SELL_ORDER_TYPE, OPTION_SELL_COOLDOWN_MINUTES
    global OPTION_SELL_OPTIONS_ONLY, OPTION_SELL_REQUIRE_PROJECTION_ALIGNMENT
    global OPTION_SELL_ALLOWED_FORECASTS
    global OPTION_SELL_LIMIT_DISCOUNT_PCT, OPTION_SELL_STOP_LOSS_LIMIT_DISCOUNT_PCT
    global OPTION_SELL_STOP_LOSS_FORCE_MARKET
    global OPTION_SELL_PROFIT_TARGET_ENABLED, OPTION_SELL_PROFIT_TARGET_PCT
    global OPTION_SELL_TRAILING_STOP_ENABLED, OPTION_SELL_TRAILING_ACTIVATION_PCT
    global OPTION_SELL_TRAILING_DRAWDOWN_PCT, OPTION_SELL_TRAILING_REQUIRE_NEGATIVE_VELOCITY
    global OPTION_SELL_PEAK_PROFIT_CALLS_ONLY
    global OPTION_SELL_STOP_LOSS_ENABLED, OPTION_SELL_STOP_LOSS_PCT
    global OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES
    global OPTION_SELL_STOP_LOSS_REQUIRE_KINEMATICS, OPTION_SELL_STOP_LOSS_HARD_PCT

    smooth_method = (
        _ini_str(cfg, "kinematics", "smooth_method", "KINEMATICS_SMOOTH_METHOD", "hma")
        .strip()
        .lower()
    )
    if smooth_method not in {"hma", "ema", "none"}:
        raise ValueError(
            f"[kinematics] smooth_method must be hma, ema, or none; got {smooth_method!r}"
        )
    KINEMATICS_SMOOTH_METHOD = smooth_method
    KINEMATICS_SMOOTH_PERIOD = _ini_int(
        cfg, "kinematics", "smooth_period", "KINEMATICS_SMOOTH_PERIOD", RSI_PERIOD, minimum=2
    )
    KINEMATICS_RSI_OVERBOUGHT = _ini_float(
        cfg, "kinematics", "rsi_overbought", "KINEMATICS_RSI_OVERBOUGHT", 70.0
    )
    KINEMATICS_RSI_OVERSOLD = _ini_float(
        cfg, "kinematics", "rsi_oversold", "KINEMATICS_RSI_OVERSOLD", 30.0
    )

    OPTION_CHAIN_RSI_LOW = _ini_float(
        cfg, "option_buyer", "rsi_low", "OPTION_CHAIN_RSI_LOW", 35.0
    )
    OPTION_CHAIN_RSI_HIGH = _ini_float(
        cfg, "option_buyer", "rsi_high", "OPTION_CHAIN_RSI_HIGH", 65.0
    )
    if cfg.has_section("option_seller") and cfg.has_option("option_seller", "rsi_high"):
        OPTION_CHAIN_RSI_HIGH = _ini_float(
            cfg, "option_seller", "rsi_high", "OPTION_CHAIN_RSI_HIGH", OPTION_CHAIN_RSI_HIGH
        )
    ALPACA_PAPER = _ini_bool(cfg, "option_buyer", "paper", "ALPACA_PAPER", True)
    OPTION_BUY_ENABLED = _ini_bool(
        cfg, "option_buyer", "enabled", "OPTION_BUY_ENABLED", True
    )
    OPTION_BUY_DRY_RUN = _ini_bool(
        cfg, "option_buyer", "dry_run", "OPTION_BUY_DRY_RUN", True
    )
    OPTION_BUY_QTY = _ini_int(
        cfg, "option_buyer", "qty", "OPTION_BUY_QTY", 1, minimum=1
    )
    OPTION_BUY_ORDER_TYPE = (
        _ini_str(cfg, "option_buyer", "order_type", "OPTION_BUY_ORDER_TYPE", "market")
        .strip()
        .lower()
    )
    OPTION_BUY_COOLDOWN_MINUTES = _ini_int(
        cfg,
        "option_buyer",
        "cooldown_minutes",
        "OPTION_BUY_COOLDOWN_MINUTES",
        15,
        minimum=0,
    )
    OPTION_BUY_MAX_OPEN_CONTRACTS = _ini_int(
        cfg,
        "option_buyer",
        "max_open_contracts",
        "OPTION_BUY_MAX_OPEN_CONTRACTS",
        5,
        minimum=1,
    )
    OPTION_BUY_MAX_AGE_SECONDS = _ini_int(
        cfg,
        "option_buyer",
        "max_contract_age_seconds",
        "OPTION_BUY_MAX_AGE_SECONDS",
        180,
        minimum=30,
    )
    OPTION_BUY_FETCH_IF_MISSING = _ini_bool(
        cfg, "option_buyer", "fetch_if_missing", "OPTION_BUY_FETCH_IF_MISSING", True
    )
    OPTION_BUY_ALLOW_DUPLICATE_UNDERLYING = _ini_bool(
        cfg,
        "option_buyer",
        "allow_duplicate_underlying",
        "OPTION_BUY_ALLOW_DUPLICATE_UNDERLYING",
        False,
    )
    OPTION_BUY_REQUIRE_PROJECTION_ALIGNMENT = _ini_bool(
        cfg,
        "option_buyer",
        "require_projection_alignment",
        "OPTION_BUY_REQUIRE_PROJECTION_ALIGNMENT",
        True,
    )
    OPTION_BUY_REQUIRE_RIGHT_MATCH = _ini_bool(
        cfg,
        "option_buyer",
        "require_right_match",
        "OPTION_BUY_REQUIRE_RIGHT_MATCH",
        True,
    )
    raw_forecasts = _ini_str(
        cfg,
        "option_buyer",
        "allowed_forecasts",
        "OPTION_BUY_ALLOWED_FORECASTS",
        "bullish_reversal,bullish_continuation,bullish_momentum",
    )
    OPTION_BUY_ALLOWED_FORECASTS = {
        s.strip().lower() for s in raw_forecasts.split(",") if s.strip()
    }

    OPTION_SELL_PAPER = _ini_bool(
        cfg, "option_seller", "paper", "OPTION_SELL_PAPER", ALPACA_PAPER
    )
    OPTION_SELL_ENABLED = _ini_bool(
        cfg, "option_seller", "enabled", "OPTION_SELL_ENABLED", True
    )
    OPTION_SELL_DRY_RUN = _ini_bool(
        cfg, "option_seller", "dry_run", "OPTION_SELL_DRY_RUN", True
    )
    OPTION_SELL_QTY = _ini_int(
        cfg, "option_seller", "qty", "OPTION_SELL_QTY", 1, minimum=1
    )
    OPTION_SELL_CLOSE_FULL_POSITION = _ini_bool(
        cfg,
        "option_seller",
        "close_full_position",
        "OPTION_SELL_CLOSE_FULL_POSITION",
        True,
    )
    OPTION_SELL_ORDER_TYPE = (
        _ini_str(cfg, "option_seller", "order_type", "OPTION_SELL_ORDER_TYPE", "market")
        .strip()
        .lower()
    )
    OPTION_SELL_COOLDOWN_MINUTES = _ini_int(
        cfg,
        "option_seller",
        "cooldown_minutes",
        "OPTION_SELL_COOLDOWN_MINUTES",
        15,
        minimum=0,
    )
    OPTION_SELL_OPTIONS_ONLY = _ini_bool(
        cfg, "option_seller", "options_only", "OPTION_SELL_OPTIONS_ONLY", False
    )
    OPTION_SELL_REQUIRE_PROJECTION_ALIGNMENT = _ini_bool(
        cfg,
        "option_seller",
        "require_projection_alignment",
        "OPTION_SELL_REQUIRE_PROJECTION_ALIGNMENT",
        True,
    )
    raw_sell_forecasts = _ini_str(
        cfg,
        "option_seller",
        "allowed_forecasts",
        "OPTION_SELL_ALLOWED_FORECASTS",
        "bearish_reversal,bearish_continuation,bearish_momentum",
    )
    OPTION_SELL_ALLOWED_FORECASTS = {
        s.strip().lower() for s in raw_sell_forecasts.split(",") if s.strip()
    }
    OPTION_SELL_LIMIT_DISCOUNT_PCT = max(
        0.0,
        abs(
            _ini_float(
                cfg,
                "option_seller",
                "limit_discount_pct",
                "OPTION_SELL_LIMIT_DISCOUNT_PCT",
                1.0,
            )
        ),
    )
    OPTION_SELL_STOP_LOSS_LIMIT_DISCOUNT_PCT = max(
        0.0,
        abs(
            _ini_float(
                cfg,
                "option_seller",
                "stop_loss_limit_discount_pct",
                "OPTION_SELL_STOP_LOSS_LIMIT_DISCOUNT_PCT",
                3.0,
            )
        ),
    )
    OPTION_SELL_STOP_LOSS_FORCE_MARKET = _ini_bool(
        cfg,
        "option_seller",
        "stop_loss_force_market",
        "OPTION_SELL_STOP_LOSS_FORCE_MARKET",
        False,
    )
    OPTION_SELL_PROFIT_TARGET_ENABLED = _ini_bool(
        cfg,
        "option_seller",
        "profit_target_enabled",
        "OPTION_SELL_PROFIT_TARGET_ENABLED",
        True,
    )
    OPTION_SELL_PROFIT_TARGET_PCT = max(
        0.0,
        abs(
            _ini_float(
                cfg,
                "option_seller",
                "profit_target_pct",
                "OPTION_SELL_PROFIT_TARGET_PCT",
                25.0,
            )
        ),
    )
    OPTION_SELL_TRAILING_STOP_ENABLED = _ini_bool(
        cfg,
        "option_seller",
        "trailing_stop_enabled",
        "OPTION_SELL_TRAILING_STOP_ENABLED",
        True,
    )
    OPTION_SELL_TRAILING_ACTIVATION_PCT = max(
        0.0,
        abs(
            _ini_float(
                cfg,
                "option_seller",
                "trailing_activation_pct",
                "OPTION_SELL_TRAILING_ACTIVATION_PCT",
                10.0,
            )
        ),
    )
    OPTION_SELL_TRAILING_DRAWDOWN_PCT = max(
        0.0,
        abs(
            _ini_float(
                cfg,
                "option_seller",
                "trailing_drawdown_pct",
                "OPTION_SELL_TRAILING_DRAWDOWN_PCT",
                5.0,
            )
        ),
    )
    OPTION_SELL_TRAILING_REQUIRE_NEGATIVE_VELOCITY = _ini_bool(
        cfg,
        "option_seller",
        "trailing_require_negative_velocity",
        "OPTION_SELL_TRAILING_REQUIRE_NEGATIVE_VELOCITY",
        True,
    )
    OPTION_SELL_PEAK_PROFIT_CALLS_ONLY = _ini_bool(
        cfg,
        "option_seller",
        "peak_profit_calls_only",
        "OPTION_SELL_PEAK_PROFIT_CALLS_ONLY",
        False,
    )
    OPTION_SELL_STOP_LOSS_ENABLED = _ini_bool(
        cfg,
        "option_seller",
        "stop_loss_enabled",
        "OPTION_SELL_STOP_LOSS_ENABLED",
        True,
    )
    OPTION_SELL_STOP_LOSS_PCT = max(
        0.0,
        abs(
            _ini_float(
                cfg,
                "option_seller",
                "stop_loss_pct",
                "OPTION_SELL_STOP_LOSS_PCT",
                4.0,
            )
        ),
    )
    OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES = _ini_int(
        cfg,
        "option_seller",
        "stop_loss_consecutive_minutes",
        "OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES",
        3,
        minimum=1,
    )
    OPTION_SELL_STOP_LOSS_REQUIRE_KINEMATICS = _ini_bool(
        cfg,
        "option_seller",
        "stop_loss_require_kinematics",
        "OPTION_SELL_STOP_LOSS_REQUIRE_KINEMATICS",
        True,
    )
    OPTION_SELL_STOP_LOSS_HARD_PCT = max(
        0.0,
        abs(
            _ini_float(
                cfg,
                "option_seller",
                "stop_loss_hard_pct",
                "OPTION_SELL_STOP_LOSS_HARD_PCT",
                0.0,
            )
        ),
    )
    logging.info(
        "FEATURE_CONFIG smooth=%s(%d) rsi_ob=%.1f rsi_os=%.1f "
        "buy_enabled=%s buy_dry_run=%s sell_enabled=%s sell_dry_run=%s "
        "paper=%s sell_paper=%s rsi_low=%.1f rsi_high=%.1f buy_qty=%d sell_full=%s "
        "limit_disc=%.2f%% sl_disc=%.2f%% pt_enabled=%s pt=%.1f%% ts_enabled=%s ts=%.1f%%/%.1f%% "
        "sl_enabled=%s sl_pct=%.2f%% sl_mins=%d sl_hard=%.2f%%",
        KINEMATICS_SMOOTH_METHOD,
        KINEMATICS_SMOOTH_PERIOD,
        KINEMATICS_RSI_OVERBOUGHT,
        KINEMATICS_RSI_OVERSOLD,
        OPTION_BUY_ENABLED,
        OPTION_BUY_DRY_RUN,
        OPTION_SELL_ENABLED,
        OPTION_SELL_DRY_RUN,
        ALPACA_PAPER,
        OPTION_SELL_PAPER,
        OPTION_CHAIN_RSI_LOW,
        OPTION_CHAIN_RSI_HIGH,
        OPTION_BUY_QTY,
        OPTION_SELL_CLOSE_FULL_POSITION,
        OPTION_SELL_LIMIT_DISCOUNT_PCT,
        OPTION_SELL_STOP_LOSS_LIMIT_DISCOUNT_PCT,
        OPTION_SELL_PROFIT_TARGET_ENABLED,
        OPTION_SELL_PROFIT_TARGET_PCT,
        OPTION_SELL_TRAILING_STOP_ENABLED,
        OPTION_SELL_TRAILING_ACTIVATION_PCT,
        OPTION_SELL_TRAILING_DRAWDOWN_PCT,
        OPTION_SELL_STOP_LOSS_ENABLED,
        OPTION_SELL_STOP_LOSS_PCT,
        OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES,
        OPTION_SELL_STOP_LOSS_HARD_PCT,
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

    # Read provider and feature flags with standard INI semantics, independent of
    # how the project's JsonIniParser interprets dotted names or quoted values.
    provider_config = configparser.ConfigParser(interpolation=None)
    with config_path.open("r", encoding="utf-8-sig") as handle:
        provider_config.read_file(handle)

    _apply_ini_feature_settings(provider_config)

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

    if selected:
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
WEBHOOK_MAX_BYTES = 65536
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
# Option-chain collection trigger: a TradingView webhook RSI at or below the
# low threshold is the BUY signal (oversold); at or above the high threshold
# is the SELL signal (overbought). Either signal fetches the pruned option
# chain and writes it to logs/optionchains/.
OPTION_CHAIN_RSI_LOW = float(os.getenv("OPTION_CHAIN_RSI_LOW", "35"))
OPTION_CHAIN_RSI_HIGH = float(os.getenv("OPTION_CHAIN_RSI_HIGH", "65"))
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
OPTION_BUY_ENABLED = os.getenv("OPTION_BUY_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
OPTION_BUY_DRY_RUN = os.getenv("OPTION_BUY_DRY_RUN", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
OPTION_BUY_QTY = max(1, int(os.getenv("OPTION_BUY_QTY", "1")))
OPTION_BUY_ORDER_TYPE = os.getenv("OPTION_BUY_ORDER_TYPE", "market").strip().lower()
OPTION_BUY_COOLDOWN_MINUTES = max(
    0, int(os.getenv("OPTION_BUY_COOLDOWN_MINUTES", "15"))
)
OPTION_BUY_MAX_OPEN_CONTRACTS = max(
    1, int(os.getenv("OPTION_BUY_MAX_OPEN_CONTRACTS", "5"))
)
OPTION_BUY_MAX_AGE_SECONDS = max(
    30, int(os.getenv("OPTION_BUY_MAX_AGE_SECONDS", "180"))
)
OPTION_BUY_FETCH_IF_MISSING = os.getenv(
    "OPTION_BUY_FETCH_IF_MISSING", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_BUY_ALLOW_DUPLICATE_UNDERLYING = os.getenv(
    "OPTION_BUY_ALLOW_DUPLICATE_UNDERLYING", "false"
).strip().lower() in {"1", "true", "yes"}
OPTION_BUY_REQUIRE_PROJECTION_ALIGNMENT = os.getenv(
    "OPTION_BUY_REQUIRE_PROJECTION_ALIGNMENT", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_BUY_REQUIRE_RIGHT_MATCH = os.getenv(
    "OPTION_BUY_REQUIRE_RIGHT_MATCH", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_BUY_ALLOWED_FORECASTS = {
    s.strip().lower()
    for s in os.getenv(
        "OPTION_BUY_ALLOWED_FORECASTS",
        "bullish_reversal,bullish_continuation,bullish_momentum",
    ).split(",")
    if s.strip()
}
OPTION_SELL_PAPER = os.getenv(
    "OPTION_SELL_PAPER", "true" if ALPACA_PAPER else "false"
).strip().lower() not in {"0", "false", "no"}
OPTION_SELL_ENABLED = os.getenv("OPTION_SELL_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
OPTION_SELL_DRY_RUN = os.getenv("OPTION_SELL_DRY_RUN", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
OPTION_SELL_QTY = max(1, int(os.getenv("OPTION_SELL_QTY", "1")))
OPTION_SELL_CLOSE_FULL_POSITION = os.getenv(
    "OPTION_SELL_CLOSE_FULL_POSITION", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_ORDER_TYPE = os.getenv("OPTION_SELL_ORDER_TYPE", "market").strip().lower()
OPTION_SELL_COOLDOWN_MINUTES = max(
    0, int(os.getenv("OPTION_SELL_COOLDOWN_MINUTES", "15"))
)
OPTION_SELL_OPTIONS_ONLY = os.getenv(
    "OPTION_SELL_OPTIONS_ONLY", "false"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_REQUIRE_PROJECTION_ALIGNMENT = os.getenv(
    "OPTION_SELL_REQUIRE_PROJECTION_ALIGNMENT", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_ALLOWED_FORECASTS = {
    s.strip().lower()
    for s in os.getenv(
        "OPTION_SELL_ALLOWED_FORECASTS",
        "bearish_reversal,bearish_continuation,bearish_momentum",
    ).split(",")
    if s.strip()
}
OPTION_SELL_LIMIT_DISCOUNT_PCT = max(
    0.0, abs(float(os.getenv("OPTION_SELL_LIMIT_DISCOUNT_PCT", "1.0")))
)
OPTION_SELL_STOP_LOSS_LIMIT_DISCOUNT_PCT = max(
    0.0, abs(float(os.getenv("OPTION_SELL_STOP_LOSS_LIMIT_DISCOUNT_PCT", "3.0")))
)
OPTION_SELL_STOP_LOSS_FORCE_MARKET = os.getenv(
    "OPTION_SELL_STOP_LOSS_FORCE_MARKET", "false"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_PROFIT_TARGET_ENABLED = os.getenv(
    "OPTION_SELL_PROFIT_TARGET_ENABLED", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_PROFIT_TARGET_PCT = max(
    0.0, abs(float(os.getenv("OPTION_SELL_PROFIT_TARGET_PCT", "25.0")))
)
OPTION_SELL_TRAILING_STOP_ENABLED = os.getenv(
    "OPTION_SELL_TRAILING_STOP_ENABLED", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_TRAILING_ACTIVATION_PCT = max(
    0.0, abs(float(os.getenv("OPTION_SELL_TRAILING_ACTIVATION_PCT", "10.0")))
)
OPTION_SELL_TRAILING_DRAWDOWN_PCT = max(
    0.0, abs(float(os.getenv("OPTION_SELL_TRAILING_DRAWDOWN_PCT", "5.0")))
)
OPTION_SELL_TRAILING_REQUIRE_NEGATIVE_VELOCITY = os.getenv(
    "OPTION_SELL_TRAILING_REQUIRE_NEGATIVE_VELOCITY", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_PEAK_PROFIT_CALLS_ONLY = os.getenv(
    "OPTION_SELL_PEAK_PROFIT_CALLS_ONLY", "false"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_STOP_LOSS_ENABLED = os.getenv(
    "OPTION_SELL_STOP_LOSS_ENABLED", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_STOP_LOSS_PCT = max(
    0.0, abs(float(os.getenv("OPTION_SELL_STOP_LOSS_PCT", "4.0")))
)
OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES = max(
    1, int(os.getenv("OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES", "3"))
)
OPTION_SELL_STOP_LOSS_REQUIRE_KINEMATICS = os.getenv(
    "OPTION_SELL_STOP_LOSS_REQUIRE_KINEMATICS", "true"
).strip().lower() in {"1", "true", "yes"}
OPTION_SELL_STOP_LOSS_HARD_PCT = max(
    0.0, abs(float(os.getenv("OPTION_SELL_STOP_LOSS_HARD_PCT", "0.0")))
)


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


def _parse_tv_bar_start(raw_time, now):
    if not isinstance(raw_time, str) or not raw_time.strip() or len(raw_time) > 100:
        raise ValueError(
            "bar_start must be a timezone-aware ISO-8601 minute-start string"
        )
    try:
        timestamp = as_utc(datetime.fromisoformat(raw_time.strip().replace("Z", "+00:00")))
        if timestamp != minute_utc(timestamp):
            raise ValueError()
        if now < timestamp:
            raise ValueError()
        if not regular_session(timestamp):
            raise ValueError()
        return timestamp
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            "bar_start must identify a completed regular-session minute"
        ) from None


def _validate_tradingview_item(
    item,
    allowed_symbols,
    now,
    *,
    default_timestamp=None,
    default_timeframe="1m",
    default_bar_closed=True,
):
    if not isinstance(item, dict):
        raise ValueError("Each symbol item must be a JSON object")

    raw_ticker = item.get("ticker") if "ticker" in item else item.get("symbol")
    if not isinstance(raw_ticker, str) or not raw_ticker.strip() or len(raw_ticker) > 100:
        raise ValueError("ticker must be a nonempty string of at most 100 characters")
    ticker = raw_ticker.strip().upper()
    symbol = ticker if ticker in allowed_symbols else ticker.rsplit(":", 1)[-1]
    if symbol not in allowed_symbols:
        return None, symbol  # Unconfigured symbol; caller records as skipped

    raw_price = item.get("price") if "price" in item else item.get("close")
    try:
        price = positive_price(raw_price)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{symbol}: price must be finite and positive") from None

    raw_rsi = item.get("rsi_value") if "rsi_value" in item else item.get("rsi")
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
                f"{symbol}: rsi_value must be between 0 and 100, or omitted"
            ) from None

    timeframe = item.get("timeframe", default_timeframe)
    if timeframe != "1m":
        raise ValueError(f'{symbol}: timeframe must be "1m"')

    bar_closed = item.get("bar_closed", default_bar_closed)
    if bar_closed is not True:
        raise ValueError(f"{symbol}: bar_closed must be JSON true")

    raw_time = item.get("bar_start")
    if raw_time is not None:
        timestamp = _parse_tv_bar_start(raw_time, now)
    elif default_timestamp is not None:
        timestamp = default_timestamp
    else:
        raise ValueError(f"{symbol}: missing bar_start timestamp")

    return (
        {
            "symbol": symbol,
            "ticker": ticker,
            "price": price,
            "rsi_value": rsi,
            "signal": item.get("signal"),
            "event_time": timestamp.isoformat(),
            "received_at": now.isoformat(),
            "timeframe": "1m",
            "bar_closed": True,
        },
        None,
    )


def validate_tradingview_payload(payload, allowed_symbols):
    """Parse and validate a consolidated 1-minute TradingView batch webhook payload.

    Expected batch structure:
      {
        "event": "rsi_batch",
        "timeframe": "1m",
        "bar_start": "2026-11-15T14:30:00Z",
        "bar_closed": true,
        "count": 21,
        "data": [
          {"ticker": "NASDAQ:NVDA", "rsi_value": 54.32, "price": 142.50, "signal": "neutral"},
          ...
        ]
      }
    Also accepts top-level "symbols" or "items" lists.
    """
    now = datetime.now(UTC)

    if isinstance(payload, list):
        raw_items = payload
        envelope = {}
    elif isinstance(payload, dict):
        envelope = payload
        raw_items = None
        for key in ("data", "symbols", "items"):
            if key in payload:
                raw_items = payload[key]
                break
        if raw_items is None:
            raise ValueError(
                'Batch payload must be an object containing a "data" (or "symbols") array of 1m symbol records'
            )
    else:
        raise ValueError("JSON payload must be a batch object")

    if not isinstance(raw_items, list) or len(raw_items) == 0:
        raise ValueError("Batch payload symbol list must be a non-empty JSON array")

    default_timeframe = envelope.get("timeframe", "1m")
    if default_timeframe != "1m":
        raise ValueError('Batch timeframe must be "1m"')

    default_bar_closed = envelope.get("bar_closed", True)
    if default_bar_closed is not True:
        raise ValueError("Batch bar_closed must be JSON true")

    default_timestamp = None
    if envelope.get("bar_start") is not None:
        default_timestamp = _parse_tv_bar_start(envelope.get("bar_start"), now)

    events = []
    skipped = []
    errors = []

    for idx, item in enumerate(raw_items):
        try:
            event, skipped_sym = _validate_tradingview_item(
                item,
                allowed_symbols,
                now,
                default_timestamp=default_timestamp,
                default_timeframe=default_timeframe,
                default_bar_closed=default_bar_closed,
            )
            if skipped_sym is not None:
                skipped.append(skipped_sym)
            elif event is not None:
                events.append(event)
        except ValueError as exc:
            errors.append(f"item[{idx}]: {exc}")

    if not events:
        if errors:
            raise ValueError("; ".join(errors[:5]))
        if skipped:
            raise ValueError(
                f"None of the submitted tickers match configured target_symbols: {', '.join(skipped[:10])}"
            )
        raise ValueError("No valid symbol records in batch payload")

    return {
        "events": events,
        "skipped": skipped,
        "errors": errors,
        "bar_start": default_timestamp.isoformat()
        if default_timestamp is not None
        else events[0]["event_time"],
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

    return TradingClient(keys["alpaca_key"], keys["alpaca_secret"], paper=ALPACA_PAPER)


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
        self.option_chain_scheduled = set()
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

            if not isinstance(payload, (dict, list)):
                return (
                    jsonify(
                        status="error",
                        message="JSON payload must be a batch object",
                    ),
                    400,
                )

            if WEBHOOK_TOKEN:
                supplied = request.headers.get("X-Webhook-Token")
                if supplied is None and isinstance(payload, dict):
                    supplied = payload.get("webhook_token", "")
                if not isinstance(supplied, str) or not hmac.compare_digest(
                    supplied.encode("utf-8"), WEBHOOK_TOKEN.encode("utf-8")
                ):
                    return jsonify(status="error", message="Unauthorized"), 401

            try:
                parsed = validate_tradingview_payload(
                    payload,
                    self.symbols,
                )
                events = parsed.get("events") or []
                skipped = parsed.get("skipped") or []
                errors = parsed.get("errors") or []

                if not events:
                    return (
                        jsonify(
                            status="error",
                            message="Batch payload yielded no valid symbol events",
                        ),
                        400,
                    )

                queued_symbols = []
                for event in events:
                    self.queue.put_nowait(event)
                    queued_symbols.append(event["symbol"])

                if skipped:
                    self.logger.warning(
                        "WEBHOOK_BATCH_SKIPPED symbols=%s reason=not_in_target_symbols",
                        ",".join(skipped),
                    )
                if errors:
                    self.logger.warning(
                        "WEBHOOK_BATCH_PARTIAL_ERRORS count=%d details=%s",
                        len(errors),
                        "; ".join(errors[:5]),
                    )

                self.logger.info(
                    "WEBHOOK_BATCH_RECEIVED queued=%d skipped=%d errors=%d bar_start=%s",
                    len(queued_symbols),
                    len(skipped),
                    len(errors),
                    parsed.get("bar_start"),
                )

                return (
                    jsonify(
                        status="queued",
                        provider=self.provider,
                        event="rsi_batch",
                        queued_count=len(queued_symbols),
                        received=queued_symbols,
                        skipped=skipped,
                        errors=errors,
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

            except Exception:
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
        dedup_key = (symbol, event.get("event_time"))
        if dedup_key in self.option_chain_scheduled:
            return
        if len(self.option_chain_scheduled) > 500:
            self.option_chain_scheduled.clear()
        self.option_chain_scheduled.add(dedup_key)
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
            self.coordinator.record_option_contract(
                symbol=symbol,
                contract=contract,
                signal=signal,
                event_time=event.get("event_time"),
                clean_payload=clean_payload,
                source=self.provider,
            )
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
        self.equity_count = 0
        self.option_count = 0

    @staticmethod
    def _num(obj, attr, default=0.0):
        """Safely extract a finite float attribute or dict key from an Alpaca model."""
        raw = obj.get(attr) if isinstance(obj, dict) else getattr(obj, attr, None)
        if raw is None or isinstance(raw, bool):
            return default
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return default
        return val if math.isfinite(val) else default

    @staticmethod
    def _is_option(position):
        """Return True if an Alpaca position represents an options contract."""
        raw_ac = (
            position.get("asset_class")
            if isinstance(position, dict)
            else getattr(position, "asset_class", None)
        )
        if raw_ac is not None:
            ac_val = getattr(raw_ac, "value", raw_ac)
            if "option" in str(ac_val).lower():
                return True
        sym = (
            position.get("symbol")
            if isinstance(position, dict)
            else getattr(position, "symbol", "")
        ) or ""
        import re

        return bool(re.match(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", str(sym).strip().upper()))

    def _format_option(self, position):
        """Format an Alpaca option position into key=value log fields."""
        import re

        sym = str(
            (
                position.get("symbol")
                if isinstance(position, dict)
                else getattr(position, "symbol", "?")
            )
            or "?"
        ).strip()
        raw_side = (
            position.get("side")
            if isinstance(position, dict)
            else getattr(position, "side", None)
        )
        side = str(getattr(raw_side, "value", raw_side) or "unknown").lower()

        occ = re.match(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", sym.upper())
        if occ:
            underlying, yy, mm, dd, cp, strike_raw = occ.groups()
            contract_meta = (
                f"underlying={underlying} "
                f"right={'CALL' if cp == 'C' else 'PUT'} "
                f"strike={int(strike_raw) / 1000.0:.2f} "
                f"exp=20{yy}-{mm}-{dd} "
            )
        else:
            contract_meta = ""

        return (
            f"symbol={sym} {contract_meta}side={side} "
            f"qty={self._num(position, 'qty'):.4g} "
            f"avg_entry={self._num(position, 'avg_entry_price'):.4f} "
            f"current_price={self._num(position, 'current_price'):.4f} "
            f"market_value={self._num(position, 'market_value'):.2f} "
            f"unrealized_pl={self._num(position, 'unrealized_pl'):.2f}"
        )

    def poll(self):
        """Fetch account + positions and log the snapshot (blocking)."""
        # --- NEW: Set active state flag ---
        self.last_status = "polling"
        account = self.client.get_account()
        positions = list(self.client.get_all_positions() or [])
        options = [p for p in positions if self._is_option(p)]
        equities = [p for p in positions if not self._is_option(p)]

        self.position_count = len(positions)
        self.equity_count = len(equities)
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


class AlpacaOptionBuyer:
    """Async task synced to 1-minute finalization that uses kinematics to buy
    an option contract returned in `contract` by collect_option_chain."""

    def __init__(self, keys, coordinator, client=None):
        self.provider = "OPTION_BUYER"
        self.credentials = dict(keys or {})
        self.coordinator = coordinator
        self.logger = make_logger("OPTION_BUYER", "ALPACA")
        self.client = (
            client if client is not None else build_option_chain_client(self.credentials)
        )
        self.last_status = "uninitialized"
        self.mode = "dry_run" if OPTION_BUY_DRY_RUN else ("paper" if ALPACA_PAPER else "live")
        self.last_eval_minute_utc = None
        self.last_order_utc = None
        self.last_order_contract = None
        self.last_order_symbol = None
        self.last_order_reason = None
        self.orders_submitted = 0
        self.dry_run_orders = 0
        self.last_buy_by_symbol = {}
        self.attempted_minutes = set()

    @staticmethod
    def _infer_occ_right(contract_symbol):
        import re

        m = re.match(
            r"^[A-Z]{1,6}\d{6}([CP])\d{8}$", str(contract_symbol or "").strip().upper()
        )
        if not m:
            return None
        return "CALL" if m.group(1) == "C" else "PUT"

    @staticmethod
    def _infer_occ_underlying(contract_symbol):
        import re

        m = re.match(
            r"^([A-Z]{1,6})\d{6}[CP]\d{8}$", str(contract_symbol or "").strip().upper()
        )
        return m.group(1) if m else None

    @classmethod
    def _extract_contract_details(cls, contract, clean_payload=None, desired_right=None):
        """Extract (contract_symbol, limit_price, inferred_right) strictly from the
        `contract` returned by get_pruned_option_chain / collect_option_chain."""
        if contract is None:
            return None, None, None
        desired_key = desired_right.lower() if desired_right else None
        candidates = [contract]

        limit_price = None
        for cand in candidates:
            if isinstance(cand, str) and cand.strip():
                sym = cand.strip().upper()
                return sym, limit_price, cls._infer_occ_right(sym)
            if isinstance(cand, dict):
                if desired_key and cand.get(desired_key) is not None:
                    nested = cand[desired_key]
                    sym, lp, right = cls._extract_contract_details(
                        nested, None, desired_right
                    )
                    if sym:
                        return sym, lp, right
                for key in ("symbol", "contract_symbol", "option_symbol", "contract"):
                    raw_sym = cand.get(key)
                    if isinstance(raw_sym, str) and raw_sym.strip():
                        sym = raw_sym.strip().upper()
                        for pk in ("limit_price", "ask_price", "ask", "close_price", "last_price"):
                            try:
                                pv = cand.get(pk)
                                if pv is not None and not isinstance(pv, bool):
                                    f = float(pv)
                                    if math.isfinite(f) and f > 0:
                                        limit_price = round(f, 2)
                                        break
                            except (TypeError, ValueError):
                                pass
                        return sym, limit_price, cls._infer_occ_right(sym)
            elif hasattr(cand, "symbol") and getattr(cand, "symbol", None):
                sym = str(getattr(cand, "symbol")).strip().upper()
                for pk in ("limit_price", "ask_price", "close_price"):
                    try:
                        pv = getattr(cand, pk, None)
                        if pv is not None and not isinstance(pv, bool):
                            f = float(pv)
                            if math.isfinite(f) and f > 0:
                                limit_price = round(f, 2)
                                break
                    except (TypeError, ValueError):
                        pass
                return sym, limit_price, cls._infer_occ_right(sym)
        return None, None, None

    @staticmethod
    def _evaluate_kinematics(entry):
        """Determine whether a finalized 1m snapshot triggers an option BUY.

        Mandatory rule:
        1. RSI must be present and strictly <= OPTION_CHAIN_RSI_LOW.
        2. Kinematic metrics must be favorable for buying (positive upward
           acceleration a > 0 with either an oversold decelerating bottom or
           upward velocity thrust, or a bullish zero-line crossover), and
           projected P_hat_{t+1} must align > 0 when enabled.

        Returns (qualifies: bool, desired_right: Optional[str], reason: str).
        """
        if not entry:
            return False, None, "no_entry"

        raw_rsi = entry.get("rsi")
        if raw_rsi is None or isinstance(raw_rsi, bool):
            return False, None, "rsi_unavailable"
        try:
            rsi = float(raw_rsi)
        except (TypeError, ValueError):
            return False, None, "rsi_invalid"
        if not math.isfinite(rsi):
            return False, None, "rsi_nonfinite"

        if rsi > OPTION_CHAIN_RSI_LOW:
            return (
                False,
                None,
                f"rsi_above_buy_threshold(rsi={rsi:.2f}>{OPTION_CHAIN_RSI_LOW:.2f})",
            )

        raw_vel = (
            entry.get("smoothed_velocity")
            if entry.get("smoothed_velocity") is not None
            else entry.get("price_velocity")
        )
        raw_acc = (
            entry.get("smoothed_acceleration")
            if entry.get("smoothed_acceleration") is not None
            else entry.get("price_acceleration")
        )
        if raw_vel is None or raw_acc is None:
            return False, None, "kinematics_unavailable"
        try:
            vel = float(raw_vel)
            acc = float(raw_acc)
        except (TypeError, ValueError):
            return False, None, "kinematics_invalid"
        if not (math.isfinite(vel) and math.isfinite(acc)):
            return False, None, "kinematics_nonfinite"

        forecast = str(entry.get("forecast_direction") or "").lower()
        crossover = str(entry.get("crossover_signal") or "none").lower()
        proj_delta = entry.get("projected_delta")

        reason = None
        if vel > 0 and acc > 0:
            reason = f"rsi={rsi:.2f}<=low&bullish_thrust(v={vel:+.4f},a={acc:+.4f})"
        elif vel < 0 and acc > 0:
            tag = (
                forecast
                if forecast in OPTION_BUY_ALLOWED_FORECASTS
                else "oversold_decel_bottom"
            )
            reason = f"rsi={rsi:.2f}<=low&{tag}(v={vel:+.4f},a={acc:+.4f})"
        elif crossover in {"leading_bullish", "confirmed_bullish"} and acc >= 0:
            reason = f"rsi={rsi:.2f}<=low&crossover={crossover}"
        elif forecast in OPTION_BUY_ALLOWED_FORECASTS and acc > 0:
            reason = f"rsi={rsi:.2f}<=low&forecast={forecast}"

        if reason is None:
            return (
                False,
                None,
                f"kinematics_unfavorable(rsi={rsi:.2f},v={vel:+.4f},a={acc:+.4f},cross={crossover})",
            )

        if OPTION_BUY_REQUIRE_PROJECTION_ALIGNMENT and proj_delta is not None:
            try:
                pdelta = float(proj_delta)
                if pdelta <= 0:
                    return (
                        False,
                        None,
                        f"projection_misaligned(CALL,delta={pdelta:+.4f})",
                    )
            except (TypeError, ValueError):
                pass

        return True, "CALL", reason

    def _select_kinematics_entry(self, symbol):
        """Return (provider, latest_entry) for symbol, preferring TRADINGVIEW if present."""
        tv_hist = self.coordinator.kinematics_history.get(("TRADINGVIEW", symbol))
        if tv_hist:
            return "TRADINGVIEW", tv_hist[-1]
        best_provider = None
        best_entry = None
        best_ts = ""
        for (provider, sym), hist in self.coordinator.kinematics_history.items():
            if sym != symbol or not hist:
                continue
            cand = hist[-1]
            ts = str(cand.get("timestamp") or "")
            if best_entry is None or ts >= best_ts:
                best_provider = provider
                best_entry = cand
                best_ts = ts
        return best_provider, best_entry

    def _submit_option_order(self, contract_symbol, qty, limit_price=None):
        """Place the option BUY order on Alpaca TradingClient (blocking)."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        if OPTION_BUY_ORDER_TYPE == "limit" and limit_price is not None and limit_price > 0:
            req = LimitOrderRequest(
                symbol=contract_symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
        else:
            req = MarketOrderRequest(
                symbol=contract_symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        return self.client.submit_order(order_data=req)

    def _write_fallback_option_chain(self, symbol, rsi, signal, clean_payload):
        """Persist fallback option-chain payload fetched directly by AlpacaOptionBuyer."""
        directory = LOG_ROOT / "optionchains"
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in symbol)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rsi_tag = f"{rsi:.2f}" if rsi is not None else "na"
        path = (
            directory
            / f"optionchain-OPTION_BUYER-{safe}-{signal.upper()}-rsi{rsi_tag}-{stamp}-{uuid4().hex[:6]}.json"
        )
        path.write_text(
            json.dumps(clean_payload, indent=2, default=str), encoding="utf-8"
        )
        return path

    async def evaluate_and_buy(self, target_minute=None):
        """Evaluate all configured symbols on the 1-minute boundary and submit
        qualifying option orders using contracts returned by collect_option_chain."""
        now = datetime.now(UTC)
        minute_key = (
            target_minute.isoformat()
            if hasattr(target_minute, "isoformat")
            else str(target_minute or now.replace(second=0, microsecond=0).isoformat())
        )
        self.last_eval_minute_utc = now.isoformat()
        self.last_status = "evaluating"

        if len(self.attempted_minutes) > 1000:
            self.attempted_minutes.clear()

        positions_loaded = False
        open_contracts = set()
        open_underlyings = set()
        open_option_count = 0

        async def ensure_open_positions():
            nonlocal positions_loaded, open_contracts, open_underlyings, open_option_count
            if positions_loaded:
                return
            positions = list(await asyncio.to_thread(self.client.get_all_positions) or [])
            for pos in positions:
                if AlpacaPositionMonitor._is_option(pos):
                    open_option_count += 1
                    psym = str(getattr(pos, "symbol", "") or "").strip().upper()
                    if psym:
                        open_contracts.add(psym)
                        und = self._infer_occ_underlying(psym)
                        if und:
                            open_underlyings.add(und)
            positions_loaded = True

        for symbol in self.coordinator.symbols:
            dedup_key = (symbol, minute_key)
            if dedup_key in self.attempted_minutes:
                continue

            provider, entry = self._select_kinematics_entry(symbol)
            if not entry:
                continue

            qualifies, desired_right, reason = self._evaluate_kinematics(entry)
            if not qualifies:
                continue

            self.attempted_minutes.add(dedup_key)

            last_buy = self.last_buy_by_symbol.get(symbol)
            if (
                last_buy is not None
                and OPTION_BUY_COOLDOWN_MINUTES > 0
                and (now - last_buy).total_seconds() < OPTION_BUY_COOLDOWN_MINUTES * 60
            ):
                self.logger.info(
                    "OPTION_BUY_SKIPPED symbol=%s reason=cooldown_active minute=%s",
                    symbol,
                    minute_key,
                )
                continue

            cached = self.coordinator.latest_option_contract(
                symbol, max_age_seconds=OPTION_BUY_MAX_AGE_SECONDS
            )
            contract = cached["contract"] if cached else None
            clean_payload = cached.get("clean_payload") if cached else None
            contract_source = cached.get("source") if cached else None

            if contract is None and OPTION_BUY_FETCH_IF_MISSING:
                if not self.credentials.get("alpaca_key") or not self.credentials.get(
                    "alpaca_secret"
                ):
                    self.logger.warning(
                        "OPTION_BUY_SKIPPED symbol=%s reason=missing_alpaca_credentials",
                        symbol,
                    )
                    continue
                try:
                    contract, clean_payload = await asyncio.to_thread(
                        get_pruned_option_chain,
                        symbol,
                        self.credentials["alpaca_key"],
                        self.credentials["alpaca_secret"],
                        self.client,
                    )
                    signal_label = "buy" if desired_right == "CALL" else "sell"
                    self.coordinator.record_option_contract(
                        symbol=symbol,
                        contract=contract,
                        signal=signal_label,
                        event_time=entry.get("timestamp"),
                        clean_payload=clean_payload,
                        source="ALPACA_OPTION_BUYER",
                    )
                    contract_source = "ALPACA_OPTION_BUYER"
                    await asyncio.to_thread(
                        self._write_fallback_option_chain,
                        symbol,
                        entry.get("rsi"),
                        signal_label,
                        clean_payload,
                    )
                except Exception as exc:
                    self.logger.error(
                        "OPTION_BUY_CHAIN_FAILED symbol=%s error_type=%s detail=%s",
                        symbol,
                        type(exc).__name__,
                        exc,
                    )
                    continue

            if contract is None:
                self.logger.info(
                    "OPTION_BUY_SKIPPED symbol=%s reason=no_contract_from_collect_option_chain trigger=%s",
                    symbol,
                    reason,
                )
                continue

            contract_symbol, limit_price, inferred_right = self._extract_contract_details(
                contract, clean_payload, desired_right=desired_right
            )
            if not contract_symbol:
                self.logger.warning(
                    "OPTION_BUY_SKIPPED symbol=%s reason=unparseable_contract raw_contract=%s",
                    symbol,
                    contract,
                )
                continue

            if (
                OPTION_BUY_REQUIRE_RIGHT_MATCH
                and inferred_right is not None
                and inferred_right != desired_right
            ):
                self.logger.info(
                    "OPTION_BUY_SKIPPED symbol=%s contract=%s reason=right_mismatch expected=%s got=%s",
                    symbol,
                    contract_symbol,
                    desired_right,
                    inferred_right,
                )
                continue

            try:
                await ensure_open_positions()
            except Exception as exc:
                self.logger.warning(
                    "OPTION_BUY_POSITIONS_CHECK_FAILED symbol=%s error_type=%s detail=%s",
                    symbol,
                    type(exc).__name__,
                    exc,
                )
                continue

            if open_option_count >= OPTION_BUY_MAX_OPEN_CONTRACTS:
                self.logger.info(
                    "OPTION_BUY_SKIPPED symbol=%s contract=%s reason=max_open_contracts count=%d limit=%d",
                    symbol,
                    contract_symbol,
                    open_option_count,
                    OPTION_BUY_MAX_OPEN_CONTRACTS,
                )
                continue

            if contract_symbol in open_contracts:
                self.logger.info(
                    "OPTION_BUY_SKIPPED symbol=%s contract=%s reason=contract_already_held",
                    symbol,
                    contract_symbol,
                )
                continue

            if not OPTION_BUY_ALLOW_DUPLICATE_UNDERLYING and symbol in open_underlyings:
                self.logger.info(
                    "OPTION_BUY_SKIPPED symbol=%s contract=%s reason=underlying_option_already_held",
                    symbol,
                    contract_symbol,
                )
                continue

            if OPTION_BUY_DRY_RUN:
                self.dry_run_orders += 1
                self.last_buy_by_symbol[symbol] = now
                self.last_order_utc = now.isoformat()
                self.last_order_contract = contract_symbol
                self.last_order_symbol = symbol
                self.last_order_reason = reason
                self.logger.info(
                    "OPTION_BUY_DRY_RUN provider=%s symbol=%s contract=%s qty=%d right=%s "
                    "contract_source=%s trigger=%s p_hat=%s rsi=%s",
                    provider,
                    symbol,
                    contract_symbol,
                    OPTION_BUY_QTY,
                    inferred_right or desired_right,
                    contract_source,
                    reason,
                    entry.get("projected_price_next"),
                    entry.get("rsi"),
                )
                logging.info(
                    "OPTION_BUY_DRY_RUN symbol=%s contract=%s qty=%d trigger=%s",
                    symbol,
                    contract_symbol,
                    OPTION_BUY_QTY,
                    reason,
                )
                continue

            try:
                order = await asyncio.to_thread(
                    self._submit_option_order,
                    contract_symbol,
                    OPTION_BUY_QTY,
                    limit_price,
                )
                order_id = getattr(order, "id", None)
                order_status = getattr(order, "status", "submitted")
                exec_at = datetime.now(UTC)
                raw_submitted = getattr(order, "submitted_at", None) or getattr(
                    order, "created_at", None
                )
                if isinstance(raw_submitted, datetime) and raw_submitted.tzinfo is not None:
                    exec_at = as_utc(raw_submitted)
                elif isinstance(raw_submitted, str) and raw_submitted.strip():
                    try:
                        parsed_sub = datetime.fromisoformat(
                            raw_submitted.strip().replace("Z", "+00:00")
                        )
                        if parsed_sub.tzinfo is not None:
                            exec_at = as_utc(parsed_sub)
                    except ValueError:
                        pass

                self.orders_submitted += 1
                open_option_count += 1
                open_contracts.add(contract_symbol)
                open_underlyings.add(symbol)
                self.last_buy_by_symbol[symbol] = exec_at
                self.last_order_utc = exec_at.isoformat()
                self.last_order_contract = contract_symbol
                self.last_order_symbol = symbol
                self.last_order_reason = reason
                self.coordinator.record_option_buy_event(
                    symbol=symbol,
                    executed_at=exec_at,
                    contract=contract_symbol,
                    qty=OPTION_BUY_QTY,
                    order_id=order_id,
                    trigger_minute=minute_key,
                    reason=reason,
                    rsi=entry.get("rsi"),
                    dry_run=False,
                )
                self.logger.info(
                    "OPTION_BUY_SUBMITTED order_id=%s status=%s provider=%s symbol=%s "
                    "contract=%s qty=%d right=%s contract_source=%s trigger=%s p_hat=%s rsi=%s",
                    order_id,
                    order_status,
                    provider,
                    symbol,
                    contract_symbol,
                    OPTION_BUY_QTY,
                    inferred_right or desired_right,
                    contract_source,
                    reason,
                    entry.get("projected_price_next"),
                    entry.get("rsi"),
                )
                logging.info(
                    "OPTION_BUY_SUBMITTED order_id=%s symbol=%s contract=%s qty=%d trigger=%s",
                    order_id,
                    symbol,
                    contract_symbol,
                    OPTION_BUY_QTY,
                    reason,
                )
            except Exception as exc:
                self.last_status = f"order_error: {type(exc).__name__}"
                self.logger.error(
                    "OPTION_BUY_FAILED symbol=%s contract=%s error_type=%s detail=%s",
                    symbol,
                    contract_symbol,
                    type(exc).__name__,
                    exc,
                )

        if not str(self.last_status).startswith("order_error"):
            self.last_status = "watching_dry_run" if OPTION_BUY_DRY_RUN else "watching_live"

    async def run(self):
        self.coordinator.attach_event_loop(asyncio.get_running_loop())
        if not OPTION_BUY_ENABLED:
            self.last_status = "disabled"
            self.logger.info("OPTION_BUYER_DISABLED reason=OPTION_BUY_ENABLED=false")
            return
        if self.client is None:
            self.last_status = "disabled"
            self.logger.warning(
                "OPTION_BUYER_DISABLED reason=missing_alpaca_credentials "
                "hint=provide api_key/secret_key in parms.ini CREDENTIALS or "
                "ALPACA_API_KEY/ALPACA_SECRET_KEY environment variables"
            )
            return

        self.last_status = "watching_dry_run" if OPTION_BUY_DRY_RUN else "watching_live"
        self.logger.info(
            "OPTION_BUYER_START mode=%s rsi_low<=%.2f qty=%d cooldown_min=%d max_open=%d allowed=%s",
            self.mode,
            OPTION_CHAIN_RSI_LOW,
            OPTION_BUY_QTY,
            OPTION_BUY_COOLDOWN_MINUTES,
            OPTION_BUY_MAX_OPEN_CONTRACTS,
            sorted(OPTION_BUY_ALLOWED_FORECASTS),
        )
        wake_event = self.coordinator.subscribe_minute_finalized()
        last_processed_minute = self.coordinator.last_finalized
        while True:
            try:
                now = datetime.now(UTC)
                next_wake = (
                    now.replace(second=0, microsecond=0)
                    + MINUTE
                    + self.coordinator.delay
                    + timedelta(seconds=1)
                )
                wait_sec = max(1.0, (next_wake - now).total_seconds())
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=wait_sec)
                    wake_event.clear()
                except asyncio.TimeoutError:
                    pass

                cur_finalized = self.coordinator.last_finalized
                if cur_finalized is not None and cur_finalized != last_processed_minute:
                    await self.evaluate_and_buy(cur_finalized)
                    last_processed_minute = cur_finalized
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_status = f"error: {type(exc).__name__}"
                self.logger.warning(
                    "OPTION_BUYER_ERROR error_type=%s detail=%s",
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(2.0)


class AlpacaOptionSeller:
    """Async task synced to 1-minute finalization that inspects all held positions
    in the Alpaca account and submits SELL orders when underlying RSI >= OPTION_CHAIN_RSI_HIGH
    and kinematic metrics are favorable for selling."""

    def __init__(
        self,
        keys,
        coordinator,
        client=None,
        option_data_client=None,
        stock_data_client=None,
    ):
        self.provider = "OPTION_SELLER"
        self.credentials = dict(keys or {})
        self.coordinator = coordinator
        self.logger = make_logger("OPTION_SELLER", "ALPACA")
        if client is not None:
            self.client = client
        elif self.credentials.get("alpaca_key") and self.credentials.get("alpaca_secret"):
            from alpaca.trading.client import TradingClient

            self.client = TradingClient(
                self.credentials["alpaca_key"],
                self.credentials["alpaca_secret"],
                paper=OPTION_SELL_PAPER,
            )
        else:
            self.client = None
        self.option_data_client = option_data_client
        self.stock_data_client = stock_data_client
        self.last_status = "uninitialized"
        self.mode = (
            "dry_run" if OPTION_SELL_DRY_RUN else ("paper" if OPTION_SELL_PAPER else "live")
        )
        self.last_eval_minute_utc = None
        self.last_order_utc = None
        self.last_order_contract = None
        self.last_order_symbol = None
        self.last_order_reason = None
        self.last_positions_checked = 0
        self.orders_submitted = 0
        self.dry_run_orders = 0
        self.profit_target_orders = 0
        self.trailing_stop_orders = 0
        self.stop_loss_orders = 0
        self.last_sell_by_symbol = {}
        self.peak_profit_state_by_symbol = {}
        self.stop_loss_state_by_symbol = {}
        self.attempted_minutes = set()

    @staticmethod
    def _extract_underlying(pos, pos_sym, is_option):
        raw_und = (
            pos.get("underlying_symbol")
            if isinstance(pos, dict)
            else getattr(pos, "underlying_symbol", None)
        )
        if isinstance(raw_und, str) and raw_und.strip():
            return raw_und.strip().upper()
        if is_option:
            inferred = AlpacaOptionBuyer._infer_occ_underlying(pos_sym)
            if inferred:
                return inferred
        return pos_sym

    @staticmethod
    def _evaluate_sell_kinematics(entry):
        """Determine whether a finalized 1m snapshot triggers a SELL decision.

        Mandatory rule:
        1. RSI must be present and strictly >= OPTION_CHAIN_RSI_HIGH.
        2. Kinematic metrics must be favorable for selling (downward acceleration
           a < 0 with either an overbought decelerating top v > 0 or bearish thrust
           v < 0, or a bearish zero-line crossover), and projected P_hat_{t+1} must
           align < 0 when enabled.

        Returns (qualifies: bool, reason: str).
        """
        if not entry:
            return False, "no_entry"

        raw_rsi = entry.get("rsi")
        if raw_rsi is None or isinstance(raw_rsi, bool):
            return False, "rsi_unavailable"
        try:
            rsi = float(raw_rsi)
        except (TypeError, ValueError):
            return False, "rsi_invalid"
        if not math.isfinite(rsi):
            return False, "rsi_nonfinite"

        if rsi < OPTION_CHAIN_RSI_HIGH:
            return (
                False,
                f"rsi_below_sell_threshold(rsi={rsi:.2f}<{OPTION_CHAIN_RSI_HIGH:.2f})",
            )

        raw_vel = (
            entry.get("smoothed_velocity")
            if entry.get("smoothed_velocity") is not None
            else entry.get("price_velocity")
        )
        raw_acc = (
            entry.get("smoothed_acceleration")
            if entry.get("smoothed_acceleration") is not None
            else entry.get("price_acceleration")
        )
        if raw_vel is None or raw_acc is None:
            return False, "kinematics_unavailable"
        try:
            vel = float(raw_vel)
            acc = float(raw_acc)
        except (TypeError, ValueError):
            return False, "kinematics_invalid"
        if not (math.isfinite(vel) and math.isfinite(acc)):
            return False, "kinematics_nonfinite"

        forecast = str(entry.get("forecast_direction") or "").lower()
        crossover = str(entry.get("crossover_signal") or "none").lower()
        proj_delta = entry.get("projected_delta")

        reason = None
        if vel < 0 and acc < 0:
            reason = f"rsi={rsi:.2f}>=high&bearish_thrust(v={vel:+.4f},a={acc:+.4f})"
        elif vel > 0 and acc < 0:
            tag = (
                forecast
                if forecast in OPTION_SELL_ALLOWED_FORECASTS
                else "overbought_decel_top"
            )
            reason = f"rsi={rsi:.2f}>=high&{tag}(v={vel:+.4f},a={acc:+.4f})"
        elif crossover in {"leading_bearish", "confirmed_bearish"} and acc <= 0:
            reason = f"rsi={rsi:.2f}>=high&crossover={crossover}"
        elif forecast in OPTION_SELL_ALLOWED_FORECASTS and acc < 0:
            reason = f"rsi={rsi:.2f}>=high&forecast={forecast}"

        if reason is None:
            return (
                False,
                f"kinematics_unfavorable(rsi={rsi:.2f},v={vel:+.4f},a={acc:+.4f},cross={crossover})",
            )

        if OPTION_SELL_REQUIRE_PROJECTION_ALIGNMENT and proj_delta is not None:
            try:
                pdelta = float(proj_delta)
                if pdelta >= 0:
                    return (
                        False,
                        f"projection_misaligned(SELL,delta={pdelta:+.4f})",
                    )
            except (TypeError, ValueError):
                pass

        return True, reason

    @staticmethod
    def _position_pnl_pct(pos):
        """Return position unrealized return as a percentage (e.g., -4.5 for -4.5%)."""
        plpc = AlpacaPositionMonitor._num(pos, "unrealized_plpc", None)
        if plpc is not None:
            return plpc * 100.0
        avg_entry = AlpacaPositionMonitor._num(pos, "avg_entry_price", None)
        cur_price = AlpacaPositionMonitor._num(pos, "current_price", None)
        if (
            avg_entry is not None
            and cur_price is not None
            and avg_entry > 0
            and cur_price > 0
        ):
            return ((cur_price - avg_entry) / avg_entry) * 100.0
        upl = AlpacaPositionMonitor._num(pos, "unrealized_pl", None)
        cost_basis = AlpacaPositionMonitor._num(pos, "cost_basis", None)
        if upl is not None and cost_basis is not None and cost_basis > 0:
            return (upl / cost_basis) * 100.0
        return None

    @staticmethod
    def _evaluate_stop_loss_kinematics(entry):
        """Check whether underlying kinematics show downward price pressure.

        Unlike profit-taking sells, stop-loss exits do NOT require RSI >=
        OPTION_CHAIN_RSI_HIGH, but still confirm downward price pressure
        (negative velocity with flat/negative acceleration or negative next-step
        projection, avoiding panic-selling into an active upward rebound).
        """
        if not entry:
            return False, "kinematics_unavailable"

        raw_vel = (
            entry.get("smoothed_velocity")
            if entry.get("smoothed_velocity") is not None
            else entry.get("price_velocity")
        )
        raw_acc = (
            entry.get("smoothed_acceleration")
            if entry.get("smoothed_acceleration") is not None
            else entry.get("price_acceleration")
        )
        if raw_vel is None or raw_acc is None:
            return False, "kinematics_unavailable"
        try:
            vel = float(raw_vel)
            acc = float(raw_acc)
        except (TypeError, ValueError):
            return False, "kinematics_invalid"
        if not (math.isfinite(vel) and math.isfinite(acc)):
            return False, "kinematics_nonfinite"

        forecast = str(entry.get("forecast_direction") or "").lower()
        crossover = str(entry.get("crossover_signal") or "none").lower()
        raw_proj = entry.get("projected_delta")
        proj_delta = None
        if raw_proj is not None and not isinstance(raw_proj, bool):
            try:
                cand = float(raw_proj)
                if math.isfinite(cand):
                    proj_delta = cand
            except (TypeError, ValueError):
                pass
        eff_proj = proj_delta if proj_delta is not None else (vel + 0.5 * acc)

        if vel < 0 and acc <= 0:
            return True, f"downward_pressure(v={vel:+.4f},a={acc:+.4f})"
        if vel < 0 and eff_proj < 0:
            return True, f"downward_bleed(v={vel:+.4f},a={acc:+.4f},p_delta={eff_proj:+.4f})"
        if vel <= 0 and acc < 0 and (
            crossover in {"leading_bearish", "confirmed_bearish"}
            or forecast in OPTION_SELL_ALLOWED_FORECASTS
        ):
            return True, f"bearish_rollover(v={vel:+.4f},a={acc:+.4f},cross={crossover})"

        return (
            False,
            f"kinematics_recovering_or_flat(v={vel:+.4f},a={acc:+.4f},p_delta={eff_proj:+.4f})",
        )

    def _update_stop_loss_streak(self, pos_sym, pnl_pct, minute_key, minute_dt):
        """Track consecutive 1-minute evaluations where position loss > threshold%."""
        st = self.stop_loss_state_by_symbol.setdefault(
            pos_sym,
            {
                "streak": 0,
                "last_minute_key": None,
                "last_minute_dt": None,
                "last_pnl_pct": None,
            },
        )
        if st["last_minute_key"] == minute_key:
            return st["streak"]

        threshold = OPTION_SELL_STOP_LOSS_PCT
        is_breached = (
            threshold > 0
            and pnl_pct is not None
            and math.isfinite(pnl_pct)
            and pnl_pct < -threshold
        )
        if is_breached:
            prev_dt = st.get("last_minute_dt")
            if prev_dt is not None and minute_dt is not None:
                gap_sec = (minute_dt - prev_dt).total_seconds()
                streak = st["streak"] + 1 if 0 < gap_sec <= 95 else 1
            else:
                streak = st["streak"] + 1
        else:
            streak = 0

        st["streak"] = streak
        st["last_minute_key"] = minute_key
        st["last_minute_dt"] = minute_dt
        st["last_pnl_pct"] = pnl_pct
        return streak

    @staticmethod
    def _is_call_contract(pos_sym, is_option):
        if not is_option:
            return False
        return AlpacaOptionBuyer._infer_occ_right(pos_sym) == "CALL"

    def _update_peak_profit_state(self, pos_sym, pnl_pct, now_iso):
        """Update session high-water mark return and compute drawdown from peak (%)."""
        if pnl_pct is None or not math.isfinite(pnl_pct):
            st = self.peak_profit_state_by_symbol.get(pos_sym)
            if not st:
                return None, 0.0
            return st.get("peak_pnl_pct"), 0.0

        st = self.peak_profit_state_by_symbol.get(pos_sym)
        if st is None:
            st = {
                "peak_pnl_pct": pnl_pct,
                "peak_at_utc": now_iso,
                "last_pnl_pct": pnl_pct,
            }
            self.peak_profit_state_by_symbol[pos_sym] = st
            return pnl_pct, 0.0

        if pnl_pct > st["peak_pnl_pct"]:
            st["peak_pnl_pct"] = pnl_pct
            st["peak_at_utc"] = now_iso
        st["last_pnl_pct"] = pnl_pct

        peak_pnl = st["peak_pnl_pct"]
        peak_factor = 1.0 + (peak_pnl / 100.0)
        cur_factor = 1.0 + (pnl_pct / 100.0)
        if peak_factor > 0 and cur_factor < peak_factor:
            drawdown_pct = ((peak_factor - cur_factor) / peak_factor) * 100.0
        else:
            drawdown_pct = 0.0
        st["drawdown_pct"] = drawdown_pct
        return peak_pnl, drawdown_pct

    @staticmethod
    def _evaluate_trailing_stop_velocity(entry):
        """Confirm downward underlying velocity (v < 0) for trailing-stop exit."""
        if not entry:
            return False, None, "velocity_unavailable"
        raw_vel = (
            entry.get("smoothed_velocity")
            if entry.get("smoothed_velocity") is not None
            else entry.get("price_velocity")
        )
        if raw_vel is None or isinstance(raw_vel, bool):
            return False, None, "velocity_unavailable"
        try:
            vel = float(raw_vel)
        except (TypeError, ValueError):
            return False, None, "velocity_invalid"
        if not math.isfinite(vel):
            return False, None, "velocity_nonfinite"
        if vel < 0:
            return True, vel, f"v={vel:+.4f}<0"
        return False, vel, f"v={vel:+.4f}>=0"

    def _select_kinematics_entry(self, symbol):
        """Return (provider, latest_entry) for symbol, preferring TRADINGVIEW if present."""
        tv_hist = self.coordinator.kinematics_history.get(("TRADINGVIEW", symbol))
        if tv_hist:
            return "TRADINGVIEW", tv_hist[-1]
        best_provider = None
        best_entry = None
        best_ts = ""
        for (provider, sym), hist in self.coordinator.kinematics_history.items():
            if sym != symbol or not hist:
                continue
            cand = hist[-1]
            ts = str(cand.get("timestamp") or "")
            if best_entry is None or ts >= best_ts:
                best_provider = provider
                best_entry = cand
                best_ts = ts
        return best_provider, best_entry

    def _fetch_live_bid(self, pos_sym, is_option):
        """Fetch the latest live bid price for an option contract or equity symbol."""
        key = self.credentials.get("alpaca_key")
        secret = self.credentials.get("alpaca_secret")
        try:
            if is_option:
                if self.option_data_client is None:
                    if not key or not secret:
                        return None
                    try:
                        from alpaca.data.historical.option import OptionHistoricalDataClient
                    except ImportError:
                        from alpaca.data.historical import OptionHistoricalDataClient
                    self.option_data_client = OptionHistoricalDataClient(key, secret)
                from alpaca.data.requests import OptionLatestQuoteRequest

                quotes = self.option_data_client.get_option_latest_quote(
                    OptionLatestQuoteRequest(symbol_or_symbols=pos_sym)
                )
            else:
                if self.stock_data_client is None:
                    if not key or not secret:
                        return None
                    from alpaca.data.historical import StockHistoricalDataClient

                    self.stock_data_client = StockHistoricalDataClient(key, secret)
                from alpaca.data.requests import StockLatestQuoteRequest

                quotes = self.stock_data_client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=pos_sym)
                )
        except Exception as exc:
            self.logger.warning(
                "OPTION_SELL_QUOTE_FAILED symbol=%s is_option=%s error_type=%s detail=%s",
                pos_sym,
                is_option,
                type(exc).__name__,
                exc,
            )
            return None

        quote = quotes.get(pos_sym) if isinstance(quotes, dict) else None
        if quote is None and isinstance(quotes, dict) and quotes:
            quote = next(iter(quotes.values()), None)
        bid = AlpacaPositionMonitor._num(quote, "bid_price", None) if quote is not None else None
        if bid is None and quote is not None:
            bid = AlpacaPositionMonitor._num(quote, "bid", None)
        return bid if (bid is not None and math.isfinite(bid) and bid > 0) else None

    async def _resolve_sell_order_params(self, pos_sym, cur_price, is_option, trigger_kind):
        """Compute (order_type, live_bid, limit_price) with marketable discount below
        min(live_bid, current_price), and deeper discount / optional market override
        for stop-loss exits."""
        if OPTION_SELL_ORDER_TYPE != "limit":
            return "market", None, None
        if trigger_kind == "stop_loss" and OPTION_SELL_STOP_LOSS_FORCE_MARKET:
            return "market", None, None

        live_bid = await asyncio.to_thread(self._fetch_live_bid, pos_sym, is_option)
        cur_val = cur_price if (cur_price is not None and math.isfinite(cur_price) and cur_price > 0) else None
        candidates = [p for p in (live_bid, cur_val) if p is not None]
        if not candidates:
            return "market", live_bid, None

        ref_price = min(candidates)
        discount_pct = (
            OPTION_SELL_STOP_LOSS_LIMIT_DISCOUNT_PCT
            if trigger_kind == "stop_loss"
            else OPTION_SELL_LIMIT_DISCOUNT_PCT
        )
        discounted = ref_price * (1.0 - (discount_pct / 100.0))
        limit_price = round(max(0.01, discounted), 2)
        if discount_pct > 0 and limit_price >= round(ref_price, 2) and ref_price > 0.01:
            limit_price = round(max(0.01, ref_price - 0.01), 2)
        return "limit", live_bid, limit_price

    def _submit_sell_order(self, contract_or_symbol, qty, limit_price=None, order_type=None):
        """Place a SELL order on Alpaca TradingClient (blocking)."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        eff_type = (order_type or OPTION_SELL_ORDER_TYPE).strip().lower()
        if eff_type == "limit" and limit_price is not None and limit_price > 0:
            req = LimitOrderRequest(
                symbol=contract_or_symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
        else:
            req = MarketOrderRequest(
                symbol=contract_or_symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
        return self.client.submit_order(order_data=req)

    async def evaluate_and_sell(self, target_minute=None):
        """Inspect all positions in the Alpaca account and submit SELL orders when
        underlying RSI >= OPTION_CHAIN_RSI_HIGH + bearish kinematics OR when the
        stop-loss trigger fires (down > stop_loss_pct for consecutive minutes +
        downward kinematic price pressure)."""
        now = datetime.now(UTC)
        if isinstance(target_minute, datetime):
            target_min_dt = as_utc(target_minute).replace(second=0, microsecond=0)
        elif isinstance(target_minute, str) and target_minute.strip():
            try:
                parsed_tm = datetime.fromisoformat(
                    target_minute.strip().replace("Z", "+00:00")
                )
                target_min_dt = (
                    as_utc(parsed_tm).replace(second=0, microsecond=0)
                    if parsed_tm.tzinfo is not None
                    else now.replace(second=0, microsecond=0)
                )
            except ValueError:
                target_min_dt = now.replace(second=0, microsecond=0)
        else:
            target_min_dt = now.replace(second=0, microsecond=0)

        minute_key = (
            target_minute.isoformat()
            if hasattr(target_minute, "isoformat")
            else str(target_minute or target_min_dt.isoformat())
        )
        self.last_eval_minute_utc = now.isoformat()
        self.last_status = "evaluating"

        if len(self.attempted_minutes) > 1000:
            self.attempted_minutes.clear()

        try:
            positions = list(await asyncio.to_thread(self.client.get_all_positions) or [])
        except Exception as exc:
            self.last_status = f"positions_error: {type(exc).__name__}"
            self.logger.warning(
                "OPTION_SELL_POSITIONS_FAILED error_type=%s detail=%s",
                type(exc).__name__,
                exc,
            )
            return

        self.last_positions_checked = len(positions)
        if not positions:
            self.stop_loss_state_by_symbol.clear()
            self.peak_profit_state_by_symbol.clear()
            self.last_status = "watching_dry_run" if OPTION_SELL_DRY_RUN else "watching_live"
            return

        seen_long_symbols = set()
        for pos in positions:
            pos_sym = str(
                (pos.get("symbol") if isinstance(pos, dict) else getattr(pos, "symbol", ""))
                or ""
            ).strip().upper()
            if not pos_sym:
                continue

            raw_qty = AlpacaPositionMonitor._num(pos, "qty", 0.0)
            raw_side = (
                pos.get("side") if isinstance(pos, dict) else getattr(pos, "side", None)
            )
            side = str(getattr(raw_side, "value", raw_side) or "long").lower()
            if raw_qty <= 0 or side == "short":
                continue

            is_option = AlpacaPositionMonitor._is_option(pos)
            if OPTION_SELL_OPTIONS_ONLY and not is_option:
                continue

            seen_long_symbols.add(pos_sym)
            is_call = self._is_call_contract(pos_sym, is_option)
            underlying = self._extract_underlying(pos, pos_sym, is_option)
            pnl_pct = self._position_pnl_pct(pos)
            peak_pnl, drawdown_pct = self._update_peak_profit_state(
                pos_sym, pnl_pct, now.isoformat()
            )
            loss_streak = 0
            if OPTION_SELL_STOP_LOSS_ENABLED:
                loss_streak = self._update_stop_loss_streak(
                    pos_sym, pnl_pct, minute_key, target_min_dt
                )

            dedup_key = (pos_sym, minute_key)
            if dedup_key in self.attempted_minutes:
                continue

            qty_avail = AlpacaPositionMonitor._num(pos, "qty_available", None)
            if qty_avail is not None and qty_avail <= 0:
                continue

            provider, entry = self._select_kinematics_entry(underlying)

            qualifies = False
            reason = None
            trigger_kind = "take_profit"
            peak_eligible = (not OPTION_SELL_PEAK_PROFIT_CALLS_ONLY) or is_call

            # 1. Fixed Profit Target (lock in spikes before RSI >= OPTION_CHAIN_RSI_HIGH)
            if (
                peak_eligible
                and OPTION_SELL_PROFIT_TARGET_ENABLED
                and OPTION_SELL_PROFIT_TARGET_PCT > 0
                and pnl_pct is not None
                and math.isfinite(pnl_pct)
                and pnl_pct >= OPTION_SELL_PROFIT_TARGET_PCT
            ):
                qualifies = True
                trigger_kind = "profit_target"
                reason = (
                    f"profit_target(pnl={pnl_pct:+.2f}%>="
                    f"{OPTION_SELL_PROFIT_TARGET_PCT:.2f}%)"
                )

            # 2. Overbought RSI + Bearish Kinematics
            if not qualifies and entry:
                qualifies, reason = self._evaluate_sell_kinematics(entry)
                if qualifies:
                    trigger_kind = "take_profit"

            # 3. Session High-Water Mark Trailing Stop (exit when drawdown > X% with v < 0)
            if (
                not qualifies
                and peak_eligible
                and OPTION_SELL_TRAILING_STOP_ENABLED
                and OPTION_SELL_TRAILING_DRAWDOWN_PCT > 0
                and peak_pnl is not None
                and peak_pnl >= OPTION_SELL_TRAILING_ACTIVATION_PCT
                and drawdown_pct > OPTION_SELL_TRAILING_DRAWDOWN_PCT
            ):
                if OPTION_SELL_TRAILING_REQUIRE_NEGATIVE_VELOCITY:
                    ts_vel_ok, _, ts_vel_reason = self._evaluate_trailing_stop_velocity(entry)
                else:
                    ts_vel_ok, _, ts_vel_reason = True, None, "velocity_bypassed"

                if ts_vel_ok:
                    qualifies = True
                    trigger_kind = "trailing_stop"
                    reason = (
                        f"trailing_stop(peak={peak_pnl:+.2f}%,pnl={pnl_pct:+.2f}%,"
                        f"dd={drawdown_pct:.2f}%>{OPTION_SELL_TRAILING_DRAWDOWN_PCT:.2f}%)&"
                        f"{ts_vel_reason}"
                    )
                else:
                    self.logger.info(
                        "OPTION_SELL_TRAILING_HOLD symbol=%s underlying=%s "
                        "peak=%.2f%% pnl=%.2f%% dd=%.2f%% reason=%s",
                        pos_sym,
                        underlying,
                        peak_pnl,
                        pnl_pct if pnl_pct is not None else 0.0,
                        drawdown_pct,
                        ts_vel_reason,
                    )

            # 4. Stop-Loss Protection (hard circuit breaker OR consecutive minutes + downward pressure)
            if not qualifies and OPTION_SELL_STOP_LOSS_ENABLED:
                hard_pct = OPTION_SELL_STOP_LOSS_HARD_PCT
                if (
                    hard_pct > 0
                    and pnl_pct is not None
                    and math.isfinite(pnl_pct)
                    and pnl_pct < -hard_pct
                ):
                    qualifies = True
                    trigger_kind = "stop_loss"
                    reason = (
                        f"hard_stop_loss(pnl={pnl_pct:.2f}%<-{hard_pct:.2f}%,"
                        f"streak={loss_streak})"
                    )
                elif (
                    OPTION_SELL_STOP_LOSS_PCT > 0
                    and loss_streak >= OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES
                ):
                    if OPTION_SELL_STOP_LOSS_REQUIRE_KINEMATICS:
                        sl_kin_ok, sl_kin_reason = self._evaluate_stop_loss_kinematics(entry)
                    else:
                        sl_kin_ok, sl_kin_reason = True, "kinematics_bypassed"

                    if sl_kin_ok:
                        qualifies = True
                        trigger_kind = "stop_loss"
                        reason = (
                            f"stop_loss(pnl={pnl_pct:.2f}%<-{OPTION_SELL_STOP_LOSS_PCT:.2f}%,"
                            f"streak={loss_streak}/{OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES})&"
                            f"{sl_kin_reason}"
                        )
                    else:
                        self.logger.info(
                            "OPTION_SELL_STOP_LOSS_HOLD symbol=%s underlying=%s "
                            "pnl_pct=%.2f%% streak=%d/%d reason=%s",
                            pos_sym,
                            underlying,
                            pnl_pct if pnl_pct is not None else 0.0,
                            loss_streak,
                            OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES,
                            sl_kin_reason,
                        )
                elif loss_streak > 0:
                    self.logger.info(
                        "OPTION_SELL_STOP_LOSS_WATCH symbol=%s underlying=%s "
                        "pnl_pct=%.2f%% threshold=-%.2f%% streak=%d/%d",
                        pos_sym,
                        underlying,
                        pnl_pct if pnl_pct is not None else 0.0,
                        OPTION_SELL_STOP_LOSS_PCT,
                        loss_streak,
                        OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES,
                    )

            if not qualifies:
                continue

            self.attempted_minutes.add(dedup_key)

            last_sell = self.last_sell_by_symbol.get(pos_sym)
            if (
                last_sell is not None
                and OPTION_SELL_COOLDOWN_MINUTES > 0
                and (now - last_sell).total_seconds() < OPTION_SELL_COOLDOWN_MINUTES * 60
            ):
                self.logger.info(
                    "OPTION_SELL_SKIPPED symbol=%s underlying=%s reason=cooldown_active minute=%s",
                    pos_sym,
                    underlying,
                    minute_key,
                )
                continue

            target_qty = raw_qty if OPTION_SELL_CLOSE_FULL_POSITION else min(
                float(OPTION_SELL_QTY), raw_qty
            )
            if target_qty <= 0:
                continue
            if is_option or abs(target_qty - round(target_qty)) < 1e-9:
                order_qty = max(1, int(round(target_qty)))
            else:
                order_qty = round(target_qty, 6)

            cur_price = AlpacaPositionMonitor._num(pos, "current_price", 0.0)
            eff_order_type, live_bid, limit_price = await self._resolve_sell_order_params(
                pos_sym, cur_price, is_option, trigger_kind
            )

            if OPTION_SELL_DRY_RUN:
                self.dry_run_orders += 1
                if trigger_kind == "profit_target":
                    self.profit_target_orders += 1
                elif trigger_kind == "trailing_stop":
                    self.trailing_stop_orders += 1
                elif trigger_kind == "stop_loss":
                    self.stop_loss_orders += 1
                self.stop_loss_state_by_symbol.pop(pos_sym, None)
                self.peak_profit_state_by_symbol.pop(pos_sym, None)
                self.last_sell_by_symbol[pos_sym] = now
                self.last_order_utc = now.isoformat()
                self.last_order_contract = pos_sym
                self.last_order_symbol = underlying
                self.last_order_reason = reason
                self.logger.info(
                    "OPTION_SELL_DRY_RUN kind=%s order_type=%s live_bid=%s limit_price=%s "
                    "provider=%s underlying=%s symbol=%s is_option=%s qty=%s trigger=%s p_hat=%s rsi=%s",
                    trigger_kind,
                    eff_order_type,
                    live_bid,
                    limit_price,
                    provider,
                    underlying,
                    pos_sym,
                    is_option,
                    order_qty,
                    reason,
                    entry.get("projected_price_next") if entry else None,
                    entry.get("rsi") if entry else None,
                )
                logging.info(
                    "OPTION_SELL_DRY_RUN kind=%s order_type=%s limit_price=%s "
                    "underlying=%s symbol=%s qty=%s trigger=%s",
                    trigger_kind,
                    eff_order_type,
                    limit_price,
                    underlying,
                    pos_sym,
                    order_qty,
                    reason,
                )
                continue

            try:
                order = await asyncio.to_thread(
                    self._submit_sell_order,
                    pos_sym,
                    order_qty,
                    limit_price,
                    eff_order_type,
                )
                order_id = getattr(order, "id", None)
                order_status = getattr(order, "status", "submitted")
                exec_at = datetime.now(UTC)
                raw_submitted = getattr(order, "submitted_at", None) or getattr(
                    order, "created_at", None
                )
                if isinstance(raw_submitted, datetime) and raw_submitted.tzinfo is not None:
                    exec_at = as_utc(raw_submitted)
                elif isinstance(raw_submitted, str) and raw_submitted.strip():
                    try:
                        parsed_sub = datetime.fromisoformat(
                            raw_submitted.strip().replace("Z", "+00:00")
                        )
                        if parsed_sub.tzinfo is not None:
                            exec_at = as_utc(parsed_sub)
                    except ValueError:
                        pass

                self.orders_submitted += 1
                if trigger_kind == "profit_target":
                    self.profit_target_orders += 1
                elif trigger_kind == "trailing_stop":
                    self.trailing_stop_orders += 1
                elif trigger_kind == "stop_loss":
                    self.stop_loss_orders += 1
                self.stop_loss_state_by_symbol.pop(pos_sym, None)
                self.peak_profit_state_by_symbol.pop(pos_sym, None)
                self.last_sell_by_symbol[pos_sym] = exec_at
                self.last_order_utc = exec_at.isoformat()
                self.last_order_contract = pos_sym
                self.last_order_symbol = underlying
                self.last_order_reason = reason
                self.coordinator.record_option_sell_event(
                    symbol=underlying,
                    executed_at=exec_at,
                    contract=pos_sym,
                    qty=order_qty,
                    order_id=order_id,
                    trigger_minute=minute_key,
                    reason=reason,
                    rsi=entry.get("rsi") if entry else None,
                    dry_run=False,
                )
                self.logger.info(
                    "OPTION_SELL_SUBMITTED kind=%s order_type=%s live_bid=%s limit_price=%s "
                    "order_id=%s status=%s provider=%s underlying=%s symbol=%s is_option=%s "
                    "qty=%s trigger=%s p_hat=%s rsi=%s",
                    trigger_kind,
                    eff_order_type,
                    live_bid,
                    limit_price,
                    order_id,
                    order_status,
                    provider,
                    underlying,
                    pos_sym,
                    is_option,
                    order_qty,
                    reason,
                    entry.get("projected_price_next") if entry else None,
                    entry.get("rsi") if entry else None,
                )
                logging.info(
                    "OPTION_SELL_SUBMITTED kind=%s order_type=%s limit_price=%s order_id=%s "
                    "underlying=%s symbol=%s qty=%s trigger=%s",
                    trigger_kind,
                    eff_order_type,
                    limit_price,
                    order_id,
                    underlying,
                    pos_sym,
                    order_qty,
                    reason,
                )
            except Exception as exc:
                self.last_status = f"order_error: {type(exc).__name__}"
                self.logger.error(
                    "OPTION_SELL_FAILED underlying=%s symbol=%s error_type=%s detail=%s",
                    underlying,
                    pos_sym,
                    type(exc).__name__,
                    exc,
                )

        for stale_sym in list(self.stop_loss_state_by_symbol.keys()):
            if stale_sym not in seen_long_symbols:
                self.stop_loss_state_by_symbol.pop(stale_sym, None)
        for stale_sym in list(self.peak_profit_state_by_symbol.keys()):
            if stale_sym not in seen_long_symbols:
                self.peak_profit_state_by_symbol.pop(stale_sym, None)

        if not str(self.last_status).startswith(("order_error", "positions_error")):
            self.last_status = "watching_dry_run" if OPTION_SELL_DRY_RUN else "watching_live"

    async def run(self):
        self.coordinator.attach_event_loop(asyncio.get_running_loop())
        if not OPTION_SELL_ENABLED:
            self.last_status = "disabled"
            self.logger.info("OPTION_SELLER_DISABLED reason=OPTION_SELL_ENABLED=false")
            return
        if self.client is None:
            self.last_status = "disabled"
            self.logger.warning(
                "OPTION_SELLER_DISABLED reason=missing_alpaca_credentials "
                "hint=provide api_key/secret_key in parms.ini CREDENTIALS or "
                "ALPACA_API_KEY/ALPACA_SECRET_KEY environment variables"
            )
            return

        self.last_status = "watching_dry_run" if OPTION_SELL_DRY_RUN else "watching_live"
        self.logger.info(
            "OPTION_SELLER_START mode=%s rsi_high>=%.2f qty=%d close_full=%s "
            "options_only=%s cooldown_min=%d order_type=%s limit_disc=%.2f%% sl_disc=%.2f%% "
            "pt_enabled=%s pt=%.1f%% ts_enabled=%s ts_act=%.1f%% ts_dd=%.1f%% ts_vel=%s "
            "allowed=%s sl_enabled=%s sl_pct=%.2f%% sl_mins=%d sl_kin=%s sl_hard=%.2f%%",
            self.mode,
            OPTION_CHAIN_RSI_HIGH,
            OPTION_SELL_QTY,
            OPTION_SELL_CLOSE_FULL_POSITION,
            OPTION_SELL_OPTIONS_ONLY,
            OPTION_SELL_COOLDOWN_MINUTES,
            OPTION_SELL_ORDER_TYPE,
            OPTION_SELL_LIMIT_DISCOUNT_PCT,
            OPTION_SELL_STOP_LOSS_LIMIT_DISCOUNT_PCT,
            OPTION_SELL_PROFIT_TARGET_ENABLED,
            OPTION_SELL_PROFIT_TARGET_PCT,
            OPTION_SELL_TRAILING_STOP_ENABLED,
            OPTION_SELL_TRAILING_ACTIVATION_PCT,
            OPTION_SELL_TRAILING_DRAWDOWN_PCT,
            OPTION_SELL_TRAILING_REQUIRE_NEGATIVE_VELOCITY,
            sorted(OPTION_SELL_ALLOWED_FORECASTS),
            OPTION_SELL_STOP_LOSS_ENABLED,
            OPTION_SELL_STOP_LOSS_PCT,
            OPTION_SELL_STOP_LOSS_CONSECUTIVE_MINUTES,
            OPTION_SELL_STOP_LOSS_REQUIRE_KINEMATICS,
            OPTION_SELL_STOP_LOSS_HARD_PCT,
        )
        wake_event = self.coordinator.subscribe_minute_finalized()
        last_processed_minute = self.coordinator.last_finalized
        while True:
            try:
                now = datetime.now(UTC)
                next_wake = (
                    now.replace(second=0, microsecond=0)
                    + MINUTE
                    + self.coordinator.delay
                    + timedelta(seconds=1)
                )
                wait_sec = max(1.0, (next_wake - now).total_seconds())
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=wait_sec)
                    wake_event.clear()
                except asyncio.TimeoutError:
                    pass

                cur_finalized = self.coordinator.last_finalized
                if cur_finalized is not None and cur_finalized != last_processed_minute:
                    await self.evaluate_and_sell(cur_finalized)
                    last_processed_minute = cur_finalized
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_status = f"error: {type(exc).__name__}"
                self.logger.warning(
                    "OPTION_SELLER_ERROR error_type=%s detail=%s",
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(2.0)


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
                            <span>&nearr;</span> Decelerating Bullish
                        </span>
                        <span class="text-[10px] mono px-2 py-0.5 rounded bg-cyan-900/40 text-cyan-200">v &gt; 0, a &le; 0</span>
                    </div>
                    <p class="text-[10px] text-cyan-400/80 mt-0.5">Bullish exhaustion &bull; upward velocity slowing &bull; Overbought (&ge;70) = Bearish Reversal watch</p>
                    <div id="quadDecBullish" class="mt-2.5 flex flex-wrap gap-1.5 min-h-[28px] items-center">
                        <span class="text-xs text-slate-500 italic">No instruments in this phase</span>
                    </div>
                </div>

                <!-- Quadrant I: Accelerating Bullish (Top Right) -->
                <div class="bg-emerald-950/20 border border-emerald-800/40 rounded-xl p-3">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-emerald-300 flex items-center gap-1.5">
                            <span>&uarr;</span> Accelerating Bullish
                        </span>
                        <span class="text-[10px] mono px-2 py-0.5 rounded bg-emerald-900/40 text-emerald-200">v &gt; 0, a &gt; 0</span>
                    </div>
                    <p class="text-[10px] text-emerald-400/80 mt-0.5">Aggressive momentum expansion &bull; Neutral RSI (40-60) = Strong Bullish Continuation</p>
                    <div id="quadAccBullish" class="mt-2.5 flex flex-wrap gap-1.5 min-h-[28px] items-center">
                        <span class="text-xs text-slate-500 italic">No instruments in this phase</span>
                    </div>
                </div>

                <!-- Quadrant III: Accelerating Bearish (Bottom Left) -->
                <div class="bg-rose-950/20 border border-rose-800/40 rounded-xl p-3">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-rose-300 flex items-center gap-1.5">
                            <span>&darr;</span> Accelerating Bearish
                        </span>
                        <span class="text-[10px] mono px-2 py-0.5 rounded bg-rose-900/40 text-rose-200">v &lt; 0, a &lt; 0</span>
                    </div>
                    <p class="text-[10px] text-rose-400/80 mt-0.5">Aggressive selling impulse &bull; Neutral RSI (40-60) = Strong Bearish Continuation</p>
                    <div id="quadAccBearish" class="mt-2.5 flex flex-wrap gap-1.5 min-h-[28px] items-center">
                        <span class="text-xs text-slate-500 italic">No instruments in this phase</span>
                    </div>
                </div>

                <!-- Quadrant IV: Decelerating Bearish (Bottom Right) -->
                <div class="bg-amber-950/20 border border-amber-800/40 rounded-xl p-3">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-amber-300 flex items-center gap-1.5">
                            <span>&searr;</span> Decelerating Bearish
                        </span>
                        <span class="text-[10px] mono px-2 py-0.5 rounded bg-amber-900/40 text-amber-200">v &lt; 0, a &ge; 0</span>
                    </div>
                    <p class="text-[10px] text-amber-400/80 mt-0.5">Downside absorption &bull; Oversold (&le;30) = Bullish Reversal watch</p>
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
                            <th class="px-4 py-2.5 text-right">Projected P̂<sub>t+1</sub></th>
                            <th class="px-4 py-2.5 text-right">Price Velocity (v)</th>
                            <th class="px-4 py-2.5 text-right">Price Accel (a)</th>
                            <th class="px-4 py-2.5 min-w-[130px]">RSI (14)</th>
                            <th class="px-4 py-2.5 text-right">RSI Vel / Acc</th>
                            <th class="px-4 py-2.5">Forecast Matrix Signal</th>
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
                    <!-- Kinematic Projection & 3-Panel Synchronized Crossover Chart -->
                    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
                        <div class="bg-slate-950/70 border border-slate-800/80 rounded-xl p-4 space-y-3">
                            <div class="flex items-center justify-between">
                                <span class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Next-Step Projection</span>
                                <span id="detailSmoothTag" class="text-[10px] mono text-indigo-300">HMA(14) Synced</span>
                            </div>
                            <div class="flex items-baseline justify-between">
                                <div>
                                    <div class="text-[11px] text-slate-400">P̂<sub>t+1</sub> = P<sub>t</sub> + v<sub>t</sub> + &frac12;a<sub>t</sub></div>
                                    <div id="detailProjectedPrice" class="text-2xl font-extrabold mono text-cyan-300 mt-0.5">--</div>
                                </div>
                                <div class="text-right">
                                    <div class="text-[11px] text-slate-400">Expected &Delta;</div>
                                    <div id="detailProjectedDelta" class="text-sm font-bold mono text-slate-300 mt-0.5">--</div>
                                </div>
                            </div>
                            <div class="pt-2.5 border-t border-slate-800/80 space-y-1.5 text-[11px]">
                                <div class="flex justify-between">
                                    <span class="text-slate-400">RSI Boundary Zone:</span>
                                    <span id="detailRsiZone" class="mono font-semibold text-slate-200">--</span>
                                </div>
                                <div class="flex justify-between">
                                    <span class="text-slate-400">Crossover Event:</span>
                                    <span id="detailCrossoverSignal" class="mono font-semibold text-slate-200">none</span>
                                </div>
                                <div class="flex justify-between">
                                    <span class="text-slate-400">Forecast Action:</span>
                                    <span id="detailTradingAction" class="font-semibold text-amber-300 text-right">--</span>
                                </div>
                            </div>
                        </div>

                        <div class="lg:col-span-2 bg-slate-950/70 border border-slate-800/80 rounded-xl p-4">
                            <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
                                <span class="text-[11px] font-bold uppercase tracking-wider text-slate-300">3-Panel Synchronized Kinematic Crossover (Price · v/a · RSI)</span>
                                <span class="flex flex-wrap items-center gap-3 text-[10px] text-slate-400 mono">
                                    <span>a crosses 0 = Leading · v crosses 0 = Confirmed</span>
                                    <span class="inline-flex items-center gap-1 text-red-400">
                                        <svg viewBox="0 0 16 16" class="w-3 h-3 inline-block"><polygon points="8,1 10.1,5.6 15,6.2 11.4,9.6 12.3,14.5 8,12.1 3.7,14.5 4.6,9.6 1,6.2 5.9,5.6" fill="#ef4444" stroke="#020617" stroke-width="0.8"/></svg>
                                        Executed Buy
                                    </span>
                                </span>
                            </div>
                            <div id="detailCrossoverChart" class="w-full min-h-[210px] flex items-center justify-center text-xs text-slate-500">
                                Collecting multi-bar history...
                            </div>
                        </div>
                    </div>

                    <!-- Provider Comparison Table for Selected Symbol -->
                    <div class="overflow-x-auto rounded-xl border border-slate-800/80 bg-slate-950/50">
                        <table class="w-full text-left text-xs">
                            <thead class="bg-slate-950/80 text-slate-400 font-semibold border-b border-slate-800">
                                <tr>
                                    <th class="px-3.5 py-2">Provider Feed</th>
                                    <th class="px-3.5 py-2 text-right">Close ($)</th>
                                    <th class="px-3.5 py-2 text-right">P̂<sub>t+1</sub> ($)</th>
                                    <th class="px-3.5 py-2 text-right">Price Vel (v)</th>
                                    <th class="px-3.5 py-2 text-right">Price Acc (a)</th>
                                    <th class="px-3.5 py-2 text-right">RSI (14)</th>
                                    <th class="px-3.5 py-2 text-right">RSI Vel / Acc</th>
                                    <th class="px-3.5 py-2">Forecast Action</th>
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

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Option Buyer (1m Sync)</span>
                            <span id="optionBuyerStatus" class="mono font-semibold text-slate-200">not_loaded</span>
                        </div>

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Last Option Order</span>
                            <span id="optionBuyerLast" class="mono text-[11px] text-cyan-300 truncate max-w-[160px]">--</span>
                        </div>

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Option Seller (1m Sync)</span>
                            <span id="optionSellerStatus" class="mono font-semibold text-slate-200">not_loaded</span>
                        </div>

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Last Sell Order</span>
                            <span id="optionSellerLast" class="mono text-[11px] text-amber-300 truncate max-w-[160px]">--</span>
                        </div>

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Stop Loss Watches</span>
                            <span id="optionSellerStopLoss" class="mono text-[11px] text-rose-300 truncate max-w-[160px]">0 active</span>
                        </div>

                        <div class="flex justify-between items-center p-2 rounded-xl bg-slate-950/50 border border-slate-800/80">
                            <span class="text-slate-400">Peak Profit Guard</span>
                            <span id="optionSellerPeakProfit" class="mono text-[11px] text-emerald-300 truncate max-w-[160px]">0 tracked</span>
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
                icon: '↑',
                pillColor: 'text-emerald-400',
                desc: 'Strong buyer thrust & momentum expansion'
            },
            decelerating_bullish: {
                label: 'Decelerating Bullish',
                badgeClass: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/30',
                icon: '↗',
                pillColor: 'text-cyan-400',
                desc: 'Upward momentum waning / consolidation'
            },
            accelerating_bearish: {
                label: 'Accelerating Bearish',
                badgeClass: 'bg-rose-500/10 text-rose-400 border-rose-500/30',
                icon: '↓',
                pillColor: 'text-rose-400',
                desc: 'Aggressive selling impulse accelerating'
            },
            decelerating_bearish: {
                label: 'Decelerating Bearish',
                badgeClass: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
                icon: '↘',
                pillColor: 'text-amber-400',
                desc: 'Downside absorption / selling deceleration'
            },
            neutral: {
                label: 'Neutral',
                badgeClass: 'bg-slate-700/30 text-slate-300 border-slate-700',
                icon: '•',
                pillColor: 'text-slate-400',
                desc: 'Equilibrium / no directional acceleration'
            }
        };

        const FORECAST_CONFIG = {
            bearish_reversal: { label: 'Bearish Reversal', cls: 'bg-rose-500/15 text-rose-300 border-rose-500/40' },
            bullish_reversal: { label: 'Bullish Reversal', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40' },
            bullish_continuation: { label: 'Bullish Continuation', cls: 'bg-teal-500/15 text-teal-300 border-teal-500/40' },
            bearish_continuation: { label: 'Bearish Continuation', cls: 'bg-orange-500/15 text-orange-300 border-orange-500/40' },
            bullish_momentum: { label: 'Bullish Momentum', cls: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20' },
            bearish_momentum: { label: 'Bearish Momentum', cls: 'bg-rose-500/10 text-rose-300 border-rose-500/20' },
            bullish_decelerating: { label: 'Bullish Decel', cls: 'bg-cyan-500/10 text-cyan-300 border-cyan-500/20' },
            bearish_decelerating: { label: 'Bearish Decel', cls: 'bg-amber-500/10 text-amber-300 border-amber-500/20' },
            neutral: { label: 'Neutral / Hold', cls: 'bg-slate-800 text-slate-400 border-slate-700' }
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
            if (m.option_buyer) {
                const ob = m.option_buyer;
                document.getElementById('optionBuyerStatus').textContent = `${ob.status || 'not_loaded'} (${ob.orders_submitted ?? 0}/${ob.dry_run_orders ?? 0})`;
                document.getElementById('optionBuyerLast').textContent = ob.last_order_contract || '--';
            }
            if (m.option_seller) {
                const os = m.option_seller;
                document.getElementById('optionSellerStatus').textContent = `${os.status || 'not_loaded'} (${os.orders_submitted ?? 0}/${os.dry_run_orders ?? 0})`;
                document.getElementById('optionSellerLast').textContent = os.last_order_contract || '--';
                const slEl = document.getElementById('optionSellerStopLoss');
                if (slEl) {
                    const watches = Object.entries(os.stop_loss_watches || {});
                    const slOrders = os.stop_loss_orders ?? 0;
                    if (watches.length > 0) {
                        slEl.textContent = watches.map(([sym, w]) => `${sym} ${w.streak ?? 0}m (${safeNum(w.pnl_pct) !== null ? safeNum(w.pnl_pct).toFixed(1) : '--'}%)`).join(', ');
                    } else {
                        slEl.textContent = slOrders > 0 ? `0 active (${slOrders} triggered)` : '0 active';
                    }
                }
                const ppEl = document.getElementById('optionSellerPeakProfit');
                if (ppEl) {
                    const peaks = Object.entries(os.peak_profit_watches || {});
                    const ptOrders = (os.profit_target_orders ?? 0) + (os.trailing_stop_orders ?? 0);
                    if (peaks.length > 0) {
                        ppEl.textContent = peaks.map(([sym, w]) => `${sym} pk ${safeNum(w.peak_pnl_pct) !== null ? safeNum(w.peak_pnl_pct).toFixed(1) : '--'}% (dd ${safeNum(w.drawdown_pct) !== null ? safeNum(w.drawdown_pct).toFixed(1) : '0.0'}%)`).join(', ');
                    } else {
                        ppEl.textContent = ptOrders > 0 ? `0 tracked (${ptOrders} exited)` : '0 tracked';
                    }
                }
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
                const fConf = FORECAST_CONFIG[item.forecast_direction] || FORECAST_CONFIG.neutral;
                const priceFormatted = fmtPrice(item.close);
                const projPrice = safeNum(item.projected_price_next);
                const projPct = safeNum(item.projected_delta_pct);
                const projColor = projPct !== null ? (projPct > 0 ? 'text-emerald-300' : (projPct < 0 ? 'text-rose-300' : 'text-slate-300')) : 'text-slate-400';

                // Price velocity (prefer smoothed if present, else raw)
                const pv = safeNum(item.smoothed_velocity !== null && item.smoothed_velocity !== undefined ? item.smoothed_velocity : item.price_velocity);
                const pvPct = safeNum(item.price_velocity_pct);
                let pVelText = '--';
                let pVelColor = 'text-slate-400';
                if (pv !== null) {
                    pVelText = `${fmtNum(pv, 2, '+')}${pvPct !== null ? ` (${fmtPct(pvPct, 2, '+')})` : ''}`;
                    pVelColor = pv > 0 ? 'text-emerald-400' : (pv < 0 ? 'text-rose-400' : 'text-slate-400');
                }

                // Price Accel
                const pa = safeNum(item.smoothed_acceleration !== null && item.smoothed_acceleration !== undefined ? item.smoothed_acceleration : item.price_acceleration);
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
                const ra = safeNum(item.rsi_acceleration);
                const rvColor = rv !== null ? (rv > 0 ? 'text-emerald-400' : (rv < 0 ? 'text-rose-400' : 'text-slate-400')) : 'text-slate-400';

                const isExpanded = expandedSymbols.has(item.symbol);
                const isSelected = selectedSymbol === item.symbol;

                let rowHtml = `
                    <tr class="hover:bg-slate-800/40 transition cursor-pointer ${isSelected ? 'bg-indigo-950/20' : ''}" onclick="selectAndInspectSymbol('${item.symbol}')">
                        <td class="px-4 py-2.5">
                            <div class="flex items-center gap-2">
                                <span class="font-bold text-white mono text-xs">${item.symbol}</span>
                                <button onclick="event.stopPropagation(); toggleExpandSymbol('${item.symbol}')" class="px-1.5 py-0.5 rounded text-[10px] mono bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-slate-700 transition" title="Toggle provider feed breakdown">
                                    ${item.providers_count} feeds ${isExpanded ? '▲' : '▼'}
                                </button>
                            </div>
                        </td>
                        <td class="px-4 py-2.5 text-right mono font-semibold text-slate-200">${priceFormatted}</td>
                        <td class="px-4 py-2.5 text-right mono font-semibold ${projColor}">
                            ${projPrice !== null ? fmtPrice(projPrice) : '--'}
                            ${projPct !== null ? `<div class="text-[10px] opacity-80">${fmtPct(projPct, 2, '+')}</div>` : ''}
                        </td>
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
                        <td class="px-4 py-2.5 text-right mono">
                            <div class="font-semibold ${rvColor}">${rv !== null ? fmtNum(rv, 2, '+') + '/m' : '--'}</div>
                            <div class="text-[10px] text-slate-400">${ra !== null ? fmtNum(ra, 2, '+') + '/m²' : '--'}</div>
                        </td>
                        <td class="px-4 py-2.5">
                            <div class="space-y-0.5">
                                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold border ${fConf.cls}">
                                    ${fConf.label}
                                </span>
                                <div class="text-[10px] text-slate-400">${rConf.icon} ${rConf.label}</div>
                            </div>
                        </td>
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
                        const sFConf = FORECAST_CONFIG[sub.forecast_direction] || FORECAST_CONFIG.neutral;
                        const sPv = safeNum(sub.price_velocity);
                        const sRv = safeNum(sub.rsi_velocity);
                        const sRa = safeNum(sub.rsi_acceleration);
                        rowHtml += `
                            <tr class="bg-slate-950/60 text-slate-400 text-[11px]">
                                <td class="px-4 py-1.5 pl-8 mono text-cyan-300">&bull; ${sub.provider}</td>
                                <td class="px-4 py-1.5 text-right mono">${fmtPrice(sub.close)}</td>
                                <td class="px-4 py-1.5 text-right mono">${fmtPrice(sub.projected_price_next)}</td>
                                <td class="px-4 py-1.5 text-right mono ${sPv > 0 ? 'text-emerald-400' : (sPv < 0 ? 'text-rose-400' : '')}">${fmtNum(sPv, 2, '+')}</td>
                                <td class="px-4 py-1.5 text-right mono">${fmtNum(sub.price_acceleration, 2, '+')}</td>
                                <td class="px-4 py-1.5 mono">${fmtNum(sub.rsi, 1)}</td>
                                <td class="px-4 py-1.5 text-right mono">${fmtNum(sRv, 2, '+')} / ${fmtNum(sRa, 2, '+')}</td>
                                <td class="px-4 py-1.5"><span class="text-[10px] px-1.5 py-0.5 rounded border ${sFConf.cls}">${sFConf.label}</span></td>
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
                const fConf = FORECAST_CONFIG[inst.forecast_direction] || FORECAST_CONFIG.neutral;
                const pv = safeNum(inst.price_velocity);
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
                        <td class="px-4 py-2.5 text-right mono text-cyan-300">${fmtPrice(inst.projected_price_next)}</td>
                        <td class="px-4 py-2.5 text-right mono font-semibold ${pv > 0 ? 'text-emerald-400' : (pv < 0 ? 'text-rose-400' : 'text-slate-400')}">${fmtNum(pv, 2, '+')}</td>
                        <td class="px-4 py-2.5 text-right mono text-slate-300">${fmtNum(inst.price_acceleration, 2, '+')}</td>
                        <td class="px-4 py-2.5 mono font-semibold text-slate-200">${fmtNum(rsi, 1)}</td>
                        <td class="px-4 py-2.5 text-right mono">${fmtNum(rv, 2, '+')} / ${fmtNum(ra, 2, '+')}</td>
                        <td class="px-4 py-2.5">
                            <span class="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-semibold border ${fConf.cls}">${fConf.label}</span>
                        </td>
                        <td class="px-4 py-2.5 text-center">
                            <button onclick="event.stopPropagation(); toggleInjectModal('${inst.symbol}')" class="px-2 py-0.5 bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white rounded border border-slate-700 text-[10px] mono transition">
                                Inject
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        function formatMinuteTick(point, idx) {
            const raw = point && (point.timestamp || point.minute);
            if (!raw) return `t${idx}`;
            const d = new Date(raw);
            if (!Number.isNaN(d.getTime())) {
                return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
            }
            const m = String(raw).match(/(\d{2}:\d{2})/);
            return m ? m[1] : `t${idx}`;
        }

        function escSvg(str) {
            return String(str ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }

        function svgStarPoints(cx, cy, outerR = 6.5, innerR = 2.8) {
            const pts = [];
            for (let i = 0; i < 10; i++) {
                const r = i % 2 === 0 ? outerR : innerR;
                const angle = (-Math.PI / 2) + (i * Math.PI / 5);
                pts.push(`${(cx + r * Math.cos(angle)).toFixed(1)},${(cy + r * Math.sin(angle)).toFixed(1)}`);
            }
            return pts.join(' ');
        }

        function renderCrossoverChart(container, points, projectedNext, buyEvents = []) {
            if (!container) return;
            if (!points || points.length < 2) {
                container.innerHTML = '<div class="text-xs text-slate-500 py-8">Need at least 2 closed minute bars to draw synchronized Price / Velocity / Acceleration / RSI panels.</div>';
                return;
            }
            const W = 640, H = 232, padL = 44, padR = 26;
            const plotW = W - padL - padR;
            const n = points.length;
            const xAt = (i) => padL + (i / Math.max(1, n)) * plotW;

            const prices = points.map(p => safeNum(p.close)).filter(v => v !== null);
            const smooths = points.map(p => safeNum(p.smoothed_close)).filter(v => v !== null);
            const allP = [...prices, ...smooths];
            if (projectedNext !== null) allP.push(projectedNext);
            const minP = Math.min(...allP), maxP = Math.max(...allP);
            const spanP = Math.max(0.01, maxP - minP);
            const yP = (val) => 12 + (1 - (val - minP) / spanP) * 52;

            const vels = points.map(p => safeNum(p.smoothed_velocity ?? p.price_velocity) ?? 0);
            const accs = points.map(p => safeNum(p.smoothed_acceleration ?? p.price_acceleration) ?? 0);
            const maxVA = Math.max(0.05, ...vels.map(Math.abs), ...accs.map(Math.abs));
            const yVA = (val) => 112 - (val / maxVA) * 28;

            const yR = (val) => 162 + (1 - Math.min(100, Math.max(0, val)) / 100) * 48;

            const poly = (vals, yFn, offset = 0) => vals.map((v, i) => v === null ? null : `${xAt(i + offset).toFixed(1)},${yFn(v).toFixed(1)}`).filter(Boolean).join(' ');

            const closePts = poly(points.map(p => safeNum(p.close)), yP);
            const smoothPts = poly(points.map(p => safeNum(p.smoothed_close)), yP);
            const velPts = poly(vels, yVA);
            const accPts = poly(accs, yVA);
            const rsiPts = poly(points.map(p => safeNum(p.rsi)), yR);

            const lastX = xAt(n - 1);
            const lastY = yP(points[n - 1].close);
            const nextX = xAt(n);
            const nextY = projectedNext !== null ? yP(projectedNext) : lastY;

            // Subtle 1-minute vertical boundaries drawn BEHIND all series
            const labelStep = n <= 8 ? 1 : (n <= 15 ? 3 : 4);
            let minuteGrid = '';
            let lastLabelX = -999;
            const latestBarX = xAt(n - 1);
            for (let i = 0; i < n; i++) {
                const xNum = xAt(i);
                const x = xNum.toFixed(1);
                const tickStr = formatMinuteTick(points[i], i);
                const minNum = parseInt((tickStr.split(':')[1] || ''), 10);
                const isMajor5m = !Number.isNaN(minNum) && minNum % 5 === 0;
                const isLatest = i === n - 1;
                const strokeColor = isMajor5m ? '#64748b' : '#475569';
                const strokeOpacity = isMajor5m ? '0.80' : '0.55';
                const dash = isMajor5m ? '3,2' : '2,3';

                minuteGrid += `<line x1="${x}" y1="10" x2="${x}" y2="212" stroke="${strokeColor}" stroke-opacity="${strokeOpacity}" stroke-dasharray="${dash}" stroke-width="1"><title>Bar ${tickStr}</title></line>`;

                const fitsSpacing = (xNum - lastLabelX >= 38) && (isLatest || (latestBarX - xNum >= 38));
                const isCandidate = i === 0 || isMajor5m || (i % labelStep === 0) || isLatest;
                if (isCandidate && fitsSpacing) {
                    const textColor = isLatest ? '#cbd5e1' : '#94a3b8';
                    minuteGrid += `<text x="${x}" y="225" text-anchor="middle" fill="${textColor}" font-size="8.5" font-family="monospace">${tickStr}</text>`;
                    lastLabelX = xNum;
                }
            }
            if (projectedNext !== null) {
                const px = xAt(n).toFixed(1);
                minuteGrid += `<line x1="${px}" y1="10" x2="${px}" y2="212" stroke="#22d3ee" stroke-opacity="0.45" stroke-dasharray="2,3" stroke-width="1" />`;
                minuteGrid += `<text x="${px}" y="225" text-anchor="middle" fill="#22d3ee" fill-opacity="0.90" font-size="8.5" font-family="monospace">+1m</text>`;
            }

            const crossMarkers = points.map((p, i) => {
                if (p.crossover_signal === 'leading_bearish' || p.crossover_signal === 'leading_bullish') {
                    return `<circle cx="${xAt(i).toFixed(1)}" cy="${yVA(accs[i]).toFixed(1)}" r="3.5" fill="#fb923c" stroke="#0f172a" stroke-width="1"><title>Leading Signal (${p.crossover_signal})</title></circle>`;
                }
                if (p.crossover_signal === 'confirmed_bearish' || p.crossover_signal === 'confirmed_bullish') {
                    return `<circle cx="${xAt(i).toFixed(1)}" cy="${yVA(vels[i]).toFixed(1)}" r="3.5" fill="#38bdf8" stroke="#0f172a" stroke-width="1"><title>Velocity Crossover (${p.crossover_signal})</title></circle>`;
                }
                return '';
            }).join('');

            // Executed option buy markers: red 5-point star pinned to exact UTC minute column
            const toMinIdx = (raw) => {
                if (!raw) return null;
                const ms = Date.parse(raw);
                return Number.isNaN(ms) ? null : Math.floor(ms / 60000);
            };
            const barMinIndices = points.map(p => toMinIdx(p.timestamp || p.minute));
            const lastBarMinIdx = barMinIndices[n - 1];
            const buyMarkers = (Array.isArray(buyEvents) ? buyEvents : [])
                .filter(ev => ev && !ev.dry_run)
                .map(ev => {
                    const evMin = toMinIdx(ev.minute_utc || ev.executed_at_utc);
                    if (evMin === null) return '';
                    let bx = null;
                    let by = null;
                    const barIdx = barMinIndices.indexOf(evMin);
                    if (barIdx >= 0) {
                        bx = xAt(barIdx);
                        const bp = safeNum(points[barIdx].close);
                        by = bp !== null ? yP(bp) : lastY;
                    } else if (lastBarMinIdx !== null && evMin === lastBarMinIdx + 1) {
                        bx = nextX;
                        by = nextY;
                    } else {
                        return '';
                    }
                    const execDate = ev.executed_at_utc ? new Date(ev.executed_at_utc) : null;
                    const execLabel = (execDate && !Number.isNaN(execDate.getTime()))
                        ? execDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
                        : (ev.minute_utc || '');
                    const tip = `BUY EXECUTED: ${ev.contract || ev.symbol || ''} x${ev.qty || 1} @ ${execLabel}${ev.order_id ? ' · Order ' + ev.order_id : ''}`;
                    const starPts = svgStarPoints(bx, by, 6.5, 2.8);
                    return `<g><line x1="${bx.toFixed(1)}" y1="10" x2="${bx.toFixed(1)}" y2="212" stroke="#ef4444" stroke-opacity="0.40" stroke-dasharray="2,2" stroke-width="1.2" /><polygon points="${starPts}" fill="#ef4444" stroke="#020617" stroke-width="1"><title>${escSvg(tip)}</title></polygon></g>`;
                })
                .join('');

            container.innerHTML = `
                <svg viewBox="0 0 ${W} ${H}" class="w-full h-auto overflow-visible">
                    ${minuteGrid}

                    <text x="4" y="20" fill="#94a3b8" font-size="9" font-family="monospace">Price</text>
                    <text x="4" y="102" fill="#94a3b8" font-size="9" font-family="monospace">v / a</text>
                    <text x="4" y="176" fill="#94a3b8" font-size="9" font-family="monospace">RSI</text>

                    <line x1="${padL}" y1="74" x2="${W - padR}" y2="74" stroke="#1e293b" stroke-width="1" />
                    <line x1="${padL}" y1="112" x2="${W - padR}" y2="112" stroke="#334155" stroke-dasharray="3,3" stroke-width="1" />
                    <line x1="${padL}" y1="150" x2="${W - padR}" y2="150" stroke="#1e293b" stroke-width="1" />

                    <rect x="${padL}" y="${yR(100).toFixed(1)}" width="${plotW}" height="${(yR(70) - yR(100)).toFixed(1)}" fill="#f43f5e" fill-opacity="0.08" />
                    <rect x="${padL}" y="${yR(30).toFixed(1)}" width="${plotW}" height="${(yR(0) - yR(30)).toFixed(1)}" fill="#10b981" fill-opacity="0.08" />
                    <line x1="${padL}" y1="${yR(70).toFixed(1)}" x2="${W - padR}" y2="${yR(70).toFixed(1)}" stroke="#f43f5e" stroke-dasharray="2,2" stroke-opacity="0.5" />
                    <line x1="${padL}" y1="${yR(30).toFixed(1)}" x2="${W - padR}" y2="${yR(30).toFixed(1)}" stroke="#10b981" stroke-dasharray="2,2" stroke-opacity="0.5" />

                    <polyline fill="none" stroke="#e2e8f0" stroke-width="1.6" points="${closePts}" />
                    ${smoothPts ? `<polyline fill="none" stroke="#818cf8" stroke-width="1.4" stroke-dasharray="4,2" points="${smoothPts}" />` : ''}
                    ${projectedNext !== null ? `<line x1="${lastX.toFixed(1)}" y1="${lastY.toFixed(1)}" x2="${nextX.toFixed(1)}" y2="${nextY.toFixed(1)}" stroke="#22d3ee" stroke-dasharray="3,2" stroke-width="1.5" /><circle cx="${nextX.toFixed(1)}" cy="${nextY.toFixed(1)}" r="3.5" fill="#22d3ee" />` : ''}

                    <polyline fill="none" stroke="#38bdf8" stroke-width="1.5" points="${velPts}" />
                    <polyline fill="none" stroke="#fb923c" stroke-width="1.5" points="${accPts}" />
                    ${crossMarkers}

                    ${rsiPts ? `<polyline fill="none" stroke="#c084fc" stroke-width="1.6" points="${rsiPts}" />` : ''}
                    ${buyMarkers}
                </svg>
            `;
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
            const fConf = FORECAST_CONFIG[symbolData.forecast_direction] || FORECAST_CONFIG.neutral;

            // Update Header Elements in Deep-Dive Panel
            document.getElementById('detailSymbolBadge').textContent = sym;
            document.getElementById('detailSymbolTitle').textContent = `${sym} Multi-Stream Kinematics Analysis`;
            document.getElementById('detailSymbolSubtitle').textContent = `Synthesized consensus across ${symbolData.providers_count} reporting feeds • Close ${fmtPrice(symbolData.close)} • RSI ${fmtNum(symbolData.rsi, 1)}`;
            
            const badgeEl = document.getElementById('detailRegimeBadge');
            badgeEl.className = `text-[10px] px-2 py-0.5 rounded-full font-semibold border ${rConf.badgeClass}`;
            badgeEl.innerHTML = `${rConf.icon} ${rConf.label}`;

            // Next-step projection & forecast cards
            const scfg = latestData.smooth_config || {};
            const smoothLabel = (scfg.method || 'hma').toUpperCase() + `(${scfg.period || 14}) Synced`;
            document.getElementById('detailSmoothTag').textContent = smoothLabel;
            document.getElementById('detailProjectedPrice').textContent = fmtPrice(symbolData.projected_price_next);
            const pDeltaPct = safeNum(symbolData.projected_delta_pct);
            const deltaEl = document.getElementById('detailProjectedDelta');
            deltaEl.textContent = pDeltaPct !== null ? `${fmtNum(symbolData.projected_delta, 2, '+')} (${fmtPct(pDeltaPct, 2, '+')})` : '--';
            deltaEl.className = `text-sm font-bold mono mt-0.5 ${pDeltaPct > 0 ? 'text-emerald-400' : (pDeltaPct < 0 ? 'text-rose-400' : 'text-slate-300')}`;
            document.getElementById('detailRsiZone').textContent = (symbolData.rsi_zone || 'unknown').replace('_', ' ');
            document.getElementById('detailCrossoverSignal').textContent = (symbolData.crossover_signal || 'none').replace('_', ' ');
            document.getElementById('detailTradingAction').textContent = symbolData.trading_action || fConf.label;

            renderCrossoverChart(
                document.getElementById('detailCrossoverChart'),
                symbolData.history_window || [],
                safeNum(symbolData.projected_price_next),
                symbolData.buy_events || []
            );

            // Diagnosis sentence
            const rv = safeNum(symbolData.rsi_velocity);
            const ra = safeNum(symbolData.rsi_acceleration);
            let diag = `${rConf.desc}. `;
            if (symbolData.projected_price_next !== null && symbolData.projected_price_next !== undefined) {
                diag += `Projected next close P̂ = ${fmtPrice(symbolData.projected_price_next)}. `;
            }
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
                const pFConf = FORECAST_CONFIG[e.forecast_direction] || FORECAST_CONFIG.neutral;
                const pRsi = safeNum(e.rsi);
                const pRv = safeNum(e.rsi_velocity);
                const pRa = safeNum(e.rsi_acceleration);

                return `
                    <tr class="hover:bg-slate-900/60 transition">
                        <td class="px-3.5 py-2 font-bold text-white">${e.provider}</td>
                        <td class="px-3.5 py-2 text-right text-slate-200">${fmtPrice(e.close)}</td>
                        <td class="px-3.5 py-2 text-right text-cyan-300">${fmtPrice(e.projected_price_next)}</td>
                        <td class="px-3.5 py-2 text-right ${safeNum(e.price_velocity) > 0 ? 'text-emerald-400' : (safeNum(e.price_velocity) < 0 ? 'text-rose-400' : 'text-slate-400')}">${fmtNum(e.price_velocity, 2, '+')}</td>
                        <td class="px-3.5 py-2 text-right text-slate-300">${fmtNum(e.price_acceleration, 2, '+')}</td>
                        <td class="px-3.5 py-2 text-right text-slate-200">${fmtNum(pRsi, 1)}</td>
                        <td class="px-3.5 py-2 text-right">${fmtNum(pRv, 2, '+')} / ${fmtNum(pRa, 2, '+')}</td>
                        <td class="px-3.5 py-2">
                            <span class="text-[10px] px-2 py-0.5 rounded border ${pFConf.cls}">
                                ${pFConf.label}
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
                    const forecasts = kin.forecasts || {};
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
                                <span class="px-1.5 py-0.5 rounded bg-emerald-950/40 text-emerald-300 border border-emerald-900/40">&uarr; Acc Bull: ${regimes.accelerating_bullish || 0}</span>
                                <span class="px-1.5 py-0.5 rounded bg-cyan-950/40 text-cyan-300 border border-cyan-900/40">&nearr; Dec Bull: ${regimes.decelerating_bullish || 0}</span>
                                <span class="px-1.5 py-0.5 rounded bg-rose-950/40 text-rose-300 border border-rose-900/40">&darr; Acc Bear: ${regimes.accelerating_bearish || 0}</span>
                                <span class="px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-300 border border-amber-900/40">&searr; Dec Bear: ${regimes.decelerating_bearish || 0}</span>
                                ${(forecasts.bearish_reversal || forecasts.bullish_reversal) ? `<span class="px-1.5 py-0.5 rounded bg-purple-950/40 text-purple-300 border border-purple-900/40">Reversals: &darr;${forecasts.bearish_reversal || 0} / &uarr;${forecasts.bullish_reversal || 0}</span>` : ''}
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


DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", os.getenv("ADMIN_HOST", "0.0.0.0"))
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", os.getenv("ADMIN_PORT", "5001")))


def get_lan_ip():
    """Detect LAN IP of current machine for subnet/remote browser access."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class AdminCliProvider:
    def __init__(self, coordinator, host=None, port=None):
        import threading

        self.coordinator = coordinator
        self.host = host or DASHBOARD_HOST
        self.port = port or DASHBOARD_PORT
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
                        "smoothed_close": cur.get("smoothed_close"),
                        "rsi": cur.get("rsi"),
                        "price_velocity": cur.get("price_velocity"),
                        "price_velocity_pct": cur.get("price_velocity_pct"),
                        "price_acceleration": cur.get("price_acceleration"),
                        "smoothed_velocity": cur.get("smoothed_velocity"),
                        "smoothed_acceleration": cur.get("smoothed_acceleration"),
                        "rsi_velocity": cur.get("rsi_velocity"),
                        "rsi_acceleration": cur.get("rsi_acceleration"),
                        "momentum_3m_pct": cur.get("momentum_3m_pct"),
                        "projected_price_next": cur.get("projected_price_next"),
                        "projected_delta": cur.get("projected_delta"),
                        "projected_delta_pct": cur.get("projected_delta_pct"),
                        "rsi_zone": cur.get("rsi_zone"),
                        "forecast_direction": cur.get("forecast_direction"),
                        "trading_action": cur.get("trading_action"),
                        "crossover_signal": cur.get("crossover_signal"),
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
                            "smoothed_velocities": [],
                            "smoothed_accelerations": [],
                            "rsi_velocities": [],
                            "rsi_accelerations": [],
                            "momentums": [],
                            "regimes": [],
                            "best_history": [],
                        },
                    )
                    sym_group["providers"].append(provider)
                    sym_group["entries"].append(entry)
                    if len(hist) > len(sym_group["best_history"]):
                        sym_group["best_history"] = [
                            {
                                "timestamp": h.get("timestamp"),
                                "close": h.get("close"),
                                "smoothed_close": h.get("smoothed_close"),
                                "price_velocity": h.get("price_velocity"),
                                "price_acceleration": h.get("price_acceleration"),
                                "smoothed_velocity": h.get("smoothed_velocity"),
                                "smoothed_acceleration": h.get("smoothed_acceleration"),
                                "rsi": h.get("rsi"),
                                "crossover_signal": h.get("crossover_signal"),
                            }
                            for h in hist[-20:]
                        ]
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
                    if entry["smoothed_velocity"] is not None:
                        sym_group["smoothed_velocities"].append(entry["smoothed_velocity"])
                    if entry["smoothed_acceleration"] is not None:
                        sym_group["smoothed_accelerations"].append(entry["smoothed_acceleration"])
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
                c_sv = median_or_none(g["smoothed_velocities"])
                c_sa = median_or_none(g["smoothed_accelerations"])
                c_rv = median_or_none(g["rsi_velocities"])
                c_ra = median_or_none(g["rsi_accelerations"])
                c_mom = median_or_none(g["momentums"])

                active_pv = c_sv if c_sv is not None else c_pv
                active_pa = c_sa if c_sa is not None else c_pa
                c_forecast = compute_kinematic_forecast(c_close, active_pv, active_pa, c_rsi)
                c_regime = classify_kinematic_regime(c_rv, c_ra, active_pv, active_pa)
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
                        "smoothed_velocity": round(c_sv, 2) if c_sv is not None else None,
                        "smoothed_acceleration": round(c_sa, 2) if c_sa is not None else None,
                        "rsi_velocity": round(c_rv, 2) if c_rv is not None else None,
                        "rsi_acceleration": (
                            round(c_ra, 2) if c_ra is not None else None
                        ),
                        "momentum_3m_pct": (
                            round(c_mom, 2) if c_mom is not None else None
                        ),
                        "projected_price_next": c_forecast["projected_price_next"],
                        "projected_delta": c_forecast["projected_delta"],
                        "projected_delta_pct": c_forecast["projected_delta_pct"],
                        "rsi_zone": c_forecast["rsi_zone"],
                        "forecast_direction": c_forecast["forecast_direction"],
                        "trading_action": c_forecast["trading_action"],
                        "crossover_signal": c_forecast["crossover_signal"],
                        "regime": c_regime,
                        "history_window": g["best_history"],
                        "buy_events": [
                            e
                            for e in self.coordinator.option_buy_events.get(sym, [])
                            if not e.get("dry_run")
                        ],
                        "sell_events": [
                            e
                            for e in self.coordinator.option_sell_events.get(sym, [])
                            if not e.get("dry_run")
                        ],
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
                    smooth_config={
                        "method": KINEMATICS_SMOOTH_METHOD,
                        "period": KINEMATICS_SMOOTH_PERIOD,
                        "overbought": KINEMATICS_RSI_OVERBOUGHT,
                        "oversold": KINEMATICS_RSI_OVERSOLD,
                    },
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
            ob_entry = PROVIDER_REGISTRY.get("OPTION_BUYER", {})
            ob = ob_entry.get("router")
            os_entry = PROVIDER_REGISTRY.get("OPTION_SELLER", {})
            osel = os_entry.get("router")

            position_monitor_report = {
                "status": pm.last_status if pm else "not_loaded",
                "last_poll_utc": pm.last_poll_utc if pm else None,
                "active_equity_positions": pm.equity_count if pm else 0,
                "active_option_positions": pm.option_count if pm else 0,
                "active_total_positions": pm.position_count if pm else 0,
            }
            option_buyer_report = {
                "status": ob.last_status if ob else "not_loaded",
                "mode": ob.mode if ob else "disabled",
                "orders_submitted": ob.orders_submitted if ob else 0,
                "dry_run_orders": ob.dry_run_orders if ob else 0,
                "last_order_utc": ob.last_order_utc if ob else None,
                "last_order_contract": ob.last_order_contract if ob else None,
                "last_order_reason": ob.last_order_reason if ob else None,
                "cached_contracts": len(self.coordinator.option_chain_cache),
            }
            option_seller_report = {
                "status": osel.last_status if osel else "not_loaded",
                "mode": osel.mode if osel else "disabled",
                "orders_submitted": osel.orders_submitted if osel else 0,
                "dry_run_orders": osel.dry_run_orders if osel else 0,
                "profit_target_orders": osel.profit_target_orders if osel else 0,
                "trailing_stop_orders": osel.trailing_stop_orders if osel else 0,
                "stop_loss_orders": osel.stop_loss_orders if osel else 0,
                "stop_loss_watches": {
                    sym: {
                        "streak": st.get("streak", 0),
                        "pnl_pct": round(st["last_pnl_pct"], 2)
                        if st.get("last_pnl_pct") is not None
                        else None,
                        "minute_utc": st.get("last_minute_key"),
                    }
                    for sym, st in (osel.stop_loss_state_by_symbol if osel else {}).items()
                    if st.get("streak", 0) > 0
                },
                "peak_profit_watches": {
                    sym: {
                        "peak_pnl_pct": round(st["peak_pnl_pct"], 2),
                        "last_pnl_pct": round(st["last_pnl_pct"], 2),
                        "drawdown_pct": round(st.get("drawdown_pct", 0.0), 2),
                    }
                    for sym, st in (osel.peak_profit_state_by_symbol if osel else {}).items()
                    if st.get("peak_pnl_pct", 0.0) > 0
                },
                "last_order_utc": osel.last_order_utc if osel else None,
                "last_order_contract": osel.last_order_contract if osel else None,
                "last_order_reason": osel.last_order_reason if osel else None,
                "positions_checked": osel.last_positions_checked if osel else 0,
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
                    k
                    for k in PROVIDER_REGISTRY.keys()
                    if k not in {"POSITIONS_MONITOR", "OPTION_BUYER", "OPTION_SELLER"}
                ],
                "position_monitor": position_monitor_report,
                "option_buyer": option_buyer_report,
                "option_seller": option_seller_report,
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

        lan_ip = get_lan_ip()
        logging.info(
            "=================================================================="
        )
        logging.info("RSI & Kinematics Web Dashboard Ready:")
        logging.info("  -> Local Machine:   http://127.0.0.1:%d/", self.port)
        if self.host == "0.0.0.0":
            if lan_ip and lan_ip != "127.0.0.1":
                logging.info(
                    "  -> Subnet / LAN:    http://%s:%d/ (accessible from other machines)",
                    lan_ip,
                    self.port,
                )
            else:
                logging.info(
                    "  -> Subnet / LAN:    http://<HOST_IP>:%d/ (bound to 0.0.0.0)",
                    self.port,
                )
        else:
            logging.info("  -> Bound Host:      http://%s:%d/", self.host, self.port)
        logging.info(
            "=================================================================="
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
    # Binds to 0.0.0.0:5001 by default (override with DASHBOARD_HOST / DASHBOARD_PORT)
    # allowing access from both localhost and other machines on the same subnet
    cli_admin = AdminCliProvider(coordinator, host=DASHBOARD_HOST, port=DASHBOARD_PORT)

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
    pm_task = asyncio.create_task(
        run_provider_isolated(position_monitor, coordinator),
        name="ALPACA_POSITION_MONITOR",
    )
    tasks.append(pm_task)

    option_buyer = AlpacaOptionBuyer(keys, coordinator)
    ob_task = asyncio.create_task(
        run_provider_isolated(option_buyer, coordinator),
        name="ALPACA_OPTION_BUYER",
    )
    tasks.append(ob_task)

    option_seller = AlpacaOptionSeller(keys, coordinator)
    os_task = asyncio.create_task(
        run_provider_isolated(option_seller, coordinator),
        name="ALPACA_OPTION_SELLER",
    )
    tasks.append(os_task)

    for provider, entry in PROVIDER_REGISTRY.items():
        task = asyncio.create_task(
            run_provider_isolated(entry["router"], coordinator), name=provider
        )
        entry["task"] = task
        tasks.append(task)

    PROVIDER_REGISTRY["POSITIONS_MONITOR"] = {
        "router": position_monitor,
        "task": pm_task,
    }
    PROVIDER_REGISTRY["OPTION_BUYER"] = {
        "router": option_buyer,
        "task": ob_task,
    }
    PROVIDER_REGISTRY["OPTION_SELLER"] = {
        "router": option_seller,
        "task": os_task,
    }

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
