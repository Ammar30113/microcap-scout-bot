"""Aggregate Finviz insider activity with StockTwits sentiment to spot high-conviction ideas."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import requests
from fastapi import APIRouter, Query

from finviz_scraper import fetch_insider_trades
from social_scanner import get_social_trending

router = APIRouter()

LOGGER = logging.getLogger(__name__)

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"


def _fetch_market_caps(symbols: List[str]) -> Dict[str, Optional[float]]:
    """Pull market caps for a batch of symbols via Yahoo Finance."""
    unique_symbols = sorted({symbol.upper() for symbol in symbols if symbol})
    if not unique_symbols:
        return {}

    try:
        joined = ",".join(unique_symbols)
        response = requests.get(f"{YAHOO_QUOTE_URL}?symbols={joined}")
        response.raise_for_status()
        results = response.json().get("quoteResponse", {}).get("result", [])
    except Exception as exc:  # broad catch to keep endpoint resilient
        LOGGER.warning("Failed to fetch market caps from Yahoo Finance: %s", exc)
        return {}

    caps: Dict[str, Optional[float]] = {}
    for item in results:
        symbol = item.get("symbol")
        if not symbol:
            continue
        caps[symbol.upper()] = item.get("marketCap")
    return caps


@router.get("/combined_signals")
def combined_signals(limit: int = Query(10, ge=1, le=50)):
    """Merge Finviz insider trades with StockTwits sentiment to rank potential breakouts."""
    insider_trades = fetch_insider_trades(limit=500)
    social_payload = get_social_trending()
    social_trending = social_payload.get("trending", [])

    social_by_symbol: Dict[str, dict] = {}
    for entry in social_trending:
        symbol = entry.get("symbol")
        if not symbol:
            continue
        social_by_symbol[symbol.upper()] = entry

    insider_by_symbol: Dict[str, dict] = {}
    for trade in insider_trades:
        symbol = trade.get("ticker")
        if not symbol:
            continue
        insider_by_symbol.setdefault(symbol.upper(), trade)

    common_symbols = sorted(set(social_by_symbol.keys()) & set(insider_by_symbol.keys()))
    market_caps = _fetch_market_caps(common_symbols)

    scored: List[dict] = []
    for symbol in common_symbols:
        social = social_by_symbol[symbol]
        insider = insider_by_symbol[symbol]
        bullish = social.get("bullish") or 0
        sentiment_index = social.get("sentiment_index") or 0.0
        market_cap = market_caps.get(symbol)

        if not market_cap or market_cap <= 0:
            continue  # Skip tickers without reliable market cap info

        score = (sentiment_index * bullish) / market_cap if bullish else 0.0
        scored.append(
            {
                "symbol": symbol,
                "name": social.get("name"),
                "price": social.get("price"),
                "bullish": bullish,
                "bearish": social.get("bearish") or 0,
                "sentiment_index": sentiment_index,
                "market_cap": market_cap,
                "score": round(score, 10),
                "insider": {
                    "insider": insider.get("insider"),
                    "relationship": insider.get("relationship"),
                    "transaction": insider.get("transaction"),
                    "shares": insider.get("shares"),
                    "price": insider.get("price"),
                    "date": insider.get("date"),
                },
            }
        )

    scored.sort(key=lambda item: item["score"], reverse=True)
    top = scored[:limit]
    return {"count": len(top), "symbols": top}
