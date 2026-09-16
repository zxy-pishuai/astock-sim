# -*- coding: utf-8 -*-
"""★ Phase75：ml_pred 短标签方案验证（对照实验，不改主流程）

四种标签（同池同特征 alpha158，同参 LightGBM；★B1 修订版）：
  a) 现行基准 label_5d  : 未来5日收益 → 当日横截面 rank（rolling_train 同款，
                          面板天然截尾最后 5 个交易日——P55 发现的滞后根源）
  b) 短标签   label_o2c : 次日开盘买→次日收盘卖（T+1 可实现口径）
  c) 短标签变体 label_o2o2: 次日开盘买→T+2 开盘卖
  d) ★B1 目标  label_c2o : 次日收盘买→T+2 开盘卖（隔夜持有；信号 T 收盘后
                          可执行、满足 T+1 规则——滞后 5 日→1 日对照的核心臂）

滚动协议：交易日每 20 日一个训练点，取**最后 3 个训练点**（任务书指定），
fit=≤T，pred=(T, T+step]。样本外评估（逐预测日）：
  - IC：spearman(模型分, 该标签自身实现收益)（各评各的持有期）
  - top-decile：模型分前 10% 的**可实现收益 o2c** 均值（可执行口径统一比较）

判定（事先写死）：mean(IC_o2c) ≥ 0.6 × |mean(IC_5d)| 且短标签 top-decile o2c
方向与长标签一致（同号且 >0）→ 方案可行；否则不可行。

用法（ml_sidecar venv，工具目录下）:
  .venv\\Scripts\\python.exe rollout_short_label.py [--codes 2000] [--step 20] [--points 3]
输出: data/ml_short_label_results.json + 终端判定表
红线: 不写 ml_pred 表；不补"到今天"的 5 日标签（前视，禁止）。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

import lightgbm as lgb
import numpy as np
import pandas as pd

import alpha158

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(BASE, "data", "market.db")
OUT_JSON = os.path.join(BASE, "data", "ml_short_label_results.json")
MIN_BARS = 150
LABELS = ["label_5d", "label_o2c", "label_o2o2", "label_c2o"]
# 标签 → 原始收益列映射（原始列 concat 后按日截面统一算 rank）
RAW_OF = {"label_5d": "fwd5", "label_o2c": "o2c",
          "label_o2o2": "o2o2", "label_c2o": "c2o"}


def load_universe(codes_limit=2000):
    """与 rolling_train.load_universe 同口径（排除例外票，流动性优先）"""
    try:
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        from app import config as C
        _ex = C.DATA_EXCLUDE_CODES or []
    except Exception:
        _ex = []
    _ex_sql = ",".join("'%s'" % c for c in _ex) if _ex else "''"
    conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % DB.replace("\\", "/"), uri=True)
    try:
        maxd = conn.execute("SELECT MAX(date) FROM kline WHERE period='day'").fetchone()[0]
        codes = [r[0] for r in conn.execute(
            "SELECT code FROM kline WHERE period='day' AND code NOT IN (%s) "
            "AND date=? AND amount>0 GROUP BY code ORDER BY MAX(amount) DESC LIMIT ?"
            % (_ex_sql), (maxd, codes_limit))]
        if len(codes) < codes_limit:
            have = set(codes)
            more = [r[0] for r in conn.execute(
                "SELECT code FROM kline WHERE period='day' AND code NOT IN (%s) "
                "GROUP BY code HAVING COUNT(*) >= ? ORDER BY code LIMIT ?"
                % (_ex_sql), (MIN_BARS, codes_limit - len(codes)))]
            codes += [c for c in more if c not in have]
    finally:
        conn.close()
    return codes


def _build_one(code):
    """单股特征 + 三种标签原始值（rank 在 concat 后按日截面统一计算）"""
    conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % DB.replace("\\", "/"), uri=True)
    try:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' AND date>=? ORDER BY date",
            (code, "2018-01-01")).fetchall()
        if len(rows) < MIN_BARS:
            return None
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                         "close", "volume", "amount"])
        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        feats, _ = alpha158.build_features(df)
        out = pd.DataFrame({"date": df["date"].values, "code": code})
        out = pd.concat([out, feats.reset_index(drop=True)], axis=1)
        o, c = df["open"].astype(float), df["close"].astype(float)
        out["fwd5"] = c.shift(-5) / c - 1.0                    # 现行：未来5日收益
        out["o2c"] = c.shift(-1) / o.shift(-1) - 1.0           # 短标签：T+1 开→收
        out["o2o2"] = o.shift(-2) / o.shift(-1) - 1.0          # 变体：T+1 开→T+2 开
        out["c2o"] = o.shift(-2) / c.shift(-1) - 1.0           # ★B1 隔夜：T+1 收→T+2 开
        return out if len(out) else None
    except Exception:
        return None
    finally:
        conn.close()


def build_panel(codes, workers=8):
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, as_completed
    frames = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_build_one, c): c for c in codes}
        for f in as_completed(futs):
            r = f.result()
            if r is not None and len(r):
                frames.append(r)
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    # 各标签独立做当日横截面 rank（当日有效票 >=15 才计，与主流程同规则）
    for lab in LABELS:
        raw = RAW_OF[lab]
        cnt = panel.groupby("date")[raw].transform("count")
        rk = panel.groupby("date")[raw].rank(pct=True)
        panel[lab] = np.where(cnt >= 15, rk, np.nan)
    return panel


def _spearman(a, b):
    if len(a) < 3:
        return np.nan
    ra = pd.Series(a).rank(pct=True)
    rb = pd.Series(b).rank(pct=True)
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def train_variant(panel, feat_cols, label, T, pred_end):
    """训练某标签变体并预测 (T, pred_end]。返回 {date: {code: score}}"""
    fit = panel[(panel["date"] <= T) & panel[label].notna()]
    if len(fit) < 5000 or fit["date"].nunique() < 150 or fit[label].std() < 0.05:
        return None
    m = lgb.LGBMRegressor(
        n_estimators=200, learning_rate=0.05, num_leaves=31,
        min_child_samples=100, subsample=0.9, colsample_bytree=0.8,
        reg_alpha=0.1, reg_lambda=1.0, random_state=42, verbosity=-1)
    m.fit(fit[feat_cols].astype(float), fit[label])
    pred_df = panel[(panel["date"] > T) & (panel["date"] <= pred_end)]
    if pred_df.empty:
        return {}
    pred = m.predict(pred_df[feat_cols].astype(float).fillna(0.0))
    out = {}
    for i in range(len(pred_df)):
        r = pred_df.iloc[i]
        out.setdefault(r["date"], {})[r["code"]] = float(pred[i])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=2000)
    ap.add_argument("--step", type=int, default=20)
    ap.add_argument("--points", type=int, default=3)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    t0 = time.time()

    codes = load_universe(a.codes)
    print("股票池: %d 只" % len(codes), flush=True)
    print("构建面板（特征 + 三标签）...", flush=True)
    panel = build_panel(codes, a.workers)
    if panel.empty:
        print("面板为空"); return
    feat_cols = [c for c in panel.columns
                 if c not in ("code", "date") + tuple(LABELS)
                 + ("fwd5", "o2c", "o2o2", "c2o")]
    print("面板 %d 行 x %d 列（特征 %d）(%.0fs)" % (
        len(panel), len(panel.columns), len(feat_cols), time.time() - t0), flush=True)

    dates = sorted(panel["date"].unique())
    dates = [d for d in dates if d >= "2019-01-01"]
    pts_all = [dates[i] for i in range(0, len(dates), a.step)]
    pts = pts_all[-a.points:]   # 最后 3 个训练点（最新数据）
    print("训练点（最后 %d 个）: %s" % (len(pts), pts), flush=True)

    # 逐训练点：三变体训练 → 预测日评估
    daily = []   # {date, variant, ic_own, ic_vs_o2c, topdec_o2c, n}
    for pi, T in enumerate(pts):
        pred_end = pts_all[pts_all.index(T) + 1] if pts_all.index(T) + 1 < len(pts_all) else dates[-1]
        pred_df = panel[(panel["date"] > T) & (panel["date"] <= pred_end)]
        for lab in LABELS:
            scores = train_variant(panel, feat_cols, lab, T, pred_end)
            if not scores:
                print("  T=%s %s: 训练跳过" % (T, lab), flush=True)
                continue
            for d, sc in scores.items():
                sub = pred_df[pred_df["date"] == d].dropna(
                    subset=["o2c"]).copy()
                if len(sub) < 15:
                    continue
                sub["score"] = sub["code"].map(sc)
                sub = sub.dropna(subset=["score"])
                if len(sub) < 15:
                    continue
                own_col = RAW_OF[lab]
                ic_own = _spearman(sub["score"], sub[own_col])
                ic_x = _spearman(sub["score"], sub["o2c"])
                k = max(1, int(len(sub) * 0.10))
                top = sub.nlargest(k, "score")
                tv = float(top[own_col].mean())
                daily.append({"train_T": T, "date": d, "variant": lab,
                              "ic_own": ic_own, "ic_vs_o2c": ic_x,
                              "topdec_o2c": float(top["o2c"].mean()),
                              "topdec_own": (round(tv, 6) if tv == tv else None),
                              "n": len(sub)})
        print("  训练点 %s 完成 (%.0fs)" % (T, time.time() - t0), flush=True)

    # 聚合
    agg = {}
    for lab in LABELS:
        rows = [x for x in daily if x["variant"] == lab]
        if not rows:
            agg[lab] = {"days": 0}
            continue
        ic_own = [x["ic_own"] for x in rows if x["ic_own"] == x["ic_own"]]
        ic_x = [x["ic_vs_o2c"] for x in rows if x["ic_vs_o2c"] == x["ic_vs_o2c"]]
        td = [x["topdec_o2c"] for x in rows]
        tdo = [x["topdec_own"] for x in rows
               if x.get("topdec_own") is not None and x["topdec_own"] == x["topdec_own"]]
        agg[lab] = {
            "pred_days": len(rows),
            "mean_ic_own": round(sum(ic_own) / len(ic_own), 5) if ic_own else None,
            "mean_ic_vs_o2c": round(sum(ic_x) / len(ic_x), 5) if ic_x else None,
            "mean_topdec_o2c": round(sum(td) / len(td), 5),
            "mean_topdec_own": round(sum(tdo) / len(tdo), 5) if tdo else None,
            "topdec_positive_ratio": round(sum(1 for x in td if x > 0) / len(td), 4),
        }

    # 事先写死的判定（门槛不变；★B1 决策目标 = label_c2o「次日收盘→次次日开盘」隔夜口径）
    ic5 = (agg.get("label_5d") or {}).get("mean_ic_own") or 0.0
    td_long = (agg.get("label_5d") or {}).get("mean_topdec_o2c") or 0.0
    by_var = {}
    for lab in LABELS:
        if lab == "label_5d":
            continue
        v = agg.get(lab) or {}
        ics = v.get("mean_ic_own") or 0.0
        tds = v.get("mean_topdec_o2c") or 0.0
        conds = {"ic_ge_60pct": bool(abs(ics) >= 0.6 * abs(ic5)),
                 "topdec_o2c_positive": bool(tds > 0),
                 "direction_match_baseline": bool((td_long > 0) == (tds > 0))}
        ok = all(conds.values())
        by_var[lab] = {"ratio_pct": round(abs(ics) / abs(ic5) * 100, 1) if ic5 else None,
                       "feasible": ok,
                       "failed": [k for k, okk in conds.items() if not okk]}
    feasible = by_var["label_c2o"]["feasible"]
    if feasible:
        verdict = ("方案可行：B1 目标 label_c2o（次日收→次次日开）IC 保留长标签 %.0f%%，"
                   "top-decile 方向一致" % by_var["label_c2o"]["ratio_pct"])
    else:
        verdict = ("方案不可行（预写规则字面判定）：B1 目标 label_c2o 未达标项=%s；"
                   "口径错位与自持有期重估结论见 docs/reports/ml_short_label.md"
                   % ",".join(by_var["label_c2o"]["failed"]))

    doc = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "75-B1",
        "protocol": "滚动 step=%d，最后 %d 个训练点；IC=spearman(模型分,自身标签实现收益)；"
                    "top-decile 双口径=前10%%可实现 o2c 均值（统一尺）/ 各自持有期均值；"
                    "滞后5日 vs 1日(可执行)对比；B1 决策目标=label_c2o" % (a.step, a.points),
        "verdict_rule": "各短标签: |mean(IC_self)| >= 0.6*|mean(IC_5d)| 且 "
                        "top-decile(o2c) 方向与长标签一致且>0",
        "aggregate": agg,
        "verdict_by_variant": by_var,
        "verdict": verdict,
        "feasible": feasible,
        "daily": daily,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("\n===== 判定表 =====")
    for lab in LABELS:
        v = agg.get(lab, {})
        print("%-10s 预测日=%-4s meanIC=%s top(o2c)=%s top(own)=%s 正向占比=%s" % (
            lab, v.get("pred_days"), v.get("mean_ic_own"),
            v.get("mean_topdec_o2c"), v.get("mean_topdec_own"),
            v.get("topdec_positive_ratio")))
    print("B1 各短标签判定:", json.dumps(by_var, ensure_ascii=False))
    print("判定:", verdict)
    print("已写出 %s（%.0fs）" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
