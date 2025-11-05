from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime
from typing import List, Optional

import pandas as pd
import requests
import yfinance as yf
from alpaca_trade_api import REST
from fastapi import FastAPI, Query, Response
from fastapi_utils.tasks import repeat_every
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import HTTPError, RetryError
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# === Risk Parameters ===
DAILY_BUDGET = 10_000
MAX_TRADE_SIZE = 2_000
MAX_TRADES_PER_DAY = 3
STOP_LOSS_PERCENT = 0.03
TAKE_PROFIT_PERCENT = 0.05
MAX_DRAWDOWN_PERCENT = 0.10
PNL_LOG_FILE = "daily_pnl.json"

# === In-Memory Cache (5 min TTL) ===
cache: dict[str, dict] = {}
CACHE_TTL = 300  # seconds
DEFAULT_TICKERS = ["CEI", "BBIG", "COSM", "GNS", "SOBR"]
CHART_THROTTLE_SECONDS = 1.5
CHART_COOLDOWN_SECONDS = 60
_next_chart_allowed = 0.0

# === Daily Trade Stats ===
trade_stats = {
    "date": str(date.today()),
    "used_capital": 0.0,
    "trades": 0,
    "pnl": 0.0,
    "stopped": False,
}

# === HTTP Session with Retries ===
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=("GET", "HEAD"),
)
session = requests.Session()
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)


def reset_daily_budget() -> None:
    """Reset the daily budget if a new UTC day has started."""
    global trade_stats
    today = str(date.today())
    if trade_stats["date"] != today:
        trade_stats = {
            "date": today,
            "used_capital": 0.0,
            "trades": 0,
            "pnl": 0.0,
            "stopped": False,
        }
        logging.info("Daily budget reset", extra={"event": "reset"})


@app.on_event("startup")
@repeat_every(seconds=86400)
def reset_job() -> None:
    reset_daily_budget()


def get_cached(symbol: str) -> Optional[pd.DataFrame]:
    data = cache.get(symbol)
    if data and (time.time() - data["timestamp"] < CACHE_TTL):
        return data["df"]
    return None


def set_cache(symbol: str, df: pd.DataFrame) -> None:
    cache[symbol] = {"df": df, "timestamp": time.time()}


def _fetch_chart(symbol: str) -> pd.DataFrame:
    global _next_chart_allowed

    wait_seconds = _next_chart_allowed - time.time()
    if wait_seconds > 0:
        time.sleep(min(wait_seconds, CHART_THROTTLE_SECONDS))

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "interval": "1h",
        "range": "5d",
        "includePrePost": "false",
        "events": "div,split",
    }

    response = session.get(url, params=params, timeout=10)
    response.raise_for_status()
    payload = response.json()

    result_set = payload.get("chart", {}).get("result") or []
    if not result_set:
        return pd.DataFrame()

    result = result_set[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators", {}).get("quote", [{}])[0]
    if not timestamps or not indicators:
        return pd.DataFrame()

    df = pd.DataFrame(indicators, index=pd.to_datetime(timestamps, unit="s"))
    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    ).dropna(subset=["Close"]).sort_index()

    _next_chart_allowed = time.time() + CHART_THROTTLE_SECONDS
    return df


def safe_download(symbol: str, retries: int = 3, delay: int = 2) -> pd.DataFrame:
    global _next_chart_allowed
    cooldown_multiplier = 1

    for attempt in range(retries):
        try:
            cached = get_cached(symbol)
            if cached is not None:
                return cached

            df = _fetch_chart(symbol)
            if df.empty:
                df = yf.download(symbol, period="5d", interval="1h", progress=False)

            if not df.empty:
                set_cache(symbol, df)
                return df

        except RetryError:
            logging.warning("Yahoo rate limit hit for %s (attempt %s/%s)", symbol, attempt + 1, retries)
            _next_chart_allowed = time.time() + CHART_COOLDOWN_SECONDS * cooldown_multiplier
            time.sleep(delay * (attempt + 1))
            cooldown_multiplier *= 2
            continue
        except HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 429:
                logging.warning("HTTP 429 for %s (attempt %s/%s)", symbol, attempt + 1, retries)
                _next_chart_allowed = time.time() + CHART_COOLDOWN_SECONDS * cooldown_multiplier
                time.sleep(delay * (attempt + 1))
                cooldown_multiplier *= 2
                continue
            logging.warning("HTTP error for %s: %s", symbol, exc)
            break
        except Exception as exc:
            logging.warning("Retry %s/%s for %s: %s", attempt + 1, retries, symbol, exc)
            time.sleep(delay)

    logging.info("Skipping %s after repeated download failures", symbol)
    return pd.DataFrame()


def get_sentiment(symbol: str) -> str:
    try:
        url = f"https://finviz.com/quote.ashx?t={symbol}"
        headers = {"User-Agent": "Mozilla/5.0"}
        html = session.get(url, headers=headers, timeout=10).text
        if "up" in html or "gain" in html:
            return "Positive"
        return "Neutral"
    except Exception as exc:
        logging.warning("Sentiment fetch failed for %s: %s", symbol, exc)
        return "Neutral"


