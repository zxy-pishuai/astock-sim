# -*- coding: utf-8 -*-
"""任务4｜大盘状态门控（研究型，只读库 + 新增数据文件，不改 app/config.py）

三臂结构：
  [labels] 全A等权线（bt_pool top500 剔除 DATA_EXCLUDE_CODES 的等权日收益线）
           每日三态标签：BULL / CHOP / BEAR —— 只用 ≤t 数据（20日收益、20日波动、量能比）
  [ic]     分状态检验：score_stock K线成分加权和（与 P28A 同管线）的横截面 IC、
           五分位多空价差；board 触发样本的前瞻收益超额
  [gates]  纸面门控快照回测：运行时子类拦截 engine._signals_on（不改引擎文件）
           V1=排除熊市开仓；V2=仅牛市开仓。基线 = 既有 exclude197+MOM8 无门控 parts

★ 预注册判据（先于任何门控评估固定，另见报告开头）：
  标签规则（常数 a priori 定死，不做网格搜索）：
    BEAR: r20 <= -0.05  或 (r20 <= -0.02 且 sigma20 >= 0.022)
    BULL: r20 >= +0.04  且 sigma20 <= 0.028
    其余为 CHOP。量能比 q=V5/V60 仅作共变量记录，不参与阈值。
  门控有效性判据（全部满足才建议落地）：
    G1 分状态单调性：五分位多空价差 mean(BULL)>mean(CHOP)>mean(BEAR) 对两策略成立其一即可深究；
    G2 门控增量：某变体四窗中 >=3 窗总收益不恶化（Δ>=−0.5pp）且四窗均值 > +0.5pp；
    G3 样本充足：被允许状态占交易日 >=35%（避免变成"几乎不交易"的伪改善，P28-B 教训）。
"""
import json
import os
import pathlib
import sqlite3
import statistics
import sys
import time

BASE = pathlib.Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tools"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C          # noqa: E402
from app.data_snapshot import latest_snapshot_path  # noqa: E402

SNAP = latest_snapshot_path()
assert SNAP and os.path.isfile(SNAP), "无可用快照"
ORIG_DB = C.DB_FILE
C.DB_FILE = SNAP                      # 全程冻结快照

import importlib                      # noqa: E402
if "app.engine" in sys.modules:
    importlib.reload(sys.modules["app.engine"])
from app import engine as eng         # noqa: E402

WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
TAGS = ["2019-20", "2021-22", "2023-24", "近1年"]
OUT_JSON = BASE / "data" / "bt_regime_gating.json"
PARTS = BASE / "data" / "bt_regime_gating_parts"
BASELINE_PARTS = BASE / "data" / "bt_exclude_landing_regression_parts"

