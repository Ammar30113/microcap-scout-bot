import logging
import os
import time
from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import pytz
from ratelimit import limits, sleep_and_retry

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
from fastapi import FastAPI, Query

if TYPE_CHECKING:
    import pandas as pd

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

FINVIZ_FILTER_ENDPOINT = "https://api.finviz.com/api/filter"
FINVIZ_INSIDER_ENDPOINT = "https://api.finviz.com/api/insider-trades"
FINVIZ_DEFAULT_LIMIT = 100


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_volume(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    cleaned = value.strip().replace(",", "").upper()
    if not cleaned:
        return None
    multiplier = 1
    if cleaned.endswith("M"):
        multiplier = 1_000_000
        cleaned = cleaned[:-1]
    elif cleaned.endswith("K"):
        multiplier = 1_000
        cleaned = cleaned[:-1]
    try:
        return int(float(cleaned) * multiplier)
    except ValueError:
        return None


def _parse_percentage(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    cleaned = value.strip().replace("%", "").replace("+", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_first(entry: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for key in keys:
        value = entry.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _summarize_trades(trades: List[Dict[str, Any]]) -> Optional[str]:
    if not trades:
        return None
    buys = sum(
        1 for trade in trades if str(trade.get("transaction", "")).lower() == "buy"
    )
    sells = sum(
        1 for trade in trades if str(trade.get("transaction", "")).lower() == "sell"
    )
    latest = trades[0]
    latest_date = latest.get("date") or "recent"
    return f"{buys} buys / {sells} sells (latest {latest_date})"


def _finviz_filter_request(token: str, filters: str, limit: int) -> List[Dict[str, Any]]:
    params = {
        "token": token,
        "type": "stock",
        "filters": filters,
        "limit": limit,
    }
    try:
        payload = rate_limited_request(
            _http_session,
            FINVIZ_FILTER_ENDPOINT,
            params=params,
        )
    except Exception as exc:
        logger.error("Finviz filter request failed (%s): %s", filters, exc)
        return []

    if not isinstance(payload, dict):
        logger.warning("Unexpected Finviz filter response format.")
        return []

    data = payload.get("data")
    if not isinstance(data, list):
        return []

    return [row for row in data if isinstance(row, dict)]


def _finviz_insider_trades(token: str, limit: int = 200) -> Dict[str, List[Dict[str, Any]]]:
    params = {"token": token, "limit": limit}
    try:
        payload = rate_limited_request(
            _http_session,
            FINVIZ_INSIDER_ENDPOINT,
            params=params,
        )
    except Exception as exc:
        logger.warning("Finviz insider trades lookup failed: %s", exc)
        return {}

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return {}

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker")
        if not ticker:
            continue
        grouped[ticker].append(entry)
    return grouped


@sleep_and_retry
@limits(calls=CALLS_PER_SECOND, period=SECONDS_BETWEEN_CALLS)
def rate_limited_request(session, url, timeout=10, **kwargs):
    response = session.get(url, timeout=timeout, **kwargs)
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


def scan_finviz_insider_stocks(
    return_dataframe: bool = False,
    limit: int = FINVIZ_DEFAULT_LIMIT,
) -> Union[List[Dict[str, Any]], "pd.DataFrame"]:
    token = os.getenv("FINVIZ_TOKEN")
    if not token:
        logger.warning("FINVIZ_TOKEN is not configured; returning no insider stocks.")
        return _coerce_dataframe([], return_dataframe)

    base_filter = "sh_price_u10,sh_avgvol_o300"
    filter_variants = [
        ("ownership", f"{base_filter},sh_insiderown_o10"),
        ("net_buying", f"{base_filter},sh_insidertranspositive"),
    ]

    candidates: Dict[str, Dict[str, Any]] = {}
    for tag, filter_string in filter_variants:
        rows = _finviz_filter_request(token, filter_string, limit)
        for row in rows:
            symbol = _extract_first(row, ["ticker", "symbol"])
            if not symbol:
                continue

            entry = candidates.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "price": None,
                    "avg_volume": None,
                    "insider_ownership": None,
                    "insider_activity": None,
                    "_sources": set(),
                },
            )
            entry["_sources"].add(tag)

            price = _parse_float(_extract_first(row, ["price", "last", "lastSale"]))
            if price is not None:
                entry["price"] = price

            avg_volume = _parse_volume(
                _extract_first(row, ["avgVolume", "averageVolume", "volume"])
            )
            if avg_volume is not None:
                entry["avg_volume"] = avg_volume

            insider_own_str = _extract_first(
                row, ["insiderOwn", "insiderOwnership", "insider_ownership"]
            )
            ownership = _parse_percentage(insider_own_str)
            if ownership is not None:
                entry["insider_ownership"] = ownership

            insider_trans = _extract_first(
                row, ["insiderTrans", "insiderTransactions", "insider_activity"]
            )
            if insider_trans:
                entry["insider_activity"] = insider_trans.strip()

    if not candidates:
        return _coerce_dataframe([], return_dataframe)

    trades_map = _finviz_insider_trades(token)

    results: List[Dict[str, Any]] = []
    for symbol, entry in candidates.items():
        sources = entry.get("_sources", set())
        price = entry.get("price")
        avg_volume = entry.get("avg_volume")

        if price is None or price > 10:
            continue
        if avg_volume is not None and avg_volume < 300_000:
            continue
        if not sources:
            continue

        if not entry.get("insider_activity"):
            summary = _summarize_trades(trades_map.get(symbol, []))
            if summary:
                entry["insider_activity"] = summary

        record = {
            "symbol": symbol,
            "price": price,
            "avg_volume": avg_volume,
            "insider_ownership": entry.get("insider_ownership"),
            "insider_activity": entry.get("insider_activity")
            or "No recent insider activity",
        }
        results.append(record)

    results.sort(key=lambda item: item["symbol"])

    return _coerce_dataframe(results, return_dataframe)


def _coerce_dataframe(
    data: List[Dict[str, Any]], return_dataframe: bool
) -> Union[List[Dict[str, Any]], "pd.DataFrame"]:
    if not return_dataframe:
        return data
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        logger.warning("pandas is not available; falling back to JSON list.")
        return data
    return pd.DataFrame(data)


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


@app.get("/products.json")
def products(insider: bool = Query(False, description="Return only insider-filtered stocks")):
    try:
        insider_stocks = scan_finviz_insider_stocks(return_dataframe=False)
    except Exception as exc:
        logger.error("Failed to fetch insider stocks: %s", exc)
        insider_stocks = []

    if insider:
        return {"insider_stocks": insider_stocks}

    stocks = scan_stocks()
    return {"stocks": stocks, "insider_stocks": insider_stocks}


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
