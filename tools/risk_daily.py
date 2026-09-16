# -*- coding: utf-8 -*-
"""风控观察日报 —— 冷却暂停期间的配套眼睛。

只读工具：仅读取 data/account.json、data/audit/audit.jsonl、data/risk_state.json、
data/market.db(sqlite ro) 与 app/config 常量，绝不调用下单/改账路径，
除本报告文件外不写任何东西（data/reports/risk_daily/）。

每日输出：
  ① 资金概览（现金/总资产/昨收估值/当日盈亏% 相对熔断线的位置）
  ② 回撤（自初始资金起的现金流重构净值曲线，标注估算口径）
  ③ 连亏计数（account 逐笔已实现口径 vs 引擎持久化口径并列展示）
  ④ 持仓快照（数量/成本/昨收市值/浮盈亏%）
  ⑤ 当日买卖流水（来自 account.trades，与 audit 当日 WARN 事件摘录对照）

告警（写入报告并置 exit=2，供计划任务/人工一眼识别）：
  - 当日盈亏 <= APPROACH_FACTOR * RISK_DAILY_LOSS_TRIGGER → ⚠ 接近熔断线
  - 当日盈亏 <= RISK_DAILY_LOSS_TRIGGER                    → 🚨 击穿熔断线（并与 breaker_on 对比）
  - 冷却暂停生效期间固定横幅提示"拦截已停用，本报告为唯一风控观察面"

用法：
  python tools/risk_daily.py              # 今天
  python tools/risk_daily.py --date 2026-08-26
"""
import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C   # noqa: E402  仅常量；config 只 import os，无副作用

DATA_DIR = C.DATA_DIR
REPORT_DIR = os.path.join(DATA_DIR, "reports", "risk_daily")
APPROACH_FACTOR = 0.8        # 日亏达到熔断线幅度的 80% 即预警告警

TRIGGER = getattr(C, "RISK_DAILY_LOSS_TRIGGER", -0.05)
SUSPEND_UNTIL = getattr(C, "RISK_COOLDOWN_SUSPEND_UNTIL", "")


def _load_json(path, retries=3):
    """读到合法 JSON 为止（服务可能正在写）。只读。"""
    for i in range(retries):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(0.4 * (i + 1))
    return None


def _load_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return out


def _ro_db():
    p = os.path.join(DATA_DIR, "market.db")
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=15)


def _closes_series(codes):
    """{code: [(date, close) 升序]}，一次性取齐，避免逐票开连接。"""
    if not codes:
        return {}
    conn = _ro_db()
    try:
        ph = ",".join("?" * len(codes))
        rows = conn.execute(
            "SELECT code, date, close FROM kline WHERE period='day' "
            "AND length(code)=6 AND code IN (%s)" % ph,
            tuple(codes)).fetchall()
        d = {}
        for c, dt, cl in rows:
            if cl and float(cl) > 0:
                d.setdefault(c, []).append((dt, float(cl)))
        return {c: sorted(v) for c, v in d.items()}
    finally:
        conn.close()


def _close_asof(series, upto):
    best = None
    for dt, cl in series:
        if dt <= upto:
            best = cl
        else:
            break
    return best


def _prev_close_map(codes, upto_date):
    if not codes:
        return {}
    s = _closes_series(codes)
    out = {}
    for c, se in s.items():
        v = _close_asof(se, upto_date)
        if v:
            out[c] = v
    return out


