# -*- coding: utf-8 -*-
"""W4 akshare 五路免费源探测脚本（研究·唯一出网块）。

范围：龙虎榜 / 两融 / 限售解禁 / 业绩预告·报表 / 复权因子（hfq-factor）。
纪律（docs/operations.md + W4 红线）：
  - 仅 akshare 官方接口；单请求超时 30s（socket 级）；全块并发=1、请求间隔 >=1s；
  - 每路只拉有限样本（2 日期截面 / 2 票）；连续 3 次失败标"暂不可用"停手，不硬刷；
  - 只读；零落地；不写 market.db/min5.db/任何既有 data/ 文件；接入只到设计层。

用法：
  python tools\\ak_probe.py                # 全五路
  python tools\\ak_probe.py --routes lhb factor   # 指定路（逗号分隔: lhb/margin/unlock/earnings/factor）
  python tools\\ak_probe.py --dry          # 只做函数存在性检查，不发请求

输出：data/ak_probe_results.json；原始样本落 tmp/w4/raw/<route>_<tag>.csv
"""
import argparse
import json
import os
import socket
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

OUT_JSON = os.path.join(BASE, "data", "ak_probe_results.json")
RAW_DIR = os.path.join(BASE, "tmp", "w4", "raw")

INTERVAL = 1.0          # 请求间隔 >=1s
TIMEOUT = 30.0          # 单请求超时
MAX_STRIKES = 3         # 连续 3 次失败 -> 暂不可用

# ---- 五路样本定义（老票 600000 / 新票 001248 / 污染票 002130；日期截面取历史+近期）----
ROUTES = {
    "lhb": {
        "name": "龙虎榜",
        "fn": "stock_lhb_detail_em",
        "samples": [
            {"tag": "20240315", "kwargs": {"start_date": "20240315", "end_date": "20240315"}},
            {"tag": "20260828", "kwargs": {"start_date": "20260828", "end_date": "20260828"}},
        ],
        "note": "东财龙虎榜每日详情（按日期段）",
    },
    "margin": {
        "name": "两融(SSE+SZSE)",
        "fn": ["stock_margin_detail_sse", "stock_margin_detail_szse"],
        "samples": [
            {"tag": "sse_20240315", "fn": "stock_margin_detail_sse", "kwargs": {"date": "20240315"}},
            {"tag": "sse_20260828", "fn": "stock_margin_detail_sse", "kwargs": {"date": "20260828"}},
            {"tag": "szse_20240315", "fn": "stock_margin_detail_szse", "kwargs": {"date": "20240315"}},
            {"tag": "szse_20260828", "fn": "stock_margin_detail_szse", "kwargs": {"date": "20260828"}},
        ],
        "note": "沪深两融明细（按日；与库内 moneyflow kind='rzrq' 对比互补）",
    },
    "unlock": {
        "name": "限售解禁",
        "fn": ["stock_restricted_release_queue_em", "stock_restricted_release_detail_em"],
        "samples": [
            {"tag": "queue_600000", "fn": "stock_restricted_release_queue_em", "kwargs": {"symbol": "600000"}},
            {"tag": "queue_001248", "fn": "stock_restricted_release_queue_em", "kwargs": {"symbol": "001248"}},
            {"tag": "detail_20260824_28", "fn": "stock_restricted_release_detail_em",
             "kwargs": {"start_date": "20260824", "end_date": "20260828"}},
        ],
        "note": "个股解禁批次(老票600000+新票001248) + 单周解禁详情一览",
    },
    "earnings": {
        "name": "业绩预告/报表",
        "fn": ["stock_yjyg_em", "stock_yjbb_em"],
        "samples": [
            {"tag": "yjyg_20240331", "fn": "stock_yjyg_em", "kwargs": {"date": "20240331"}},
            {"tag": "yjyg_20260630", "fn": "stock_yjyg_em", "kwargs": {"date": "20260630"}},
            {"tag": "yjbb_20240331", "fn": "stock_yjbb_em", "kwargs": {"date": "20240331"}},
            {"tag": "yjbb_20260630", "fn": "stock_yjbb_em", "kwargs": {"date": "20260630"}},
        ],
        "note": "业绩预告(yjyg)+业绩报表(yjbb)，重点验证'披露/公告日期'字段（PIT 关键）",
    },
    "factor": {
        "name": "复权因子(hfq-factor)",
        "fn": "stock_zh_a_daily",
        "samples": [
            {"tag": "sh600000", "kwargs": {"symbol": "sh600000", "start_date": "20240801",
                                           "end_date": "20260831", "adjust": "hfq-factor"}},
            {"tag": "sz002130", "kwargs": {"symbol": "sz002130", "start_date": "20240801",
                                           "end_date": "20260831", "adjust": "hfq-factor"}},
            {"tag": "sh600000_raw", "kwargs": {"symbol": "sh600000", "start_date": "20240801",
                                               "end_date": "20260831", "adjust": ""}},
            {"tag": "sz002130_raw", "kwargs": {"symbol": "sz002130", "start_date": "20240801",
                                               "end_date": "20260831", "adjust": ""}},
        ],
        "note": "新浪 hfq-factor（老票600000 对照 + 污染票002130 靶子）+ 未复权close（qfq重算用）",
    },
}