# ---------------- ① 标签 ----------------
def equal_weight_line():
    """等权日收益线：池内两日均有收盘的股票等权平均日收益（因果）。返回 (dates, levels, rets, vol_sum, regimes)"""
    from factor_ic_audit import load_klines  # 复用加载器（读 C.DB_FILE=快照）
    pool = json.loads((BASE / "data" / "bt_pool.json").read_text(encoding="utf-8"))
    ex = set(C.DATA_EXCLUDE_CODES or [])
    codes = [c for c in pool["codes"][:500] if c not in ex]
    kls = load_klines(codes)
    # 日 -> [(ret, vol)] 聚合
    day_ret, day_vol = {}, {}
    for code, kl in kls.items():
        for i in range(1, len(kl)):
            c0, c1 = kl[i - 1]["close"], kl[i]["close"]
            if c0 and c0 > 0 and c1:
                d = kl[i]["date"]
                day_ret.setdefault(d, []).append(c1 / c0 - 1.0)
                day_vol.setdefault(d, 0.0)
                day_vol[d] += kl[i]["volume"] or 0.0
    dates = sorted(day_ret)
    rets = {d: statistics.fmean(v) for d, v in day_ret.items()}
    levels, lv = {}, 1.0
    for d in dates:
        lv *= (1.0 + rets[d])
        levels[d] = lv
    def sma_series(vals, n):
        out = []
        s = 0.0
        for i, x in enumerate(vals):
            s += x
            if i >= n:
                s -= vals[i - n]
            out.append(s / n if i >= n - 1 else None)
        return out
    r_list = [rets[d] for d in dates]
    sig20 = []
    for i in range(len(dates)):
        sig20.append(statistics.stdev(r_list[i - 19:i + 1]) if i >= 19 else None)
    # 量能比 V5/V60
    vd = [day_vol[d] for d in dates]
    v5 = sma_series(vd, 5)
    v60 = sma_series(vd, 60)
    reg, meta = {}, {}
    for i, d in enumerate(dates):
        if i < 60:
            continue
        r20 = levels[dates[i]] / levels[dates[i - 20]] - 1.0
        s20 = sig20[i]
        q = (v5[i] / v60[i]) if (v5[i] and v60[i]) else None
        if r20 <= -0.05 or (r20 <= -0.02 and (s20 or 0) >= 0.022):
            st = "BEAR"
        elif r20 >= 0.04 and (s20 or 1) <= 0.028:
            st = "BULL"
        else:
            st = "CHOP"
        reg[d] = st
        meta[d] = {"r20": round(r20, 4), "sigma20": round(s20, 4) if s20 else None,
                   "q_vol": round(q, 3) if q else None}
    print(f"[labels] 交易日 {len(reg)} 有标签（{dates[60]}..{dates[-1]}）")
    shares = {"BULL": 0, "CHOP": 0, "BEAR": 0}
    for v in reg.values():
        shares[v] += 1
    tot = sum(shares.values())
    print("[labels] 占比:", {k: f"{v/tot:.1%}" for k, v in shares.items()})
    return reg, meta, shares, tot


