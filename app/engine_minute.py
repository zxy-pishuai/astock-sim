# -*- coding: utf-8 -*-
"""分钟级打板回测引擎（5分钟K线，最近约33个交易日窗口）
策略：
  board_intraday   盘中扫板：5分钟bar盘中涨幅进入[7%,9.8%)且未封死 → bar收盘买入
  auction_intraday 竞价打板：首根bar(9:35)开盘价高开[3%,9.8%)且竞价放量 → 开盘买入
规则：T+1（当日买入不可卖）、涨跌停（涨停买不进/跌停卖不出）、佣金+印花税+滑点
退出：次日低开≤-3%开盘止损 / 高开≥+5%开盘半仓止盈 / 常规止损止盈移动止损时间止损
输出：完整指标 + 净值曲线 + 交易明细 + 次日溢价统计
"""
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config as C
from . import datafeed as df
from . import engine as eng
from . import performance as perf


class MinuteBoardBacktest:
    def __init__(self, codes, names, start, end, initial_capital=100000.0,
                 strategy="board_intraday", params=None):
        self.codes = [c for c in codes if c]
        self.names = names or {}
        self.start, self.end = start, end
        self.capital = initial_capital
        self.strategy = strategy
        p = params or {}
        self.max_pos = p.get("max_positions", 3)
        self.pos_pct = p.get("position_pct", C.POSITION_PCT)
        self.slippage = p.get("slippage", C.SLIPPAGE)
        self.board_min = p.get("board_min_pct", C.BOARD_MIN_PCT)
        self.board_max = p.get("board_max_pct", C.BOARD_MAX_PCT)
        self.auction_min = p.get("auction_min_pct", 3.0)
        self.auction_max = p.get("auction_max_pct", 9.8)
        self.auction_vr_min = p.get("auction_vr_min", 0.015)
        # v3.6：分钟回测窗口扩展 —— 本地一年分钟数据优先（约48000根），回退新浪1600根
        self.min5_days = p.get("min5_days", 0)  # 0=尽量取全（本地数据一年）
        self.use_local_min5 = True

        self.min_bars = {}   # code -> {date: [bars]}
        self.daily = {}      # code -> {date: bar}
        self.daily_dates = {}  # code -> [dates]
        self.trading_days = []
        self.cash = initial_capital
        self.positions = {}
        self.trades = []
        self.equity = []
        self.signals = []    # 全部买入信号（溢价统计用）

    # ---------- 数据 ----------
    def _load(self, progress_cb=None):
        def worker(code):
            try:
                m = df.fetch_kline(code, "min5", self.min5_days or 80000)
                d = df.fetch_kline(code, "day", 400)
                return code, m, d
            except Exception:
                return code, [], []
        done, total = 0, len(self.codes)
        with ThreadPoolExecutor(max_workers=C.PARALLEL_WORKERS) as ex:
            futs = {ex.submit(worker, c): c for c in self.codes}
            for f in as_completed(futs):
                code, m, d = f.result()
                done += 1
                if progress_cb and done % 10 == 0:
                    progress_cb(done, total)
                if len(m) < 96:  # 至少2天分钟数据
                    continue
                days = {}
                for b in m:
                    days.setdefault(b["date"][:10], []).append(b)
                self.min_bars[code] = {k: v for k, v in sorted(days.items())}
                if d:
                    self.daily[code] = {k["date"]: k for k in d}
                    self.daily_dates[code] = sorted(self.daily[code].keys())
        all_dates = set()
        for code, days in self.min_bars.items():
            for d in days:
                if self.start <= d <= self.end:
                    all_dates.add(d)
        self.trading_days = sorted(all_dates)

    def _prev_close(self, code, date):
        dates = self.daily_dates.get(code) or []
        dl = self.daily.get(code) or {}
        for i, d in enumerate(dates):
            if d == date:
                return dl[dates[i - 1]]["close"] if i > 0 else None
            if d > date:
                return dl[dates[i - 1]]["close"] if i > 0 else None
        return dl[dates[-1]]["close"] if dates else None

    def _daily_bar(self, code, date, offset=0):
        dates = self.daily_dates.get(code) or []
        dl = self.daily.get(code) or {}
        for i, d in enumerate(dates):
            if d == date:
                j = i + offset
                if 0 <= j < len(dates):
                    return dl[dates[j]], dates[j]
                return None, None
        return None, None

    # ---------- 撮合 ----------
    def _buy(self, date, code, price, qty, reason, sig):
        if qty < 100 or price <= 0:
            return False
        cost = price * qty
        fee = eng.buy_fee(cost)
        if cost + fee > self.cash + 1e-6:
            qty = int((self.cash * 0.98) / price / 100) * 100
            if qty < 100:
                return False
            cost = price * qty
            fee = eng.buy_fee(cost)
        if cost + fee > self.cash + 1e-6:
            return False
        self.cash -= cost + fee
        self.positions[code] = {
            "qty": qty, "entry_price": price, "entry_date": date,
            "peak": price, "days": 0, "reason": reason,
        }
        self.trades.append({
            "date": date, "code": code, "name": self.names.get(code, code),
            "side": "buy", "price": round(price, 3), "qty": qty,
            "amount": round(cost, 2), "fee": round(fee, 2),
            "pnl": None, "reason": reason,
        })
        self.signals.append({**sig, "code": code, "name": self.names.get(code, code),
                             "entry_price": price, "entry_date": date})
        return True

    def _sell(self, date, code, price, qty, reason):
        pos = self.positions.get(code)
        if not pos or qty <= 0 or price <= 0:
            return False
        if qty > pos["qty"]:
            qty = pos["qty"]
        amount = price * qty
        fee = eng.sell_fee(amount)
        pnl = amount - fee - pos["entry_price"] * qty
        self.cash += amount - fee
        pos["qty"] -= qty
        self.trades.append({
            "date": date, "code": code, "name": self.names.get(code, code),
            "side": "sell", "price": round(price, 3), "qty": qty,
            "amount": round(amount, 2), "fee": round(fee, 2),
            "pnl": round(pnl, 2), "reason": reason,
        })
        if pos["qty"] <= 0:
            del self.positions[code]
        return True

    # ---------- 买入信号 ----------
    def _intraday_signals(self, code, date):
        """盘中扫板信号：返回买入价或 None"""
        bars = self.min_bars.get(code, {}).get(date)
        if not bars:
            return None
        prev = self._prev_close(code, date)
        if not prev:
            return None
        lu, ld = eng.limit_prices(code, prev)
        first_open = bars[0]["open"]
        if first_open >= lu * 0.999:
            return None  # 一字涨停开盘买不进
        for b in bars:
            if b["close"] <= 0:
                continue
            pct = (b["close"] - prev) / prev * 100
            if self.board_min <= pct < self.board_max and b["close"] < lu * 0.995:
                return {"price": b["close"], "pct": round(pct, 2),
                        "time": b["date"][11:16],
                        "reason": f"盘中扫板({pct:.1f}%@{b['date'][11:16]})"}
        return None

    def _auction_signals(self, code, date):
        """竞价打板信号：开盘高开买入"""
        bars = self.min_bars.get(code, {}).get(date)
        if not bars:
            return None
        prev = self._prev_close(code, date)
        if not prev:
            return None
        lu, ld = eng.limit_prices(code, prev)
        first = bars[0]
        open_px = first["open"]
        if open_px <= 0:
            return None
        apct = (open_px - prev) / prev * 100
        if not (self.auction_min <= apct < self.auction_max):
            return None
        if open_px >= lu * 0.999:
            return None  # 一字涨停
        # 竞价量比 = 首根5分钟量 / 昨日全天量
        prev_bar = self._daily_bar(code, date, -1)
        prev_vol = prev_bar[0]["volume"] if prev_bar and prev_bar[0] else 0
        vr = first["volume"] / prev_vol if prev_vol > 0 else 0
        if vr < self.auction_vr_min:
            return None
        return {"price": open_px, "pct": round(apct, 2),
                "time": "09:35", "vr": round(vr, 3),
                "reason": f"竞价高开({apct:+.1f}%,量比{vr:.2f})"}

    # ---------- 退出（T+1） ----------
    def _check_exits(self, date):
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            bar, _ = self._daily_bar(code, date)
            if not bar:
                continue  # 停牌
            cp = bar["close"]
            entry = pos["entry_price"]
            pnl_pct = (cp - entry) / entry
            pos["days"] += 1
            # ★ F2：peak 只用建仓后的 bar high（三处口径统一到 engine.update_peak_after_entry）。
            #   分钟回测同语义：_check_exits 在买入前执行，买入当日不进本段，
            #   此处恒为建仓后交易日 → 无建仓前污染（显式守卫防回归）。
            if pos["days"] >= 1:
                pos["peak"] = eng.update_peak_after_entry(pos["peak"], entry,
                                                          bar["high"], ref_price=cp)
            if pos["days"] < 1:
                continue  # ★ T+1（v3.4 修正）：days=0 买入当日不可卖；days>=1（次日）起可卖
            reason = None
            # 次日开盘处理（v3.4 修正：days==1 即买入后第一个交易日）
            open_px = bar["open"]
            lu, ld = eng.limit_prices(code, self._prev_close(code, date) or cp)
            if pos["days"] == 1:
                if open_px <= ld * 1.001:
                    pass  # 跌停卖不出
                elif open_px <= entry * (1 + C.BOARD_NEXT_DAY_LOW):
                    self._sell(date, code, open_px * (1 - self.slippage), pos["qty"],
                               f"次日低开止损({(open_px/entry-1)*100:+.1f}%)")
                    continue
                elif open_px >= entry * (1 + 0.05):
                    half = pos["qty"] // 200 * 100
                    if half >= 100:
                        self._sell(date, code, open_px * (1 - self.slippage), half,
                                   f"次日高开半仓止盈({(open_px/entry-1)*100:+.1f}%)")
            if pnl_pct <= C.STOP_LOSS_PCT:
                reason = "止损"
            elif pnl_pct >= C.TAKE_PROFIT_PCT:
                reason = "止盈"
            elif (pos["days"] >= 2 and pos["peak"] >= entry * (1 + C.INTRADAY_HIGH_TRIGGER)
                  and (pos["peak"] - cp) / pos["peak"] >= C.INTRADAY_PULLBACK and pnl_pct >= 0):
                reason = f"冲高回落止盈(峰{pos['peak']:.2f}→{cp:.2f})"
            elif pos["peak"] >= entry * (1 + C.TRAILING_ACTIVATE_PCT) and cp <= pos["peak"] * (1 + C.TRAILING_STOP_PCT):
                reason = f"移动止损(峰{pos['peak']:.2f})"
            elif pos["days"] >= C.TIME_STOP_DAYS and pnl_pct < 0:
                reason = f"时间止损({pos['days']}天亏{pnl_pct:+.1%})"
            elif pos["days"] >= C.MAX_HOLD_DAYS:
                reason = f"超时退出({pos['days']}天)"
            if reason:
                self._sell(date, code, cp, pos["qty"], reason)

    # ---------- 主循环 ----------
    def run(self, progress_cb=None):
        self._load(progress_cb)
        if not self.trading_days:
            return {"error": "分钟数据不足（检查股票池/日期/网络）"}
        for di, date in enumerate(self.trading_days):
            if progress_cb and di % 5 == 0:
                progress_cb(di, len(self.trading_days), phase="回放")
            self._check_exits(date)
            # 买入信号（盘中触发）
            if len(self.positions) < self.max_pos:
                used = len(self.positions)
                slots = self.max_pos - used
                pos_cash = self.cash * self.pos_pct
                if self.cash >= pos_cash * 0.5:
                    # 候选：池内所有股票按信号评分排序
                    cands = []
                    for code in self.codes:
                        if code in self.positions:
                            continue
                        sig = (self._intraday_signals(code, date)
                               if self.strategy == "board_intraday"
                               else self._auction_signals(code, date))
                        if sig:
                            cands.append((code, sig))
                    for code, sig in cands[:slots]:
                        if len(self.positions) >= self.max_pos:
                            break
                        px = sig["price"] * (1 + self.slippage)
                        qty = int(pos_cash / px / 100) * 100
                        if qty >= 100:
                            self._buy(date, code, px, qty, f"[{sig['reason']}]", sig)
            # 估值
            mv = 0.0
            for code, pos in self.positions.items():
                bar, _ = self._daily_bar(code, date)
                mv += (bar["close"] if bar else pos["entry_price"]) * pos["qty"]
            self.equity.append({"date": date, "equity": round(self.cash + mv, 2),
                                "cash": round(self.cash, 2), "mv": round(mv, 2)})

        last_date = self.trading_days[-1]
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            bar, _ = self._daily_bar(code, last_date)
            if bar:
                self._sell(last_date, code, bar["close"], pos["qty"], "期末清仓")
        return self._metrics()

    # ---------- 指标 ----------
    def _metrics(self):
        if len(self.equity) < 2:
            return {"error": "回测天数不足"}
        eq = [e["equity"] for e in self.equity]
        n = len(eq)
        total_ret = eq[-1] / self.capital - 1
        years = n / 244.0
        annual = (eq[-1] / self.capital) ** (1 / years) - 1 if years > 0 and eq[-1] > 0 else -1.0
        peak = eq[0]
        max_dd = 0.0
        for v in eq:
            peak = max(peak, v)
            max_dd = min(max_dd, v / peak - 1)
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n)]
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / len(rets)
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = mean_r / sd * math.sqrt(244) if sd > 0 else 0.0
        sells = [t for t in self.trades if t["side"] == "sell"]
        wins = [t for t in sells if (t["pnl"] or 0) > 0]
        losses = [t for t in sells if (t["pnl"] or 0) <= 0]
        win_rate = len(wins) / len(sells) if sells else 0.0
        gw = sum(t["pnl"] for t in wins)
        gl = -sum(t["pnl"] for t in losses)
        pf = gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0)
        # ★ v3.4：完整绩效分析
        pa = perf.analyze_equity(self.equity, self.trades, self.capital)
        return {
            "total_return": round(total_ret, 4),
            "annual_return": round(annual, 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe": round(sharpe, 3),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(pf, 2),
            "trade_count": len(self.trades),
            "buy_count": sum(1 for t in self.trades if t["side"] == "buy"),
            "sell_count": len(sells),
            "final_equity": round(eq[-1], 2),
            "equity_curve": self.equity,
            "trades": self.trades[-500:],
            "premium": self._premium_stats(),
            "performance": pa if "error" not in pa else None,
        }

    def _premium_stats(self):
        """次日溢价统计（买入信号 → 次日高开/收盘/最高溢价，来自日K）"""
        rows = []
        for s in self.signals:
            nxt, nxt_date = self._daily_bar(s["code"], s["entry_date"], 1)
            if not nxt:
                continue
            rows.append({
                "date": s["entry_date"], "next_date": nxt_date,
                "code": s["code"], "name": s["name"],
                "pct": s.get("pct"),
                "time": s.get("time", ""),
                "open_prem": round((nxt["open"] / s["entry_price"] - 1) * 100, 2),
                "close_prem": round((nxt["close"] / s["entry_price"] - 1) * 100, 2),
                "high_prem": round((nxt["high"] / s["entry_price"] - 1) * 100, 2),
            })
        if not rows:
            return {"total": 0, "rows": []}
        win = sum(1 for r in rows if r["close_prem"] > 0)
        return {
            "total": len(rows),
            "win_rate": round(win / len(rows), 4),
            "avg_open": round(sum(r["open_prem"] for r in rows) / len(rows), 3),
            "avg_close": round(sum(r["close_prem"] for r in rows) / len(rows), 3),
            "avg_high": round(sum(r["high_prem"] for r in rows) / len(rows), 3),
            "rows": rows[-300:],
        }
