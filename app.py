import os
import time
from typing import Optional

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
from fastapi import FastAPI

app = FastAPI()

_trading_client: Optional[TradingClient] = None
_http_session = requests.Session()


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

# === Step 1: Scan insider BUY trades on stocks $1–$10 ===
def scan_stocks():
    finviz_token = os.getenv("FINVIZ_TOKEN")
    stockdata_key = os.getenv("STOCKDATA_API_KEY")

    if not finviz_token:
        print("⚠️ FINVIZ_TOKEN not set; skipping insider scan.")
        return []
    if not stockdata_key:
        print("⚠️ STOCKDATA_API_KEY not set; skipping insider scan.")
        return []

    finviz_url = f"https://api.finviz.com/api/insider-trades?token={finviz_token}"
    try:
        finviz_response = _http_session.get(finviz_url, timeout=10)
        finviz_response.raise_for_status()
        finviz_data = finviz_response.json()
    except Exception as e:
        print(f"❌ Finviz API error: {e}")
        return []

    qualifying = []
    for entry in finviz_data.get("data", []):
        if entry.get("transaction", "").lower() != "buy":
            continue
        ticker = entry.get("ticker")
        if not ticker:
            continue

        try:
            quote_url = (
                f"https://api.stockdata.org/v1/data/quote?symbols={ticker}&api_token={stockdata_key}"
            )
            quote_response = _http_session.get(quote_url, timeout=10)
            quote_response.raise_for_status()
            quote_data = quote_response.json()
            data = quote_data.get("data") or []
            if not data:
                raise ValueError("Empty price payload")
            price = float(data[0]["price"])
        except Exception as e:
            print(f"⚠️ Could not fetch price for {ticker}: {e}")
            continue

        if 1.0 <= price <= 10.0:
            qualifying.append({"symbol": ticker, "price": price})

    print(f"📊 Found {len(qualifying)} qualifying insider-buy stocks.")
    return qualifying

# === Step 2: Execute bracket trades ===
def place_bracket_order(client: TradingClient, symbol: str, price: float, qty: int = 10):
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
        print(f"✅ {symbol}: Bought at ${price} | TP ${take_profit} | SL ${stop_loss}")
        return order
    except Exception as e:
        print(f"❌ Failed to place order for {symbol}: {e}")
        return None

# === Step 3: Auto-trade loop ===
def auto_trade():
    stocks = scan_stocks()
    if not stocks:
        return

    client = get_trading_client()
    try:
        positions = {position.symbol for position in client.get_all_positions()}
    except Exception as exc:
        print(f"⚠️ Could not fetch current positions: {exc}")
        positions = set()

    for stock in stocks:
        symbol = stock["symbol"]
        price = stock["price"]

        if symbol in positions:
            print(f"🔁 Skipping {symbol} — position already open.")
            continue

        place_bracket_order(client, symbol, price)

# === Main loop ===
if __name__ == "__main__":
    while True:
        print("🚀 Running Microcap Scout AI — Insider Momentum Cycle")
        auto_trade()
        print("💤 Sleeping 15 minutes...\n")
        time.sleep(900)
