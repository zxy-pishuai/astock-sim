# -*- coding: utf-8 -*-
"""★ 阶段4 离线对比报告：ML 分 vs score_stock（IC / top20 五日收益 / 胜率）
判定：ML 在测试窗口的 IC 与 top20 五日收益均需明显优于 score_stock 才建议接入；
未达优势 → 不接入并给出原因（阶段4 交付物）。
用法（工具目录下）：
  .venv\\Scripts\\python.exe compare_report.py [--codes 400] [--test_days 40]
注意：本脚本在 sidecar venv 运行，但通过 sys.path 引入主系统 app.scoring
（app 模块为纯标准库，可在隔离环境直接使用，保证评分口径与线上一致）。
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
sys.path.insert(0, BASE)          # 引入主系统 app.scoring（纯标准库）
from app import scoring as sc     # noqa: E402

DB = os.path.join(BASE, "data", "market.db")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(BASE, "docs", "reports", "phase4_ml_sidecar.md")
MIN_BARS = 130
L = []
def P(s=""):
    print(s); L.append(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=400)
    ap.add_argument("--test_days", type=int, default=40)
    args = ap.parse_args()
    t0 = time.time()

    # ★ 中文路径：LightGBM 文件读写已知缺陷，统一用 model_str= 从字符串加载
    with open(os.path.join(OUT_DIR, "model.txt"), "r", encoding="ascii") as f:
        model = lgb.Booster(model_str=f.read())
    meta = json.load(open(os.path.join(OUT_DIR, "model_feats.json"), encoding="utf-8"))
    feat_cols = meta["feature_cols"]

    uri = "file:" + DB.replace("\\", "/") + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM kline WHERE period='day' GROUP BY code "
        f"HAVING COUNT(*) >= {MIN_BARS} ORDER BY code LIMIT ?", (args.codes,))]

    # 各股面板
    panels = {}
    for code in codes:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()
        if len(rows) < MIN_BARS:
            continue
        panels[code] = rows
    conn.close()

    # 测试窗口：全局最近 N 个交易日（取各股日期并集的尾部）
    all_dates = sorted({r[0] for rows in panels.values() for r in rows})
    test_dates = all_dates[-args.test_days:]

    # 逐股预计算：ML特征行 + score_stock 分（按 test_dates 索引）
    ml_rows = {}     # code -> {date: pred}
    ss_rows = {}     # code -> {date: (score, signals)}
    for ci, (code, rows) in enumerate(panels.items()):
        if ci % 100 == 0:
            print(f"  面板计算 {ci}/{len(panels)} {time.time()-t0:.0f}s")
        if len(rows) < MIN_BARS:
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                         "close", "volume", "amount"])
        df = df.dropna(subset=["close"])
        date_list = list(df["date"])
        idx = {d: i for i, d in enumerate(date_list)}
        feats, _label = alpha158.build_features(df)
        # ML 预测（对每个测试日）
        m = {}
        for d in test_dates:
            i = idx.get(d)
            if i is None or i < 60:
                continue
            row = feats.iloc[i:i + 1]
            if row.isna().any(axis=1).any():
                continue
            m[d] = float(model.predict(row[feat_cols].astype(float))[0])
        if m:
            ml_rows[code] = m
        # score_stock（对每个测试日，PIT：传入该日及之前K线）
        kl_full = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
                    "close": r[4], "volume": r[5], "amount": r[6]} for r in rows]
        s = {}
        for d in test_dates:
            i = idx.get(d)
            if i is None or i < 60:
                continue
            hist = kl_full[:i + 1]
            try:
                score, _sig = sc.score_stock(hist, code=code, as_of=d)
                s[d] = score
            except Exception:
                continue
        if s:
            ss_rows[code] = s

    # 逐日截面统计
    ml_ics, ss_ics = [], []
    ml_top20, ss_top20 = [], []
    ml_top20_wr, ss_top20_wr = [], []
    for d in test_dates:
        cross = []
        for code in panels:
            base_i = None
            i5 = None
            rows = panels[code]
            for j, r in enumerate(rows):
                if r[0] == d:
                    base_i = j
            if base_i is None or base_i + 5 >= len(rows):
                continue
            fwd = rows[base_i + 5][4] / rows[base_i][4] - 1
            mlv = ml_rows.get(code, {}).get(d)
            ssv = ss_rows.get(code, {}).get(d)
            cross.append({"code": code, "fwd": fwd, "ml": mlv, "ss": ssv})
        if len(cross) < 30:
            continue
        def _ic(key):
            pairs = [(c[key], c["fwd"]) for c in cross if c[key] is not None]
            if len(pairs) < 30:
                return None
            xs = pd.Series([p[0] for p in pairs])
            ys = pd.Series([p[1] for p in pairs])
            return float(xs.rank().corr(ys.rank()))
        def _top20(key):
            cand = sorted([c for c in cross if c[key] is not None],
                          key=lambda x: -x[key])[:20]
            if not cand:
                return None, None
            rets = [c["fwd"] for c in cand]
            return float(np.mean(rets)), float(np.mean([1 if r > 0 else 0 for r in rets]))
        m_ic, s_ic = _ic("ml"), _ic("ss")
        if m_ic is not None:
            ml_ics.append(m_ic)
        if s_ic is not None:
            ss_ics.append(s_ic)
        m20, m20w = _top20("ml")
        s20, s20w = _top20("ss")
        if m20 is not None:
            ml_top20.append(m20); ml_top20_wr.append(m20w)
        if s20 is not None:
            ss_top20.append(s20); ss_top20_wr.append(s20w)

    P("# 阶段4 ML 选股 sidecar 离线对比报告")
    P("")
    P(f"- 测试窗口: {test_dates[0]} ~ {test_dates[-1]}（{len(test_dates)} 交易日）｜股票池: {len(panels)} 只")
    P("- ML: qlib Alpha158-style 特征（~44 维）+ LightGBM 回归(未来5日收益)｜score_stock: 主系统多维评分")
    P("")
    P("| 指标 | ML 分 | score_stock | 评定 |")
    P("|---|---|---|---|")
    def _avg(x):
        return float(np.mean(x)) if x else None
    mic = _avg(ml_ics); sic = _avg(ss_ics)
    mk = "ML更优" if (mic is not None and sic is not None and mic > sic) else ""
    P(f"| IC 均值 | {mic if mic is not None else '-'} | {sic if sic is not None else '-'} | {mk} |")
    m20 = _avg(ml_top20); s20 = _avg(ss_top20)
    mk2 = "ML更优" if (m20 is not None and s20 is not None and m20 > s20) else ""
    P(f"| top20 五日平均收益 | {m20 if m20 is not None else '-'} | {s20 if s20 is not None else '-'} | {mk2} |")
    mw = _avg(ml_top20_wr); sw = _avg(ss_top20_wr)
    P(f"| top20 胜率 | {mw if mw is not None else '-'} | {sw if sw is not None else '-'} | |")
    P("")
    advantage = (mic is not None and sic is not None and mic > sic * 1.05
                 and m20 is not None and s20 is not None and m20 > s20)
    P("## 结论")
    if advantage:
        P("**ML 分在 IC 与 top20 五日收益上明显优于 score_stock → 建议接入（小权重，开关默认关闭）**")
    elif m20 is not None and s20 is not None and m20 <= s20 and (mic is not None and sic is not None and mic > sic):
        P("**ML 分 IC 更优但 top20 五日收益/胜率不占优 → 排序能力未转化为选股收益，"
          "不满足接入标准，暂不接入（sidecar 保留供夜间研究）**")
    elif m20 is not None and s20 is not None and m20 > s20:
        P("**ML 分 top20 收益略优但 IC 未占优 → 收益同源（跟随市场行情），无增量选股能力，暂不接入**")
    else:
        P("**ML 分相对 score_stock 无显著优势（IC 与 top20 均未占优）→ 不接入评分，sidecar 保留供夜间研究**")
    P("")
    P("### 说明")
    P("- ML sidecar 与主系统严格隔离：独立 venv（numpy/pandas/lightgbm/qlib），主系统零第三方依赖")
    P("- data/ml_scores.json 由 predict.py 夜间生成；主系统读取 >26 小时自动失效降级")
    P("- 单一区间样本（约 2 个月测试窗）；若启用需多窗口复核")
    P(f"- 总用时 {time.time()-t0:.0f}s")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("报告:", REPORT)


if __name__ == "__main__":
    main()