# -*- coding: utf-8 -*-
"""★ Phase75：predict.py 覆盖率诊断（只诊断，不改 predict.py）

现状：predict.py 700 只候选只产出 177 分。本脚本**逐条复刻其筛选链**
（同一 SQL 候选集 HAVING COUNT(*)>=130 ORDER BY code LIMIT 700；同序 R1→R2→R3），
逐只归因剔除原因：
  OK                正常出分
  R1 bars<MIN_BARS(130)        历史根数不足（SQL 已滤，正常为 0，防御性保留）
  R2 末根日期≠信号日           停牌/数据滞后（信号日 = 首只候选 dropna 后末根日期）
  R3 末根特征含 NaN            alpha158 末行任一特征 NaN（逐列统计）
并量化修复建议的回收效果：
  F1 末行 NaN 特征 fillna(0)：按 NaN 列数分档（≤3 低风险 / 4-10 中 / >10 高）
  F2 MIN_BARS 130→100：量化新增可入池票数及其对 ORDER BY code LIMIT 700 的挤占
输出: data/ml_coverage_diag.json + 终端表（结论进 docs/reports/ml_short_label.md）

用法（ml_sidecar venv，工具目录下）:
  .venv\\Scripts\\python.exe diagnose_coverage.py
"""
import json
import os
import sqlite3
import sys
from collections import Counter

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(BASE, "data", "market.db")
OUT = os.path.join(BASE, "data", "ml_coverage_diag.json")
MIN_BARS = 130          # 与 predict.py 完全一致
UNIVERSE = 700          # predict.py --codes 默认
WORKERS = 8
URI = "file:" + DB.replace("\\", "/") + "?mode=ro&immutable=1"


def candidate_codes():
    """predict.py 同一条 SQL：候选集（ORDER BY code LIMIT 700）"""
    conn = sqlite3.connect(URI, uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT code FROM kline WHERE period='day' GROUP BY code "
            "HAVING COUNT(*) >= ? ORDER BY code LIMIT ?", (MIN_BARS, UNIVERSE))]
    finally:
        conn.close()


def anchor_signal_date(codes):
    """predict 口径：首只 len(rows)>=MIN_BARS 候选，dropna(close) 后末根日期"""
    conn = sqlite3.connect(URI, uri=True)
    try:
        for code in codes:
            rows = conn.execute(
                "SELECT date,close FROM kline WHERE code=? AND period='day' "
                "ORDER BY date", (code,)).fetchall()
            if len(rows) < MIN_BARS:
                continue
            dates = [str(r[0]) for r in rows if r[1] is not None]
            return dates[-1] if dates else ""
        return ""
    finally:
        conn.close()


def _diag_one(task):
    code, signal_date = task
    import alpha158      # worker 内导入（spawn 下子进程自行加载）
    conn = sqlite3.connect(URI, uri=True)
    try:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()
    finally:
        conn.close()
    rec = {"code": code}
    if len(rows) < MIN_BARS:                     # 防御：SQL 已滤，正常不应出现
        rec["reason"] = "R1_bars<%d" % MIN_BARS
        rec["bars"] = len(rows)
        return rec
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                     "close", "volume", "amount"])
    df = df.dropna(subset=["close"])
    last = str(df["date"].iloc[-1])
    rec["bars"] = int(len(df))
    rec["last_date"] = last
    rec["amt20"] = round(float(pd.Series(
        [r[6] if r[6] is not None else 0.0 for r in rows[-20:]], dtype=float).mean()), 2)
    if signal_date and last != signal_date:
        rec["reason"] = "R2_lastdate_mismatch"
        return rec
    feats, _label = alpha158.build_features(df)
    row = feats.iloc[-1]
    na_cols = [c for c in feats.columns if bool(row[c] != row[c])]
    if na_cols:
        rec["reason"] = "R3_nan_features"
        rec["na_count"] = len(na_cols)
        rec["na_cols"] = na_cols
        return rec
    rec["reason"] = "OK"
    return rec