def build_report(date):
    acct = _load_json(os.path.join(DATA_DIR, "account.json")) or {
        "cash": None, "positions": {}, "trades": []}
    trades = acct.get("trades") or []
    day_trades = [t for t in trades
                  if str(t.get("time", "")).startswith(date)]
    sells = [t for t in day_trades if t.get("side") == "sell"]
    buys = [t for t in day_trades if t.get("side") == "buy"]

    # ---- 资金概览 ----
    cash = acct.get("cash")
    positions = acct.get("positions") or {}
    pcodes = list(positions.keys())
    px = _prev_close_map(pcodes, date)
    mv = 0.0
    pos_rows = []
    for c, p in sorted(positions.items()):
        q = int(p.get("qty", 0) or 0)
        ep = float(p.get("entry_price", 0) or 0)
        lp = px.get(c, ep)
        m = q * lp
        mv += m
        pos_rows.append({
            "code": c, "name": p.get("name", ""), "qty": q,
            "entry": ep, "last": lp,
            "mkt": round(m, 2),
            "upl_pct": (lp / ep - 1.0) if ep > 0 else None,
        })
    total = (cash or 0) + mv

    # 当日盈亏 = 已实现(sells.pnl 原样) + 未平仓头寸的昨日→今日收盘变动
    realized_today = sum(float(s.get("pnl") or 0) for s in sells)
    fees_today = sum(float(t.get("fee") or 0) for t in day_trades)
    pre_codes = [c for c, p in positions.items()]
    prev_px_prev_day = _prev_close_map(pre_codes,
                                       _prev_trade_date(trades, date))
    overnight_move = 0.0
    for c, p in positions.items():
        q = int(p.get("qty", 0) or 0)
        b = float(prev_px_prev_day.get(c, 0) or 0)
        e = float(px.get(c, 0) or 0)
        if b > 0 and e > 0:
            overnight_move += q * (e - b)
    day_pnl = realized_today + overnight_move
    base = (total - day_pnl) if total else None   # 日初总资产近似基准
    day_pnl_pct = (day_pnl / base) if (base and abs(base) > 1e-9) else 0.0

    # ---- 回撤：逐笔事件时点的 MTM 净值曲线（现金+持仓按本地日线收盘估值）----
    codes_traded = sorted({str(t.get("code")) for t in trades})
    series_map = _closes_series(codes_traded)
    eq = float(getattr(C, "INITIAL_CAPITAL", 100000.0))
    peak = eq
    max_dd = 0.0
    lots = {}      # code -> {qty, ref_px(最近买价，作无收盘时的回退估值)}
    for t in sorted(trades, key=lambda x: str(x.get("time", ""))):
        c = str(t.get("code"))
        q = int(t.get("qty") or 0)
        px_ = float(t.get("price") or 0)
        if t.get("side") == "buy":
            eq -= px_ * q + float(t.get("fee") or 0)
            lot = lots.setdefault(c, {"qty": 0, "ref": px_})
            lot["qty"] += q
            lot["ref"] = px_
        else:
            eq += px_ * q - float(t.get("fee") or 0)
            lot = lots.setdefault(c, {"qty": 0, "ref": px_})
            lot["qty"] = max(0, lot["qty"] - q)
        d = str(t.get("time", ""))[:10]
        mv_eq = 0.0
        for cc, lot in lots.items():
            if not lot["qty"]:
                continue
            cl = _close_asof(series_map.get(cc, []), d) or lot["ref"]
            mv_eq += lot["qty"] * cl
        total_eq = eq + mv_eq
        peak = max(peak, total_eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - total_eq) / peak)
    # 报告日终值：现金 + 存续持仓按 <=date 收盘估值（与①同口径）
    mv_end = sum(int(l["qty"]) * (_close_asof(series_map.get(c, []), date) or l["ref"])
                 for c, l in lots.items() if l["qty"])
    cur_total_eq = eq + mv_end
    cur_dd_pct = ((peak - cur_total_eq) / peak) if peak > 0 else 0.0

    # ---- 连亏计数 ----
    consec_recon = 0
    for t in reversed(trades):
        if t.get("side") != "sell":
            continue
        if float(t.get("pnl") or 0) <= 0:
            consec_recon += 1
        else:
            break
    rs = _load_json(os.path.join(DATA_DIR, "risk_state.json")) or {}

    # ---- 告警判定 ----
    alerts = []
    today = time.strftime("%Y-%m-%d")
    suspend_active = bool(SUSPEND_UNTIL) and today <= SUSPEND_UNTIL
    if day_pnl_pct <= TRIGGER:
        alerts.append("🚨 击穿熔断线：当日盈亏 %+.2f%% ≤ %.0f%%"
                      % (day_pnl_pct * 100, TRIGGER * 100))
        if not rs.get("breaker_on"):
            alerts.append("   ⚠ 但引擎 breaker_on=false——离线重算与服务内状态不一致，请人工核对（服务停机也会造成此现象）")
    elif day_pnl_pct <= APPROACH_FACTOR * TRIGGER:
        alerts.append("⚠ 接近熔断线：当日盈亏 %+.2f%%，已进入熔断线(%.0f%%) 80%% 区间"
                      % (day_pnl_pct * 100, TRIGGER * 100))
    ml = int(getattr(C, "RISK_MAX_CONSEC_LOSSES", 3))
    eff_consec = int(rs.get("consec_losses", consec_recon))
    if eff_consec >= ml and suspend_active:
        alerts.append("⚠ 连亏 %d 笔已达暂停线(%d)，但冷却拦截已暂停——不会自动停，需人工决策"
                      % (eff_consec, ml))
    lvl2 = bool(alerts)

    # ---- 渲染 markdown ----
    L = []
    L.append("# 风控观察日报 %s" % date)
    L.append("")
    L.append("- 生成时间：%s｜数据源：account.json / audit.jsonl / risk_state.json / market.db（全部只读）"
             % time.strftime("%Y-%m-%d %H:%M:%S"))
    if suspend_active:
        L.append("- 🟡 **冷却拦截暂停生效中（至 %s）**：连亏暂停不拦新买单，单日熔断仍生效；本报告为此期间的唯一风控观察面。" % SUSPEND_UNTIL)
    L.append("")
    if lvl2:
        L.append("## 🔔 告警")
        L.extend("- " + a for a in alerts)
        L.append("")
    L.append("## ① 资金概览")
    L.append("| 现金 | 持仓市值(昨收/今收估值) | 总资产 | 当日盈亏 | 当日盈亏%% | 距熔断线(%.0f%%) |" % (abs(TRIGGER) * 100))
    L.append("|---|---|---|---|---|---|")
    if cash is None:
        L.append("| 读取失败(account.json 缺失) | - | - | - | - | - |")
    else:
        room = day_pnl_pct - TRIGGER
        L.append("| {:,.2f} | {:,.2f} | {:,.2f} | {:+,.2f} | {:+.2f}% | 还剩 {:.2f} 个百分点 |".format(
            cash, mv, total, day_pnl, day_pnl_pct * 100, room * 100))
    L.append("")
    L.append("> 口径：当日盈亏 = 已实现(sells.pnl 原样) + 存续持仓昨收→今收变动；日初基准由当前总资产反推，属观察口径，与服务内 update_daily_pnl 的开盘快照存在小幅差异。")
    L.append("")
    L.append("## ② 回撤（自初始资金 ¥{:,.0f} 逐笔事件时点 MTM 净值，持仓按本地收盘估值）".format(
        float(getattr(C, "INITIAL_CAPITAL", 100000))))
    L.append("- 当前回撤：%.2f%%　历史最大回撤：%.2f%%" % (cur_dd_pct * 100, max_dd * 100))
    if cash is not None:
        gap = cur_total_eq - total
        L.append("- 曲线终值 ¥{:,.2f} vs 账户总资产 ¥{:,.2f}（差额 {:+,.2f}，为记账/取整噪声；|差额|>1% 请人工核对）".format(
            cur_total_eq, total, gap))
    L.append("")
    L.append("## ③ 连亏计数")
    L.append("| 口径 | 数值 | 暂停线(RISK_MAX_CONSEC_LOSSES) | 冷却截止 |")
    L.append("|---|---|---|---|")
    L.append("| 引擎持久化(risk_state.json) | %s | %d | %s |" % (
        rs.get("consec_losses", "?"), ml, rs.get("cooldown_until", "-")))
    L.append("| 报告重算(account 全量逐笔) | %d | %d | 不适用 |" % (consec_recon, ml))
    L.append("")
    L.append("## ④ 持仓快照（估值=market.db 本地日线，无外部请求）")
    if pos_rows:
        L.append("| 代码 | 名称 | 数量 | 成本 | 收盘估值 | 市值 | 浮盈亏% |")
        L.append("|---|---|---|---|---|---|---|")
        for r in pos_rows:
            L.append("| {} | {} | {:,} | {:.3f} | {:.3f} | {:,.2f} | {} |".format(
                r["code"], r["name"], r["qty"], r["entry"], r["last"],
                r["mkt"],
                "-" if r["upl_pct"] is None else "{:+.2f}%".format(r["upl_pct"] * 100)))
    else:
        L.append("空仓（positions=[]）")
    L.append("")
    L.append("## ⑤ 当日买卖流水（%s，共 %d 笔：买 %d / 卖 %d）" % (
        date, len(day_trades), len(buys), len(sells)))
    if day_trades:
        L.append("| 时间 | 方向 | 代码 | 名称 | 价格 | 数量 | 金额 | 手续费 | 已实现盈亏 | 事由 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for t in day_trades:
            amt = float(t.get("price") or 0) * float(t.get("qty") or 0)
            pnl = t.get("pnl")
            L.append("| {} | {} | {} | {} | {:.3f} | {:,} | {:,.2f} | {:.2f} | {} | {} |".format(
                str(t.get("time", ""))[11:], t.get("side"), t.get("code"),
                t.get("name", ""), float(t.get("price") or 0),
                int(t.get("qty") or 0), amt, float(t.get("fee") or 0),
                "-" if pnl is None else "{:+,.2f}".format(float(pnl)),
                (t.get("reason") or "").replace("|", "/")))
        L.append("")
        L.append("- 当日合计：手续费 {:.2f}，已实现盈亏 {:+,.2f}".format(fees_today, realized_today))
    else:
        L.append("当日无成交。")
    L.append("")
    L.append("## ⑥ audit 当日摘要（WARN/ERROR）")
    all_aud = _load_jsonl(os.path.join(DATA_DIR, "audit", "audit.jsonl"))
    upd = [r for r in all_aud if str(r.get("t", "")).startswith(date)
           and r.get("event") == "data_update"]
    if upd:
        u = upd[-1]
        L.append("- 日更(15:1x)：min5 {}/{}，daily {}".format(
            u.get("min5_updated", "?"), u.get("min5_total", "?"),
            u.get("daily_updated", "?")))
    aud = [r for r in all_aud if str(r.get("t", "")).startswith(date)
           and r.get("level") in ("WARN", "ERROR")]
    if aud:
        for r in aud[-8:]:
            L.append("- `[{}]` {} {} {}".format(r.get("level"), r.get("kind"),
                                                r.get("event"), r.get("msg", "")))
        if len(aud) > 8:
            L.append("- （当日共 %d 条，仅列最后 8 条）" % len(aud))
    else:
        L.append("- 当日无 WARN/ERROR 记录。")
    L.append("")
    return "\n".join(L), lvl2


def _prev_trade_date(trades, date):
    ds = sorted({str(t.get("time", ""))[:10] for t in trades})
    prev = [d for d in ds if d < date]
    return prev[-1] if prev else "1990-01-01"


def main():
    ap = argparse.ArgumentParser(description="风控观察日报（只读）")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    a = ap.parse_args()
    text, alert = build_report(a.date)
    os.makedirs(REPORT_DIR, exist_ok=True)
    out = os.path.join(REPORT_DIR, "risk_daily_%s.md" % a.date)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    print("[written] %s" % out)
    sys.exit(2 if alert else 0)


if __name__ == "__main__":
    main()
