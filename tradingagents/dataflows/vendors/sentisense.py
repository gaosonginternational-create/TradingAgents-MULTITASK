"""SentiSense social-sentiment vendor.

SentiSense (https://sentisense.ai) serves model-scored retail sentiment for US
stocks — a signed ``sentiment`` polarity (-1.0 .. +1.0), a ``mentions`` volume
and a proprietary ``sentisense`` bull/bear score — drawn from Reddit, X,
YouTube, Substack and news, all scored by the same financial-text models.

This is the recommended replacement for StockTwits, whose public developer API
was permanently closed (every server-side request now returns HTTP 403). Unlike
StockTwits' user-labelled tags, SentiSense returns a model score, which is a
cleaner signal for the sentiment analyst to weigh.

Authentication is a header (``X-SentiSense-API-Key``), read from
``SENTISENSE_API_KEY``. When the key is unset the fetch degrades gracefully to a
placeholder rather than raising, so a missing key never aborts a run.

The function is deliberately self-contained: short timeout, graceful
degradation on any HTTP or parse failure, and a string return type so the
calling agent gets a uniform interface regardless of whether the network call
succeeded.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://app.sentisense.ai"
_METRICS = "/api/v2/metrics/entity/{ticker}/metric/{metric}"

# Header authentication. The key is stripped from any logged error so it never
# leaks into logs or tracebacks (mirrors net.get_scrubbed's secret handling).
_AUTH_HEADER = "X-SentiSense-API-Key"

REQUEST_TIMEOUT = 15

# How many days of history to request. Sentiment is noisy day-to-day, so a
# short trailing window (latest + prior points) is enough for a directional read.
LOOKBACK_DAYS = 7

# Cap the rendered series to keep the prompt block compact.
MAX_POINTS = 7


def get_api_key() -> str:
    """Retrieve the SentiSense API key from the environment.

    Returns an empty string when unset so callers can degrade gracefully, rather
    than raising — a missing optional social feed must not abort the analysis.
    """
    return os.getenv("SENTISENSE_API_KEY", "").strip()


def _get(metric: str, ticker: str, api_key: str) -> list:
    """GET a SentiSense metrics time series, stripping the key from any error."""
    url = f"{_API_BASE}{_METRICS.format(ticker=ticker.upper(), metric=metric)}"
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=LOOKBACK_DAYS)
    params = {
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(now.timestamp() * 1000),
        "maxDataPoints": MAX_POINTS,
    }
    headers = {_AUTH_HEADER: api_key, "Accept": "application/json"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        # Re-raise a scrubbed error so the key never appears in logs/tracebacks.
        scrubbed = str(exc).replace(api_key, "***") if api_key else str(exc)
        raise OSError(scrubbed) from None
    except ValueError:
        raise OSError("SentiSense returned non-JSON response") from None

    if not isinstance(data, list):
        return []
    return data


def _latest_value(point: dict) -> float | None:
    """Extract a scalar reading from a metrics data point.

    Numeric metrics nest the scalar under ``metricValue.value.value``; count
    metrics under ``metricValue.value``. Both are tolerated, and ``value`` is
    tried as a fallback.
    """
    mv = point.get("metricValue") if isinstance(point, dict) else None
    if isinstance(mv, dict):
        inner = mv.get("value")
        if isinstance(inner, dict):
            val = inner.get("value")
            if isinstance(val, (int, float)):
                return float(val)
        if isinstance(inner, (int, float)):
            return float(inner)
    val = point.get("value")
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _series(points: list) -> list[tuple[int, float]]:
    """Return (timestamp_ms, value) pairs for the points that carry a value."""
    out = []
    for p in points:
        ts = p.get("timestamp") if isinstance(p, dict) else None
        val = _latest_value(p)
        if isinstance(ts, (int, float)) and val is not None:
            out.append((int(ts), val))
    return out


def _latest_bull_bear(points: list) -> tuple[int | None, int | None]:
    """Extract the bull/bear counts from the latest sentisense data point.

    The sentisense metric carries a ``properties`` map with ``bull``, ``bear``
    and ``directional`` (bull + bear) counts for the day, which is a much more
    interpretable signal than the raw score alone.
    """
    if not points:
        return None, None
    latest = points[-1]
    mv = latest.get("metricValue") if isinstance(latest, dict) else None
    props = (mv or {}).get("properties") if isinstance(mv, dict) else None
    if not isinstance(props, dict):
        return None, None
    bull = props.get("bull")
    bear = props.get("bear")
    return (int(bull) if isinstance(bull, (int, float)) else None,
            int(bear) if isinstance(bear, (int, float)) else None)


def _fmt_series(name: str, series: list[tuple[int, float]]) -> str:
    """Render a metric series as compact dated lines."""
    if not series:
        return f"{name}: <no data>"
    lines = []
    for ts, val in series:
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%m-%d")
        lines.append(f"  {day}: {val:.2f}")
    return f"{name}:\n" + "\n".join(lines)


def fetch_sentisense_sentiment(
    ticker: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Fetch SentiSense social sentiment for ``ticker`` as a prompt block.

    Pulls three metrics — ``sentiment`` (signed polarity), ``mentions``
    (attention volume) and ``sentisense`` (bull/bear score) — and renders them
    as a compact, dated block for the sentiment analyst.

    ``start_date``/``end_date`` are accepted for interface parity with the other
    social vendors, but SentiSense only serves the most recent window (metrics
    are computed continuously), so a historical run's sentiment read is
    point-in-time to "now", not to the analysis date — the same limitation as
    StockTwits and Reddit.

    Returns a placeholder string on any failure, never raises.
    """
    api_key = get_api_key()
    if not api_key:
        return (
            "<SentiSense unavailable: SENTISENSE_API_KEY not set. "
            "Get a free key at https://app.sentisense.ai/get-api-key>"
        )

    try:
        sentiment_points = _get("sentiment", ticker, api_key)
        mentions_points = _get("mentions", ticker, api_key)
        sentisense_points = _get("sentisense", ticker, api_key)
    except OSError as exc:
        logger.warning("SentiSense fetch failed for %s: %s", ticker, exc)
        return f"<SentiSense unavailable: {exc}>"

    sentiment_series = _series(sentiment_points)
    mentions_series = _series(mentions_points)
    sentisense_series = _series(sentisense_points)

    # Latest values drive the directional read.
    latest_sentiment = sentiment_series[-1][1] if sentiment_series else None
    latest_sentisense = sentisense_series[-1][1] if sentisense_series else None
    bull, bear = _latest_bull_bear(sentisense_points)

    if latest_sentiment is None and latest_sentisense is None and not mentions_series:
        return f"<no SentiSense data found for ${ticker.upper()}>"

    # Interpret the signed polarity for the analyst.
    if latest_sentiment is None:
        stance = "unknown"
    elif latest_sentiment >= 0.05:
        stance = "Bullish"
    elif latest_sentiment <= -0.05:
        stance = "Bearish"
    else:
        stance = "Neutral"

    lines = [
        "SentiSense model-scored social sentiment (Reddit / X / YouTube / Substack / news):",
        f"Latest sentiment polarity: {latest_sentiment:+.2f} -> {stance}"
        if latest_sentiment is not None
        else "Latest sentiment polarity: <no data>",
    ]
    if latest_sentisense is not None:
        lines.append(f"Latest SentiSense bull/bear score: {latest_sentisense:.2f}")
    if bull is not None and bear is not None:
        total = bull + bear
        bull_pct = round(100 * bull / total) if total else 0
        lines.append(
            f"Latest day bull/bear: {bull} bull / {bear} bear "
            f"({bull_pct}% bullish, n={total})"
        )
    lines.append("")
    lines.append(_fmt_series("sentiment (polarity)", sentiment_series))
    lines.append(_fmt_series("mentions (attention volume)", mentions_series))
    lines.append(_fmt_series("SentiSense score (bull/bear)", sentisense_series))
    return "\n".join(lines)
