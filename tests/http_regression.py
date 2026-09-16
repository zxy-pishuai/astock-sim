# -*- coding: utf-8 -*-
"""J1 回归基线采集：请求全部 GET 接口 + 静态资源 → (status, JSON 顶层 keys)
用法：python tests/http_regression.py --baseline baseline.json   （打 8899 改动前）
      python tests/http_regression.py --check baseline.json      （打 <port> 改动后对比）
      python tests/http_regression.py --check baseline.json --port 8897
"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1"
DEFAULT_PORT = 8899

# 全部 GET API（来自 server._api_get 分支；无副作用只读；带最小参数）
GET_APIS = [
    "overview", "experiments", "sectors", "sector/flow", "news/premarket",
    "news/brief", "news/sectors", "trading/status", "quotes", "kline",
    "score", "minute", "stocklist", "backtest/pool", "tactics", "state",
    "watchlist", "notify", "llm/config", "commission", "backtest/status",
    "log", "risk", "audit", "audit/daily", "review", "review/pack",
    "ai/meeting", "ai/morning", "ai/health", "factors", "factor/ic",
    "factor/scan", "factor/mine", "factor/pool", "portfolio/weights",
    "moneyflow", "reports", "report", "reports/compare", "watchgroups",
    "alerts", "update", "sentiment", "sentiment/gate", "global/quotes",
    "global/history", "global/summary", "global/movers", "global/a_share_hint",
    "news", "limitup", "earnings", "sentiment/history", "calendar", "data_health",
]
# 参数化 GET（带默认参数，保证可响应）
GET_PARAMS = {
    "kline": "code=000001&days=10", "score": "code=000001", "minute": "code=000001",
    "report": "name=demo", "review/pack": "pack=demo", "log": "lines=20",
    "audit": "lines=20", "moneyflow": "code=000001", "sector/flow": "name=银行",
    "factor/ic": "factor=momentum", "alerts": "kind=all", "ai/qa": "q=测试",
    "ai/morning": "refresh=0",
}
STATIC = ["/", "/index.html", "/css/app.css", "/js/app.js", "/js/charts.js",
          "/js/experiments.js", "/vendor/echarts.min.js", "/favicon.ico"]


def fetch(path, port):
    url = "%s:%d%s" % (BASE, port, path)
    req = urllib.request.Request(url, headers={"User-Agent": "j1-regression",
                                               "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            data = raw
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                data = gzip.decompress(raw)
            ct = r.headers.get("Content-Type", "")
            keys = None
            if "json" in ct:
                try:
                    keys = sorted(json.loads(data.decode("utf-8", "replace")).keys())
                except Exception:
                    keys = ["<parse-fail>"]
            return r.status, keys
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return -1, [str(e)[:60]]


def collect(port):
    out = {}
    for api in GET_APIS:
        p = "/api/" + api
        if api in GET_PARAMS:
            p += "?" + GET_PARAMS[api]
        out[p] = fetch(p, port)
    for s in STATIC:
        out[s] = fetch(s, port)
    return out


def main():
    args = sys.argv[1:]
    if args and args[0] == "--baseline":
        out = collect(DEFAULT_PORT)
        json.dump(out, open(args[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("baseline saved: %s (%d 项)" % (args[1], len(out)))
    elif args and args[0] == "--check":
        port = DEFAULT_PORT
        bl = json.load(open(args[1], encoding="utf-8"))
        for i, a in enumerate(args[2:]):
            if a == "--port" and i + 3 < len(args) + 1:
                port = int(args[i + 3])
                break
        # 简化参数解析
        if "--port" in args:
            port = int(args[args.index("--port") + 1])
        cur = collect(port)
        diffs = []
        for p in bl:
            b = bl[p]
            c = cur.get(p)
            if c is None:
                diffs.append((p, "missing", b, None))
            elif b[0] != c[0]:
                diffs.append((p, "status", b, c))
            elif b[1] != c[1]:
                diffs.append((p, "keys", b, c))
        if diffs:
            print("DIFF %d:" % len(diffs))
            for d in diffs:
                print("  %s [%s] before=%s after=%s" % d)
        else:
            print("MATCH OK: %d 项状态码+JSON 结构全部一致" % len(bl))
    else:
        print("usage: --baseline <json> | --check <json> [--port N]")


if __name__ == "__main__":
    main()
