import logging
import os
import time
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional

import pytz
from ratelimit import limits, sleep_and_retry

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from fastapi import FastAPI, Query
from finviz_scraper import fetch_insider_trades

app = FastAPI()

_trading_client: Optional[TradingClient] = None
_http_session = requests.Session()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('trading.log')
    ]
)
logger = logging.getLogger(__name__)

# === Load environment variables ===
ALPACA_API_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
STOCKDATA_API_KEY = os.getenv("STOCKDATA_API_KEY")

# API rate limits
CALLS_PER_SECOND = 2
SECONDS_BETWEEN_CALLS = 1

INSIDER_DEFAULT_LIMIT = 100


@sleep_and_retry
@limits(calls=CALLS_PER_SECOND, period=SECONDS_BETWEEN_CALLS)
def rate_limited_request(session, url, timeout=10, **kwargs):
    response = session.get(url, timeout=timeout, **kwargs)
    # Treat 403/404 as signal to back off instead of crashing the trading loop.
    if response.status_code in (403, 404):
        logger.warning("HTTP %s from %s; skipping.", response.status_code, url)
        return {}
    response.raise_for_status()
    return response.json()


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def get_trading_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        api_key = ALPACA_API_KEY or _require_env("APCA_API_KEY_ID")
        api_secret = ALPACA_SECRET_KEY or _require_env("APCA_API_SECRET_KEY")
        _trading_client = TradingClient(api_key, api_secret, paper=True)
    return _trading_client