def analyze_tickers(symbols: List[str]) -> List[dict]:
    results: List[dict] = []
    for symbol in symbols:
        df = safe_download(symbol)
        if df.empty:
            continue

        closes = df["Close"].dropna()
        if closes.empty:
            continue

        ema9 = EMAIndicator(closes, window=9).ema_indicator().iloc[-1]
        ema21 = EMAIndicator(closes, window=21).ema_indicator().iloc[-1]
        rsi = RSIIndicator(closes, window=14).rsi().iloc[-1]

        trend = "Bullish" if ema9 > ema21 else "Bearish"
        sentiment = get_sentiment(symbol)
        sentiment_boost = 0.1 if sentiment == "Positive" else 0

        score = ((rsi / 100) + (1 if trend == "Bullish" else 0) + sentiment_boost) / 2
        results.append(
            {
                "symbol": symbol,
                "rsi": round(float(rsi), 2),
                "trend": trend,
                "sentiment": sentiment,
                "score": round(float(score), 2),
            }
        )

    results.sort(key=lambda item: item["score"], reverse=True)
    return results


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/products.json")
def products():
    reset_daily_budget()
    results = analyze_tickers(DEFAULT_TICKERS)
    if not results:
        return {"message": "Data unavailable"}
    return {"results": results}


@app.get("/scan")
def scan_microcaps(tickers: Optional[str] = Query(None, description="Comma separated list of tickers to scan")):
    reset_daily_budget()
    try:
        symbols = [ticker.strip().upper() for ticker in tickers.split(",")] if tickers else DEFAULT_TICKERS
        results = analyze_tickers(symbols)
        if not results:
            return {"message": "Data unavailable"}
        return {"results": results}
    except Exception as exc:
        logging.error("Error scanning microcaps: %s", exc)
        return {"error": str(exc)}


@app.get("/trade")
def trade_signal(symbol: str, action: str = "buy"):
    reset_daily_budget()

    if trade_stats["stopped"]:
        logging.info("Trading halted due to drawdown", extra={"stats": trade_stats})
        return {"error": "Daily drawdown limit reached", "stats": trade_stats}

    if trade_stats["trades"] >= MAX_TRADES_PER_DAY:
        return {"error": "Daily trade count exceeded", "stats": trade_stats}

    capital_remaining = DAILY_BUDGET - trade_stats["used_capital"]
    if capital_remaining < 1:
        return {"error": "Daily limit reached", "stats": trade_stats}

    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return {"error": "Missing Alpaca credentials"}

    api = REST(key, secret, base_url="https://paper-api.alpaca.markets")

    price: Optional[float] = None
    try:
        latest = api.get_latest_trade(symbol)
        price = float(latest.price)
    except Exception as exc:
        logging.warning("Alpaca price fetch failed for %s: %s", symbol, exc)

    if price is None:
        df = yf.download(symbol, period="1d", interval="1m", progress=False)
        if not df.empty:
            price = float(df["Close"].iloc[-1])

    if price is None or price <= 0:
        return {"error": "Unable to determine price", "symbol": symbol}

    qty = max(1, int(MAX_TRADE_SIZE // price))
    if qty == 0:
        return {"error": "Price too high for risk limits", "stats": trade_stats}

    trade_value = price * qty
    if trade_value > MAX_TRADE_SIZE:
        return {"error": "Trade size exceeds per-trade limit", "stats": trade_stats}

    if trade_stats["used_capital"] + trade_value > DAILY_BUDGET:
        return {"error": "Daily limit reached", "stats": trade_stats}

    if action.lower() != "buy":
        return {"error": "Only buy orders are supported in risk-managed mode"}

    tp = round(price * (1 + TAKE_PROFIT_PERCENT), 2)
    sl = round(price * (1 - STOP_LOSS_PERCENT), 2)

    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="limit",
            limit_price=price,
            time_in_force="gtc",
            take_profit={"limit_price": tp},
            stop_loss={"stop_price": sl},
        )
        logging.info("Submitted trade", extra={"symbol": symbol, "qty": qty, "price": price})
    except Exception as exc:
        logging.error("Trade failed for %s: %s", symbol, exc)
        return {"error": str(exc)}

    trade_stats["used_capital"] += trade_value
    trade_stats["trades"] += 1

    try:
        account = api.get_account()
        daily_pnl = float(account.equity) - float(account.last_equity)
        trade_stats["pnl"] = daily_pnl
        if daily_pnl < -DAILY_BUDGET * MAX_DRAWDOWN_PERCENT:
            trade_stats["stopped"] = True
            logging.info("Trading halted due to drawdown", extra={"stats": trade_stats})

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "stats": trade_stats.copy(),
        }
        with open(PNL_LOG_FILE, "a", encoding="utf-8") as f:
            json.dump(entry, f)
            f.write("\n")
    except Exception as exc:
        logging.warning("PnL logging failed: %s", exc)

    return {
        "status": "order placed",
        "symbol": symbol,
        "qty": qty,
        "price": round(price, 4),
        "take_profit": tp,
        "stop_loss": sl,
        "stats": trade_stats,
    }
