from fastapi import FastAPI
import yfinance as yf
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
import logging
import os
import time
from alpaca_trade_api import REST
import requests
from requests.adapters import HTTPAdapter, Retry

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# === In-Memory Cache (5 min TTL) ===
cache = {}
CACHE_TTL = 300  # seconds

# === Shared HTTP session with retries ===
retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session = requests.Session()
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)


def get_cached(symbol):
    data = cache.get(symbol)
    if data and (time.time() - data["timestamp"] < CACHE_TTL):
        return data["df"]
    return None


def set_cache(symbol, df):
    cache[symbol] = {"df": df, "timestamp": time.time()}


# === Resilient Downloader ===
def safe_download(symbol, retries=3, delay=2):
    for attempt in range(retries):
        try:
            cached = get_cached(symbol)
            if cached is not None:
                return cached
            df = yf.download(symbol, period="5d", interval="1h", progress=False)
            if not df.empty:
                set_cache(symbol, df)
                return df
        except Exception as e:
            logging.warning(f"Retry {attempt+1}/{retries} for {symbol}: {e}")
            time.sleep(delay)
    return pd.DataFrame()


# === Optional Finviz Sentiment ===
def get_sentiment(symbol):
    try:
        url = f"https://finviz.com/quote.ashx?t={symbol}"
        headers = {"User-Agent": "Mozilla/5.0"}
        html = session.get(url, headers=headers, timeout=10).text
        if "up" in html or "gain" in html:
            return "Positive"
        return "Neutral"
    except Exception as e:
        logging.warning(f"Sentiment fetch failed for {symbol}: {e}")
        return "Neutral"


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/scan")
def scan_microcaps():
    try:
        tickers = ["CEI", "BBIG", "COSM", "GNS", "SOBR"]
        results = []

        for symbol in tickers:
            df = safe_download(symbol)
            if df.empty:
                continue

            ema9 = EMAIndicator(df['Close'], window=9).ema_indicator().iloc[-1]
            ema21 = EMAIndicator(df['Close'], window=21).ema_indicator().iloc[-1]
            rsi = RSIIndicator(df['Close'], window=14).rsi().iloc[-1]

            trend = "Bullish" if ema9 > ema21 else "Bearish"
            sentiment = get_sentiment(symbol)
            sentiment_boost = 0.1 if sentiment == "Positive" else 0

            score = ((rsi/100) + (1 if trend=="Bullish" else 0) + sentiment_boost) / 2
            results.append({
                "symbol": symbol,
                "rsi": round(rsi, 2),
                "trend": trend,
                "sentiment": sentiment,
                "score": round(score, 2)
            })

        ranked = sorted(results, key=lambda x: x["score"], reverse=True)
        return {"results": ranked}

    except Exception as e:
        logging.error(f"Error scanning microcaps: {e}")
        return {"error": str(e)}


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
    except Exception as e:
        logging.error(f"Trade failed: {e}")
        return {"error": str(e)}
