# -*- coding: utf-8 -*-
"""★ 阶段4 ML 选股 sidecar —— 推理：生成 data/ml_scores.json（code→分数+日期）
主系统只读该文件（scoring.ml_score_bonus），文件超过 ML_SCORE_MAX_AGE_HOURS 自动失效降级。
用法（工具目录下）：
  .venv\\Scripts\\python.exe predict.py [--codes 2000] [--max-lag-days 5]
输出：{BASE}/data/ml_scores.json  {"date": 信号日, "generated": 时间戳, "scores": {code: score}}

★ Phase76/B2 特征补齐口径修订（此前 700 候选只出 177 分，覆盖率骤降）：
  1. 候选上限 --codes 默认 700→2000：对齐 bt_pool 规模（池 1878，700 封顶只够 37%）。
  2. 末根滞后容忍 --max-lag-days（默认 5 个交易日）：各股用**自身末根**做 as-of 打分，
     只用 <=该日数据（PIT 安全）；上调原因=池内 41% 的票末根停在 2026-08-21
     （updater 增量缺口，非真实停牌；禁碰 app/updater，此为预测端可执行口径）。
     记分文件 schema 不变；滞后分布由 tools/ml_sidecar/diagnose_coverage.py 报告。
  3. 特征补齐：a) AMTRATIO*/CORR_RA* 族 NaN→0.0（近窗零成交额的除零保护位，
     语义=无量价互动→中性；覆盖长期零成交与刚复牌票）；
     b) 其余残余 NaN 兜底 0.0（常数窗口相关系数等退化位），逐票计数入报告。
  4. 数据面守门不变：MIN_BARS=130 历史根数、DATA_EXCLUDE_CODES 例外票仍排除。
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
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
MIN_BARS = 130
FILL_ZERO_PREFIXES = ("AMTRATIO", "CORR_RA")   # ★B2 显式中性化族

# ★B2 并行收集的 worker 全局（spawn 下经 initializer 传入，子进程重建连接）
_W = {}


def _init_worker(uri, min_bars, tdates, anchor_i, max_lag, feat_cols):
    _W.update(uri=uri, min_bars=min_bars,
              tidx={d: i for i, d in enumerate(tdates)},
              anchor_i=anchor_i, max_lag=max_lag, feat_cols=feat_cols)


def _collect_one(code):
    """单股收集：全量取数→滞后守门→特征末行+补齐。返回 [code, lag, fam, back, vals] 或 None"""
    conn = sqlite3.connect(_W["uri"], uri=True, timeout=30)
    try:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()
    finally:
        conn.close()
    if len(rows) < _W["min_bars"]:
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                     "close", "volume", "amount"])
    df = df.dropna(subset=["close"])
    li = _W["tidx"].get(str(df["date"].iloc[-1]))
    if _W["anchor_i"] is None or li is None:
        return None
    lag = _W["anchor_i"] - li                 # 交易日滞后（>=0）
    if lag > _W["max_lag"]:                   # 超过容忍滞后不记分
        return None
    feats, _label = alpha158.build_features(df)
    row = feats.iloc[-1]
    if int(row.isna().sum()) == len(row):     # 整行退化防御
        return None
    vals = []
    fam_hit = back_hit = False
    for c in _W["feat_cols"]:
        v = row[c]
        if v == v:
            vals.append(float(v))
            continue
        vals.append(0.0)
        if c.startswith(FILL_ZERO_PREFIXES):
            fam_hit = True                    # 近窗零成交额 → 中性
        else:
            back_hit = True                   # 残余退化位兜底（常数窗等）
    return [str(code), lag, int(fam_hit), int(back_hit), vals]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=2000)
    ap.add_argument("--max-lag-days", type=int, default=5,
                    help="容忍个股末根滞后信号日的最大交易日数")
    args = ap.parse_args()
    max_lag = args.max_lag_days

    # ★ 中文路径：LightGBM 文件读写已知缺陷，统一用 model_str= 从字符串加载
    with open(os.path.join(OUT_DIR, "model.txt"), "r", encoding="ascii") as f:
        model = lgb.Booster(model_str=f.read())
    meta = json.load(open(os.path.join(OUT_DIR, "model_feats.json"), encoding="utf-8"))
    feat_cols = meta["feature_cols"]

    # ★B2: 常驻服务可能在推理中途写库（快照/情绪等定时任务）——immutable 快照会
    # 因文件变更报 "database disk image is malformed"；改用普通 ro 连接 + 锁等待
    uri = "file:" + DB.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    # ★ Phase21: 过滤例外票（DATA_EXCLUDE_CODES——复权不可靠）
    try:
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        from app import config as C
        _ex = C.DATA_EXCLUDE_CODES or []
    except Exception:
        _ex = []
    _ex_sql = ",".join("'%s'" % c for c in _ex) if _ex else "''"
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM kline WHERE period='day' AND code NOT IN (%s) "
        "GROUP BY code HAVING COUNT(*) >= %d ORDER BY code LIMIT ?" % (_ex_sql, MIN_BARS),
        (args.codes,))]
    # 全局交易日轴（交易日滞后计数，kline 实际日期即轴）
    tdates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM kline WHERE period='day' ORDER BY date")]
    tidx = {d: i for i, d in enumerate(tdates)}

    # 先取各股原始末根日期定锚（≈全库最新交易日），再逐股补齐口径打分
    last_raw = dict(conn.execute(
        "SELECT code, MAX(date) FROM kline WHERE period='day' GROUP BY code").fetchall())
    cand_last = [last_raw.get(c) for c in codes if last_raw.get(c)]
    signal_date = max(cand_last) if cand_last else ""
    anchor_i = tidx.get(signal_date)

    scores = {}
    stat_lag = {}          # code -> 交易日滞后
    stat_famfill = 0       # 触发族内中性化的票数
    stat_backstop = 0      # 触发残余兜底的票数
    # 候选中超滞后票计数（主进程自算，免传 worker）
    skip_lag = sum(
        1 for c in codes
        if last_raw.get(c) and tidx.get(last_raw[c]) is not None
        and anchor_i is not None and anchor_i - tidx[last_raw[c]] > max_lag)
    t0 = time.time()
    conn.close()   # 主进程只读部分结束；worker 各自开连接

    # ★B2 两遍式：8 进程并行收集特征行（worker 自取数+补齐），最后一次批量推理。
    # （前两轮实测教训：单行逐票 predict 触发 LightGBM 全核并行区自旋，机器繁忙时极慢。）
    from multiprocessing import Pool
    collected = []
    done_n = 0
    with Pool(processes=8, initializer=_init_worker,
              initargs=(uri, MIN_BARS, tdates, anchor_i, max_lag, feat_cols)) as pool:
        for r in pool.imap_unordered(_collect_one, codes, chunksize=16):
            done_n += 1
            if r is not None:
                collected.append(r)
            if done_n % 300 == 0:
                print("  已收集 %d/%d（入池 %d）(%.0fs)"
                      % (done_n, len(codes), len(collected), time.time() - t0),
                      flush=True)

    for code, _lag, fam, back, _vals in collected:
        stat_famfill += fam
        stat_backstop += back
    if collected:
        X = pd.DataFrame([x[4] for x in collected], columns=feat_cols).astype(float)
        preds = model.predict(X, num_threads=8)   # 批量一次推理
        for (code, lag, _f, _b, _vals), p in zip(collected, preds):
            scores[code] = round(float(p), 6)
            stat_lag[code] = lag

    out = {"date": signal_date,
           "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "model": os.path.join(OUT_DIR, "model.txt"),
           "scores": scores}
    path = os.path.join(BASE, "data", "ml_scores.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, path)          # 原子写，主系统读取安全
    from collections import Counter
    lag_hist = Counter(stat_lag.values())
    print(f"已生成 {path}: 锚日={signal_date} 打分={len(scores)}（候选 {len(codes)}）"
          f" 用时{time.time()-t0:.0f}s")
    print(f"口径: min_bars={MIN_BARS} max_lag_td={max_lag} 滞后跳过={skip_lag} "
          f"族中性化={stat_famfill} 票")
    print(f"滞后分布(交易日): {dict(sorted(lag_hist.items()))}")


if __name__ == "__main__":
    main()