def _load_ak():
    try:
        import akshare as ak
        return ak, ak.__version__
    except Exception as e:
        return None, "IMPORT-FAIL: %r" % e


def probe_route(ak, name, spec, dry):
    """探测一路。返回该路结果 dict。"""
    res = {"name": spec["name"], "note": spec["note"], "available": False,
           "samples": [], "strikes": 0, "last_exception": None, "elapsed_s": 0.0}
    # 函数存在性
    fns = spec["fn"] if isinstance(spec["fn"], list) else [spec["fn"]]
    missing = [f for f in fns if not hasattr(ak, f)]
    if missing:
        res["last_exception"] = "函数不存在: %s" % missing
        res["available"] = False
        return res
    if dry:
        res["available"] = True
        res["dry_only"] = True
        res["fns"] = fns
        return res
    t0 = time.time()
    strikes = 0
    for s in spec["samples"]:
        fn_name = s.get("fn", fns[0])
        fn = getattr(ak, fn_name)
        tag = s["tag"]
        rec = {"tag": tag, "fn": fn_name, "params": s["kwargs"], "ok": False,
               "rows": 0, "cols": [], "head": None, "raw_file": None,
               "elapsed_s": 0.0, "exception": None}
        try:
            t1 = time.time()
            df = fn(**s["kwargs"])
            rec["elapsed_s"] = round(time.time() - t1, 2)
            if df is None:
                raise RuntimeError("返回 None")
            rows = len(df)
            rec["rows"] = rows
            rec["cols"] = list(df.columns) if rows > 0 else []
            if rows > 0:
                head = df.head(3).astype(str).to_dict(orient="records")
                rec["head"] = head
                raw = os.path.join(RAW_DIR, "%s_%s.csv" % (name, tag))
                df.to_csv(raw, index=False, encoding="utf-8-sig")
                rec["raw_file"] = raw
            rec["ok"] = True
            strikes = 0
        except Exception as e:
            rec["exception"] = "%s: %s" % (type(e).__name__, str(e)[:300])
            strikes += 1
        res["samples"].append(rec)
        if strikes >= MAX_STRIKES:
            res["last_exception"] = "连续 %d 次失败，标暂不可用（%s）" % (
                strikes, res["samples"][-1]["exception"])
            res["available"] = False
            res["strikes"] = strikes
            break
        time.sleep(INTERVAL)  # 全局串行 + 间隔
    res["elapsed_s"] = round(time.time() - t0, 2)
    res["strikes"] = strikes
    res["available"] = all(s["ok"] for s in res["samples"]) and res.get("last_exception") is None
    if res["available"] and not res["samples"]:
        res["available"] = False
    return res


# ---- 历史起点补充探测（每路一次早日期，证据"能回溯多远"）----
HISTORY_PROBES = [
    {"tag": "lhb_20100104", "fn": "stock_lhb_detail_em", "kwargs": {"start_date": "20100104", "end_date": "20100104"}},
    {"tag": "margin_sse_20110104", "fn": "stock_margin_detail_sse", "kwargs": {"date": "20110104"}},
    {"tag": "margin_szse_20110104", "fn": "stock_margin_detail_szse", "kwargs": {"date": "20110104"}},
    {"tag": "unlock_detail_20100104_08", "fn": "stock_restricted_release_detail_em",
     "kwargs": {"start_date": "20100104", "end_date": "20100108"}},
    {"tag": "yjyg_20100331", "fn": "stock_yjyg_em", "kwargs": {"date": "20100331"}},
    {"tag": "yjbb_20100331", "fn": "stock_yjbb_em", "kwargs": {"date": "20100331"}},
]


