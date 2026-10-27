import math
import time
import requests


class TwelveData:
    BASE_URL = "https://api.twelvedata.com"

    def __init__(self, api_key, min_interval=8.0):
        if not api_key:
            raise ValueError("A Twelve Data API key is required")

        self.api_key = api_key
        self.min_interval = max(0.0, float(min_interval))
        self.session = requests.Session()
        self._last_request = None

    def _get(self, endpoint, **params):
        # Pace requests made by this client.
        if self._last_request is not None:
            elapsed = time.monotonic() - self._last_request
            time.sleep(max(0.0, self.min_interval - elapsed))

        self._last_request = time.monotonic()

        response = self.session.get(
            f"{self.BASE_URL}/{endpoint}",
            params={**params, "apikey": self.api_key},
            timeout=30,
        )

        try:
            data = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise RuntimeError(
                "Twelve Data returned a non-JSON response"
            ) from exc

        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(
                f"Twelve Data error "
                f"{data.get('code', response.status_code)}: "
                f"{data.get('message', 'Unknown error')}"
            )

        response.raise_for_status()

        if not isinstance(data, dict):
            raise RuntimeError("Twelve Data returned an unexpected response")

        return data

    def rsi(self, symbol, interval="1day", period=30, outputsize=30, date=None, end_date=None):
        """
        Return RSI observations, oldest first.

        period: RSI calculation length, measured in candles.
        outputsize: Number of RSI observations requested.
        """
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

        symbol = symbol.strip()

        #data = self._get(
        #    "rsi",
        #    symbol=symbol,
        #    interval=interval,
        #    time_period=period,
        #    series_type="close",
        #    outputsize=outputsize,
        #    order="asc",
        #    format="JSON",
        #)
        
        params = {
            "symbol": symbol,
            "interval": interval,
            "time_period": period,
            "series_type": "close",
            "outputsize": outputsize,
            "order": "asc",
            "format": "JSON",
        }

        if date is not None and end_date is not None:
            raise ValueError("Specify date or end_date, not both")

        if date is not None:
            params["date"] = date

        if end_date is not None:
            params["end_date"] = end_date
            params["timezone"] = "America/New_York"

        data = self._get("rsi", **params)
        values = data.get("values")
        if not isinstance(values, list) or not values:
            raise RuntimeError(f"No RSI values returned for {symbol}")

        observations = []

        for row in values:
            try:
                timestamp = row["datetime"]
                value = float(row["rsi"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid RSI observation for {symbol}: {row!r}"
                ) from exc

            if (
                not isinstance(timestamp, str)
                or not math.isfinite(value)
                or not 0.0 <= value <= 100.0
            ):
                raise RuntimeError(
                    f"Invalid RSI observation for {symbol}: {row!r}"
                )

            observations.append({
                "datetime": timestamp,
                "rsi": value,
            })

        # Explicit sorting also makes latest selection unambiguous.
        observations.sort(key=lambda row: row["datetime"])
        return observations

    def daily_rsi_history(self, symbol, period=30, sessions=30, interval="1day"):
        """Return daily RSI for the latest available trading sessions."""
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

    def daily_rsi(self, symbol, period=30, sessions=30, interval="1day"):


        return self.daily_rsi_history(
            symbol=symbol,
            period=period,
            sessions=sessions,
            interval=interval,
        )

    def close(self):
        self.session.close()


def mk_twelvedata_client(api_key, min_interval=8.0):
    return TwelveData(api_key, min_interval=min_interval)