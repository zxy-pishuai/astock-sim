# -*- coding: utf-8 -*-
"""实时模拟交易引擎（v3.2）—— 把 v2.0 的扫描/打板/竞价/自动退出能力移植到新架构
规则：T+1、涨跌停（涨停买不进/跌停卖不出）、佣金+印花税+滑点、停牌不可交易
功能：
  scan_once()         全市场评分扫描买入（市场环境分级 + 板块抱团/龙头加分）
  _board_round()      打板秒级轮询（涨幅7~9.8%主板，打板资金池）
  _auction_round()    竞价打板公示（9:25-9:30，只公示不交易）
  _exits_round()      持仓实时退出（止损/止盈/移动止损/时间止损/打板专属）
  start/stop          自动循环控制（交易时段自动运行）
"""
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import alert as al
from . import audit
from . import config as C
from . import datafeed as df
from . import engine as eng
from . import portfolio as portfolio
from . import pools
from . import risk as rk
from . import scoring as sc
from . import sector as sector
from . import state as st


# ★ C1（2026-09-13）：F2 peak 门控上线日。此前建仓（无 entry_ts）的持仓 peak 可能被
#   建仓前早盘高点污染（实证：09-08 002011 峰12.54=建仓前高点，09-09 按脏峰止损 -867.37）。
#   启动加载持仓时对这类持仓做一致性校验并重算，杜绝脏 peak 再进移动止损。
_F2_GATE_DATE = "2026-09-09"
_PEAK_REPAIR_TOL = 0.005   # 污染判定容差：peak 超出建仓后观测最高 0.5% 视为污染


def _peak_buy_ts(acct, code, entry_date):
    """C1：从 trades 找该仓建仓时刻（entry_date 当天 buy 的 time），返回 "HH:MM:SS"；无则 ""。"""
    for t in reversed(acct.get("trades", []) or []):
        if (t.get("code") == code and t.get("side") == "buy"
                and str(t.get("time", "")).startswith(entry_date)):
            return str(t["time"])[11:19]
    return ""


def _peak_obs_high_after(code, entry_date, buy_ts=""):
    """C1：建仓后观测最高价（实时尺度，与 peak/entry_price 同尺度）。
    min5 优先（动态清单 1002 只，5 分钟线 high 精确到分钟；历史持仓票实测全覆盖）。
    无 min5 覆盖 → 返回 (None, "no_min5")——日K为 qfq 方言尺度（70.8% 恒定放大类），
    与实时 peak 跨尺度不可直接比较，宁可 WARN 不修也不误修。返回 (high, source)。"""
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % C.MIN5_DB_FILE, uri=True, timeout=10)
        try:
            if buy_ts:
                row = conn.execute(
                    "SELECT MAX(high) FROM kline_min5 WHERE code=? AND date >= ?",
                    (code, entry_date + " " + buy_ts)).fetchone()
            else:
                row = conn.execute(
                    "SELECT MAX(high) FROM kline_min5 WHERE code=? AND date LIKE ?",
                    (code, entry_date + "%")).fetchone()
            if row and row[0]:
                return float(row[0]), "min5"
        finally:
            conn.close()
    except Exception:
        pass
    return None, "no_min5"


class _ResilientPool:
    """★ H1 半死状态根治（submit 守护）+ F2（2026-09-13）常驻池化。
    F2：底层池从 app.pools 常驻注册表取（thread_name_prefix=池名），不再每次新建销毁
    ThreadPoolExecutor（09-11 停摆快照单进程 418 个池、SSL 上下文反复初始化的根因）。
    submit 捕获 RuntimeError(cannot schedule new futures) → 重建重试一次（H1 语义保持）；
    解释器收尾（pools.is_shutting_down()）时静默返回 None，由各循环判空退出，
    不再刷 "cannot schedule new futures" ERROR（实测累计 208 条）。
    with 退出不再 shutdown（常驻池统一由 pools.shutdown_all 回收）。"""
    __slots__ = ("_name", "_max_workers")

    def __init__(self, name, max_workers):
        self._name = name
        self._max_workers = max_workers

    def _ex(self):
        return pools.get_pool(self._name, self._max_workers)

    def submit(self, fn, *args, **kwargs):
        try:
            return self._ex().submit(fn, *args, **kwargs)
        except RuntimeError as e:
            if "cannot schedule new futures" not in str(e):
                raise
            if pools.is_shutting_down():
                return None   # 静默退出（收尾期不重试，杠绝刷 ERROR）
            try:
                return self._ex().submit(fn, *args, **kwargs)
            except RuntimeError:
                try:
                    self._ex().shutdown(wait=False)
                except Exception:
                    pass
                raise

    def shutdown(self, wait=False):
        pass   # 常驻池不随实例关停（统一 pools.shutdown_all）

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
def _is_trading_day_today(now=None):
    """★ P0-3 修复：交易日判断（法定节假日休市/调休补班交易），
    原来只看 weekday() → 节假日引擎照常"成交"、补班日停摆。"""
    now = now or datetime.now()
    try:
        from . import trading_calendar as tcal
        return tcal.is_trading_day(now.strftime("%Y-%m-%d"))
    except Exception:
        return now.weekday() < 5


def _is_trading_time(now=None):
    now = now or datetime.now()
    if not _is_trading_day_today(now):
        return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def _is_auction_time(now=None):
    now = now or datetime.now()
    if not _is_trading_day_today(now):
        return False
    return now.hour == 9 and 25 <= now.minute <= 29


