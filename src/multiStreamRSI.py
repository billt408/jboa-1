import asyncio
import aiohttp
import json
import logging
import math
import os
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

from lib.logger import initialize_logging
from lib.utils import JsonIniParser

initialize_logging(default_level=logging.INFO)

MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = clock_time(9, 30)
CLOSE_TIME = clock_time(16, 0)

MODE = "wilder"
RSI_PERIOD = 14
FEED = DataFeed.IEX

# =====================================================================
# ROUTER SWITCH CONFIGURATION
# Options: "ALPACA", "TRADIER", or "BOTH"
# =====================================================================
ROUTER_SWITCH = "ALPACA"  


def setup_symbol_logger(symbol: str, client_name: str) -> logging.Logger:
    """Creates an isolated logger routing to unique client disk folders."""
    logger_name = f"rsi_logger_{client_name.lower()}_{symbol}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        logger.handlers.clear()

    log_dir = Path(f"./logs/{client_name.lower()}")
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"{symbol}-{client_name.upper()}-{timestamp}.log"
    
    # 1. File Handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    logger.addHandler(file_handler)

    # 2. Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        f'%(asctime)s - [{client_name.upper()}:{symbol}] - %(levelname)s - %(message)s'
    ))
    logger.addHandler(console_handler)

    return logger


