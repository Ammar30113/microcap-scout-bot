"""Utility helpers for scraping Finviz insider trades without relying on a private API."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

LOGGER = logging.getLogger(__name__)

ELITE_INSIDER_URL = "https://elite.finviz.com/insidertrading.ashx"
REQUEST_HEADERS = {
    # Pretend to be a modern browser so Finviz returns HTML instead of blocking the request.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    )
}
DEFAULT_TIMEOUT = 10
DEFAULT_LIMIT = 100


def _parse_float(value: str) -> Optional[float]:
    cleaned = value.strip().replace("$", "").replace(",", "").replace("+", "")
    cleaned = cleaned.replace("(", "-").replace(")", "")
    if not cleaned:
        return None
    multiplier = 1.0
    if cleaned.endswith(("M", "m")):
        multiplier = 1_000_000.0
        cleaned = cleaned[:-1]
    elif cleaned.endswith(("K", "k")):
        multiplier = 1_000.0
        cleaned = cleaned[:-1]
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def _parse_int(value: str) -> Optional[int]:
    parsed = _parse_float(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value


def fetch_insider_trades(
    *,
    limit: int = DEFAULT_LIMIT,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """Scrape the Finviz insider trading page and return structured rows."""
    http = session or requests.Session()
    try:
        response = http.get(
            ELITE_INSIDER_URL,
            headers=REQUEST_HEADERS,
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as exc:
        LOGGER.warning("Unable to fetch Finviz insider trades: %s", exc)
        return []

    if response.status_code in (403, 404):
        LOGGER.warning(
            "Finviz responded with HTTP %s at %s; returning empty insider list.",
            response.status_code,
            ELITE_INSIDER_URL,
        )
        return []

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        LOGGER.warning("Finviz insider request failed: %s", exc)
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    # Elite renders the insider trades inside a table where the first row starts with "Ticker".
    table = soup.find("table", class_="body-table")
    if table is None:
        table = soup.find("table", attrs={"class": lambda value: value and "insider" in value})

    if table is None:
        LOGGER.warning("Unable to locate insider trading table on Finviz insider page.")
        return []

    trades: List[Dict[str, Any]] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        header_label = cells[0].get_text(strip=True).lower()
        if header_label == "ticker":
            continue

        values = [cell.get_text(strip=True) for cell in cells]
        if len(values) < 8:
            continue

        trades.append(
            {
                "ticker": values[0].upper(),
                "insider": values[1],
                "relationship": values[2],
                "date": _parse_date(values[3]),
                "transaction": values[4],
                "price": _parse_float(values[5]),
                "shares": _parse_int(values[6]),
                "value": _parse_float(values[7]),
                "shares_total": _parse_int(values[8]) if len(values) > 8 else None,
                "sec_form": values[9] if len(values) > 9 else None,
            }
        )

        if 0 < limit <= len(trades):
            break

    return trades
