# -*- coding: utf-8 -*-
"""★ 阶段3 GTJA191 量价因子验证：IC/ICIR/分层单调性/与现有因子相关性
用法：python -m tools.validate_gtja_factors
输出：docs/reports/phase3_gtja_factors.md；候选/入选因子写入 market.db factor_pool 表
入选规则：abs(IC)>0.03 且 与现有因子最大相关性<0.8 → 候选
         候选按 ICIR 与单调性择优 → 建议接入（config.GTJA_FACTOR_WEIGHTS，默认关闭）
"""
import json
import math
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import factor as F
from app import factor_gtja as FG

DB = os.path.join(BASE, "data", "market.db")
REPORT = os.path.join(BASE, "docs", "reports", "phase3_gtja_factors.md")
HORIZON = 5          # 未来 5 日收益
MIN_BARS = 160       # 个股最少根数（20窗口+tsrank20+IC5）
CORR_BAR = 120       # 相关性计算尾部根数

# 与现有因子比对集合（排除 GTJA 新注册的）
NEW_NAMES = list(FG.GTJA_FACTORS.keys())
EXISTING_NAMES = [n for n in F.FACTORS.keys() if n not in NEW_NAMES]

L = []
def P(s=""):
    print(s); L.append(s)


def load_universe():
    conn = sqlite3.connect(DB)
    codes = [r[0] for r in conn.execute(
        "SELECT code, COUNT(*) c FROM kline WHERE period='day' "
        "GROUP BY code HAVING c>=?", (MIN_BARS,))]
    kbc = {}
    for c in codes:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' ORDER BY date", (c,)).fetchall()
        kl = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
               "close": r[4], "volume": r[5], "amount": r[6]} for r in rows
              if r[0] and r[4]]
        if len(kl) >= MIN_BARS:
            kbc[c] = kl
    conn.close()
    return kbc


def per_code_corr(name_a, name_b, kbc, bar=CORR_BAR):
    """两因子同股对齐时序 |Pearson| 均值（跨股票平均）。
    传入已算好的 (values, ok) 缓存字典 {code: {name: values}}，避免重复计算。"""
    corrs = []
    for c, kl in kbc.items():
        va = _VAL_CACHE.get(c, {}).get(name_a)
        vb = _VAL_CACHE.get(c, {}).get(name_b)
        if va is None or vb is None:
            continue
        xs, ys = [], []
        for i in range(len(va) - bar, len(va)):
            if va[i] is not None and vb[i] is not None:
                xs.append(va[i]); ys.append(vb[i])
        if len(xs) < 20:
            continue
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        num = sum((xs[j] - mx) * (ys[j] - my) for j in range(len(xs)))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if dx > 0 and dy > 0:
            corrs.append(abs(num / (dx * dy)))
    return sum(corrs) / len(corrs) if corrs else None


# 因子值缓存：{code: {name: values}} —— 每只股票每个因子只算一次（含 chip 等重因子）
_VAL_CACHE = {}


def warm_value_cache(kbc, names):
    """预计算每只股票在 names 集合下全部因子值（只保留尾部 bar 个）"""
    n_done = 0
    for c, kl in kbc.items():
        d = {}
        for name in names:
            vals, ok = F.compute_factor(name, kl)
            d[name] = vals if ok else None
        _VAL_CACHE[c] = d
        n_done += 1
        if n_done % 300 == 0:
            print(f"  因子值预热 {n_done}/{len(kbc)}")


