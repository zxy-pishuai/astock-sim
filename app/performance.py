# -*- coding: utf-8 -*-
"""绩效分析模块（v3.4）—— 借鉴 backtrader Analyzer / quantstats 的指标体系
纯标准库实现，回测引擎与模拟盘账户共用同一套绩效口径。

输入约定：
  equity: 净值序列，元素为 {date, equity} 或 (date, equity) 或纯数字（按顺序）
  trades: 交易列表（含 side/pnl/entry_date 等），用于胜率/盈亏比/连续亏损/持仓天数
  initial: 初始资金（用于年化计算）

输出：完整绩效指标 dict，含：
  收益类: total_return / annual_return / volatility / sharpe / sortino / calmar
  回撤类: max_drawdown / dd_peak_date / dd_trough_date / dd_recover_date / max_dd_days / underwater_days
  交易类: win_rate / profit_factor / avg_trade_return / max_consecutive_losses / avg_holding_days
  分布类: best_trade / worst_trade / monthly_returns（月度收益热力图数据）
"""
import math
import os


# ---------- ★ D4（2026-09-13）：执行滑点 KPI（触发价 vs 成交价） ----------
# 回测滑点假设：V1 常量 SLIP=0.002 → 20bp（tools/tactic_lianban_backtest.py / 报告 §0）。
# 实盘 slippage_bp 语义（trader._sell）：(fill_price/trigger_price - 1) * 10000，
#   卖出 fill<trigger 为负 → 成本视角 cost_bp = -slippage_bp（正=成本）。
# 告警规则：本周/当日平均成本 > 2×回测假设（40bp）→ SLIPPAGE_ALERT。
BACKTEST_SLIP_BP = 20.0


def _exit_slip_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "exit_slippage.jsonl")


def _exit_slip_rows(path=None):
    """读 exit_slippage.jsonl 全部行（append-only；坏行跳过）"""
    import json as _json
    p = path or _exit_slip_path()
    rows = []
    if not os.path.exists(p):
        return rows
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(_json.loads(line))
            except Exception:
                pass
    return rows


def _p95(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(0.95 * len(s)))]


def execution_quality(slips, backtest_slip_bp=BACKTEST_SLIP_BP):
    """执行质量 KPI（D5 周报"执行质量"章节的数据源）：
    按 exit_kind 分组 avg_bp / p95_bp / avg_delay_min + 总体 vs 回测假设 20bp 对比。
    slips: exit_slippage.jsonl 行列表（含 slippage_bp/delay_min/exit_kind）。
    返回 {"by_exit_kind": {kind: {...}}, "overall": {...}}；无滑点样本 → n=0 空表。
    """
    groups = {}
    all_costs = []
    for r in slips:
        if r.get("slippage_bp") is None:
            continue
        k = str(r.get("exit_kind") or "未知")
        g = groups.setdefault(k, {"n": 0, "costs": [], "delays": []})
        g["n"] += 1
        cost = -float(r["slippage_bp"])          # 成本向（正=损失）
        g["costs"].append(cost)
        all_costs.append(cost)
        if r.get("delay_min") is not None:
            g["delays"].append(float(r["delay_min"]))
    out = {}
    for k, g in groups.items():
        out[k] = {"n": g["n"],
                  "avg_bp": round(sum(g["costs"]) / len(g["costs"]), 1),
                  "p95_bp": round(_p95(g["costs"]), 1) if g["costs"] else None,
                  "avg_delay_min": round(sum(g["delays"]) / len(g["delays"]), 1)
                  if g["delays"] else None}
    if all_costs:
        avg = sum(all_costs) / len(all_costs)
        overall = {"n": len(all_costs), "avg_bp": round(avg, 1),
                   "p95_bp": round(_p95(all_costs), 1),
                   "vs_backtest_20bp": round(avg / backtest_slip_bp, 2),
                   "alert": ("SLIPPAGE_ALERT" if avg > 2 * backtest_slip_bp else "OK"),
                   "alert_rule": "avg_bp > 2x backtest(20bp)=40bp -> SLIPPAGE_ALERT"}
    else:
        overall = {"n": 0, "avg_bp": None, "p95_bp": None,
                   "vs_backtest_20bp": None, "alert": "NO_SAMPLE",
                   "alert_rule": "avg_bp > 2x backtest(20bp)=40bp -> SLIPPAGE_ALERT"}
    return {"by_exit_kind": out, "overall": overall,
            "backtest_assumption_bp": backtest_slip_bp}


