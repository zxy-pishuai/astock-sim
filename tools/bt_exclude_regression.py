# -*- coding: utf-8 -*-
"""P71 冻结基线重跑 — 用最新快照重跑 score+board 四窗，量化排除扩容影响

配方: bt_pool top500, fetch_quotes置空, score 25/3/0.30/0.001 board 40/2/0.25/0.001
      BOARD_MOMENTUM_MIN 用当前默认 8.0 不冻结，C.DB_FILE=latest_snapshot
与落地前基线并列存档到 data/bt_exclude_landing_regression.json
"""
import json, sys, time, pathlib
BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from app import data_snapshot, config as C
# ★ B2（2026-09-13）：快照新鲜度断言——超期（默认 >3 天）直接拒绝运行，杜绝
#   过期快照静默进入回测。snapshot_tag/age/coverage 进结果 JSON 供追溯。
snap = data_snapshot.latest_snapshot_path(max_age_days=3)
if not snap:
    print("[B2] 无可用快照或最新快照超期（>3 天，含无快照），拒绝运行——"
          "请先补快照（python tools/data_snapshot.py --force 或等服务收盘自动生成）",
          flush=True)
    sys.exit(2)
snap_info = data_snapshot.snapshot_info(snap)
orig_db = C.DB_FILE
if pathlib.Path(snap).exists():
    C.DB_FILE = snap
print(f"[regression] snapshot_tag={snap_info['tag']} age={snap_info['age_days']}d "
      f"coverage={snap_info['coverage'] or {}}", flush=True)
print(f"[regression] BOARD_MOMENTUM_MIN={C.BOARD_MOMENTUM_MIN}", flush=True)
print(f"[regression] excluded={len(C.DATA_EXCLUDE_CODES)}", flush=True)

from app import engine as eng

WINDOWS=[["2019-01-01","2020-12-31"],["2021-01-01","2022-12-31"],["2023-01-01","2024-12-31"],["2025-08-18","2026-08-21"]]
TAGS=["2019-20","2021-22","2023-24","近1年"]

def run_one(widx, strategy):
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        pool = json.loads((BASE/"data/bt_pool.json").read_text(encoding="utf-8"))
        codes = pool["codes"][:500]
        names = {c:c for c in codes}
        w0,w1 = WINDOWS[widx]
        params = {"buy_threshold": 25 if strategy=="score" else 40,
                  "max_positions": 3 if strategy=="score" else 2,
                  "position_pct": 0.30 if strategy=="score" else 0.25,
                  "slippage": 0.001}
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, strategy, params)
        r = bt.run()
        return {"total_return": r.get("total_return"),"max_drawdown": r.get("max_drawdown"),"sharpe": r.get("sharpe"),"trade_count": r.get("trade_count"),"win_rate": r.get("win_rate"),"annual_return": r.get("annual_return")}
    finally:
        eng.df.fetch_quotes = orig_q

# before: use snapshot_baseline.json snapshot side
pre = json.loads((BASE/"data/snapshot_baseline.json").read_text(encoding="utf-8"))
pre_lookup={}
for row in pre["comparison"]:
    pre_lookup[(row["strategy"], row["widx"])] = row["snapshot"]

results=[]
for strategy in ["score","board"]:
    for wi in range(4):
        print(f"[{strategy} win{wi} {TAGS[wi]}]", flush=True)
        t0=time.time()
        cur=run_one(wi, strategy)
        elapsed=round(time.time()-t0,1)
        prev=pre_lookup.get((strategy, wi), {})
        dret=(cur["total_return"]-(prev.get("total_return")or 0))*100
        ddd=(cur["max_drawdown"]-(prev.get("max_drawdown")or 0))*100
        print(f" -> ret {prev.get('total_return')} -> {cur['total_return']} ({dret:+.2f}pp) dd {prev.get('max_drawdown')} -> {cur['max_drawdown']} ({ddd:+.2f}pp) {elapsed}s", flush=True)
        results.append({"strategy":strategy,"widx":wi,"window":WINDOWS[wi],"window_tag":TAGS[wi],"before":prev,"after":cur,"delta_pp":{"total_return":round(dret,2),"max_drawdown":round(ddd,2)}})

out={"generated_at":time.strftime("%Y-%m-%d %H:%M:%S"),"phase":"P71","tool":"frozen snapshot baseline regression","snapshot":snap,
     "snapshot_tag":snap_info["tag"],"snapshot_age_days":snap_info["age_days"],
     "snapshot_coverage":snap_info["coverage"] or {},"recipe":"bt_pool top500 fetch_quotes置空 score25/3/0.30 board40/2/0.25 BOARD_MOMENTUM_MIN=8.0 C.DB_FILE=latest_snapshot","excluded_codes_count":len(C.DATA_EXCLUDE_CODES),"comparison":results}
out_path=BASE/"data/bt_exclude_landing_regression.json"
out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
print(f"Written {out_path} {len(results)} rows", flush=True)
C.DB_FILE=orig_db
