# -*- coding: utf-8 -*-
"""★ B4：ml_scores 前瞻台账 —— 每日记录 top/bottom 各 N 只与 T+1 实现收益

目的：对夜间打分（data/ml_scores.json）做**事前登记、事后兑现**的前瞻监控，
杜绝任何事后回填式的前视：选样在信号日收盘后落账，收益等 T+1 真实出现后补记。

账本：data/ml_scores_ledger.jsonl（纯追加；两类行，均幂等——重复运行不产生重复行）
  {"kind":"pick","date":信号日,"code":...,"bucket":"top|bottom","rank":k,
   "score":...,"asof":该票自身末根bar日,"close_asof":该票末根收盘价,
   "n_scores":当日总分,"generated":时间戳}
  {"kind":"ret","date":信号日,"code":...,"ret_c2c":...,  # close[T+1]/close[asof]-1
   "ret_o2c":...,                                        # close[T+1]/open[T+1]-1
   "gap_td":交易日间隔(1=严格T+1),"realized_at":时间戳}

口径：
- asof 用**该票自身末根 bar**（B2 滞后容忍打分的诚实对照基准），非全局锚日；
- 头条统计只用 gap_td==1（严格次日）样本；含跨停牌样本的口径单列；
- 单日有效样本 ≥8 且两侧桶各 ≥3 才计当日 IC/spread。

IC 报告（自动）：已实现收益的不同信号日 ≥20 时，写出
data/ml_scores_ledger_report.json + docs/reports/ml_scores_forward.md。

自检模式（不触碰真实账本/报告位）：
  python tools/ml_sidecar/scores_ledger.py --selftest-pred 25
  用 market.db ml_pred 的最近 25 个预测截面作为伪台账跑通同一套管线，
  验证幂等/兑现/IC 报告全链路（输出 *_selftest.json，stdout 表格）。

日常调度：tools/ml_scores_ledger_daily.cmd（纯标准库，系统 Python 即可）。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(BASE, "data", "market.db")
SCORES = os.path.join(BASE, "data", "ml_scores.json")
LEDGER = os.path.join(BASE, "data", "ml_scores_ledger.jsonl")
REPORT_JSON = os.path.join(BASE, "data", "ml_scores_ledger_report.json")
REPORT_MD = os.path.join(BASE, "docs", "reports", "ml_scores_forward.md")
MIN_REPORT_DAYS = 20       # ★任务书：满 20 交易日（已实现信号日）出报告
MIN_DAY_SAMPLES = 8        # 单日计 IC 的最少已实现样本
MIN_BUCKET = 3             # 当日每侧桶最少样本


def read_ledger(path):
    """读全部台账行（坏行跳过并计数）。返回 (lines, n_bad)"""
    lines, n_bad = [], 0
    if not os.path.isfile(path):
        return lines, n_bad
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
                if isinstance(o, dict) and o.get("kind") in ("pick", "ret"):
                    lines.append(o)
                else:
                    n_bad += 1
            except Exception:
                n_bad += 1
    return lines, n_bad


def append_lines(path, rows):
    if not rows:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def conn_ro():
    return sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro",
                           uri=True, timeout=30)


def trading_axis(conn):
    ds = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM kline WHERE period='day' ORDER BY date")]
    return {d: i for i, d in enumerate(ds)}


def next_bar(conn, code, after_date):
    """该票自身序列中 after_date 之后的第一根 bar"""
    r = conn.execute(
        "SELECT date, open, close FROM kline WHERE code=? AND period='day' "
        "AND date>? AND close IS NOT NULL ORDER BY date LIMIT 1",
        (code, after_date)).fetchone()
    return r


def close_at(conn, code, date):
    r = conn.execute(
        "SELECT close FROM kline WHERE code=? AND period='day' AND date=? "
        "AND close IS NOT NULL", (code, date)).fetchone()
    return r[0] if r else None


def spearman(pairs):
    """pairs=[(x,y)]；并列平均秩的 Spearman。样本<3 或零方差返回 None"""
    if len(pairs) < 3:
        return None

    def ranks(vals):
        idx = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                rk[idx[k]] = avg
            i = j + 1
        return rk

    xs = ranks([p[0] for p in pairs])
    ys = ranks([p[1] for p in pairs])
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = sum((a - mx) ** 2 for a in xs)
    vy = sum((b - my) ** 2 for b in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def mean(v):
    v = [x for x in v if x == x]
    return sum(v) / len(v) if v else None


# ---------------- 台账分析（真实/自检共用） ----------------

def analyze(lines, strict_gap=True):
    """按 (date,code) 聚合 pick+ret，产出逐日统计与聚合指标。"""
    picks, rets = {}, {}
    for o in lines:
        k = (o.get("date"), o.get("code"))
        if o["kind"] == "pick":
            picks[k] = o
        elif o["kind"] == "ret":
            rets[k] = o
    by_day = {}
    joined = 0
    for k, p in picks.items():
        r = rets.get(k)
        if not r:
            continue
        joined += 1
        if strict_gap and r.get("gap_td") != 1:
            continue
        d = k[0]
        by_day.setdefault(d, []).append({
            "code": k[1], "bucket": p.get("bucket"), "score": p.get("score"),
            "ret_c2c": r.get("ret_c2c"), "ret_o2c": r.get("ret_o2c")})
    days = sorted(by_day)
    daily = []
    for d in days:
        rows = by_day[d]
        if len(rows) < MIN_DAY_SAMPLES:
            continue
        nb = [x for x in rows if x["bucket"] == "bottom"]
        nt = [x for x in rows if x["bucket"] == "top"]
        if len(nt) < MIN_BUCKET or len(nb) < MIN_BUCKET:
            continue
        ic = spearman([(x["score"], x["ret_c2c"]) for x in rows])
        ic_o2c = spearman([(x["score"], x["ret_o2c"]) for x in rows])
        mt = mean([x["ret_c2c"] for x in nt])
        mb = mean([x["ret_c2c"] for x in nb])
        spread = (mt - mb) if (mt is not None and mb is not None) else None
        daily.append({"date": d, "n": len(rows),
                      "ic": round(ic, 4) if ic == ic else None,
                      "ic_o2c": round(ic_o2c, 4) if ic_o2c and ic_o2c == ic_o2c else None,
                      "top_ret_bp": round(mt * 10000, 1),
                      "bottom_ret_bp": round(mb * 10000, 1),
                      "spread_bp": round(spread * 10000, 1)})
    mics = [x["ic"] for x in daily if x["ic"] is not None]
    msp = [x["spread_bp"] for x in daily]
    mu = mean(mics)
    sd = None
    if mics and len(mics) > 1:
        m2 = sum(v for v in mics) / len(mics)
        sd = (sum((v - m2) ** 2 for v in mics) / (len(mics) - 1)) ** 0.5
    agg = {
        "realized_days_used": len(daily),
        "ic_mean": round(mu, 4) if mu is not None else None,
        "icir": round(mu / sd, 3) if mu is not None and sd and sd > 0 else None,
        "ic_positive_day_ratio": round(sum(1 for x in mics if x > 0) / len(mics), 3)
        if mics else None,
        "spread_mean_bp": round(mean(msp), 1) if msp else None,
        "spread_win_ratio": round(sum(1 for x in msp if x > 0) / len(msp), 3)
        if msp else None,
        "top_ret_mean_bp": round(mean([x["top_ret_bp"] for x in daily]), 1)
        if daily else None,
        "bottom_ret_mean_bp": round(mean([x["bottom_ret_bp"] for x in daily]), 1)
        if daily else None,
    }
    monthly = {}
    for x in daily:
        g = monthly.setdefault(x["date"][:7], [])
        g.append(x)
    months = [{"month": mo, "days": len(g),
               "ic_mean": round(mean([y["ic"] for y in g]), 4),
               "spread_bp_mean": round(mean([y["spread_bp"] for y in g]), 1)}
              for mo, g in sorted(monthly.items())]
    return {"joined_picks": joined, "distinct_pick_dates": len(set(k[0] for k in picks)),
            "distinct_realized_dates": len(days), "daily": daily,
            "aggregate": agg, "monthly": months}


def render_md(doc):
    agg = doc["aggregate"]
    L = ["# B4：ml_scores 前瞻台账 IC 报告（自动生成）", "",
         "- 生成: %s ｜ 工具: tools/ml_sidecar/scores_ledger.py（每日 tick，jsonl 追加幂等）"
         % doc["generated_at"],
         "- 口径: 头条仅 gap_td==1（严格 T+1）；单日样本 ≥%d 且两桶各 ≥%d；"
         "IC=spearman(score, ret_c2c)，T+1 实现=close[T+1]/close[asof]-1" % (
             MIN_DAY_SAMPLES, MIN_BUCKET),
         "- 样本: 登记信号日 %d 个，已实现 %d 个（本报告使用 %d 日）；"
         "登记总行 %d、兑现总行 %d" % (
             doc["scope"]["pick_dates_total"], doc["scope"]["realized_dates_total"],
             agg["realized_days_used"], doc["scope"]["pick_lines"],
             doc["scope"]["ret_lines"]),
         "", "## 聚合", "",
         "| 已实现日 | IC 均值 | ICIR | IC>0 占比 | 多空价差均值(bp) | 价差胜率 | top均值(bp) | bottom均值(bp) |",
         "|---|---|---|---|---|---|---|---|"]
    a = agg
    L.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
        a["realized_days_used"], a["ic_mean"], a["icir"], a["ic_positive_day_ratio"],
        a["spread_mean_bp"], a["spread_win_ratio"], a["top_ret_mean_bp"],
        a["bottom_ret_mean_bp"]))
    L += ["", "## 月度", "", "| 月 | 日数 | IC均值 | 价差均值(bp) |", "|---|---|---|---|"]
    for m in doc["monthly"]:
        L.append("| %s | %s | %s | %s |" % (m["month"], m["days"],
                                            m["ic_mean"], m["spread_bp_mean"]))
    tail = doc["daily"][-10:]
    L += ["", "## 最近交易日明细", "",
          "| 日期 | n | IC | IC(o2c) | top(bp) | bottom(bp) | spread(bp) |",
          "|---|---|---|---|---|---|---|"]
    for x in tail:
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            x["date"], x["n"], x["ic"], x["ic_o2c"], x["top_ret_bp"],
            x["bottom_ret_bp"], x["spread_bp"]))
    L += ["", "> 前瞻性质：全部收益在登记之后才落账，无回填与幸存者选择；"
          "报告随台账滚动覆盖更新。", ""]
    return "\n".join(L)


def maybe_report(lines, selftest=False):
    ana_strict = analyze(lines, strict_gap=True)
    n_realized = ana_strict["distinct_realized_dates"]
    if n_realized < MIN_REPORT_DAYS:
        return None, ana_strict
    doc = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "phase": "B4",
           "method": {"ledger": LEDGER, "headline_filter": "gap_td==1",
                      "min_day_samples": MIN_DAY_SAMPLES, "min_bucket": MIN_BUCKET},
           "scope": {"pick_lines": sum(1 for o in lines if o["kind"] == "pick"),
                     "ret_lines": sum(1 for o in lines if o["kind"] == "ret"),
                     "pick_dates_total": ana_strict["distinct_pick_dates"],
                     "realized_dates_total": n_realized},
           "aggregate": ana_strict["aggregate"],
           "monthly": ana_strict["monthly"],
           "daily": ana_strict["daily"]}
    out_json = REPORT_JSON
    if selftest:
        out_json = REPORT_JSON.replace(".json", "_selftest.json")
    with open(out_json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    if not selftest:
        with open(REPORT_MD, "w", encoding="utf-8", newline="\n") as f:
            f.write(render_md(doc))
    return doc, ana_strict


# ---------------- 主流程 ----------------

def do_tick(top_n):
    if not os.path.isfile(SCORES):
        print("ml_scores.json 不存在，退出"); return 0
    d = json.load(open(SCORES, encoding="utf-8"))
    S = d.get("date") or ""
    scores = d.get("scores") or {}
    if not S or not scores:
        print("ml_scores.json 无有效分数，退出"); return 0
    lines, n_bad = read_ledger(LEDGER)
    pick_dates = {o["date"] for o in lines if o["kind"] == "pick"}
    ret_keys = {(o["date"], o["code"]) for o in lines if o["kind"] == "ret"}

    conn = conn_ro()
    try:
        tidx = trading_axis(conn)
        new_lines = []
        if S not in pick_dates:
            items = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            for bucket, seq in (("top", items[:top_n]),
                                ("bottom", items[-top_n:])):
                for i, (code, sc) in enumerate(seq, 1):
                    asof_rows = conn.execute(
                        "SELECT MAX(date) FROM kline WHERE code=? AND period='day'",
                        (code,)).fetchone()[0]
                    cl = close_at(conn, code, asof_rows) if asof_rows else None
                    new_lines.append({"kind": "pick", "date": S, "code": code,
                                      "bucket": bucket, "rank": i, "score": sc,
                                      "asof": asof_rows or "", "close_asof": cl,
                                      "n_scores": len(scores), "generated": ts})
        # 兑现：所有缺 ret 的 pick（t+1 出现后自然补齐；停牌票顺延）
        picks_idx = {(o["date"], o["code"]): o for o in lines}
        for nl in new_lines:
            picks_idx[(nl["date"], nl["code"])] = nl
        todo = [(k, p) for k, p in picks_idx.items() if k not in ret_keys]
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        n_real_now = 0
        for (pd_, code), p in sorted(todo):
            asof = p.get("asof") or ""
            ca = p.get("close_asof")
            if not asof or ca in (None, 0):
                continue
            nb = next_bar(conn, code, asof)
            if not nb:
                continue                                   # 尚无 T+1，继续挂账
            nd, no, nc = str(nb[0]), nb[1], nb[2]
            if not no or not nc:
                continue
            gi = tidx.get(nd)
            ai = tidx.get(asof)
            gap = (gi - ai) if (gi is not None and ai is not None) else None
            new_lines.append({"kind": "ret", "date": pd_, "code": code,
                              "ret_c2c": round(nc / ca - 1.0, 6),
                              "ret_o2c": round(nc / no - 1.0, 6),
                              "gap_td": gap, "realized_at": ts})
            n_real_now += 1
    finally:
        conn.close()
    append_lines(LEDGER, new_lines)
    lines, _ = read_ledger(LEDGER)
    doc, ana = maybe_report(lines, selftest=False)
    print("tick 完成: 信号日=%s 总分=%d 追加 %d 行（新登记 %d，新兑现 %d，坏行累计 %d）"
          % (S, len(scores), len(new_lines),
             sum(1 for o in new_lines if o["kind"] == "pick"),
             sum(1 for o in new_lines if o["kind"] == "ret"), n_bad))
    print("进度: 已实现信号日 %d/%d（用时 %d 个登记日）"
          % (ana["distinct_realized_dates"], MIN_REPORT_DAYS,
             ana["distinct_pick_dates"]))
    if doc:
        print("★ 满 %d 个已实现交易日：IC 报告已写出\n  %s\n  %s"
              % (MIN_REPORT_DAYS, REPORT_JSON, REPORT_MD))
        print("聚合:", json.dumps(doc["aggregate"], ensure_ascii=False))
    return 0


def do_selftest_pred(n_days, top_n):
    """用 ml_pred 最近 n_days 个截面作伪台账，端到端验证管线（不碰真实文件）。"""
    conn = conn_ro()
    try:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM ml_pred ORDER BY date DESC LIMIT ?", (n_days,))]
        lines = []
        tidx = trading_axis(conn)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for dt in sorted(dates):
            rows = conn.execute(
                "SELECT code, score FROM ml_pred WHERE date=? ORDER BY score DESC",
                (dt,)).fetchall()
            for bucket, seq in (("top", rows[:top_n]), ("bottom", rows[-top_n:])):
                for i, (code, sc) in enumerate(seq, 1):
                    ca = close_at(conn, code, dt)
                    if ca in (None, 0):
                        continue
                    lines.append({"kind": "pick", "date": dt, "code": code,
                                  "bucket": bucket, "rank": i, "score": sc,
                                  "asof": dt, "close_asof": ca,
                                  "n_scores": len(rows), "generated": ts})
        ret_lines = []                                        # 先建齐再并入，避免遍历中追加
        for o in [x for x in lines if x["kind"] == "pick"]:
            nb = next_bar(conn, o["code"], o["asof"])
            if not nb or not nb[1] or not nb[2]:
                continue
            gi, ai = tidx.get(str(nb[0])), tidx.get(o["asof"])
            ret_lines.append({"kind": "ret", "date": o["date"], "code": o["code"],
                              "ret_c2c": round(nb[2] / o["close_asof"] - 1.0, 6),
                              "ret_o2c": round(nb[2] / nb[1] - 1.0, 6),
                              "gap_td": (gi - ai) if (gi is not None and ai is not None) else None,
                              "realized_at": ts})
        lines.extend(ret_lines)
    finally:
        conn.close()
    doc, ana = maybe_report(lines, selftest=True)
    print("[selftest-ml_pred] 登记 %d 行 / 兑现 %d 行；不同实现日 %d；join %d"
          % (sum(1 for o in lines if o["kind"] == "pick"),
             sum(1 for o in lines if o["kind"] == "ret"),
             ana["distinct_realized_dates"], ana["joined_picks"]))
    if doc:
        print("聚合:", json.dumps(doc["aggregate"], ensure_ascii=False))
        print("最近 5 日:")
        for x in doc["daily"][-5:]:
            print("  ", json.dumps(x, ensure_ascii=False))
        print("自检报告: %s" % REPORT_JSON.replace(".json", "_selftest.json"))
    else:
        print("自检未达 %d 实现日，管线未出报告" % MIN_REPORT_DAYS)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--selftest-pred", type=int, default=0,
                    help="用 ml_pred 历史 N 个截面端到端自检（不写真实台账/报告）")
    a = ap.parse_args()
    if a.selftest_pred:
        return do_selftest_pred(a.selftest_pred, a.top_n)
    return do_tick(a.top_n)


if __name__ == "__main__":
    sys.exit(main())
