# -*- coding: utf-8 -*-
"""召回评估：若评分池按 amount 粗筛到 top-N，会漏掉哪些最终高分股？
复刻 scan_once 候选生成与评分，记录 (amount排名, score)。"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import config as C
from app import datafeed as df
from app import scoring as sc
from app.trader import TradingEngine

eng = TradingEngine()
stocks = df.fetch_all_stocks()

candidates = []
for code, q in stocks.items():
    price = q.get("price", 0) or 0
    if price < 5 or price > 150:
        continue
    pct = q.get("pct_chg", 0) or 0
    if pct >= C.LIMIT_UP_PCT or pct <= C.LIMIT_DOWN_PCT:
        continue
    if (q.get("amount", 0) or 0) < 8e7:
        continue
    candidates.append((code, q))
candidates.sort(key=lambda x: x[1].get("amount", 0), reverse=True)
candidates = candidates[:250]
print(f"候选: {len(candidates)}")

scored = []
t0 = time.time()
with ThreadPoolExecutor(max_workers=C.PARALLEL_WORKERS) as ex:
    futs = {ex.submit(eng._score_one, c, q): (i, c) for i, (c, q) in enumerate(candidates)}
    res = {futs[f][0]: f.result() for f in as_completed(futs)}
t1 = time.time()
print(f"精评 250 只: {t1 - t0:.2f}s")

rows = []
for i, (c, q) in enumerate(candidates):
    r = res.get(i)
    rows.append({"rank": i, "code": c, "score": r["score"] if r else 0, "ok": bool(r)})
rows.sort(key=lambda x: -x["score"])
print("全池高分前 12:")
for r in rows[:12]:
    print(f"  score={r['score']:5.1f} amount排名={r['rank']+1:4d} {r['code']}")

for N in (60, 80, 100, 150):
    topN = {r["code"] for r in rows if r["rank"] < N}
    miss = [r for r in rows[:6] if r["code"] not in topN]
    print(f"top{N} 粗筛：前6高分漏掉 {len(miss)} 只 -> {[(m['code'], m['score']) for m in miss]}")

# ---- quote 强度粗筛分（零网络，全部来自已缓存行情）----
def rough(q):
    pct = q.get("pct_chg", 0) or 0
    vr = q.get("vol_ratio", 0) or 0
    to = q.get("turnover", 0) or 0
    am = q.get("amount", 0) or 0
    s = pct + min(5, vr) * 2 + min(20, to) * 0.8 + min(3, (am / 1e7) ** 0.5) * 2
    return s

qmap = {c: q for c, q in candidates}
for N in (80, 100, 120, 150):
    # 粗筛池 = 强度分 top N（含 ties）
    ranked = sorted(candidates, key=lambda x: -rough(x[1]))[:N]
    pool = {c for c, _ in ranked}
    miss = [r for r in rows[:6] if r["code"] not in pool]
    print(f"强度粗筛 top{N}：前6高分漏掉 {len(miss)} 只 -> {[(m['code'], m['score']) for m in miss]}")

# 两层池：强度 top100 + (今日平淡 pct<1且量比<1.5 的按金额 top20)
for N in (80, 100):
    strong = sorted(candidates, key=lambda x: -rough(x[1]))[:N]
    pool = {c for c, _ in strong}
    quiet = [(c, q) for c, q in candidates
             if (q.get("pct_chg", 0) or 0) < 1 and (q.get("vol_ratio", 0) or 0) < 1.5
             and c not in pool]
    quiet.sort(key=lambda x: -(x[1].get("amount", 0) or 0))
    pool |= {c for c, _ in quiet[:20]}
    miss = [r for r in rows[:6] if r["code"] not in pool]
    print(f"两层池(强{N}+静20)：前6高分漏掉 {len(miss)} 只 -> {[(m['code'], m['score']) for m in miss]} 池大小={len(pool)}")