def get_recent_insider_trades(limit: int = INSIDER_DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Shared helper so both the trading loop and APIs reuse the same Finviz scrape logic."""
    return fetch_insider_trades(limit=limit, session=_http_session)


def calculate_position_size(client: TradingClient, price: float, max_position_pct: float = 0.02) -> int:
    try:
        account = client.get_account()
        equity = float(account.equity)
        max_position_value = equity * max_position_pct
        qty = int(max_position_value / price)
        return max(1, min(qty, 100))  # Minimum 1, maximum 100 shares
    except Exception as e:
        logger.error(f"Error calculating position size: {e}")
        return 10  # fallback to default


def is_market_hours() -> bool:
    ny_tz = pytz.timezone('America/New_York')
    now = datetime.now(ny_tz)
    market_open = dt_time(9, 30)
    market_close = dt_time(16, 0)

    if now.time() < market_open or now.time() > market_close:
        return False
    if now.weekday() > 4:  # Saturday = 5, Sunday = 6
        return False
    return True


def seconds_until_market_open(now: Optional[datetime] = None) -> int:
    """Return seconds until the next market open from the current time."""
    ny_tz = pytz.timezone('America/New_York')
    market_open_time = dt_time(9, 30)
    market_close_time = dt_time(16, 0)

    if now is None:
        now = datetime.now(ny_tz)
    else:
        now = now.astimezone(ny_tz)

    # Market currently open
    if (
        now.weekday() < 5
        and market_open_time <= now.time() < market_close_time
    ):
        return 0

    # Before open on a trading day
    if now.weekday() < 5 and now.time() < market_open_time:
        next_open = ny_tz.localize(datetime.combine(now.date(), market_open_time))
    else:
        # Advance to the next trading day (Mon–Fri)
        next_day = now + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        next_open = ny_tz.localize(datetime.combine(next_day.date(), market_open_time))

    delta = next_open - now
    return max(int(delta.total_seconds()), 0)


# === Step 1: Scan insider BUY trades on stocks $1–$10 ===
def scan_stocks():
    if not is_market_hours():
        logger.info("Market is closed. Skipping scan.")
        return []

    stockdata_key = STOCKDATA_API_KEY or os.getenv("STOCKDATA_API_KEY")
    if not stockdata_key:
        logger.error("Missing STOCKDATA_API_KEY; cannot look up quotes.")
        return []

    # Scrape the insider trading page because Finviz does not expose a JSON API.
    # Reuse the same scrape the API serves so our trading loop and HTTP responses stay in sync.
    insider_trades = get_recent_insider_trades(limit=INSIDER_DEFAULT_LIMIT)
    if not insider_trades:
        logger.info("No insider trades scraped from Finviz.")
        return []

    seen_symbols: set[str] = set()
    qualifying = []
    for entry in insider_trades:
        if entry.get("transaction", "").lower() != "buy":
            continue
        ticker = entry.get("ticker")
        if not ticker or ticker in seen_symbols:
            continue
        seen_symbols.add(ticker)

        try:
            quote_data = rate_limited_request(
                _http_session,
                f"https://api.stockdata.org/v1/data/quote?symbols={ticker}&api_token={stockdata_key}"
            )
            data = quote_data.get("data", []) if isinstance(quote_data, dict) else []
            if not data:
                raise ValueError("Empty price payload")
            price = float(data[0]["price"])

            if 1.0 <= price <= 10.0:
                qualifying.append({"symbol": ticker, "price": price})
        except Exception as e:
            logger.warning(f"Could not fetch price for {ticker}: {e}")
            continue

        if len(qualifying) >= 25:
            break

    logger.info(f"Found {len(qualifying)} qualifying insider-buy stocks.")
    return qualifying


# === Step 2: Execute bracket trades ===
def place_bracket_order(client: TradingClient, symbol: str, price: float, qty: Optional[int] = None):
    if qty is None:
        qty = calculate_position_size(client, price)

    take_profit = round(price * 1.05, 2)
    stop_loss = round(price * 0.98, 2)

    order_request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=take_profit),
        stop_loss=StopLossRequest(stop_price=stop_loss),
    )

    try:
        order = client.submit_order(order_data=order_request)
        logger.info(f"✅ {symbol}: Bought at ${price} | TP ${take_profit} | SL ${stop_loss}")
        return order
    except Exception as e:
        logger.error(f"❌ Failed to place order for {symbol}: {e}")
        return None


# === Step 3: Auto-trade loop ===
def auto_trade():
    if not is_market_hours():
        return

    stocks = scan_stocks()
    if not stocks:
        return

    client = get_trading_client()
    try:
        positions = {position.symbol for position in client.get_all_positions()}
    except Exception as exc:
        logger.warning(f"Could not fetch current positions: {exc}")
        positions = set()

    for stock in stocks:
        symbol = stock["symbol"]
        price = stock["price"]

        if symbol in positions:
            logger.info(f"🔁 Skipping {symbol} — position already open.")
            continue

        place_bracket_order(client, symbol, price)


# Provide a health endpoint so Railway stops reporting 404 on the root path.
@app.get("/")
def root_status():
    return {"status": "ok"}


@app.get("/products.json")
def products(insider: bool = Query(False, description="Return only raw insider trades")):
    try:
        insider_trades = get_recent_insider_trades(limit=INSIDER_DEFAULT_LIMIT)
    except Exception as exc:
        logger.error("Failed to fetch insider trades: %s", exc)
        insider_trades = []

    if insider:
        return {"insider_trades": insider_trades}

    stocks = scan_stocks()
    return {"stocks": stocks, "insider_trades": insider_trades}


@app.get("/insider-trades.json")
def insider_trades(limit: int = Query(INSIDER_DEFAULT_LIMIT, ge=1, le=500)):
    """Expose the raw insider trade scrape so clients can consume it directly."""
    trades = get_recent_insider_trades(limit=limit)
    return {"insider_trades": trades}


# === Main loop ===
if __name__ == "__main__":
    logger.info("Starting Microcap Scout AI — Insider Momentum Cycle")
    while True:
        try:
            wait_seconds = seconds_until_market_open()
            if wait_seconds > 0:
                hours, remainder = divmod(wait_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                logger.info(
                    "Market closed. Next open in %02dh:%02dm:%02ds — sleeping.",
                    hours,
                    minutes,
                    seconds,
                )
                time.sleep(wait_seconds)
                continue

            auto_trade()
            logger.info("Sleeping 15 minutes...")
            time.sleep(900)
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            time.sleep(60)  # Wait a minute before retrying on error
