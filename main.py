import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volume import VolumeWeightedAveragePrice

app = FastAPI(title="Microcap Scout", version="2.1.0")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "symbol"):
            payload["symbol"] = getattr(record, "symbol")
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
LOGGER = logging.getLogger("microcap_scout")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
LOGGER.addHandler(handler)

DEFAULT_TICKERS: List[str] = ["CEI", "BBIG", "COSM", "GNS", "SOBR"]
FINVIZ_HEADERS = {"User-Agent": "Mozilla/5.0"}
STOCKTWITS_STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
STOCKTWITS_LIMIT = 60

CACHE_TTL_SECONDS = 300
SCAN_CACHE: Dict[str, Optional[object]] = {"timestamp": None, "results": None, "symbols": None}
START_TIME = time.time()
LAST_SCAN_TIME: Optional[datetime] = None


def _fetch_stocktwits_sentiment(symbol: str) -> Dict[str, object]:
    bullish = bearish = 0
    scores: List[int] = []

    try:
        response = requests.get(
            STOCKTWITS_STREAM_URL.format(symbol=symbol),
            params={"per_page": STOCKTWITS_LIMIT},
            timeout=10,
        )
        response.raise_for_status()
        messages = response.json().get("messages", [])
    except Exception as exc:  # pragma: no cover - external network
        LOGGER.debug("Stocktwits sentiment fetch failed", extra={"symbol": symbol, "exc": str(exc)})
        return {"sentiment": "Neutral", "sentiment_score": 0, "bullish": 0, "bearish": 0, "source": "Stocktwits"}

    for message in messages[:STOCKTWITS_LIMIT]:
        sentiment = message.get("entities", {}).get("sentiment", {}).get("basic")
        if sentiment == "Bullish":
            bullish += 1
            scores.append(1)
        elif sentiment == "Bearish":
            bearish += 1
            scores.append(-1)
        else:
            scores.append(0)

    recent_scores = scores[:20]
    avg_recent = sum(recent_scores) / len(recent_scores) if recent_scores else 0.0
    sentiment_score = int(round(avg_recent * 100))

    if sentiment_score > 20:
        label = "Positive"
    elif sentiment_score < -20:
        label = "Negative"
    else:
        label = "Neutral"

    return {
        "sentiment": label,
        "sentiment_score": sentiment_score,
        "bullish": bullish,
        "bearish": bearish,
        "source": "Stocktwits",
    }


def _fallback_finviz_sentiment(symbol: str) -> str:
    try:
        url = f"https://finviz.com/quote.ashx?t={symbol}"
        response = requests.get(url, headers=FINVIZ_HEADERS, timeout=8)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        headlines = [element.get_text(strip=True).lower() for element in soup.select(".news-link-left")][:5]
        if any(word in headline for word in ("up", "gain", "surge", "upgrade") for headline in headlines):
            return "Positive"
        if any(word in headline for word in ("down", "loss", "drop", "downgrade") for headline in headlines):
            return "Negative"
        return "Neutral"
    except Exception as exc:  # pragma: no cover - external network
        LOGGER.debug("Finviz sentiment fallback failed", extra={"symbol": symbol, "exc": str(exc)})
        return "Neutral"


def _compute_sentiment(symbol: str) -> Dict[str, object]:
    sentiment = _fetch_stocktwits_sentiment(symbol)
    if sentiment["sentiment"] == "Neutral" and not sentiment["bullish"] and not sentiment["bearish"]:
        # Only use Finviz fallback if Stocktwits returned no tagged messages
        sentiment["sentiment"] = _fallback_finviz_sentiment(symbol)
        sentiment["source"] = "Finviz"
    return sentiment


def _volume_spike(volumes: pd.Series) -> bool:
    if len(volumes) < 6:
        return False
    recent = volumes.iloc[-1]
    avg_prev = volumes.iloc[-6:-1].mean()
    if pd.isna(avg_prev) or avg_prev == 0:
        return False
    return bool(recent > avg_prev * 1.5)


def _calculate_score(
    rsi_value: float,
    trend: str,
    macd_diff: float,
    spike: bool,
    sentiment_label: str,
    sentiment_score: int,
) -> float:
    score = 0.0
    score += max(0.0, min(0.3, (rsi_value / 100) * 0.3))
    if trend == "Bullish":
        score += 0.2
    if macd_diff > 0:
        score += 0.2
    if spike:
        score += 0.15
    if sentiment_label == "Positive":
        score += 0.1
    elif sentiment_label == "Negative":
        score -= 0.05
    score += max(-0.1, min(0.1, sentiment_score / 1000))
    return round(max(0.0, min(1.0, score)), 2)


