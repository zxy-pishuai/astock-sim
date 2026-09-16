# -*- coding: utf-8 -*-
"""★ 阶段4 ML 选股 sidecar —— 离线训练（qlib Alpha158-style 特征 + LightGBM）
严格隔离：独立 venv（numpy/pandas/lightgbm/qlib 仅安装于此），主系统零依赖。
用法（工具目录下）：
  .venv\\Scripts\\python.exe train.py            # 全量训练
  .venv\\Scripts\\python.exe train.py --codes 300 # 或用部分股票（更快验证）
读 market.db 日K（immutable 只读，不锁库、不写主库）→ 特征 → 标签=未来5日收益
→ LightGBM 回归（按日期切 train/valid/test）→ 保存 model.txt + 特征清单。
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


def load_panel(codes):
    """读日K → 长表（code,date,features...,label）"""
    uri = "file:" + DB.replace("\\", "/") + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    frames = []
    for code in codes:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()
        if len(rows) < MIN_BARS:
            continue
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                         "close", "volume", "amount"])
        df = df.dropna(subset=["close"])
        feats, label = alpha158.build_features(df)
        df = pd.concat([df[["date"]], feats, label.rename("label")], axis=1)
        df["code"] = code
        frames.append(df.dropna(subset=["label"]))
    conn.close()
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=700, help="股票数上限")
    ap.add_argument("--model", default=os.path.join(OUT_DIR, "model.txt"))
    ap.add_argument("--meta", default=os.path.join(OUT_DIR, "model_feats.json"))
    args = ap.parse_args()

    t0 = time.time()
    uri = "file:" + DB.replace("\\", "/") + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
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
    conn.close()
    print(f"股票池 {len(codes)} 只（≥{MIN_BARS}根，排除例外 {len(_ex)} 只），建特征中...")
    panel = load_panel(codes)
    if panel.empty:
        print("无数据")
        return
    print(f"长表样本: {len(panel)} 行 × {len(panel.columns)-3} 特征（{time.time()-t0:.0f}s）")

    feat_cols = [c for c in panel.columns if c not in ("code", "date", "label")]
    X = panel[feat_cols].astype(float)
    y = panel["label"].astype(float)
    # 按日期切分（时间顺序，防未来函数）。
    # ★ 注意：不能按"日期并集"直接切——库内只有少数老股票有超长期历史，
    #   早期日期几乎无样本。改用"行数加权"分位：累计行数达 60%/80% 处为切点。
    cnt = panel.groupby("date")["label"].count()
    dates_sorted = sorted(cnt.index)
    total = int(cnt.sum())
    cut1 = cut2 = None
    acc = 0
    for d in dates_sorted:
        acc += int(cnt[d])
        if cut1 is None and acc >= total * 0.60:
            cut1 = d
        if cut2 is None and acc >= total * 0.80:
            cut2 = d
            break
    if cut1 is None or cut2 is None:
        print("切分失败")
        return
    d_train = [d for d in dates_sorted if d <= cut1]
    d_valid = [d for d in dates_sorted if cut1 < d <= cut2]
    d_test = [d for d in dates_sorted if d > cut2]
    def _slice(ds):
        m = panel["date"].isin(ds)
        return X[m], y[m]
    Xtr, ytr = _slice(d_train)
    Xva, yva = _slice(d_valid)
    Xte, yte = _slice(d_test)
    print(f"切分点: train<= {cut1} ({len(d_train)}日) / valid<= {cut2} ({len(d_valid)}日) / test ({len(d_test)}日)")
    print(f"样本 split: train {len(ytr)} / valid {len(yva)} / test {len(yte)}")

    model = lgb.LGBMRegressor(
        n_estimators=500, learning_rate=0.05, num_leaves=31,
        min_child_samples=200, subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.1, reg_lambda=1.0, random_state=42, verbosity=-1)
    model.fit(Xtr, ytr, eval_set=[(Xva, yva)],
              callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)])

    def _corr(pred, real):
        p = pd.Series(pred); r = pd.Series(real)
        return float(p.corr(r, method="spearman"))

    ic_tr = _corr(model.predict(Xtr), ytr)
    ic_va = _corr(model.predict(Xva), yva)
    ic_te = _corr(model.predict(Xte), yte)
    print(f"IC（预测分 vs 未来5日收益，Spearman）: train {ic_tr:.4f} / valid {ic_va:.4f} / test {ic_te:.4f}")
    print(f"训练用时 {time.time()-t0:.0f}s")
    # ★ 中文路径下的 LightGBM :save_model 直接写文件是已知缺陷（fopen 非 ASCII 路径失败），
    #   改用 model_to_string() 由 Python 落盘；读取端对应用 model_str= 加载。
    model_str = model.booster_.model_to_string()
    with open(args.model, "w", encoding="ascii") as f:
        f.write(model_str)
    feats_imp = sorted(zip(feat_cols, model.feature_importances_),
                       key=lambda x: -x[1])[:15]
    json.dump({"feature_cols": feat_cols, "trained": time.strftime("%Y-%m-%d %H:%M:%S"),
               "codes": len(codes), "samples": len(panel),
               "ic_train": round(ic_tr, 4), "ic_valid": round(ic_va, 4),
               "ic_test": round(ic_te, 4),
               "top_features": [[f, int(i)] for f, i in feats_imp]},
              open(args.meta, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("模型已保存:", args.model)
    print("Top 特征:", feats_imp)


if __name__ == "__main__":
    main()