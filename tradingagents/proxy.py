"""Validate the container's outbound proxy before an analysis starts.

The host runs a VPN whose local proxy port changes on every reconnect, while the
container is launched with whatever ``HOST_PROXY`` the webapp last detected. A
stale port makes every US data call (Yahoo, StockTwits, Reddit) hang until its
timeout, which is slow and looks like a broken run.

This checks the proxy from *inside* the container — where it actually has to
work — and, when it is dead, probes the host's usual proxy ports through
``host.docker.internal`` to find the live one. It is best-effort and never
raises: A-share/ETF data (Eastmoney, Sina) is reachable without a proxy, so a
missing proxy must degrade US-only data, not the whole run.
"""

from __future__ import annotations

import logging
import os
import socket
import urllib.request

logger = logging.getLogger(__name__)

# Health check targets, most-relevant first. A single Google probe is a bad
# signal: it can fail while the proxy is perfectly healthy (routing rules,
# regional blocks), which looks exactly like "proxy down". Any one of these
# answering proves the proxy forwards traffic.
_PROXY_TEST_URLS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d",
    "https://github.com",
    "https://www.google.com/generate_204",
)
_PROXY_TEST_URL = _PROXY_TEST_URLS[0]
_TEST_TIMEOUT = 4.0
_PORT_TIMEOUT = 0.4

# Ports the user's VPN client has actually listened on, then the common
# defaults for Clash / V2Ray / Shadowsocks / plain HTTP proxies.
_CANDIDATE_PORTS = [
    10793, 62274, 57817, 56044, 60793,
    7890, 7891, 7897,
    10808, 10809, 1080, 1081,
    8118, 8888, 8080, 3128, 10080, 33210,
]

# Ways to reach the host from inside a container: the compose alias first, then
# the docker bridge gateway as a fallback for setups without the alias.
_HOST_ALIASES = ["host.docker.internal", "172.17.0.1"]

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

# ---------------------------------------------------------------------------
# 分流规则：国内源直连，海外源走 VPN
# ---------------------------------------------------------------------------
# 这份清单是**唯一权威**。docker-compose.yml 里的 NO_PROXY 必须与它保持一致
# （compose 只负责容器启动时的那一份，进程内由 _merge_no_proxy 兜底合并）。
#
# 为什么国内源绝不能走代理：
#   东财/新浪/同花顺/交易所都在国内，直连几十毫秒；绕 VPN 要绕出去再回来，
#   不仅慢，而且 VPN 一断这些 A 股数据立刻全挂（之前就出现过容器连不上
#   host 代理、所有东财请求 "Network is unreachable" 的事故）。
#   DeepSeek 也一样——它是国内 API，走 VPN 纯属自找麻烦。
#
# 为什么海外源必须走代理：
#   雅虎/Reddit/StockTwits/Google 在境内要么不可达，要么被限流，
#   不走代理就是超时和 403。
DOMESTIC_HOSTS = (
    # 本机与容器内部
    "localhost", "127.0.0.1", "host.docker.internal", "172.17.0.1",
    # 行情与资讯
    "eastmoney.com", ".eastmoney.com",
    "sina.com.cn", ".sina.com.cn", "sina.com", ".sina.com",
    "gtimg.cn", ".gtimg.cn",              # 腾讯行情
    "10jqka.com.cn", ".10jqka.com.cn",    # 同花顺资金流/行业
    "hexun.com", ".hexun.com",
    "163.com", ".163.com",
    "xueqiu.com", ".xueqiu.com",
    # 交易所与信息披露
    "sse.com.cn", ".sse.com.cn",          # 上交所（ETF 份额）
    "szse.cn", ".szse.cn",                # 深交所
    "cninfo.com.cn", ".cninfo.com.cn",    # 巨潮资讯
    # 其他国内站点
    "qq.com", ".qq.com", "baidu.com", ".baidu.com", "ifeng.com", ".ifeng.com",
    # 国内 LLM / 数据 API
    "deepseek.com", ".deepseek.com",
    # 少数可直连的海外数据源（实测直连更稳，走代理反而超时）
    "stlouisfed.org", ".stlouisfed.org",  # FRED 宏观
    "alphavantage.co", ".alphavantage.co",
)

# 仅作文档与自检用：这些必须走代理，绝不能出现在 NO_PROXY 里。
OVERSEAS_HOSTS = (
    "finance.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com",
    "reddit.com", "www.reddit.com", "oauth.reddit.com",
    "api.stocktwits.com",
    "google.com", "translate.googleapis.com",
    "github.com", "api.github.com",
    "gamma-api.polymarket.com",
)