def _run_scan(symbols: List[str]) -> List[Dict[str, object]]:
    global LAST_SCAN_TIME
    results: List[Dict[str, object]] = []

    for symbol in symbols:
        try:
            df = yf.download(symbol, period="5d", interval="1h", progress=False)
        except Exception as exc:  # pragma: no cover - external network
            LOGGER.warning(f"Download failed: {exc}", extra={"symbol": symbol})
            continue

        if df.empty or df["Close"].dropna().empty:
            LOGGER.info("No data returned", extra={"symbol": symbol})
            continue

        closes = df["Close"].dropna()
        highs = df["High"].dropna()
        lows = df["Low"].dropna()
        volumes = df["Volume"].fillna(0)

        rsi_value = float(RSIIndicator(close=closes, window=14).rsi().iloc[-1])
        ema9 = float(EMAIndicator(close=closes, window=9).ema_indicator().iloc[-1])
        ema21 = float(EMAIndicator(close=closes, window=21).ema_indicator().iloc[-1])
        trend = "Bullish" if ema9 > ema21 else "Bearish"

        macd_indicator = MACD(close=closes)
        macd_value = float(macd_indicator.macd().iloc[-1])
        macd_signal = float(macd_indicator.macd_signal().iloc[-1])
        macd_diff = float(macd_indicator.macd_diff().iloc[-1])

        vwap_indicator = VolumeWeightedAveragePrice(
            high=highs, low=lows, close=closes, volume=volumes, window=14
        )
        vwap_value = float(vwap_indicator.volume_weighted_average_price().iloc[-1])

        spike = _volume_spike(volumes)
        sentiment = _compute_sentiment(symbol)

        score = _calculate_score(
            rsi_value=rsi_value,
            trend=trend,
            macd_diff=macd_diff,
            spike=spike,
            sentiment_label=sentiment["sentiment"],
            sentiment_score=sentiment["sentiment_score"],
        )

        result = {
            "symbol": symbol,
            "rsi": round(rsi_value, 2),
            "trend": trend,
            "sentiment": sentiment["sentiment"],
            "sentiment_score": sentiment["sentiment_score"],
            "sentiment_source": sentiment.get("source", "Stocktwits"),
            "bullish_messages": sentiment.get("bullish", 0),
            "bearish_messages": sentiment.get("bearish", 0),
            "macd": round(macd_value, 4),
            "macd_signal": round(macd_signal, 4),
            "macd_diff": round(macd_diff, 4),
            "vwap": round(vwap_value, 4),
            "volume_spike": spike,
            "score": score,
        }
        results.append(result)
        LOGGER.info("Scanned symbol", extra={"symbol": symbol})

    LAST_SCAN_TIME = datetime.utcnow()
    results.sort(key=lambda item: item["score"], reverse=True)
    return results


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/status")
def status():
    uptime = time.time() - START_TIME
    cache_timestamp = SCAN_CACHE["timestamp"]
    cache_age = (time.time() - cache_timestamp) if cache_timestamp else None
    return {
        "uptime_seconds": round(uptime, 1),
        "last_scan": LAST_SCAN_TIME.isoformat() if LAST_SCAN_TIME else None,
        "cache_age_seconds": round(cache_age, 1) if cache_age else None,
    }


@app.get("/scan")
def scan_microcaps(tickers: Optional[str] = Query(None, description="Comma separated list of tickers to scan")):
    try:
        symbols = [ticker.strip().upper() for ticker in (tickers.split(",") if tickers else DEFAULT_TICKERS)]
        cache_key = ",".join(sorted(symbols))
        cache_timestamp = SCAN_CACHE["timestamp"]

        if (
            SCAN_CACHE["results"] is not None
            and SCAN_CACHE["symbols"] == cache_key
            and cache_timestamp
            and (time.time() - cache_timestamp) < CACHE_TTL_SECONDS
        ):
            LOGGER.info("Returning cached scan results")
            return {"results": SCAN_CACHE["results"], "cached": True}

        results = _run_scan(symbols)
        if not results:
            return {"message": "Data unavailable"}

        SCAN_CACHE["results"] = results
        SCAN_CACHE["timestamp"] = time.time()
        SCAN_CACHE["symbols"] = cache_key

        return {"results": results, "cached": False}

    except Exception as exc:
        LOGGER.error(f"Error scanning microcaps: {exc}", extra={"symbol": "NA"})
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
        LOGGER.error("Failed to place trade", extra={"symbol": symbol})
        return {"error": str(exc)}

    return {"status": f"{action} order placed for {symbol}"}
