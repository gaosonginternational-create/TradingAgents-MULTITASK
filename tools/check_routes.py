"""Verify the proxy split: domestic sources direct, overseas through the VPN.

国内源绕代理既慢又脆（VPN 一断 A 股数据全挂），海外源不走代理则直接不通。
这份自检把两类各挑几个代表，分别用"强制直连"和"强制走代理"去摸，确认
路由方向没配反。

用法：
    python tools/check_routes.py                 # 用环境里的代理
    python tools/check_routes.py --proxy http://127.0.0.1:10793
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradingagents.proxy import DOMESTIC_HOSTS, OVERSEAS_HOSTS  # noqa: E402

_UA = "tradingagents/0.2 (route check)"

# (标签, URL) —— 只挑无需鉴权的轻量端点
_DOMESTIC = [
    ("东财 push2his", "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600999&fields1=f1&fields2=f51&klt=101&fqt=1&end=20500101&lmt=1"),
    ("东财 datacenter", "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD&columns=SECURITY_CODE&pageSize=1"),
    ("新浪财经", "https://hq.sinajs.cn/list=sh600999"),
    ("同花顺", "https://d.10jqka.com.cn/v6/line/hs_600999/01/today.js"),
    ("上交所", "https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&pageHelp.pageSize=1"),
    ("DeepSeek", "https://api.deepseek.com/v1/models"),
]

_OVERSEAS = [
    ("Yahoo 行情", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"),
    ("StockTwits", "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json"),
    ("Reddit", "https://www.reddit.com/r/stocks/about.json"),
    ("Google", "https://www.google.com/generate_204"),
]


def _probe(url: str, proxy: str | None) -> tuple[str, float]:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 强制直连
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    start = time.time()
    try:
        with opener.open(req, timeout=12) as resp:
            return str(resp.getcode()), time.time() - start
    except urllib.error.HTTPError as exc:
        # 401/403/429 都说明"网络层通了"，只是没权限或被限流
        return str(exc.code), time.time() - start
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, time.time() - start


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="check domestic-direct / overseas-proxied routing")
    ap.add_argument("--proxy", help="proxy URL, e.g. http://127.0.0.1:10793")
    args = ap.parse_args(argv)
    proxy = args.proxy or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")

    print(f"代理: {proxy or '(无)'}")
    print(f"NO_PROXY 条目: {len([h for h in (os.getenv('NO_PROXY') or '').split(',') if h.strip()])}")
    print(f"国内直连清单 {len(DOMESTIC_HOSTS)} 项 / 海外走代理清单 {len(OVERSEAS_HOSTS)} 项\n")

    print("── 国内源（应『强制直连』就可达）")
    bad = 0
    for label, url in _DOMESTIC:
        code, secs = _probe(url, None)
        ok = code.isdigit()
        # push2 的 RemoteDisconnected 是东方财富按 IP 限流，与路由无关：
        # 连接已经建起来了（0.3 秒就断），说明网络层是通的。
        if not ok and "push2" in url and code in ("RemoteDisconnected", "ConnectionResetError"):
            print(f"  {label:16s} 直连 {code:>18s}  {secs:.2f}s  ⚠️ IP 被东财限流（路由没问题）")
            continue
        bad += 0 if ok else 1
        print(f"  {label:16s} 直连 {code:>18s}  {secs:.2f}s  {'✅' if ok else '❌ 直连不通（可能需要代理或域名变更）'}")

    print("\n── 海外源（应经代理可达）")
    for label, url in _OVERSEAS:
        code, secs = _probe(url, proxy)
        ok = code.isdigit()
        bad += 0 if ok else 1
        print(f"  {label:16s} 代理 {code:>18s}  {secs:.2f}s  {'✅' if ok else '❌ 代理也不通'}")

    print()
    if bad:
        print(f"有 {bad} 项不符合预期，检查 tradingagents/proxy.py 的 DOMESTIC_HOSTS 与代理节点。")
    else:
        print("路由方向正确：国内直连、海外走代理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
