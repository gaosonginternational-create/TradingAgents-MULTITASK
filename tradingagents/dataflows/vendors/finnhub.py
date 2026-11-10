"""Finnhub company-news vendor.

Finnhub (https://finnhub.io) serves structured company news over
``/api/v1/company-news`` with ``from``/``to`` date filtering, which reaches back
about a year on the free tier. This makes it the fallback for the sentiment
analyst's news block when yfinance's news feed — which only serves recent
articles — cannot reach a historical analysis window.

A free API key (https://finnhub.io/register) is read from ``FINNHUB_API_KEY``;
if it is unset the vendor raises ``FinnhubNotConfiguredError`` (a
``VendorNotConfiguredError``) so the routing layer treats it as "unavailable"
and falls through to the next configured vendor rather than crashing.

Unlike the sentiment sources, this vendor raises typed ``VendorError``
subclasses instead of returning placeholders: it sits behind the router's
fallback chain, so a failure here must signal "try the next vendor", not "emit
an empty string into the prompt".
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError

logger = logging.getLogger(__name__)

_API_BASE = "https://finnhub.io/api/v1"
_COMPANY_NEWS = "/company-news"

REQUEST_TIMEOUT = 30


class FinnhubNotConfiguredError(VendorNotConfiguredError):
    """Raised when Finnhub is selected but no API key is configured."""


def get_api_key() -> str:
    """Retrieve the Finnhub API key from the environment."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        raise FinnhubNotConfiguredError(
            "FINNHUB_API_KEY environment variable is not set. Get a free key at "
            "https://finnhub.io/register."
        )
    return api_key


def _fmt_articles(articles: list[dict]) -> str:
    """Render Finnhub company-news articles as a compact markdown block."""
    lines = []
    for a in articles:
        headline = (a.get("headline") or "No headline").replace("\n", " ").strip()
        source = a.get("source") or "Unknown"
        summary = (a.get("summary") or "").replace("\n", " ").strip()
        url = a.get("url") or ""
        ts = a.get("datetime")
        when = ""
        if ts:
            try:
                when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OSError, TypeError):
                when = ""
        lines.append(f"### {headline} (source: {source})")
        if when:
            lines.append(f"Published: {when}")
        if summary:
            lines.append(summary)
        if url:
            lines.append(f"Link: {url}")
        lines.append("")
    return "\n".join(lines).rstrip()


def get_news_finnhub(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """News about ``ticker`` published in ``[start_date, end_date]``, from Finnhub.

    Queries Finnhub's company-news endpoint with ``from``/``to`` set to the
    analysis window, so a historical run receives point-in-time headlines that
    yfinance's recent-only feed cannot supply. Finnhub's free tier reaches back
    roughly a year.

    Raises ``VendorUnavailableError`` when the request fails or returns no
    usable articles, so the router falls through to the next vendor.
    """
    api_key = get_api_key()
    params = {
        "symbol": ticker.upper(),
        "from": start_date,
        "to": end_date,
        "token": api_key,
    }
    url = f"{_API_BASE}{_COMPANY_NEWS}"
    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        scrubbed = str(exc).replace(api_key, "***") if api_key else str(exc)
        raise VendorUnavailableError(f"Finnhub request failed: {scrubbed}") from None
    except ValueError:
        raise VendorUnavailableError("Finnhub returned a non-JSON response") from None

    if not isinstance(data, list) or not data:
        raise VendorUnavailableError(
            f"Finnhub returned no news articles for {ticker.upper()} in "
            f"{start_date}..{end_date}"
        )

    return (
        f"## {ticker.upper()} News, from {start_date} to {end_date} "
        f"(Finnhub):\n\n{_fmt_articles(data)}"
    )
