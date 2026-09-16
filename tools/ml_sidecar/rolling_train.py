# -*- coding: utf-8 -*-
"""ML 滚动训练 v3（严格 PIT）：rank 标签 + 每 20 交易日再训练 + 预测落 ml_pred 表
v3: 并行构建全量面板(特征+rank), 消除重复特征计算; 滚动训练点只做 date 切片
- 标签: 未来5日收益 → 当日横截面 rank 归一化 [0,1]（组内排除 NaN）
- 滚动: 交易日(取自 kline 实际日期)每 step 日一个训练点 T;
  只用 date<=T 样本训练 LGBM; 预测 (T, T+step] 每日全市场横截面分
- 落库: market.db ml_pred(code,date,score) PK(code,date), INSERT OR REPLACE, 分批短事务
- 断点续跑: data/ml_rolling_progress.json 记录已完成训练点索引
用法(ml_sidecar venv):
  .venv\Scripts\python.exe rolling_train.py [--start 2019-01-01] [--codes 2000] [--step 20]
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
PROGRESS = os.path.join(BASE, "data", "ml_rolling_progress.json")
MIN_BARS = 150
MIN_TRAIN_DAYS = 150


def load_universe(codes_limit=2000):
    """股票池（排除 DATA_EXCLUDE_CODES，按最近成交额排序取前 N = 流动性优先）"""
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
        # 兜底：当日无成交额的票（停牌）用全样本 X >= MIN_BARS 补
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
    """单股特征+fwd5 标签（并行 worker；rank 在 concat 后按日截面统一计算）"""
    conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % DB.replace("\\", "/"), uri=True)
    try:
        try:
            rows = conn.execute(
                "SELECT date,open,high,low,close,volume,amount FROM kline "
                "WHERE code=? AND period='day' AND date>=? ORDER BY date",
                (code, "2018-01-01")).fetchall()
        except Exception:
            return None
        if len(rows) < MIN_BARS:
            return None
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                         "close", "volume", "amount"])
        df = df.dropna(subset=["close"])
        feats, label = alpha158.build_features(df)
        lab = pd.DataFrame({"date": df["date"].values, "fwd5": label.values})
        out = pd.DataFrame({"date": df["date"].values, "code": code})
        out = pd.concat([out, feats.reset_index(drop=True),
                         lab["fwd5"].reset_index(drop=True).rename("fwd5")], axis=1)
        out = out.dropna(subset=["fwd5"])
        return out if len(out) > 0 else None
    except Exception:
        return None
    finally:
        conn.close()


def build_all_panel(codes, workers=8):
    """并行构建全量面板 + 统一计算当日横截面 rank 标签（当日 >=15 票有效）"""
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
    # ★ 当日横截面 rank（跨票；当日有效票 >=15 才计，否则 NaN 剔除）
    cnt = panel.groupby("date")["fwd5"].transform("count")
    rank = panel.groupby("date")["fwd5"].rank(pct=True)
    panel["label"] = np.where(cnt >= 15, rank, np.nan)
    panel = panel.dropna(subset=["label"])
    panel = panel.drop(columns=["fwd5"])
    return panel


def trade_dates(panel):
    all_d = sorted(panel["date"].unique())
    return [d for d in all_d if d >= "2019-01-01"]


def train_and_predict(fit_df, pred_df, feat_cols):
    """fit_df(≤T): 训练; pred_df((T,T+step]): 预测横截面分。
    返回 {code: {date: score}}"""
    if fit_df.empty or len(fit_df) < 5000 or fit_df["date"].nunique() < MIN_TRAIN_DAYS:
        return None
    X = fit_df[feat_cols].astype(float)
    y = fit_df["label"]
    # ★ 防御: y 标准差过小（rank 失真）→ skip 该点
    if y.std() < 0.05:
        return None
    m = lgb.LGBMRegressor(
        n_estimators=200, learning_rate=0.05, num_leaves=31,
        min_child_samples=100, subsample=0.9, colsample_bytree=0.8,
        reg_alpha=0.1, reg_lambda=1.0, random_state=42, verbosity=-1)
    m.fit(X, y)
    if pred_df.empty:
        return {}
    Xp = pred_df[feat_cols].astype(float).fillna(0.0)
    pred = m.predict(Xp)
    out = {}
    for i in range(len(pred_df)):
        r = pred_df.iloc[i]
        out.setdefault(r["date"], {})[r["code"]] = round(float(pred[i]), 6)
    return out


def write_ml_pred(rows):
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS ml_pred(
            code TEXT, date TEXT, score REAL,
            PRIMARY KEY(code, date))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mlpred_d ON ml_pred(date)")
        batch = []
        for row in rows:
            batch.append(row)
            if len(batch) >= 5000:
                conn.executemany("INSERT OR REPLACE INTO ml_pred(code,date,score) VALUES(?,?,?)", batch)
                conn.commit()
                batch = []
        if batch:
            conn.executemany("INSERT OR REPLACE INTO ml_pred(code,date,score) VALUES(?,?,?)", batch)
            conn.commit()
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--codes", type=int, default=2000)
    ap.add_argument("--step", type=int, default=20)
    args = ap.parse_args()
    t0 = time.time()

    codes = load_universe(args.codes)
    print("股票池: %d 只" % len(codes), flush=True)
    if not codes:
        print("无股票池"); return
    # 全量面板一次性构建
    print("构建全量面板(特征+rank标签)...", flush=True)
    panel = build_all_panel(codes)
    if panel.empty:
        print("面板为空"); return
    print("面板: %d 行 x %d 列 (%.0fs)" % (len(panel), len(panel.columns), time.time() - t0), flush=True)
    feat_cols = [c for c in panel.columns if c not in ("code", "date", "label")]
    print("特征列: %d" % len(feat_cols), flush=True)

    dates = trade_dates(panel)
    print("交易日: %d (%s ~ %s)" % (len(dates), dates[0], dates[-1]), flush=True)
    train_points = [dates[i] for i in range(0, len(dates), args.step) if dates[i] >= args.start]
    print("训练点: %d 个（每 %d 日）" % (len(train_points), args.step), flush=True)

    done = {}
    if os.path.exists(PROGRESS):
        try:
            with open(PROGRESS, "r", encoding="utf-8") as f:
                done = json.load(f)
        except Exception:
            done = {}
    total_pts = len(train_points)
    for pi, T in enumerate(train_points):
        if str(pi) in done:
            continue
        pred_end = train_points[pi + 1] if pi + 1 < total_pts else dates[-1]
        fit_df = panel[panel["date"] <= T]
        pred_df = panel[(panel["date"] > T) & (panel["date"] <= pred_end)]
        out = train_and_predict(fit_df, pred_df, feat_cols)
        if out is None:
            done[str(pi)] = "skip-early"
        elif out:
            rows = []
            for d, m in out.items():
                for c, s in m.items():
                    rows.append((c, d, s))
            write_ml_pred(rows)
            done[str(pi)] = "ok:%d" % len(rows)
            print("  T[%d]=%s 预测 %d 日 %d 行 (%.0fs)" % (
                pi, T, len(out), len(rows), time.time() - t0), flush=True)
        else:
            done[str(pi)] = "no-pred"
        with open(PROGRESS, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False)
    print("完成, 总耗时 %.0fs" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    main()