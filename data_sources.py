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
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

CHART_THROTTLE_SECONDS = 1.5
CHART_COOLDOWN_SECONDS = 300
_rate_limited_until = 0.0

# Temporary cache so we do not hammer upstream providers repeatedly within a single run.
_FUNDAMENTAL_CACHE: Dict[str, Dict] = {}
_HISTORY_CACHE: Dict[str, Dict] = {}
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


def is_rate_limited() -> bool:
    """Expose rate-limit state so callers can surface status."""
    return _is_rate_limited()


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
        volume = info.get("last_volume") or info.get("volume") or 0.0

        needs_fallback = price is None or market_cap is None or not volume
        if needs_fallback:
            try:
                detailed = ticker.get_info()
            except HTTPError as exc:
                if getattr(exc.response, "status_code", None) == 429:
                    _mark_rate_limited()
                raise DataFetchError(f"HTTP error: {exc}") from exc
            except Exception as exc:
                message = str(exc)
                if "Too Many Requests" in message or "429" in message:
                    _mark_rate_limited()
                detailed = {}

            if detailed:
                price = price or detailed.get("regularMarketPrice") or detailed.get("previousClose")
                market_cap = market_cap or detailed.get("marketCap")
                volume = volume or detailed.get("regularMarketVolume") or detailed.get("volume") or 0.0
                if pe_ratio is None:
                    pe_ratio = detailed.get("trailingPE") or detailed.get("forwardPE")

        if price is None or market_cap is None:
            raise DataFetchError("Incomplete Yahoo fundamentals")

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


def _fetch_yahoo_quote(symbol: str) -> Optional[Dict]:
    try:
        resp = SESSION.get(
            YAHOO_QUOTE_URL,
            params={"symbols": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("quoteResponse", {}).get("result", [])
        if not results:
            return None
        quote = results[0]
        price = quote.get("regularMarketPrice") or quote.get("regularMarketPreviousClose")
        market_cap = quote.get("marketCap")
        volume = quote.get("regularMarketVolume") or quote.get("averageDailyVolume10Day") or 0.0
        pe_ratio = quote.get("trailingPE") or quote.get("forwardPE")
        if price is None or market_cap is None:
            return None
        return {
            "symbol": symbol,
            "price": float(price),
            "market_cap": float(market_cap),
            "pe_ratio": float(pe_ratio) if pe_ratio else None,
            "volume": float(volume),
            "source": "yahoo_quote",
        }
    except RequestException as exc:
        _log_warning(symbol, f"yahoo_quote_error:{exc}", "yahoo_quote")
        return None


def _download_yfinance(symbol: str, interval: str, period: str) -> pd.DataFrame:
    """Return price history from yfinance; raise DataFetchError when unavailable."""
    if _is_rate_limited():
        raise DataFetchError("Yahoo temporarily rate limited")

    try:
        df = yf.download(
            symbol,
            interval=interval,
            period=period,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            raise DataFetchError("Empty price history")
        return df
    except Exception as exc:
        message = str(exc)
        if "429" in message or "Too Many Requests" in message:
            _mark_rate_limited()
        raise DataFetchError(message)


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

    yahoo_quote = _fetch_yahoo_quote(symbol)
    if yahoo_quote:
        _FUNDAMENTAL_CACHE[symbol] = {"data": yahoo_quote, "timestamp": time.time()}
        return yahoo_quote

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


def get_price_history(symbol: str, interval: str = "1h", period: str = "5d") -> Optional[pd.DataFrame]:
    cached = _HISTORY_CACHE.get(symbol)
    now = time.time()
    if cached and now - cached["timestamp"] < _CACHE_TTL_SECONDS:
        return cached["data"]

    cooldown_multiplier = 1

    for attempt in range(3):
        if is_rate_limited():
            sleep_for = max(0.0, _rate_limited_until - time.time())
            LOGGER.info("Cooling off %.1fs before retrying %s", sleep_for, symbol)
            time.sleep(min(sleep_for, CHART_COOLDOWN_SECONDS))

        try:
            df = _download_yfinance(symbol, interval, period)
            df = df.dropna()
            if not df.empty:
                _HISTORY_CACHE[symbol] = {"data": df, "timestamp": now}
                return df
        except DataFetchError:
            pass
        except RetryError:
            LOGGER.warning("Yahoo rate limit hit for %s (attempt %s/3)", symbol, attempt + 1)
            _mark_rate_limited(cooldown_multiplier)
            time.sleep((attempt + 1) * CHART_THROTTLE_SECONDS)
            cooldown_multiplier *= 2
            continue
        except HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 429:
                LOGGER.warning("HTTP 429 for %s (attempt %s/3)", symbol, attempt + 1)
                _mark_rate_limited(cooldown_multiplier)
                time.sleep((attempt + 1) * CHART_THROTTLE_SECONDS)
                cooldown_multiplier *= 2
                continue
            LOGGER.warning("HTTP error for %s: %s", symbol, exc)
            break
        except Exception as exc:
            LOGGER.warning("Retry %s/3 for %s failed: %s", attempt + 1, symbol, exc)
            time.sleep(CHART_THROTTLE_SECONDS)

    fundamentals = fetch_fundamentals(symbol)
    if fundamentals and fundamentals.get("price"):
        price = fundamentals["price"]
        df = pd.DataFrame({"Close": [price], "Volume": [fundamentals.get("volume", 0.0)]})
        _HISTORY_CACHE[symbol] = {"data": df, "timestamp": now}
        return df

    return None
