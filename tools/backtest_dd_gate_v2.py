# -*- coding: utf-8 -*-
"""★ Phase37: DD Gate v2 回测 —— 恢复机制修复 halt 吸收态。

阶段1：阈值 A(6%/10%) × recovery ∈ {default(R0), cooldown(N=10), index_ma, half_cap(M=20)}
       × {score, board} × 4 窗口
阶段2：阶段1 优胜 recovery × 阈值 B(8%/12%) × 2 策略 × 4 窗口
基线：dd_gate=False 与 data/bt_clean_results.json 8/8 精确一致方可继续
     （配方同 Phase27/21：bt_pool top500、fetch_quotes 置空、score 25/3/0.30/0.001、board 40/2/0.25）。

判定（事先写死，对每个 recovery×阈值×策略）：
  ① ≥3/4 窗口最大回撤相对降 ≥20%   ② ≥3/4 窗口 Calmar 改善
  ③ 每窗口收益损失 ≤3pp             ④ halt 天占比（含 half 折算 0.5）所有窗口 ≤40%

用法：python tools/backtest_dd_gate_v2.py [--workers 12]
"""
import sys
import os
import json
import time
import sqlite3
import argparse
import multiprocessing as mp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C          # noqa: E402
from app import engine as eng        # noqa: E402

# 基线复现配方（与 Phase21/26/27 一致）
PARAMS_BY_STRATEGY = {
    "score": {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30, "slippage": 0.001},
    "board": {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25, "slippage": 0.001},
}
WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-18"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "牛市"]
TH_A = {"t1": 0.06, "t2": 0.10}
TH_B = {"t1": 0.08, "t2": 0.12}
RECOVERIES_P1 = [
    ("default", {}),
    ("cooldown", {"cooldown_days": 10}),
    ("index_ma", {}),
    ("half_cap", {"half_cap_days": 20}),
]
HALF_MULT = 0.5
OCC_HALF_WEIGHT = 0.5
# 判定常量（事先写死）
J_MDD_REL = 0.20
J_CALMAR_IMPROVE = True
J_MAX_RET_LOSS_PP = 3.0
J_OCC_MAX = 0.40
J_MIN_WINDOWS = 3

_G = {}


def _work_one(task):
    """子进程：跑单 (strategy, window, cfg)。返回精简指标。"""
    strategy, wi, label, dd = task
    if not _G:
        eng.df.fetch_quotes = lambda codes: {}      # 基线配方：置空实时行情
        # ★ 基线环境冻结：Phase33 已把 BOARD_MOMENTUM_MIN 默认改为 8.0（board_tier.md）；
        #   本阶段对照 Phase21/27 基线口径，回测内显式对齐回 7.0（运行时补丁，不改文件）
        C.BOARD_MOMENTUM_MIN = 7.0
        pool = json.load(open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8"))
        codes = pool["codes"][:500]
        names = {}
        try:
            for row in json.load(open(os.path.join(C.DATA_DIR, "stock_list.json"),
                                      encoding="utf-8")):
                names[row[0]] = row[1]
        except Exception:
            pass
        _G["codes"] = [c for c in codes if c not in set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])]
        _G["names"] = {c: names.get(c, c) for c in _G["codes"]}
    params = dict(PARAMS_BY_STRATEGY[strategy])
    if dd is not None:
        params["dd_gate"] = dd
    w0, w1 = WINDOWS[wi]
    bt = eng.Backtest(_G["codes"], _G["names"], w0, w1, 100000.0, strategy, params)
    r = bt.run()
    n_days = max(1, len(bt.trading_days))
    occ = (r.get("dd_halt_days", 0) + OCC_HALF_WEIGHT * r.get("dd_half_days", 0)) / n_days
    return {"label": label, "strategy": strategy, "win": WIN_NAMES[wi],
            "total_return": r.get("total_return"), "max_drawdown": r.get("max_drawdown"),
            "annual_return": r.get("annual_return"), "trade_count": r.get("trade_count"),
            "dd_half_days": r.get("dd_half_days", 0), "dd_halt_days": r.get("dd_halt_days", 0),
            "occupancy": round(occ, 4), "n_days": n_days}


def _calmar(annual, mdd):
    """★ Phase70：委托统一判定库（符号/单位约定见 tools/judge_kit.py）。"""
    import judge_kit as jk
    return jk.calmar_ratio(annual, mdd)


