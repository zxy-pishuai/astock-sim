# -*- coding: utf-8 -*-
"""★ Phase42: 真打板回测引擎（独立新模块，不修改任何现有文件）。

与现有 board 策略（日线近似"昨日涨7~9.8%次日接力"）的本质区别：
  用 min5.db 的 5 分钟 bar 精确检测【盘中触板/封板/炸板】，并按三档成交假设模拟
  排板成交不确定性 —— 这是日线近似完全缺失的两大核心机制。

信号与成交模型（全部由 min5+日线推导，严格只用当日及以前信息）：
  - 涨停价：engine.limit_prices(昨收) 口径（主板10%/创业科创20%/ST5%）；
  - 触板：某根5分钟bar high ≥ 涨停价；首次触板时刻=排队时刻；
  - 封死：当日收盘==涨停价；否则=炸板；
  - 成交三档：
      M1 保守：仅炸板单成交（封死单默认排不进），成交价=涨停价；
      M2 中性：炸板单 100% 成交；封死单按首触时间给概率（10:30 前 p_early、之后 p_late，
               实例级 RNG(seed) 决定，可复现）；
      M3 激进：全部触板单成交（上限对照）；
  - 买入价 = 涨停价×(1+滑点)；费用复用 engine.buy_fee/sell_fee；T+1。

退出（在日线上执行，语义对齐 engine._check_exits 的参数）：
  阶梯止盈(LADDER_TP_STEPS 分批) → 止损(STOP_LOSS_PCT) → 全清止盈(TAKE_PROFIT_PCT)
  → 时间止损(TIME_STOP_DAYS 且浮亏) → 超时(MAX_HOLD_DAYS)；卖出=收盘×(1-滑点)。
  未复刻：冲高回落止盈/移动止损/主力出货（本阶段退出集按任务书四项，差异写入报告）。
"""
import random
import sqlite3
import os

from . import config as C          # noqa: F401  (只读)
from . import engine as eng        # noqa: F401  (只读 limit_prices/buy_fee/sell_fee)
from . import db as _db            # ★ J3：统一连接工厂（读连接复用+PRAGMA）

MIN5_DB = getattr(C, "MIN5_DB_FILE", None) or os.path.join(C.DATA_DIR, "min5.db")


def _m5_code(code):
    return ("sh" if code.startswith("6") else "sz") + code


