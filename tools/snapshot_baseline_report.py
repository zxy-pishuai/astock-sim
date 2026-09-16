# -*- coding: utf-8 -*-
"""P53 基线对账与漂移监控 — 聚合 snapshot_baseline_parts 的 2*2*4 窗

输入: data/snapshot_baseline_parts/{snapshot,live}_{score,board}.json
输出: data/snapshot_baseline.json + 终端预注册判定

判定（预注册）: 所有窗口 |Δ收益|≤0.5pp 且 |Δ回撤|≤0.5pp → 宣布冻结基线生效
报告中注明: 基线工具内 BOARD_MOMENTUM_MIN 冻结为 7.0（发布口径），与实盘 8.0 不同属预期设计
"""
import json, os, sys, time, pathlib

BASE = pathlib.Path(__file__).resolve().parents[1]
DATA = BASE / "data"
PARTS = DATA / "snapshot_baseline_parts"
OUT_JSON = DATA / "snapshot_baseline.json"

WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-21"],
]
WINDOW_TAGS = ["2019-20", "2021-22", "2023-24", "近1年"]
TOL_PP = 0.005  # 0.5pp

def load_parts():
    out = {}
    for tag in ("snapshot", "live"):
        for strat in ("score", "board"):
            p = PARTS / f"{tag}_{strat}.json"
            if not p.exists():
                print(f"[warn] missing {p}")
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            # normalize windows keys to int
            wins = d.get("windows", {})
            norm = {}
            for k, v in wins.items():
                try:
                    ik = int(k)
                except Exception:
                    ik = k
                norm[str(ik)] = v
            out[(tag, strat)] = {"tag": d.get("tag", tag), "strategy": strat, "windows": norm, "path": str(p)}
    return out

def summarize(parts):
    rows = []
    max_d_ret = 0.0
    max_d_dd = 0.0
    all_ok = True
    missing = []
    for strat in ("score", "board"):
        for wi, tag in enumerate(WINDOW_TAGS):
            key = str(wi)
            snap = parts.get(("snapshot", strat), {}).get("windows", {}).get(key)
            live = parts.get(("live", strat), {}).get("windows", {}).get(key)
            if snap is None or live is None:
                missing.append((strat, wi, tag, "snapshot" if snap is None else "live"))
                all_ok = False
                continue
            d_ret = (live.get("total_return") or 0) - (snap.get("total_return") or 0)
            d_dd = (live.get("max_drawdown") or 0) - (snap.get("max_drawdown") or 0)
            d_ret_pp = d_ret * 100
            d_dd_pp = d_dd * 100
            max_d_ret = max(max_d_ret, abs(d_ret))
            max_d_dd = max(max_d_dd, abs(d_dd))
            ok = abs(d_ret) <= TOL_PP and abs(d_dd) <= TOL_PP
            if not ok:
                all_ok = False
            rows.append({
                "strategy": strat,
                "widx": wi,
                "window": WINDOWS[wi],
                "window_tag": tag,
                "snapshot": {
                    "total_return": snap.get("total_return"),
                    "max_drawdown": snap.get("max_drawdown"),
                    "sharpe": snap.get("sharpe"),
                    "trade_count": snap.get("trade_count"),
                },
                "live": {
                    "total_return": live.get("total_return"),
                    "max_drawdown": live.get("max_drawdown"),
                    "sharpe": live.get("sharpe"),
                    "trade_count": live.get("trade_count"),
                },
                "delta": {
                    "total_return_pp": round(d_ret * 100, 4),
                    "max_drawdown_pp": round(d_dd * 100, 4),
                    "total_return_abs": round(d_ret, 6),
                    "max_drawdown_abs": round(d_dd, 6),
                },
                "pass": ok,
            })
    return rows, all_ok, max_d_ret, max_d_dd, missing

def main():
    t0 = time.time()
    parts = load_parts()
    rows, all_ok, max_d_ret, max_d_dd, missing = summarize(parts)
    verdict = "冻结基线生效" if (all_ok and not missing) else "未生效"
    # pre-registered criterion
    criterion = f"所有窗口 |Δ收益|≤0.5pp 且 |Δ回撤|≤0.5pp (当前 max |Δ收益|={max_d_ret*100:.3f}pp, max |Δ回撤|={max_d_dd*100:.3f}pp; 缺失 {len(missing)} 窗口)"
    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "P53",
        "tool": "tools/snapshot_baseline_report.py",
        "criterion": criterion,
        "verdict": verdict,
        "pass": bool(all_ok and not missing),
        "tolerance_pp": 0.5,
        "note_BOARD_MOMENTUM_MIN": "基线工具内 BOARD_MOMENTUM_MIN 冻结为 7.0（发布口径），与实盘 8.0 不同属预期设计（Phase33 已切实盘 8.0，基线保持 7.0 以与历史可比）",
        "windows": WINDOWS,
        "window_tags": WINDOW_TAGS,
        "parts_found": sorted([f"{k[0]}_{k[1]}" for k in parts.keys()]),
        "missing_windows": [{"strategy": s, "widx": wi, "tag": tag, "missing_side": side} for s, wi, tag, side in missing],
        "comparison": rows,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    tmp.replace(OUT_JSON)
    # terminal summary
    print("=" * 72)
    print(f"P53 快照基线对账 {OUT_JSON}")
    print(f"  判定: {verdict} — {criterion}")
    print(f"  口径: BOARD_MOMENTUM_MIN=7.0 冻结（与实盘8.0不同，预期设计）")
    if missing:
        print(f"  缺失窗口 {len(missing)}:")
        for s, wi, tag, side in missing:
            print(f"    {s} {tag} 缺 {side}")
    for r in rows:
        mark = "OK" if r["pass"] else "超差"
        print(f"  [{mark}] {r['strategy']:5s} {r['window_tag']:6s} Δ收益 {r['delta']['total_return_pp']:+.3f}pp  Δ回撤 {r['delta']['max_drawdown_pp']:+.3f}pp  快照 {r['snapshot']['total_return']:+.4f}/{r['snapshot']['max_drawdown']:+.4f}  活库 {r['live']['total_return']:+.4f}/{r['live']['max_drawdown']:+.4f}")
    print(f"已写出 {OUT_JSON} 耗时 {out['elapsed_sec']}s")
    if missing:
        print("[提示] 仍有窗口未完成，待 2 个 score 进程结束后重跑本工具即可自动补齐（断点续跑）")

if __name__ == "__main__":
    main()
