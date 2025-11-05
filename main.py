from typing import List, Optional
import logging
import os
import time

import pandas as pd
import requests
import yfinance as yf
from alpaca_trade_api import REST
from fastapi import FastAPI, Query, Response
from requests.adapters import HTTPAdapter, Retry
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

cache = {}
CACHE_TTL = 300  # seconds
DEFAULT_TICKERS = ["CEI", "BBIG", "COSM", "GNS", "SOBR"]

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


def get_cached(symbol: str) -> Optional[pd.DataFrame]:
    data = cache.get(symbol)
    if data and (time.time() - data["timestamp"] < CACHE_TTL):
        return data["df"]
    return None


def set_cache(symbol: str, df: pd.DataFrame) -> None:
    cache[symbol] = {"df": df, "timestamp": time.time()}




def _fetch_chart(symbol: str) -> pd.DataFrame:
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
    rename_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    df = df.rename(columns=rename_map)
    return df.dropna(subset=["Close"]).sort_index()

def safe_download(symbol: str, retries: int = 3, delay: int = 2) -> pd.DataFrame:
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
        except Exception as exc:
            logging.warning(f"Retry {attempt + 1}/{retries} for {symbol}: {exc}")
            time.sleep(delay)
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
        logging.warning(f"Sentiment fetch failed for {symbol}: {exc}")
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
    results = analyze_tickers(DEFAULT_TICKERS)
    if not results:
        return {"message": "Data unavailable"}
    return {"results": results}


@app.get("/scan")
def scan_microcaps(tickers: Optional[str] = Query(None, description="Comma separated list of tickers to scan")):
    try:
        symbols = [ticker.strip().upper() for ticker in tickers.split(",")] if tickers else DEFAULT_TICKERS
        results = analyze_tickers(symbols)
        if not results:
            return {"message": "Data unavailable"}
        return {"results": results}
    except Exception as exc:
        logging.error(f"Error scanning microcaps: {exc}")
        return {"error": str(exc)}


@app.get("/trade")
def trade_signal(symbol: str, action: str = "buy"):
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return {"error": "Missing Alpaca credentials"}

    try:
        api = REST(key, secret, base_url="https://paper-api.alpaca.markets")
        qty = 10
        api.submit_order(symbol=symbol, qty=qty, side=action, type="market", time_in_force="day")
        return {"status": f"{action} order placed for {symbol}"}
    except Exception as exc:
        logging.error(f"Trade failed: {exc}")
        return {"error": str(exc)}