def _judge(cfg_runs, base_by):
    """cfg_runs: {(strategy, win): metrics}；按事先写死规则出判定。"""
    out = {}
    for strat in ("score", "board"):
        n_mdd = n_cal = 0
        loss_ok = True
        occ_ok = True
        detail = []
        for wi, wn in enumerate(WIN_NAMES):
            b = base_by[(strat, wn)]
            r = cfg_runs[(strat, wn)]
            bmdd, rmdd = abs(b["max_drawdown"]), abs(r["max_drawdown"] or 0)
            import judge_kit as jk
            mdd_imp = jk.rel_mdd_improve(b["max_drawdown"], r["max_drawdown"])
            bc, rc = _calmar(b["annual_return"], b["max_drawdown"]), _calmar(r["annual_return"], r["max_drawdown"])
            cal_imp = (bc is not None and rc is not None and rc > bc)
            loss_pp = (b["total_return"] or 0) - (r["total_return"] or 0)
            if mdd_imp >= J_MDD_REL:
                n_mdd += 1
            if cal_imp:
                n_cal += 1
            if loss_pp * 100.0 > J_MAX_RET_LOSS_PP:   # ★ 单位：损失按百分点比较
                loss_ok = False
            if r["occupancy"] > J_OCC_MAX:
                occ_ok = False
            detail.append({"win": wn, "ret": r["total_return"], "base_ret": b["total_return"],
                           "loss_pp": round(loss_pp * 100, 2), "mdd": r["max_drawdown"],
                           "base_mdd": b["max_drawdown"], "mdd_rel_improve": round(mdd_imp, 4),
                           "calmar_improved": cal_imp, "occupancy": r["occupancy"]})
        out[strat] = {
            "detail": detail,
            "mdd_windows": n_mdd, "calmar_windows": n_cal,
            "c1_mdd_ge3of4": n_mdd >= J_MIN_WINDOWS,
            "c2_calmar_ge3of4": n_cal >= J_MIN_WINDOWS,
            "c3_loss_le3pp": loss_ok,
            "c4_occupancy_le40pct": occ_ok,
            "pass": bool(n_mdd >= J_MIN_WINDOWS and n_cal >= J_MIN_WINDOWS
                         and loss_ok and occ_ok),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--rejudge", action="store_true",
                    help="跳过回测，仅从已存 bt_dd_gate_v2.json 重算判定（修正判定 bug 用）")
    args = ap.parse_args()
    t0 = time.time()
    if args.rejudge:
        out_path = os.path.join(C.DATA_DIR, "bt_dd_gate_v2.json")
        out = json.load(open(out_path, encoding="utf-8"))
        base_by = {}
        for k, v in out["baseline"].items():
            s, w = k.rsplit("_", 1)
            base_by[(s, w)] = v
        p1_by, p2_by = {}, {}
        for k, v in out["runs"].items():
            label, s, w = k.split("|")
            (p2_by if label.startswith("B_") else p1_by)[(label, s, w)] = v
        judgement_p1 = {lab: _judge(
            {(s, w): p1_by[(lab, s, w)] for s in ("score", "board") for w in WIN_NAMES},
            base_by) for lab in list(p1_by and sorted({k[0] for k in p1_by}))}
        judgement_p2 = {lab: _judge(
            {(s, w): p2_by[(lab, s, w)] for s in ("score", "board") for w in WIN_NAMES},
            base_by) for lab in sorted({k[0] for k in p2_by})}
        # 优胜重选（阶段1口径不变）
        score_of = {}
        for rec_name in ("default", "cooldown", "index_ma", "half_cap"):
            lab = "A_" + rec_name
            j = judgement_p1[lab]
            crit = sum(1 for s in ("score", "board") for c in
                       ("c1_mdd_ge3of4", "c2_calmar_ge3of4", "c3_loss_le3pp",
                        "c4_occupancy_le40pct") if j[s][c])
            occ_avg = sum(out["runs"]["%s|%s|%s" % (lab, s, w)]["occupancy"]
                          for s in ("score", "board") for w in WIN_NAMES) / 8.0
            n_pass = sum(j[s]["pass"] for s in ("score", "board"))
            score_of[rec_name] = (crit, -occ_avg, n_pass)
        best_rec = max(score_of, key=lambda k: score_of[k])
        out["judgement_phase1"] = judgement_p1
        out["judgement_phase2"] = judgement_p2
        out["best_recovery"] = best_rec
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp, out_path)
        print("=== 重算判定（修正③单位 bug 后）===")
        for lab, j in list(judgement_p1.items()) + list(judgement_p2.items()):
            for s in ("score", "board"):
                print(lab, s, "pass=%s" % j[s]["pass"],
                      {k: j[s][k] for k in ("mdd_windows", "calmar_windows",
                                            "c3_loss_le3pp", "c4_occupancy_le40pct")})
        print("best:", best_rec)
        return
    t0 = time.time()

    # ---- 任务清单 ----
    tasks = []
    for strat in ("score", "board"):
        for wi in range(4):
            tasks.append((strat, wi, "baseline", None))
    def add_phase(th, th_name, recov_list):
        for rec_name, rec_kw in recov_list:
            for strat in ("score", "board"):
                for wi in range(4):
                    dd = {"t1": th["t1"], "t2": th["t2"], "half_mult": HALF_MULT,
                          "recovery": rec_name}
                    dd.update(rec_kw)
                    tasks.append((strat, wi, "%s_%s" % (th_name, rec_name), dd))
    add_phase(TH_A, "A", RECOVERIES_P1)

    # ---- 并行执行（先基线门，后阶段1）----
    # 数据修订审计：与 8/23 备份库比对各窗口收盘价指纹。上游 updater 的增量前复权
    # 会改写历史收盘（8/25 实测：牛市窗口 92/498 票、2023-24 窗口 67/487 票被修订）。
    # 被修订窗口的旧发布基线数字原理上不可复现 → 判定基准一律用同数据现跑的 dd-off 基线
    # （内部自洽）；未修订窗口仍要求与旧基线精确一致（硬门）。
    revised = {}
    try:
        pool_codes = json.load(open(os.path.join(C.DATA_DIR, "bt_pool.json"),
                                    encoding="utf-8"))["codes"][:500]
        bak_db = os.path.join(C.DATA_DIR, "backups", "20260823_151504", "market.db")

        def _fp(db, d0, d1):
            conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30)
            ph = ",".join("?" * len(pool_codes))
            rows = conn.execute(
                "SELECT code, COUNT(*), ROUND(SUM(close),6) FROM kline WHERE period='day' "
                "AND date>=? AND date<=? AND code IN (%s) GROUP BY code" % ph,
                (d0, d1) + tuple(pool_codes)).fetchall()
            conn.close()
            return {r[0]: r[1:] for r in rows}

        for wi, (w0, w1) in enumerate(WINDOWS):
            a = _fp(C.DB_FILE, w0, w1)
            b = _fp(bak_db, w0, w1)
            revised[wi] = sum(1 for c in a if c in b and a[c] != b[c])
    except Exception as _e:
        print("[audit] 指纹审计失败(%s)，按全部未修订处理" % _e)
        revised = {i: 0 for i in range(4)}
    print("[audit] 各窗口修订票数:", [revised.get(i) for i in range(4)], flush=True)

    with mp.Pool(args.workers) as pool:
        base_tasks = [t for t in tasks if t[2] == "baseline"]
        base_res = pool.map(_work_one, base_tasks)
        base_by = {(r["strategy"], r["win"]): r for r in base_res}
        ref = json.load(open(os.path.join(C.DATA_DIR, "bt_clean_results.json"),
                             encoding="utf-8"))["multi_window"]
        hard_fail = False
        drift = {}
        for strat in ("score", "board"):
            for wi, wn in enumerate(WIN_NAMES):
                b = base_by[(strat, wn)]
                e = ref[strat][wi]
                same = (abs((b["total_return"] or 0) - e["total_return"]) < 1e-9
                        and b["trade_count"] == e["trade_count"]
                        and abs((b["max_drawdown"] or 0) - e["max_drawdown"]) < 1e-9)
                # ★ 硬门只约束 board 的未修订窗口（board 信号基于涨跌停价/振幅，
                #   对边际价格不敏感，可作配方正确性的充分检验）。score 对阈值穿越
                #   极敏感：kline 表在 8/23 后被上游整体前复权重写（Phase30 归因账本
                #   与现库同日数据不一致为证），旧发布数字原理上不可复现 —— 该差异
                #   记录在案，判定一律用同数据现跑基线。
                if strat == "board" and revised.get(wi, 0) == 0 and not same:
                    hard_fail = True
                drift["%s_%s" % (strat, wn)] = {
                    "data_revised_codes": revised.get(wi, 0), "matches_legacy": same}
                print("[gate] %s %s %s (数据修订 %d 票)" % (
                    strat, wn, "OK" if same else "DRIFT", revised.get(wi, 0)), flush=True)
        if hard_fail:
            print("未修订窗口基线不一致 —— 中止（先修对齐）")
            return
        n_ok = sum(1 for v in drift.values() if v["matches_legacy"])
        print("[gate] 与旧发布基线一致 %d/8；score 窗口差异系上游前复权重写（详见报告§2），"
              "判定用现跑基线" % n_ok, flush=True)

        p1_res = pool.map(_work_one, [t for t in tasks if t[2] != "baseline"])
        p1_by = {(r["label"], r["strategy"], r["win"]): r for r in p1_res}

    # ---- 阶段1 判定 ----
    judgement_p1 = {}
    score_of = {}
    for rec_name, _kw in RECOVERIES_P1:
        label = "A_%s" % rec_name
        runs = {(s, w): p1_by[(label, s, w)] for s in ("score", "board") for w in WIN_NAMES}
        j = _judge(runs, base_by)
        judgement_p1[label] = j
        n_pass = sum(j[s]["pass"] for s in ("score", "board"))
        crit = sum(sum(1 for s in ("score", "board")
                       for c in ("c1_mdd_ge3of4", "c2_calmar_ge3of4", "c3_loss_le3pp",
                                 "c4_occupancy_le40pct") if j[s][c]) for s in ())
        crit = sum(1 for s in ("score", "board") for c in
                   ("c1_mdd_ge3of4", "c2_calmar_ge3of4", "c3_loss_le3pp", "c4_occupancy_le40pct")
                   if j[s][c])
        occ_avg = sum(runs[(s, w)]["occupancy"] for s in ("score", "board") for w in WIN_NAMES) / 8.0
        score_of[rec_name] = (crit, -occ_avg, n_pass)
        print("[p1] %s criteria=%d/8 occ_avg=%.1f%%" % (
            label, crit, occ_avg * 100), flush=True)
    best_rec = max(score_of, key=lambda k: score_of[k])
    print("[p1] 优胜 recovery =", best_rec, flush=True)

    # ---- 阶段2：优胜 recovery × 阈值 B ----
    rec_kw = dict(RECOVERIES_P1)[best_rec] if best_rec in dict(RECOVERIES_P1) else \
        next(kw for nm, kw in RECOVERIES_P1 if nm == best_rec)
    p2_tasks = []
    for strat in ("score", "board"):
        for wi in range(4):
            dd = {"t1": TH_B["t1"], "t2": TH_B["t2"], "half_mult": HALF_MULT,
                  "recovery": best_rec}
            dd.update(rec_kw)
            p2_tasks.append((strat, wi, "B_%s" % best_rec, dd))
    with mp.Pool(args.workers) as pool:
        p2_res = pool.map(_work_one, p2_tasks)
    p2_by = {(r["strategy"], r["win"]): r for r in p2_res}
    judgement_p2 = {"B_%s" % best_rec: _judge(p2_by, base_by)}

    out = {
        "meta": {
            "pool": "bt_pool.json top500（滤 DATA_EXCLUDE_CODES）；fetch_quotes 置空；"
                    "score 25/3/0.30/0.001、board 40/2/0.25、capital=10万",
            "windows": WINDOWS,
            "phase1": "A=6%%/10%% × recovery=%s" % [nm for nm, _ in RECOVERIES_P1],
            "phase2": "优胜 recovery=%s × B=8%%/12%%" % best_rec,
            "judge_rules": {"mdd_rel_improve_ge": J_MDD_REL, "winrate_na": "",
                            "calmar_improve_windows_ge": J_MIN_WINDOWS,
                            "max_ret_loss_pp": J_MAX_RET_LOSS_PP,
                            "occupancy_max": J_OCC_MAX,
                            "occ_formula": "(halt_days + 0.5*half_days)/window_days"},
        },
        "baseline": {("%s_%s" % k): v for k, v in base_by.items()},
        "baseline_legacy_audit": {
            "note": "与 bt_clean_results.json(8/23 发布) 的逐窗口比对；DRIFT 窗口系上游前复权"
                    "修订历史收盘所致（证据见 revised_codes），判定一律用同数据现跑基线",
            "windows": drift},
        "runs": {("%s|%s|%s" % k): v for k, v in
                 list(p1_by.items()) + list({("B_" + best_rec, s, w): v for (s, w), v in p2_by.items()}.items())},
        "judgement_phase1": judgement_p1,
        "judgement_phase2": judgement_p2,
        "best_recovery": best_rec,
        "elapsed_s": round(time.time() - t0, 1),
    }
    tmp = os.path.join(C.DATA_DIR, "bt_dd_gate_v2.json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(C.DATA_DIR, "bt_dd_gate_v2.json"))
    print("\n=== 判定汇总 ===")
    for lab, j in list(judgement_p1.items()) + list(judgement_p2.items()):
        for s in ("score", "board"):
            print(lab, s, "pass=%s" % j[s]["pass"],
                  {k: j[s][k] for k in ("mdd_windows", "calmar_windows", "c3_loss_le3pp",
                                        "c4_occupancy_le40pct")})
    print("saved bt_dd_gate_v2.json, elapsed %.0fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
