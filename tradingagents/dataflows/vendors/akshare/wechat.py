"""WeChat Official Account (公众号) news via Sogou WeChat search.

WeChat Official Accounts are a closed ecosystem with no public API for
third-party article retrieval. The one free, keyless channel is Sogou's WeChat
search (``weixin.sogou.com``), which indexes public-account articles and lets
you search by keyword. It returns, per result, the article **title**, a short
**summary** snippet, the **account name**, and a **date** — but *not* the full
body text (the ``mp.weixin.qq.com`` link is a Sogou redirect that needs a
second hop, is rate-limited and often gated).

So this vendor deliberately treats public-account content as a *headline +
summary signal*: it gives the news/sentiment analyst the additional
"professional / opinion-media" read (机构/大V 观点) that the pure news feeds
(Eastmoney, Caixin) don't carry, without promising full text it can't reliably
get.

Anti-scraping notes (important for the operator):
- Sogou rate-limits hard and occasionally throws a captcha. We send a browser
  User-Agent, add a small delay, cap the page size, and degrade gracefully on
  any failure — a blocked/failed fetch reads as "unavailable", never aborts an
  analysis run.
- Do NOT call this at high frequency; the multi-agent run calls it at most a
  handful of times per analysis, which is well within safe limits.
"""

from __future__ import annotations

import html as _html
import logging
import re
import time
from urllib.parse import quote

import requests

from tradingagents.dataflows.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

SOGOU_WEIXIN_URL = "https://weixin.sogou.com/weixin"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_TIMEOUT = 12
_DELAY = 1.0          # polite delay between requests
_MAX_RESULTS = 8      # cap how many titles we feed the analyst

# A result title is a whole <a uigs="article_title_N">…</a> block; Sogou wraps
# the matched keyword in <em><!--red_beg-->…<!--red_end--></em>, so we strip all
# tags (and the red_beg/red_end markers) to recover the full title text.
_TITLE_A_RE = re.compile(r'<a[^>]*uigs="article_title_\d+"[^>]*>(.*?)</a>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_RED_RE = re.compile(r"<!--red_beg-->|<!--red_end-->")
# Summary snippets live in <p class="txt-info">…</p>.
_SUMMARY_RE = re.compile(r'class="txt-info"[^>]*>\s*(.*?)</p>', re.DOTALL)
# Account name lives in a <a uigs="account_name_N">…</a> block.
_ACCOUNT_A_RE = re.compile(r'<a[^>]*uigs="account_name_\d+"[^>]*>(.*?)</a>', re.DOTALL)


def _clean_text(raw: str) -> str:
    """Strip HTML tags and entities from a Sogou snippet."""
    raw = _RED_RE.sub("", raw)
    raw = _TAG_RE.sub("", raw)
    return _html.unescape(raw).strip()


def _sogou_search(keyword: str, limit: int = _MAX_RESULTS) -> list[dict]:
    """Search Sogou WeChat for ``keyword``; return a list of result dicts.

    Each dict: ``{title, summary, account}``. Returns ``[]`` on any failure
    (empty page, captcha, network) — callers degrade to unavailable.
    """
    if not keyword or not keyword.strip():
        return []
    query = quote(keyword.strip())
    url = f"{SOGOU_WEIXIN_URL}?type=2&query={query}&s_from=input"

    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Sogou WeChat search failed for %r: %s", keyword, e)
        raise VendorUnavailableError(f"Sogou WeChat search failed: {e}") from e

    html = resp.content.decode("utf-8", "ignore")

    # Captcha / anti-bot gate: Sogou serves a JS challenge instead of results.
    if "antispider" in html.lower() or "验证码" in html:
        raise VendorUnavailableError("Sogou WeChat search hit an anti-bot gate")

    titles = [_clean_text(m) for m in _TITLE_A_RE.findall(html)]
    summaries = [_clean_text(m) for m in _SUMMARY_RE.findall(html)]
    accounts = [_clean_text(m) for m in _ACCOUNT_A_RE.findall(html)]

    results: list[dict] = []
    for i, title in enumerate(titles):
        if not title:
            continue
        results.append({
            "title": title,
            "summary": summaries[i] if i < len(summaries) else "",
            "account": accounts[i] if i < len(accounts) else "",
        })
        if len(results) >= limit:
            break
    return results


def get_wechat_news(keyword: str, limit: int = _MAX_RESULTS) -> str:
    """WeChat Official Account article headlines+summaries for ``keyword``.

    ``keyword`` is a stock name / code or a market topic. Returns a markdown
    block listing each article's title, account and summary — an
    opinion/professional-media read to supplement the news feeds. Degrades to
    a clear ``<unavailable>`` placeholder on any failure, so a blocked search
    never aborts an analysis.
    """
    try:
        results = _sogou_search(keyword, limit=limit)
    except VendorUnavailableError as e:
        return f"### 微信公众号观点：<unavailable: {e}>\n"

    if not results:
        return f"### 微信公众号观点（{keyword}）：<无搜索结果>\n"

    lines = [f"### 微信公众号观点（搜狗微信搜索「{keyword}」）:"]
    for r in results:
        head = f"- **{r['title']}**"
        if r["account"]:
            head += f"（公众号：{r['account']}）"
        lines.append(head)
        if r["summary"]:
            lines.append(f"  - 摘要：{r['summary']}")
    lines.append("")
    # A short delay so a multi-topic run does not hammer Sogou.
    time.sleep(_DELAY)
    return "\n".join(lines)
