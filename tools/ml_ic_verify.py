# -*- coding: utf-8 -*-
"""★ ML 预测 IC 验证工具（纯标准库）：逐月 Spearman IC / ICIR / 十分组未来5日均收益
- 读 market.db 的 ml_pred（滚动预测分）与 kline（未来5日收益）
- 输出: 逐日/逐月 IC、ICIR、日 IC>0 占比、十分组未来5日均收益
- 落盘: data/ml_ic_monthly.json（逐月 IC 序列）+ data/ml_ic_report.json（汇总）
用法: python tools/ml_ic_verify.py [--start 2019-06-01]
"""
import argparse
import collections
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "market.db")


def spearman(x, y):
    """纯标准库 Spearman 秩相关"""
    n = len(x)
    if n < 3:
        return None
    def rank(v):
        idx = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(x), rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx * dy > 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-06-01", help="fwd5 计算起点（早于 ml_pred 首日）")
    args = ap.parse_args()
    t0 = time.time()
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)

    # 1) kline: 每股日 close，逐票算未来5日收益（fwd5: date -> {code: ret}）
    fwd = {}
    last_code, seq = None, []
    rows = conn.execute(
        "SELECT code, date, close FROM kline WHERE period='day' AND date>=? "
        "ORDER BY code, date", (args.start,)).fetchall()
    for code, d, c in rows:
        if code != last_code:
            if last_code is not None and len(seq) >= 6:
                for i in range(len(seq) - 5):
                    c0, d0 = seq[i]
                    c5 = seq[i + 5][0]
                    if c0 and c0 > 0 and c5:
                        fwd.setdefault(d0, {})[last_code] = (c5 / c0 - 1.0)
            last_code, seq = code, []
        seq.append((c, d))
    if last_code is not None and len(seq) >= 6:
        for i in range(len(seq) - 5):
            c0, d0 = seq[i]
            c5 = seq[i + 5][0]
            if c0 and c0 > 0 and c5:
                fwd.setdefault(d0, {})[last_code] = (c5 / c0 - 1.0)
    print("fwd5 日期数: %d (%.0fs)" % (len(fwd), time.time() - t0), flush=True)

    # 2) ml_pred 预测分
    pred = {}
    for d, c, s in conn.execute("SELECT date, code, score FROM ml_pred ORDER BY date"):
        pred.setdefault(d, {})[c] = s
    print("ml_pred 日期数: %d (%.0fs)" % (len(pred), time.time() - t0), flush=True)
    conn.close()

    # 3) 逐日 IC
    daily_ic = []
    for d in sorted(pred.keys()):
        fp = fwd.get(d)
        if not fp:
            continue
        xs, ys = [], []
        for c, s in pred[d].items():
            if c in fp:
                xs.append(s)
                ys.append(fp[c])
        ic = spearman(xs, ys)
        if ic is not None:
            daily_ic.append((d, ic))
    print("有效日 IC: %d" % len(daily_ic), flush=True)

    # 4) 逐月 IC 序列（落盘）
    monthly = collections.OrderedDict()
    for d, ic in daily_ic:
        monthly.setdefault(d[:7], []).append(ic)
    month_ics = []
    for m, vals in monthly.items():
        month_ics.append({"month": m, "ic": round(sum(vals) / len(vals), 4), "n": len(vals)})
    json.dump(month_ics, open(os.path.join(BASE, "data", "ml_ic_monthly.json"), "w",
              encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n逐月 IC 序列已存 data/ml_ic_monthly.json (%d 个月)" % len(month_ics), flush=True)
    for m in month_ics:
        print("  %s: IC=%.4f (n=%d)" % (m["month"], m["ic"], m["n"]), flush=True)

    # 5) 汇总: ICIR / 占比 / 十分组
    ics = [m["ic"] for m in month_ics]
    report = {"months": month_ics, "ic_mean": None, "ic_std": None, "icir": None,
              "pos_day_ratio": None, "deciles": [], "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if len(ics) >= 2:
        ic_mean = sum(ics) / len(ics)
        ic_std = (sum((x - ic_mean) ** 2 for x in ics) / max(len(ics) - 1, 1)) ** 0.5
        report["ic_mean"] = round(ic_mean, 4)
        report["ic_std"] = round(ic_std, 4)
        report["icir"] = round(ic_mean / ic_std, 3) if ic_std > 0 else None
    pos_cnt = sum(1 for _, ic in daily_ic if ic > 0)
    report["pos_day_ratio"] = round(pos_cnt / max(len(daily_ic), 1) * 100, 2)
    report["days_ic"] = len(daily_ic)
    # 十分组未来5日均收益
    dec_ret = [0.0] * 10
    dec_cnt = [0] * 10
    for d in sorted(pred.keys()):
        fp = fwd.get(d)
        if not fp:
            continue
        pairs = [(c, s, fp[c]) for c, s in pred[d].items() if c in fp]
        if len(pairs) < 20:
            continue
        pairs.sort(key=lambda x: x[1])
        n = len(pairs)
        for i in range(n):
            g = min(9, int(i * 10 / n))
            dec_ret[g] += pairs[i][2]
            dec_cnt[g] += 1
    for g in range(10):
        if dec_cnt[g] > 0:
            report["deciles"].append({
                "group": g, "label": "分位%d(最低%d%%)" % (g, (g + 1) * 10),
                "avg_fwd5": round(dec_ret[g] / dec_cnt[g], 4), "n": dec_cnt[g]})
    if report["deciles"]:
        lo, hi = report["deciles"][0], report["deciles"][-1]
        report["long_short_gap"] = round(hi["avg_fwd5"] - lo["avg_fwd5"], 4)
    json.dump(report, open(os.path.join(BASE, "data", "ml_ic_report.json"), "w",
              encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n=== IC 汇总 ===")
    print("ICIR=%.3f (IC均值 %.4f, std %.4f, %d 个月)" % (
        report["icir"] or 0, report["ic_mean"] or 0, report["ic_std"] or 0, len(ics)))
    print("日 IC>0 占比: %.1f%% (%d/%d)" % (report["pos_day_ratio"] or 0, pos_cnt, len(daily_ic)))
    print("\n=== 十分组未来5日均收益 ===")
    for g in report["deciles"]:
        print("  %s: %+.4f (n=%d)" % (g["label"], g["avg_fwd5"], g["n"]))
    print("多头-空头价差: %+.4f /5日" % (report["long_short_gap"] or 0))
    print("已存 data/ml_ic_report.json | 总耗时 %.0fs" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    main()