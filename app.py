from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# Read API keys from environment variables
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")

# Initialize Alpaca client in paper mode
client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)

app = FastAPI()

class Alert(BaseModel):
    event: str
    symbol: str
    side: str
    price: float | None = None

def penny_risk_checks(symbol: str, est_price: float):
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

# Example test:
# curl -X POST https://microcap-scout-bot-nazesthetic.replit.app/tv \
#   -H "Content-Type: application/json" \
#   -d '{"event": "ENTRY", "symbol": "TSLA", "side": "BUY", "price": 19.8}'
