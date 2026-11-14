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

# A no-content endpoint: connection success is all we need.
_PROXY_TEST_URL = "https://www.google.com/generate_204"
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
    try:
        with opener.open(_PROXY_TEST_URL, timeout=timeout) as resp:
            # 204 is the expected reply; a 403/429 still proves the proxy
            # forwarded the request (some exit IPs are blocked, not broken).
            return resp.getcode() in (200, 204, 301, 302, 403, 429)
    except Exception:  # noqa: BLE001 - any failure means "not usable"
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

        _apply_proxy(None)
        msg = (
            "[代理] 未找到可用代理：A股/ETF 数据不受影响，"
            "美股数据源（Yahoo/Reddit/StockTwits）可能失败"
        )
        emit(msg)
        logger.warning(msg)
        return None
    except Exception as exc:  # noqa: BLE001 - a proxy check must never break a run
        logger.warning("[代理] 检测异常，继续执行: %s", exc)
        return None
