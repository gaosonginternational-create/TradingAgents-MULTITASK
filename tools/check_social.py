"""Probe the social-sentiment sources and report which ones this exit IP can reach.

Both StockTwits and Reddit judge the *exit IP*, not the code: a VPN node that
works today may be blocked tomorrow (Cloudflare WAF on StockTwits, anti-scraper
rules on Reddit). When either starts failing, the first question is "is it us or
the node?" — this answers it in a couple of seconds.

Usage:
    python tools/check_social.py            # probes through the current env proxy
    python tools/check_social.py --direct   # bypass the proxy
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

_PROBES = [
    ("StockTwits API", "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json"),
    ("StockTwits 网页", "https://stocktwits.com/"),
    ("Reddit 首页", "https://www.reddit.com/"),
    ("Reddit 匿名 JSON", "https://www.reddit.com/r/stocks/search.json?q=AAPL&restrict_sr=on&limit=3"),
    ("Reddit OAuth 端点", "https://www.reddit.com/api/v1/access_token"),
]


def _verdict(code: int, name: str) -> str:
    if code == 200:
        return "✅ 可用"
    if code == 401:
        return "✅ 可达（需凭证）" if "OAuth" in name else "⚠️ 需登录"
    if code == 403:
        return "❌ 被拦截（WAF/反爬，与代码无关）"
    if code == 429:
        return "❌ 限流（这个出口 IP 已被打上记号）"
    if code == 404:
        return "❌ 端点不存在"
    return f"❌ HTTP {code}"


def probe(direct: bool, proxy_url: str | None = None) -> int:
    handlers = []
    if direct:
        handlers.append(urllib.request.ProxyHandler({}))
    elif proxy_url:
        # 显式指定，避免被环境里的 HTTPS_PROXY 抢走（系统里常残留一个旧端口）
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)

    proxy = proxy_url or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "(直连)"
    print(f"出口：{proxy}\n")

    ok = 0
    for name, url in _PROBES:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                  "Accept": "application/json"})
        if "OAuth" in name:
            # 假凭证：401 就说明端点可达，只差真实 client_id/secret
            import base64
            req.add_header("Authorization", "Basic " + base64.b64encode(b"x:y").decode())
            req.data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        start = time.time()
        try:
            with opener.open(req, timeout=15) as resp:
                code = resp.getcode()
        except urllib.error.HTTPError as exc:
            code = exc.code
        except Exception as exc:  # noqa: BLE001
            print(f"{name:20s}  ❌ {type(exc).__name__}（连不上）")
            continue
        line = f"{name:20s}  {code}  {_verdict(code, name)}  ({time.time() - start:.1f}s)"
        print(line)
        if code in (200, 401):
            ok += 1

    print(f"\n{ok}/{len(_PROBES)} 个探测点可用。")
    if ok < 3:
        print("建议：换一个 VPN 节点再跑一次。Cloudflare/Reddit 是按出口 IP 判定的，"
              "换节点通常能立即改善；换完后不需要改任何代码。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="probe StockTwits / Reddit reachability")
    ap.add_argument("--direct", action="store_true", help="bypass the proxy")
    ap.add_argument("--proxy", help="explicit proxy URL, e.g. http://127.0.0.1:10793")
    args = ap.parse_args(argv)
    return probe(args.direct, args.proxy)


if __name__ == "__main__":
    sys.exit(main())
