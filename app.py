from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import os, json
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from datetime import datetime

# === API Keys from Railway Environment Variables ===
ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")

# Initialize Alpaca client in paper mode
client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)

# === Initialize FastAPI app ===
app = FastAPI()

# --- Simple heartbeat check ---
@app.get("/")
async def root():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# --- Webhook Model (for TradingView Alerts) ---
class AlertPayload(BaseModel):
    ticker: str
    signal: str
    price: float | None = None


# --- Webhook Route ---
@app.post("/")
async def webhook(request: Request):
    try:
        data = await request.json()
        print(f"📩 TradingView Webhook Received: {json.dumps(data, indent=2)}")

        alert = AlertPayload(**data)
        ticker = alert.ticker
        signal = alert.signal.upper()

        if signal not in ["BUY", "SELL"]:
            raise HTTPException(status_code=400, detail="Invalid signal type")

        side = OrderSide.BUY if signal == "BUY" else OrderSide.SELL

        order = MarketOrderRequest(
            symbol=ticker,
            qty=1,
            side=side,
            time_in_force=TimeInForce.GTC
        )

        client.submit_order(order)
        print(f"✅ Order placed: {signal} {ticker}")

        return {"status": "success", "symbol": ticker, "action": signal}

    except Exception as e:
        print(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

