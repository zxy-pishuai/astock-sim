# -*- coding: utf-8 -*-
"""★ Phase41: score_board 成分归因 —— 复用 Phase30 方法，度量打板评分每个成分。

方法：
  1) 独立进程跑 board 基线四窗口（配方：bt_pool top500、fetch_quotes 置空、board 40/2/0.25/
     slippage 0.001、★BOARD_MOMENTUM_MIN 运行时冻结 7.0 —— Phase33 已把默认改 8.0，
     为对照 Phase21/27 发布基线必须冻结）。
  2) 引擎买入 reason 只记前 3 个信号 → 本工具对每笔买入按引擎同款输入重算
     sc.score_board(hist, q, hour=None) 取【全部】成分信号（重算分值与 reason 内
     记录的评分数一致作为正确性校验）。
  3) FIFO 配对买卖成回合（复用 Phase30 口径），每个成分都记账。
  4) seal_quality_bonus 切片：引擎调用时不传 code → 回测路径恒不生效，如实呈现。

判定清单口径（事先写死）：
  建议保留：n≥80 且 胜率≥52% 且 总盈亏>0 且 有数窗口方向一致≥3/4
  建议降权：n≥80 且 胜率<50% 且 总盈亏<0
  建议审查：其余（含"减分成分却伴随正贡献"的反向指标）
"""
import sys
import os
import json
import re
import time
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C          # noqa: E402  (只读)
from app import engine as eng        # noqa: E402  (只读)
from app import scoring as sc        # noqa: E402  (只读)

OUT_JSON = os.path.join(C.DATA_DIR, "attribution", "board_components.json")
WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-18"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "牛市"]
BOARD_PARAMS = {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25,
                "slippage": 0.001}
LEGACY = [(-0.1177, 628), (-0.0665, 747), (-0.0166, 616), (0.2730, 448)]
# seal_quality_bonus 的信号名（app/scoring.py seal_quality_bonus）
SEALQ_NAMES = {"首封今日早", "尾盘板", "封单资金比强", "封单资金比中", "零炸板", "多次炸板"}
# 推荐阈值（事先写死）
R_KEEP_N, R_KEEP_WIN = 80, 0.52
R_DROP_N, R_DROP_WIN = 80, 0.50


def parse_component(tok):
    """"巨量封板(+20,量比2.3)" / "⚠封板分歧加大(-8)" → (名称, 分值)；不匹配返回 None"""
    t = tok.strip()
    if t.startswith("⚠"):
        t = t[1:]
    m = re.match(r"^(.+?)\(([+-]?\d+)(?:[,，][^)]*)?\)$", t)
    if not m:
        return None
    try:
        return m.group(1).strip(), int(m.group(2))
    except ValueError:
        return None


def build_rounds(trades):
    """FIFO 配对买卖 → 回合列表（复用 Phase30 口径）。"""
    from collections import defaultdict
    lots = defaultdict(list)
    rounds = []
    for t in sorted(trades, key=lambda x: (x.get("didx") if x.get("didx") is not None else 0,
                                           0 if x["side"] == "buy" else 1)):
        code = t["code"]
        if t["side"] == "buy":
            lots[code].append({
                "code": code, "entry_date": t["date"], "entry_didx": t.get("didx"),
                "qty_left": t["qty"], "amount": t["amount"],
                "widx": t.get("widx"), "realized": 0.0,
                "exit_date": None, "exit_didx": None,
            })
            continue
        qty_left = t["qty"]
        sell_qty = max(t["qty"], 1)
        pnl = t.get("pnl") or 0.0
        while qty_left > 0 and lots[code]:
            lot = lots[code][0]
            q = min(qty_left, lot["qty_left"])
            lot["realized"] += pnl * (q / float(sell_qty))
            lot["exit_date"] = t["date"]
            lot["exit_didx"] = t.get("didx")
            lot["qty_left"] -= q
            qty_left -= q
            if lot["qty_left"] <= 0:
                lots[code].pop(0)
                hold = None
                if lot["entry_didx"] is not None and lot["exit_didx"] is not None:
                    hold = lot["exit_didx"] - lot["entry_didx"]
                rounds.append({
                    "code": lot["code"], "widx": lot["widx"],
                    "entry_date": lot["entry_date"], "exit_date": lot["exit_date"],
                    "hold_days": hold, "amount": lot["amount"],
                    "pnl": round(lot["realized"], 2),
                    "ret": round(lot["realized"] / lot["amount"], 6) if lot["amount"] else None,
                })
    return rounds


