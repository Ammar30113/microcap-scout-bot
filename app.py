from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# Read API keys from environment variables
ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")

# Initialize Alpaca client in paper mode
client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)

app = FastAPI()

class Alert(BaseModel):
    event: str
    symbol: str
    side: str
    price: float | None = None

class AISignal(BaseModel):
    symbol: str
    action: str
    qty: int | None = 1

def penny_risk_checks(symbol: str, est_price: float | None):
    # Basic risk checks for penny stocks
    if est_price is None or est_price < 1.0:
        raise HTTPException(status_code=400, detail="Price too low for trade")
    # Additional risk checks can be added here

@app.post("/tv")
async def tradingview_webhook(alert: Alert, request: Request):
    """Handle incoming TradingView webhook alerts and submit orders to Alpaca."""
    # Determine side based on event
    if alert.event == "ENTRY":
        side = OrderSide.BUY
    elif alert.event == "EXIT":
        side = OrderSide.SELL
    else:
        raise HTTPException(status_code=400, detail="Unknown event")

    # Run penny stock risk checks
    penny_risk_checks(alert.symbol, alert.price)

    order = MarketOrderRequest(
        symbol=alert.symbol,
        qty=100,
        side=side,
        time_in_force=TimeInForce.DAY
    )
    resp = client.submit_order(order)
    return {"ok": True, "order_id": str(resp.id)}

@app.post("/ai_signal")
async def ai_signal(signal: AISignal, request: Request):
    """Handle AI-generated trading signals."""
    action = signal.action.upper()
    qty = signal.qty or 1
    if action not in ["BUY", "SELL"]:
        raise HTTPException(status_code=400, detail="Invalid action")
    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL

    order = MarketOrderRequest(
        symbol=signal.symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY
    )
    resp = client.submit_order(order)

@app.get("/")
async def root():
    return {"message": "Microcap Scout Bot API is running"}

@app.get("/products.json")
async def products():
    return {"products": []}
    return {"status": "executed", "symbol": signal.symbol, "action": action, "order_id": str(resp.id)}