def _is_twothirty_time(now=None):
    """两点半战法窗口：14:20-14:35（买入时机）"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 1420 <= hm <= 1435


# ★ C5（2026-09-13）：卖出退出类别归类（exit_kind 枚举，模块级供 _sell 引用）。
#   主枚举（任务书 C5 定版）：移动止损/冲高回落止盈/阶梯止盈/时间止损/止损/
#   两点半战法/打板半仓止盈/手动。
#   归并规则（报告 §口径 声明）：超时退出(MAX_HOLD_DAYS)→时间止损（时间维度
#   退出）；打板低开止损→止损；普通止盈(TAKE_PROFIT_PCT 全清)→冲高回落止盈
#   （价格高点止盈语义，reason 原文可区分）；未命中→reason 原文截断，保证
#   审计字段非空可追溯。顺序敏感：长模式在前。
_EXIT_KIND_RULES = (
    ("打板半仓止盈", "打板半仓止盈"),
    ("冲高回落止盈", "冲高回落止盈"),
    ("阶梯止盈", "阶梯止盈"),
    ("移动止损", "移动止损"),
    ("两点半战法", "两点半战法"),
    ("时间止损", "时间止损"),
    ("超时退出", "时间止损"),
    ("止损", "止损"),
    ("止盈", "冲高回落止盈"),
)


def _exit_kind_of(reason):
    """把卖出 reason 文本归类为 exit_kind 枚举值（未命中→原文截断）。"""
    r = (reason or "").strip()
    for kw, kind in _EXIT_KIND_RULES:
        if kw in r:
            return kind
    return (r[:12] or "手动") if r else "手动"


class TradingEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.auto = False
        self.thread = None
        self._user_stopped = False   # ★ 开盘自动开启：用户手动停止标记（停后当天不自动重启）
        self._user_stopped_day = ""   # 手动停止日期（跨日自动清除）
        self.last_scan = []        # 最近一次扫描候选 [{code,name,score,signals,pct,price}]
        self.last_scan_time = ""
        self.last_scan_msg = ""
        self.last_regime = ""      # ★ 4.6：最近一次扫描的市场环境分级（供状态栏展示）
        self.last_breadth = 0.0    # ★ 4.6：市场上涨占比（同步展示）
        self.breadth_ts = 0.0      # ★ B-2：last_breadth 的数据时间（秒 lib）
        self._breadth_thread = None  # ★ B-2：上涨占比快链路线程（与全量扫描解耦）
        # ★D-S1 分歧转一致哨兵状态
        self._diverg_watch = {}      # code -> 过期时间戳
        self._diverg_last = {}       # code -> {'o':外盘,'i':内盘}（跨日重置）
        self._diverg_ts = 0.0        # 上次采样时刻（节流）
        self._diverg_alert_ts = {}   # code -> 上次告警时刻（60s 去重）
        self._depth_decay_last = {}  # ★D-S3 封单衰减基线：code -> {'d','q','ts'}
        self._board_watch_codes = []  # ★D-S5 打板热窗快环候选表（board 轮询每轮刷新）
        self._sentinel_ff_ts = {}     # ★D-S6b 分钟主力流上次取数时刻（Σ55s 限频）
        self._scan_thread = None   # ★ 4.6：后台扫描线程（防阻塞主循环）
        self._scan_thread_ts = 0.0 # ★ 4.6：上次启动后台扫描的时间戳（节流）
        self._break_alert_ts = {}  # ★ Phase17：炸板提醒冷却（code -> ts，15分钟）
        self.board_results = 0     # 最近一次打板轮询买入数
        self.auction = {"results": [], "time": "", "breadth": 0.0}
        self.twothirty = {"results": [], "time": "", "msg": ""}   # v3.4 两点半战法
        self.events = deque(maxlen=300)   # [{t, level, msg}]
        self.watch_events = deque(maxlen=100)  # 自选监控异动
        self._scan_lock = threading.Lock()   # 防止扫描重入
        self._fast_watch_thread = None   # ★ Phase51：高频监控守护线程
        self._last_wd_check_ts = 0.0     # R2-P0.3：watchdog 心跳低频检查时间戳
        self._auction_snap_ts = {}      # R2-P1.7：竞价快照每日去重 {date: ts}
        # H4：watchdog 心跳分级+节流状态
        self._wd_boot_time = None       # 系统开机时刻（秒），首次计算缓存；None=未计算
        self._wd_stale_start = None     # 当前断更持续状态起始时间（None=健康）
        self._wd_last_critical_ts = 0.0 # 上次 CRITICAL 告警时间（每小时节流）
        # K4：主循环阶段轨迹 + 停摆判定状态
        self._lp_phase = "idle"
        self._lp_phase_since = time.time()
        self._lp_seq = 0
        # ★ F4（2026-09-13）：相位耗时环形缓冲（保留最近 LP_HIST_MAX=2000 条，不落盘）
        self._lp_hist = deque(maxlen=getattr(C, "LP_HIST_MAX", 2000))
        self._lp_lat_ts = 0.0      # loop_phase_latency 上次聚合时刻
        self._lp_alert_ts = {}     # 相位 -> 上次 WARN 告警时刻（1h 去重）
        self._lp_last_finished_phase = ""
        self._lp_last_finished_at = 0.0
        self._lp_last_phase_ms = 0
        self._lp_stall_start = None
        self._lp_stall_last_warn = 0.0

    @property
    def quote_channel_degraded(self):
        """★ F1（2026-09-13）：交易行情通道降级状态（只读，实时读 datafeed 状态机）。
        供 trading/status 展示（server 接入由 J 道负责，本属性只读不改 server）。"""
        try:
            return bool(df.trading_channel_state().get("degraded"))
        except Exception:
            return False

    # ---------- ★ B-2 上涨占比快链路 ----------
    def _breadth_loop(self, live_every=25, idle_every=120):
        """独立线程维护 last_breadth：交易时段 25s/次，非时段 120s/次。
        用 enrich=False 的纯行情（3s TTL 缓存复用），不碰 emotion/评分路径，
        失败保留旧值。目标：开仓门禁看到的上涨家数延迟 ≤25s。"""
        while getattr(self, "running", False):
            try:
                in_hours = pools.is_shutting_down() is False and self._in_trading_window() \
                    if hasattr(self, "_in_trading_window") else True
            except Exception:
                in_hours = True
            try:
                stocks = df.fetch_all_stocks(enrich=False)
                if stocks:
                    self.last_breadth = sc.calc_breadth(stocks)
                    self.breadth_ts = time.time()
            except Exception:
                pass   # 保留旧值；扫描路径会兜底重算
            time.sleep(live_every if in_hours else idle_every)

    def _fresh_breadth(self):
        """有 75s 内的快链路 breadth 就用；否则返回 None（调用方用扫描自带重算兜底）。"""
        return self.last_breadth if (time.time() - self.breadth_ts) < 75 else None

    # ---------- ★ B-2 上涨占比快链路 ----------
    def _breadth_loop(self, live_every=25, idle_every=120):
        """独立线程维护 last_breadth：交易时段 25s/次，非时段 120s/次。
        用 enrich=False 的纯行情（3s TTL 缓存复用），不碰 emotion/评分路径，
        失败保留旧值。目标：开仓门禁看到的上涨家数延迟 ≤25s。"""
        while getattr(self, "running", False):
            in_hours = True
            try:
                in_hours = bool(self._in_trading_window()) if hasattr(
                    self, "_in_trading_window") else True
            except Exception:
                in_hours = True
            try:
                stocks = df.fetch_all_stocks(enrich=False)
                if stocks:
                    self.last_breadth = sc.calc_breadth(stocks)
                    self.breadth_ts = time.time()
            except Exception:
                pass   # 保留旧值；扫描路径会兜底重算
            time.sleep(live_every if in_hours else idle_every)

    def _fresh_breadth(self):
        """有 75s 内的快链路 breadth 就用；否则返回 None（调用方用扫描自带重算兜底）。"""
        return self.last_breadth if (time.time() - self.breadth_ts) < 75 else None

    # ---------- 日志 ----------
    def _event(self, level, msg):
        with self._lock:
            self.events.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        # v3.5：同步审计日志（BUY/SELL/WARN/ERROR 等关键事件）
        try:
            kind = {"BUY": "order", "SELL": "position"}.get(level, "alert")
            audit.record(kind, "trading_event", level=level, msg=msg)
        except Exception:
            pass

    def _notify(self, title, msg):
        """★ 4.5 修复：推送改后台线程执行（原同步 HTTP 推送，最坏阻塞交易主循环 20 秒：
        wechat_notify 10s + alert.notify 10s，5秒轮询被拖死）"""
        def _push():
            try:
                st.wechat_notify(title, msg)
            except Exception:
                pass
            try:
                level = "SELL" if "卖出" in title or "止盈" in title or "止损" in title else "BUY"
                al.notify(level, title[:30], title, msg, cooldown=30)
            except Exception:
                pass
        threading.Thread(target=_push, daemon=True).start()

    def _repair_positions_peak(self, acct=None, persist=True):
        """★ C1（2026-09-13）：启动时清洗 F2 遗留的污染 peak（一致性校验）。
        判定：entry_date < F2 上线日 且 无 entry_ts（F2 前建仓特征）且 peak > entry_price
        → 用建仓后行情观测最高（min5 实时尺度）重算 peak = max(entry_price, 观测最高)，
        审计记 position_peak_repaired（改前/改后/数据源）。无行情数据 → WARN 跳过不误修。
        acct=None 读生产账户；persist=False 不写回（验收/演练用副本）。"""
        if acct is None:
            try:
                acct = st.load_account()
            except Exception:
                return
        changed = False
        for code, pos in list(acct.get("positions", {}).items()):
            entry_date = str(pos.get("entry_date", "") or "")
            if not entry_date or entry_date >= _F2_GATE_DATE:
                continue
            if pos.get("entry_ts"):          # F2 后建仓（含 entry_ts）不校验
                continue
            entry = pos.get("entry_price") or 0
            peak = pos.get("peak") or 0
            if peak <= entry:                # 无污染嫌疑
                continue
            buy_ts = _peak_buy_ts(acct, code, entry_date)
            obs_high, src = _peak_obs_high_after(code, entry_date, buy_ts)
            if obs_high is None:
                self._event("WARN",
                            f"⛑ C1 {pos.get('name', code)}({code}) peak 无法校验"
                            f"（{src}，无分钟行情），未清洗")
                continue
            if peak <= obs_high * (1 + _PEAK_REPAIR_TOL):
                continue                     # peak 可由建仓后行情解释
            new_peak = max(entry, obs_high)
            try:
                audit.record("order", "position_peak_repaired", level="WARN",
                             code=code, name=str(pos.get("name", "") or "")[:40],
                             entry_date=entry_date, old_peak=round(peak, 3),
                             new_peak=round(new_peak, 3), obs_source=src,
                             test=not persist)
            except Exception:
                pass
            pos["peak"] = round(new_peak, 3)
            changed = True
            self._event("WARN",
                        f"⛑ C1 {pos.get('name', code)}({code}) peak 污染修复 "
                        f"{peak:.3f}→{new_peak:.3f}（{src}）")
        if changed and persist:
            try:
                st.save_account(acct)
            except Exception:
                self._event("ERROR", "C1 peak 修复写回失败")

    # ---------- 控制 ----------
    def start(self, auto=False):
        with self._lock:
            if self.running:
                return False, "已在运行"
            self.running = True
            self.auto = auto
            self._user_stopped = False   # ★ 自动开启：手动启动清除"用户停止"标记
            self._user_stopped_day = ""
            # ★ C1 护栏：peak 门控关闭 = 回退旧行为（建仓前高点可污染 peak），必须显式 WARN
            if not C.TRAILING_ENTRY_GATE:
                print("[WARN] TRAILING_ENTRY_GATE=False：peak 建仓门控关闭，回退旧行为"
                      "（建仓前早盘高点可能污染移动止损 peak）")
                self._event("WARN", "TRAILING_ENTRY_GATE=False：peak 门控关闭，回退旧行为")
            # ★ C1：启动时清洗 F2 前遗留的污染 peak（无持仓则零成本）
            self._repair_positions_peak()
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            # ★ Phase51：持仓+预选清单高频监控守护线程（2秒/次，只告警不下单）
            self._fast_watch_thread = threading.Thread(
                target=self._fast_watch_loop, daemon=True)
            self._fast_watch_thread.start()
            # ★ B-2：上涨占比快链路（25s/次，纯行情不带 enrich）——
            #   原实现 breadth 只随全量扫描（冷缓存实测 60s+）刷新，门禁据此"弱势禁买"，
            #   大盘转暖时开仓信号要迟到 1~2 分钟。此线程维持一个永远较新的 breadth。
            self._breadth_thread = threading.Thread(
                target=self._breadth_loop, daemon=True)
            self._breadth_thread.start()
        self._event("INFO", "交易引擎已启动" + ("（自动交易）" if auto else ""))
        return True, "已启动"

    def stop(self):
        with self._lock:
            if not self.running:
                return False, "未在运行"
            self.running = False
            self._user_stopped = True    # ★ 自动开启：用户手动停止 → 当天不再自动重启
            self._user_stopped_day = time.strftime("%Y-%m-%d")
        self._event("INFO", "交易引擎已停止")
        return True, "已停止"

    def _risk_capped_qty(self, acct, px, qty):
        """★ B-4：单笔风险定容——qty ≤ 权益×RISK_PER_TRADE_PCT% ÷ (买入价−止损价)。
        交割单依据见 config 注释。cap<100 股 → 返回 0（此票按当前止损距离
        无论如何都会亏超 1% 权益，直接不买，比事后止损便宜）。"""
        try:
            pct = float(getattr(C, 'RISK_PER_TRADE_PCT', 0))
            if pct <= 0:
                return qty, ''
            sl = abs(C.STOP_LOSS_PCT)
            if sl <= 0:
                return qty, ''
            pos = acct.get('positions') or {}
            mv = 0.0
            for p in pos.values():
                q0 = p.get('qty') or p.get('shares') or 0
                mv += (p.get('entry_price') or p.get('avg_cost') or p.get('price') or 0) * q0
            eq = acct.get('cash', 0) + mv
            per_share = px * sl          # 每股最大损失（触发即按 STOP_LOSS_PCT 止损）
            if per_share <= 0 or eq <= 0:
                return qty, ''
            cap_sh = eq * pct / 100.0 / per_share
            cap_qty = int(cap_sh / 100) * 100
            if cap_qty < 100:
                return 0, '风险仓位不足一手(per_share_lose=%.0f 元>1%%权益)' % per_share
            if cap_qty < qty:
                return cap_qty, '风险定容 %d→%d股(单笔止损≤%.0f元)' % (qty, cap_qty, eq * pct / 100)
            return qty, ''
        except Exception:
            return qty, ''

    # ---------- ★ A：弱市切抱团（分歧切核心） ----------
    _weak_tried = {}   # {date: set(code)} 当日已尝试的核心票（防反复挂单）

    def _limitup_streak(self, code):
        """连续涨停高度（主板口径 pct≥9.45%；取最近 40 根日K，倒序累计）。"""
        try:
            kl = df.fetch_kline(code, "day", 40)
            n = 0
            pre = None
            for i in reversed(range(len(kl))):
                k = kl[i]
                c = float(k.get('close')) if isinstance(k, dict) else float(k[4])
                if pre is None:
                    pre = c
                    # 需要前一日收盘判断涨停：改为顺序累计更简单
                    n = 0
                    continue
                o_prev = pre
                pre = c
            # 顺序重算（避免逆序复杂化）
            n = 0
            for i in range(1, len(kl)):
                c = float(kl[i].get('close')) if isinstance(kl[i], dict) else float(kl[i][4])
                p = float(kl[i - 1].get('close')) if isinstance(kl[i - 1], dict) else float(kl[i - 1][4])
                n = n + 1 if (p and c / p - 1 >= 0.0945) else 0
            return n
        except Exception:
            return 0

    def _weak_core_buys(self, acct, stocks, used, breadth):
        """★A 弱市抱团核心买入：R2/R3 环境触发。
        1 年回测依据（bt_weak_core_1y.py）：胜率 57%/盈亏比 2.54/净 +111.7%/回撤 -5.6%。
        规则（限价实证条文）：当日封板 + 连板高度≥3 的票里，买高度最高的 ≤2 仓；
        开盘一字/接近一字(≥+9%)不可买剔除；仓位走 B-4 风险定容。返回买入数。"""
        if not getattr(C, 'WEAK_CORE_ENABLE', False):
            return 0
        today = df._today_str()
        tried = self._weak_tried.setdefault(today, set())
        cap = int(getattr(C, 'WEAK_CORE_MAX_POS', 2))
        bought = 0
        cand = []
        for code, s in stocks.items():
            if code in (acct.get('positions') or {}):
                continue
            pct = s.get('pct_chg') or 0
            if pct < 9.4 or code in tried:
                continue
            h = self._limitup_streak(code)
            if h >= int(getattr(C, 'WEAK_CORE_MIN_STREAK', 3)):
                cand.append((h, code, s))
        cand.sort(key=lambda x: -x[0])
        for h, code, s in cand[:cap + 1]:
            if used + bought >= cap:
                break
            try:
                code = code.zfill(6) if code.isdigit() else code
                kl = df.fetch_kline(code, "day", 5)
                if not kl:
                    continue
                last_close = float(kl[-1]['close']) if isinstance(kl[-1], dict) else float(kl[-1][4])
            except Exception:
                continue
            if last_close and s.get('price', 0) >= last_close * 1.09:
                tried.add(code)     # 一字/接近平开不给溢价
                continue
            # 固定 10% 权益（回测口径）→ 再过 B-4 风险定容
            mv = 0.0
            for p in (acct.get('positions') or {}).values():
                q0 = p.get('qty') or p.get('shares') or 0
                mv += (p.get('entry_price') or p.get('price') or 0) * q0
            pos_cash = (acct.get('cash', 0) + mv) * 0.10
            px = self._buy_px(s, s.get('price'), "buy",
                              qty=int(pos_cash / (s.get('price') or 1) / 100) * 100,
                              day_amount=s.get('amount'))
            if px <= 0 or px >= last_close * 1.095:
                tried.add(code)
                continue
            qty = int(pos_cash / px / 100) * 100
            qty, _rsz = self._risk_capped_qty(acct, px, qty)
            if qty < 100:
                tried.add(code)
                continue
            ok_risk, msg_risk = rk.engine.check_buy(
                code, s.get('name', ''), px, qty, acct.get('cash', 0),
                acct.get('positions') or {}, amount=px * qty)
            if not ok_risk:
                self._event("WARN", "弱市抱团风控拦截 %s(%s): %s" % (s.get('name', ''), code, msg_risk))
                tried.add(code)
                continue
            ok, msg = self._buy(acct, code, s.get('name', ''), px, qty,
                                '[弱市抱团]连板%d·' % h + (f"breadth{breadth:.0%}"))
            if ok:
                bought += 1
                self._event("AUDIT", "[弱市抱团]买入 %s 高度=%d" % (code, h))
        return bought

    # ---------- ★ 开盘自动开启 ----------
    _auto_start_ts = {}      # 记录自动开启状态
    _auto_monitor_started = False
    _auto_pending_day = ""   # ★ H1：非允许窗口时"待明晨自动启动"记录日（防刷屏）

    # ---------- H4：watchdog 心跳分级+节流 ----------
    @staticmethod
    def _wd_get_boot_time():
        """H4：纯标准库获取系统开机时刻（time.time 秒）。
        用 ctypes 调 kernel32.GetTickCount64（系统启动后毫秒数）推算；
        失败返回 0.0（降级：不触发 boot_or_sleep 降级，保持原 CRITICAL 行为）。"""
        try:
            import ctypes as _ct
            _uptime_ms = _ct.windll.kernel32.GetTickCount64()
            return time.time() - _uptime_ms / 1000.0
        except Exception:
            return 0.0

    def _wd_emit_critical(self, now, reason, detail):
        """H4：断更 CRITICAL 节流——首次一条 + 之后每小时一条，带 streak_s。"""
        if self._wd_stale_start is None:
            self._wd_stale_start = now
            self._wd_last_critical_ts = now
            audit.record(kind="daily", event="watchdog_stale",
                         level="CRITICAL", streak_s=0,
                         watchdog_stale_reason=reason,
                         detail=detail + "，首次告警")
        elif now - self._wd_last_critical_ts >= 3600:
            self._wd_last_critical_ts = now
            streak = int(now - self._wd_stale_start)
            audit.record(kind="daily", event="watchdog_stale",
                         level="CRITICAL", streak_s=streak,
                         watchdog_stale_reason=reason,
                         detail=detail + "，持续中（每小时一条）")

    def _wd_emit_recovered_if_needed(self, now):
        """H4：从断更状态恢复时记一条 watchdog_recovered（带 streak_s），并清状态。"""
        if self._wd_stale_start is not None:
            streak = int(now - self._wd_stale_start)
            audit.record(kind="daily", event="watchdog_recovered",
                         level="INFO", streak_s=streak,
                         detail="watchdog 心跳恢复（断更持续 %ds）" % streak)
            self._wd_stale_start = None
            self._wd_last_critical_ts = 0.0

    def _check_watchdog_heartbeat(self):
        """H4：watchdog 心跳检查（分级 + 节流 + 恢复检测）。
        - 心跳 mtime 早于系统开机时刻 → INFO（reason=boot_or_sleep），不记 CRITICAL
        - 断更 >10min → 首次 CRITICAL + 每小时一条（带 streak_s），不再每 10 分钟刷屏
        - 恢复 → 一条 watchdog_recovered（带 streak_s）
        调用间隔约 10 分钟（由 enable_auto_start 控制）；事件字段扩展不破坏 verify_chain。"""
        try:
            import os as _os
            _base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            hb = _os.path.join(_base, "tmp", "watchdog_heartbeat.ts")
            now = time.time()
            # ★ F5：每次重算 boot_time（GetTickCount64 在睡眠期间暂停，
            #   time.time()-uptime 即"最近一次开机/唤醒时刻"）。
            #   旧实现首次计算后缓存 → 昨晚 15:58 睡眠、20:14 唤醒后仍用睡眠前的
            #   boot_time，20:15 心跳 mtime(15:55)>boot_time(12:05) 不命中 2b → 误报
            #   CRITICAL stale（实锤：System 日志 Kernel-Power 42 / Power-Troubleshooter 1）。
            self._wd_boot_time = self._wd_get_boot_time()

            # 1) 心跳缺失：无法判断 boot_or_sleep，仍走 CRITICAL 节流
            if not _os.path.exists(hb):
                self._wd_emit_critical(now, reason="missing",
                                       detail="watchdog 心跳缺失（计划任务未运行）")
                return

            # 2) 心跳存在
            mtime = _os.path.getmtime(hb)
            age = now - mtime

            # 2a) 健康（≤10min）：若之前在断更状态则记恢复
            if age <= 600:
                self._wd_emit_recovered_if_needed(now)
                return

            # 2b) 心跳早于最近开机/唤醒时刻 → 整机曾关机/睡眠，降级为 INFO（不进 CRITICAL 状态机）
            if self._wd_boot_time > 0 and mtime < self._wd_boot_time:
                audit.record(kind="daily", event="watchdog_stale",
                             level="INFO", watchdog_stale_reason="boot_or_sleep",
                             detail="watchdog 心跳早于最近开机/唤醒时刻（%.0f 分钟前），疑似关机/睡眠" % (age / 60.0))
                return

            # 2c) 真正断更：走 CRITICAL 节流
            self._wd_emit_critical(now, reason="stale",
                                   detail="watchdog 断更 %.0f 分钟（>10min）" % (age / 60.0))
        except Exception:
            pass

    # ---------- K4：主循环阶段轨迹 + 停摆判定 ----------
    def _lp_enter(self, phase):
        """K4：进入主循环阶段，记录阶段名与进入时刻。"""
        try:
            self._lp_phase = phase
            self._lp_phase_since = time.time()
        except Exception:
            pass

    def _lp_finish(self, phase):
        """K4：完成阶段，记录耗时与完成时刻。"""
        try:
            now = time.time()
            self._lp_last_finished_phase = phase
            self._lp_last_finished_at = now
            self._lp_last_phase_ms = int((now - self._lp_phase_since) * 1000)
            # F4：相位耗时埋点（内存环形缓冲，聚合由 _lp_latency_flush 每 5 分钟落 audit）
            try:
                self._lp_hist.append((self._lp_last_finished_phase,
                                      self._lp_phase_since, now))
            except Exception:
                pass
            self._lp_phase = "idle"
            self._lp_phase_since = now
        except Exception:
            pass

    def _lp_flush(self):
        """K4：原子落盘 tmp/loop_phase.json（临时名+os.replace，异常静默）。"""
        try:
            import os as _os
            import json as _json
            _base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            data = {
                "phase": self._lp_phase,
                "phase_since": self._lp_phase_since,
                "loop_seq": self._lp_seq,
                "last_finished_phase": self._lp_last_finished_phase,
                "last_finished_at": self._lp_last_finished_at,
                "phase_ms": self._lp_last_phase_ms,
            }
            path = _os.path.join(_base, "tmp", "loop_phase.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False)
            _os.replace(tmp, path)
        except Exception:
            pass

    def _lp_stall_snapshot(self):
        """K4：停摆时抓线程栈快照，落 data/logs/stall_trace_<ts>.txt，保留最近5份。"""
        try:
            import os as _os
            import sys as _sys
            import traceback as _tb
            _base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            log_dir = _os.path.join(_base, "data", "logs")
            _os.makedirs(log_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            # F4：按日期分子目录（data/logs/stall_trace/<YYYYMMDD>/），保留最近
            # STALL_TRACE_KEEP=20 份，避免关键现场被覆盖（原 5 份太薄）。
            _sub = _os.path.join(log_dir, "stall_trace",
                                 time.strftime("%Y%m%d"))
            _os.makedirs(_sub, exist_ok=True)
            fp = _os.path.join(_sub, "stall_trace_%s.txt" % ts)
            lines = ["=== stall snapshot %s ===" % ts,
                     "phase=%s since=%s seq=%s" % (self._lp_phase, self._lp_phase_since, self._lp_seq)]
            frames = _sys._current_frames()
            for t in threading.enumerate():
                lines.append("\n--- thread %s %s (daemon=%s) ---" % (t.ident, t.name, t.daemon))
                fr = frames.get(t.ident)
                if fr:
                    lines.extend(_tb.format_stack(fr))
                else:
                    lines.append("  (frame unavailable)")
            with open(fp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            # 只保留最近 STALL_TRACE_KEEP 份（跨日期子目录按 mtime 排序删最旧）
            try:
                _keep = int(getattr(C, "STALL_TRACE_KEEP", 20))
                files = []
                for _root, _dirs, _fs in _os.walk(_os.path.join(log_dir, "stall_trace")):
                    for _f in _fs:
                        if _f.startswith("stall_trace_") and _f.endswith(".txt"):
                            files.append(_os.path.join(_root, _f))
                files.sort(key=lambda _x: _os.path.getmtime(_x))
                for old in files[:-_keep]:
                    try:
                        _os.remove(old)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    def _lp_latency_flush(self):
        """★ F4（2026-09-13）：相位延迟聚合——每 LP_LATENCY_WINDOW_S=300s 一条
        audit loop_phase_latency（各相位 p50/p95/max/count + 慢阈值相位列表）。
        慢相位级别 WARN + al.notify 告警（同相位 1h 去重，双保险：本表 + notify cooldown）。
        开销：仅当距上次聚合 >= 窗口才计算（每轮调用零成本返回）。"""
        try:
            now = time.time()
            if now - getattr(self, "_lp_lat_ts", 0) < getattr(C, "LP_LATENCY_WINDOW_S", 300):
                return
            self._lp_lat_ts = now
            hist = getattr(self, "_lp_hist", None)
            if not hist:
                return
            cutoff = now - getattr(C, "LP_LATENCY_WINDOW_S", 300)
            by_phase = {}
            for _ph, _s, _e in hist:
                if _s >= cutoff:
                    by_phase.setdefault(_ph, []).append((_e - _s) * 1000)
            if not by_phase:
                return
            thr = getattr(C, "LP_LATENCY_THRESHOLD_S",
                          {"board": 7, "fast_watch": 3, "monitor": 10, "scan": 60})
            per = {}
            slow = []
            for _ph, _ms in by_phase.items():
                _arr = sorted(_ms)
                _n = len(_arr)
                per[_ph] = {"count": _n,
                            "p50_ms": int(_arr[min(_n - 1, int(_n * 0.50))]),
                            "p95_ms": int(_arr[min(_n - 1, int(_n * 0.95))]),
                            "max_ms": int(_arr[-1])}
                _t = thr.get(_ph, 10)
                if per[_ph]["p95_ms"] / 1000.0 > _t:
                    slow.append(_ph)
            audit.record(kind="trading", event="loop_phase_latency",
                         level="WARN" if slow else "INFO",
                         window_s=int(getattr(C, "LP_LATENCY_WINDOW_S", 300)),
                         phases=per, slow_phases=slow,
                         detail=("相位超阈: " + ",".join(slow)) if slow else "相位正常")
            if not hasattr(self, "_lp_alert_ts"):
                self._lp_alert_ts = {}
            for _ph in slow:
                _last = self._lp_alert_ts.get(_ph, 0.0)
                if now - _last >= getattr(C, "LP_ALERT_DEDUP_S", 3600):
                    self._lp_alert_ts[_ph] = now
                    al.notify("WARN", "loop_phase_%s" % _ph,
                              "主循环相位超阈: %s p95=%.1fs > 阈值%.1fs"
                              % (_ph, per[_ph]["p95_ms"] / 1000.0, thr.get(_ph, 10)),
                              "窗口%d秒 样本%d p95=%dms max=%dms"
                              % (int(getattr(C, "LP_LATENCY_WINDOW_S", 300)),
                                 per[_ph]["count"], per[_ph]["p95_ms"], per[_ph]["max_ms"]),
                              cooldown=3600)
        except Exception:
            pass

    def _lp_check_stall(self):
        """K4：停摆判定（由 _monitor 定期调用）。
        阈值：getattr(C, 'LOOP_PHASE_STALL_S', 300)；首次WARN+每小时一条+恢复loop_stall_cleared。"""
        try:
            if not self.running:
                if self._lp_stall_start is not None:
                    self._lp_stall_start = None
                    self._lp_stall_last_warn = 0.0
                return
            # K4b 双保险：idle 是非交易时段的合法停留（_loop 每轮刷新 phase_since），
            # 即使 phase_since 意外冻结也不参与停摆判定；真停摆发生在交易阶段名
            # （account_update/scan/board/exits/monitor/eod_*）上，不受此守卫影响。
            if self._lp_phase == "idle":
                return
            # F4：盘中 90s / 盘外 300s（config 追加块可配）
            try:
                from . import trading_calendar as _tcal
                _is_td = _tcal.is_trading_day(time.strftime("%Y-%m-%d"))
            except Exception:
                _is_td = time.localtime().tm_wday < 5
            _hm = int(time.strftime("%H%M"))
            _in_session = _is_td and 915 <= _hm <= 1505
            threshold = (getattr(C, "LOOP_PHASE_STALL_S_TRADING", 90) if _in_session
                         else getattr(C, "LOOP_PHASE_STALL_S_IDLE", 300))
            now = time.time()
            stuck_for = now - self._lp_phase_since
            if stuck_for > threshold:
                if self._lp_stall_start is None:
                    self._lp_stall_start = now
                    self._lp_stall_last_warn = now
                    streak = int(stuck_for)
                    audit.record(kind="trading", event="loop_stall", level="WARN",
                                 phase=self._lp_phase, since=self._lp_phase_since,
                                 loop_seq=self._lp_seq, streak_s=streak,
                                 detail="主循环停摆：阶段=%s 持续%ss" % (self._lp_phase, streak))
                    self._lp_stall_snapshot()
                elif now - self._lp_stall_last_warn >= 3600:
                    self._lp_stall_last_warn = now
                    streak = int(now - self._lp_stall_start)
                    audit.record(kind="trading", event="loop_stall", level="WARN",
                                 phase=self._lp_phase, since=self._lp_phase_since,
                                 loop_seq=self._lp_seq, streak_s=streak,
                                 detail="主循环停摆持续：阶段=%s 总持续%ss" % (self._lp_phase, streak))
            else:
                if self._lp_stall_start is not None:
                    total = int(now - self._lp_stall_start)
                    audit.record(kind="trading", event="loop_stall_cleared", level="INFO",
                                 phase=self._lp_phase, streak_s=total,
                                 detail="主循环恢复：停摆总时长%ss" % total)
                    self._lp_stall_start = None
                    self._lp_stall_last_warn = 0.0
        except Exception:
            pass

    def enable_auto_start(self, check_interval=15):
        """★ 开盘时间自动开启交易引擎（防 bug 设计）：
        1. 独立守护线程，每 check_interval 秒检查一次，不阻塞主服务
        2. 只在【交易日】且时间到达【9:15 竞价前】自动启动（或已处交易时段立即启动）
        3. 用户手动停止（_user_stopped=True）后当天不再自动重启 —— 尊重手动操作
        4. start() 内部有 running 防重入；自动启动成功后当天只触发一次
        """
        if self._auto_monitor_started:
            return
        self._auto_monitor_started = True

        def _monitor():
            while True:
                try:
                    now = datetime.now()
                    today = now.strftime("%Y-%m-%d")
                    # R2-P1.7：交易日 09:26-09:39 主动抓竞价快照（不依赖用户打开板块；
                    #   否则用户上午不开板块就永远拿 09:40 后的降级评分）。
                    #   auction_board() 窗口内且无快照 → 抓取落盘；已有快照则零成本返回。
                    if self._auction_snap_ts.get(today) is None:
                        try:
                            from . import trading_calendar as _tcal
                            _td = _tcal.is_trading_day(today)
                        except Exception:
                            _td = now.weekday() < 5
                        _hm = now.hour * 100 + now.minute
                        if _td and 926 <= _hm <= 939:
                            self._auction_snap_ts[today] = time.time()
                            try:
                                from . import tactics as _tct
                                _tct.auction_board()
                            except Exception:
                                pass
                    # ★ 修复：手动停止标记仅当天有效，跨日自动清除（否则手动停一次后自动开启永久失效）
                    if self._user_stopped and self._user_stopped_day and self._user_stopped_day != today:
                        self._user_stopped = False
                        self._user_stopped_day = ""
                    if not self.running and not self._user_stopped:
                        # 交易日判断（含节假日/调休）
                        try:
                            from . import trading_calendar as tcal
                            is_td = tcal.is_trading_day(now.strftime("%Y-%m-%d"))
                        except Exception:
                            is_td = now.weekday() < 5
                        if is_td:
                            hm = now.hour * 100 + now.minute
                            today = now.strftime("%Y-%m-%d")
                            # ★ H1 夜间误启动守卫：仅允许 09:10–15:05 窗口内自动启动引擎；
                            #   其它时段（夜间/盘后，如 22:18）不启动，记录"待明晨自动启动"，
                            #   等次日窗口到达再由本守护线程自然启动（_auto_start_ts 按日去重）。
                            if self._auto_start_ts.get(today) is None:
                                if 910 <= hm <= 1505:
                                    # ★ 接口加速：启动引擎前先后台预热日K（并发刷新，避免扫描时
                                    #   400 只逐只触发网络拉K线卡死；预热后扫描全命中缓存秒级完成）
                                    try:
                                        from . import updater
                                        updater.start_background_update()
                                    except Exception:
                                        pass
                                    ok, msg = self.start(auto=True)
                                    self._auto_start_ts[today] = time.time()
                                    if ok:
                                        self._event("OK", f"⏰ 开盘自动开启交易引擎（{now.strftime('%H:%M')}）")
                                    else:
                                        self._event("INFO", f"开盘自动开启跳过: {msg}")
                                else:
                                    # 非 09:10-15:05 窗口：当日记录一次"待明晨自动启动"，不启动引擎
                                    if self._auto_pending_day != today:
                                        self._auto_pending_day = today
                                        try:
                                            self._event(
                                                "INFO",
                                                f"开盘自动开启：{now.strftime('%H:%M')} 非 09:10-15:05 窗口，"
                                                "待明晨自动启动")
                                        except Exception:
                                            pass
                    # R2-P0.3：低频检查 watchdog 心跳（~10 分钟一次）
                    if time.time() - self._last_wd_check_ts >= 600:
                        self._last_wd_check_ts = time.time()
                        try:
                            self._check_watchdog_heartbeat()
                        except Exception:
                            pass
                    # K4：主循环停摆判定（每轮检查，内部节流不刷屏）
                    try:
                        self._lp_check_stall()
                    except Exception:
                        pass
                    # F4：相位延迟聚合（每 LP_LATENCY_WINDOW_S=300s 一条 audit）
                    try:
                        self._lp_latency_flush()
                    except Exception:
                        pass
                except Exception as e:
                    try:
                        self._event("ERROR", f"自动开启检查异常: {e}")
                    except Exception:
                        pass
                time.sleep(check_interval)

        threading.Thread(target=_monitor, daemon=True).start()
        self._event("INFO", "开盘自动开启已启用（交易日 9:15 自动启动引擎）")

    def status(self):
        with self._lock:
            running = self.running
            auto = self.auto
            events = list(self.events)[-50:]
            auction = self.auction
        acct = st.load_account()
        return {
            "running": running, "auto": auto,
            "in_trading_time": _is_trading_time(),
            "in_auction_time": _is_auction_time(),
            "board_enabled": C.BOARD_TRADE_ENABLED,
            "positions": len(acct["positions"]),
            "cash": round(acct["cash"], 2),
            "last_scan": self.last_scan[:20],
            "last_scan_time": self.last_scan_time,
            "last_scan_msg": self.last_scan_msg,
            "last_regime": self.last_regime,      # ★ 4.6 环境分级（前端提示用）
            "last_breadth": self.last_breadth,    # ★ 4.6 市场上涨占比
            "auction": auction,
            "twothirty": self.twothirty,  # v3.4 两点半战法候选
            "watch_events": list(self.watch_events)[-30:],
            "events": events,
            "risk": rk.engine.snapshot(),  # v3.4: 组合风控状态
            "sentiment": self._sentiment_status(),  # 4.0: 市场情绪
        }

    def _sentiment_status(self):
        """情绪快照（含 TTL 缓存，失败返回 None）"""
        try:
            from . import sentiment as senti
            return senti.cached_sentiment(max_age=90)
        except Exception:
            return None

    # ---------- 主循环 ----------
    def _loop(self):
        _last_daily = ""
        # _last_data_upd 收盘更新去重已迁移至 updater.close_update_triggered()（文件持久化，
        # F1/2026-09-02），不再用 _loop 局部变量（多实例失效）。
        while self.running:
            now = datetime.now()
            try:
                try:
                    if _is_auction_time(now):
                        self._lp_enter("auction")
                        self._auction_round()
                        self._lp_finish("auction")
                        time.sleep(C.BOARD_SCAN_INTERVAL if hasattr(C, "BOARD_SCAN_INTERVAL") else 5)
                        continue
                    if not _is_trading_time(now):
                        # v3.5：收盘后触发一次日结推送（15:00-15:30 窗口，每天一次）
                        hm = now.hour * 100 + now.minute
                        today = now.strftime("%Y-%m-%d")
                        if 1500 <= hm <= 1530 and today != _last_daily and C.DAILY_REPORT_ENABLED:
                            _last_daily = today
                            try:
                                self._lp_enter("eod_report")
                                al.daily_report(today)
                                self._lp_finish("eod_report")
                                self._event("OK", "已推送收盘日结")
                            except Exception as e:
                                self._event("ERROR", f"日结推送异常: {e}")
                            # ★ D4：收盘日结附执行滑点日汇总（exit_slippage_daily 事件，
                            #   按 exit_kind 分组 avg_bp/p95/avg_delay_min；失败不阻塞日结）
                            try:
                                from . import performance as _perf
                                _perf.record_daily_exit_slippage(today)
                            except Exception:
                                pass
                        # ★ 4.5：收盘后自动增量更新数据（min5 + 日K，每天一次）
                        if 1510 <= hm <= 1540:
                            try:
                                from . import updater
                                if not updater.close_update_triggered(today):
                                    updater.mark_close_update_triggered(today)
                                    self._lp_enter("eod_update")
                                    updater.start_background_update()
                                    self._lp_finish("eod_update")
                                    self._event("INFO", "收盘数据增量更新已触发")
                            except Exception as e:
                                self._event("ERROR", f"数据更新触发失败: {e}")
                        # 非交易时段：隔30s检查
                        # K4b：每轮刷新 idle 阶段时刻——否则 _lp_phase_since 冻结在
                        # 最后一次 _lp_finish（≈15:00），15:10 起 stuck_for>300s
                        # 必然误报 loop_stall（每交易日 15:05→次日 9:10 约 18h 刷屏）
                        self._lp_enter("idle")
                        time.sleep(30)
                        continue
                    # 交易时段：完整扫描 + 打板轮询 + 自选高频监控
                    # ★ 4.2：每日熔断基准（开盘首次记录总资产）
                    self._lp_enter("account_update")
                    try:
                        acct0 = st.load_account()
                        mv0 = 0.0
                        q0 = df.fetch_quotes_trading(list(acct0["positions"].keys())) if acct0["positions"] else {}
                        for c, p in acct0["positions"].items():
                            mv0 += (q0.get(c, {}).get("price") or p["entry_price"]) * p["qty"]
                        rk.engine.update_daily_pnl(acct0["cash"] + mv0, rk.engine._day_start_total or acct0["cash"] + mv0)
                    except Exception:
                        pass
                    self._lp_finish("account_update")
                    # ★ 4.6：扫描异步化
                    self._lp_enter("scan")
                    self._maybe_launch_scan_async()
                    self._lp_finish("scan")
                    rounds = 10
                    for _ in range(rounds):
                        if not self.running:
                            break
                        if not _is_trading_time():
                            break
                        self._lp_enter("board")
                        self._board_round()
                        self._lp_finish("board")
                        self._lp_enter("exits")
                        self._exits_round()
                        self._lp_finish("exits")
                        self._lp_enter("monitor")
                        self._watch_round()   # ★ 自选股着重扫描（每5秒）
                        self._price_alert_round()   # ★ 4.5 价格提醒（条件单）
                        self._lp_finish("monitor")
                        if _is_twothirty_time():
                            self._lp_enter("twothirty")
                            self._twothirty_round()   # ★ v3.4 两点半战法扫描
                            self._lp_finish("twothirty")
                        # v3.5：心跳（每分钟约1次）
                        if int(time.time()) % 60 == 0:
                            al.heartbeat("trading")
                        time.sleep(5)
                finally:
                    # K4：每轮末原子落盘阶段轨迹（异常静默，绝不影响交易路径）
                    self._lp_flush()
                    self._lp_seq += 1
            except Exception as e:
                self._event("ERROR", f"循环异常: {e}")
                time.sleep(5)

    # ---------- 买入/卖出（真实规则，线程安全） ----------
    def _buy_px(self, q, price, side="buy", qty=None, day_amount=None):
        """★ 4.2：实盘买入/卖出价 = 市值分档滑点（微盘股滑点大）
        ★ 4.7：成交价钳制在涨跌停区间内（防滑点越过涨跌停产生非法成交价）
        ★ P59 审计-D 口径对齐：新口径传【真实委托量 qty】、开 impact 冲击成本、
          amount/day_amount 与 engine.execute_price 对齐（旧口径固定 1000 股假设
          且 impact=False）。当前处于"双价观察期"：新旧两个价格都记 audit
          （event=buy_px_dual），实际采用仍为旧口径；观察一周后由验收方置
          config.BUY_PX_USE_NEW=True 切换为新价。
        异常回退路径保留：任何异常退回固定 C.SLIPPAGE 滑点价。"""
        da_used = None
        try:
            from . import execution as ex
            mcap = q.get("float_mktcap", 0) or 0
            # 旧口径（现行采用）：固定 1000 股假设 + 不开冲击成本
            px_old = ex.execute_price(price, side, mcap,
                                      amount=price * 1000, day_amount=0,
                                      impact=False)
        except Exception:
            px_old = price * (1 + C.SLIPPAGE) if side == "buy" else price * (1 - C.SLIPPAGE)
        try:
            from . import execution as ex
            mcap = q.get("float_mktcap", 0) or 0
            amt = price * float(qty) if qty else price * 1000   # qty 缺省退回旧假设
            da_used = float(day_amount) if day_amount is not None \
                else float(q.get("amount", 0) or 0)
            px_new = ex.execute_price(price, side, mcap,
                                      amount=amt, day_amount=da_used,
                                      impact=True)
        except Exception:
            px_new = px_old
        # ★ P59 新旧双价日志（观察期证据流；两价一致时不刷日志）
        try:
            if abs(px_new - px_old) > 1e-9:
                audit.record("order", "buy_px_dual",
                             code=q.get("code", ""), side=side,
                             base_price=price, old_px=round(px_old, 4),
                             new_px=round(px_new, 4), qty=qty,
                             day_amount=da_used, adopted="old")
        except Exception:
            pass
        use_new = getattr(C, "BUY_PX_USE_NEW", False)
        px = px_new if use_new else px_old
        lu, ld = q.get("limit_up") or 0, q.get("limit_down") or 0
        if lu > 0 and ld > 0:
            px = max(ld, min(lu, px))
        return px

    def _sell_px(self, q, price, qty=None, day_amount=None):
        """★ P2-1 修复：卖出价 = 市值分档滑点（与买入对称，原固定 C.SLIPPAGE）"""
        return self._buy_px(q, price, side="sell", qty=qty, day_amount=day_amount)

    def _buy(self, acct, code, name, price, qty, reason):
        """带规则买入。返回 (ok, msg)"""
        if qty < 100 or price <= 0:
            return False, "数量不足"
        cost = price * qty
        fee = eng.buy_fee(cost)
        if cost + fee > acct["cash"] + 1e-6:
            return False, "现金不足"
        acct["cash"] -= cost + fee
        acct["positions"][code] = {
            "name": name, "qty": qty, "entry_price": price,
            "entry_date": df._today_str(), "peak": price, "days": 0,
            # ★ F2：peak 建仓门控基准——只接受"行情时间 ≥ entry_ts"的观测刷新 peak，
            #   防建仓前早盘高点污染（行情快照 high=当日最高价，无条件采用必被污染）。
            "entry_ts": time.strftime("%H:%M:%S"),
            "score": 0,
            # ★D-S1：策略标签——弱市抱团票卖出后转入"分歧转一致"哨兵池
            "tag": ("WC" if str(reason or "").startswith("[弱市抱团]") else "STD"),
        }
        # ★D-S1：弱市抱团票自买入起即进哨兵池（持仓期一起采，卖出无缝衔接）
        if str(acct["positions"][code].get("tag")) == "WC" and getattr(
                C, "DIVERG_SENTINEL_ENABLE", False):
            self._diverg_watch[code] = time.time() + int(
                getattr(C, "DIVERG_EXPIRE_DAYS", 8)) * 86400
            self._event("AUDIT", "[哨兵登记] %s 持仓期间同步观察(至断板后 8 天)" % code)
        acct["trades"].append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "code": code,
            "name": name, "side": "buy", "price": round(price, 3),
            "qty": qty, "fee": round(fee, 2), "pnl": None, "reason": reason,
        })
        # ★ 任务1-①：策略买入接入统一审计事件流（此前 0 条——成交只写 account.json）。
        #   字段语义对齐 state.manual_buy（kind=order/level=BUY + code/name/
        #   price/qty/fee），另带 strategy=策略名。失败不回滚交易、不抛出。
        try:
            audit.record("order", "strategy_buy", level="BUY", code=code,
                         name=name, price=round(price, 3), qty=int(qty),
                         fee=round(fee, 2),
                         strategy=str(reason or "")[:80])
        except Exception:
            pass
        return True, f"买入 {name}({code}) {price:.2f}x{qty}股 {reason}"

    def _sell(self, acct, code, pos, price, qty, reason, trigger_price=None, trigger_ts=None):
        """带规则卖出（检查T+1/跌停）。返回 (ok, msg, pnl)
        ★ D4（2026-09-13）：执行滑点 KPI——trigger_price=条件触发价（判据价/挂起触发价），
          fill_price=实际成交价（滑点后），slippage_bp=(fill/trigger-1)*10000（负=成本），
          trigger_ts/fill_ts/delay_min 进 strategy_sell 事件 + data/exit_slippage.jsonl。
          缺 trigger 时相关字段 None（兼容既有调用点）。"""
        entry_date = pos.get("entry_date", "")
        if entry_date >= df._today_str():
            return False, "T+1", 0.0
        qty = min(qty, pos["qty"])
        qty = int(qty / 100) * 100
        if qty < 100:
            qty = pos["qty"]
        amount = price * qty
        fee = eng.sell_fee(amount)
        pnl = amount - fee - pos["entry_price"] * qty
        acct["cash"] += amount - fee
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            del acct["positions"][code]
            # ★D-S1：弱市抱团票清仓 → 进入"分歧转一致"哨兵池（采集期后经商核准自动买）
            if str(pos.get("tag") or "") == "WC" and getattr(C, "DIVERG_SENTINEL_ENABLE", False):
                self._diverg_watch[code] = time.time() + int(
                    getattr(C, "DIVERG_EXPIRE_DAYS", 8)) * 86400
                self._event("AUDIT", "[哨兵登记] %s 进入分歧转一致观察池(8天)" % code)
        acct["trades"].append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "code": code,
            "name": pos["name"], "side": "sell", "price": round(price, 3),
            "qty": qty, "fee": round(fee, 2), "pnl": round(pnl, 2),
            "reason": reason,
        })
        # ★ D4：执行滑点 KPI 计算（trigger_price 为空 → 全部 None，不硬算）
        fill_ts = time.strftime("%Y-%m-%d %H:%M:%S")
        slippage_bp = None
        delay_min = None
        if trigger_price and trigger_price > 0:
            slippage_bp = round((price / trigger_price - 1.0) * 10000.0, 1)
        if trigger_ts:
            try:
                def _pd(s):
                    s = str(s)
                    if len(s) == 8 and ":" in s:      # HH:MM:SS → 补当日日期
                        s = time.strftime("%Y-%m-%d ") + s
                    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
                delay_min = round((_pd(fill_ts) - _pd(trigger_ts)) / 60.0, 1)
                if delay_min < 0:
                    delay_min = None   # 跨日/时序异常 → 不硬算
            except Exception:
                delay_min = None
        # ★ C5（2026-09-13）：策略卖出结构化审计事件补全字段（此前只有
        #   code/name/price/qty/fee/pnl/strategy，缺 hold_days/reason/exit_kind）。
        #   字段与 strategy_buy 对称 + 卖出专有字段：hold_days/reason/exit_kind。
        #   出口：strategy_sell（kind=position/level=SELL），供周报/告警机器对拍。
        #   ★ D4 追加：trigger_price/fill_price/slippage_bp/trigger_ts/fill_ts/delay_min。
        try:
            audit.record("position", "strategy_sell", level="SELL",
                         code=code, name=pos["name"],
                         price=round(price, 3), qty=int(qty),
                         fee=round(fee, 2), pnl=round(pnl, 2),
                         hold_days=int(pos.get("days") or 0),
                         reason=str(reason or "")[:80],
                         exit_kind=_exit_kind_of(reason),
                         strategy=str(reason or "")[:80],
                         trigger_price=round(trigger_price, 3) if trigger_price else None,
                         fill_price=round(price, 3),
                         slippage_bp=slippage_bp,
                         trigger_ts=str(trigger_ts or "")[:20] or None,
                         fill_ts=fill_ts, delay_min=delay_min)
        except Exception:
            pass
        # ★ D4：exit_slippage.jsonl（append-only，source=live）
        try:
            self._log_exit_slippage(code, pos["name"], _exit_kind_of(reason), reason,
                                    round(trigger_price, 3) if trigger_price else None,
                                    round(price, 3), slippage_bp, trigger_ts,
                                    fill_ts, delay_min, "live")
        except Exception:
            pass
        return True, f"卖出 {pos['name']}({code}) {price:.2f}x{qty}股 {reason}", pnl

    def _log_exit_slippage(self, code, name, exit_kind, reason, trigger_price,
                           fill_price, slippage_bp, trigger_ts, fill_ts, delay_min, source):
        """★ D4：执行滑点明细落 data/exit_slippage.jsonl（append-only，逐笔一条）。
        source: live=实盘落账 / reconstructed=历史回填（09-07~09-10 验收用）。"""
        try:
            import json as _json
            import os as _os
            row = {"t": fill_ts, "code": code, "name": name, "exit_kind": exit_kind,
                   "reason": str(reason or "")[:80], "trigger_price": trigger_price,
                   "fill_price": fill_price, "slippage_bp": slippage_bp,
                   "trigger_ts": str(trigger_ts or "")[:20] or None,
                   "fill_ts": fill_ts, "delay_min": delay_min, "source": source}
            p = _os.path.join(_os.path.dirname(_os.path.dirname(
                _os.path.abspath(__file__))), "data", "exit_slippage.jsonl")
            with open(p, "a", encoding="utf-8") as f:
                f.write(_json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------- ★ 4.6 扫描异步化 ----------
    def _maybe_launch_scan_async(self):
        """★ 4.6：全市场扫描放后台线程执行（节流 + 防重入），主循环不再被扫描阻塞。
        - 原串行主循环：scan_once(全市场拉行情，网络抖动时数十秒) → 打板/自选/条件单全被拖后
        - 现：扫描在 daemon 线程跑（_scan_lock 防重入），主循环回到 rounds 循环保持 5 秒节奏
        - 崩溃(防守)/弱势等禁买模式同样照常扫描 —— 逆势机会仍产出候选，只是不自动买入
        """
        if not C.SCAN_ASYNC_ENABLED:
            self.scan_once(verbose=False)   # 开关关闭 → 恢复旧串行行为
            return
        now = time.time()
        if now - self._scan_thread_ts < C.SCAN_ASYNC_INTERVAL:
            return   # 节流：距上次启动不足间隔，本轮跳过（防止每 5 秒都开线程）
        if self._scan_thread and self._scan_thread.is_alive():
            return   # 上一轮扫描还没跑完（_scan_lock 外再兜底一次）
        self._scan_thread_ts = now

        def _worker():
            try:
                self.scan_once(verbose=False)
            except Exception as e:
                try:
                    self._event("ERROR", f"后台扫描异常: {e}")
                except Exception:
                    pass

        self._scan_thread = threading.Thread(target=_worker, daemon=True)
        self._scan_thread.start()

    # ---------- 全市场扫描买入 ----------
    def scan_once(self, verbose=True):
        if not self._scan_lock.acquire(blocking=False):
            return self.last_scan
        try:
            stocks = df.fetch_all_stocks(enrich=True)   # ★ P0-3：交易判定路径补齐换手/量比/流通市值+name
            if not stocks:
                self._event("WARN", "扫描：行情获取失败")
                return self.last_scan
            acct = st.load_account()
            # ★ B-2：优先用快链路的近期 breadth（≤75s），否则用本轮重算兜底。
            #   全量 enrich 拉取冷缓存可达 60s+，门禁不必等它。
            breadth = self._fresh_breadth()
            if breadth is None:
                breadth = sc.calc_breadth(stocks)
            regime, max_pos, threshold, pos_pct = sc.get_regime(breadth)
            # ★ 4.0：市场情绪周期（实战派核心，替代单一上涨占比）
            try:
                from . import sentiment as senti
                senti_data = senti.cached_sentiment(max_age=90)
                if not senti_data:
                    # ★ F2（2026-09-09 补）：情绪门禁 fail-closed——拿不到情绪数据 → 禁开仓。
                    #   审计 P1-3：原 fail-open（缺失时默认开仓）与"冰点禁开仓"设计意图相反，
                    #   冷缓存/源抖动日照常开仓。edge-triggered（当日同态只推一次）防刷屏。
                    self.last_scan_msg = "情绪数据缺失，fail-closed 禁开仓"
                    _k2 = "%s|senti-missing" % df._today_str()
                    if self._scan_block_key != _k2:
                        self._scan_block_key = _k2
                        self._event("WARN", self.last_scan_msg)
                    return self.last_scan
                if not senti_data.get("open", True):
                    self.last_scan_msg = f"情绪[{senti_data['phase']}]禁开仓：{senti_data['desc']}"
                    # ★ F2：告警噪音治理——edge-triggered（状态变化才推，同状态当日不重复）。
                    #   实测 2026-09-08 全天真重复 128 条完全相同的"情绪[冰点]禁开仓"
                    #   （09:30:03~11:21:31 约每 50 秒一条）。
                    _key = "%s|%s" % (df._today_str(), senti_data.get("phase", ""))
                    if self._scan_block_key != _key:
                        self._scan_block_key = _key
                        self._event("WARN", self.last_scan_msg)
                    return self.last_scan
                if senti_data["phase"] == "高潮":
                    max_pos = max(max_pos, senti_data.get("max_pos", 3))
                    threshold = max(20, threshold - 5)   # 高潮期放宽阈值
                elif senti_data["phase"] == "发酵":
                    max_pos = min(max_pos, senti_data.get("max_pos", 2))
                elif senti_data["phase"] in ("冰点", "退潮"):
                    # ★ A（2026-09-16）：冰点/退潮不再一刀切 return——禁 GENERIC 买入
                    #   (max_pos=0)，但"弱市抱团核心"条文照跑（回测 R3 样本 80% 胜率、
                    #   +1.53%/笔正是这批天）。仍保留边缘触发的 WARN 文本（去噪）。
                    max_pos = 0
                    self.last_scan_msg = f"情绪[{senti_data['phase']}]禁一般开仓：{senti_data['desc']}（弱市抱团通道未尽）"
            except Exception as e:
                # ★ F2（2026-09-09 补）：情绪门禁异常不再静默——记录 audit 并 fail-closed 禁开仓
                #   （审计 P1-3：原 except: pass 完全静默，门禁整块失效）。
                self.last_scan_msg = "情绪门禁异常，fail-closed 禁开仓"
                _k3 = "%s|senti-error" % df._today_str()
                if self._scan_block_key != _k3:
                    self._scan_block_key = _k3
                    self._event("ERROR", "情绪门禁异常，fail-closed 禁开仓: %s" % str(e)[:120])
                return self.last_scan
            strength = sc.calc_sector_strength(stocks)
            top_sectors = sc.get_top_sectors(strength)
            strong_names = {s[0] for s in top_sectors}
            leaders = set()
            for _, info in top_sectors:
                for c, n, p in info.get("leaders", []):
                    leaders.add(c)

            used = len(acct["positions"])
            self.last_regime = regime
            self.last_breadth = breadth
            if max_pos == 0 or regime in ('弱势(抱团)', '崩溃(防守)'):
                # ★ 4.6：崩溃(防守)/弱势禁买模式不再早退 —— 照常扫描并产出候选供观察。
                # ★ A（2026-09-16）：弱市切抱团——唯一例外通道，只买"当日封板+连板≥3"
                #   的核心票（1 年回测 +111.7%/回撤 -5.6% 依据），仓位过 B-4 风险定容。
                slots = 0
                _nwc = self._weak_core_buys(acct, stocks, used, breadth)
                self.last_scan_msg = (
                    f"{regime}({breadth:.0%}) 禁自动买入，已按[弱市抱团]条文{(f'买入{_nwc}仓' if _nwc else '无符合核心票')}"
                    if _nwc or regime in ('弱势(抱团)', '崩溃(防守)')
                    else f"{regime}({breadth:.0%}) 禁自动买入，仍扫描候选供观察")
                if _nwc:
                    self.last_scan_msg = f"{regime}({breadth:.0%}) [弱市抱团]买入{_nwc}仓，其余禁自动买入"
            elif used >= max_pos:
                self.last_scan_msg = f"{regime} 持仓已满，无需买入"
                return self.last_scan
            else:
                slots = max_pos - used
            focus = set()
            wl = st.load_watchlist()
            focus = {w["code"] for w in wl.get("watchlist", [])}

            candidates = []
            for code, q in stocks.items():
                if code in acct["positions"] or not sc.is_main_board(code):
                    continue
                price = q.get("price", 0) or 0
                if price < 5 or price > 150:
                    continue
                pct = q.get("pct_chg", 0) or 0
                if pct >= C.LIMIT_UP_PCT or pct <= C.LIMIT_DOWN_PCT:
                    continue
                if (q.get("amount", 0) or 0) < 8e7 and code not in focus:
                    continue
                candidates.append((code, q))
            candidates.sort(key=lambda x: x[1].get("amount", 0), reverse=True)
            candidates = candidates[:400]   # ★ 4.5.1：来源放宽到 amount top400（供强度粗筛捞低金额强势股），精评池仍 ≤~130

            # ★ 4.5.1 评分池两级粗筛（quote 硬指标，零网络）：
            #   实测纯按 amount 粗筛会漏掉"今日强度高但成交额靠后"的高分股（300679/300724 漏网），
            #   改为 quote 强度分（涨幅+量比+换手+金额）top80 + 今日平淡高额 top20 + focus + 板块领涨
            def _rough(q):
                pct = q.get("pct_chg", 0) or 0
                vr = q.get("vol_ratio", 0) or 0
                to = q.get("turnover", 0) or 0
                am = q.get("amount", 0) or 0
                return (pct + min(5.0, vr) * 2 + min(20.0, to) * 0.8
                        + min(3.0, (am / 1e7) ** 0.5) * 2)
            candidates.sort(key=lambda x: -_rough(x[1]))
            pool = {c for c, _ in candidates[:80]}
            # B 层：今日平淡（涨幅<1% 且量比<1.5）但成交额大的——覆盖"缩量回踩"类低吸信号
            quiet = [(c, q) for c, q in candidates[80:]
                     if (q.get("pct_chg", 0) or 0) < 1 and (q.get("vol_ratio", 0) or 0) < 1.5]
            quiet.sort(key=lambda x: -(x[1].get("amount", 0) or 0))
            pool |= {c for c, _ in quiet[:20]}
            pool |= focus | leaders   # 自选/板块领涨必精评
            candidates = [(c, q) for c, q in candidates if c in pool]

            buy_list = []
            # ★ I2：实盘 ctx（自选/板块强度/龙头）→ 评分收敛到 score_final 单一入口
            _ctx = {"focus": focus, "strong_sectors": strong_names, "leaders": leaders}
            with _ResilientPool("scan", max_workers=C.PARALLEL_WORKERS) as ex:
                if pools.is_shutting_down():
                    return
                futs = {ex.submit(self._score_one, c, q, _ctx): c for c, q in candidates}
                for f in as_completed(futs):
                    r = f.result()
                    if r:
                        buy_list.append(r)
            buy_list = [b for b in buy_list if b["score"] >= threshold]
            buy_list.sort(key=lambda x: x["score"], reverse=True)
            # ★ 4.5 提速：资金面信号（龙虎榜/两融/北向/席位）从 400 只热循环移出——
            #   只对过阈值的前若干名做深度资金面评估（东财慢/被封时不再拖死扫描），并行执行
            if buy_list:
                # 池大小以可买 slots 驱动（±2 只缓冲）：资金面只影响最终排序权重，无需覆盖全榜
                mf_pool = buy_list[:min(16, max(8, slots * 2 + 2))]
                mf_map = {}
                try:
                    with _ResilientPool("mf", max_workers=min(8, max(1, len(mf_pool)))) as _ex2:
                        if pools.is_shutting_down():
                            return
                        _mf_futs = {_ex2.submit(sc.moneyflow_signals, b["code"], b["name"]): b["code"]
                                    for b in mf_pool}
                        for _f in as_completed(_mf_futs):
                            _code = _mf_futs[_f]
                            try:
                                _bonus, _sig = _f.result()
                                if _bonus:
                                    mf_map[_code] = (_bonus, _sig)
                            except Exception:
                                pass
                    # ★ I2：资金面加分收敛到唯一评分入口 score_final（只重评命中 mf 的票，
                    #   K线/quote 复用 _score_one 缓存，零重读；breakdown 含 moneyflow 项）
                    for _b in buy_list:
                        if _b["code"] in mf_map:
                            _bonus, _sig = mf_map[_b["code"]]
                            _kl = _b.get("_kl")
                            if _kl:
                                _sc, _sg, _bd = sc.score_final(
                                    _kl, _b.get("_quote"), code=_b["code"], name=_b["name"],
                                    ctx=_ctx, mf_bonus=_bonus, mf_signals=_sig)
                                _b["score"], _b["signals"], _b["breakdown"] = _sc, _sg, _bd
                            _b.pop("_kl", None)
                            _b.pop("_quote", None)
                    buy_list.sort(key=lambda x: x["score"], reverse=True)
                except Exception:
                    pass

            self.last_scan = buy_list
            self.last_scan_time = time.strftime("%H:%M:%S")
            if max_pos == 0:
                # ★ 4.6：崩溃(防守)等禁买模式 —— 保留专属提示（候选照常刷新供观察）
                self.last_scan_msg = f"{regime}({breadth:.0%}) 禁自动买入，扫描{len(buy_list)}只候选供观察"
            else:
                self.last_scan_msg = f"{regime}({breadth:.0%}) 达标{len(buy_list)}只"

            bought = 0
            # ★ v3.6：组合优化权重（非 equal 时按风险平价/HRP 分配目标仓位）
            pf_weights = {}
            if C.PORTFOLIO_METHOD != "equal" and buy_list:
                closes_by_code = {}
                for b in buy_list[:slots]:
                    try:
                        kl = df.fetch_kline(b["code"], "day", C.PORTFOLIO_LOOKBACK + 30)
                        if len(kl) >= 30:
                            closes_by_code[b["code"]] = [k["close"] for k in kl]
                    except Exception:
                        continue
                pf_weights = portfolio.compute_weights(
                    closes_by_code, method=C.PORTFOLIO_METHOD,
                    target_vol=C.PORTFOLIO_TARGET_VOL,
                    days=C.PORTFOLIO_LOOKBACK)
            # ★ 4.6：指数择时闸门（实盘，config.INDEX_TIMING_ENABLED 控制；只调仓位，不改变
            #   情绪周期逻辑）；每轮扫描只算一次，乘数与理由记 audit 事件流（kind=signal）供复盘页
            tim_mult = 1.0
            if C.INDEX_TIMING_ENABLED:
                try:
                    from . import index_timing as it
                    tim_mult, tim_reasons = it.timing_multiplier()
                    if tim_mult < 1.0:
                        try:
                            audit.record(kind="signal", event="index_timing", level="INFO",
                                         multiplier=tim_mult,
                                         reasons="；".join(tim_reasons))
                        except Exception:
                            pass
                except Exception:
                    tim_mult = 1.0
            # ★ Phase16：情绪周期仓位闸门（实盘）——每轮扫描算一次，乘数记 audit
            sent_mult = 1.0
            sent_phase_ = None
            if C.SENTIMENT_POS_GATE:
                try:
                    from . import sentiment_gate as sg
                    sent_phase_, sent_mult, _lowc = sg.phase_label(use_live=True)
                    if sent_mult < 1.0:
                        try:
                            audit.record(kind="signal", event="sentiment_gate", level="INFO",
                                         phase=sent_phase_, multiplier=sent_mult)
                        except Exception:
                            pass
                except Exception:
                    sent_mult = 1.0
            with st.transaction() as acct:
                for b in buy_list[:slots]:
                    if b["code"] in acct["positions"]:
                        continue
                    q = stocks.get(b["code"], {})
                    lu, ld = eng.limit_prices(b["code"], q.get("yest_close") or b["price"],
                                              q.get("name", ""))   # ★ 4.3 ST识别
                    w = pf_weights.get(b["code"])
                    # ★ 修复：风险平价权重是组合内占比，直接作单仓比例并受单票上限约束；
                    #   原实现 w*slots clamp 到 1.0 → 满仓单票 → 风控"单票超上限"误拦
                    w_cap = min(1.0, w, C.RISK_SINGLE_STOCK_PCT * 0.95) if w else None  # 留5%余量，防贴边浮点误拦
                    pos_cash = acct["cash"] * (min(w_cap, pos_pct) if w_cap else pos_pct)  # ★ Phase15 修复：position_pct 为单仓预算上限，组合权重不得绕过仓位控制
                    # ★ 4.6：指数择时乘数（提到循环外，每轮扫描只算一次）
                    if tim_mult < 1.0:
                        pos_cash = pos_cash * tim_mult
                    # ★ Phase16：情绪闸门乘数（叠加在 min() 与指数乘数之后，只缩仓不扩仓）
                    if sent_mult < 1.0:
                        pos_cash = pos_cash * sent_mult
                    # ★ P76 修复：pos_cash 必须先于 _buy_px 计算（原顺序致首轮 UnboundLocalError，
                    #   2026-08-27 全天扫描崩溃）；与打板路径 per_stock 先算的模式对齐
                    px = self._buy_px(q, b["price"], "buy",
                                      qty=int(pos_cash / b["price"] / 100) * 100
                                      if pos_cash else None,
                                      day_amount=q.get("amount"))   # ★ P59 真实委托量+impact
                    if px >= lu * 0.999:
                        continue
                    qty = int(pos_cash / px / 100) * 100
                    # ★ B-4：单笔风险定容（止损距离 × 股数 ≤ 权益 1%）
                    qty, _rsz = self._risk_capped_qty(acct, px, qty)
                    if _rsz:
                        self._event("AUDIT", f"风险定容 {b['name']}({b['code']}): {_rsz}")
                    if qty < 100:
                        continue
                    # ★ v3.4：组合风控事前校验（硬风控）
                    ok_risk, msg_risk = rk.engine.check_buy(
                        b["code"], b["name"], px, qty, acct["cash"], acct["positions"],
                        amount=px * qty, regime=regime, quote=q)
                    if not ok_risk:
                        self._event("WARN", f"风控拦截 {b['name']}({b['code']}): {msg_risk}")
                        continue
                    # ★ 4.5：分时量硬过滤（防高位缩量追涨，与打板共用阈值）
                    try:
                        from . import minute_vol as mv
                        mv_ratio, _mv_v = mv.minute_vol_check(b["code"], q)
                        if mv_ratio > 0 and mv_ratio < C.MINUTE_VOL_BOARD_MIN:
                            self._event("WARN", f"分时量拦截 {b['name']}({b['code']}): {_mv_v}（追涨危险）")
                            continue
                        if mv_ratio > C.MINUTE_VOL_BOARD_MAX:
                            self._event("WARN", f"分时量拦截 {b['name']}({b['code']}): {_mv_v}（爆量分歧）")
                            continue
                    except Exception:
                        pass
                    # ★ 4.5：多源信号投票过滤（明确看空时拦截；分歧/看多交给评分体系）
                    try:
                        from . import consensus as cs
                        con = cs.consensus(b["code"], quote=q, name=b["name"])
                        if con and con.get("vote") == "看空" and con.get("shorts", 0) >= 2:
                            self._event("WARN", f"共识投票拦截 {b['name']}({b['code']}): "
                                                f"{con['longs']}多{con['shorts']}空 → 看空")
                            continue
                    except Exception:
                        pass
                    ok, msg = self._buy(acct, b["code"], b["name"], px, qty,
                                        f"[{regime}]评分{b['score']}分: " + "、".join(b["signals"][:3]))
                    if ok:
                        rk.engine.record_buy(px * qty)  # v3.4: 风控记账（日度限额）
                        bought += 1
                        self._event("BUY", msg)
                        self._notify(f"📈 模拟买入 {b['name']}",
                                     f"{b['code']} {px:.2f}x{qty}股\n{b['reason'] if 'reason' in b else ''}\n"
                                     f"评分{b['score']}分：" + "、".join(b["signals"][:5]))
            if verbose or bought:
                self._event("OK", f"扫描完成：{regime} 达标{len(buy_list)} 买入{bought}笔")
            return buy_list
        finally:
            self._scan_lock.release()

    def _score_one(self, code, quote, ctx=None):
        try:
            klines = df.fetch_kline(code, "day", 270)
            if len(klines) < 60:
                return None
            # ★ I2：统一评分入口 score_final（基础分 + ctx 加分：自选/板块抱团/板块龙头；
            #   breakdown 每项可审计对拍）。_kl/_quote 供资金面精评重评复用（零重读）。
            score, signals, _bd = sc.score_final(klines, quote, code=code,
                                                 name=quote.get("name", code), ctx=ctx)
            if score < 20:
                return None
            return {"code": code, "name": quote.get("name", code),
                    "price": quote.get("price", 0), "pct_chg": quote.get("pct_chg", 0),
                    "score": score, "signals": signals, "breakdown": _bd,
                    "_kl": klines, "_quote": quote}
        except Exception:
            return None

    # ---------- 打板轮询 ----------
    def _board_round(self):
        if not C.BOARD_TRADE_ENABLED:
            return 0
        _f3_done = False
        _f3_start = 0.0
        try:
            # ★ 4.0：情绪周期闸门（冰点/退潮不打板）
            try:
                from . import sentiment as senti
                senti_data = senti.cached_sentiment(max_age=90)
                if senti_data.get("phase") in ("冰点", "退潮"):
                    return 0
                if senti_data.get("phase") == "发酵" and senti_data.get("score", 0) < 55:
                    return 0  # 发酵初期情绪不足，谨慎
            except Exception:
                pass
            lst = df.get_stock_list()
            mb = [(c, n, p) for c, n, p in lst if sc.is_main_board(c) and p > 0]
            mb.sort(key=lambda x: x[2], reverse=True)
            top = [c for c, _, _ in mb[:300]]
            stocks = df.fetch_quotes_trading(top, enrich=True)   # ★ P0-3：打板判定需要换手/量比/流通市值+name；F1：交易通道
            acct = st.load_account()
            board_pos = {c: p for c, p in acct["positions"].items() if p.get("board_trade")}
            if len(board_pos) >= C.BOARD_MAX_POSITIONS:
                return 0
            pool = acct["cash"] * C.BOARD_POSITION_RATIO
            per_stock = pool * C.BOARD_PER_STOCK_PCT
            # ★D-S5：热门区间票登记进快环观察表（fast_watch 2s 盯）
            try:
                _pmap = {c: (q.get("pct_chg") or 0) for c, q in stocks.items()}
                self._board_watch_codes = [
                    c for c, _, _ in mb
                    if 7.0 <= (_pmap.get(c) or 0) < C.BOARD_MAX_PCT
                ][: int(getattr(C, "BOARD_HOT_WATCH_N", 20))]
            except Exception:
                self._board_watch_codes = []
            # ★ Phase47+50：指数择时 board 专属（config.INDEX_TIMING_BOARD_ONLY，
            #   全局 INDEX_TIMING_ENABLED 维持 False 不影响 score 路径）。
            #   乘数只作用于 board 开仓预算（只缩不扩）；实盘信号即时点即 PIT
            #   （timing_multiplier 只用 ≤now 的指数 bar）。依据 Phase43 复验。
            if getattr(C, "INDEX_TIMING_BOARD_ONLY", False):
                try:
                    from . import index_timing as it
                    tim_mult, tim_reasons = it.timing_multiplier()
                    if tim_mult < 1.0:
                        pool = pool * tim_mult
                        per_stock = per_stock * tim_mult
                        self._event("INFO", "指数择时(board)×%.2f：%s" % (
                            tim_mult, "；".join(tim_reasons[:2])))
                except Exception as e:
                    self._event("ERROR", f"指数择时(board)异常: {e}")
            if pool < 2000:
                return 0
            cands = []
            for code, q in stocks.items():
                if code in acct["positions"]:
                    continue
                pct = q.get("pct_chg", 0) or 0
                price = q.get("price", 0) or 0
                if not (C.BOARD_MIN_PCT <= pct < C.BOARD_MAX_PCT):
                    continue
                if price < 3 or price > 100:
                    continue
                if (q.get("amount", 0) or 0) < 5e7:
                    continue
                cands.append((code, q))
            if not cands:
                return 0
            cands.sort(key=lambda x: x[1].get("pct_chg", 0), reverse=True)
            # ★ F3（2026-09-13）：打板候选子集强制刷新——绕过 3s 行情 TTL，
            #   拿最新价做"接近涨停"判定；受 F1 交易通道整体 deadline（8s）约束，
            #   且限制在候选子集（≤BOARD_FORCE_REFRESH_MAX=50）以免放大请求量。
            _f3_done = False
            if cands:
                _f3_start = time.time()
                _f3_codes = [c for c, _ in cands[:getattr(C, "BOARD_FORCE_REFRESH_MAX", 50)]]
                try:
                    _f3_fresh = df.fetch_quotes_trading(_f3_codes, enrich=True, force=True)
                    if _f3_fresh:
                        stocks.update(_f3_fresh)   # 判定用最新行情覆盖
                        # 用刷新后行情重筛候选（pct 变化可能退出/进入候选区）
                        cands = []
                        for code, q in stocks.items():
                            if code in acct["positions"]:
                                continue
                            pct = q.get("pct_chg", 0) or 0
                            price = q.get("price", 0) or 0
                            if not (C.BOARD_MIN_PCT <= pct < C.BOARD_MAX_PCT):
                                continue
                            if price < 3 or price > 100:
                                continue
                            if (q.get("amount", 0) or 0) < 5e7:
                                continue
                            cands.append((code, q))
                        cands.sort(key=lambda x: x[1].get("pct_chg", 0), reverse=True)
                    _f3_done = True
                except Exception:
                    _f3_done = False   # 刷新失败不阻塞判定（用首轮行情）
            sec_burst = {}
            for code, q in cands:
                sec = sc.classify_sector(q.get("name", ""), code)
                sec_burst[sec] = sec_burst.get(sec, 0) + 1
            breadth = sc.calc_breadth(stocks)
            # ★ 打板情绪（v3.3）：涨停家数/炸板率
            senti = self._board_sentiment(stocks)
            # ★ 4.1 实时性优化：并行拉K线精评（原来串行 20 只 = 37 秒 → 并行 <1 秒）
            #   先按实时行情硬指标粗筛出 TOP 8，再并行精评，避免为所有候选拉K线
            best = None
            top_cands = cands[:8]   # 精评池缩小：实时指标已筛掉大部分
            klines_map = {}
            if top_cands:
                with _ResilientPool("board", max_workers=C.PARALLEL_WORKERS) as ex:
                    if pools.is_shutting_down():
                        return
                    futs = {ex.submit(df.fetch_kline, c, "day", 250): c for c, _, q in
                            [(x[0], x[1], x[1]) for x in top_cands]}
                    for f in as_completed(futs):
                        try:
                            klines_map[futs[f]] = f.result()
                        except Exception:
                            klines_map[futs[f]] = []
            for code, q in top_cands:
                try:
                    klines = klines_map.get(code) or []
                    if len(klines) < 60:
                        continue
                    score, signals = sc.score_board(klines, q)
                    sec = sc.classify_sector(q.get("name", ""), code)
                    bc = sec_burst.get(sec, 0)
                    if bc >= 2:
                        score += 15; signals.append(f"板块助攻:{sec}(+15,{bc}只)")
                    if breadth >= 0.5:
                        score += 10; signals.append("大盘强势(+10)")
                    elif breadth < 0.2:
                        score -= 15; signals.append("大盘弱势(-15)")
                    # 打板情绪加成
                    if senti:
                        if senti["up_ratio"] >= C.BOARD_SENTIMENT_GOOD:
                            score += 10
                            signals.append(f"打板情绪热(+10,涨停{senti['limit_up']}家)")
                        elif senti["up_ratio"] < C.BOARD_SENTIMENT_BAD:
                            score -= 10
                            signals.append(f"打板情绪冷(-10,涨停{senti['limit_up']}家)")
                        if senti["broken_rate"] > C.BOARD_BROKEN_RATE_MAX:
                            score -= 10
                            signals.append(f"炸板率高(-10,{senti['broken_rate']:.0%})")
                    # ★ 4.0：情绪周期加分（实战体系，替代简单涨停占比）
                    try:
                        sb, ssig = sc.sentiment_bonus(code)
                        if sb:
                            score += sb
                            signals.extend(ssig)
                    except Exception:
                        pass
                    # ★ 4.0：龙头质量打分（带动性/抗跌性/领涨性/资金承接）
                    try:
                        lb, lsig = sc.leader_score(code, q.get("name", ""), q)
                        if lb:
                            score += lb
                            signals.extend(lsig)
                    except Exception:
                        pass
                    if score >= C.BOARD_SCORE_THRESHOLD:
                        item = {"code": code, "name": q["name"], "price": q["price"],
                                "pct_chg": pct, "score": score, "signals": signals}
                        if best is None or score > best["score"]:
                            best = item
                except Exception:
                    continue
            if not best:
                return 0
            # 买入最优
            q = stocks.get(best["code"], {})
            lu, ld = eng.limit_prices(best["code"], q.get("yest_close") or best["price"],
                                      q.get("name", ""))   # ★ 4.3 ST识别
            px = self._buy_px(q, best["price"], "buy",
                              qty=int(per_stock / best["price"] / 100) * 100,
                              day_amount=q.get("amount"))   # ★ P59 真实委托量+impact
            if px >= lu * 0.999:
                return 0
            qty = int(per_stock / px / 100) * 100
            # ★ B-4：单笔风险定容（打板同样适用——实测打板交割单止损 -1k 量级）
            qty, _rsz = self._risk_capped_qty(acct, px, qty)
            if _rsz:
                self._event("AUDIT", f"风险定容(打板) {best['name']}({best['code']}): {_rsz}")
            if qty < 100:
                if _rsz:
                    self._event("AUDIT", "风险定容拦截打板 %s: %s" % (best['name'], _rsz))
                return 0
            cost = px * qty
            if cost > acct["cash"]:
                qty = int(acct["cash"] * 0.95 / px / 100) * 100
                if qty < 100:
                    return 0
            # ★ v3.4：组合风控事前校验（硬风控，与扫描共用）
            ok_risk, msg_risk = rk.engine.check_buy(
                best["code"], best["name"], px, qty, acct["cash"], acct["positions"],
                amount=px * qty, quote=q)
            if not ok_risk:
                self._event("WARN", f"风控拦截打板 {best['name']}({best['code']}): {msg_risk}")
                return 0
            # ★ 4.4 分时量硬过滤：追涨打板前必须分钟量能确认（借鉴 limit-up-sniper 时段量比<0.7 跳过）
            try:
                from . import minute_vol as mv
                mv_ratio, mv_verdict = mv.minute_vol_check(best["code"], q)
                if mv_ratio > 0 and mv_ratio < C.MINUTE_VOL_BOARD_MIN:
                    self._event("WARN", f"分时量拦截打板 {best['name']}({best['code']}): {mv_verdict}（追涨危险）")
                    return 0
                if mv_ratio > C.MINUTE_VOL_BOARD_MAX:
                    self._event("WARN", f"分时量拦截打板 {best['name']}({best['code']}): {mv_verdict}（爆量分歧，谨防对倒）")
                    return 0
            except Exception:
                pass
            # ★ 4.5：共识投票过滤（打板本质是判断分歧，多空对立时谨慎）
            try:
                from . import consensus as cs
                con = cs.consensus(best["code"], quote=q, name=best["name"])
                if con and con.get("vote") == "看空" and con.get("shorts", 0) >= 2:
                    self._event("WARN", f"共识投票拦截打板 {best['name']}({best['code']}): "
                                        f"{con['longs']}多{con['shorts']}空 → 看空")
                    return 0
                if con and con.get("vote") == "分歧" and con.get("divergence", 0) >= 0.75:
                    self._event("WARN", f"共识投票拦截打板 {best['name']}({best['code']}): "
                                        f"分歧度{con['divergence']}（多空激烈，谨慎）")
                    return 0
            except Exception:
                pass
            with st.transaction() as acct:
                if best["code"] in acct["positions"]:
                    return 0
                ok, msg = self._buy(acct, best["code"], best["name"], px, qty,
                                    f"[打板]评分{best['score']}分: " + "、".join(best["signals"][:3]))
                if ok:
                    rk.engine.record_buy(px * qty)  # v3.4: 风控记账
                    acct["positions"][best["code"]]["board_trade"] = True
                    acct["positions"][best["code"]]["entry_pct"] = best.get("pct_chg", 0)
                    self.board_results = 1
                    self._event("BUY", msg)
                    self._notify(f"🎯 模拟打板 {best['name']}",
                                 f"{best['code']} {px:.2f}x{qty}股 +{best.get('pct_chg', 0):.1f}%\n"
                                 + "、".join(best["signals"][:5]))
                    return 1
                return 0
        except Exception as e:
            self._event("ERROR", f"打板轮询异常: {e}")
            return 0
        finally:
            # F3：本轮强制刷新路径的端到端决策延迟（取数发起→判定完成），窗口汇总
            if _f3_done:
                try:
                    self._board_latency_record(time.time() - _f3_start)
                except Exception:
                    pass

    def _board_latency_record(self, latency_s):
        """★ F3（2026-09-13）：打板决策延迟窗口汇总。
        board_decision_latency 事件：每 BOARD_LATENCY_WINDOW_S=300s 且样本≥20 才写一条
        audit（p50/p95/max），不每轮写。latency = 强制刷新取数发起 → 判定完成。"""
        if not hasattr(self, "_f3_lat_samples"):
            self._f3_lat_samples = []
        self._f3_lat_samples.append(latency_s)
        if len(self._f3_lat_samples) < 20:
            return
        now = time.time()
        if now - getattr(self, "_f3_lat_ts", 0) < getattr(C, "BOARD_LATENCY_WINDOW_S", 300):
            return
        self._f3_lat_ts = now
        arr = sorted(self._f3_lat_samples)
        n = len(arr)
        p50 = arr[min(n - 1, int(n * 0.50))]
        p95 = arr[min(n - 1, int(n * 0.95))]
        try:
            audit.record(kind="trading", event="board_decision_latency", level="INFO",
                         window_s=int(getattr(C, "BOARD_LATENCY_WINDOW_S", 300)),
                         n=n, p50_ms=int(p50 * 1000), p95_ms=int(p95 * 1000),
                         max_ms=int(arr[-1] * 1000))
        except Exception:
            pass
        self._f3_lat_samples = []

    # ---------- 打板情绪统计（v3.3） ----------
    def _board_sentiment(self, stocks):
        """涨停家数/炸板率（从行情快照近似）"""
        limit_up = 0
        touched = sealed = 0
        for code, q in stocks.items():
            pct = q.get("pct_chg", 0) or 0
            hi = q.get("high", 0) or 0
            price = q.get("price", 0) or 0
            yc = q.get("yest_close", 0) or 0
            if yc <= 0:
                continue
            lu = yc * (1 + eng.limit_pct_of(code))
            if hi >= lu * 0.999:
                touched += 1
                if price >= lu * 0.999:
                    sealed += 1
            if pct >= 9.8:
                limit_up += 1
        n = max(1, len(stocks))
        return {
            "limit_up": limit_up,
            "up_ratio": limit_up / n,
            "broken_rate": max(0, touched - sealed) / max(1, touched),
        }

    # ---------- 竞价打板公示（9:25-9:30） ----------
    def _auction_round(self):
        try:
            stocks = df.fetch_all_stocks()
            if not stocks:
                return
            cands = []
            for code, q in stocks.items():
                if not sc.is_main_board(code):
                    continue
                op = q.get("open", 0) or q.get("open_price", 0) or 0  # v3.4: 字段名已改为 open
                yc = q.get("yest_close", 0) or 0
                if op <= 0 or yc <= 0:
                    continue
                apct = (op - yc) / yc * 100
                if apct < C.AUCTION_MIN_PCT or apct >= C.AUCTION_MAX_PCT:
                    continue
                if (q.get("amount", 0) or 0) < C.AUCTION_MIN_AMOUNT:
                    continue
                cands.append((code, q))
            sec_burst = {}
            for code, q in cands:
                sec = sc.classify_sector(q.get("name", ""), code)
                sec_burst[sec] = sec_burst.get(sec, 0) + 1
            up = sum(1 for s in stocks.values()
                     if (s.get("open", 0) or s.get("open_price", 0) or 0) > 0 and (s.get("yest_close", 0) or 0) > 0
                     and (s["open"] if s.get("open") else s.get("open_price", 0)) > s["yest_close"])
            tot = sum(1 for s in stocks.values()
                      if (s.get("open", 0) or s.get("open_price", 0) or 0) > 0 and (s.get("yest_close", 0) or 0) > 0)
            ab = up / tot if tot > 0 else 0.5
            results = []
            for code, q in cands:
                try:
                    klines = df.fetch_kline(code, "day", 250)
                    if len(klines) < 20:
                        continue
                    score, signals = sc.score_auction(q, klines, sec_burst, ab)
                    if score >= C.AUCTION_SCORE_THRESHOLD:
                        # ★D-S6c：竞价撤单率（真口径，纯标注不拦交易）。
                        #   <40% 真承接 / >70% 假高开嫌疑 / 其间中性；失败不标注。
                        auc = None
                        try:
                            from . import l2_auction as _l2
                            auc = _l2.auction_cancel(code, force=True)
                        except Exception:
                            auc = None
                        results.append({
                            "code": code, "name": q.get("name", ""),
                            "price": q.get("price", 0),
                            "auction_pct": round((q.get("open", q.get("open_price", 0)) - q["yest_close"]) / q["yest_close"] * 100, 2),
                            "score": score, "signals": signals,
                            "sector": sc.classify_sector(q.get("name", ""), code),
                            "streak": 0,
                            "cancel_ratio": (auc or {}).get("cancel_ratio"),
                            "cancel_peak": (auc or {}).get("peak_vol"),
                            "cancel_dir": (auc or {}).get("peak_dir"),
                        })
                except Exception:
                    continue
            results.sort(key=lambda x: x["score"], reverse=True)
            results = results[:C.AUCTION_TOP_N]
            self.auction = {"results": results, "time": time.strftime("%H:%M:%S"),
                            "breadth": round(ab, 3)}
            # ★ F2：告警噪音治理——一次性事件幂等键（当日只推一次）。
            #   实测 2026-09-08 竞价时段重复推 4 条完全相同的"竞价打板公示：15只候选"
            #   （09:29:35/:41/:48/:55）；候选数随时间变化属正常展示，不重复刷屏。
            if self._auction_notified != df._today_str():
                self._auction_notified = df._today_str()
                self._event("OK", f"竞价打板公示：{len(results)}只候选（高开占比{ab:.0%}）")
        except Exception as e:
            self._event("ERROR", f"竞价扫描异常: {e}")

    # ---------- 两点半战法扫描（v3.4，14:20-14:35） ----------
    _tt_scanned = ""

    def _twothirty_round(self, verbose=True):
        """两点半战法：涨幅2~7% + MACD金叉 + 量比>1（回测结论：次日收盘卖）。
        每5分钟窗口内扫描一次，展示候选；不自动买入（由用户14:30确认后手动/纪律执行）。
        """
        try:
            today = time.strftime("%Y-%m-%d")
            if self._tt_scanned == today:
                return 0  # 当日只扫一次
            stocks = df.fetch_all_stocks()
            if not stocks:
                return 0
            acct = st.load_account()
            cands = []
            for code, q in stocks.items():
                if code in acct["positions"] or not sc.is_main_board(code):
                    continue
                price = q.get("price", 0) or 0
                pct = q.get("pct_chg", 0) or 0
                if price < 5 or price > 100:
                    continue
                if not (2.0 <= pct < 7.0):
                    continue
                if (q.get("amount", 0) or 0) < 5e7:
                    continue
                cands.append((code, q))
            cands.sort(key=lambda x: x[1].get("amount", 0), reverse=True)
            cands = cands[:120]
            results = []
            with _ResilientPool("twothirty", max_workers=C.PARALLEL_WORKERS) as ex:
                if pools.is_shutting_down():
                    return
                futs = {ex.submit(self._tt_score_one, c, q): c for c, q in cands}
                for f in as_completed(futs):
                    r = f.result()
                    if r:
                        results.append(r)
            results.sort(key=lambda x: x["score"], reverse=True)
            results = results[:20]
            self.twothirty = {"results": results, "time": time.strftime("%H:%M:%S"),
                              "msg": f"共{len(results)}只候选"}
            self._tt_scanned = today
            if results:
                self._event("OK", f"两点半战法：{len(results)}只候选（MACD金叉+量比）")
                top = results[0]
                self._notify("⏰ 两点半战法候选",
                             f"{top['name']}({top['code']}) {top['price']:.2f} +{top['pct']:.1f}%\n"
                             + "、".join(top["signals"][:4]) + f"\n共{len(results)}只，详见实盘交易页")
            return len(results)
        except Exception as e:
            self._event("ERROR", f"两点半扫描异常: {e}")
            return 0

    def _tt_score_one(self, code, quote):
        try:
            klines = df.fetch_kline(code, "day", 250)
            if len(klines) < 60:
                return None
            score, signals = sc.score_twothirty(klines, quote)
            if score < 60:
                return None
            return {"code": code, "name": quote.get("name", code),
                    "price": quote.get("price", 0), "pct": quote.get("pct_chg", 0) or 0,
                    "score": score, "signals": signals}
        except Exception:
            return None

    # ---------- 持仓实时退出（v3.3：修复冲高回落误报/重复 + 打板转普通） ----------
    def _pullback_ok(self, pos, entry, peak, cp, pnl_pct):
        """冲高回落止盈判定：
        1. 峰值需超过激活线（+3%）
        2. 从峰值回落 ≥2% 且仍有浮盈
        3. ★ 去重：同一回落事件只触发一次（峰值创新高才重新启用）
        4. ★ 峰值防御：异常行情数据不影响 peak（已在调用方过滤）
        """
        if pos.get("days", 0) < 1:
            return False
        if peak < entry * (1 + C.INTRADAY_HIGH_TRIGGER):
            return False
        if (peak - cp) / peak < C.INTRADAY_PULLBACK:
            return False
        if pnl_pct < 0:
            return False
        last_pk = pos.get("pullback_peak", 0) or 0
        if last_pk and peak <= last_pk * 1.01:
            return False  # 横盘/同一回落事件 → 不重复触发
        pos["pullback_peak"] = peak
        return True

    def _ladder_tp_sell(self, acct, code, pos, cp, entry, pnl_pct):
        """★ 4.6 阶梯止盈（分批卖出）：
        成本涨幅达到 LADDER_TP_STEPS 中未兑现的台阶 → 按台阶比例卖出当前剩余持仓（整手取整）；
        已兑现台阶记录在 pos['ladder_sold']（跨轮询持久化，同一台阶只触发一次）。
        返回 (sold_qty, msg, reason) 或 (0, None, None)。全清线 TAKE_PROFIT_PCT 由常规止盈承接。
        """
        if not C.LADDER_TP_ENABLED or not C.LADDER_TP_STEPS:
            return 0, None, None
        if pos.get("days", 0) < 1:
            return 0, None, None
        if pnl_pct <= 0:
            return 0, None, None
        sold_steps = pos.get("ladder_sold", []) or []
        for i, (thr, frac) in enumerate(C.LADDER_TP_STEPS):
            if i in sold_steps:
                continue  # 该台阶已兑现过
            if pnl_pct < thr:
                continue  # 未达到该台阶涨幅
            qty_total = pos["qty"]
            qty_sell = int(qty_total * frac / 100) * 100
            if qty_sell < 100:
                # 剩余持仓不足一手：视为最后一次兑现，直接清空剩余仓位
                qty_sell = qty_total
            qty_sell = min(qty_sell, qty_total)
            thr_str = f"+{int(round(thr * 100))}%"
            ok, msg, pnl = self._sell(acct, code, pos, cp, qty_sell,
                                      f"阶梯止盈{thr_str}(+{pnl_pct:.1%})",
                                      trigger_price=cp,
                                      trigger_ts=time.strftime("%Y-%m-%d %H:%M:%S"))
            if ok:
                # ★ 4.7 修复：成交后再记账（原在 _sell 前写入，若卖出失败该台阶被白白消耗）
                pos["ladder_sold"] = list(sold_steps) + [i]
                return qty_sell, msg, f"阶梯止盈{thr_str}"
        return 0, None, None

    def _exits_round(self):
        view = st.load_account()
        codes = list(view["positions"].keys())
        if not codes:
            return 0
        # ★SPEED-4：出场巡检不吃 enrich（告警名称走查表兜底）
        quotes = df.fetch_quotes_trading(codes, enrich=False)
        exited = 0
        tx = st.transaction()
        with tx as acct:
            changed = False
            for code in list(acct["positions"].keys()):
                pos = acct["positions"][code]
                q = quotes.get(code, {})
                cp = q.get("price", 0) or pos["entry_price"]
                if not q.get("price"):
                    continue  # 无行情（停牌）
                # ★ F2：T+1 冻结挂起的移动止损/冲高回落 → 解冻后开盘第一笔行情立即执行
                #   （对齐 _sell L739：entry_date < today 即已解冻；竞价结束 09:30 首轮即触发，
                #     不再等下一轮 15 分钟扫描——任务书子任务④）
                if C.TRAILING_NEXT_OPEN_EXEC and pos.get("trail_pending") \
                        and (pos.get("entry_date", "") or "") < df._today_str():
                    try:
                        _pend = pos["trail_pending"]
                        sell_px = self._sell_px(q, cp, qty=pos["qty"],
                                                day_amount=q.get("amount"))
                        ok, msg, pnl = self._sell(
                            acct, code, pos, sell_px, pos["qty"],
                            f"{_pend.get('reason', '移动止损')}(次日开盘执行)",
                            trigger_price=_pend.get("trigger_px"),
                            trigger_ts=_pend.get("trigger_ts"))
                        if ok:
                            pos.pop("trail_pending", None)
                            pos.pop("trail_alert_date", None)
                            exited += 1
                            changed = True
                            self._event("SELL", msg)
                            self._notify(f"📉 模拟卖出 {pos['name']}",
                                         f"{msg}\n盈亏{pnl:+,.0f}")
                            try:
                                self._event(
                                    "KPI",
                                    f"挂起执行滑点 {code} 触发≈{_pend.get('trigger_px')} "
                                    f"成交{sell_px:.3f} "
                                    f"滑点{(sell_px / _pend.get('trigger_px', cp) - 1):+.2%}")
                            except Exception:
                                pass
                            continue
                    except Exception as _e:
                        try:
                            self._event("WARN", f"挂起执行异常 {code}: {_e}")
                        except Exception:
                            pass
                entry = pos["entry_price"]
                pnl_pct = (cp - entry) / entry
                # ★ F2：peak 只用建仓之后的价格（三处口径统一到 eng.update_peak_after_entry）。
                #   行情快照 high=当日最高价（腾讯 f33），无条件刷新会把建仓前早盘高点
                #   记到持仓头（2026-09-08 002011 实例：11:22:45 买入@11.9119 → peak
                #   立刻变 12.54 → 1 秒后误触发"自峰回撤超3%"；5 笔移动止损全亏根因）。
                #   修复：只接受"行情时间 ≥ 建仓时刻"的当前价（tick 级）创新高；
                #   异常值防御（≤ entry*2.0 且 ≥ cp*0.5）保留。
                peak = pos.get("peak", entry) or entry
                if C.TRAILING_ENTRY_GATE:
                    peak = eng.update_peak_after_entry(
                        peak, entry, cp,
                        obs_time=q.get("time", "") or "",
                        entry_ts=pos.get("entry_ts", "") or "",
                        ref_price=cp)
                else:
                    hi = q.get("high", 0) or 0
                    if hi > peak and hi <= entry * 2.0 and hi >= cp * 0.5:
                        peak = hi
                if peak != pos.get("peak"):
                    pos["peak"] = peak
                    changed = True
                # ★ 4.5 修复：持仓天数按【交易日】计算（原用自然日，周五买入周一被算成第3天，
                #   导致 MAX_HOLD_DAYS/TIME_STOP_DAYS 提前 2 天触发）
                try:
                    from . import trading_calendar as tcal
                    entry_d = pos.get("entry_date", df._today_str())
                    pos["days"] = max(0, len(tcal.trading_days(entry_d, df._today_str())) - 1)
                except Exception:
                    pos["days"] = (datetime.now() - datetime.strptime(
                        pos.get("entry_date", df._today_str()), "%Y-%m-%d")).days
                is_board = pos.get("board_trade", False)
                reason = None
                # ★ Phase17：持仓炸板提醒（曾封涨停→现跌破涨停价；只提醒不自动卖，冷却防刷屏）
                if C.BOARD_BREAK_ALERT and is_board and q.get("price"):
                    try:
                        eng2 = eng.limit_prices(code, q.get("yest_close") or cp,
                                                q.get("name", ""))
                        _lu = eng2[0]
                        if _lu > 0 and cp < _lu * (1 - C.BOARD_BREAK_DROP) and \
                                pos.get("peak", 0) >= _lu * 0.99:
                            now_ts = time.time()
                            if now_ts - self._break_alert_ts.get(code, 0) > 900:
                                self._break_alert_ts[code] = now_ts
                                self._event("WARN",
                                            f"⚠️ 炸板提醒 {pos['name']}({code}) "
                                            f"封板价{_lu:.2f}→现{cp:.2f} 打开")
                                self._notify(f"🔔 炸板 {pos['name']}",
                                             f"{code} 涨停打开 {_lu:.2f}→{cp:.2f}"
                                             f"（只提醒，未自动卖出）")
                    except Exception:
                        pass
                # ★D-S3（2026-09-16，验收方）：封单衰减前置预警（十档口径）。
                #   原炸板提醒在"跌破涨停价"才响（慢一步）；此处盯**买一封单量的
                #   萎缩速率 + 十档买比**，在价格还在板上时就提示撤单/卖压风险。
                #   只提醒不自动卖；冷却 15 分钟；腾讯一档源只有 bid1 同样可用。
                try:
                    _lu2 = eng.limit_prices(code, q.get("yest_close") or price,
                                            q.get("name", ""))[0]
                    if (_lu2 > 0):
                        pass
                except Exception:
                    _lu2 = 0
                if (_lu2 > 0 and is_board
                        and (q.get("price") or 0) >= _lu2 * 0.999):
                    try:
                        bq, asv = q.get("bid1_vol", 0) or 0, q.get(
                            "ask1_vol", 0) or 0
                        b5 = sum(q.get("bid%d_vol" % k, 0) or 0 for k in range(1, 6)) or bq
                        a5 = sum(q.get("ask%d_vol" % k, 0) or 0 for k in range(1, 6)) or asv
                        prev = self._depth_decay_last.get(code)
                        key_day = df._today_str()
                        if not prev or prev.get("d") != key_day:
                            prev = {"d": key_day, "q": bq, "ts": time.time()}
                        if prev["q"] > 0 and bq > 0:
                            dec = 1 - (bq / prev["q"])
                            ratio5 = (b5 / (b5 + a5)) if (b5 + a5) > 0 else 0.5
                            now_ts = time.time()
                            if (dec >= getattr(C, "SEAL_DECAY_WARN", 0.4)
                                    and ratio5 < getattr(C, "SEAL_BID_RATIO_WARN", 0.6)
                                    and now_ts - self._diverg_alert_ts.get(
                                        "seal" + code, 0) > 900):
                                self._diverg_alert_ts["seal" + code] = now_ts
                                self._event("WARN",
                                            f"[封单衰减] {pos['name']}({code}) "
                                            f"买一封单萎缩{dec:.0%}，十档买比{ratio5:.0%}"
                                            f"(≥封单量{b5/10000:.1f}万手) 谨防炸板")
                        self._depth_decay_last[code] = {
                            "d": key_day,
                            "q": max(bq, (prev or {}).get("q", bq) or 0),
                            "ts": time.time()}
                    except Exception:
                        pass
                # 打板规则（T+1 后生效；次日过后转普通持仓）
                if is_board:
                    if pos["days"] >= 2:
                        pos["board_trade"] = False
                        is_board = False
                        changed = True
                    elif pos["days"] >= 1:
                        if pnl_pct <= C.BOARD_NEXT_DAY_LOW:
                            reason = f"打板低开止损({pnl_pct:+.1%})"
                        elif pnl_pct >= 0.05:
                            half = pos["qty"] // 200 * 100
                            if half >= 100:
                                ok, msg, pnl = self._sell(acct, code, pos, cp, half,
                                                          f"打板半仓止盈({pnl_pct:+.1%})",
                                                          trigger_price=cp,
                                                          trigger_ts=time.strftime("%Y-%m-%d %H:%M:%S"))
                                if ok:
                                    self._event("SELL", msg)
                                    self._notify(f"📉 模拟止盈 {pos['name']}", msg)
                                    exited += 1
                                    changed = True
                            continue  # 剩余半仓由常规规则接管（次日转普通）
                # 常规退出规则
                # ★ 4.6 阶梯止盈（附加分支，不与其他退出规则互斥）：达到台阶即分批兑现，
                #   剩余仓位继续走全清止盈/回撤/移动止损/时间止损/超时
                if C.LADDER_TP_ENABLED and pos["qty"] > 0 and pnl_pct > 0:
                    _ls_qty, _ls_msg, _ls_why = self._ladder_tp_sell(
                        acct, code, pos, cp, entry, pnl_pct)
                    if _ls_qty > 0:
                        exited += 1
                        changed = True
                        self._event("SELL", _ls_msg)
                        self._notify(f"📉 模拟止盈 {pos['name']}", _ls_msg)
                        continue  # 部分卖出后本轮结束该股，剩余仓位下轮再评估
                # ★ 4.6 盘中量价背离卖出（秒级实时 + 分钟量比，不依赖日K，config.VOLP_SELL_MIN_ENABLED）
                #   放量滞涨→清仓 / 高位缩量上冲→卖半仓；对每只持仓每 5 秒用实时 quote 判定一次
                if C.VOLP_SELL_MIN_ENABLED and pos["qty"] > 0 and pos["days"] >= 1:
                    try:
                        from . import minute_vol as _mv
                        _mvk, _mvratio, _mvr = _mv.minute_volp_sell(code, q)
                        if _mvk == "min_surge_stall":
                            reason = _mvr          # 对倒派发 → 清仓
                            changed = True
                        elif _mvk == "min_shrink_rise" and not pos.get("volp_min_half_done"):
                            # ★ 4.7 修复：标志只挡半仓分支（原挡整块 → 半仓后放量滞涨清仓永久失效）
                            _hqty = int(pos["qty"] * C.VOLP_SELL_HALF_PCT / 100) * 100
                            if _hqty >= 100:
                                _hk, _hmsg, _hpnl = self._sell(
                                    acct, code, pos, cp, min(_hqty, pos["qty"]), _mvr,
                                    trigger_price=cp,
                                    trigger_ts=time.strftime("%Y-%m-%d %H:%M:%S"))
                                if _hk:
                                    pos["volp_min_half_done"] = True   # ★ 成交后再置标志（原 T+1 拒单也会白白消耗）
                                    changed = True
                                    exited += 1
                                    self._event("SELL", _hmsg)
                                    self._notify(f"📉 量价减仓 {pos['name']}", _hmsg)
                                    continue  # 剩余半仓下轮再评估
                    except Exception:
                        pass
                if not reason and pos["qty"] > 0:
                    # ★ v3.4 两点半战法：次日收盘卖（依据一年期回测结论）
                    if pos.get("twothirty") and pos["days"] >= 1:
                        reason = f"两点半战法:次日收盘卖({pnl_pct:+.1%})"
                    elif pnl_pct <= C.STOP_LOSS_PCT:
                        reason = "止损"
                    elif pnl_pct >= C.TAKE_PROFIT_PCT:
                        reason = "止盈"
                    if not reason and self._pullback_ok(pos, entry, peak, cp, pnl_pct):
                        reason = f"冲高回落止盈(峰{peak:.2f}→{cp:.2f})"
                        changed = True
                    elif not reason and (peak >= entry * (1 + C.TRAILING_ACTIVATE_PCT)
                          and cp <= peak * (1 + C.TRAILING_STOP_PCT)):
                        reason = f"移动止损(峰{peak:.2f})"
                    elif pos["days"] >= C.TIME_STOP_DAYS and pnl_pct < 0:
                        reason = f"时间止损({pos['days']}天亏{pnl_pct:+.1%})"
                    elif pos["days"] >= C.MAX_HOLD_DAYS:
                        reason = f"超时退出({pos['days']}天)"
                if reason:
                    lu, ld = eng.limit_prices(code, q.get("yest_close") or cp)
                    if cp <= ld * 1.001:
                        self._event("WARN", f"{pos['name']}({code}) 跌停卖不出，跳过")
                        continue
                    # ★ F2：T+1 冻结治理——移动止损/冲高回落当日触发但买入当日不可卖
                    #   （对齐 _sell L739 判据 entry_date >= today）。此前纯告警空转：
                    #   今日同标的同条件重复推 10 次、account.json 一条都没执行。
                    #   现在：当日去重 + 挂起（trail_pending），次日开盘第一笔行情执行。
                    _today = df._today_str()
                    _t1_frozen = (pos.get("entry_date", "") or "") >= _today
                    if _t1_frozen and ("移动止损" in reason or "冲高回落" in reason):
                        if not (C.TRAILING_ALERT_DEDUP
                                and pos.get("trail_alert_date") == _today):
                            if C.TRAILING_ALERT_DEDUP:
                                pos["trail_alert_date"] = _today
                            pos["trail_pending"] = {
                                "reason": reason.split("(")[0],
                                "trigger_px": round(cp, 3),
                                "peak": round(peak, 3),
                                "trigger_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                            changed = True
                            self._event("WARN",
                                        f"🔒 {pos['name']}({code}) T+1 冻结，"
                                        f"{reason.split('(')[0]}将于次日开盘执行"
                                        f"（触发价 {cp:.2f}，现价 {cp:.2f}）")
                        continue
                    # ★ P2-1 修复：卖出也用市值分档滑点（原固定 C.SLIPPAGE，与买入不对称）
                    sell_px = self._sell_px(q, cp, qty=pos["qty"],
                                            day_amount=q.get("amount"))   # ★ P59 真实委托量+impact
                    ok, msg, pnl = self._sell(acct, code, pos, sell_px,
                                              pos["qty"], reason,
                                              trigger_price=cp,
                                              trigger_ts=time.strftime("%Y-%m-%d %H:%M:%S"))
                    if ok:
                        exited += 1
                        changed = True
                        self._event("SELL", msg)
                        self._notify(f"📉 模拟卖出 {pos['name']}", f"{msg}\n盈亏{pnl:+,.0f}")
                        # ★ F2 KPI：止损触发价 vs 实际成交价滑点写 audit
                        #   （实测 3~6% 属异常；触发≈判据价 cp，成交=sell_px 滑点后价）
                        if "止损" in reason or "移动" in reason or "冲高" in reason:
                            try:
                                self._event(
                                    "KPI",
                                    f"止损滑点 {code} 触发≈{cp:.3f} 成交{sell_px:.3f} "
                                    f"滑点{(sell_px / cp - 1):+.2%}")
                            except Exception:
                                pass
                        # ★ 4.2：连亏计数（风控冷却）
                        try:
                            rk.engine.record_trade_result(pnl)
                        except Exception:
                            pass
            tx.auto_save = changed
        return exited

    # ---------- 自选股高频异动监控（v3.3 着重扫描） ----------
    _watch_hint_ts = {}

    def _watch_hint(self, code, kind, msg, hits, now, title="", level=None):
        """异动信号去重冷却（每类15分钟）"""
        ts_map = self._watch_hint_ts.setdefault(code, {})
        if now - ts_map.get(kind, 0) < 900:
            return
        ts_map[kind] = now
        hits.append({"code": code, "kind": kind, "msg": msg,
                     "level": level or {"拉升": "OK", "大跌": "WARN", "回落": "WARN"}.get(kind, "INFO"),
                     "title": title or kind, "t": time.strftime("%H:%M:%S")})

    def _watch_round(self):
        """自选/关注股高频扫描：快速拉升/大跌/冲高回落异动（交易时段每5秒）"""
        wl = st.load_watchlist()
        codes = [w["code"] for w in wl.get("watchlist", [])]
        if not codes:
            return []
        # ★SPEED-4：自选 5s 巡检同样不吃 enrich（名称走缓存查表）
        quotes = df.fetch_quotes_trading(codes, enrich=False)
        try:
            _nm = {c: n for c, n, _p in df.get_stock_list()}
        except Exception:
            _nm = {}
        hits = []
        now = time.time()
        for code, q in quotes.items():
            pct = q.get("pct_chg", 0) or 0
            price = q.get("price", 0) or 0
            hi = q.get("high", 0) or 0
            name = q.get("name") or _nm.get(code) or code
            # ★ 修复：判断是否封死涨停（价格≈涨停价 → 买不进去，不提示"打板机会"）
            sealed = False
            try:
                lu, _ld = eng.limit_prices(code, q.get("yest_close") or price,
                                           q.get("name", ""))
                sealed = price > 0 and lu > 0 and price >= lu * 0.999
            except Exception:
                pass
            if pct >= 7.0:
                if sealed:
                    # 封死涨停：买不进，提醒要换措辞且降低频率（合并到拉升冷却）
                    self._watch_hint(code, "封板", f"🔒 {name}({code}) 封死涨停{pct:+.1f}%，排队难成交（勿追）", hits, now, "封板提示", level="INFO")
                else:
                    self._watch_hint(code, "拉升", f"🔥 {name}({code}) 涨幅{pct:+.1f}%，接近涨停，关注打板机会", hits, now, "快速拉升")
            elif pct >= 5.0:
                vr_now = q.get("vol_ratio", 0) or 0
                vp_tag = ""
                if vr_now >= C.VP_SURGE_GOOD:
                    vp_tag = f"，量能突变{vr_now:.1f}x"
                elif vr_now >= 1.5:
                    vp_tag = f"，放量{vr_now:.1f}x"
                self._watch_hint(code, "拉升", f"📈 {name}({code}) 快速拉升{pct:+.1f}%{vp_tag}", hits, now, "快速拉升")
            if pct <= -3.0:
                self._watch_hint(code, "大跌", f"🚨 {name}({code}) 下跌{pct:+.1f}%，注意风险", hits, now, "大跌预警")
            if hi > 0 and price > 0 and hi > price:
                rev = (hi - price) / hi
                if rev >= 0.03 and pct < 0:
                    self._watch_hint(code, "回落", f"⚠️ {name}({code}) 冲高回落{(hi-price)/price*100:.1f}%（高点{hi:.2f}）", hits, now, "冲高回落")
            # ★ 4.4 分时量预警：冲高回落/追涨打板风险（分钟量能确认，零网络开销）
            try:
                from . import minute_vol as mv
                vsig, vlev = mv.vp_minute_signal(code, q)
                if vsig:
                    self._watch_hint(code, "分时量", f"📊 {name}({code}) {vsig}", hits, now, "分时量预警", level=vlev)
            except Exception:
                pass
        for h in hits:
            self._event(h["level"], h["msg"])
            self._notify(f"🔔 {h['title']}：{h['code']}", h["msg"])
            with self._lock:
                self.watch_events.append(h)
            # ★ v3.8：AI 异动点评（后台线程，不阻塞轮询；LLM 未配置时自动跳过）
            try:
                self._ai_comment(h, quotes.get(h["code"], {}))
            except Exception:
                pass
        return hits

    _ai_comment_ts = {}

    # ---------- ★ Phase51：持仓+预选清单高频监控（独立守护线程，只告警不下单） ----------
    _fast_watch_hint_ts = {}   # {code: {kind: ts}} 冷却（每类 15 分钟）
    # F2（2026-09-08）：告警噪音治理状态位（edge-triggered / 当日幂等）
    _scan_block_key = ""       # 情绪[冰点]禁开仓：状态键（日期|phase），变化才推
    _auction_notified = ""     # 竞价打板公示：当日幂等键（日期），当天只推一次

    def _fast_watch_hint(self, code, kind, msg, level="WARN"):
        now = time.time()
        ts_map = self._fast_watch_hint_ts.setdefault(code, {})
        # ★ F2：去重键升级为 kind|当日 —— 原 15 分钟冷却跨小时挡不住重复推
        #   （实测同标的同条件当日重复推 10 次：11:22/13:00/13:15/…/15:00）。
        #   同一 (标的, 条件, 交易日) 当日只推一次。
        _k = "%s|%s" % (kind, df._today_str())
        if now - ts_map.get(_k, 0) < 900:
            return
        ts_map[_k] = now
        h = {"code": code, "kind": kind, "msg": msg, "level": level,
             "title": "高频监控", "t": time.strftime("%H:%M:%S")}
        self._event(level, h["msg"])
        self._notify(f"⚡ {h['title']}：{code}", h["msg"])
        with self._lock:
            self.watch_events.append(h)

    def _fast_watch_loop(self):
        """★ Phase51：持仓+预选清单秒级监控守护线程（回应"封板几秒内完成，
        系统能否秒级反应"）。主循环全池 5 秒轮询不变；本线程仅盯
        持仓 ∪ 自选 ∪ 最近扫描候选（≤30 只），默认 2 秒/次拉实时快照
        （config.FAST_WATCH_INTERVAL，接口限流实测后再定最终频率），检测：
          a) 触板后回落 ≥ BOARD_BREAK_DROP（封板不牢）
          b) 持仓票触及移动止损 / 冲高回落条件
        只产出告警事件（watch_events + 推送），不自动下单。
        异常完全隔离（照抄 _loop 的 try/except 风格）。"""
        interval = max(1, int(getattr(C, "FAST_WATCH_INTERVAL", 2)))
        while self.running:
            _fw_done = False
            try:
                if not _is_trading_time():
                    time.sleep(15)
                    continue
                acct = st.load_account()
                codes = list(acct["positions"].keys())
                for s in (self.last_scan or [])[:10]:
                    c = s.get("code")
                    if c and c not in codes:
                        codes.append(c)
                try:
                    for w in (st.load_watchlist().get("watchlist") or []):
                        c = w.get("code")
                        if c and c not in codes:
                            codes.append(c)
                except Exception:
                    pass
                # ★D-S5：打板热窗 → 候选注入快环（2s 级事件流，非 5s 轮询等值）
                try:
                    import datetime as _dt
                    hm0 = _dt.datetime.now().strftime("%H:%M")
                    for _w0, _w1 in getattr(C, "BOARD_HOT_WINDOWS", ()):
                        if _w0 <= hm0 <= _w1:
                            for c in getattr(self, "_board_watch_codes", []):
                                if c not in codes:
                                    codes.append(c)
                            break
                except Exception:
                    pass
                codes = codes[:30]
                if not codes:
                    time.sleep(interval)
                    continue
                _fw_t0 = time.time()
                _fw_done = True
                # ★SPEED-4：快环停征 enrich 附加费（30只 249ms→~60ms：换手/市值/名称
                #   补全砍掉，告警只吃价/涨跌/五档）；名称走 6h 缓存的全股列表兜底。
                quotes = df.fetch_quotes_trading(codes, enrich=False)
                try:
                    _nm = {c: n for c, n, _p in df.get_stock_list()} \
                        if quotes else {}
                except Exception:
                    _nm = {}
                for code, q in quotes.items():
                    pos = acct["positions"].get(code)
                    price = q.get("price", 0) or 0
                    high = q.get("high", 0) or 0
                    name = q.get("name") or _nm.get(code) or code
                    if price <= 0 or high <= 0:
                        continue
                    # a) 触板后回落（监控全集都盯；封板几秒内完成 → 快照要密）
                    try:
                        lu, _ld = eng.limit_prices(code,
                                                   q.get("yest_close") or price,
                                                   name)
                        if lu > 0 and high >= lu * 0.995 \
                                and price < lu * (1 - C.BOARD_BREAK_DROP):
                            drop = (lu - price) / lu * 100
                            self._fast_watch_hint(
                                code, "触板回落",
                                f"⚠ {name}({code}) 触板后回落：现价{price:.2f} 距涨停"
                                f"{drop:.1f}%（≥{C.BOARD_BREAK_DROP:.1%}），封板不牢谨防炸板")
                    except Exception:
                        pass
                    if not pos:
                        continue
                    entry = pos.get("entry_price") or 0
                    # ★ F2：peak 只用建仓后观测（同 _exits_round 口径；行情 high=当日最高，
                    #   无条件采用会把建仓前早盘高点计入 → 移动止损虚假触发）。
                    peak = pos.get("peak") or entry
                    if C.TRAILING_ENTRY_GATE:
                        peak = eng.update_peak_after_entry(
                            peak, entry, price,
                            obs_time=q.get("time", "") or "",
                            entry_ts=pos.get("entry_ts", "") or "",
                            ref_price=price)
                    else:
                        if high > peak:
                            peak = high
                    pnl_pct = (price - entry) / entry if entry else 0.0
                    # ★ F2：T+1 冻结判定（对齐 _sell L739：entry_date >= today 不可卖）
                    _t1 = (pos.get("entry_date", "") or "") >= df._today_str()
                    # b1) 移动止损条件：峰值达激活线后自高点回撤越线
                    if (entry > 0 and peak >= entry * (1 + C.TRAILING_ACTIVATE_PCT)
                            and price <= peak * (1 + C.TRAILING_STOP_PCT)):
                        if _t1:
                            # ★ F2：T+1 冻结 → 只告警一次，次日开盘执行（主循环已挂起）
                            self._fast_watch_hint(
                                code, "移动止损",
                                f"🔒 {name}({code}) T+1 冻结，移动止损将于次日开盘执行"
                                f"（触发价 {price:.2f}，现价 {price:.2f}）",
                                level="SELL")
                        else:
                            self._fast_watch_hint(
                                code, "移动止损",
                                f"🛑 {name}({code}) 触发移动止损条件：现价{price:.2f} 自峰"
                                f"{peak:.2f}回撤超{abs(C.TRAILING_STOP_PCT):.0%}（收益{pnl_pct:+.1%}）",
                                level="SELL")
                    # b2) 冲高回落条件：达日内冲高触发线后回落超阈且仍盈利
                    elif (entry > 0 and high >= entry * (1 + C.INTRADAY_HIGH_TRIGGER)
                          and (high - price) / high >= C.INTRADAY_PULLBACK
                          and pnl_pct >= 0):
                        if _t1:
                            # ★ F2：T+1 冻结 → 次日开盘执行
                            self._fast_watch_hint(
                                code, "冲高回落",
                                f"🔒 {name}({code}) T+1 冻结，冲高回落将于次日开盘执行"
                                f"（触发价 {price:.2f}，现价 {price:.2f}）",
                                level="SELL")
                        else:
                            self._fast_watch_hint(
                                code, "冲高回落",
                                f"📉 {name}({code}) 冲高回落：高点{high:.2f}→{price:.2f}"
                                f"（回落{(high - price) / high:.1%}，收益{pnl_pct:+.1%}）")
            except Exception as e:
                try:
                    self._event("ERROR", f"高频监控异常: {e}")
                except Exception:
                    pass
            if _fw_done:
                try:
                    self._lp_hist.append(("fast_watch", _fw_t0, time.time()))
                except Exception:
                    pass
            try:
                self._diverg_sentinel_step()   # ★D-S1 哨兵（节流自控 15s/次）
            except Exception as e:
                try:
                    self._event("ERROR", f"哨兵异常: {e}")
                except Exception:
                    pass
            time.sleep(interval)

    def _ai_comment(self, h, quote):
        """用 LLM 对异动生成一句话点评（借鉴 xuanji AI 复盘；复用 review.llm 通道）"""
        key = h["code"] + h["kind"]
        now = time.time()
        if now - self._ai_comment_ts.get(key, 0) < 1800:  # 30分钟冷却
            return
        self._ai_comment_ts[key] = now
        from . import review as rv
        cfg = st.load_llm_config()
        if not (cfg.get("ollama_model") or (cfg.get("api_key") and cfg.get("model"))):
            return  # 未配置 LLM，跳过
        prompt = (
            f"股票 {quote.get('name','')}({h['code']}) 盘中异动：{h['msg']}。"
            f"当前价 {quote.get('price',0):.2f}，涨跌 {quote.get('pct_chg',0):+.1f}%，"
            f"换手 {quote.get('turnover',0):.1f}%，量比 {quote.get('vol_ratio',0):.2f}。"
            f"请用一句话点评该异动的成因与是否值得关注（30字内）。"
        )
        def _run():
            try:
                if cfg.get("ollama_model"):
                    rep = rv._ollama_chat(cfg["ollama_model"], prompt,
                                          cfg.get("ollama_url", "http://127.0.0.1:11434"))
                else:
                    rep = rv._openai_chat(cfg["api_key"], cfg["model"], prompt,
                                          cfg.get("api_base", "https://api.deepseek.com/v1"))
                if rep:
                    h["ai"] = rep.strip()
                    self._event("INFO", f"🤖 {h['code']} AI点评: {h['ai']}")
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    # ---------- ★ 4.5 价格提醒（条件单） ----------
    # ---------- ★D-S1 分歧转一致哨兵（Phase-1：只采集+告警，不自动买） ----------
    def _diverg_sentinel_step(self):
        """哨兵流水：哨兵池（弱市抱团票买入起/卖出后 8 天）15 秒/次，通达信五档+内外盘。
        数据落 data/logs/diverg_watch.jsonl（Phase-2 用真实大单序列回测判据）。
        触发告警判据（预注册）：外盘增量占比≥0.6 且 五档买卖失衡≥0.55 且 距涨停≤2%。"""
        if not getattr(C, "DIVERG_SENTINEL_ENABLE", False):
            return
        now = time.time()
        if now - self._diverg_ts < max(5, int(getattr(C, "DIVERG_INTERVAL", 15))):
            return
        self._diverg_ts = now
        for c, e in list(self._diverg_watch.items()):
            if e <= now:
                try:
                    del self._diverg_watch[c]
                except Exception:
                    pass
        try:
            acct = st.load_account()
            for c, p in (acct.get("positions") or {}).items():
                if str(p.get("tag") or "") == "WC" and c not in self._diverg_watch:
                    self._diverg_watch[c] = now + int(
                        getattr(C, "DIVERG_EXPIRE_DAYS", 8)) * 86400
        except Exception:
            pass
        if not self._diverg_watch:
            return
        codes = list(self._diverg_watch.keys())[:80]
        try:
            from . import tdx as tdx_mod
            quotes = tdx_mod.fetch_quotes_fast(codes) if tdx_mod.available() else None
        except Exception:
            quotes = None
        if not quotes:
            try:
                # ★SPEED-4：哨兵兜底源同样只取裸行情
                quotes = df.fetch_quotes_trading(codes, enrich=False)
            except Exception:
                return
        if not quotes:
            return
        log_path = "data/logs/diverg_watch.jsonl"
        import json as _jj
        for code, q in quotes.items():
            c = code[2:] if code[:2] in ("sz", "sh", "SH", "SZ") else code
            o, i = q.get("outer_vol", 0) or 0, q.get("inner_vol", 0) or 0
            last = self._diverg_last.get(c)
            if last and last.get('ymc') != (q.get("yest_close") or 0):
                last = None          # 跨日重置基线
            do = o - last['o'] if last else 0
            di = i - last['i'] if last else 0
            self._diverg_last[c] = {'o': o, 'i': i, 'ymc': q.get("yest_close") or 0}
            bid_sum = sum(q.get("bid%d_vol" % k, 0) or 0 for k in range(1, 6))
            ask_sum = sum(q.get("ask%d_vol" % k, 0) or 0 for k in range(1, 6))
            bmr = bid_sum / (bid_sum + ask_sum) if (bid_sum + ask_sum) > 0 else 0.5
            yc = q.get("yest_close") or 0
            price = q.get("price") or 0
            gap = max(((yc * 1.095) - price) / price if price > 0 else 1.0, 0)
            pct = q.get("pct_chg") or 0
            tot = abs(do) + abs(di)
            odr = (do / (do + di)) if (do + di) > 0 else (0.5 if tot == 0 else 0.0)
            rec = {"t": time.strftime("%H:%M:%S"), "code": c,
                   "price": price, "pct": pct,
                   "outer": o, "inner": i, "d_outer": do, "d_inner": di,
                   "od_ratio": round(odr, 3), "bid_ratio5": round(bmr, 3),
                   "gap_limit": round(gap, 4), "trigger": 0}
            # ★D-S6b：分钟级主力净流（东财 push2 WAF 封禁后 → eltdx 逐笔大单净额主源）
            try:
                from . import l2_auction as _l2
                if (self._sentinel_ff_ts.get("l2" + c, 0) or 0) < now - 55:
                    self._sentinel_ff_ts["l2" + c] = now
                    _mf = _l2.main_flow_1m(c)
                    if _mf:
                        rec["l2_m1_net"] = _mf.get("m1_net")
                        rec["l2_m1_n"] = _mf.get("m1_count")
            except Exception:
                pass
            trig = (odr >= float(getattr(C, "DIVERG_DELTA_OUTER_RATIO", 0.6))
                    and bmr >= float(getattr(C, "DIVERG_BID_ASK_RATIO", 0.55))
                    and 0 <= gap <= float(getattr(C, "DIVERG_GAP_LIMIT", 0.02)))
            if trig:
                rec["trigger"] = 1
                if now - self._diverg_alert_ts.get(c, 0) > 60:
                    self._diverg_alert_ts[c] = now
                    self._event("WARN",
                                "[分歧转一致候选] %s %.1f%% 外盘增量占%.0f%% 五档买比%.0f%% 距回封%.1f%%"
                                % (c, pct, odr * 100, bmr * 100, gap * 100))
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(_jj.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass

    # ---------- ★ 4.5 价格提醒（条件单） ----------
    def _price_alert_round(self):

    # ---------- ★ 4.5 价格提醒（条件单） ----------
        """检查用户自定义价格提醒（涨到/跌到触发）。每5秒轮询一次。"""
        try:
            triggered = st.check_price_alerts()
            for a in triggered:
                direction = "涨到" if a.get("direction") == "up" else "跌到"
                msg = f"⏰ 条件单触发：{a.get('name','')}({a.get('code','')}) {direction} {a.get('price',0):.2f}"
                self._event("OK", msg)
                self._notify(f"⏰ 价格提醒 {a.get('name','')}", msg)
                self._event("INFO", f"条件单[{a.get('id')}] 已触发标记")
        except Exception:
            pass


engine = TradingEngine()