def agg(items):
    n = len(items)
    if not n:
        return {"count": 0}
    pnls = [x["pnl"] for x in items]
    wins = sum(1 for p in pnls if p > 0)
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p <= 0)
    rets = [x["ret"] for x in items if x.get("ret") is not None]
    return {
        "count": n,
        "win_rate": round(wins / n, 4),
        "avg_ret": round(sum(rets) / len(rets), 6) if rets else None,
        "total_pnl": round(sum(pnls), 2),
        "profit_factor": round(gw / gl, 3) if gl > 0 else (99.0 if gw > 0 else None),
    }


def recommend(name, st, window_signs):
    neg = name in ("振幅过大", "尾盘慎入", "封板分歧加大", "多次炸板")
    consistent = sum(1 for s in window_signs if s != 0) >= 3 and \
        len({s for s in window_signs if s != 0}) <= 1
    if st["count"] >= R_KEEP_N and st["win_rate"] >= R_KEEP_WIN \
            and st["total_pnl"] > 0 and consistent:
        return "建议保留"
    if st["count"] >= R_DROP_N and st["win_rate"] < R_DROP_WIN and st["total_pnl"] < 0:
        return "建议降权"
    if neg and st["count"] >= 30 and st["total_pnl"] > 0:
        return "建议审查(减分成分为正贡献，疑似反向指标)"
    return "建议审查(样本不足或方向不稳)"


