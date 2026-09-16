# -*- coding: utf-8 -*-
"""★ 阶段1 量价信号包回测：触发后 1/3/5 日期望收益与胜率（本地数据，PIT 合规）
用法：python -m tools.backtest_volprice
输出：docs/reports/phase1_volprice_signals.md + data/signal_pool.json
判定：弃用 if 期望(3日/5日)<=0 or 胜率(3日/5日)<52%（应用回测数据，保守起步）
--include-full：附带全历史（2004 至今）交叉验证
"""
import json
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import volprice as vp

DB = os.path.join(BASE, "data", "market.db")
START = "2025-08-18"      # 回测窗口起点（本地一年数据范围）
END = "2026-08-18"        # 回测窗口终点（最新交易日）
REPORT = os.path.join(BASE, "docs", "reports", "phase1_volprice_signals.md")
POOL = os.path.join(BASE, "data", "signal_pool.json")
INCLUDE_FULL = "--include-full" in sys.argv

# ST/退市 过滤（不可交易标的）
_ST_MAP = {}
try:
    with open(os.path.join(BASE, "data", "stock_list.json"), encoding="utf-8") as f:
        for c, n, _p in json.load(f):
            if "ST" in n or "退" in n:
                _ST_MAP[c] = n
except Exception:
    pass


