from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, Optional

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import HTTPError, RequestException, RetryError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)

STOCKDATA_BASE_URL = "https://api.stockdata.org/v1/data/quote"
FINVIZ_URL = "https://finviz.com/quote.ashx?t={symbol}"

CHART_THROTTLE_SECONDS = 1.5
CHART_COOLDOWN_SECONDS = 300
_rate_limited_until = 0.0

# Temporary cache so we do not hammer upstream providers repeatedly within a single run.
_FUNDAMENTAL_CACHE: Dict[str, Dict] = {}
_CACHE_TTL_SECONDS = 300
_WARNED: set[tuple[str, str, str]] = set()

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
    """Raised when upstream data is unavailable."""


def _is_rate_limited() -> bool:
    return time.time() < _rate_limited_until


def _mark_rate_limited(multiplier: int = 1) -> None:
    global _rate_limited_until
    _rate_limited_until = time.time() + CHART_COOLDOWN_SECONDS * multiplier


def _log_warning(symbol: str, reason: str, source: str) -> None:
    key = (symbol, reason, source)
    if key in _WARNED:
        return
    _WARNED.add(key)
    LOGGER.warning(json.dumps({"symbol": symbol, "reason": reason, "source": source}))


@retry(  # type: ignore
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(DataFetchError),
)
def _fetch_yahoo(symbol: str) -> Dict:
    if _is_rate_limited():
        raise DataFetchError("Yahoo temporarily rate limited")

    try:
        ticker = yf.Ticker(symbol)
        info = getattr(ticker, "fast_info", None) or {}
        price = info.get("last_price") or info.get("last_price_usd") or info.get("previous_close")
        market_cap = info.get("market_cap")
        pe_ratio = info.get("pe_ratio") or info.get("trailing_pe")
        if price is None or market_cap is None:
            raise DataFetchError("Incomplete Yahoo fundamentals")
        volume = info.get("last_volume") or info.get("volume") or 0.0
        return {
            "symbol": symbol,
            "price": float(price),
            "market_cap": float(market_cap),
            "pe_ratio": float(pe_ratio) if pe_ratio else None,
            "volume": float(volume),
            "source": "yahoo",
        }
    except RetryError as exc:  # pragma: no cover - raised by tenacity wrapper
        _mark_rate_limited()
        raise DataFetchError(str(exc))
    except HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 429:
            _mark_rate_limited()
        raise DataFetchError(f"HTTP error: {exc}")
    except Exception as exc:
        raise DataFetchError(str(exc))


def _fetch_finviz(symbol: str) -> Optional[Dict]:
    try:
        html = SESSION.get(FINVIZ_URL.format(symbol=symbol), headers=FINVIZ_HEADERS, timeout=10).text
        soup = BeautifulSoup(html, "html.parser")
        cells = soup.select("table.snapshot-table2 td")
        if not cells:
            return None
        data = {cells[i].get_text(strip=True): cells[i + 1].get_text(strip=True) for i in range(0, len(cells) - 1, 2)}
        price_text = data.get("Price")
        pe_text = data.get("P/E")
        mcap_text = data.get("Market Cap")
        volume_text = data.get("Volume")

        if not price_text or not mcap_text:
            return None

        def _parse_float(value: str) -> Optional[float]:
            value = value.replace(",", "")
            multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
            suffix = value[-1]
            if suffix in multipliers:
                return float(value[:-1]) * multipliers[suffix]
            try:
                return float(value)
            except ValueError:
                return None

        price = _parse_float(price_text)
        mcap = _parse_float(mcap_text)
        pe_ratio = _parse_float(pe_text) if pe_text else None
        volume = _parse_float(volume_text) if volume_text else 0.0

        if price is None or mcap is None:
            return None

        return {
            "symbol": symbol,
            "price": price,
            "market_cap": mcap,
            "pe_ratio": pe_ratio,
            "volume": volume,
            "source": "finviz",
        }
    except Exception as exc:
        _log_warning(symbol, f"finviz_error:{exc}", "finviz")
        return None


def _fetch_stockdata(symbol: str) -> Optional[Dict]:
    api_key = os.getenv("STOCKDATA_API_KEY")
    if not api_key:
        return None
    try:
        resp = SESSION.get(
            STOCKDATA_BASE_URL,
            params={"symbols": symbol, "api_token": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None
        quote = data[0]
        price = quote.get("price")
        market_cap = quote.get("market_cap") or quote.get("marketCap")
        pe_ratio = quote.get("pe_ratio") or quote.get("pe")
        volume = quote.get("volume")
        if price is None or market_cap is None:
            return None
        return {
            "symbol": symbol,
            "price": float(price),
            "market_cap": float(market_cap),
            "pe_ratio": float(pe_ratio) if pe_ratio else None,
            "volume": float(volume) if volume is not None else 0.0,
            "source": "stockdata",
        }
    except RequestException as exc:
        _log_warning(symbol, f"stockdata_error:{exc}", "stockdata")
        return None


def fetch_fundamentals(symbol: str) -> Optional[Dict]:
    cached = _FUNDAMENTAL_CACHE.get(symbol)
    if cached and time.time() - cached["timestamp"] < _CACHE_TTL_SECONDS:
        return cached["data"]

    try:
        yahoo_data = _fetch_yahoo(symbol)
        if yahoo_data:
            _FUNDAMENTAL_CACHE[symbol] = {"data": yahoo_data, "timestamp": time.time()}
            return yahoo_data
    except DataFetchError as exc:
        _log_warning(symbol, f"yahoo_failure:{exc}", "yahoo")

    finviz_data = _fetch_finviz(symbol)
    if finviz_data:
        _FUNDAMENTAL_CACHE[symbol] = {"data": finviz_data, "timestamp": time.time()}
        return finviz_data

    stockdata = _fetch_stockdata(symbol)
    if stockdata:
        _FUNDAMENTAL_CACHE[symbol] = {"data": stockdata, "timestamp": time.time()}
        return stockdata

    _log_warning(symbol, "missing fundamentals", "all")
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
        _log_warning(symbol, f"sentiment_error:{exc}", "finviz")
        return "Neutral"