def main():
    t0 = time.time()
    kbc = load_universe()
    P("# 阶段3 GTJA191 量价因子精选验证报告")
    P("")
    P(f"- 股票池: {len(kbc)} 只（本地≥{MIN_BARS}根日K）｜未来收益: {HORIZON} 日｜因子数: {len(NEW_NAMES)}")
    P("- 适配说明: GTJA191 的 RANK 为截面；本系统按个股时序实现（rank→ts_rank 滚动分位），"
      "即与 qlib Alpha158 单股切片同思路")
    P("")
    P("## 1. 单因子 IC 验证（Spearman 截面相关 逐日 → IC均值/ICIR/正占比）")
    P("| 因子 | 表达式 | IC均值 | ICIR | 正占比 | 分层单调性 | 判定 |")
    P("|---|---|---|---|---|---|---|")
    ic_results = []
    for name in NEW_NAMES:
        icr = F.factor_ic(name, kbc, horizon=HORIZON)
        ic_results.append(icr)
    ic_by_name = {r["name"]: r for r in ic_results}
    layers_by_name = {}
    for name in NEW_NAMES:
        lay = F.factor_layers(name, kbc, horizon=HORIZON, layers=5)
        layers_by_name[name] = lay
    # 与现有因子相关性（只对有 IC 的因子）
    # 预热因子值缓存（现有+新因子，每只股票各算一次，避免两两比对时重复计算）
    warm_value_cache(kbc, EXISTING_NAMES + NEW_NAMES)
    corr_max = {}
    for name in NEW_NAMES:
        best, bestv = "", 0.0
        for en in EXISTING_NAMES:
            v = per_code_corr(name, en, kbc)
            if v is not None and v > bestv:
                best, bestv = en, v
        corr_max[name] = (best, bestv)
    for name in NEW_NAMES:
        r = ic_by_name.get(name) or {}
        icm = r.get("ic_mean")
        expr = FG.GTJA_FACTORS[name][1]
        lay = layers_by_name.get(name, {})
        mono = F.factor_monotonicity(lay.get("layers", []))
        if icm is None:
            P(f"| {name} | {expr} | - | - | - | - | 样本不足 |")
            continue
        ok_ic = abs(icm) > 0.03
        maxc = corr_max.get(name, ("", 1.0))[1]
        ok_corr = maxc < 0.8
        verdict = "候选" if (ok_ic and ok_corr) else \
            ("IC不足" if not ok_ic else "与现有因子重叠(>0.8)")
        P(f"| {name} | {expr} | {icm:+.4f} | {r.get('icir') or 0:+.2f} | "
          f"{r.get('positive_ratio') or 0:.2f} | {mono:.2f} | {verdict} |")
    P("")
    P("## 2. 候选因子与现有因子最大相关性")
    for name in NEW_NAMES:
        best, bestv = corr_max.get(name, ("", 1.0))
        if best:
            P(f"- {name}: 与[{best}] |r|={bestv:.3f}")
    P("")
    # 择优：候选按 (ICIR, 单调性) 排序
    cands = []
    for name in NEW_NAMES:
        r = ic_by_name.get(name) or {}
        icm = r.get("ic_mean")
        if icm is None:
            continue
        maxc = corr_max.get(name, ("", 1.0))[1]
        if abs(icm) > 0.03 and maxc < 0.8:
            lay = layers_by_name.get(name, {})
            mono = F.factor_monotonicity(lay.get("layers", []))
            cands.append({
                "name": name, "ic": icm, "icir": r.get("icir") or 0,
                "mono": mono, "pos": r.get("positive_ratio") or 0,
                "corr": maxc, "expr": FG.GTJA_FACTORS[name][1],
            })
    cands.sort(key=lambda x: (x["icir"], x["mono"]), reverse=True)
    P("## 3. 择优建议（候选按 ICIR×单调性排序）")
    P("| 排名 | 因子 | IC | ICIR | 单调性 | 与现有因子|r|max | 建议权重 |")
    P("|---|---|---|---|---|---|---|")
    TAKE = 6
    admitted = cands[:TAKE]
    for i, c in enumerate(cands):
        w = round(0.05 * max(1, 6 - i), 3) if i < TAKE else 0.0
        P(f"| {i+1} | {c['name']} | {c['ic']:+.4f} | {c['icir']:+.2f} | {c['mono']:.2f} | "
          f"{c['corr']:.2f} | {w if i < TAKE else '-'} |")
    P("")
    if not admitted:
        P("**无候选因子达标（IC>0.03 且 |r|<0.8）→ 阶段3 不接入评分，保留因子库供后续验证**")
    else:
        P(f"**入选 {len(admitted)} 个候选；接入评分需在 config.GTJA_FACTOR_WEIGHTS 配权重，"
          f"且 GTJA_FACTORS_ENABLED 默认=False**")
    # 写入 factor_pool
    conn = sqlite3.connect(DB)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for name in NEW_NAMES:
        r = ic_by_name.get(name) or {}
        icm = r.get("ic_mean")
        if icm is None:
            continue
        st = "gtja_admitted" if name in {c["name"] for c in admitted} else "gtja_candidate"
        conn.execute(
            "INSERT OR REPLACE INTO factor_pool(expr, ic, icir, samples, created, status) "
            "VALUES(?,?,?,?,?,?)",
            (f"{name} :: {FG.GTJA_FACTORS[name][1]}", round(icm, 4),
             round(r.get("icir") or 0, 3), r.get("samples") or 0, now, st))
    conn.commit(); conn.close()
    P("")
    P(f"- factor_pool 已更新（{len(NEW_NAMES)} 条，status=gtja_admitted/gtja_candidate）")
    P(f"- 总用时 {time.time()-t0:.0f}s")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("报告:", REPORT)


if __name__ == "__main__":
    main()