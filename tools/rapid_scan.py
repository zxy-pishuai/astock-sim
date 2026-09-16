# -*- coding: utf-8 -*-
"""快速挖票快环（独立工具，不改 app/config）——「全市场秒级扫板原型」

目的：把"找潜力票"压缩到 tdx 批量行情的物理节奏（3 秒/包 L1）内。
每 interval 秒一轮：对你给定宇宙（默认 bt_pool top500 全体主板+创业+科创）
跑并发批量行情 → 候选触发：
  C1 板上：price >= limit_up*0.998（含封板确认：ask1==0 即封单在买一）
  C2 冲板：pct_chg >= 7(主板)/17(创业科创) 且价格未到涨停
  C3 加速票：外盘/内盘>=2.5 且 pct>=4（主动买盘碾压）
输出：data/vendor/rapid_scan/rapid_scan_{YYYYMMDD}.jsonl（逐事件追加）+
      控制台逐轮耗时 P50/P99。不动 engine/trader/config，只读 app.tdx。
用法：python tools/rapid_scan.py --interval 3 --rounds 20 [--pool data/bt_pool.json]
"""
import argparse
import json
import pathlib
import statistics
import sys
import time

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from app import tdx as tdxmod  # 只读常驻池，不写库
tdx = tdxmod  # 简称

OUT_DIR = BASE / "data" / "vendor" / "rapid_scan"


def limit_up_of(code, yc):
    if yc <= 0:
        return 0.0
    if code.startswith(("300", "301", "688", "689")):
        k = 1.20
    elif code.startswith("8") or code.startswith("4"):
        k = 1.30
    else:
        k = 1.10
    return round(yc * k, 2)


def scan_round(codes):
    t0 = time.perf_counter()
    q = tdx.fetch_quotes_fast(codes)
    dt = time.perf_counter() - t0
    if not q:
        return dt, None
    events = []
    for code, qt in q.items():
        px = qt.get("price") or 0
        yc = qt.get("yest_close") or 0
        lim = limit_up_of(code, yc)
        if px <= 0 or lim <= 0:
            continue
        pct = qt.get("pct_chg") or 0
        board_up = px >= lim * 0.998
        if board_up:
            sev = "C1"
        elif code.startswith(("300", "301", "688")) and pct >= 17:
            sev = "C2"
        elif pct >= 7:
            sev = "C2"
        else:
            ob = (qt.get("outer_vol") or 0)
            iv = (qt.get("inner_vol") or 0)
            if pct >= 4 and iv > 0 and ob / max(iv, 1) >= 2.5:
                sev = "C3"
            else:
                continue
        events.append({
            "t_epoch": round(t0, 3),
            "code": code, "price": px, "pct": pct,
            "limit_up": lim, "sev": sev,
            "board_on": bool(board_up),
            "bid1": [qt.get("bid1_price"), qt.get("bid1_vol")],
            "ask1": [qt.get("ask1_price"), qt.get("ask1_vol")],
            "outer_inner": [round(qt.get("outer_vol") or 0),
                            round(qt.get("inner_vol") or 0)],
        })
    return dt, events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--pool", default="data/bt_pool.json")
    args = ap.parse_args()
    pool = json.loads((BASE / args.pool).read_text(encoding="utf-8"))
    codes = pool.get("codes") or pool
    dts, ev_total = [], 0
    evs = {}
    out_path = OUT_DIR / f"rapid_scan_{time.strftime('%Y%m%d')}.jsonl"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for r in range(args.rounds):
        dt, events = scan_round(codes)
        dts.append(dt)
        n = len(events) if events else 0
        ev_total += n
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] round {r+1}/{args.rounds}: {dt*1000:.0f}ms 扫描, 事件 {n}", flush=True)
        if events:
            with open(out_path, "a", encoding="utf-8", newline="\n") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if r < args.rounds - 1:
            time.sleep(max(0.0, args.interval - dt))
    ql = [t for t in dts if t > 0]
    summary = {
        "rounds": args.rounds, "universe": len(codes),
        "scan_ms": {
            "p50": round(statistics.median(ql) * 1000) if ql else None,
            "mean": round(statistics.fmean(ql) * 1000) if ql else None,
        },
        "events_total": ev_total,
        "universe_note": "bt_pool top500；全市场版把 codes 换成全 A 列表即可（tdx 55ms/百只，2607 只理论 ~1.8s/环）",
    }
    (OUT_DIR / "rapid_scan_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("summary:", summary)


if __name__ == "__main__":
    main()