def record_daily_exit_slippage(trade_date, audit=None, slips=None):
    """每日收盘汇总（D4 任务2）：读当日 exit_slippage.jsonl →
    按 exit_kind 分组的 exit_slippage_daily 审计事件（kind='daily'）。
    trade_date: YYYY-MM-DD；slips: 可注入行列表（测试用），缺省读文件当日行。
    返回事件字段 dict（验收/复现用）；audit 失败不抛出。"""
    rows = slips if slips is not None else _exit_slip_rows()
    day_rows = [r for r in rows if str(r.get("t", ""))[:10] == trade_date]
    eq = execution_quality(day_rows)
    fields = {"trade_date": trade_date, "n": eq["overall"]["n"],
              "avg_bp": eq["overall"]["avg_bp"], "p95_bp": eq["overall"]["p95_bp"],
              "avg_delay_min": round(sum(
                  [d for d in [r.get("delay_min") for r in day_rows] if d is not None]
              ) / max(1, sum(1 for r in day_rows if r.get("delay_min") is not None)), 1)
              if any(r.get("delay_min") is not None for r in day_rows) else None,
              "vs_backtest_20bp": eq["overall"]["vs_backtest_20bp"],
              "alert": eq["overall"]["alert"],
              "by_exit_kind": eq["by_exit_kind"]}
    try:
        if audit is None:
            from . import audit as _audit
            audit = _audit
        audit.record("daily", "exit_slippage_daily",
                     level=("WARN" if fields["alert"] == "SLIPPAGE_ALERT" else "INFO"),
                     **fields)
    except Exception:
        pass
    return fields


def _to_equity_list(equity):
    """把多种输入形态统一为 [(date_str, equity_float), ...]"""
    out = []
    for e in equity:
        if isinstance(e, dict):
            out.append((e.get("date", ""), float(e.get("equity", 0))))
        elif isinstance(e, (tuple, list)) and len(e) >= 2:
            out.append((str(e[0]), float(e[1])))
        else:
            out.append(("", float(e)))
    return out


def _annual_factor(span_days):
    """按区间天数估算年化因子（244个交易日/年）"""
    return 244.0 / span_days if span_days > 0 else 1.0


