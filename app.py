import os
import time
import logging
from typing import Optional
from datetime import datetime, time as dt_time
import pytz
from ratelimit import limits, sleep_and_retry

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
from fastapi import FastAPI

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

# API rate limits
CALLS_PER_SECOND = 2
SECONDS_BETWEEN_CALLS = 1

@sleep_and_retry
@limits(calls=CALLS_PER_SECOND, period=SECONDS_BETWEEN_CALLS)
def rate_limited_request(session, url, timeout=10):
    response = session.get(url, timeout=timeout)
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
        api_key = _require_env("APCA_API_KEY_ID")
        api_secret = _require_env("APCA_API_SECRET_KEY")
        _trading_client = TradingClient(api_key, api_secret, paper=True)
    return _trading_client


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


# === Step 1: Scan insider BUY trades on stocks $1–$10 ===
def scan_stocks():
    if not is_market_hours():
        logger.info("Market is closed. Skipping scan.")
        return []

    finviz_token = os.getenv("FINVIZ_TOKEN")
    stockdata_key = os.getenv("STOCKDATA_API_KEY")

    if not finviz_token or not stockdata_key:
        logger.error("Missing API keys")
        return []

    try:
        finviz_data = rate_limited_request(
            _http_session,
            f"https://api.finviz.com/api/insider-trades?token={finviz_token}"
        )
    except Exception as e:
        logger.error(f"Finviz API error: {e}")
        return []

    qualifying = []
    for entry in finviz_data.get("data", []):
        if entry.get("transaction", "").lower() != "buy":
            continue
        ticker = entry.get("ticker")
        if not ticker:
            continue

        try:
            quote_data = rate_limited_request(
                _http_session,
                f"https://api.stockdata.org/v1/data/quote?symbols={ticker}&api_token={stockdata_key}"
            )
            data = quote_data.get("data") or []
            if not data:
                raise ValueError("Empty price payload")
            price = float(data[0]["price"])

            if 1.0 <= price <= 10.0:
                qualifying.append({"symbol": ticker, "price": price})
        except Exception as e:
            logger.warning(f"Could not fetch price for {ticker}: {e}")
            continue

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
        logger.info("Market is closed. Skipping trading cycle.")
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


# === Main loop ===
if __name__ == "__main__":
    logger.info("Starting Microcap Scout AI — Insider Momentum Cycle")
    while True:
        try:
            auto_trade()
            logger.info("Sleeping 15 minutes...")
            time.sleep(900)
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            time.sleep(60)  # Wait a minute before retrying on error