class TrueBoardBacktest:
    """真打板回测：min5 触板检测 + 三档排板成交假设 + 日线退出。"""

    def __init__(self, codes, names, start, end, initial_capital=100000.0,
                  fill_model="M2", max_positions=2, position_pct=0.25,
                  p_early=None, p_late=None, early_cutoff=None, seed=42,
                  fill_params=None):
        # P65 参数化：fill_prob 进 params（fill_params 优先，其次 config，显式仍兼容）
        fp = fill_params or {}
        if p_early is None:
            p_early = fp.get("p_early", getattr(C, "SHADOW_BOARD_FILL_P_EARLY", 0.30))
        if p_late is None:
            p_late = fp.get("p_late", getattr(C, "SHADOW_BOARD_FILL_P_LATE", 0.60))
        if early_cutoff is None:
            early_cutoff = fp.get("early_cutoff", getattr(C, "SHADOW_BOARD_EARLY_CUTOFF", "10:30"))
        self.codes = [c for c in codes
                      if c not in set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])]
        self.names = names or {}
        self.start, self.end = start, end
        self.capital = initial_capital
        self.fill_model = fill_model
        self.max_positions = max_positions
        self.position_pct = position_pct
        self.p_early, self.p_late = p_early, p_late
        self.early_cutoff = early_cutoff.replace(":", "")
        self.rng = random.Random(seed)
        self.slip = C.SLIPPAGE
        self.trades = []
        self.equity = []
        self.cash = initial_capital
        self.positions = {}
        self._events_cache = {}      # date -> [event]
        self.calendar = []
        self.daily = {}

    # ---------- 数据 ----------
    def _load_daily(self):
        conn = _db.open_ro(C.DB_FILE, 20000)   # ★ J3：读连接复用（原每次新建+close）
        out = {}
        cs = self.codes
        for i in range(0, len(cs), 100):
                chunk = cs[i:i + 100]
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT code,date,open,high,low,close FROM kline WHERE period='day' "
                    "AND length(code)=6 AND code IN (%s) AND date<=? ORDER BY code,date"
                    % ph, tuple(chunk) + (self.end,)).fetchall()
                for c, d, o, h, l, cl in rows:
                    if cl is None or cl <= 0 or o is None or o <= 0:
                        continue
                    out.setdefault(c, {"dates": [], "o": [], "h": [], "l": [], "c": []})
                    rec = out[c]
                    rec["dates"].append(d)
                    rec["o"].append(o)
                    rec["h"].append(h)
                    rec["l"].append(l)
                    rec["c"].append(cl)
        # ★ J3：读连接由 db.py 复用持有，不再 close
        for c, rec in out.items():
            rec["idx"] = {d: i for i, d in enumerate(rec["dates"])}
        return out

    def _scan_min5(self, daily):
        """扫描 min5（全表顺序流式）：产出触板事件 {date: [{code,hhmm,sealed,lu,...}]}。"""
        want = {_m5_code(c): c for c in self.codes}
        events_by_day = {}
        dates_set = set()
        conn = _db.open_ro(MIN5_DB, 60000)   # ★ J3：读连接复用（原 timeout=60 每次新建）
        state = {"code": None, "day": None, "first_touch": None}

        def flush_day():
            code5, day, ft = state["code"], state["day"], state["first_touch"]
            if code5 is None or day is None or ft is None:
                return
            code = want.get(code5)
            if not code or not (self.start <= day <= self.end):
                return
            rec = daily.get(code)
            if not rec:
                return
            di = rec["idx"].get(day)
            if di is None or di < 1:
                return
            nm = self.names.get(code, "")
            if "ST" in (nm or "").upper():
                return                          # ST 剔除
            lu, _ld = eng.limit_prices(code, rec["c"][di - 1], nm)
            day_close = rec["c"][di]
            sealed = abs(day_close - lu) < 0.005
            events_by_day.setdefault(day, []).append({
                "code": code, "date": day, "hhmm": ft,
                "sealed": sealed, "lu": lu, "close": day_close,
            })
            dates_set.add(day)

        q = ("SELECT code, substr(date,1,10), substr(date,12,5), high "
             "FROM kline_min5 ORDER BY code, date")
        for code5, day, hm, high in conn.execute(q):
            if code5 != state["code"] or day != state["day"]:
                flush_day()
                state["code"], state["day"], state["first_touch"] = code5, day, None
            if state["first_touch"] is not None or high is None:
                continue
            code6 = want.get(code5)
            rec = daily.get(code6) if code6 else None
            if not rec:
                continue
            di = rec["idx"].get(day)
            if di is None or di < 1:
                continue
            nm = self.names.get(code6, "")
            if "ST" in (nm or "").upper():
                continue
            lu, _ld = eng.limit_prices(code6, rec["c"][di - 1], nm)
            if high >= lu - 1e-9:
                state["first_touch"] = hm
        flush_day()
        # ★ J3：读连接由 db.py 复用持有，不再 close
        self._events_cache = events_by_day
        self.calendar = sorted(dates_set)
        return events_by_day

    # ---------- 主循环 ----------
    def run(self, daily=None):
        self.daily = daily if daily is not None else self._load_daily()
        if not self._events_cache:               # 允许多档模型共享一次 min5 扫描
            self._scan_min5(self.daily)
        calendar, daily = self.calendar, self.daily
        cash = self.cash
        positions = {}

        def do_sell(code, px, di, tag):
            nonlocal cash
            p = positions.pop(code)
            amount = px * p["qty"]
            fee = eng.sell_fee(amount)
            cash += amount - fee
            # 剩余仓位对应成本（阶梯已卖部分按比例分摊并计入 realized_pnl）
            part_cost = p["cost_all"] * (p["qty"] / float(p["orig_qty"]))
            pnl = p["realized_pnl"] + (amount - fee - part_cost)
            self.trades.append({
                "code": code, "entry_date": p["entry_date"],
                "exit_date": calendar[di], "buy_px": round(p["buy_px"], 3),
                "qty": p["qty"], "sell_px": round(px, 3), "pnl": round(pnl, 2),
                "ret": round(pnl / p["cost_all"], 6), "exit": tag,
                "fill_model": self.fill_model, "sealed": p.get("sealed"),
            })

        for di, d in enumerate(calendar):
            # ---- 1) 退出 ----
            for code in list(positions.keys()):
                rec = daily.get(code)
                p = positions[code]
                i = rec["idx"].get(d) if rec else None
                if i is None or i - p["entry_i"] < 1:
                    continue                     # 停牌 / T+1
                held = i - p["entry_i"]
                cp = rec["c"][i]
                pnl_pct = cp * (1 - self.slip) / p["buy_px"] - 1.0
                # 阶梯止盈（分批）
                if C.LADDER_TP_ENABLED:
                    for si, (thr, frac) in enumerate(C.LADDER_TP_STEPS):
                        if si in p["ladder_sold"] or pnl_pct < thr:
                            continue
                        qs = int(p["qty"] * frac / 100) * 100
                        qs = min(max(qs, 0), p["qty"])
                        if qs >= 100:
                            px = cp * (1 - self.slip)
                            amount = px * qs
                            fee = eng.sell_fee(amount)
                            cash += amount - fee
                            part_cost = p["cost_all"] * (qs / float(p["orig_qty"]))
                            realized = amount - fee - part_cost
                            p["realized_pnl"] += realized
                            p["qty"] -= qs
                            p["ladder_sold"].append(si)
                            self.trades.append({
                                "code": code, "entry_date": p["entry_date"],
                                "exit_date": d, "buy_px": round(p["buy_px"], 3),
                                "qty": qs, "sell_px": round(px, 3),
                                "pnl": round(realized, 2), "ret": None,
                                "exit": "阶梯止盈%d" % si,
                                "fill_model": self.fill_model, "sealed": p.get("sealed"),
                            })
                        else:
                            p["ladder_sold"].append(si)
                    if p["qty"] <= 0:
                        positions.pop(code)
                        continue
                tag = None
                if pnl_pct <= C.STOP_LOSS_PCT:
                    tag = "止损"
                elif pnl_pct >= C.TAKE_PROFIT_PCT:
                    tag = "止盈"
                elif held >= C.TIME_STOP_DAYS and pnl_pct < 0:
                    tag = "时间止损"
                elif held >= C.MAX_HOLD_DAYS:
                    tag = "超时"
                if tag:
                    do_sell(code, cp * (1 - self.slip), di, tag)
            # ---- 2) 入场（按首触时间排队）----
            evs = sorted(self._events_cache.get(d) or [], key=lambda e: e["hhmm"])
            for ev in evs:
                if len(positions) >= self.max_positions:
                    break
                code = ev["code"]
                if code in positions or code not in daily:
                    continue
                filled = True
                if self.fill_model == "M1":
                    filled = not ev["sealed"]
                elif self.fill_model == "M2":
                    if ev["sealed"]:
                        prob = self.p_early if ev["hhmm"] < self.early_cutoff else self.p_late
                        filled = self.rng.random() < prob
                if not filled:
                    continue
                px = ev["lu"] * (1 + self.slip)
                qty = int((cash * self.position_pct) / px / 100) * 100
                if qty < 100:
                    continue
                cost = px * qty
                fee = eng.buy_fee(cost)
                if cost + fee > cash:
                    continue
                cash -= cost + fee
                positions[code] = {
                    "buy_px": ev["lu"], "qty": qty, "orig_qty": qty,
                    "cost_all": cost + fee, "realized_pnl": 0.0,
                    "entry_i": daily[code]["idx"][d], "entry_date": d,
                    "ladder_sold": [], "sealed": ev["sealed"],
                }
                self.trades.append({
                    "code": code, "entry_date": d, "exit_date": None,
                    "buy_px": round(ev["lu"], 3), "qty": qty,
                    "sell_px": None, "pnl": None, "ret": None, "exit": None,
                    "fill_model": self.fill_model, "sealed": ev["sealed"],
                })
            # ---- 3) 日终估值 ----
            mv = 0.0
            for code, p in positions.items():
                rec = daily.get(code)
                i = rec["idx"].get(d) if rec else None
                px = rec["c"][i] if rec and i is not None else p["buy_px"]
                mv += px * p["qty"]
            self.equity.append({"date": d, "equity": round(cash + mv, 2)})
        # ---- 窗口末强平 ----
        last_di = len(calendar) - 1
        for code in list(positions.keys()):
            rec = daily.get(code)
            di = None
            for dd in reversed(calendar):
                j = rec["idx"].get(dd) if rec else None
                if j is not None:
                    di = j
                    break
            if di is not None:
                do_sell(code, rec["c"][di] * (1 - self.slip), last_di, "期末清仓")

        total_ret = cash / self.capital - 1.0
        peak, mdd = -1e18, 0.0
        for e in self.equity:
            peak = max(peak, e["equity"])
            if peak > 0:
                mdd = min(mdd, e["equity"] / peak - 1.0)
        sells = [t for t in self.trades if t["exit_date"]]
        closed = [t for t in sells if t["pnl"] is not None]
        wins = sum(1 for t in closed if t["pnl"] > 0)
        return {
            "total_return": round(total_ret, 4),
            "max_drawdown": round(mdd, 4),
            "trade_count": len(self.trades),
            "closed_rounds": len(closed),
            "win_rate": round(wins / len(closed), 4) if closed else None,
            "n_days": len(calendar),
            "touches": sum(len(v) for v in self._events_cache.values()),
        }

    # ---------- 封板生态统计（副产品） ----------
    def ecosystem_stats(self):
        evs = [e for _, lst in sorted(self._events_cache.items()) for e in lst]
        daily = self.daily
        sealed_n = sum(1 for e in evs if e["sealed"])
        broken_n = len(evs) - sealed_n

        def next_perf(lst):
            rets, gaps = [], []
            for e in lst:
                rec = daily.get(e["code"])
                if not rec:
                    continue
                i = rec["idx"].get(e["date"])
                if i is None or i + 1 >= len(rec["c"]):
                    continue
                rets.append((rec["c"][i + 1] / rec["c"][i] - 1) * 100)
                gaps.append((rec["o"][i + 1] / rec["c"][i] - 1) * 100)
            return {
                "n": len(rets),
                "next_day_avg_pct": round(sum(rets) / len(rets), 4) if rets else None,
                "next_day_gap_avg_pct": round(sum(gaps) / len(gaps), 4) if gaps else None,
                "next_day_up_ratio": round(sum(1 for r in rets if r > 0) / len(rets), 4)
                                     if rets else None,
            }

        early = [e for e in evs if e["hhmm"] < self.early_cutoff]
        late = [e for e in evs if e["hhmm"] >= self.early_cutoff]
        by_time = {
            "10:30前": {"touches": len(early),
                        "seal_rate": round(sum(1 for e in early if e["sealed"]) / len(early), 4)
                        if early else None},
            "10:30后": {"touches": len(late),
                        "seal_rate": round(sum(1 for e in late if e["sealed"]) / len(late), 4)
                        if late else None},
        }
        return {
            "total_touches": len(evs),
            "sealed": sealed_n, "broken": broken_n,
            "seal_rate": round(sealed_n / len(evs), 4) if evs else None,
            "break_rate": round(broken_n / len(evs), 4) if evs else None,
            "by_first_touch_time": by_time,
            "sealed_next_day": next_perf([e for e in evs if e["sealed"]]),
            "broken_next_day": next_perf([e for e in evs if not e["sealed"]]),
        }
