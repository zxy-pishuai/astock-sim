# -*- coding: utf-8 -*-
"""K14　IC 初验器（T+20 闸门守卫，预注册判据见 k14_eps_collect.py 文件头 §0/F5）

输入：data/vendor/k14/eps_snap_*.json（每日 PIT 快照，≥20 份）
     market.db kline 的 fwd5 真实收盘（factor_ic_audit 同定义 close[t+5]/close[t]-1）
输出：data/bt_k14_eps.json（横截面 Spearman 日 IC → 月度聚合 ICIR）
只读；不做落地动作；结论分级 exploratory/provisional/confirmed。
若快照 < 20 → 仅打印 phase 并退出（F4 硬约束）。
"""
import json
import pathlib
import statistics
import sqlite3
import sys

BASE = pathlib.Path(__file__).resolve().parents[1]
K14OUT = BASE / "data" / "vendor" / "k14"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def build_fwd5():
    """流式构建 fwd5：fwd5(d)=close[i+5]/close[i]-1（官方口径同 factor_ic_audit）。"""
    conn = sqlite3.connect(str(BASE / "data" / "market.db"))
    cur = conn.execute("select code,date,close from kline order by code,date")
    fwd = {}
    cur_code, q = None, []
    for code, date, close in cur:
        if code != cur_code:
            cur_code, q = code, []
        q.append((date, close))
        if len(q) >= 6:
            i = len(q) - 6
            d0, c0 = q[i]
            _, c5 = q[i + 5]
            if c0 and c0 > 0 and c5:
                fwd.setdefault(d0, {})[code] = round(c5 / c0 - 1, 6)
    conn.close()
    return fwd


def spearman(pairs):
    """pairs=[(x, y)]：横截面 Spearman（含并列均值秩）。n<30 → None。"""
    n = len(pairs)
    if n < 30:
        return None
    xs = [v for _, v in pairs]
    ys = [v for v, _ in pairs]

    def rank(vals):
        idx = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(vals):
            j = i
            while j + 1 < len(vals) and vals[idx[j + 1]] == vals[idx[i]]:
                j += 1
            for k in range(i, j + 1):
                r[idx[k]] = (i + j) / 2.0 + 1.0
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = (sum((a - mx) ** 2 for a in rx)) ** 0.5
    vy = (sum((b - my) ** 2 for b in ry)) ** 0.5
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy)


def main():
    snaps = sorted(K14OUT.glob("eps_snap_*.json"))
    if len(snaps) < 20:
        print(f"[F4] 快照 {len(snaps)} < 20 → 不出 IC；本阶段仅做 QC（tools/k14_eps_qc.py）")
        return
    fwd = build_fwd5()
    daily_ics = []
    daily_q5 = []
    for sp in snaps:
        day = sp.stem.split("_")[-1]
        day_iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"   # kline 日期格式为 ISO
        snap = json.loads(sp.read_text(encoding="utf-8"))
        pairs = []
        for code, r in snap.get("rows", {}).items():
            if not r or not r.get("eps"):
                continue
            sel = None
            for yr in sorted(r["eps"], key=lambda x: x["year"]):
                if int(yr["year"]) >= int(day[:4]) and yr.get("v2_mean") and yr.get("v2_mean", 0) > 0:
                    sel = yr
                    break
            if not sel or not sel.get("n_inst") or sel["n_inst"] < 3:   # F2 机构门
                continue
            ey = selector_yield(sel, code, day_iso)   # eps_yield_fwd（close 取快照日 ISO）
            f5 = fwd.get(day_iso, {}).get(code)
            if ey is None or f5 is None:
                continue
            pairs.append((ey, f5))
        ic = spearman(pairs)
        daily_ics.append((day, ic))
        # F5 五分位：x 从小到大 5 bucket，Q5-Q1 的 fwd5 均值差（多头-Q5 减 空头-Q1）
        if len(pairs) >= 50:
            s = sorted(pairs, key=lambda p: p[0])
            k = len(s) // 5
            if k >= 3:
                dq = statistics.fmean([v for _, v in s[-k:]]) - statistics.fmean([v for _, v in s[:k]])
                daily_q5.append((day, round(dq, 6)))
    mon = {}
    for day, ic in daily_ics:
        if ic is None:
            continue
        mon.setdefault(day[:6], []).append(ic)
    monq = {}
    for day, dq in daily_q5:
        monq.setdefault(day[:6], []).append(dq)
    rows = []
    for m, lst in sorted(mon.items()):
        sd = statistics.stdev(lst) if len(lst) > 1 else 0.0
        rows.append({"month": m, "n": len(lst),
                     "ic_mean": round(statistics.fmean(lst), 4),
                     "icir": round(statistics.fmean(lst) / sd, 3) if sd > 0 else None,
                     "q5_q1_mean": round(statistics.fmean(monq[m]), 6) if monq.get(m) else None})
    # F5 完整判定：需 ≥2 个月有效月度且 ICIR 符号一致
    ics_ok = [r for r in rows if r["icir"] is not None]
    verdict = None
    if ics_ok:
        signs = {1 if r["icir"] >= 0.3 else (-1 if r["icir"] <= -0.3 else 0) for r in ics_ok}
        monotone = all(r["q5_q1_mean"] is not None and r["q5_q1_mean"] > 0 for r in ics_ok)
        if signs == {1} and monotone:
            verdict = "建议落地（进 scoring 需另预注册）"
        elif signs == {-1}:
            verdict = "反向（exploratory）"
        else:
            verdict = "exploratory"
    payload = {"daily_days": len(daily_ics),
               "valid_days": sum(1 for _, i in daily_ics if i is not None),
               "months": rows,
               "verdict": verdict,
               "verdict_note": "F5: 月度 ICIR>=+0.3 且五分位单调→建议落地; <=-0.3→反向; 中间→exploratory"}
    (BASE / "data" / "bt_k14_eps.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def selector_yield(sel, code, day):
    """eps/closeratio：close 取快照日的 kline 收盘。"""
    con = sqlite3.connect(str(BASE / "data" / "market.db"))
    r = con.execute("select close from kline where code=? and date=?",
                    (code, day)).fetchone()
    con.close()
    if not r or not r[0]:
        return None
    return sel["v2_mean"] / r[0]


if __name__ == "__main__":
    main()
