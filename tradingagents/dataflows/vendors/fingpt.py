"""FinGPT local-inference vendor.

FinGPT (https://github.com/AI4Finance-Foundation/FinGPT) is an open-source
financial LLM family. This project runs a local FinGPT workbench
(``app.py``) that serves three LoRA-tuned models on ``http://127.0.0.1:7860``:

  - ``/api/sentiment``  — FinGPT-Sentiment (Llama2-13B LoRA): classifies a news
    headline into positive/neutral/negative.
  - ``/api/forecast``   — FinGPT-Forecaster (Llama2-7B LoRA): given price
    history + company news, produces a bull/bear read with a weekly
    price-movement prediction.
  - ``/api/multitask``  — FinGPT-MT (Llama2-7B LoRA): relation / NER / headline
    tasks (not used here).

Unlike the cloud sentiment vendors, this is a *local model* with no API key and
no per-call cost — but it needs the FinGPT service to be running (started by
``启动FinGPT.bat``). When the service is down, the fetch degrades gracefully to
a placeholder rather than raising, so a stopped local service never aborts a
TradingAgents run.

Both functions return a formatted markdown string, mirroring the other vendor
helpers so the sentiment analyst can drop them in unchanged.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

# Local FinGPT workbench endpoint. Override with FINGPT_URL if it moves.
# Inside the TradingAgents Docker container the FinGPT service lives on the
# host, so prefer host.docker.internal (wired via host-gateway in
# docker-compose.yml); outside Docker, 127.0.0.1 is correct.
FINGPT_URL = os.getenv(
    "FINGPT_URL",
    "http://host.docker.internal:7860"
    if os.getenv("TRADINGAGENTS_IN_DOCKER") == "1"
    else "http://127.0.0.1:7860",
)

REQUEST_TIMEOUT = 120  # local 13B inference can take a while on cold start


def _post(path: str, payload: dict) -> dict:
    """POST to the local FinGPT service; raise OSError on any failure."""
    url = f"{FINGPT_URL}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise OSError(f"FinGPT service unreachable ({url}): {exc}") from None
    except ValueError:
        raise OSError("FinGPT returned non-JSON response") from None


def get_fingpt_sentiment(texts: list[str]) -> str:
    """Score a list of news headlines for sentiment via FinGPT-Sentiment.

    Returns a markdown block listing each headline and its FinGPT label
    (positive/neutral/negative). Degrades to a clear placeholder on any
    failure, so a stopped local service reads as "unavailable", not an error.
    """
    if not texts:
        return "### FinGPT 情绪评分：<无输入文本>\n"

    try:
        data = _post("/api/sentiment", {"texts": texts})
        results = data.get("results", [])
    except OSError as e:
        return f"### FinGPT 情绪评分：<unavailable: {e}>\n"

    if not results:
        return "### FinGPT 情绪评分：<服务返回空结果>\n"

    lines = ["### FinGPT 情绪评分（本地模型 FinGPT-Sentiment）:"]
    pos = neg = neu = 0
    for item in results:
        text = str(item.get("text", "")).strip()
        label = str(item.get("sentiment", "")).strip()
        # 统计方向（兼容中英标签）
        low = label.lower()
        if "positive" in low or "正面" in label:
            pos += 1
            mark = "正面"
        elif "negative" in low or "负面" in label:
            neg += 1
            mark = "负面"
        elif "neutral" in low or "中性" in label:
            neu += 1
            mark = "中性"
        else:
            mark = label or "?"
        snippet = text[:120] + ("…" if len(text) > 120 else "")
        lines.append(f"- [{mark}] {snippet}")
    total = pos + neg + neu
    if total:
        lines.append(
            f"- 汇总: 正面 {pos} / 负面 {neg} / 中性 {neu}"
            f"（共 {total} 条）"
        )
    lines.append("")
    return "\n".join(lines)


def get_fingpt_forecast(ticker: str, weeks: int = 2) -> str:
    """Run FinGPT-Forecaster for a US ticker (weekly price-movement read).

    Returns a markdown block with the model's positive developments, potential
    concerns, and a weekly prediction. Note the forecaster pulls its own
    yfinance/Finnhub data for *US* tickers — it is not wired for A-shares/HK.
    Degrades to a placeholder when the local service is down.
    """
    try:
        data = _post("/api/forecast", {"ticker": ticker.upper(), "weeks": weeks})
        result = str(data.get("result", "")).strip()
    except OSError as e:
        return f"### FinGPT 涨跌预测：<unavailable: {e}>\n"

    if not result:
        return "### FinGPT 涨跌预测：<服务返回空结果>\n"

    lines = [f"### FinGPT 涨跌预测（本地模型 FinGPT-Forecaster，{ticker.upper()}）:"]
    # result 可能已经是中文；按段落拆开保留结构
    for para in result.split("\n"):
        para = para.strip()
        if para:
            lines.append(para)
    lines.append("")
    return "\n".join(lines)
