import time
import requests
class TwelveData:
    BASE_URL = "https://api.twelvedata.com"

    def __init__(self, api_key, min_interval=8.0):
        self.api_key = api_key
        self.min_interval = min_interval
        self.session = requests.Session()
        self._last_request = None

    def _get(self, endpoint, **params):
        # Pace all requests made by this client.
        if self._last_request is not None:
            elapsed = time.monotonic() - self._last_request
            time.sleep(max(0, self.min_interval - elapsed))

        self._last_request = time.monotonic()

        response = self.session.get(
            f"{self.BASE_URL}/{endpoint}",
            params={**params, "apikey": self.api_key},
            timeout=30,
        )

        # Twelve Data may return an API error in a JSON response.
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise RuntimeError("Twelve Data returned a non-JSON response")

        if data.get("status") == "error":
            raise RuntimeError(
                f"Twelve Data error {data.get('code', response.status_code)}: "
                f"{data.get('message', 'Unknown error')}"
            )

        response.raise_for_status()
        return data

    def rsi(self, symbol, interval="1day", period=14, outputsize=1):
        data = self._get(
            "rsi",
            symbol=symbol,
            interval=interval,
            time_period=period,
            series_type="close",
            outputsize=outputsize,
            order="desc",
            format="JSON",
        )

        values = data.get("values")
        if not values:
            raise RuntimeError(f"No RSI values returned for {symbol}: {data}")

        return [
            {
                "datetime": row["datetime"],
                "rsi": float(row["rsi"]),
            }
            for row in values
        ]

    def daily_monthly_rsi(self, symbol, period=14):
        return {
            "symbol": symbol,
            "period": period,
            "daily": self.rsi(symbol, interval="1day", period=period)[0],
            "monthly": self.rsi(symbol, interval="1month", period=period)[0],
        }

    def close(self):
        self.session.close()

def make_twelvedata_client(api_key, min_interval=8.0):
    return TwelveData(api_key, min_interval)
        

