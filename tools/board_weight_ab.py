# -*- coding: utf-8 -*-
"""P66 打板评分再配权验证 — 按 board_components.md 建议跑变体
基线: score_board 现权重
变体: 放量扫板 +15→8、动量启动 +10→0（双降权）；另保留单变体对照
同日四窗 A/B；预注册: ≥3 窗不恶化且均值>0 才建议落地
"""
import json, os, sys, time, pathlib
BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from app import config as C
C.BOARD_MOMENTUM_MIN = 7.0  # 冻结到发布基线口径

WINDOWS = [
    ["2019-01-01","2020-12-31"],
    ["2021-01-01","2022-12-31"],
    ["2023-01-01","2024-12-31"],
    ["2025-08-18","2026-08-21"],
]
WINDOW_TAGS = ["2019-20","2021-22","2023-24","近1年"]
POOL = BASE / "data" / "bt_pool.json"
OUT = BASE / "data" / "board_weight_ab.json"

VARIANTS = {
    "baseline": None,
    "vol_sweep_8": {"vol_sweep": 8},           # 放量扫板 15→8
    "momentum_7_0": {"momentum_7": 0},         # 动量启动 10→0
    "both": {"vol_sweep": 8, "momentum_7": 0}, # 双降权（主变体）
}

def run_one(strategy, widx, board_weights):
    from app import engine as eng
    from app import scoring as sc
    orig = sc.score_board
    if board_weights is not None:
        def patched(klines, quote, hour=None, board_weights=board_weights):
            return orig(klines, quote, hour=hour, board_weights=board_weights)
        # monkey patch via engine's sc reference
        import app.engine as eng_mod
        old = eng_mod.sc.score_board
        eng_mod.sc.score_board = patched
        try:
            return _run_engine(widx)
        finally:
            eng_mod.sc.score_board = old
    else:
        return _run_engine(widx)

def _run_engine(widx):
    from app import engine as eng
    pool = json.loads(POOL.read_text(encoding="utf-8"))
    codes = pool["codes"][:500]
    names = {c: c for c in codes}
    w0,w1 = WINDOWS[widx]
    # monkey fetch_quotes -> {}
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, "board",
                          {"buy_threshold":40,"max_positions":2,"position_pct":0.25,"slippage":0.001})
        r = bt.run()
        return {
            "total_return": r.get("total_return"),
            "max_drawdown": r.get("max_drawdown"),
            "sharpe": r.get("sharpe"),
            "trade_count": r.get("trade_count"),
            "win_rate": r.get("win_rate"),
        }
    finally:
        eng.df.fetch_quotes = orig_q

def main():
    t0 = time.time()
    results = {}
    for vname, bw in VARIANTS.items():
        results[vname] = {}
        for wi in range(len(WINDOWS)):
            print(f"[{vname}] win{wi} {WINDOW_TAGS[wi]} ...", flush=True)
            st = time.time()
            r = run_one("board", wi, bw)
            results[vname][str(wi)] = r
            print(f"  -> ret={r['total_return']:+.4f} dd={r['max_drawdown']:+.4f} n={r['trade_count']} ({time.time()-st:.1f}s)", flush=True)

    # 预注册判定：主变体 both vs baseline，≥3窗不恶化且均值>0
    # ★ Phase70：不恶化判定委托 judge_kit（Δ≥-0.5pp 口径）
    _tdir = os.path.dirname(os.path.abspath(__file__))
    if _tdir not in sys.path:
        sys.path.insert(0, _tdir)
    import judge_kit as jk
    both = results["both"]
    base = results["baseline"]
    deltas = []
    loss_pp_list = []
    for wi in range(len(WINDOWS)):
        d = (both[str(wi)]["total_return"] or 0) - (base[str(wi)]["total_return"] or 0)
        deltas.append(d)
        loss_pp_list.append(-d * 100)   # Δ 为负=损失；judge_kit 损失取正数 pp
    _, not_worse = jk.window_pass(loss_pp_list, tol_loss_pp=0.5, min_windows=3)
    mean_delta = sum(deltas)/len(deltas) if deltas else 0
    verdict = "建议落地" if (not_worse >=3 and mean_delta>0) else "不建议落地"
    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "P66",
        "tool": "tools/board_weight_ab.py",
        "note_BOARD_MOMENTUM_MIN": "冻结 7.0（发布口径）",
        "windows": WINDOWS,
        "window_tags": WINDOW_TAGS,
        "variants": VARIANTS,
        "results": results,
        "prereg": {
            "主变体": "both (vol_sweep 15→8, momentum_7 10→0)",
            "对比": "baseline",
            "deltas_pp": [round(d*100,2) for d in deltas],
            "not_worse_windows": not_worse,
            "mean_delta_pp": round(mean_delta*100,2),
            "criterion": "≥3 窗不恶化(Δ≥-0.5pp) 且 均值>0",
            "verdict": verdict,
        },
        "elapsed_sec": round(time.time()-t0,1),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"\n=== P66 预注册判定: {verdict} (not_worse={not_worse}/4, mean {mean_delta*100:+.2f}pp) ===")
    print(f"已写出 {OUT}")

if __name__ == "__main__":
    main()