def analyze_equity(equity, trades=None, initial=100000.0, benchmark=None):
    """主入口：对净值序列 + 交易记录做完整绩效分析。
    benchmark: 可选基准净值序列（如指数/等权），用于 Alpha/Beta/信息比率。
    """
    eq = _to_equity_list(equity)
    if len(eq) < 2:
        return {"error": "净值序列不足"}
    vals = [v for _, v in eq]
    n = len(vals)
    capital = initial if initial and initial > 0 else vals[0]
    total_ret = vals[-1] / capital - 1 if capital > 0 else 0.0

    # ---- 年化 / 波动 / 夏普 ----
    span_days = n  # 交易日数
    years = span_days / 244.0
    annual = (vals[-1] / capital) ** (1 / years) - 1 if years > 0 and vals[-1] > 0 else -1.0
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, n) if vals[i - 1] > 0]
    mean_r = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean_r) ** 2 for r in rets) / len(rets) if rets else 0.0
    sd = math.sqrt(var) if var > 0 else 0.0
    vol_annual = sd * math.sqrt(244)
    sharpe = mean_r / sd * math.sqrt(244) if sd > 0 else 0.0

    # ★ 4.5：Alpha / Beta / 信息比率（相对基准，区分能力 vs 贝塔）
    alpha = beta = info_ratio = None
    if benchmark:
        try:
            bench_vals = [v for _, v in _to_equity_list(benchmark)]
            # 允许长度差 ≤1（benchmark 常含初始值 1.0）
            if abs(len(bench_vals) - n) <= 1 and bench_vals[0] > 0 and n > 2:
                if len(bench_vals) == n + 1:
                    bench_vals = bench_vals[1:]
                bench_rets = [bench_vals[i] / bench_vals[i - 1] - 1
                              for i in range(1, len(bench_vals)) if bench_vals[i - 1] > 0]
                # 对齐长度
                min_len = min(len(rets), len(bench_rets))
                if min_len >= 2:
                    rs = rets[-min_len:]
                    bs = bench_rets[-min_len:]
                    bm = sum(bs) / len(bs)
                    rm = sum(rs) / len(rs)
                    # Beta = cov(r,b)/var(b)
                    cov = sum((rs[i] - rm) * (bs[i] - bm) for i in range(min_len)) / min_len
                    bvar = sum((b - bm) ** 2 for b in bs) / min_len
                    beta = cov / bvar if bvar > 0 else 0.0
                    # Alpha（年化）：rf 按 0 处理
                    rf_daily = 0.0
                    alpha_daily = (rm - rf_daily) - beta * (bm - rf_daily)
                    alpha = alpha_daily * 244
                    # 信息比率：超额收益 / 超额波动（年化）
                    excess = [rs[i] - bs[i] for i in range(min_len)]
                    em = sum(excess) / len(excess)
                    evar = sum((e - em) ** 2 for e in excess) / len(excess)
                    esd = math.sqrt(evar) if evar > 0 else 0.0
                    info_ratio = em / esd * math.sqrt(244) if esd > 0 else 0.0
        except Exception:
            alpha = beta = info_ratio = None

    # 下行波动（Sortino：只算负收益）
    downs = [r for r in rets if r < 0]
    dmean = sum(downs) / len(downs) if downs else 0.0
    dvar = sum((r - dmean) ** 2 for r in downs) / len(downs) if downs else 0.0
    dsd = math.sqrt(dvar) if dvar > 0 else 0.0
    sortino = mean_r / dsd * math.sqrt(244) if dsd > 0 else (99.0 if mean_r > 0 else 0.0)

    # ---- 回撤曲线：峰值/谷值/恢复日期/水下时间 ----
    peak = vals[0]
    peak_idx = 0
    max_dd = 0.0
    dd_peak_idx = dd_trough_idx = 0
    underwater_days = 0       # 总水下（低于峰值）天数
    cur_underwater_start = None
    max_underwater_span = 0   # 最长水下持续天数
    recover_idx = None
    recover_after_max = None
    cur_peak_idx = 0
    for i, v in enumerate(vals):
        if v > peak:
            peak = v
            peak_idx = i
        dd = v / peak - 1 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
            dd_peak_idx = peak_idx
            dd_trough_idx = i
            recover_idx = None  # 恢复未发生
        if dd < -1e-9:
            underwater_days += 1
            if cur_underwater_start is None:
                cur_underwater_start = i
        else:
            if cur_underwater_start is not None:
                span = i - cur_underwater_start
                if span > max_underwater_span:
                    max_underwater_span = span
                cur_underwater_start = None
            if dd_peak_idx == peak_idx and i > dd_trough_idx and recover_idx is None:
                recover_idx = i  # 自最大回撤谷值后的首次恢复
    # 若当前仍在水下，把到末尾的跨度也计入
    if cur_underwater_start is not None:
        span = n - 1 - cur_underwater_start
        max_underwater_span = max(max_underwater_span, span)
    recover_after_max = (eq[recover_idx][0] if recover_idx is not None and recover_idx >= dd_trough_idx
                         else "")

    # ---- 交易统计 ----
    sells = [t for t in (trades or []) if t.get("side") == "sell"]
    pnls = [t.get("pnl") or 0 for t in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) if pnls else 0.0
    gw = sum(wins)
    gl = -sum(losses)
    pf = gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0)
    avg_trade = sum(pnls) / len(pnls) if pnls else 0.0
    best = max(pnls) if pnls else 0.0
    worst = min(pnls) if pnls else 0.0

    # 最大连续亏损次数
    max_streak = streak = 0
    for p in pnls:
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # 平均持仓天数（买入→卖出，按 entry_date/date 粗算；无日期则跳过）
    # ★ 4.5：改用交易日历计算（原用自然日，周末虚增持仓天数）
    hold_days = []
    buys = {t.get("code"): t for t in (trades or []) if t.get("side") == "buy"}
    for t in sells:
        b = buys.get(t.get("code"))
        if not b:
            continue
        try:
            d0 = str(b.get("date", b.get("time", "")))[:10]
            d1 = str(t.get("date", t.get("time", "")))[:10]
            if d0 and d1:
                try:
                    from . import trading_calendar as tcal
                    hold_days.append(max(0, len(tcal.trading_days(d0, d1)) - 1))
                except Exception:
                    from datetime import datetime
                    hold_days.append((datetime.strptime(d1, "%Y-%m-%d")
                                      - datetime.strptime(d0, "%Y-%m-%d")).days)
        except Exception:
            pass
    avg_hold = sum(hold_days) / len(hold_days) if hold_days else None

    # ---- 月度收益热力图 ----
    monthly = {}
    prev_v = capital
    prev_m = ""
    for date, v in eq:
        m = str(date)[:7]
        if m and m != prev_m and prev_m:
            monthly[prev_m] = v / prev_v - 1 if prev_v > 0 else 0.0
            prev_v = v
        elif m != prev_m:
            prev_v = capital if not prev_m else prev_v
        prev_m = m
    if prev_m and prev_v > 0:
        monthly[prev_m] = vals[-1] / prev_v - 1

    calmar = annual / abs(max_dd) if max_dd < 0 else 0.0

    return {
        "total_return": round(total_ret, 4),
        "annual_return": round(annual, 4),
        "volatility": round(vol_annual, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "calmar": round(calmar, 3),
        "alpha": round(alpha, 4) if alpha is not None else None,       # ★ 4.5
        "beta": round(beta, 3) if beta is not None else None,          # ★ 4.5
        "info_ratio": round(info_ratio, 3) if info_ratio is not None else None,  # ★ 4.5
        "max_drawdown": round(max_dd, 4),
        "dd_peak_date": eq[dd_peak_idx][0] if dd_peak_idx < n else "",
        "dd_trough_date": eq[dd_trough_idx][0] if dd_trough_idx < n else "",
        "dd_recover_date": recover_after_max,
        "max_dd_days": max(0, dd_trough_idx - dd_peak_idx),
        "underwater_days": underwater_days,
        "max_underwater_days": max_underwater_span,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(pf, 2),
        "avg_trade_return": round(avg_trade, 2),
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
        "max_consecutive_losses": max_streak,
        "avg_holding_days": round(avg_hold, 1) if avg_hold is not None else None,
        "monthly_returns": {k: round(v, 4) for k, v in monthly.items()},
    }


def trade_attribution(trades):
    """★ 4.5 持仓归因（Brinson 简化版）：把已实现盈亏拆成 选股/择时/规模 三部分。
    借鉴 skill-portfolio-attribution Brinson-Fachler / pybrinson 思路，纯标准库：
      基准 = 组合等权平均单笔收益（把每笔交易看成等权持仓）
      选股贡献 = Σ 权重 × (个股收益 - 基准收益)    （跑赢/跑输均仓的部分）
      择时贡献 = Σ (个股权重 - 均权) × 个股收益     （超配/低配的效果）
      规模贡献 = 权重×收益 与上两者之差（交互项，实战中归并到择时）
    返回 dict：by_stock 明细 + 汇总 + 结论文本。
    trades: [{code,name,side,price,qty,fee,pnl,time}]（含买卖）
    """
    sells = [t for t in (trades or []) if t.get("side") == "sell" and t.get("pnl") is not None]
    buys = {}
    for t in (trades or []):
        if t.get("side") == "buy":
            buys.setdefault(t.get("code"), []).append(t)
    if not sells:
        return {"stocks": [], "selection": 0.0, "timing": 0.0, "scale": 0.0,
                "total_pnl": 0.0, "trade_count": 0, "bench_return": 0.0,
                "verdict": "暂无已实现交易"}

    # 每只股票：买入成本基准 → 卖出盈亏
    per_stock = {}   # code -> {name, cost, pnl, qty}
    for t in sells:
        code = t.get("code")
        d = per_stock.setdefault(code, {"name": t.get("name", code), "cost": 0.0,
                                        "pnl": 0.0, "qty": 0})
        d["pnl"] += t.get("pnl") or 0
        d["qty"] += t.get("qty") or 0
        b = (buys.get(code) or [{}])[0]
        d["cost"] += (b.get("price") or 0) * (t.get("qty") or 0)

    n = len(per_stock)
    total_pnl = sum(d["pnl"] for d in per_stock.values())
    total_cost = sum(d["cost"] for d in per_stock.values()) or 1.0
    # 基准：均权组合收益率（Brinson 标准：把每只股票视为等权持仓的基准组合）
    avg_ret = sum(d["pnl"] / d["cost"] for d in per_stock.values() if d["cost"] > 0) / n if n else 0.0
    bench = avg_ret

    # 金额口径分解（Brinson 单期 + 余项，四项恒等）：
    #   组合总盈亏 total = Σ cost_i × ret_i
    #   基准盈亏   bench_pnl = total_cost × bench            （均权基准）
    #   选股贡献   selection = Σ cost_i × (ret_i - bench)    （跑赢/跑输基准）
    #   择时贡献   timing    = Σ cost_i × (w_i - w_avg) × bench （仓位偏移效果）
    #   规模/交互   scale     = total - bench_pnl - selection - timing （余项，恒等式强制成立）
    bench_pnl = total_cost * bench
    selection = 0.0
    timing = 0.0
    rows = []
    for code, d in per_stock.items():
        ret = d["pnl"] / d["cost"] if d["cost"] > 0 else 0.0
        w = d["cost"] / total_cost if total_cost > 0 else 0.0
        w_avg = 1.0 / n if n > 0 else 0.0
        sel = d["cost"] * (ret - bench)             # 选股（超额部分）
        tim = d["cost"] * (w - w_avg) * bench       # 择时
        selection += sel
        timing += tim
        rows.append({
            "code": code, "name": d["name"],
            "pnl": round(d["pnl"], 2), "cost": round(d["cost"], 2),
            "ret": round(ret, 4), "weight": round(w, 4),
            "selection": round(sel, 2), "timing": round(tim, 2),
        })
    scale = total_pnl - bench_pnl - selection - timing  # 余项（交互+规模效应）

    # 结论：哪个维度贡献最大（超额部分，不含基准本身）
    parts = [("选股", selection), ("择时", timing), ("规模", scale)]
    best_dim, best_val = max(parts, key=lambda x: abs(x[1]))
    excess = selection + timing + scale  # = total - bench_pnl（恒等）
    if excess >= 0:
        verdict = (f"超额收益 {excess:+,.0f}（均权基准{bench*100:+.2f}%贡献{bench_pnl:+,.0f}），"
                   f"主要来自【{best_dim}】"
                   f"({'会选股' if best_dim == '选股' else '仓位管理' if best_dim == '择时' else '仓位规模'})"
                   if abs(best_val) > 0 else "超额接近零，与均权基准相当")
    else:
        verdict = (f"超额亏损 {excess:+,.0f}（均权基准{bench*100:+.2f}%贡献{bench_pnl:+,.0f}），"
                   f"主要拖累来自【{best_dim}】"
                   f"({'选股不佳' if best_dim == '选股' else '仓位错配' if best_dim == '择时' else '规模效应'})")
    return {
        "stocks": sorted(rows, key=lambda x: -abs(x["pnl"])),
        "selection": round(selection, 2),
        "timing": round(timing, 2),
        "scale": round(scale, 2),
        "total_pnl": round(total_pnl, 2),
        "bench_pnl": round(bench_pnl, 2),
        "excess_pnl": round(excess, 2),
        "trade_count": len(sells),
        "bench_return": round(bench, 4),
        "verdict": verdict,
    }
