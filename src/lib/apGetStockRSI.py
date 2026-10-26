import math
import time
import logging
from datetime import date as Date
from datetime import datetime, time as DateTime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


class AlpacaClient:
    NY = ZoneInfo("America/New_York")

    # Alpaca timeframe and approximate calendar days per bar.
    INTERVALS = {
        "1min": (TimeFrame(1, TimeFrameUnit.Minute), 1 / 390),
        "5min": (TimeFrame(5, TimeFrameUnit.Minute), 5 / 390),
        "15min": (TimeFrame(15, TimeFrameUnit.Minute), 15 / 390),
        "30min": (TimeFrame(30, TimeFrameUnit.Minute), 30 / 390),
        "45min": (TimeFrame(45, TimeFrameUnit.Minute), 45 / 390),
        "1h": (TimeFrame(1, TimeFrameUnit.Hour), 1 / 6.5),
        "2h": (TimeFrame(2, TimeFrameUnit.Hour), 2 / 6.5),
        "4h": (TimeFrame(4, TimeFrameUnit.Hour), 4 / 6.5),
        "8h": (TimeFrame(8, TimeFrameUnit.Hour), 1),
        "1day": (TimeFrame.Day, 1),
        "1week": (TimeFrame.Week, 7),
        "1month": (TimeFrame.Month, 31),
    }

    def __init__(
        self,
        api_key=None,
        secret_key=None,
        min_interval=0.0,
        *,
        client=None,
        #feed="iex",
        feed="sip",
        adjustment="split",
        warmup_bars=500,
    ):
        """
        Supply either:
          - api_key and secret_key, or
          - an existing StockHistoricalDataClient via client.

        feed:
          "iex" or "sip", subject to your Alpaca subscription.

        adjustment:
          "raw", "split", "dividend", or "all".
        """
        if client is None:
            if not api_key or not secret_key:
                raise ValueError(
                    "An Alpaca API key and secret key are required"
                )

            client = StockHistoricalDataClient(
                api_key=api_key,
                secret_key=secret_key,
            )

        if (
            isinstance(warmup_bars, bool)
            or not isinstance(warmup_bars, int)
            or warmup_bars < 0
        ):
            raise ValueError("warmup_bars must be an integer >= 0")

        self.client = client
        self.feed = DataFeed(feed)
        self.adjustment = Adjustment(adjustment)
        self.warmup_bars = warmup_bars
        self.min_interval = max(0.0, float(min_interval))
        self._last_request = None

    def _get_bars(self, request):
        if self._last_request is not None:
            elapsed = time.monotonic() - self._last_request
            time.sleep(max(0.0, self.min_interval - elapsed))

        self._last_request = time.monotonic()

        # Let Alpaca SDK exceptions propagate with their API details.
        return self.client.get_stock_bars(request)

    def _end_timestamp(self, value):
        """
        No date: use the current timestamp.

        YYYY-MM-DD / date object: include that entire New York date.

        datetime / ISO datetime: use that instant as the cutoff.
        Naive datetimes are interpreted in America/New_York.
        """
        if value is None:
            return datetime.now(timezone.utc)

        if isinstance(value, str):
            value = value.strip()

            if len(value) == 10:
                value = Date.fromisoformat(value)
            else:
                value = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )

        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=self.NY)
            return value.astimezone(timezone.utc)

        if isinstance(value, Date):
            next_day = value + timedelta(days=1)
            return (
                datetime.combine(next_day, DateTime.min, tzinfo=self.NY)
                - timedelta(microseconds=1)
            ).astimezone(timezone.utc)

        raise ValueError(
            "date must be YYYY-MM-DD, an ISO datetime, "
            "a date object, or a datetime object"
        )

    @staticmethod
    def _rsi_value(avg_gain, avg_loss):
        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0.0 else 0.0

        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    def _calculate_rsi(self, bars, period, interval):
        if len(bars) <= period:
            return []

        closes = [float(bar.close) for bar in bars]

        if any(not math.isfinite(close) for close in closes):
            raise RuntimeError("Alpaca returned a non-finite closing price")

        gains = []
        losses = []

        for previous, current in zip(closes, closes[1:]):
            change = current - previous
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))

        # Seed Wilder's averages with the first `period` changes.
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        observations = []

        for index in range(period, len(bars)):
            if index > period:
                avg_gain = (
                    avg_gain * (period - 1) + gains[index - 1]
                ) / period

                avg_loss = (
                    avg_loss * (period - 1) + losses[index - 1]
                ) / period

            timestamp = bars[index].timestamp.astimezone(self.NY)

            if interval in {"1day", "1week", "1month"}:
                formatted_timestamp = timestamp.strftime("%Y-%m-%d")
            else:
                formatted_timestamp = timestamp.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

            observations.append({
                "datetime": formatted_timestamp,
                "rsi": self._rsi_value(avg_gain, avg_loss),
            })

        return observations

    def rsi(
        self,
        symbol,
        interval="1day",
        period=30,
        outputsize=30,
        date=None,
    ):
        """Return RSI observations, oldest first."""
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a nonempty string")

        if isinstance(period, bool) or not isinstance(period, int) or period < 2:
            raise ValueError("period must be an integer >= 2")

        if (
            isinstance(outputsize, bool)
            or not isinstance(outputsize, int)
            or not 1 <= outputsize <= 5000
        ):
            raise ValueError("outputsize must be an integer from 1 to 5000")

        if interval not in self.INTERVALS:
            raise ValueError(
                f"Unsupported interval: {interval!r}. "
                f"Supported intervals: {', '.join(self.INTERVALS)}"
            )

    #    symbol = symbol.strip().upper()
    #    timeframe, days_per_bar = self.INTERVALS[interval]
        #end = self._end_timestamp(date)
        #This account cannot query the most recent 15 minutes of SIP data.
        # Use 16 minutes to leave a small safety margin.
    #    if self.feed == DataFeed.SIP:
    #        latest_allowed = datetime.now(timezone.utc) - timedelta(minutes=16)
    #        end = min(end, latest_allowed)

        # Extra history reduces the effect of RSI initialization.
    #    warmup = max(self.warmup_bars, period * 10)
    #    required_bars = period + outputsize + warmup

    #    lookback_days = max(
    #        30,
    #        math.ceil(required_bars * days_per_bar * 2.0),
    #    )
        
        symbol = symbol.strip().upper()
        timeframe, days_per_bar = self.INTERVALS[interval]

        # Initialize end before using it.
        end = self._end_timestamp(date)

        # Keep SIP requests outside the restricted recent-data window.
        if self.feed == DataFeed.SIP:
            latest_allowed = (
                datetime.now(timezone.utc) - timedelta(minutes=16)
            )
            end = min(end, latest_allowed)

        # Extra history reduces the effect of RSI initialization.
        warmup = max(self.warmup_bars, period * 10)
        required_bars = period + outputsize + warmup

        lookback_days = max(
            30,
            math.ceil(required_bars * days_per_bar * 2.0),
        )

        bars = []

        # Expand the window if holidays, sparse trading, etc. leave
        # fewer bars than requested.
        for _ in range(5):
            try:
                start = end - timedelta(days=lookback_days)
            except OverflowError:
                start = datetime(1970, 1, 1, tzinfo=timezone.utc)

            start = max(
                start,
                datetime(1970, 1, 1, tzinfo=timezone.utc),
            )

            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                limit=required_bars,
                sort=Sort.DESC,
                feed=self.feed,
                adjustment=self.adjustment,
            )

            response = self._get_bars(request)
            bars = list(response.data.get(symbol, []))

            if len(bars) >= required_bars:
                break

            if start.year == 1970:
                break

            lookback_days *= 2

        # Sort and deduplicate before calculating.
        by_timestamp = {bar.timestamp: bar for bar in bars}
        bars = sorted(
            by_timestamp.values(),
            key=lambda bar: bar.timestamp,
        )

        observations = self._calculate_rsi(
            bars=bars,
            period=period,
            interval=interval,
        )

        if not observations:
            raise RuntimeError(
                f"Not enough price history to calculate RSI for {symbol}: "
                f"need at least {period + 1} bars, received {len(bars)}"
            )

        return observations[-outputsize:]

    def daily_rsi_history(
        self,
        symbol,
        period=30,
        sessions=30,
        interval="1day",
    ):
        """Return RSI for the latest available bars."""
        history = self.rsi(
            symbol=symbol,
            interval=interval,
            period=period,
            outputsize=sessions,
        )

        return {
            "symbol": symbol.strip(),
            "interval": interval,
            "period": period,
            "requested_sessions": sessions,
            "returned_sessions": len(history),
            "latest": history[-1],
            "history": history,
        }

    def daily_rsi(
        self,
        symbol,
        period=30,
        sessions=30,
        interval="1day",
    ):
        return self.daily_rsi_history(
            symbol=symbol,
            period=period,
            sessions=sessions,
            interval=interval,
        )

    def close(self):
        # Compatibility method; this wrapper does not create a
        # separate requests.Session.
        pass

def mk_alpaca_client(
    api_key=None,
    secret_key=None,
    min_interval=0.0,
    **kwargs,
):
    return AlpacaClient(
        api_key=api_key,
        secret_key=secret_key,
        min_interval=min_interval,
        **kwargs,
    )