def main():
    codes = candidate_codes()
    signal_date = anchor_signal_date(codes)
    print("候选（predict 同 SQL）%d 只，信号日=%s" % (len(codes), signal_date), flush=True)

    from concurrent.futures import ProcessPoolExecutor
    tasks = [(c, signal_date) for c in codes]
    per_code = []
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for i, rec in enumerate(ex.map(_diag_one, tasks)):
            per_code.append(rec)
            if (i + 1) % 100 == 0:
                print("  %d/%d" % (i + 1, len(tasks)), flush=True)

    cnt = Counter(x["reason"] for x in per_code)
    nan_cols = Counter()
    na_width = Counter()               # 每只 R3 票的 NaN 列数分布
    for x in per_code:
        if x["reason"] == "R3_nan_features":
            na_width[x["na_count"]] += 1
            for c in x["na_cols"]:
                nan_cols[c] += 1

    def _median(key, reason):
        vals = sorted(x.get(key) for x in per_code
                      if x["reason"] == reason and x.get(key) is not None)
        n = len(vals)
        return round(vals[n // 2], 2) if n else None

    # F2 模拟：MIN_BARS→100 时新增可入池票（且会被 ORDER BY code 挤占尾部名额）
    cur_max_code = max(codes) if codes else ""
    conn = sqlite3.connect(URI, uri=True)
    try:
        f2_new = [r[0] for r in conn.execute(
            "SELECT code FROM kline WHERE period='day' GROUP BY code "
            "HAVING COUNT(*) >= 100 AND COUNT(*) < ? AND code <= ?",
            (MIN_BARS, cur_max_code))]
        total_100plus = conn.execute(
            "SELECT COUNT(*) FROM (SELECT code FROM kline WHERE period='day' "
            "GROUP BY code HAVING COUNT(*) >= 100)").fetchone()[0]
    finally:
        conn.close()

    doc = {
        "universe": len(codes), "signal_date": signal_date,
        "scored_now": cnt.get("OK", 0),
        "reason_counts": dict(cnt),
        "median_bars": {"OK": _median("bars", "OK"),
                        "R3": _median("bars", "R3_nan_features"),
                        "R2": _median("bars", "R2_lastdate_mismatch")},
        "median_amt20": {"OK": _median("amt20", "OK"),
                         "R3": _median("amt20", "R3_nan_features"),
                         "R2": _median("amt20", "R2_lastdate_mismatch")},
        "na_cols_top15": nan_cols.most_common(15),
        "r3_na_width_hist": dict(sorted(na_width.items())),
        "fix_F1_fillna0_tiers": {
            "low_risk_le3cols": sum(v for k, v in na_width.items() if k <= 3),
            "mid_risk_4to10": sum(v for k, v in na_width.items() if 4 <= k <= 10),
            "high_risk_gt10": sum(v for k, v in na_width.items() if k > 10)},
        "fix_F2_minbars_100": {
            "new_eligible_total": len(f2_new),
            "note": "并入后仍 ORDER BY code LIMIT 700：新增票按代码序插入，"
                    "会顶掉尾部的最大代码若干只；DB 内 ≥100 根总票数为 %d" % total_100plus,
            "new_codes_sample": f2_new[:20]},
        "per_code": per_code,
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    print("\n===== 归因表 =====")
    print("OK=%d  R1根数不足=%d  R2末根日期不符=%d  R3特征NaN=%d" % (
        cnt.get("OK", 0), cnt.get("R1_bars<%d" % MIN_BARS, 0),
        cnt.get("R2_lastdate_mismatch", 0), cnt.get("R3_nan_features", 0)))
    print("NaN 列 top10:", nan_cols.most_common(10))
    print("R3 每票 NaN 列数分布:", dict(sorted(na_width.items())))
    print("F1 fillna(0) 分档: ≤3列=%d  4-10列=%d  >10列=%d" % (
        doc["fix_F1_fillna0_tiers"]["low_risk_le3cols"],
        doc["fix_F1_fillna0_tiers"]["mid_risk_4to10"],
        doc["fix_F1_fillna0_tiers"]["high_risk_gt10"]))
    print("F2 MIN_BARS→100: 新增可入池 %d 只（DB 内 ≥100 根共 %d 只）"
          % (len(f2_new), total_100plus))
    print("已写出", OUT)


if __name__ == "__main__":
    main()
