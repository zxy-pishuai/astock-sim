# -*- coding: utf-8 -*-
"""P71 冻结基线重跑（后台版）— score 4窗慢（500-1800s），用 bt_snapshot_baseline 风格分治"""
import json, sys, time, pathlib
BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from app import data_snapshot, config as C
# Force use snapshot DB + BOARD 8.0 (current default already 8.0, but ensure not frozen 7.0)
snap = data_snapshot.latest_snapshot_path()
orig_db = C.DB_FILE
if snap and pathlib.Path(snap).exists():
    C.DB_FILE = snap
print(f"[bg] snapshot={snap} exclude={len(C.DATA_EXCLUDE_CODES)} BOARD_MOMENTUM 8.0={C.BOARD_MOMENTUM_MIN}", flush=True)
from app import engine as eng

WINDOWS=[["2019-01-01","2020-12-31"],["2021-01-01","2022-12-31"],["2023-01-01","2024-12-31"],["2025-08-18","2026-08-21"]]
TAGS=["2019-20","2021-22","2023-24","近1年"]

# Load before baseline for delta
pre = json.loads((BASE/"data/snapshot_baseline.json").read_text(encoding="utf-8"))
pre_lookup={}
for row in pre["comparison"]:
    pre_lookup[(row["strategy"], row["widx"])] = row["snapshot"]

results=[]
for strategy in ["board","score"]:
    for wi in range(4):
        tag=TAGS[wi]
        w0,w1=WINDOWS[wi]
        part_path=BASE/f"data/bt_exclude_landing_regression_parts/{strategy}_{wi}.json"
        if part_path.exists():
            print(f"[bg] {strategy} {tag} 已有 {part_path} 跳过", flush=True)
            try:
                cur=json.loads(part_path.read_text(encoding="utf-8"))
                results.append({"strategy":strategy,"widx":wi,"window":[w0,w1],"window_tag":tag,"before":pre_lookup.get((strategy,wi),{}),"after":cur,"delta_pp":{"total_return":round((cur["total_return"]-(pre_lookup.get((strategy,wi),{}).get("total_return")or 0))*100,2),"max_drawdown":round((cur["max_drawdown"]-(pre_lookup.get((strategy,wi),{}).get("max_drawdown")or 0))*100,2)}})
            except Exception as e:
                print(f"[bg] read err {e}", flush=True)
            continue
        print(f"[bg] {strategy} {tag} ({w0}->{w1}) starting...", flush=True)
        t0=time.time()
        pool=json.loads((BASE/"data/bt_pool.json").read_text(encoding="utf-8"))
        codes=pool["codes"][:500]
        names={c:c for c in codes}
        orig_q=eng.df.fetch_quotes
        eng.df.fetch_quotes=lambda cs: {}
        try:
            params={"buy_threshold":25 if strategy=="score" else 40,"max_positions":3 if strategy=="score" else 2,"position_pct":0.30 if strategy=="score" else 0.25,"slippage":0.001}
            bt=eng.Backtest(codes, names, w0,w1, 100000.0, strategy, params)
            r=bt.run()
            cur={"total_return":r.get("total_return"),"max_drawdown":r.get("max_drawdown"),"sharpe":r.get("sharpe"),"trade_count":r.get("trade_count"),"win_rate":r.get("win_rate"),"annual_return":r.get("annual_return")}
        finally:
            eng.df.fetch_quotes=orig_q
        elapsed=round(time.time()-t0,1)
        print(f"[bg] {strategy} {tag} done {elapsed}s ret={cur['total_return']:+.4f} dd={cur['max_drawdown']:+.4f}", flush=True)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        results.append({"strategy":strategy,"widx":wi,"window":[w0,w1],"window_tag":tag,"before":pre_lookup.get((strategy,wi),{}),"after":cur,"delta_pp":{"total_return":round((cur["total_return"]-(pre_lookup.get((strategy,wi),{}).get("total_return")or 0))*100,2),"max_drawdown":round((cur["max_drawdown"]-(pre_lookup.get((strategy,wi),{}).get("max_drawdown")or 0))*100,2)}})

# Aggregate
out={"generated_at":time.strftime("%Y-%m-%d %H:%M:%S"),"phase":"P71","tool":"frozen snapshot baseline regression (board+score 4窗, board 8.0, snapshot)","snapshot":snap,"recipe":"bt_pool top500 fetch_quotes置空 score25/3/0.30 board40/2/0.25 BOARD_MOMENTUM_MIN=8.0 C.DB_FILE=latest_snapshot","excluded_codes_count":len(C.DATA_EXCLUDE_CODES),"comparison":sorted(results, key=lambda x: (x["strategy"], x["widx"]))}
out_path=BASE/"data/bt_exclude_landing_regression.json"
out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
print(f"[bg] Written {out_path}", flush=True)
C.DB_FILE=orig_db