def probe_history(ak):
    """历史起点探测：每路一次早日期。返回 {tag: {ok, rows, cols, head, exception, elapsed_s}}。"""
    out = {}
    for p in HISTORY_PROBES:
        fn = getattr(ak, p["fn"], None)
        rec = {"fn": p["fn"], "params": p["kwargs"], "ok": False, "rows": 0,
               "cols": [], "head": None, "exception": None, "elapsed_s": 0.0}
        if fn is None:
            rec["exception"] = "函数不存在"
            out[p["tag"]] = rec
            continue
        try:
            t1 = time.time()
            df = fn(**p["kwargs"])
            rec["elapsed_s"] = round(time.time() - t1, 2)
            rows = 0 if df is None else len(df)
            rec["rows"] = rows
            if rows > 0:
                rec["cols"] = list(df.columns)
                rec["head"] = df.head(2).astype(str).to_dict(orient="records")
            rec["ok"] = rows > 0
        except Exception as e:
            rec["exception"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        out[p["tag"]] = rec
        time.sleep(INTERVAL)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--routes", default="all",
                    help="逗号分隔: lhb/margin/unlock/earnings/factor, 默认 all")
    ap.add_argument("--dry", action="store_true", help="只查函数存在性，不发网络请求")
    ap.add_argument("--history", action="store_true", help="只跑历史起点补充探测（6 个早日期样本）")
    a = ap.parse_args()

    socket.setdefaulttimeout(TIMEOUT)
    os.makedirs(RAW_DIR, exist_ok=True)

    ak, ver = _load_ak()
    if ak is None:
        print("akshare 导入失败:", ver)
        return 1
    print("akshare", ver, flush=True)

    if a.history:
        hist = probe_history(ak)
        # 并入现有 results JSON 的 history_probes 键
        if os.path.exists(OUT_JSON):
            with open(OUT_JSON, encoding="utf-8") as f:
                out = json.load(f)
        else:
            out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "probe_script": "tools/ak_probe.py",
                   "akshare_version": ver,
                   "rate_limits": {"concurrency": 1, "interval_sec": INTERVAL,
                                   "timeout_sec": TIMEOUT, "max_strikes": MAX_STRIKES},
                   "routes": {}}
        out["history_probes"] = hist
        out["history_probed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = OUT_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp, OUT_JSON)
        for tag, r in hist.items():
            print("%-28s ok=%s rows=%s exc=%s" % (tag, r["ok"], r["rows"],
                  (r["exception"] or "")[:80]), flush=True)
        print("saved", OUT_JSON, flush=True)
        return 0

    if a.routes == "all":
        routes = ROUTES
    else:
        routes = {k: ROUTES[k] for k in a.routes.split(",") if k in ROUTES}

    # 合并而非覆盖：保留既有 results（避免 --routes 单路重跑时丢其他路）
    if os.path.exists(OUT_JSON):
        try:
            with open(OUT_JSON, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = {}
    else:
        prev = {}
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "probe_script": "tools/ak_probe.py",
           "akshare_version": ver,
           "rate_limits": {"concurrency": 1, "interval_sec": INTERVAL,
                           "timeout_sec": TIMEOUT, "max_strikes": MAX_STRIKES},
           "routes": prev.get("routes", {})}
    if "history_probes" in prev:
        out["history_probes"] = prev["history_probes"]
        out["history_probed_at"] = prev.get("history_probed_at")
    for name, spec in routes.items():
        print(">>> 探测 %-8s %s" % (name, spec["name"]), flush=True)
        r = probe_route(ak, name, spec, a.dry)
        out["routes"][name] = r
        print("    available=%s samples_ok=%d/%d strikes=%d elapsed=%.1fs" % (
            r["available"], sum(1 for s in r["samples"] if s["ok"]),
            len(r["samples"]), r["strikes"], r["elapsed_s"]), flush=True)

    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)
    print("saved", OUT_JSON, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