# 兼容旧名字
_DOMESTIC_NO_PROXY = DOMESTIC_HOSTS


def _merge_no_proxy() -> None:
    """Ensure domestic hosts bypass any proxy (both env spellings)."""
    current = os.getenv("NO_PROXY") or os.getenv("no_proxy") or ""
    items = [x.strip() for x in current.split(",") if x.strip()]
    lowered = {x.lower() for x in items}
    for host in DOMESTIC_HOSTS:
        if host.lower() not in lowered:
            items.append(host)
    value = ",".join(items)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


def _apply_proxy(url: str | None) -> None:
    """Point every proxy env var at ``url`` (or clear them all when None)."""
    for key in _PROXY_ENV_KEYS:
        if url:
            os.environ[key] = url
        else:
            os.environ.pop(key, None)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_PORT_TIMEOUT):
            return True
    except OSError:
        return False


def proxy_works(url: str, timeout: float = _TEST_TIMEOUT) -> bool:
    """Whether ``url`` behaves as a working HTTP proxy to the outside world."""
    if not url:
        return False
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url})
    )
    for target in _PROXY_TEST_URLS:
        try:
            with opener.open(target, timeout=timeout) as resp:
                # 204 is the expected reply; a 403/429 still proves the proxy
                # forwarded the request (some exit IPs are blocked, not broken).
                if resp.getcode() in (200, 204, 301, 302, 403, 429):
                    return True
        except Exception:  # noqa: BLE001 - try the next target
            continue
    return False


def direct_works(timeout: float = _TEST_TIMEOUT) -> bool:
    """Whether the container can reach the internet with no proxy at all.

    A VPN in TUN / global mode has **no local proxy port** — traffic leaves
    through a virtual adapter instead. In that mode "no proxy found" is the
    correct, working state, and telling the user "no proxy" would be misleading.
    An empty ProxyHandler disables any inherited proxy env for this check.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in _PROXY_TEST_URLS:
        try:
            with opener.open(url, timeout=timeout) as resp:
                if resp.getcode() < 500:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _candidate_urls(preferred_host: str | None) -> list[str]:
    hosts = ([preferred_host] if preferred_host else []) + [
        h for h in _HOST_ALIASES if h != preferred_host
    ]
    return [f"http://{h}:{p}" for p in _CANDIDATE_PORTS for h in hosts]


def ensure_proxy(log=None) -> str | None:
    """Make sure a working proxy is configured in this process; return its URL.

    Verifies the configured proxy first (fast path), then walks the candidate
    ports on the host. On success the proxy env vars are set for this process;
    on failure they are cleared so requests fail fast instead of stalling on a
    dead proxy. Never raises.
    """
    emit = log or (lambda msg: print(msg, flush=True))
    try:
        # Domestic hosts bypass the proxy whatever else happens.
        _merge_no_proxy()

        configured = (
            os.getenv("HOST_PROXY", "").strip()
            or os.getenv("HTTPS_PROXY", "").strip()
            or os.getenv("https_proxy", "").strip()
        )

        if configured and proxy_works(configured):
            _apply_proxy(configured)
            emit(f"[代理] {configured} 可用")
            return configured

        preferred_host = None
        if "//" in configured:
            netloc = configured.split("//", 1)[1].split("/", 1)[0]
            preferred_host = netloc.rsplit(":", 1)[0] if ":" in netloc else netloc

        for url in _candidate_urls(preferred_host):
            host = url.split("//", 1)[1].rsplit(":", 1)[0]
            port = int(url.rsplit(":", 1)[1])
            if not _port_open(host, port):
                continue
            if proxy_works(url):
                _apply_proxy(url)
                if configured and configured != url:
                    emit(f"[代理] 原代理 {configured} 不可用，已自动改用 {url}")
                else:
                    emit(f"[代理] 已自动检测到可用代理 {url}")
                return url

        # 已知端口都不通时，先别急着报"没有代理"：TUN/全局模式下本来就没有
        # 本地代理端口，此时直连就是正确状态。直连可用就直接用，省掉慢扫描。
        _apply_proxy(None)
        if direct_works():
            msg = (
                "[代理] 未发现本地代理端口，改走直连（VPN 为 TUN/全局模式）——"
                "直连可用，美股数据源正常"
            )
        else:
            msg = (
                "[代理] 未找到可用代理，直连也不通：A股/ETF 数据仍可尝试，"
                "美股数据源（Yahoo/Reddit/StockTwits）预计失败"
            )
        emit(msg)
        logger.warning(msg)
        return None
    except Exception as exc:  # noqa: BLE001 - a proxy check must never break a run
        logger.warning("[代理] 检测异常，继续执行: %s", exc)
        return None
