from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional

import pandas as pd
from fastapi import FastAPI, Query, Response

from data_sources import fetch_fundamentals, get_price_history, get_sentiment, is_rate_limited
from trade_engine import TradeEngine

app = FastAPI(title="Microcap Scout v2", version="3.2.0")
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

CACHE_TTL = 300
SYMBOL_DELAY_SECONDS = float(os.getenv("SYMBOL_DELAY_SECONDS", "2"))
_cache: dict[str, dict] = {}
START_TIME = time.time()

engine = TradeEngine(
    daily_budget=10_000,
    per_trade_budget=1_000,
    max_trades=3,
    stop_loss_percent=0.05,
    take_profit_percent=0.10,
    max_positions=5,
    drawdown_limit_percent=0.10,
    pnl_log_file="daily_pnl.json",
)


def cached_history(symbol: str) -> Optional[pd.Series]:
    entry = _cache.get(symbol)
    now = time.time()
    if entry and now - entry["timestamp"] < CACHE_TTL:
        return entry["series"]

    df = get_price_history(symbol)
    if df is None or df.empty:
        return None

    closes = df["Close"].dropna()
    if closes.empty:
        return None

    series = closes.tail(120)
    _cache[symbol] = {"series": series, "timestamp": now}
    return series


def _compute_indicators(close_series: pd.Series) -> dict:
    closes = close_series.tail(90)
    if closes.empty:
        closes = pd.Series([close_series.iloc[-1]])

    if closes.size < 30:
        closes = pd.concat(
            [closes, pd.Series([closes.iloc[-1]] * (30 - closes.size))], ignore_index=True
        )

    ema9 = closes.ewm(span=9, adjust=False).mean().iloc[-1]
    ema21 = closes.ewm(span=21, adjust=False).mean().iloc[-1]
    trend = "Bullish" if ema9 > ema21 else "Bearish"

    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(window=14, min_periods=14).mean()
    loss = -delta.clip(upper=0).rolling(window=14, min_periods=14).mean()
    if loss.iloc[-1] == 0 or pd.isna(loss.iloc[-1]):
        rsi_value = 50.0
    else:
        rs = gain.iloc[-1] / loss.iloc[-1]
        rsi_value = 100 - (100 / (1 + rs))

    return {
        "ema9": float(ema9),
        "ema21": float(ema21),
        "trend": trend,
        "rsi": round(float(rsi_value), 2),
    }


def analyze(symbols: List[str]) -> List[dict]:
    results = []

    for idx, symbol in enumerate(symbols):
        if is_rate_limited():
            LOGGER.warning("Rate limit active; stopping scan early")
            break

        if SYMBOL_DELAY_SECONDS > 0 and idx != 0:
            time.sleep(SYMBOL_DELAY_SECONDS)

        fundamentals = fetch_fundamentals(symbol)
        if not fundamentals:
            LOGGER.warning(json.dumps({"symbol": symbol, "reason": "missing fundamentals"}))
            continue

        price = fundamentals.get("price")
        market_cap = fundamentals.get("market_cap")
        volume = fundamentals.get("volume", 0)

        if price is None or market_cap is None:
            LOGGER.warning(json.dumps({"symbol": symbol, "reason": "incomplete fundamentals"}))
            continue

        if market_cap > 500_000_000 or volume < 300_000:
            continue

        closes = cached_history(symbol)
        synthetic_history = False
        if closes is None:
            synthetic_history = True
            closes = pd.Series([price] * 30)

        indicators = _compute_indicators(closes)
        trend = indicators["trend"]
        rsi_value = indicators["rsi"]
        sentiment = get_sentiment(symbol)

        action = "hold"
        if rsi_value < 40:
            action = "watch"
        elif rsi_value > 55:
            action = "buy"

        trade_info = None
        if action == "buy":
            trade_info = engine.attempt_trade(symbol, price)

        results.append(
            {
                "symbol": symbol,
                "market_cap": market_cap,
                "volume": volume,
                "price": round(price, 4),
                "trend": trend,
                "rsi": rsi_value,
                "sentiment": sentiment,
                "action": action,
                "synthetic_history": synthetic_history,
                "fundamentals_source": fundamentals.get("source"),
                "trade": trade_info,
            }
        )

    return results


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/status")
def status():
    uptime = time.time() - START_TIME
    return {
        "uptime_seconds": round(uptime, 1),
        "trade_stats": engine.get_status(),
        "rate_limited": is_rate_limited(),
    }


@app.get("/products.json")
def products():
    if is_rate_limited():
        return {"message": "Data temporarily rate limited", "results": []}
    symbols = os.getenv("SCAN_TICKERS", "CEI,BBIG,COSM,GNS,SOBR").split(",")
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    summary = analyze(symbols)
    if not summary:
        return {"message": "Data unavailable", "results": []}
    return {"results": summary}


@app.get("/scan")
def scan(tickers: Optional[str] = Query(None, description="Comma separated tickers")):
    if is_rate_limited():
        return {"message": "Data temporarily rate limited", "results": []}
    if tickers:
        symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()]
    else:
        symbols = os.getenv("SCAN_TICKERS", "CEI,BBIG,COSM,GNS,SOBR").split(",")
        symbols = [s.strip().upper() for s in symbols if s.strip()]
    summary = analyze(symbols)
    if not summary:
        return {"message": "Data unavailable", "results": []}
    return {"results": summary}


@app.get("/trade")
def trade(symbol: str, action: str = "buy"):
    fundamentals = fetch_fundamentals(symbol)
    price = fundamentals.get("price") if fundamentals else None
    if price is None:
        return {"error": "Price unavailable"}
    result = engine.attempt_trade(symbol, price)
    return result