def load_klines(conn, code):
    rows = conn.execute(
        "SELECT date,open,high,low,close,volume,amount FROM kline "
        "WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()
    out = []
    for r in rows:
        if r[0] is None or r[4] is None:
            continue
        out.append({"date": r[0], "open": r[1], "high": r[2],
                    "low": r[3], "close": r[4], "volume": r[5], "amount": r[6]})
    return out


def _limit_pct(code):
    """涨跌停幅度（与 engine.limit_pct_of 一致）：主板10% / 创业·科创20% / 北交所30%"""
    if code.startswith(("4", "8", "92")):
        return 0.30
    if code.startswith(("30", "68")):
        return 0.20
    return 0.10


def _buyable(code, closes, i):
    """排除接近涨停（收盘≥涨停价×0.99）的触发——涨停买不进，会系统性拉低胜率"""
    lim = _limit_pct(code)
    prev = closes[i - 1]
    if not prev:
        return True
    return closes[i] < prev * (1 + lim) * 0.99


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM kline WHERE period='day' ORDER BY code")]
    codes = [c for c in codes if c not in _ST_MAP]
    print(f"回测股票数: {len(codes)}（剔除 ST/退市 {len(_ST_MAP)}）")

    stats = {k: {"n": 0, "r1": [], "r3": [], "r5": []} for k in vp.SIGNAL_NAMES}
    stats_full = {k: {"n": 0, "r1": [], "r3": [], "r5": []} for k in vp.SIGNAL_NAMES}
    base = {"r1": [], "r3": [], "r5": []}   # 全市场基线（同窗口、同可成交条件）
    for si, code in enumerate(codes):
        if si % 200 == 0:
            print(f"  {si}/{len(codes)} 用时{time.time()-t0:.0f}s")
        kl = load_klines(conn, code)
        n = len(kl)
        if n < 65:
            continue
        closes = [k["close"] for k in kl]
        volumes = [k.get("volume", 0) or 0 for k in kl]
        lows = [k["low"] for k in kl]
        chip = None
        for i in range(60, n - 5):
            d = kl[i]["date"]
            if not (START <= d <= END):
                continue
            if not _buyable(code, closes, i):
                continue
            r1 = closes[i + 1] / closes[i] - 1
            r3 = closes[i + 3] / closes[i] - 1
            r5 = closes[i + 5] / closes[i] - 1
            base["r1"].append(r1); base["r3"].append(r3); base["r5"].append(r5)
            matches = {}
            # 信号函数内部自守卫：pullback_ma250 需 i>=250（年线预热），其余需 20 根
            if vp.detect_breakout_platform(closes, volumes, i):
                matches["breakout_platform"] = True
            if vp.detect_pullback_ma250(closes, lows, volumes, i):
                matches["pullback_ma250"] = True
            if vp.detect_vol_up_confirm(closes, volumes, i):
                matches["vol_up_confirm"] = True
            if chip is None:
                chip = ind_chip(kl)
            if vp.detect_chip_peak_support(chip, lows, closes, i):
                matches["chip_peak_support"] = True
            if not matches:
                continue
            for k in matches:
                stats[k]["n"] += 1
                stats[k]["r1"].append(r1); stats[k]["r3"].append(r3); stats[k]["r5"].append(r5)
                stats_full[k]["n"] += 1
                stats_full[k]["r1"].append(r1); stats_full[k]["r3"].append(r3); stats_full[k]["r5"].append(r5)
        if INCLUDE_FULL:
            # 全历史交叉验证（窗口外也统计）
            for i in range(60, n - 5):
                if START <= kl[i]["date"] <= END:
                    continue
                if not _buyable(code, closes, i):
                    continue
                matches = {}
                if vp.detect_breakout_platform(closes, volumes, i):
                    matches["breakout_platform"] = True
                if vp.detect_pullback_ma250(closes, lows, volumes, i):
                    matches["pullback_ma250"] = True
                if vp.detect_vol_up_confirm(closes, volumes, i):
                    matches["vol_up_confirm"] = True
                if chip is None:
                    chip = ind_chip(kl)
                if vp.detect_chip_peak_support(chip, lows, closes, i):
                    matches["chip_peak_support"] = True
                if not matches:
                    continue
                r1 = closes[i + 1] / closes[i] - 1
                r3 = closes[i + 3] / closes[i] - 1
                r5 = closes[i + 5] / closes[i] - 1
                for k in matches:
                    stats_full[k]["n"] += 1
                    stats_full[k]["r1"].append(r1); stats_full[k]["r3"].append(r3); stats_full[k]["r5"].append(r5)
    conn.close()

    L = []
    def P(s=""):
        print(s); L.append(s)

    P("# 阶段1 量价信号包回测报告")
    P("")
    P(f"- 回测窗口: {START} ~ {END}（本地一年数据）｜股票数: {len(codes)}")
    P("- 数据覆盖: 信号 a/c/d 需 ≥65 根日K（全库评估）；信号 b 需 ≥260 根（年线预热，仅覆盖历史充足股票）")
    P("- 逻辑口径: 触发日后 1/3/5 个该股实际交易日的收盘对收盘收益")
    P("- 可成交过滤: 排除触发日收盘≥涨停价×99% 的样本（涨停买不进）")
    P("- 入选规则: 期望收益(3日/5日)>0 且 胜率(3日/5日)≥52%；不满足即弃用")
    P("")
    if base["r1"]:
        bn = len(base["r1"])
        bw3 = sum(1 for r in base["r3"] if r > 0) / bn
        bw5 = sum(1 for r in base["r5"] if r > 0) / bn
        bm1 = sum(base["r1"]) / bn; bm3 = sum(base["r3"]) / bn; bm5 = sum(base["r5"]) / bn
        P(f"- 全市场基线（同窗口同条件，N={bn}）: 1日均值{bm1:+.2%} / 3日均值{bm3:+.2%}(胜率{bw3:.1%}) "
          f"/ 5日均值{bm5:+.2%}(胜率{bw5:.1%})")
        P("")
    P("| 信号 | N(≥阈值) | 1日胜率 | 3日胜率 | 5日胜率 | 1日均值 | 3日均值 | 5日均值 | 判定 |")
    P("|---|---|---|---|---|---|---|---|---|")
    keep = []
    for k, name in vp.SIGNAL_NAMES.items():
        s = stats[k]
        if s["n"] == 0:
            P(f"| {name} | 0 | - | - | - | - | - | - | ❌ 样本不足 |")
            continue
        win1 = sum(1 for r in s["r1"] if r > 0) / s["n"]
        win3 = sum(1 for r in s["r3"] if r > 0) / s["n"]
        win5 = sum(1 for r in s["r5"] if r > 0) / s["n"]
        m1 = sum(s["r1"]) / s["n"]; m3 = sum(s["r3"]) / s["n"]; m5 = sum(s["r5"]) / s["n"]
        ok = (m3 > 0 and m5 > 0 and win3 >= 0.52 and win5 >= 0.52)
        verdict = "✅ 入选" if ok else "❌ 弃用"
        if ok:
            keep.append(k)
        P(f"| {name} | {s['n']} | {win1:.1%} | {win3:.1%} | {win5:.1%} | "
          f"{m1:+.2%} | {m3:+.2%} | {m5:+.2%} | {verdict} |")
    P("")
    P("### 入选信号")
    P("| 信号 | 键 | 建议权重 |")
    P("|---|---|---|")
    if not keep:
        P("| (无) | - | - |")
    else:
        for k in keep:
            P(f"| {vp.SIGNAL_NAMES[k]} | {k} | （config.VOLPRICE_WEIGHTS 配置） |")
    P("")
    P("### 说明")
    P("- 信号在回测与线上评分共用 app/volprice.py 同一实现，无漂移")
    if INCLUDE_FULL:
        P("")
        P("### 附：全历史交叉验证（2004 至今，窗口外触发）")
        P("| 信号 | N | 3日胜率 | 5日胜率 | 3日均值 | 5日均值 |")
        P("|---|---|---|---|---|---|")
        for k, name in vp.SIGNAL_NAMES.items():
            s = stats_full[k]
            if s["n"] == 0:
                continue
            win3 = sum(1 for r in s["r3"] if r > 0) / s["n"]
            win5 = sum(1 for r in s["r5"] if r > 0) / s["n"]
            m3 = sum(s["r3"]) / s["n"]; m5 = sum(s["r5"]) / s["n"]
            P(f"| {name} | {s['n']} | {win3:.1%} | {win5:.1%} | {m3:+.2%} | {m5:+.2%} |")
    P("")
    P(f"总用时 {time.time()-t0:.0f}s")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    with open(POOL, "w", encoding="utf-8") as f:
        json.dump({
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "window": [START, END],
            "admitted": keep,
            "metrics": {
                k: {"n": stats[k]["n"],
                    "win3": (sum(1 for r in stats[k]["r3"] if r > 0) / stats[k]["n"]) if stats[k]["n"] else 0,
                    "win5": (sum(1 for r in stats[k]["r5"] if r > 0) / stats[k]["n"]) if stats[k]["n"] else 0,
                    "mean3": (sum(stats[k]["r3"]) / stats[k]["n"]) if stats[k]["n"] else 0,
                    "mean5": (sum(stats[k]["r5"]) / stats[k]["n"]) if stats[k]["n"] else 0}
                for k in vp.SIGNAL_NAMES},
        }, f, ensure_ascii=False, indent=1)
    print("报告:", REPORT)
    print("信号池:", POOL)


def ind_chip(kl):
    """懒加载 chip_profile（只有信号 d 用到时才计算）"""
    from app import indicators as ind
    return ind.chip_profile(kl)


if __name__ == "__main__":
    main()