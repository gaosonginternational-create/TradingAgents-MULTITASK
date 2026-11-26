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


def health() -> dict | None:
    """GET /api/health — tells "not started" apart from "still loading models".

    13B LoRA 在这台机器上冷启动要几十秒到几分钟，期间端口已经在监听、但
    推理请求会失败。原来的实现一律报"unreachable"，看起来像服务没开，
    很容易误判成地址写错——先问一次健康检查就能说清楚。
    """
    try:
        resp = requests.get(f"{FINGPT_URL}/api/health", timeout=8)
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def _post(path: str, payload: dict) -> dict:
    """POST to the local FinGPT service; raise OSError with an actionable reason."""
    url = f"{FINGPT_URL}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        # 服务是活的，返回了 4xx/5xx（多半是模型还在加载或显存不足）。
        status = getattr(exc.response, "status_code", "?")
        body = ""
        try:
            body = (exc.response.text or "")[:160]
        except Exception:  # noqa: BLE001
            pass
        info = health() or {}
        raise OSError(
            f"FinGPT 服务可达但 {path} 返回 HTTP {status}"
            + (f"（loaded_model={info.get('loaded_model')}, vram={info.get('vram_used_gb')}GB）"
               if info else "（健康检查也不通，模型可能正在加载）")
            + (f"；响应：{body}" if body else "")
        ) from None
    except requests.RequestException as exc:
        # 连不上：要么服务没起，要么端口/地址不对。
        hint = ("健康检查通过但该接口请求失败" if health() else
                f"端口无响应（{FINGPT_URL}）——请确认 启动FinGPT.bat 正在运行、"
                "窗口内无报错，且端口是 7860")
        raise OSError(f"FinGPT 不可达：{hint}；{type(exc).__name__}") from None
    except ValueError:
        raise OSError("FinGPT 返回了非 JSON 响应（可能命中了别的服务）") from None


def needs_translation(texts: list[str]) -> bool:
    """True when the batch is predominantly Chinese.

    FinGPT-Sentiment is a Llama2-13B LoRA tuned on **English** financial tweets;
    on Chinese headlines it degrades to near-constant labels (实测 "净利润同比
    增长20%" 被判为中性). Scoring a translated copy is the only way to get a
    usable read out of it for A-share news.
    """
    text = " ".join(texts or [])
    if not text:
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk >= max(4, len(text) * 0.15)


def get_fingpt_sentiment(texts: list[str], display_texts: list[str] | None = None) -> str:
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
        # 打分用英文（模型只认英文），展示用原文，避免中文报告里跳出一堆英文标题。
        if display_texts and len(display_texts) == len(results):
            for r, orig in zip(results, display_texts):
                r["display"] = orig
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
        shown = item.get("display") or text
        snippet = shown[:120] + ("…" if len(shown) > 120 else "")
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