def main():
    t0 = time.time()
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        codes_all = json.load(f)["codes"][:500]
    ex = set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])
    codes = [c for c in codes_all]
    names = {c: c for c in codes}

    orig_quotes = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    orig_mm = getattr(C, "BOARD_MOMENTUM_MIN", None)
    C.BOARD_MOMENTUM_MIN = 7.0      # ★ 冻结到 Phase21/27 口径（Phase33 已改默认 8.0）
    all_trades = []
    runs = []
    try:
        for wi, (w0, w1) in enumerate(WINDOWS):
            bt = eng.Backtest(codes, names, w0, w1, 100000.0, "board",
                              dict(BOARD_PARAMS))
            r = bt.run()
            day_idx = {d: i for i, d in enumerate(bt.trading_days)}
            for t in bt.trades:
                t["didx"] = day_idx.get(t["date"])
                t["widx"] = wi
                t["strategy"] = "board"
            all_trades.extend(bt.trades)
            ok = (abs(r["total_return"] - LEGACY[wi][0]) < 1e-9
                  and r["trade_count"] == LEGACY[wi][1])
            runs.append({
                "window": WIN_NAMES[wi], "start": w0, "end": w1,
                "total_return": r.get("total_return"), "max_drawdown": r.get("max_drawdown"),
                "trade_count": r.get("trade_count"), "buy_count": r.get("buy_count"),
                "matches_legacy_baseline": ok,
            })
            print("[run] %s ret=%+.4f trades=%d 与发布基线%s" % (
                WIN_NAMES[wi], r.get("total_return") or 0, r.get("trade_count") or 0,
                "一致" if ok else "不一致(上游前复权重写，见报告)"), flush=True)
    finally:
        eng.df.fetch_quotes = orig_quotes
        C.BOARD_MOMENTUM_MIN = orig_mm   # 恢复并行任务的现值（8.0）
    hard_ok = all(r["matches_legacy_baseline"] for r in runs[:3])
    print("[gate] 窗口0-2 基线精确复现：%s（窗口3 受上游前复权重写影响，差异入档）"
          % ("✅" if hard_ok else "❌ 中止"), flush=True)
    if not hard_ok:
        return

    # ---- 重算每笔买入的全量成分信号（引擎同款输入） ----
    eng.df.fetch_quotes = lambda cs: {}
    C.BOARD_MOMENTUM_MIN = 7.0
    bt_by_wi = {}
    for wi, (w0, w1) in enumerate(WINDOWS):
        b = eng.Backtest(codes, names, w0, w1, 100000.0, "board", dict(BOARD_PARAMS))
        b._load_data()
        bt_by_wi[wi] = b
    buys = [t for t in all_trades if t["side"] == "buy"]
    mismatch_score = 0
    comp_matrix = []       # 每回合: {components:[(name,score)], ...}
    buy_meta = {}
    for t in buys:
        b = bt_by_wi[t["widx"]]
        di = b.date_index.get(t["code"])
        i = di.get(t["date"]) if di else None
        if i is None or i < 1:
            continue
        prev_date = b.klines[t["code"]][i - 1]["date"]
        hist = b._hist_klines(t["code"], prev_date)
        bar = hist[-1]
        pct = (bar["close"] - hist[-2]["close"]) / hist[-2]["close"] * 100
        q = {"pct_chg": pct, "price": bar["close"], "high": bar["high"],
             "low": bar["low"], "volume": bar["volume"], "turnover": 0.0}   # 引擎同款：无 code
        score, signals = sc.score_board(hist, q, hour=None)
        m = re.search(r"评分(\d+)分", t["reason"])
        if m and int(m.group(1)) != score:
            mismatch_score += 1
        comps = []
        for tok in signals:
            pc = parse_component(tok)
            if pc:
                comps.append(pc)
        key = (t["widx"], t["code"], t["date"])
        buy_meta[key] = {"components": comps, "score": score}
    print("[recompute] 买入 %d 笔重算完成，评分数不一致 %d 笔" % (
        len(buys), mismatch_score), flush=True)

    # ---- 回合 × 成分矩阵 ----
    rounds = build_rounds(all_trades)
    for rd in rounds:
        meta = buy_meta.get((rd["widx"], rd["code"], rd["entry_date"]))
        rd["components"] = meta["components"] if meta else []
        rd["has_sealq"] = any(nm in SEALQ_NAMES for nm, _ in rd["components"])

    comp_rows = collections.defaultdict(list)
    for rd in rounds:
        for nm, sc_ in rd["components"]:
            comp_rows[nm].append({"round": rd, "score": sc_, "widx": rd["widx"]})

    components = []
    for nm, items in sorted(comp_rows.items(), key=lambda kv: -len(kv[1])):
        st = agg([it["round"] for it in items])
        per_win = []
        signs = []
        for wi, wn in enumerate(WIN_NAMES):
            grp = [it for it in items if it["widx"] == wi]
            g = agg([it["round"] for it in grp])
            g["window"] = wn
            per_win.append(g)
            signs.append((g["avg_ret"] > 0) - (g["avg_ret"] < 0) if g["count"] else 0)
        st.update({
            "component": nm,
            "avg_score_when_present": round(sum(it["score"] for it in items) / len(items), 2),
            "windows": per_win,
            "direction_consistent": sum(1 for s in signs if s != 0) >= 3
                                    and len({s for s in signs if s != 0}) <= 1,
            "recommendation": recommend(nm, st, signs),
        })
        components.append(st)

    # ---- seal_quality 切片 ----
    with_q = [rd for rd in rounds if rd["has_sealq"]]
    without_q = [rd for rd in rounds if not rd["has_sealq"]]
    seal_slice = {
        "note": "引擎 _signals_on 的 board 分支给 score_board 的 quote 不含 code，"
                "回测路径中 seal_quality_bonus 恒不生效（limitup 涨停池亦为实时接口）。"
                "以下切片预期为空，属机制性事实而非结论。",
        "with_sealq": {"count": len(with_q), **agg(with_q)},
        "without_sealq": {"count": len(without_q), **agg(without_q)},
    }

    out = {
        "meta": {
            "pool": "bt_pool.json 前500只（engine 内滤 DATA_EXCLUDE_CODES），names=代码兜底",
            "params": BOARD_PARAMS, "capital": 100000,
            "recipe_patches": ["fetch_quotes 置空", "BOARD_MOMENTUM_MIN 冻结 7.0（对照发布基线口径）"],
            "method": "FIFO 配对回合 × 成分矩阵（每笔买入按引擎同款输入重算 score_board 全信号）；"
                      "引擎 reason 只记前3信号，故重算是完整覆盖的唯一途径",
            "judge_thresholds": {"keep": {"n": R_KEEP_N, "win_rate": R_KEEP_WIN},
                                 "drop": {"n": R_DROP_N, "win_rate": R_DROP_WIN}},
        },
        "runs": runs,
        "baseline_note": "窗口0-2 必须且已与发布基线精确一致；窗口3(牛市) 因上游 kline 于 "
                         "8/23 后整体前复权重写(91/498 票被修订) 无法逐位复现，差异如实入档",
        "recompute_score_mismatch": mismatch_score,
        "rounds_count": len(rounds),
        "components": components,
        "seal_quality_slice": seal_slice,
        "elapsed_s": round(time.time() - t0, 1),
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)

    print("\n===== 成分归因（按出现次数降序） =====")
    for c in components:
        print("%-14s n=%-5d 胜率=%.3f 平均收益=%+.4f%% 总盈亏=%+.0f 一致=%s → %s" % (
            c["component"], c["count"], c["win_rate"] or 0,
            (c["avg_ret"] or 0) * 100, c["total_pnl"],
            c["direction_consistent"], c["recommendation"]), flush=True)
    print("saved", OUT_JSON)


if __name__ == "__main__":
    main()