class SessionRSI:
    """Maintain independent RSI state for one symbol."""

    def __init__(self, symbol, client_name, mode="since_open", period=14):
        if mode not in {"since_open", "wilder"}:
            raise ValueError(f"Unknown RSI mode: {mode}")
        if isinstance(period, bool) or not isinstance(period, int) or period < 2:
            raise ValueError("RSI period must be an integer >= 2")

        self.symbol = symbol
        self.client_name = client_name
        self.mode = mode
        self.period = period
        self.logger = setup_symbol_logger(symbol, client_name)
        self.session_date = None
        self.last_timestamp = None
        self.previous_close = None
        self.changes = 0
        self.gain_sum = 0.0
        self.loss_sum = 0.0
        self.average_gain = None
        self.average_loss = None
        self.latest = None

    @property
    def is_ready(self):
        return self.latest is not None

    @property
    def warmup_bars_remaining(self):
        closes_seen = self.changes + 1 if self.previous_close is not None else 0
        required = self.period + 1 if self.mode == "wilder" else 2
        return max(0, required - closes_seen)

    def warmup_session_alpaca(self, historical_client, through=None):
        """Replay completed regular-session minute bars for one day via Alpaca Client."""
        now = datetime.now(MARKET_TZ)
        if through is None:
            through = now
        elif through.tzinfo is None or through.utcoffset() is None:
            raise ValueError("through must be timezone-aware")

        through = min(through.astimezone(MARKET_TZ), now)
        cutoff = through.replace(second=0, microsecond=0)
        day = cutoff.date()

        if self.session_date is not None and day < self.session_date:
            return 0
        if day.weekday() >= 5:
            return 0

        session_open = datetime.combine(day, OPEN_TIME, tzinfo=MARKET_TZ)
        session_close = datetime.combine(day, CLOSE_TIME, tzinfo=MARKET_TZ)

        end_exclusive = min(cutoff, session_close)
        if end_exclusive <= session_open:
            return 0

        start = session_open
        if self.session_date == day and self.last_timestamp is not None:
            start = max(start, self.last_timestamp + timedelta(minutes=1))

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
        response = historical_client.get_stock_bars(request)
        returned_bars = response.data.get(self.symbol, [])

        unique = {}
        for bar in returned_bars:
            timestamp = bar.timestamp.astimezone(MARKET_TZ)
            if not start <= timestamp < end_exclusive:
                continue
            if timestamp + timedelta(minutes=1) > end_exclusive:
                continue

            close = float(bar.close)
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"Invalid close for {self.symbol}: {bar.close!r}")
            unique[timestamp] = bar

        processed = 0
        for timestamp in sorted(unique):
            previous_timestamp = self.last_timestamp
            self.process_bar(timestamp, float(unique[timestamp].close))
            if self.last_timestamp != previous_timestamp:
                processed += 1
        return processed

    def log_warmup_status(self, loaded):
        if self.is_ready:
            self.logger.info(
                "Warm-up: loaded=%d changes=%d latest=%s RSI[%s]=%.2f",
                loaded, self.changes, self.latest["datetime"], self.mode, self.latest["rsi"],
            )
        else:
            self.logger.info(
                "Warm-up: loaded=%d ready=False additional_closes_required=%d",
                loaded, self.warmup_bars_remaining,
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
        else:
            if self.changes < self.period:
                return None
            if self.changes == self.period:
                self.average_gain = self.gain_sum / self.period
                self.average_loss = self.loss_sum / self.period
            else:
                self.average_gain = (self.average_gain * (self.period - 1) + gain) / self.period
                self.average_loss = (self.average_loss * (self.period - 1) + loss) / self.period
            up = self.average_gain
            down = self.average_loss

        if up == 0.0 and down == 0.0:
            return 50.0
        if down == 0.0:
            return 100.0

        return 100.0 - 100.0 / (1.0 + up / down)

    def process_bar(self, timestamp, close_val):
        timestamp = timestamp.astimezone(MARKET_TZ)
        day = timestamp.date()

        if self.session_date != day:
            self.reset_session(day)

        self.last_timestamp = timestamp
        rsi_val = self.calculate_rsi(close_val)

        if rsi_val is not None:
            self.latest = {
                "datetime": timestamp.isoformat(),
                "rsi": rsi_val,
                "close": close_val,
            }
            return self.latest
        return None


class StreamRouter:
    """Orchestrates multi-symbol live streaming using Alpaca Data Stream Websockets."""

    def __init__(self, symbols, historical_client, mode="wilder", period=14):
        self.symbols = symbols
        self.historical_client = historical_client
        self.trackers = {s: SessionRSI(s, "alpaca", mode=mode, period=period) for s in symbols}
        self.queues = {s: asyncio.Queue() for s in symbols}

    async def start(self, stream_client: StockDataStream):
        warmup_tasks = []
        for symbol, tracker in self.trackers.items():
            logging.info("Warming up Alpaca session for %s...", symbol)
            task = asyncio.to_thread(tracker.warmup_session_alpaca, self.historical_client)
            warmup_tasks.append((tracker, task))

        for tracker, task in warmup_tasks:
            loaded = await task
            tracker.log_warmup_status(loaded)

        worker_tasks = [asyncio.create_task(self.consume_symbol_bars(s)) for s in self.symbols]

        async def handle_stream_bar(bar):
            symbol = bar.symbol
            if symbol in self.queues:
                await self.queues[symbol].put((bar.timestamp, float(bar.close)))

        logging.info("Subscribing Alpaca client to symbols: %s", self.symbols)
        stream_client.subscribe_bars(handle_stream_bar, *self.symbols)
        await asyncio.gather(stream_client._run_forever(), *worker_tasks)

    async def consume_symbol_bars(self, symbol: str):
        queue = self.queues[symbol]
        tracker = self.trackers[symbol]
        while True:
            timestamp, close_val = await queue.get()
            try:
                observation = tracker.process_bar(timestamp, close_val)
                if observation:
                    tracker.logger.info("Alpaca Real-time RSI update: %s", observation)
            except Exception as e:
                tracker.logger.error("Exception handling live bar frame: %s", e, exc_info=True)
            finally:
                queue.task_done()


class TradierStreamRouter:
    """Orchestrates multi-symbol live streaming using Tradier's HTTP session and Websocket API."""

    def __init__(self, symbols, tradier_token, mode="wilder", period=14):
        self.symbols = [s.upper() for s in symbols]
        self.tradier_token = tradier_token
        self.trackers = {s: SessionRSI(s, "tradier", mode=mode, period=period) for s in symbols}
        self.queues = {s: asyncio.Queue() for s in symbols}

    async def start(self):
        # Concurrently perform HTTP warm-ups against Tradier APIs
        async with aiohttp.ClientSession() as session:
            warmup_tasks = [self.warmup_symbol_tradier(session, s) for s in self.symbols]
            await asyncio.gather(*warmup_tasks)

        worker_tasks = [asyncio.create_task(self.consume_symbol_bars(s)) for s in self.symbols]
        websocket_task = asyncio.create_task(self.run_tradier_websocket())
        await asyncio.gather(websocket_task, *worker_tasks)

    async def warmup_symbol_tradier(self, session: aiohttp.ClientSession, symbol: str):
        """Fetches historical bars straight from Tradier endpoints to seed the calculator."""
        tracker = self.trackers[symbol]
        now = datetime.now(MARKET_TZ)
        cutoff = now.replace(second=0, microsecond=0)
        day = cutoff.date()

        if day.weekday() >= 5:
            tracker.log_warmup_status(0)
            return

        session_open = datetime.combine(day, OPEN_TIME, tzinfo=MARKET_TZ)
        if cutoff <= session_open:
            tracker.log_warmup_status(0)
            return

        # Fetch historical 1-minute tracking interval array
        url = "https://tradier.com"
        params = {
            "symbol": symbol,
            "interval": "1min",
            "start": day.strftime("%Y-%m-%d %H:%M"),
            "end": cutoff.strftime("%Y-%m-%d %H:%M"),
            "session_filter": "all"
        }
        headers = {
            "Authorization": f"Bearer {self.tradier_token}",
            "Accept": "application/json"
        }

        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    logging.error(f"Tradier warm-up HTTP error {resp.status} for {symbol}")
                    tracker.log_warmup_status(0)
                    return

                payload = await resp.json()
                history = payload.get("history", {})
                if not history or "day" not in history:
                    tracker.log_warmup_status(0)
                    return

                # Tradier nests historical intervals in a "day" list or a single dict mapping
                day_data = history["day"]
                bars = day_data if isinstance(day_data, list) else [day_data]
                processed = 0

                for bar in bars:
                    # Format layout parsing string matches Tradier format: "2026-10-30 09:31:00"
                    dt_str = bar.get("date")
                    if not dt_str:
                        continue
                    timestamp = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MARKET_TZ)
                    
                    if not session_open <= timestamp < cutoff:
                        continue

                    close_val = float(bar["close"])
                    tracker.process_bar(timestamp, close_val)
                    processed += 1

                tracker.log_warmup_status(processed)

        except Exception as e:
            logging.error(f"Failed parsing Tradier history for {symbol}: {e}", exc_info=True)
            tracker.log_warmup_status(0)

    async def run_tradier_websocket(self):
        session_url = "https://tradier.com"
        headers = {
            "Authorization": f"Bearer {self.tradier_token}",
            "Accept": "application/json"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(session_url, headers=headers) as resp:
                if resp.status != 200:
                    logging.error(f"Failed to generate Tradier Session Key. HTTP Status: {resp.status}")
                    return
                session_data = await resp.json()
                
            stream_config = session_data.get("stream", {})
            ws_url = stream_config.get("url")
            session_id = stream_config.get("sessionid")

            if not ws_url or not session_id:
                logging.error("Invalid streaming payload received from Tradier APIs.")
                return

            payload = {
                "filter": self.symbols,
                "line": "timesale",
                "sessionid": session_id,
                "account": "None"
            }

            logging.info(f"Connecting to Tradier WebSocket infrastructure for: {self.symbols}")
            async with session.ws_connect(ws_url) as ws:
                await ws.send_str(json.dumps(payload))
                
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("event") == "timesale":
                            symbol = data.get("symbol")
                            if symbol in self.queues:
                                ms_epoch = data.get("date", datetime.now(MARKET_TZ).timestamp() * 1000)
                                dt_object = datetime.fromtimestamp(ms_epoch / 1000.0, tz=MARKET_TZ)
                                close_val = float(data.get("last"))
                                await self.queues[symbol].put((dt_object, close_val))
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    async def consume_symbol_bars(self, symbol: str):
        queue = self.queues[symbol]
        tracker = self.trackers[symbol]
        while True:
            timestamp, close_val = await queue.get()
            try:
                observation = tracker.process_bar(timestamp, close_val)
                if observation:
                    tracker.logger.info("Tradier Real-time RSI update: %s", observation)
            except Exception as e:
                tracker.logger.error("Exception handling live bar frame: %s", e, exc_info=True)
            finally:
                queue.task_done()


def load_settings():
    try:
        config = JsonIniParser(str(Path(__file__).resolve().parent / "lib" / "parms.ini"))
        config.load_globals()
    except Exception as ce:
        logging.error(f"? Failed to parse configuration file: {ce}")
        sys.exit(1)

    try:
        target_symbols = config.get("symbols", "target_symbols")
    except Exception as e:
        logging.error(f"Critical Error initializing symbols: {e}")
        sys.exit(2)

    try:
        credentials = config.CREDENTIALS
        alpaca_api_key = credentials.get("api_key", "")
        alpaca_secret_key = credentials.get("secret_key", "")
        tradier_token = credentials.get("tradier_token", os.environ.get("TRADIER_TOKEN", ""))
    except Exception as e:
        logging.error(f"Critical Error loading config credential dictionaries: {e}")
        sys.exit(2)

    if isinstance(target_symbols, str):
        symbols = [s.strip().upper() for s in target_symbols.split(",") if s.strip()]
    else:
        symbols = [s.strip().upper() for s in target_symbols]

    symbols = list(dict.fromkeys(symbols))
    return symbols, alpaca_api_key, alpaca_secret_key, tradier_token


async def main():
    symbols, alpaca_key, alpaca_secret, tradier_token = load_settings()
    execution_tasks = []

    # Branch routing logic using switch framework configurations
    if ROUTER_SWITCH in {"ALPACA", "BOTH"}:
        if not alpaca_key or not alpaca_secret:
            logging.error("Alpaca switch active but API keys are missing from configuration profiles.")
            return 1
        logging.info("Starting up Alpaca Streaming Engine Pipeline...")
        historical_client = StockHistoricalDataClient(api_key=alpaca_key, secret_key=alpaca_secret)
        stream_client = StockDataStream(api_key=alpaca_key, secret_key=alpaca_secret)
        alpaca_router = StreamRouter(symbols=symbols, historical_client=historical_client, mode=MODE, period=RSI_PERIOD)
        execution_tasks.append(alpaca_router.start(stream_client))

    if ROUTER_SWITCH in {"TRADIER", "BOTH"}:
        if not tradier_token:
            logging.error("Tradier switch active but 'tradier_token' missing from parameter configurations.")
            return 1
        logging.info("Starting up Tradier Streaming Engine Pipeline...")
        tradier_router = TradierStreamRouter(symbols=symbols, tradier_token=tradier_token, mode=MODE, period=RSI_PERIOD)
        execution_tasks.append(tradier_router.start())

    if not execution_tasks:
        logging.error("Invalid ROUTER_SWITCH parameter configuration selection choice.")
        return 2

    await asyncio.gather(*execution_tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Application cleanly interrupted by user.")
        sys.exit(0)
