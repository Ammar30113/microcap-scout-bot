import logging
import os
from typing import List, Optional

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

app = FastAPI(title="Microcap Scout", version="2.0.0")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)

DEFAULT_TICKERS: List[str] = ["CEI", "BBIG", "COSM", "GNS", "SOBR"]
FINVIZ_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _compute_sentiment(symbol: str) -> str:
    """Fetch a lightweight sentiment signal from Finviz headlines."""
    try:
        url = f"https://finviz.com/quote.ashx?t={symbol}"
        response = requests.get(url, headers=FINVIZ_HEADERS, timeout=8)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        headlines = [element.get_text(strip=True) for element in soup.select(".news-link-left")]
        sample = headlines[:5]
        if any(keyword in headline.lower() for headline in sample for keyword in ("up", "gain", "surge", "upgrade")):
            return "Positive"
        if any(keyword in headline.lower() for headline in sample for keyword in ("down", "loss", "drop", "downgrade")):
            return "Negative"
        return "Neutral"
    except Exception as exc:  # pragma: no cover - external network
        LOGGER.debug("Sentiment lookup failed for %s: %s", symbol, exc)
        return "Neutral"


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/scan")
def scan_microcaps(tickers: Optional[str] = Query(None, description="Comma separated list of tickers to scan")):
    try:
        symbols = [ticker.strip().upper() for ticker in (tickers.split(",") if tickers else DEFAULT_TICKERS)]
        results = []

        for symbol in symbols:
            try:
                df = yf.download(symbol, period="5d", interval="1h", progress=False)
            except Exception as pull_err:  # pragma: no cover - external network
                LOGGER.warning("Failed to download data for %s: %s", symbol, pull_err)
                continue

            if df.empty:
                continue

            closes = df["Close"].dropna()
            if closes.empty:
                continue

            ema9 = EMAIndicator(close=closes, window=9).ema_indicator().iloc[-1]
            ema21 = EMAIndicator(close=closes, window=21).ema_indicator().iloc[-1]
            rsi_value = RSIIndicator(close=closes, window=14).rsi().iloc[-1]

            trend = "Bullish" if ema9 > ema21 else "Bearish"
            base_score = (rsi_value / 100)
            trend_boost = 0.5 if trend == "Bullish" else 0.25

            sentiment = _compute_sentiment(symbol)
            sentiment_boost = 0.25 if sentiment == "Positive" else 0.0
            sentiment_boost = -0.1 if sentiment == "Negative" else sentiment_boost

            score = max(0.0, min(1.0, base_score * 0.5 + trend_boost + sentiment_boost))

            results.append(
                {
                    "symbol": symbol,
                    "rsi": round(float(rsi_value), 2),
                    "trend": trend,
                    "sentiment": sentiment,
                    "score": round(score, 2),
                }
            )

        ranked = sorted(results, key=lambda item: item["score"], reverse=True)
        if not ranked:
            return {"message": "Data unavailable"}

        return {"results": ranked}

    except Exception as exc:
        LOGGER.error("Error scanning microcaps: %s", exc)
        return {"error": str(exc)}


@app.get("/trade")
def trade_signal(symbol: str, action: str = "buy"):
    from alpaca_trade_api import REST

    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")

    if not key or not secret:
        return {"error": "Missing Alpaca credentials"}

    api = REST(key, secret, base_url="https://paper-api.alpaca.markets")
    qty = 10

    try:
        if action.lower() == "buy":
            api.submit_order(symbol=symbol, qty=qty, side="buy", type="market", time_in_force="day")
        else:
            api.submit_order(symbol=symbol, qty=qty, side="sell", type="market", time_in_force="day")
    except Exception as exc:  # pragma: no cover - external network
        LOGGER.error("Failed to place trade for %s: %s", symbol, exc)
        return {"error": str(exc)}

    return {"status": f"{action} order placed for {symbol}"}
