# -*- coding: utf-8 -*-
"""组合风控模块（v3.4 + v3.7 + 4.2）—— 借鉴 vnpy RiskManager / xuanji"硬风控" / riskguard
与策略解耦的"组合级风控规则引擎"：回测引擎与实盘交易中心共用同一模块，
保证【回测约束 == 实盘约束】。

设计要点：
  1. 事前校验：任何买入委托先过 check_buy()，不满足直接拒绝（硬风控）
  2. 白名单过滤：ST/退市/北交所/停牌/涨跌停不可买
  3. 仓位约束：单票仓位上限、最大持仓数、单日最大买入金额/次数
  4. 市场冰点禁开仓：上涨占比过低时不买（与 scoring.get_regime 联动）
  5. 全量审计：每次校验结果记录到 events（供 Web 审计页展示）
  v3.7：行业敞口上限 + 相关性告警
  v4.2：
  6. 单日亏损熔断：当日组合浮亏超阈值 → 禁开新仓
  7. 连续亏损暂停：连续 N 笔亏损 → 冷却 N 个交易日
  8. 情绪退潮强制清仓标记：情绪退潮/冰点 → 提示清仓（配合 trader）
"""
import sqlite3
import time
from collections import deque

from . import config as C


class RiskEngine:
    def __init__(self):
        # ★ P2-4：移除未使用的 self._lock
        self.events = deque(maxlen=200)     # [{t, level, msg}]
        self._day = ""
        self._day_buy_amount = 0.0          # 当日累计买入金额
        self._day_buy_count = 0             # 当日累计买入次数
        # ★ 4.2：熔断/连亏状态
        self._day_start_total = None        # 当日开盘组合资产（熔断基准）
        self._day_pnl_pct = 0.0             # 当日组合盈亏%
        self._consec_losses = 0             # 连续亏损笔数
        self._cooldown_until = ""           # 连亏冷却截止日（YYYY-MM-DD）
        self._breaker_on = False            # 熔断标记
        self._load_state()

    def _reset_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._day_buy_amount = 0.0
            self._day_buy_count = 0
            # ★ P0-2 修复：熔断基准跨日重置（原来永不复位 → 当日盈亏变多日累计，熔断误判）
            self._day_start_total = None
            self._day_pnl_pct = 0.0
            self._breaker_on = False

    def _event(self, level, msg):
        self.events.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})

    # ---------- 4.2：熔断/连亏状态持久化 ----------
    def _state_path(self):
        import os
        return os.path.join(C.DATA_DIR, "risk_state.json")

    def _load_state(self):
        import json
        import os
        try:
            if os.path.exists(self._state_path()):
                with open(self._state_path(), "r", encoding="utf-8") as f:
                    d = json.load(f)
                self._consec_losses = int(d.get("consec_losses", 0))
                self._cooldown_until = d.get("cooldown_until", "")
                self._breaker_on = bool(d.get("breaker_on", False))
                # 跨日自动解除连亏冷却
                today = time.strftime("%Y-%m-%d")
                if self._cooldown_until and self._cooldown_until < today:
                    self._consec_losses = 0
                    self._cooldown_until = ""
        except Exception:
            pass

    def _save_state(self):
        import json
        try:
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump({"consec_losses": self._consec_losses,
                           "cooldown_until": self._cooldown_until,
                           "breaker_on": self._breaker_on}, f, ensure_ascii=False)
        except Exception:
            pass

    def update_daily_pnl(self, total_now, total_start):
        """每日更新组合盈亏（熔断判定）。total_now: 当前总资产, total_start: 当日开盘总资产"""
        # ★ 修复：每次调用自动滚日（进程跨日不重启时，防止买额/熔断基准跨日沿用）
        self._reset_day()
        if not total_start or total_start <= 0:
            return
        self._day_start_total = total_start
        self._day_pnl_pct = (total_now - total_start) / total_start
        # 单日熔断：浮亏超阈值 → 禁开新仓
        if self._day_pnl_pct <= C.RISK_DAILY_LOSS_TRIGGER and not self._breaker_on:
            self._breaker_on = True
            self._event("WARN", f"🚨 单日熔断触发：当日盈亏{self._day_pnl_pct*100:.1f}%（超{C.RISK_DAILY_LOSS_TRIGGER*100:.0f}%）")
            self._save_state()
        elif self._day_pnl_pct > C.RISK_DAILY_LOSS_TRIGGER:
            # 恢复阈值之上解除（滞后性）
            if self._breaker_on and self._day_pnl_pct >= C.RISK_BREAKER_RESUME:
                self._breaker_on = False
                self._event("OK", "熔断解除（当日盈亏回到阈值之上）")
                self._save_state()

    def record_trade_result(self, pnl):
        """记录一笔已实现盈亏（连亏计数）。pnl > 0 盈利，<= 0 亏损"""
        if pnl is None:
            return
        if pnl <= 0:
            self._consec_losses += 1
            if self._consec_losses >= C.RISK_MAX_CONSEC_LOSSES:
                # ★ 4.5 修复：冷却按【交易日】计算（原用自然日，周末会虚减冷却期）
                try:
                    from . import trading_calendar as tcal
                    nxt = tcal.next_n_trading_days(time.strftime("%Y-%m-%d"), C.RISK_COOLDOWN_DAYS)
                    self._cooldown_until = nxt[-1] if nxt else time.strftime("%Y-%m-%d")
                except Exception:
                    import datetime
                    self._cooldown_until = (datetime.date.today() +
                                            datetime.timedelta(days=C.RISK_COOLDOWN_DAYS)).isoformat()
                self._event("WARN", f"🚨 连续{self._consec_losses}笔亏损，冷却至{self._cooldown_until}")
        else:
            self._consec_losses = 0
        self._save_state()

    def in_cooldown(self):
        """是否处于连亏冷却期（禁止开新仓）"""
        today = time.strftime("%Y-%m-%d")
        # ★ 2026-08-27 冷却暂停开关（config.RISK_COOLDOWN_SUSPEND_UNTIL，验收方落地）：
        #   暂停期内不拦截新买单，过期自动恢复；单日熔断不受影响。
        if getattr(C, "RISK_COOLDOWN_SUSPEND_UNTIL", "") and today <= C.RISK_COOLDOWN_SUSPEND_UNTIL:
            return False
        return bool(self._cooldown_until and self._cooldown_until >= today)

    def market_breaker(self):
        """是否熔断/退潮禁开仓"""
        return self._breaker_on

    # ---------- 白名单 / 标的质量 ----------
    def is_st(self, name=""):
        n = (name or "").upper()
        return "ST" in n or "退" in n

    def is_tradable(self, code, quote):
        """基本面/流动性白名单：ST、退市、北交所、停牌、无行情不可交易"""
        if not quote:
            return False, "无行情"
        price = quote.get("price", 0) or 0
        if price <= 0:
            return False, "停牌或无价"
        if self.is_st(quote.get("name", "")):
            return False, "ST/退市股"
        if code.startswith(("4", "8", "92")):
            return False, "北交所/三板"
        return True, ""

    # ---------- 事前买入校验（硬风控） ----------
    def check_buy(self, code, name, price, qty, cash, positions,
                  amount=None, regime=None, quote=None):
        """买入委托事前校验。返回 (ok, msg)。
        positions: 当前持仓 dict（code -> pos）
        regime: 市场环境名称（来自 scoring.get_regime）
        """
        self._reset_day()
        # ★ 4.2：熔断/连亏冷却拦截
        if self._breaker_on:
            return False, f"单日熔断中（当日盈亏{self._day_pnl_pct*100:.1f}%）"
        if self.in_cooldown():
            return False, f"连续亏损冷却中（至{self._cooldown_until}）"
        if not price or price <= 0 or qty < 100:
            return False, "委托无效"
        if code in positions:
            return False, "已持有该股"
        if len(positions) >= C.RISK_MAX_POSITIONS:
            return False, f"已达最大持仓数({C.RISK_MAX_POSITIONS})"
        if quote is not None:
            ok, msg = self.is_tradable(code, quote)
            if not ok:
                return False, msg
        # 涨跌停（涨停买不进）
        yc = (quote or {}).get("yest_close", 0) or 0
        if yc > 0:
            from . import engine as eng
            lu, _ = eng.limit_prices(code, yc, (quote or {}).get("name", ""))  # ★ 4.3 ST识别
            if price >= lu * 0.999:
                return False, f"涨停价不可买入({lu:.2f})"
        # 市场冰点禁开仓
        if regime in ("崩溃(防守)",):
            return False, "市场崩溃(防守)模式禁开仓"
        # 单票仓位上限（按总资产而非现金）
        total = cash + sum(p.get("qty", 0) * p.get("entry_price", 0)
                           for p in positions.values())
        cost = price * qty
        if total > 0 and cost / total > C.RISK_SINGLE_STOCK_PCT:
            return False, f"单票仓位超上限(>{C.RISK_SINGLE_STOCK_PCT:.0%})"
        # ★ v3.7：行业敞口上限（买入后该行业总占比不得超过阈值）
        if total > 0:
            sec = self._sector_of(code, name)
            if sec:
                sec_mv = cost
                for c, p in positions.items():
                    if self._sector_of(c, p.get("name", "")) == sec:
                        sec_mv += p.get("qty", 0) * p.get("entry_price", 0)
                if sec_mv / total > C.RISK_SECTOR_PCT:
                    return False, f"行业[{sec}]敞口超上限(>{C.RISK_SECTOR_PCT:.0%})"
        # 单日买入金额/次数限制
        amt = amount or cost
        if self._day_buy_amount + amt > C.RISK_DAILY_BUY_AMOUNT:
            return False, f"单日买入金额超上限(¥{C.RISK_DAILY_BUY_AMOUNT:,.0f})"
        if self._day_buy_count >= C.RISK_DAILY_BUY_COUNT:
            return False, f"单日买入次数超上限({C.RISK_DAILY_BUY_COUNT}次)"
        return True, ""

    # ---------- v3.7：行业敞口 & 相关性 ----------
    @staticmethod
    def _sector_of(code, name=""):
        """个股行业归属（复用 scoring.classify_sector；失败返回空）"""
        try:
            from . import scoring as sc
            return sc.classify_sector(name or "", code)
        except Exception:
            return ""

    def sector_exposure(self, positions, quote_of=None):
        """当前组合行业敞口：{sector: {mv, pct, codes}}（按持仓市值占比）"""
        from . import datafeed as df
        quotes = {}
        if quote_of:
            quotes = quote_of
        else:
            codes = list(positions.keys())
            if codes:
                quotes = df.fetch_quotes(codes)
        total = 0.0
        per_sec = {}
        for code, p in positions.items():
            q = quotes.get(code, {})
            cp = q.get("price", 0) or p.get("entry_price", 0) or 0
            mv = cp * p.get("qty", 0)
            total += mv
            sec = self._sector_of(code, p.get("name", "")) or "其他"
            d = per_sec.setdefault(sec, {"mv": 0.0, "codes": []})
            d["mv"] += mv
            d["codes"].append(code)
        out = {}
        for sec, d in per_sec.items():
            out[sec] = {
                "pct": round(d["mv"] / total, 4) if total > 0 else 0.0,
                "mv": round(d["mv"], 2),
                "codes": d["codes"],
                "over": d["mv"] / total > C.RISK_SECTOR_PCT if total > 0 else False,
            }
        return out

    def correlation_alert(self, positions, quote_of=None):
        """组合相关性告警：两持仓相关系数 |r| 超阈值时列出。
        返回 [{code_a, code_b, corr, msg}]（按 |corr| 降序，最多 5 条）
        """
        codes = list(positions.keys())
        if len(codes) < 2:
            return []
        from . import datafeed as df
        from . import portfolio as pf
        closes_by_code = {}
        for c in codes:
            try:
                kl = df.fetch_kline(c, "day", C.RISK_CORR_LOOKBACK + 30)
                if len(kl) >= 30:
                    closes_by_code[c] = [k["close"] for k in kl]
            except Exception:
                continue
        if len(closes_by_code) < 2:
            return []
        rets = {}
        for c, closes in closes_by_code.items():
            seg = closes[-C.RISK_CORR_LOOKBACK:]
            rets[c] = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg)) if seg[i - 1] > 0]
        corr, cset = pf.corr_matrix(rets)
        alerts = []
        for i in range(len(cset)):
            for j in range(i + 1, len(cset)):
                a, b = cset[i], cset[j]
                r = corr.get((a, b), 0.0)
                if abs(r) >= C.RISK_CORR_ALERT:
                    na = positions.get(a, {}).get("name", a)
                    nb = positions.get(b, {}).get("name", b)
                    alerts.append({
                        "code_a": a, "name_a": na, "code_b": b, "name_b": nb,
                        "corr": round(r, 3),
                        "msg": f"持仓相关性 {r:+.2f}（{na} × {nb}）——同涨同跌风险",
                    })
        alerts.sort(key=lambda x: -abs(x["corr"]))
        return alerts[:5]

    # ---------- 成交后记账（供审计与日度限额） ----------
    def record_buy(self, amount):
        self._reset_day()
        self._day_buy_amount += amount
        self._day_buy_count += 1
        self._event("OK", f"风控记账: 买入 ¥{amount:,.0f}（当日累计¥{self._day_buy_amount:,.0f}/{self._day_buy_count}次）")

    def reset_day(self):
        self._reset_day()
        self._day_buy_amount = 0.0
        self._day_buy_count = 0

    # ★ P2-2：snapshot 结果缓存（前端 3 秒轮询，行业敞口/相关性每次重算拉行情太贵）
    _snap_ts = 0.0
    _snap_cache = None

    def snapshot(self):
        """风控状态快照（Web 展示）。★ P2-2：30 秒 TTL 缓存（敞口/相关性不每次重算）。"""
        now = time.time()
        if self._snap_cache and now - self._snap_ts < 30:
            snap = dict(self._snap_cache)
            snap["day_pnl_pct"] = round(self._day_pnl_pct, 4)
            snap["breaker_on"] = self._breaker_on
            snap["consec_losses"] = self._consec_losses
            snap["in_cooldown"] = self.in_cooldown()
            snap["daily_buy_amount"] = round(self._day_buy_amount, 2)
            snap["daily_buy_count"] = self._day_buy_count
            snap["cooldown_until"] = self._cooldown_until
            snap["events"] = list(self.events)[-40:]
            return snap
        self._reset_day()
        # ★ v3.7：行业敞口 + 相关性告警（需要持仓）
        from . import state as st
        try:
            acct = st.load_account()
            positions = acct.get("positions", {})
            exposure = self.sector_exposure(positions) if positions else {}
            corr_alerts = self.correlation_alert(positions) if positions else []
        except Exception:
            exposure, corr_alerts = {}, []
        snap = {
            "day": self._day,
            "daily_buy_amount": round(self._day_buy_amount, 2),
            "daily_buy_amount_limit": C.RISK_DAILY_BUY_AMOUNT,
            "daily_buy_count": self._day_buy_count,
            "daily_buy_count_limit": C.RISK_DAILY_BUY_COUNT,
            "max_positions": C.RISK_MAX_POSITIONS,
            "single_stock_pct": C.RISK_SINGLE_STOCK_PCT,
            "st_blacklist": True,
            "market_freeze_regime": "崩溃(防守)",
            # v3.7 组合级风控
            "sector_pct_limit": C.RISK_SECTOR_PCT,
            "corr_alert_threshold": C.RISK_CORR_ALERT,
            "sector_exposure": exposure,
            "corr_alerts": corr_alerts,
            # ★ 4.2：熔断/连亏
            "breaker_on": self._breaker_on,
            "day_pnl_pct": round(self._day_pnl_pct, 4),
            "daily_loss_trigger": C.RISK_DAILY_LOSS_TRIGGER,
            "consec_losses": self._consec_losses,
            "max_consec_losses": C.RISK_MAX_CONSEC_LOSSES,
            "cooldown_until": self._cooldown_until,
            "in_cooldown": self.in_cooldown(),
            "events": list(self.events)[-40:],
        }
        self._snap_ts = now
        self._snap_cache = snap
        return snap


engine = RiskEngine()
