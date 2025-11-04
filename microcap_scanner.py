"""Utilities for assembling microcap scouting data for the FastAPI service."""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterable, List, Optional

import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest
from alpaca.trading.client import TradingClient

from finviz_scraper import fetch_insider_trades

LOGGER = logging.getLogger(__name__)

STOCKDATA_QUOTE_URL = "https://api.stockdata.org/v1/data/quote"
STOCKTWITS_TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"
STOCKTWITS_STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
FINVIZ_QUOTE_URL = "https://elite.finviz.com/quote.ashx"

MAX_STOCKTWITS_MESSAGES = 100
DEFAULT_RESULT_LIMIT = 10

FINVIZ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    )
}


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {key}")
    return value


def _chunked(iterable: Iterable[str], size: int) -> Iterable[List[str]]:
    seq = list(iterable)
    for idx in range(0, len(seq), size):
        yield seq[idx : idx + size]


def _fetch_candidate_symbols() -> List[str]:
    symbols: set[str] = set()

    try:
        insider_trades = fetch_insider_trades(limit=200)
        for trade in insider_trades:
            ticker = trade.get("ticker")
            if ticker:
                symbols.add(ticker.upper())
    except Exception as exc:  # pragma: no cover - network dependency
        LOGGER.warning("Unable to fetch Finviz insider trades: %s", exc)

    try:
        resp = requests.get(STOCKTWITS_TRENDING_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json().get("symbols", [])
        for entry in data:
            symbol = entry.get("symbol")
            if symbol:
                symbols.add(symbol.upper())
    except Exception as exc:  # pragma: no cover - network dependency
        LOGGER.warning("Unable to fetch StockTwits trending symbols: %s", exc)

    return list(symbols)


def _get_alpaca_clients() -> tuple[TradingClient, StockHistoricalDataClient]:
    api_key = _require_env("APCA_API_KEY_ID")
    api_secret = _require_env("APCA_API_SECRET_KEY")
    trading_client = TradingClient(api_key, api_secret, paper=True)
    data_client = StockHistoricalDataClient(api_key, api_secret)
    return trading_client, data_client


def _is_tradable(trading_client: TradingClient, symbol: str) -> bool:
    try:
        asset = trading_client.get_asset(symbol)
        return bool(asset and asset.tradable)
    except Exception as exc:  # pragma: no cover - network dependency
        LOGGER.debug("Failed to verify Alpaca asset %s: %s", symbol, exc)
        return False


def _fetch_latest_bars(
    data_client: StockHistoricalDataClient, symbols: List[str]
) -> Dict[str, object]:
    results: Dict[str, object] = {}
    for chunk in _chunked(symbols, 50):
        try:
            request = StockLatestBarRequest(symbol_or_symbols=chunk)
            response = data_client.get_stock_latest_bar(request)
        except Exception as exc:  # pragma: no cover - network dependency
            LOGGER.warning("Failed to fetch Alpaca market data for %s: %s", chunk, exc)
            continue

        if isinstance(response, dict):
            for key, bar in response.items():
                results[key.upper()] = bar
        elif response:
            # Single bar response – store with the only symbol from the chunk.
            results[chunk[0].upper()] = response
    return results


def _first_available(payload: Dict[str, object], keys: List[str]) -> Optional[float]:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _fetch_stockdata_quotes(symbols: List[str], api_key: str) -> Dict[str, Dict[str, object]]:
    results: Dict[str, Dict[str, object]] = {}
    for chunk in _chunked(symbols, 20):
        try:
            resp = requests.get(
                STOCKDATA_QUOTE_URL,
                params={"symbols": ",".join(chunk), "api_token": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            for entry in data:
                symbol = entry.get("symbol")
                if symbol:
                    results[symbol.upper()] = entry
        except Exception as exc:  # pragma: no cover - network dependency
            LOGGER.warning("StockData quote request failed for %s: %s", chunk, exc)
    return results


def _try_finviz_sentiment(symbol: str) -> Optional[Dict[str, object]]:
    """Attempt to grab sentiment from Finviz; return None if not available."""
    try:
        resp = requests.get(
            FINVIZ_QUOTE_URL,
            params={"t": symbol},
            headers=FINVIZ_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        LOGGER.info(
            "Finviz sentiment not parsed for %s – falling back to Stocktwits.", symbol
        )
    except Exception as exc:  # pragma: no cover - network dependency
        LOGGER.debug("Finviz sentiment fetch failed for %s: %s", symbol, exc)
    return None


def _fetch_stocktwits_sentiment(symbol: str) -> Dict[str, object]:
    bullish = bearish = 0
    scores: List[int] = []

    try:
        resp = requests.get(
            STOCKTWITS_STREAM_URL.format(symbol=symbol),
            params={"per_page": MAX_STOCKTWITS_MESSAGES},
            timeout=10,
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
    except Exception as exc:  # pragma: no cover - network dependency
        LOGGER.warning("StockTwits sentiment fetch failed for %s: %s", symbol, exc)
        messages = []

    for message in messages[:MAX_STOCKTWITS_MESSAGES]:
        sentiment = (
            message.get("entities", {}).get("sentiment", {}).get("basic")
        )
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

    total_tagged = bullish + bearish
    sentiment_index = round(bullish / total_tagged, 2) if total_tagged else 0.0

    if sentiment_score > 20:
        sentiment_label = "bullish"
    elif sentiment_score < -20:
        sentiment_label = "bearish"
    else:
        sentiment_label = "neutral"

    return {
        "sentiment": sentiment_label,
        "sentiment_score": sentiment_score,
        "sentiment_index": sentiment_index,
        "bullish": bullish,
        "bearish": bearish,
        "source": "Stocktwits",
    }


def _fetch_sentiment(symbol: str) -> Dict[str, object]:
    finviz_sentiment = _try_finviz_sentiment(symbol)
    if finviz_sentiment:
        return finviz_sentiment
    return _fetch_stocktwits_sentiment(symbol)


def gather_products(limit: int = DEFAULT_RESULT_LIMIT) -> List[Dict[str, object]]:
    """Collect and rank microcap candidates for the /products.json endpoint."""
    trading_client, data_client = _get_alpaca_clients()
    stockdata_key = _require_env("STOCKDATA_API_KEY")

    symbols = _fetch_candidate_symbols()
    if not symbols:
        LOGGER.warning("No candidate symbols found from Finviz or StockTwits.")
        return []

    tradable_symbols = [
        symbol for symbol in symbols if _is_tradable(trading_client, symbol)
    ]
    if not tradable_symbols:
        LOGGER.warning("No tradable symbols after Alpaca screening.")
        return []

    bars = _fetch_latest_bars(data_client, tradable_symbols)
    filtered: Dict[str, Dict[str, object]] = {}

    for symbol in tradable_symbols:
        bar = bars.get(symbol.upper())
        if not bar:
            continue

        try:
            price = float(bar.close)
            volume = int(getattr(bar, "volume", 0) or 0)
        except (TypeError, ValueError):
            continue

        if price <= 10 and volume >= 300_000:
            filtered[symbol.upper()] = {"price": round(price, 4), "volume": volume}

    if not filtered:
        LOGGER.info("No symbols met the microcap price/volume filters.")
        return []

    fundamentals = _fetch_stockdata_quotes(list(filtered.keys()), stockdata_key)

    products: List[Dict[str, object]] = []
    for symbol, metrics in filtered.items():
        fundamentals_row = fundamentals.get(symbol, {})
        market_cap = _first_available(
            fundamentals_row, ["market_cap", "marketCap", "market_capitalization"]
        )
        change_percent = _first_available(
            fundamentals_row,
            ["change_percent", "percent_change", "day_change_percent"],
        )
        sector = fundamentals_row.get("sector")

        sentiment = _fetch_sentiment(symbol)
        bullish_count = sentiment.get("bullish", 0)
        sentiment_index = sentiment.get("sentiment_index", 0.0) or 0.0

        rank_score = 0.0
        if market_cap and market_cap > 0:
            try:
                rank_score = (
                    sentiment_index * bullish_count / float(market_cap)
                )
            except (TypeError, ValueError, ZeroDivisionError):
                rank_score = 0.0

        trade_flag = bool(
            sentiment.get("sentiment_score", 0) > 70 and metrics["volume"] > 500_000
        )

        products.append(
            {
                "symbol": symbol,
                "price": metrics["price"],
                "volume": metrics["volume"],
                "market_cap": market_cap,
                "change_percent": change_percent,
                "sector": sector,
                "sentiment": sentiment.get("sentiment"),
                "sentiment_score": sentiment.get("sentiment_score"),
                "sentiment_index": sentiment_index,
                "bullish_messages": bullish_count,
                "bearish_messages": sentiment.get("bearish", 0),
                "source": sentiment.get("source"),
                "rank_score": round(rank_score, 12),
                "trade_flag": trade_flag,
            }
        )

    products.sort(key=lambda item: item["rank_score"], reverse=True)
    return products[: limit or DEFAULT_RESULT_LIMIT]