# ---------------- ② 分状态 IC ----------------
def spearman(pairs):
    """pairs=[(x_raw, y_rank)] -> 每日横截面 Spearman"""
    n = len(pairs)
    if n < 30:
        return None
    xs = sorted(range(n), key=lambda i: pairs[i][0])
    rx = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pairs[xs[j + 1]][0] == pairs[xs[i]][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            rx[xs[k]] = avg
        i = j + 1
    sr = sum(r for _, r in pairs)
    sx = sum(rx)
    sxx = sum(r * r for r in rx)
    syy = sum(r * r for _, r in pairs)
    sxy = sum(a * b for (_, b), a in zip(pairs, rx))
    cov = sxy - sx * sr / n
    vx = sxx - sx * sx / n
    vy = syy - sr * sr / n
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def state_ic(reg):
    """分状态：score_stock K线代理 IC + 五分位多空；board 触发前瞻超额"""
    import factor_ic_audit as F
    from app import scoring as sc
    pool = json.loads((BASE / "data" / "bt_pool.json").read_text(encoding="utf-8"))
    ex = set(C.DATA_EXCLUDE_CODES or [])
    codes = [c for c in pool["codes"][:500] if c not in ex]
    print(f"[ic] 池 {len(codes)} 只", flush=True)
    fwd, ranks, _sum_r2 = F.build_fwd_and_ranks(codes, "2018-12-01")
    print(f"[ic] 有标签股票日 {sum(len(m) for m in fwd.values())}", flush=True)
    kls = F.load_klines(codes)

    acc = {}          # state -> [(date, ic)]
    ls_acc = {}       # state -> [[n_top, s_top, n_bot, s_bot]]
    board_acc = {}    # state -> [[n_trig, s_trig_ret, n_base, s_base_ret]] board触发相对当日全体
    freq = {}         # state -> [score_pass_days, score_cands, board_days, board_trigs]
    thr_score = 25    # 与回测 score 配方一致
    bd_lo, bd_hi, bd_thr = C.BOARD_MIN_PCT, C.BOARD_MAX_PCT, C.BOARD_SCORE_THRESHOLD

    Ps = {}
    idx_in_window = 0
    t0 = time.time()
    for code, kl in kls.items():
        n = len(kl)
        if n >= F.MIN_BARS + F.FWD_N:
            Ps[code] = (F.precompute(kl), kl)
    print(f"[ic] precompute {len(Ps)} 股（{time.time()-t0:.0f}s）", flush=True)

    # day -> [(code, i)]
    byday = {}
    for code, (P, kl) in Ps.items():
        for i in range(F.MIN_BARS - 1, len(kl) - F.FWD_N):
            d = kl[i]["date"]
            fm = fwd.get(d)
            if fm and code in fm:
                byday.setdefault(d, []).append((code, i))

    print(f"[ic] 日循环 {len(byday)} 天...", flush=True)
    day_list = sorted(byday)
    board_calls = 0
    for di_, d in enumerate(day_list):
        st = reg.get(d)
        if st is None:
            continue
        items = []
        board_cand = []
        all_item_cnt = len(byday[d])
        base_ls = []
        for code, i in byday[d]:
            P, kl = Ps[code]
            vals = F.component_values(P, i)
            sv = sum(vals.values())
            fm = fwd[d]
            items.append((code, sv, fm[code]))
            base_ls.append(fm[code])
            bar = kl[i]
            prev = kl[i - 1]["close"]
            pct = (bar["close"] - prev) / prev * 100 if prev else 0.0
            if bd_lo <= pct < bd_hi:
                board_cand.append((code, i, fm[code], bar, pct))
        # score：日频 Spearman IC（对排名，抗离群）+ 按代理分五分位的真实 fwd5 多空
        sp_pairs = [(x[1], ranks[d][x[0]]) for x in items
                    if x[0] in ranks[d]]
        ic_v = spearman(sp_pairs)
        if ic_v is not None:
            acc.setdefault(st, []).append((d, ic_v))
        fr = freq.setdefault(st, [0, 0, 0, 0])
        npass = sum(1 for x in items if x[1] >= thr_score)
        fr[0] += 1; fr[1] += npass
        # 五分位多空：按代理分排序，累计真实收益；|fwd5|>35% 剔除（停复牌接缝离群防护）
        scored = [(x[1], x[2]) for x in items if abs(x[2]) <= 0.35]
        m_ = len(scored)
        if m_ >= 30:
            k5 = max(m_ // 5, 1)
            arrl = sorted(scored, key=lambda t: t[0])
            lb = ls_acc.setdefault(st, [0, 0.0, 0, 0.0])
            lb[0] += k5;                lb[1] += sum(r for _, r in arrl[-k5:])
            lb[2] += k5;                lb[3] += sum(r for _, r in arrl[:k5])
        # board 触发前瞻
        if board_cand:
            tb = board_acc.setdefault(st, [0, 0.0, 0, 0.0])
            bpass = 0
            for code, i, ret, bar, pct in board_cand:
                P, kl = Ps[code]
                lo = max(0, i - 119)
                sub = kl[lo:i + 1]
                q = {"pct_chg": pct, "price": bar["close"], "high": bar["high"],
                     "low": bar["low"], "volume": bar["volume"], "turnover": 0.0}
                try:
                    bs, _sig = sc.score_board(sub, q, hour=None)
                except Exception:
                    continue
                board_calls += 1
                tb[0] += 1; tb[1] += ret
                bm = statistics.fmean(base_ls) if base_ls else 0.0
                tb[2] += 1; tb[3] += bm   # 当日全体均值作对照
                if bs >= bd_thr:
                    bpass += 1
            fr[2] += 1; fr[3] += bpass
        if di_ % 300 == 0:
            print(f"  [ic] {di_}/{len(day_list)} board_calls={board_calls}", flush=True)

    def pack_state(state):
        ics = acc.get(state, [])
        mon = {}
        for d, ic in ics:
            mon.setdefault(d[:7], []).append(ic)
        mic = [statistics.fmean(v) for v in mon.values()]
        lb = ls_acc.get(state, [0, 0.0, 0, 0.0])
        tb = board_acc.get(state, [0, 0.0, 0, 0.0])
        top = lb[1] / lb[0] if lb[0] else None
        bot = lb[3] / lb[2] if lb[2] else None
        return {
            "days_with_ic": len(ics),
            "months": len(mic),
            "monthly_ic_mean": round(statistics.fmean(mic), 4) if mic else None,
            "monthly_ic_std": round(statistics.stdev(mic), 4) if len(mic) > 1 else None,
            "icir": round(statistics.fmean(mic) / statistics.stdev(mic), 3)
                    if len(mic) > 1 and statistics.stdev(mic) > 0 else None,
            "ls_top_q_mean_bp": round(top * 10000, 1) if top is not None else None,
            "ls_bottom_q_mean_bp": round(bot * 10000, 1) if bot is not None else None,
            "ls_spread_bp": round((top - bot) * 10000, 1) if top is not None and bot is not None else None,
            "board_trigger_n": tb[0],
            "board_trigger_fwd5_bp": round(tb[1] / tb[0] * 10000, 1) if tb[0] else None,
            "board_base_fwd5_bp": round(tb[3] / tb[2] * 10000, 1) if tb[2] else None,
            "board_excess_bp": round((tb[1] / tb[0] - tb[3] / tb[2]) * 10000, 1) if tb[0] and tb[2] else None,
            "trade_freq": {
                "score_days": freq.get(state, [0]*4)[0],
                "score_cands_total": freq.get(state, [0]*4)[1],
                "score_cands_per_day": round(freq.get(state, [0]*4)[1] / max(1, freq.get(state, [0]*4)[0]), 2),
                "board_days": freq.get(state, [0]*4)[2],
                "board_pass_per_day": round(freq.get(state, [0]*4)[3] / max(1, freq.get(state, [0]*4)[2]), 2),
            },
        }
    return {st: pack_state(st) for st in ["BULL", "CHOP", "BEAR"]}, board_calls


# ---------------- ③ 门控回测 ----------------
def make_gated_cls(allowed):
    class GatedBT(eng.Backtest):
        def _signals_on(self, date):
            st = self._regimes.get(date)
            if st is not None and st not in allowed:
                return []
            return super()._signals_on(date)
    return GatedBT


def run_gates(reg):
    PARTS.mkdir(parents=True, exist_ok=True)
    variants = {"V1_no_bear": {"BULL", "CHOP"}, "V2_bull_only": {"BULL"}}
    out_rows = []
    orig_db = C.DB_FILE
    C.DB_FILE = SNAP
    for var, allowed in variants.items():
        for strategy in ["board", "score"]:
            for wi in range(4):
                pf = PARTS / f"{var}_{strategy}_{wi}.json"
                if pf.exists():
                    cur = json.loads(pf.read_text(encoding="utf-8"))
                else:
                    t0 = time.time()
                    pool = json.loads((BASE / "data" / "bt_pool.json").read_text(encoding="utf-8"))
                    codes = pool["codes"][:500]
                    names = {c: c for c in codes}
                    params = {"buy_threshold": 25 if strategy == "score" else 40,
                              "max_positions": 3 if strategy == "score" else 2,
                              "position_pct": 0.30 if strategy == "score" else 0.25,
                              "slippage": 0.001}
                    cls = make_gated_cls(allowed)
                    bt = cls(codes, names, WINDOWS[wi][0], WINDOWS[wi][1],
                             100000.0, strategy, params)
                    bt._regimes = reg
                    orig_q = eng.df.fetch_quotes
                    eng.df.fetch_quotes = lambda cs: {}
                    try:
                        r = bt.run()
                    finally:
                        eng.df.fetch_quotes = orig_q
                    cur = {"total_return": r.get("total_return"),
                           "max_drawdown": r.get("max_drawdown"),
                           "sharpe": r.get("sharpe"),
                           "trade_count": r.get("trade_count"),
                           "buy_count": r.get("buy_count")}
                    pf.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                                  encoding="utf-8", newline="\n")
                    print(f"[gate] {var}/{strategy}/{TAGS[wi]} ret={cur['total_return']:+.4f} "
                          f"trades={cur['trade_count']} ({time.time()-t0:.0f}s)", flush=True)
                base_p = BASELINE_PARTS / f"{strategy}_{wi}.json"
                base = json.loads(base_p.read_text(encoding="utf-8")) if base_p.exists() else {}
                d_pp = round((cur["total_return"] - (base.get("total_return") or 0)) * 100, 2)
                trades_cut = round(1 - cur["trade_count"] / max(1, base.get("trade_count") or 1), 3)
                out_rows.append({"variant": var, "strategy": strategy, "widx": wi,
                                 "window_tag": TAGS[wi], "allowed": sorted(allowed),
                                 "gated": cur, "ungated_baseline": base,
                                 "delta_return_pp": d_pp, "trade_reduction": trades_cut})
    C.DB_FILE = orig_db
    return out_rows


def main():
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "phase": "T4-regime-gating",
               "snapshot": SNAP,
               "universe": "bt_pool top500 ∩ 非 DATA_EXCLUDE_CODES（等权线同口径）"}
    reg, meta, shares, tot = equal_weight_line()
    payload["label_rule_preregistered"] = {
        "bear": "r20<=-0.05 或 (r20<=-0.02 且 sigma20>=0.022)",
        "bull": "r20>=+0.04 且 sigma20<=0.028",
        "else": "CHOP", "note": "常数 a priori 固定；量能比 q=V5/V60 记录不参与判态",
    }
    payload["state_shares"] = {k: round(v / tot, 4) for k, v in shares.items()}
    yearly = {}
    for d, st in sorted(reg.items()):
        yearly.setdefault(d[:4], {"BULL": 0, "CHOP": 0, "BEAR": 0})[st] += 1
    payload["state_shares_by_year"] = yearly
    payload["regime_sample"] = {d: meta[d] for d in list(sorted(meta))[-5:]}

    ic, board_calls = state_ic(reg)
    payload["state_ic"] = ic
    payload["ic_note"] = ("score 代理 = score_stock 18 个K线成分带符号权重和（P28A 管线复用）；"
                          "board = 触发带样本 fwd5 相对当日全体的超额")
    print("\n=== 分状态结果 ===")
    for st, v in ic.items():
        print(f"{st}: 月IC={v['monthly_ic_mean']} ICIR={v['icir']} "
              f"LS={v['ls_spread_bp']}bp board_excess={v['board_excess_bp']}bp "
              f"cands/day={v['trade_freq']['score_cands_per_day']}")

    rows = run_gates(reg)
    payload["gated_backtests"] = rows
    # 判据汇总
    verdict = {}
    for var in ["V1_no_bear", "V2_bull_only"]:
        for strategy in ["board", "score"]:
            ds = [r["delta_return_pp"] for r in rows
                  if r["variant"] == var and r["strategy"] == strategy]
            if not ds:
                continue
            g2_ok = (sum(1 for x in ds if x >= -0.5) >= 3 and statistics.fmean(ds) > 0.5)
            tr = [r["trade_reduction"] for r in rows
                  if r["variant"] == var and r["strategy"] == strategy]
            verdict[f"{var}/{strategy}"] = {
                "delta_pp_per_window": ds, "mean_delta_pp": round(statistics.fmean(ds), 2),
                "g2_pass": bool(g2_ok),
                "avg_trade_reduction": round(statistics.fmean(tr), 3) if tr else None,
            }
    payload["gating_verdicts"] = verdict
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8", newline="\n")
    print("\nwritten", OUT_JSON)
    for k, v in verdict.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
