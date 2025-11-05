from __future__ import annotations

import logging
import os
import time
from typing import Optional

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import HTTPError, RetryError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)

STOCKDATA_BASE_URL = "https://api.stockdata.org/v1/data/quote"
FINVIZ_URL = "https://finviz.com/quote.ashx?t={symbol}"

CHART_THROTTLE_SECONDS = 1.5
CHART_COOLDOWN_SECONDS = 300
_rate_limited_until = 0.0

retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=("GET", "HEAD"),
)

SESSION = requests.Session()
ADAPTER = HTTPAdapter(max_retries=retry_strategy)
SESSION.mount("https://", ADAPTER)
SESSION.mount("http://", ADAPTER)

FINVIZ_HEADERS = {"User-Agent": "Mozilla/5.0"}


class DataFetchError(RuntimeError):
    pass


def is_rate_limited() -> bool:
    return time.time() < _rate_limited_until


def _mark_rate_limited(multiplier: int = 1) -> None:
    global _rate_limited_until
    _rate_limited_until = time.time() + CHART_COOLDOWN_SECONDS * multiplier


@retry(  # type: ignore
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(DataFetchError),
)
def _download_yfinance(symbol: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        raise DataFetchError(f"Empty response for {symbol}")
    return df


def get_price_history(symbol: str, interval: str = "1h", period: str = "5d") -> Optional[pd.DataFrame]:
    global _rate_limited_until
    if is_rate_limited():
        LOGGER.info("Skipping Yahoo fetch for %s during cooldown", symbol)
        return None

    try:
        df = _download_yfinance(symbol, period, interval)
        return df.dropna()
    except Exception as exc:
        LOGGER.warning("Yahoo Finance failed for %s: %s", symbol, exc)
        _mark_rate_limited()

    stockdata_key = os.getenv("STOCKDATA_API_KEY")
    if not stockdata_key:
        return None

    try:
        resp = SESSION.get(
            STOCKDATA_BASE_URL,
            params={"symbols": symbol, "api_token": stockdata_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        quote = data[0]
        price = quote.get("price")
        volume = quote.get("volume")
        if price is None:
            return None
        df = pd.DataFrame(
            {
                "Close": [float(price)],
                "Volume": [float(volume) if volume is not None else 0],
            }
        )
        return df
    except Exception as exc:
        LOGGER.warning("StockData fallback failed for %s: %s", symbol, exc)
        return None


def get_quote_snapshot(symbol: str) -> Optional[dict]:
    stockdata_key = os.getenv("STOCKDATA_API_KEY")
    if not stockdata_key:
        return None
    try:
        resp = SESSION.get(
            STOCKDATA_BASE_URL,
            params={"symbols": symbol, "api_token": stockdata_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        return data[0]
    except Exception as exc:
        LOGGER.warning("Quote snapshot failed for %s: %s", symbol, exc)
        return None


def get_sentiment(symbol: str) -> str:
    try:
        html = SESSION.get(FINVIZ_URL.format(symbol=symbol), headers=FINVIZ_HEADERS, timeout=10).text
        soup = BeautifulSoup(html, "html.parser")
        headlines = [node.get_text(strip=True).lower() for node in soup.select(".news-link-left")][:5]
        if any(word in headline for headline in headlines for word in ("up", "gain", "surge", "upgrade")):
            return "Positive"
        if any(word in headline for headline in headlines for word in ("down", "drop", "loss", "downgrade")):
            return "Negative"
        return "Neutral"
    except Exception as exc:
        LOGGER.warning("Finviz sentiment failed for %s: %s", symbol, exc)
        return "Neutral"
*** End Patch
