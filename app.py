from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# --- Read API keys from Railway environment variables ---
ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")

# Initialize Alpaca client (Paper trading mode)
client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)

app = FastAPI()

# --- Define payload structure to match TradingView webhook ---
class Alert(BaseModel):
    event: str
    symbol: str
    side: str
    price: float | None = None

@app.get("/")
async def root():
    return {"status": "ok", "timestamp": str(__import__("datetime").datetime.utcnow())}

# --- TradingView webhook endpoint ---
@app.post("/")
async def handle_alert(alert: Alert, request: Request):
    """Handle incoming TradingView webhook alerts and submit orders to Alpaca."""
    print(f"📩 TradingView Webhook Received: {alert.dict()}")

    # Determine order side
    side = OrderSide.BUY if alert.side.upper() == "BUY" else OrderSide.SELL

    # Create and submit a market order
    order = MarketOrderRequest(
        symbol=alert.symbol,
        qty=100,
        side=side,
        time_in_force=TimeInForce.DAY
    )

    try:
        resp = client.submit_order(order)
        print(f"✅ Order placed: {alert.symbol} ({alert.side}) @ {alert.price}")
        return {"status": "executed", "symbol": alert.symbol, "side": alert.side, "order_id": str(resp.id)}
    except Exception as e:
        print(f"❌ Order failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


