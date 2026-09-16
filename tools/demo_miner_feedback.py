# -*- coding: utf-8 -*-
"""★ 阶段5 LLM 因子挖掘闭环升级演示：3 轮挖掘（开关全开，provider=local 离线可测）
展示：失败原因回注下一轮提示 / 相关性>0.8 拒绝入池 / walk-forward 稳定性门禁
用法：python -m tools.demo_miner_feedback
输出：docs/reports/phase5_miner_feedback.md
"""
import json
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app.config as C
from app import factor_miner as fm

DB = os.path.join(BASE, "data", "market.db")
REPORT = os.path.join(BASE, "docs", "reports", "phase5_miner_feedback.md")
ROUNDS = 3
N_PER_ROUND = 5

L = []
def P(s=""):
    print(s); L.append(s)


def load_universe(limit=400):
    conn = sqlite3.connect(DB)
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM kline WHERE period='day' GROUP BY code HAVING COUNT(*)>=100 "
        "ORDER BY code LIMIT ?", (limit,))]
    kbc = {}
    for c in codes:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,amount FROM kline "
            "WHERE code=? AND period='day' ORDER BY date", (c,)).fetchall()
        kl = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
               "close": r[4], "volume": r[5], "amount": r[6]} for r in rows
              if r[0] and r[4]]
        if len(kl) >= 100:
            kbc[c] = kl
    conn.close()
    return kbc


def main():
    t0 = time.time()
    kbc = load_universe()
    P("# 阶段5 LLM 因子挖掘闭环升级演示报告")
    P("")
    P(f"- 股票池: {len(kbc)} 只｜每轮候选: {N_PER_ROUND}｜轮数: {ROUNDS}｜provider: local（离线模板，无 LLM 也可验证管线）")
    # 开启三道闸（仅本演示进程内有效，不修改 config 文件默认值）
    C.MINER_FEEDBACK_ENABLED = True
    C.MINER_CORR_REJECT_ENABLED = True
    C.MINER_WF_ENABLED = True

    P(f"- 开关（演示进程内开启）: MINER_FEEDBACK_ENABLED={C.MINER_FEEDBACK_ENABLED} "
      f"MINER_CORR_REJECT_ENABLED={C.MINER_CORR_REJECT_ENABLED} MINER_WF_ENABLED={C.MINER_WF_ENABLED} "
      f"| IC门槛|{C.MINER_IC_MIN}| 相关性阈值{C.MINER_CORR_MAX} WF折数{C.MINER_WF_FOLDS} 需同向{C.MINER_WF_MIN_STABLE}折")
    P("")

    feedback = []
    for rnd in range(1, ROUNDS + 1):
        P(f"## 第 {rnd} 轮")
        res = fm.run_mining_round(kbc, n=N_PER_ROUND, horizon=5,
                                  provider="local", feedback=feedback)
        # 展示回注提示（第2轮起 feedback 非空）
        if res.get("error"):
            P(f"- 错误: {res['error']}")
            continue
        P(f"- 回注提示（进入下一轮 prompt 的失败原因，第{rnd}轮新增 {len(res['feedback']) - len(feedback)} 条）：")
        for f in res["feedback"]:
            P(f"    · {f['expr']} :: {f['reason']}")
        P(f"- 入池因子（过三道闸）: {len(res['candidates'])}")
        for c in res["candidates"]:
            P(f"    ✅ {c['name']}: IC={c['ic_mean']:+.4f} ICIR={c['icir']:+.2f} "
              f"WF={c.get('wf_folds')}")
        P(f"- 拒绝 {len(res['rejected'])} 条：")
        for rj in res["rejected"][:10]:
            P(f"    ❌ {rj['expr']} :: {rj['reason']}")
        feedback = res["feedback"]
        P("")
    P("## 结论")
    P("- 失败原因（IC不足/相关重叠/WF不稳定）随轮次累积并回注入下一轮生成提示 → 挖掘闭环成立")
    P("- 三道闸默认关闭（config 默认 False），保持原有挖掘行为不变；开启后才有拒绝/回注/门禁")
    P(f"- 总用时 {time.time()-t0:.0f}s")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("报告:", REPORT)


if __name__ == "__main__":
    main()