import os
import time
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from fastapi import FastAPI
app = FastAPI()
# === Load environment variables ===
ALPACA_API_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
FINVIZ_TOKEN = os.getenv("FINVIZ_TOKEN")
STOCKDATA_API_KEY = os.getenv("STOCKDATA_API_KEY")

# === Alpaca client ===
client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)

# === Step 1: Scan insider BUY trades on stocks $1–$10 ===
def scan_stocks():
    finviz_url = f"https://api.finviz.com/api/insider-trades?token={FINVIZ_TOKEN}"
    try:
        finviz_data = requests.get(finviz_url, timeout=10).json()
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
            quote_url = f"https://api.stockdata.org/v1/data/quote?symbols={ticker}&api_token={STOCKDATA_API_KEY}"
            quote_data = requests.get(quote_url, timeout=10).json()
            price = float(quote_data["data"][0]["price"])
        except Exception as e:
            print(f"⚠️ Could not fetch price for {ticker}: {e}")
            continue

        if 1.0 <= price <= 10.0:
            qualifying.append({"symbol": ticker, "price": price})

    print(f"📊 Found {len(qualifying)} qualifying insider-buy stocks.")
    return qualifying

# === Step 2: Execute bracket trades ===
def place_bracket_order(symbol: str, price: float, qty: int = 10):
    take_profit = round(price * 1.05, 2)
    stop_loss = round(price * 0.98, 2)

    try:
        order = client.submit_order(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            take_profit={"limit_price": take_profit},
            stop_loss={"stop_price": stop_loss}
        )
        print(f"✅ {symbol}: Bought at ${price} | TP ${take_profit} | SL ${stop_loss}")
        return order
    except Exception as e:
        print(f"❌ Failed to place order for {symbol}: {e}")
        return None

# === Step 3: Auto-trade loop ===
def auto_trade():
    stocks = scan_stocks()
    for stock in stocks:
        symbol = stock["symbol"]
        price = stock["price"]

        # Optional: skip if position already open
        positions = [p.symbol for p in client.get_all_positions()]
        if symbol in positions:
            print(f"🔁 Skipping {symbol} — position already open.")
            continue

        place_bracket_order(symbol, price)

# === Main loop ===
if __name__ == "__main__":
    while True:
        print("🚀 Running Microcap Scout AI — Insider Momentum Cycle")
        auto_trade()
        print("💤 Sleeping 15 minutes...\n")
        time.sleep(900)



