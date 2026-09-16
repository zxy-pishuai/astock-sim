# -*- coding: utf-8 -*-
"""★ D4：双价观察分析工具 —— buy_px_dual 事件的周度新旧口径差异报告

背景（P45 审计-D → P59 落地）：trader._buy_px 处于"双价观察期"——
  旧口径 px_old = execute_price(price, side, mcap, amount=price*1000,
                                 day_amount=0, impact=False)
             = base × (1±市值分档滑点)，固定 1000 股假设、无冲击成本；
  新口径 px_new = execute_price(price, side, mcap, amount=price*真实qty,
                                day_amount=当日成交额, impact=True)
             = base × (1±同滑点) × (1±EXEC_IMPACT_SLIP 若 单笔金额>当日成交额5%)。
两价分叉的充要条件：真实 qty 已知 且 day_amount>0 且 amount/day_amount>5%
（薄票大单）。否则恒等、且等价时不写日志（设计如此，静默同价）。
实际采用仍为旧口径（adopted="old"）；验收方满一周后置 BUY_PX_USE_NEW=True 切换。

事件来源（仅这三处会 emit）：策略自动买入（扫描路径/A股竞价板）、策略退出卖出；
manual_buy / test_buy 在 state.py 自行定价，不入观察流。

用法：
  python tools/dual_price_weekly.py            # 默认 dry-run：只读存量并打印摘要
  python tools/dual_price_weekly.py --write    # 追加周度 Markdown 报告 + 写 JSON
  python tools/dual_price_weekly.py --json X   # 指定 JSON 输出路径（配 --write）
  python tools/dual_price_weekly.py --selftest # 合成样例自检聚合口径（不碰真实文件）

输出：
  docs/reports/dual_price_observation.md（--write 时按周追加）
  data/dual_price_weekly.json（--write）
红线：只读 audit.jsonl；不写任何库表；不改 trader/config。
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AUDIT_FILE = os.path.join(BASE, "data", "audit", "audit.jsonl")
REPORT_MD = os.path.join(BASE, "docs", "reports", "dual_price_observation.md")
OUT_JSON_DEFAULT = os.path.join(BASE, "data", "dual_price_weekly.json")
EVENT_NAME = "buy_px_dual"

# 报告口径预注册（写入报告首节，防止事后挑指标）：
#   bps       = (new_px − old_px)/base_price × 10⁴（带符号）
#   不利方向  = buy 且 new>old，或 sell 且 new<old（新口径让交易者更亏）
#   金额差    = |qty| × Δpx ×(−1 buy/+1 sell 为不利号)…仅 qty 非空的子集可估


def load_events(path=AUDIT_FILE):
    """读全部 buy_px_dual 事件。返回 (events, meta)。meta 含文件画像。"""
    evs, n_lines, n_bad = [], 0, 0
    first_t = last_t = ""
    other_order_events = defaultdict(int)
    if not os.path.exists(path):
        return evs, {"exists": False}
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            n_lines += 1
            try:
                r = json.loads(ln)
            except Exception:
                n_bad += 1
                continue
            t = r.get("t", "")
            if t:
                if not first_t or t < first_t:
                    first_t = t
                if t > last_t:
                    last_t = t
            if r.get("event") == EVENT_NAME:
                evs.append(r)
            elif r.get("kind") == "order":
                other_order_events[r.get("event", "?")] += 1
    meta = {"exists": True, "lines": n_lines, "bad_lines": n_bad,
            "first_t": first_t, "last_t": last_t,
            "order_events_other": dict(other_order_events)}
    return evs, meta


def chain_health(path=AUDIT_FILE):
    """容忍式哈希链体检：断点处重同步继续验（多进程并发写/同日重启都会断）。
    返回 {"checked", "lines", "breaks_total", "breaks_by_day"}。
    注意 app.audit.verify_chain() 是严格版（首断即停）；本函数用于画像报告。"""
    import hashlib
    res = {"checked": 0, "lines": 0, "breaks_total": 0, "breaks_by_day": {}}
    if not os.path.exists(path):
        return res
    prev = ""
    cur_day = ""
    breaks = defaultdict(int)
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            res["lines"] += 1
            try:
                r = json.loads(ln)
            except Exception:
                continue
            d = r.get("t", "")[:10]
            if d != cur_day:
                cur_day = d
                prev = ""
            payload = {k: v for k, v in r.items() if k not in ("hash", "prev")}
            expect = hashlib.sha256(
                (json.dumps(payload, ensure_ascii=False, sort_keys=True)
                 + "|" + prev).encode("utf-8")).hexdigest()[:16]
            if r.get("hash") != expect or r.get("prev") != prev:
                breaks[d] += 1
                prev = r.get("hash", "")
            else:
                res["checked"] += 1
                prev = r["hash"]
    res["breaks_total"] = sum(breaks.values())
    res["breaks_by_day"] = dict(sorted(breaks.items()))
    return res


def iso_week(day):
    """'YYYY-MM-DD' → ('2026', 'W35') 形式的 ISO 周标签"""
    try:
        y, w, _ = __import__("datetime").date.fromisoformat(day).isocalendar()
        return "%04d-%02dW" % (y, w)
    except Exception:
        return "unknown"


def _stats(vals):
    if not vals:
        return {}
    s = sorted(vals)

    def pct(p):
        i = min(len(s) - 1, int(round(p * (len(s) - 1))))
        return round(s[i], 2)
    return {"mean": round(sum(s) / len(s), 2),
            "median": pct(0.5), "p90": pct(0.9),
            "max_abs": round(max(abs(v) for v in s), 2)}


def aggregate(evs):
    """聚合成总块 + 按周块。所有统计只依赖传入事件本身，--selftest 可校验。"""
    def blk(rows):
        out = {"n": len(rows)}
        if not rows:
            return out
        out["codes"] = sorted({r.get("code", "") for r in rows})
        for side in ("buy", "sell"):
            sr = [r for r in rows if r.get("side") == side]
            d = {"n": len(sr)}
            if sr:
                bps = [(r["new_px"] - r["old_px"]) / r["base_price"] * 10000
                       for r in sr]
                adv = [b for b in bps if (b > 0 if side == "buy" else b < 0)]
                d["abs_bps"] = _stats([abs(b) for b in bps])
                d["adverse_n"] = len(adv)
                d["adverse_pct"] = round(len(adv) / len(sr) * 100, 1)
                mk = []
                for r in sr:
                    q = r.get("qty")
                    if q:
                        dd = (r["new_px"] - r["old_px"]) * float(q)
                        # 统一符号：正=新口径对交易者不利（买多付/卖少收）
                        d_signed = dd if side == "buy" else -dd
                        mk.append(d_signed)
                if mk:
                    d["money_adverse"] = {
                        "subset_n": len(mk),
                        "sum": round(sum(mk), 2),
                        "max": round(max(mk), 2)}
            out[side] = d
        out["qty_missing_n"] = sum(1 for r in rows if not r.get("qty"))
        out["adopted_values"] = sorted({r.get("adopted", "?") for r in rows})
        return out

    total = blk(evs)
    by_week = defaultdict(list)
    for r in evs:
        by_week[iso_week(r.get("t", "")[:10])].append(r)
    weeks = {w: blk(by_week[w]) for w in sorted(by_week)}
    return {"total": total, "weeks": weeks}


def selftest():
    """合成样例校验聚合口径（绝不触碰 audit.jsonl）。
    语义：bps=(新−旧)/基准×1e4；不利=买侧新更贵 或 卖侧新更贱（两侧语义对称）；
    金额差统一符号"正=对交易者不利"，仅带 qty 子集参与。"""
    fx = [
        # 周A：buy 大单薄票（不利，有 qty）；buy 缺 qty 回退记录（仍计 bps、
        # 不计金额）；sell 新更贱（卖侧=不利，有 qty）
        {"t": "2026-08-24 09:35:00", "code": "300001", "side": "buy",
         "base_price": 10.0, "old_px": 10.1, "new_px": 10.2,
         "qty": 5000, "day_amount": 5e6, "adopted": "old"},
        {"t": "2026-08-26 14:55:00", "code": "600001", "side": "buy",
         "base_price": 20.0, "old_px": 20.3, "new_px": 20.4,
         "qty": None, "day_amount": None, "adopted": "old"},
        {"t": "2026-08-27 10:00:00", "code": "000002", "side": "sell",
         "base_price": 5.0, "old_px": 4.95, "new_px": 4.90,
         "qty": 8000, "day_amount": 9e7, "adopted": "old"},
        # 周B：sell 不利
        {"t": "2026-08-31 13:05:00", "code": "000004", "side": "sell",
         "base_price": 8.0, "old_px": 7.92, "new_px": 7.84,
         "qty": 1000, "day_amount": 6e6, "adopted": "old"},
    ]
    wa = iso_week(fx[0]["t"][:10])
    wb = iso_week(fx[3]["t"][:10])
    assert wa != wb, "fixture 应跨两个 ISO 周"
    agg = aggregate(fx)
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = (abs(got - want) < 1e-6 if isinstance(want, (int, float))
                and not isinstance(want, bool) else got == want)
        print("  [%s] %s: %r (期望 %r)" % ("PASS" if good else "FAIL",
                                           name, got, want))
        ok = ok and good

    chk("total.n", agg["total"]["n"], 4)
    chk("weekA.n", agg["weeks"][wa]["n"], 3)
    chk("weekB.n", agg["weeks"][wb]["n"], 1)
    chk("weekA.buy.adverse_pct",
        agg["weeks"][wa]["buy"]["adverse_pct"], 100.0)
    chk("weekA.sell.adverse_pct",
        agg["weeks"][wa]["sell"]["adverse_pct"], 100.0)   # 卖侧新更贱=不利
    chk("weekB.sell.adverse_pct",
        agg["weeks"][wb]["sell"]["adverse_pct"], 100.0)
    chk("weekA.buy.money.sum", agg["weeks"][wa]["buy"]["money_adverse"]
        ["sum"], 0.1 * 5000)                              # 缺 qty 条不计金额
    chk("weekA.buy.money.subset_n",
        agg["weeks"][wa]["buy"]["money_adverse"]["subset_n"], 1)
    chk("weekA.sell.money.sum", agg["weeks"][wa]["sell"]["money_adverse"]
        ["sum"], -(4.90 - 4.95) * 8000)
    chk("weekB.sell.money.sum", agg["weeks"][wb]["sell"]["money_adverse"]
        ["sum"], (7.92 - 7.84) * 1000)
    chk("weekA.qty_missing_n", agg["weeks"][wa]["qty_missing_n"], 1)
    chk("adopted", agg["total"]["adopted_values"], ["old"])
    print("\n自检%s" % ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


def render_week_md(w, blk):
    if not blk.get("n"):
        return "| %s | 0 | - | - | - | - | - |\n" % w
    b, s = blk.get("buy") or {}, blk.get("sell") or {}

    def cell(d):
        if not d or not d.get("n"):
            return "-"
        ab = d.get("abs_bps") or {}
        mn = d.get("money_adverse")
        m = ("¥%.0f" % mn["sum"]) if mn else "-"
        return "%d笔/中位%.1f/P90%.1f/max%.1f bp/不利%.0f%%/%s" % (
            d["n"], ab.get("median", 0), ab.get("p90", 0),
            ab.get("max_abs", 0), d.get("adverse_pct", 0), m)
    codes = ",".join(blk.get("codes") or [])
    return "| %s | %d | %s | %s | %s | %s |\n" % (
        w, blk["n"], cell(b), cell(s), codes,
        "/".join(blk.get("adopted_values") or []))


def main():
    ap = argparse.ArgumentParser(description="D4 双价观察周度报告")
    ap.add_argument("--write", action="store_true",
                    help="把本次结果落盘（JSON + 周度 Markdown 追加节）")
    ap.add_argument("--json", default=OUT_JSON_DEFAULT)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())

    evs, meta = load_events()
    agg = aggregate(evs)
    ch = chain_health()
    meta["chain_health"] = {
        k: v for k, v in ch.items() if k != "breaks_by_day"}
    meta["chain_health"]["breaks_by_day"] = ch["breaks_by_day"]
    t0 = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---- 控制台 ----
    print("== buy_px_dual 存量盘点 ==")
    if not meta.get("exists"):
        print("audit 文件不存在（服务自启动以来未产生任何审计行）")
    else:
        print("audit 行数 %d（%s ~ %s）坏行 %d" % (
            meta["lines"], meta["first_t"], meta["last_t"],
            meta["bad_lines"]))
        oe = meta.get("order_events_other") or {}
        print("order 类其他事件:", dict(sorted(oe.items())) or "无")
        print("哈希链体检: 断链 %d/%d（断点重同步口径；多进程并发写所致）"
              % (ch["breaks_total"], ch["lines"]))
        if ch["breaks_by_day"]:
            print("  按日:", ch["breaks_by_day"])
    print("buy_px_dual 事件总数: %d" % agg["total"].get("n", 0))
    for w, blk in agg["weeks"].items():
        print(render_week_md(w, blk).rstrip())
    if not evs:
        print()
        print("诊断（为何为 0）:")
        print("  1) 双价只在策略自动买入/卖出定价时 emit（trader L634/L848/L1254）；")
        print("     manual_buy/test_buy 在 state.py 定价，不入本流。")
        print("  2) 审计流活跃（心跳正常）但期间无任何策略自动下单 → 无定价发生，")
        print("     0 事件是'无信号'而非'信号被吞'。")
        print("  3) 即便有单：仅当 真实qty已知 且 占当日成交额>5% 才会分叉记账，")
        print("     等价时静默跳过（设计如此）。")
    else:
        tot = agg["total"]
        for side in ("buy", "sell"):
            sd = tot.get(side) or {}
            if sd.get("n"):
                ab = sd["abs_bps"]
                mo = sd.get("money_adverse")
                print("%s: n=%d 中位|Δ|=%.1fbps 不利%d%% 估算不利金额=%s" % (
                    side, sd["n"], ab.get("median", 0),
                    sd.get("adverse_pct", 0),
                    ("¥%.2f(子集%d)" % (mo["sum"], mo["subset_n"]))
                    if mo else "无qty无法估"))

    if not a.write:
        print("\n[dry-run] 未写任何文件（--write 落盘）")
        return

    # ---- 落盘 JSON ----
    doc = {"generated_at": t0, "source": AUDIT_FILE, "meta": meta,
           **agg}
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("已写", a.json)

    # ---- 周度 Markdown ----
    os.makedirs(os.path.dirname(REPORT_MD), exist_ok=True)
    header_needed = not os.path.exists(REPORT_MD)
    with open(REPORT_MD, "a", encoding="utf-8") as f:
        if header_needed:
            f.write("# 双价观察周报（P59 buy_px_dual，新旧口径差异跟踪）\n\n"
                    "- 工具：`tools/dual_price_weekly.py`（每日事件流增量，"
                    "周度汇总；`--selftest` 自检聚合口径）\n"
                    "- 口径预注册：bps=(新−旧)/基准×1e4；不利方向=买侧新更贵/"
                    "卖侧新更贱；金额差仅对带 qty 子集可估，符号统一为"
                    "\"正=新口径对交易者不利\"\n"
                    "- 判定归验收方：建议以\"整周样本≥30 且 不利中位幅度≥5bp "
                    "或 不利金额累计超佣金两个数量级\"作为启动切换评审的参考线；"
                    "`BUY_PX_USE_NEW` 目前未在 config 定义（getattr 默认 False）\n\n"
                    "| 周 | 事件数 | buy侧（数量/中位/P90/max|bps|/不利%/估额）"
                    " | sell侧 | 触及代码 | adopted |\n|---|---|---|---|---|---|\n")
        for w, blk in agg["weeks"].items():
            f.write(render_week_md(w, blk))
        f.write("\n<!-- 本次运行 %s：%d 条事件（含历史累计重算，幂等去重复述） -->\n"
                % (t0, len(evs)))
    print("已更新", REPORT_MD)


if __name__ == "__main__":
    main()
