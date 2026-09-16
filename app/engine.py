# -*- coding: utf-8 -*-
"""回测引擎 — 事件驱动日线回测，真实 A 股规则建模
规则：T+1（当日买入不可卖）、涨跌停（涨停买不进/跌停卖不出）、
     佣金(万2.5,最低5元)+印花税(卖出0.05%)+过户费(沪市0.001%)、滑点、停牌(缺K线不可交易)
策略：score 评分策略（T-1信号→T开盘成交）/ board 打板日线近似（次日接力）
输出：净值曲线、交易明细、收益/回撤/胜率/夏普等指标、基准对比
"""
import bisect
import math
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from . import config as C
from . import datafeed as df
from . import indicators as ind
from . import performance as perf
from . import pools as pools
from . import portfolio as portfolio
from . import scoring as sc


# ============ ★ F2（2026-09-08）：移动止损 peak 公共规则（三处口径统一） ============
def update_peak_after_entry(peak, entry, obs_price, obs_time="", entry_ts="",
                            ref_price=0.0):
    """peak 只用建仓之后的观测价刷新 —— 实盘/日线回测/分钟回测共用同一条规则。

    - peak:      当前峰值（建仓时=entry_price）
    - entry:     建仓价
    - obs_price: 本次观测价（实盘=行情快照当前价 price；回测=建仓后 bar high）
    - obs_time / entry_ts: 可比较时间戳（实盘 "HH:MM:SS"）。当两者都非空且
      obs_time < entry_ts 时，视为建仓前旧观测，拒绝刷新（防行情缓存回填）。
      回测日线/分钟粒度无日内时间戳 → 传 ""，不设时间门控（建仓后 bar 语义天然干净）。
    - ref_price: 防御区间下限基准（实盘=当前价 cp；回测=当日收盘 cp；默认回退 entry）
    - 异常值防御保留：obs_price 须在 (ref*0.5, entry*2.0] 合理区间才接受。
    - 返回新 peak；无变化返回原值。
    """
    if entry <= 0 or obs_price <= 0:
        return peak
    if entry_ts and obs_time and obs_time < entry_ts:
        return peak  # 建仓前旧观测（如停牌后回填的旧行情），拒绝
    ref = ref_price if ref_price and ref_price > 0 else entry
    if obs_price > peak and obs_price <= entry * 2.0 and obs_price >= ref * 0.5:
        return obs_price
    return peak


# ============ 涨跌停价格 ============
def limit_pct_of(code, name=""):
    """★ 4.3：涨跌停规则表化（真实交易所规则）：
    主板 ±10% / 创业板·科创板 ±20% / ST ±5% / 北交所 ±30%
    code: 股票代码；name: 股票名称（判断 ST）
    """
    if code.startswith(("4", "8", "92")):   # 北交所
        return 0.30
    n = (name or "").upper()
    if "ST" in n:                            # ST/*ST ±5%
        return 0.05
    if code.startswith(("30", "68")):        # 创业板/科创板
        return 0.20
    return 0.10


def _round_cent(v):
    """A股价格四舍五入到分（真实规则：先乘100用标准四舍五入再除100）。
    Python round() 是银行家舍入（.5 取偶），与交易所规则不一致。
    """
    import math
    # floor(x*100 + 0.5) 实现四舍五入到分
    return math.floor(v * 100 + 0.5) / 100


def limit_prices(code, yest_close, name=""):
    p = limit_pct_of(code, name)
    # ★ 4.2 修复：涨停/跌停价四舍五入到分（交易所规则），
    #   原 round() 银行家舍入导致部分价格错误（如昨收7.85涨停应为8.64非8.63）
    return _round_cent(yest_close * (1 + p)), _round_cent(yest_close * (1 - p))


# ============ 交易成本 ============
def buy_fee(amount):
    comm = max(amount * C.COMMISSION_RATE, C.COMMISSION_MIN)
    return comm  # 佣金（过户费已含于佣金模型，沪市可忽略差异）


def sell_fee(amount):
    comm = max(amount * C.COMMISSION_RATE, C.COMMISSION_MIN)
    return comm + amount * C.STAMP_TAX_RATE


# ============ 主力出货简化检测 ============
def _distribution_signal(klines):
    """简化洗盘/出货检测（回测内联，仅判断'出货'）"""
    if len(klines) < 20:
        return False
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    highs = [k["high"] for k in klines]
    i = len(klines) - 1
    avg20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1.0
    if avg20 <= 0:
        return False
    vol_spike = max(volumes[-5:]) / avg20
    high_to_close = (highs[i] - closes[i]) / highs[i] if highs[i] else 0.0
    price_5d = (closes[i] - closes[i - 5]) / closes[i - 5] if closes[i - 5] else 0.0
    score = 0
    if vol_spike > 3.0 and high_to_close > 0.03:
        score += 3
    if vol_spike > 2.0 and price_5d < -0.03:
        score += 2
    if max(volumes[-5:]) > 5 * avg20:
        score += 2
    return score >= 3


# ============ 回测主引擎 ============
class Backtest:
    def __init__(self, codes, names, start, end, initial_capital=100000.0,
                 strategy="score", params=None, _preloaded=None):
        """
        codes: 股票池代码列表（纯数字）
        names: {code: name}
        strategy: "score" 评分策略 |
        _preloaded: ★ H3 数据共享——{codes,start,end,klines,date_index,trading_days,
                   start_idx,mcap_of,_karr,_ind,_karr_dtype} 由外层一次性加载后传入，
                   _load_data 直接采纳（跳过 SQLite/网络与预计算），多个 Backtest
                   共享同一份只读数据（run() 不改 klines/_karr/_ind，共享安全）。 "board" 打板接力 | "premium" 次日溢价统计
        params: dict, 可覆盖阈值/仓位等
        """
        self.codes = [c for c in codes if c]
        # ★ Phase21: 过滤复权不可靠的例外票（DATA_EXCLUDE_CODES）
        try:
            _ex = set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])
            if _ex:
                self.codes = [c for c in self.codes if c not in _ex]
        except Exception:
            pass
        self.names = names or {}
        self.start, self.end = start, end
        self.capital = initial_capital
        self.strategy = strategy
        p = params or {}
        # ★ Phase24: ML 主力选股池（可选）—— 第 t 日只用 ml_pred 中 date<=t 的
        #   最新预测 top-N（防未来函数：预测日期须早于信号日）。None=禁用（用全池）
        self.ml_pool_topn = p.get("ml_pool_topn")
        self._ml_cache = {}      # date -> [topN codes]（按日缓存）
        self.buy_threshold = p.get("buy_threshold", C.BUY_SCORE_THRESHOLD)
        self.max_pos = p.get("max_positions", C.MAX_POSITIONS)
        self.pos_pct = p.get("position_pct", C.POSITION_PCT)
        self.board_enabled = p.get("board_enabled", True)
        self.slippage = p.get("slippage", C.SLIPPAGE)
        self.warmup = p.get("warmup_days", 260)  # 评分预热天数
        # v3.4：两点半战法按回测结论"次日收盘卖"
        self.next_close_sell = strategy == "twothirty"
        # v3.6：组合优化方法（equal 保持原固定比例行为）
        self.portfolio_method = p.get("portfolio_method", C.PORTFOLIO_METHOD)
        self.portfolio_weights = {}   # 每笔买入时计算的动态权重 {code: w}
        # v3.7：行业敞口限制（默认开启，回测实盘共用）
        self.sector_limit_active = p.get("sector_limit", True)
        # ★ 4.6：指数择时闸门（阶段2；回测 A/B 用 params.index_timing，实盘走 config.INDEX_TIMING_ENABLED）
        self.index_timing = p.get("index_timing", False)
        # v3.8：执行质量与成交模拟
        self.exec_prob = p.get("exec_prob_model", C.EXEC_PROB_MODEL)
        self.exec_partial = p.get("exec_partial_fill", C.EXEC_PARTIAL_FILL)
        self.failed_fills = 0   # 未成交（排单失败）次数
        # ★ 4.6：回测可复现 —— 成交概率模拟用实例级 RNG（params.seed，默认42），
        #   不碰全局 random（A/B 对比与参数扫描才能同条件复跑）
        self.rng = random.Random(p.get("seed", 42))
        # ★ Phase26：涨停生态温度计仓位闸门（board 打板专用；PIT 只用信号日 t-1 温度）
        #   params.zt_eco_gate: False=强制禁用；dict={"threshold":x,"mode":"stop"|"mult",
        #   "mult":m}=显式配置；缺省走 config 默认（None=禁用，零影响）
        if "zt_eco_gate" in p:
            self.zt_eco_gate = p["zt_eco_gate"] or None
        elif getattr(C, "ZT_ECO_GATE_THRESHOLD", None):
            self.zt_eco_gate = {"threshold": C.ZT_ECO_GATE_THRESHOLD,
                                "mode": C.ZT_ECO_GATE_MODE,
                                "mult": C.ZT_ECO_GATE_MULT}
        else:
            self.zt_eco_gate = None
        self.zt_gate_checked_days = 0   # 闸门生效天数（有温度可查）
        self.zt_gate_blocked_days = 0   # 触发干预天数
        # ★ Phase27：组合回撤熔断（默认关闭）。params.dd_gate：False=强制禁用；
        #   dict={"t1":0.08,"t2":0.12,"half_mult":0.5}=显式启用（t2=None 仅 half）；
        #   缺省走 config 默认（None=禁用，零影响）。只影响新开仓预算。
        if "dd_gate" in p:
            self.dd_gate = p["dd_gate"] or None
        elif getattr(C, "DD_GATE_T1", None):
            self.dd_gate = {"t1": C.DD_GATE_T1, "t2": C.DD_GATE_T2,
                            "half_mult": C.DD_GATE_HALF_MULT}
        else:
            self.dd_gate = None
        self._eq_peak = float(initial_capital)   # 总权益（含现金）历史峰值
        self._dd_state = "normal"                # normal | half | halt
        self.dd_half_days = 0                    # half 状态交易日数
        self.dd_halt_days = 0                    # halt 状态交易日数
        self.dd_halt_dates = []                  # halt 日期列表（机会成本分析用）
        # ★ Phase37：dd_gate 恢复机制扩展（默认 "default" 与 Phase27 行为完全一致）。
        #   R1 cooldown：halt 满 N 个交易日强制回 half（half 仍按原 dd 条件恢复）
        #   R2 index_ma：halt 直到基准指数(sh000001)收盘站上自身20日线才回 half（严格 PIT）
        #   R3 half_cap：取消 halt 状态（-t2 同 -t1 只进 half）；half 最长 M 日强制回 normal
        _dg = self.dd_gate or {}
        self._dd_recovery = str(_dg.get("recovery") or "default")
        self._dd_cooldown = int(_dg.get("cooldown_days") or 0)
        self._dd_half_cap = int(_dg.get("half_cap_days") or 0)
        self._dd_halt_streak = 0                 # 连续处于 halt 的交易日数
        self._dd_half_streak = 0                 # 连续处于 half 的交易日数（R3 用）
        self._idx_ma_cache = None                # {date: bool} sh000001 收盘>MA20，懒加载
        self._idx_ma_dates = None                # ★ H2：cache 键升序列表（bisect 查询）

        self.klines = {}       # code -> [bar...] 含预热
        self.date_index = {}   # code -> {date: idx}
        # ★ H2（2026-09-13）：指标预计算与零拷贝视图
        self._ind = {}         # code -> {name: np.ndarray}（score 策略评分指标全序列）
        self._karr = {}        # code -> np.ndarray 结构化日K（_hist_klines 零拷贝视图源）
        self._karr_dtype = None  # 结构化 dtype（_load_data 构造）
        self._preloaded = _preloaded  # ★ H3：外层共享数据（见 __init__ docstring）
        self.trading_days = [] # 排序后的交易日（区间内）
        self.cash = initial_capital
        self.positions = {}    # code -> dict(qty, entry_price, entry_date, peak, days, ...)
        self.trades = []
        self.equity = []       # [{date, equity, cash, mv}]
        self.log_lines = []
        self.suspension_skips = 0   # v3.4: 因停牌/缺K线跳过的交易次数（审计）
        self.mcap_of = {}           # 4.2: code -> 流通市值（撮合分档滑点用）

    # ---------- 数据准备 ----------
    def _cache_key(self):
        """★ H3：_load_data 快照缓存键 = (codes 集合指纹, start, end, 复权口径, 库内新鲜度)。
        新鲜度 = 该批 codes 的 MAX(date)+COUNT(*)（同日增量更新会改变 COUNT → 键变 → 缓存失效）。
        失败返回 None（走无缓存路径）。"""
        try:
            import hashlib
            import sqlite3
            if not C.DB_FILE:
                return None
            codes_sig = ",".join(sorted(self.codes))
            conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=20)
            try:
                ph = ",".join("?" * len(self.codes))
                row = conn.execute(
                    "SELECT MAX(date), COUNT(*) FROM kline WHERE period='day' "
                    "AND code IN (%s)" % ph, tuple(self.codes)).fetchone()
                fresh = "%s/%s" % (row[0] or "0", row[1] or 0)
            finally:
                conn.close()
            raw = "%s|%s|%s|qfq|%s" % (codes_sig, self.start, self.end, fresh)
            return hashlib.sha1(raw.encode("utf-8")).hexdigest()
        except Exception:
            return None

    def _load_cache(self, key):
        """命中返回 (klines, date_index, trading_days, start_idx)，未命中/损坏返回 None。"""
        try:
            path = os.path.join(C.DATA_DIR, "bt_cache", key + ".npz")
            if not os.path.exists(path):
                return None
            z = np.load(path, allow_pickle=False)
            klines, date_index = {}, {}
            for name in z.files:
                if name.startswith("_meta"):
                    continue
                arr = z[name]
                kl = [{"date": r["date"], "open": r["open"], "high": r["high"],
                       "low": r["low"], "close": r["close"],
                       "volume": r["volume"], "amount": r["amount"]} for r in arr]
                if len(kl) >= 30:
                    klines[name] = kl
                    date_index[name] = {k["date"]: i for i, k in enumerate(kl)}
            td = z["_meta_trading_days"]
            si = z["_meta_start_idx"]
            trading_days = [str(x) for x in td] if len(td) else []
            start_idx = {str(r["code"]): int(r["idx"]) for r in si}
            z.close()
            return klines, date_index, trading_days, start_idx
        except Exception:
            return None

    def _save_cache(self, key):
        """把当前 klines/date_index/trading_days/start_idx 序列化到 data/bt_cache/<key>.npz
        （原子替换；写后按 BT_CACHE_MAX_GB 做 LRU 清理）。失败静默。"""
        try:
            d = os.path.join(C.DATA_DIR, "bt_cache")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, key + ".npz")
            tmp = path + ".part.npz"   # savez 对已 .npz 结尾不再追加后缀
            arrs = {}
            for code, kl in self.klines.items():
                a = np.zeros(len(kl), dtype=self._karr_dtype)
                for i, k in enumerate(kl):
                    a[i] = (k["date"], k["open"], k["high"], k["low"],
                            k["close"], k["volume"], k["amount"])
                arrs[code] = a
            arrs["_meta_trading_days"] = np.array(self.trading_days, dtype="U10")
            arrs["_meta_start_idx"] = np.array(
                [(c, self.start_idx.get(c, 0)) for c in self.start_idx],
                dtype=[("code", "U10"), ("idx", "i8")])
            np.savez(tmp, **arrs)
            if os.path.exists(tmp):
                os.replace(tmp, path)
            self._cache_cleanup(d)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    @staticmethod
    def _cache_cleanup(d):
        """LRU 按 mtime 清理，目录总大小 ≤ BT_CACHE_MAX_GB（至少保留 1 个文件）。"""
        try:
            limit = float(getattr(C, "BT_CACHE_MAX_GB", 5) or 5) * 1024 ** 3
            files = sorted(
                (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".npz")),
                key=lambda p: os.path.getmtime(p))
            total = sum(os.path.getsize(p) for p in files)
            while total > limit and len(files) > 1:
                p = files.pop(0)
                total -= os.path.getsize(p)
                try:
                    os.remove(p)
                except Exception:
                    pass
        except Exception:
            pass

    def _load_data(self, progress_cb=None):
        # ★ H3：外层共享数据直接采纳（walk_forward 网格 / optimize / sensitivity 共享一次加载；
        #   run() 不改 klines/_karr/_ind/date_index，多实例共享只读引用安全）
        if self._preloaded is not None:
            _pl = self._preloaded
            if (_pl.get("codes") == self.codes and _pl.get("start") == self.start
                    and _pl.get("end") == self.end
                    and _pl.get("warmup") == self.warmup):
                self.klines = _pl["klines"]
                self.date_index = _pl["date_index"]
                self.trading_days = list(_pl["trading_days"])
                self.start_idx = dict(_pl["start_idx"])
                self.mcap_of = dict(_pl.get("mcap_of") or {})
                self._karr = _pl.get("_karr") or {}
                self._ind = _pl.get("_ind") or {}
                self._karr_dtype = _pl.get("_karr_dtype")
                return
        # ★ H3：bt_cache 快照缓存命中 → 直接还原（省 SQLite 直读+解析；mcap 每次现取）
        _ck = None
        if getattr(C, "BT_CACHE_ENABLED", True) and not os.environ.get("BT_NO_CACHE"):
            _ck = self._cache_key()
            _hit = self._load_cache(_ck) if _ck else None
            if _hit:
                self.klines, self.date_index = _hit[0], _hit[1]
                self.trading_days, self.start_idx = list(_hit[2]), dict(_hit[3])
                try:
                    quotes = df.fetch_quotes(self.codes)
                    for code in self.codes:
                        q = quotes.get(code) or {}
                        mcap = q.get("float_mktcap", 0) or 0
                        if not mcap:
                            kl = self.klines.get(code)
                            if kl:
                                mcap = kl[-1].get("amount", 0) * 20
                        self.mcap_of[code] = mcap or 0
                except Exception:
                    pass
                self._build_karr()
                self._precompute_indicators()
                return
        days_needed = self.warmup + 60
        codes = self.codes[:]
        done, total = 0, len(codes)
        # ★ Phase21: 优先从 market.db 本地区间直读（历史窗口回测数据正确；
        #   fetch_kline 只能取"最近 N 根"，2019-2020 等旧窗口会无数据）
        try:
            if C.DB_FILE:
                import sqlite3
                try:
                    from datetime import datetime, timedelta
                    _w0 = datetime.strptime(self.start, "%Y-%m-%d") - timedelta(days=days_needed + 30)
                    _start = _w0.strftime("%Y-%m-%d")
                except Exception:
                    _start = self.start
                _conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=20)
                try:
                    ph = ",".join("?" * len(codes))
                    _args = tuple(codes) + (_start, self.end)
                    rows = (_conn.execute(
                        "SELECT code, date, open, high, low, close, volume, amount FROM kline "
                        "WHERE period='day' AND code IN (%s) AND date>=? AND date<=? "
                        "ORDER BY code, date" % ph, _args).fetchall() if codes else [])
                    _by = {}
                    for c, d, o, h, l, cl, v, a in rows:
                        if cl is None or cl <= 0:
                            continue
                        _by.setdefault(c, []).append(
                            {"date": d, "open": o, "high": h, "low": l, "close": cl,
                             "volume": v or 0, "amount": a or 0})
                    for c, kl in _by.items():
                        if len(kl) >= 30:
                            self.klines[c] = kl
                            self.date_index[c] = {k["date"]: i for i, k in enumerate(kl)}
                    done = total
                finally:
                    _conn.close()
        except Exception:
            pass
        if not self.klines:
            with ThreadPoolExecutor(max_workers=C.PARALLEL_WORKERS) as ex:
                futs = {ex.submit(df.fetch_kline, c, "day", days_needed): c for c in codes}
                for f in as_completed(futs):
                    code = futs[f]
                    try:
                        kl = f.result()
                    except Exception:
                        kl = []
                    done += 1
                    if progress_cb and done % 20 == 0:
                        progress_cb(done, total)
                    if len(kl) >= 30:
                        self.klines[code] = kl
                        self.date_index[code] = {k["date"]: i for i, k in enumerate(kl)}

        # 交易日序列（取池内股票日期并集，限定区间）
        all_dates = set()
        for code, kl in self.klines.items():
            for k in kl:
                if self.start <= k["date"] <= self.end:
                    all_dates.add(k["date"])
        self.trading_days = sorted(all_dates)
        # 每股在交易区间的起始索引
        self.start_idx = {}
        for code, kl in self.klines.items():
            si = None
            for i, k in enumerate(kl):
                if k["date"] >= self.start:
                    si = i
                    break
            self.start_idx[code] = si if si is not None else len(kl)
        # ★ 4.2：流通市值（撮合分档滑点）—— 批量行情取 float_mktcap，失败用成交额近似
        try:
            quotes = df.fetch_quotes(self.codes)
            for code in self.codes:
                q = quotes.get(code) or {}
                mcap = q.get("float_mktcap", 0) or 0
                if not mcap:
                    kl = self.klines.get(code)
                    if kl:
                        mcap = kl[-1].get("amount", 0) * 20  # 成交额×20 粗糙近似流通市值
                self.mcap_of[code] = mcap or 0
        except Exception:
            pass
        self._build_karr()
        self._precompute_indicators()
        # ★ H3：写缓存（防污染守卫：加载到一半以上才缓存；失败静默）
        if _ck and len(self.klines) >= max(5, int(len(self.codes) * 0.5)):
            self._save_cache(_ck)

    # ---------- H2：指标预计算与零拷贝视图 ----------
    def _build_karr(self):
        """把 klines 转成结构化 numpy 数组（date 用 U10 定长），供 _hist_klines 零拷贝切片。"""
        if self.klines:
            self._karr_dtype = np.dtype([
                ("date", "U10"), ("open", "f8"), ("high", "f8"),
                ("low", "f8"), ("close", "f8"), ("volume", "f8"),
                ("amount", "f8")])
        else:
            self._karr_dtype = None
        for code, kl in self.klines.items():
            try:
                arr = np.zeros(len(kl), dtype=self._karr_dtype)
                for i, k in enumerate(kl):
                    arr[i] = (k["date"], k["open"], k["high"], k["low"],
                              k["close"], k["volume"], k["amount"])
                self._karr[code] = arr
            except Exception:
                continue

    @staticmethod
    def _arr(lst):
        """list（含 None 前缀）→ float64 数组，None → nan。
        nan 与 None 在评分条件里等价（nan 参与比较恒 False，与 None 的'直接跳过'结果一致；
        唯一例外 vr 分母已在 _score_stock_at 显式守卫）。"""
        return np.array([float("nan") if x is None else x for x in lst], dtype="f8")

    def _precompute_indicators(self):
        """★ H2：对每只股票一次性算全历史指标序列（score 策略评分所需全部 ind.* 调用），
        存 numpy 数组字典。board/twothirty 的评分函数（score_board/score_twothirty）在
        _signals_on 内联调用且内部逻辑不动，不预计算（行为不变）。"""
        self._ind = {}
        if self.strategy != "score":
            return
        for code in self.codes:
            kl = self.klines.get(code)
            if not kl or len(kl) < 60:
                continue
            try:
                closes = [k["close"] for k in kl]
                volumes = [k["volume"] for k in kl]
                highs = [k["high"] for k in kl]
                dif, dea, _h = ind.macd(closes, C.MACD_FAST, C.MACD_SLOW, C.MACD_SIGNAL)
                # ★ rsrs 保留全序列值；截断语义（score_stock 每次用 kl[:i+1]，zscore 起点门
                #   len(bv)=n-window+1 >= zscore_window+window → 整条有值当且仅当
                #   i >= zscore_window+2*window-2=634）由 _score_stock_at 的 _rs_avail 门处理
                #   （rs[i-1] 在查询日 i>=634 时同一次调用正常出值，数组须保留）。
                self._ind[code] = {
                    "ma5": self._arr(ind.sma(closes, 5)),
                    "ma10": self._arr(ind.sma(closes, 10)),
                    "ma20": self._arr(ind.sma(closes, 20)),
                    "vma5": self._arr(ind.sma(volumes, 5)),
                    "dif": self._arr(dif),
                    "dea": self._arr(dea),
                    "rsi": self._arr(ind.rsi(closes, C.RSI_PERIOD)),
                    "h20": self._arr(ind.rolling_max(highs, 20)),
                    "rs": self._arr(ind.rsrs(kl, C.RSRS_WINDOW, C.RSRS_ZSCORE)),
                    "vp_vwap": self._arr(ind.vwap(kl)),
                    "vp_corr": self._arr(ind.price_volume_corr(closes, volumes, 10)),
                    "vp_slope": self._arr(ind.volume_slope(volumes, 5)),
                    "vp_surge": self._arr(ind.volume_surge(volumes, 5)),
                    "vp_div": self._arr(ind.price_vol_divergence(closes, volumes, 10, 10)),
                }
            except Exception:
                continue

    def _score_stock_at(self, code, i, date):
        """★ H2：按预计算数组索引取分（与 sc.score_stock 逐行对应，数值等价）。
        指标值来自 _precompute_indicators 的全序列数组，第 i 个元素 = score_stock 在
        第 i 根处的同名变量；评分条件逐条复制（2026-09-13 对照 scoring.py:257-410）。
        nan 语义：预计算 None → nan；条件里 nan 参与比较恒 False，与 None'直接跳过'
        结果一致（vr 分母与业绩/ML 分支已显式对齐）。"""
        if self.strategy != "score":
            return 0, ["非score"]
        d = self._ind.get(code)
        if d is None:
            return 0, ["无预计算"]
        kl = self.klines[code]
        if i >= len(kl):
            return 0, ["越界"]
        # ★ F6：历史长度门（与 score_stock 同一口径）
        _min_bars = max(60, int(getattr(C, "MIN_HISTORY_BARS", 250) or 250))
        if i + 1 < _min_bars:
            return 0, ["历史不足"]
        if code and code in sc._universe_excluded():
            return 0, ["universe_excluded"]
        bar = kl[i]
        c, o, h = bar["close"], bar["open"], bar["high"]
        prev = kl[i - 1]["close"] if i > 0 else c

        ma5, ma10, ma20 = d["ma5"], d["ma10"], d["ma20"]
        vma5, dif, dea = d["vma5"], d["dif"], d["dea"]
        rsi, h20, rs = d["rsi"], d["h20"], d["rs"]
        vp_vwap, vp_corr = d["vp_vwap"], d["vp_corr"]
        vp_slope, vp_surge, vp_div = d["vp_slope"], d["vp_surge"], d["vp_div"]

        pct = (c - prev) / prev if prev else 0.0
        _v5 = vma5[i]
        vr = bar["volume"] / _v5 if (_v5 == _v5 and _v5) else 1.0  # nan/0 → 1.0（对齐 None 语义）
        W = getattr(C, "FACTOR_WEIGHT_OVERRIDE", None) or C.SCORE_WEIGHTS
        total, sig = 0, []

        if c > o and pct >= 0.02 and vr >= C.VOL_RATIO_HIGH:
            total += W["volume_price"]; sig.append(f"量价齐升(+{W['volume_price']})")
        if h20[i] and h >= h20[i] and vr >= C.VOL_RATIO_BREAKOUT:
            total += W["breakout"]; sig.append(f"放量突破20日新高(+{W['breakout']})")
        if ma5[i] and ma10[i] and ma20[i] and ma5[i] > ma10[i] > ma20[i]:
            total += W["ma_bullish"]; sig.append(f"均线多头(+{W['ma_bullish']})")
        # ★ rsrs 截断语义门：score_stock 每次用 kl[:i+1] 调用 rsrs，zscore 起点门使整条
        #   rs 有值当且仅当 i >= zscore_window+2*window-2（=634@600/18）；i 低于该门时
        #   rs[i]/rs[i-1] 均为 None（预计算数组保留全序列值，此处显式加门等价）。
        _rs_avail = C.RSRS_ZSCORE + 2 * C.RSRS_WINDOW - 2
        if rs[i] is not None and i >= _rs_avail and rs[i] > C.RSRS_BUY_THRESHOLD:
            total += W["rsrs_bullish"]; sig.append(f"RSRS看涨({rs[i]:.2f},+{W['rsrs_bullish']})")
        if dif[i] and dea[i] and dif[i - 1] and dea[i - 1] and dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            total += W["macd_golden"]; sig.append(f"MACD金叉(+{W['macd_golden']})")
        if rsi[i] and rsi[i - 1] and rsi[i - 1] < C.RSI_OVERSOLD and rsi[i] > rsi[i - 1]:
            total += W["rsi_oversold"]; sig.append(f"RSI超卖反弹(+{W['rsi_oversold']})")
        if ma5[i] and ma10[i] and ma5[i] > ma10[i] and vr <= C.VOLUME_SHRINK_RATIO and c >= ma10[i]:
            total += W["volume_shrink"]; sig.append(f"缩量回踩(+{W['volume_shrink']})")
        if ma20[i] and 0 < c - ma20[i] <= ma20[i] * 0.02 and prev < c:
            total += W["pullback_ma"]; sig.append(f"回踩20日线企稳(+{W['pullback_ma']})")
        if ma5[i] and ma10[i] and ma5[i - 1] and ma10[i - 1] and ma5[i - 1] <= ma10[i - 1] and ma5[i] > ma10[i]:
            total += W["ma_cross"]; sig.append(f"MA5/10金叉(+{W['ma_cross']})")
        if h20[i] and c >= h20[i] * 0.995:
            total += W["turtle_break"]; sig.append(f"海龟20日突破(+{W['turtle_break']})")
        if rs[i] is not None and i >= _rs_avail and rs[i - 1] is not None and rs[i] > rs[i - 1] > 0:
            total += W["rsrs_accel"]; sig.append(f"RSRS加速(+{W['rsrs_accel']})")
        if pct > 0.05 and vr > 3.0 and c < h * 0.98:
            total += W["penalty_stall"]; sig.append(f"⚠高位放量滞涨({W['penalty_stall']})")
        if rsi[i] and rsi[i] > C.RSI_OVERBOUGHT:
            total += W["penalty_overbought"]; sig.append(f"⚠RSI超买({rsi[i]:.0f},{W['penalty_overbought']})")

        # ★ 4.4 量价深度评分
        if vp_corr[i] is not None and vp_corr[i] >= C.VP_CORR_GOOD:
            total += W["vp_corr"]; sig.append(f"量价相关{vp_corr[i]:.2f}(+{W['vp_corr']})")
        if vp_slope[i] is not None and vp_slope[i] > C.VP_VOL_SLOPE_GOOD:
            total += W["vp_vol_slope"]; sig.append(f"量能递增(+{W['vp_vol_slope']})")
        if vp_vwap[i] and c > vp_vwap[i]:
            total += W["vp_vwap"]; sig.append(f"站稳VWAP(+{W['vp_vwap']})")
        if vp_surge[i] is not None and vp_surge[i] >= C.VP_SURGE_GOOD:
            total += W["vp_surge"]; sig.append(f"放量启动{vp_surge[i]:.1f}x(+{W['vp_surge']})")
        if vp_div[i] is not None and vp_div[i] > 0 and vp_div[i] >= C.VP_DIVERGENCE_BAD:
            total += W["vp_divergence"]; sig.append(f"⚠量价背离({W['vp_divergence']})")
        if vp_corr[i] is not None and vp_corr[i] < C.VP_CORR_BAD:
            total += W["vp_divergence"]; sig.append(f"⚠量价负相关{vp_corr[i]:.2f}({W['vp_divergence']})")

        # ★ 4.6 量价信号包 / GTJA191（默认关闭；传 kl[:i+1] 与 score_stock 的 hist 同界，PIT 严格）
        if C.VOLPRICE_SIGNALS_ENABLED and C.VOLPRICE_WEIGHTS:
            try:
                from . import volprice as vp
                for key, desc in vp.detect_signals(kl[:i + 1]):
                    w = C.VOLPRICE_WEIGHTS.get(key)
                    if w:
                        total += w
                        sig.append(f"{desc}(+{w})")
            except Exception:
                pass
        if C.GTJA_FACTORS_ENABLED and C.GTJA_FACTOR_WEIGHTS:
            try:
                from . import factor as fac
                for fname, w in C.GTJA_FACTOR_WEIGHTS.items():
                    vals, ok = fac.compute_factor(fname, kl[:i + 1])
                    if not ok or not vals or vals[-1] is None:
                        continue
                    v = vals[-1]
                    if fname == "VOL变异20" and v > C.GTJA_VOLCV_HIGH:
                        total += w
                        sig.append(f"量能变异{v:.2f}({w})")
            except Exception:
                pass

        # ★ 4.5 业绩事件驱动（PIT：as_of=信号日，offline 只读本地库）
        try:
            if code:
                from . import earnings as ea
                e_bonus, e_sig = ea.earnings_signal(code, as_of=date, offline=bool(date))
                if e_bonus:
                    total += e_bonus
                    sig.extend(e_sig)
        except Exception as e:
            try:
                import logging
                logging.getLogger("tianji").warning("业绩信号异常 code=%s: %s", code, e)
            except Exception:
                pass

        # ★ 4.6 ML 选股 / Phase25 ml_rank（默认关闭/权重0，与 score_stock 同判断）
        if C.ML_SCORE_ENABLED:
            try:
                _mb, _msig = sc.ml_score_bonus(code)
                if _mb:
                    total += _mb
                    sig.extend(_msig)
            except Exception:
                pass
        if getattr(C, "ML_RANK_WEIGHT", 0):
            try:
                _pb, _psig = sc.ml_rank_bonus(code, date or (kl[-1].get("date") if kl else None))
                if _pb:
                    total += _pb
                    sig.extend(_psig)
            except Exception:
                pass
        return max(0, total), sig

    # ---------- 撮合 ----------
    def _buy(self, date, code, price, qty, reason, fee_extra=0.0):
        if qty < 100 or price <= 0:
            return False
        cost = price * qty
        fee = buy_fee(cost) + fee_extra
        if cost + fee > self.cash + 1e-6:
            qty = int((self.cash * 0.98) / price / 100) * 100
            if qty < 100:
                return False
            cost = price * qty
            fee = buy_fee(cost) + fee_extra
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
        return True

    def _sell(self, date, code, price, qty, reason):
        pos = self.positions.get(code)
        if not pos or qty <= 0 or price <= 0:
            return False
        if qty > pos["qty"]:
            qty = pos["qty"]
        amount = price * qty
        fee = sell_fee(amount)
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

    def _bar(self, code, date):
        di = self.date_index.get(code)
        if not di:
            return None
        i = di.get(date)
        if i is None or i < 0 or i >= len(self.klines[code]):
            return None
        return self.klines[code][i]

    def _hist_klines(self, code, date):
        """截止 date 收盘的历史K线（含当日）。
        ★ H2：返回结构化 numpy 数组的 [:i+1] 视图（零拷贝），严格截止 i（含），
        下游不可能看到 i 之后的数据（PIT 纪律由切片天然保证）。
        访问语义与 list 兼容：h[-1]["close"] / h[-2]["close"] / len(h) / 迭代 k["close"]。"""
        di = self.date_index.get(code)
        if not di:
            return np.zeros(0, dtype=self._karr_dtype)
        i = di.get(date)
        if i is None:
            return np.zeros(0, dtype=self._karr_dtype)
        karr = self._karr.get(code)
        if karr is None:
            return []
        return karr[:i + 1]

    # ---------- 策略信号 ----------
    def _ml_top_codes(self, date):
        """★ Phase24: 第 t 日 ML 池 = ml_pred 中 date<=t 的最新预测 top-N（PIT：只用 t 日可得）
        缓存每日结果；返回 code 集合（含已有持仓，由调用方过滤）。"""
        if self.ml_pool_topn is None:
            return None
        hit = self._ml_cache.get(date)
        if hit is not None:
            return hit
        try:
            import sqlite3
            conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=10)
            try:
                # 取 date<=t 的最后预测日 top-N（该日截面分最高 N 只）
                rows = conn.execute(
                    "SELECT code, score FROM ml_pred WHERE date<=? "
                    "AND date=(SELECT MAX(date) FROM ml_pred WHERE date<=?) "
                    "ORDER BY score DESC LIMIT ?", (date, date, self.ml_pool_topn)).fetchall()
                top = set(r[0] for r in rows)
            finally:
                conn.close()
        except Exception:
            top = None
        self._ml_cache[date] = top
        return top

    def _dd_index_above_ma(self, date):
        """★ Phase37 R2：基准指数 sh000001 收盘是否站上自身 20 日均线（严格 PIT）。
        只用 <=date 的指数日K；返回 True/False；库内无该日之前数据时返回 None（视为不恢复）。
        ★ H2：查询由 O(N) 线性扫描改为 bisect 二分 O(logN)（dates 缓存升序）。"""
        if self._idx_ma_cache is None:
            ma = {}
            try:
                import sqlite3
                conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=10)
                try:
                    rows = conn.execute(
                        "SELECT date, close FROM kline WHERE period='day' AND code='sh000001' "
                        "ORDER BY date").fetchall()
                finally:
                    conn.close()
                closes = []
                for d, cl in rows:
                    if cl is None or cl <= 0:
                        continue
                    closes.append(cl)
                    if len(closes) >= 20:
                        ma[d] = cl > sum(closes[-20:]) / 20.0
            except Exception:
                ma = {}
            self._idx_ma_cache = ma
            self._idx_ma_dates = list(ma.keys())   # 插入序 = 日期升序（SQL ORDER BY date）
        dates = self._idx_ma_dates
        if not dates:
            return None
        pos = bisect.bisect_right(dates, date) - 1
        if pos < 0:
            return None
        return self._idx_ma_cache[dates[pos]]

    def _bt_ctx(self, date):
        """I2：回测 ctx 重建（SCORING_UNIFIED=True 时使用）——
        板块强度/龙头用**信号日历史行情**计算（calc_sector_strength PIT 重建），
        自选 focus 用当前 watchlist 静态近似（无历史版本，报告已标注）。
        按 date 缓存（_signals_on 逐票循环只算一次）。
        """
        if not hasattr(self, "_bt_ctx_cache"):
            self._bt_ctx_cache = {}
        if date in self._bt_ctx_cache:
            return self._bt_ctx_cache[date]
        ctx = {"focus": set(), "strong_sectors": set(), "leaders": set()}
        try:
            try:
                from . import state as st
                wl = st.load_watchlist()
                ctx["focus"] = {w["code"] for w in wl.get("watchlist", [])}
            except Exception:
                pass
            quotes = {}
            for code in self.codes:
                h = self._hist_klines(code, date)
                if len(h) < 2:
                    continue
                bar = h[-1]
                pct = (bar["close"] - h[-2]["close"]) / h[-2]["close"] * 100
                quotes[code] = {"pct_chg": pct, "amount": bar.get("amount", 0) or 0,
                                "high": bar["high"], "low": bar["low"]}
            strength = sc.calc_sector_strength(quotes)
            top = sc.get_top_sectors(strength)
            ctx["strong_sectors"] = {s[0] for s in top}
            leads = set()
            for _, info in top:
                for c, n, p in info.get("leaders", []):
                    leads.add(c)
            ctx["leaders"] = leads
        except Exception:
            pass
        self._bt_ctx_cache[date] = ctx
        return ctx

    def _score_one(self, code, date, ml_top):
        """单票评分（★ H3 从 _signals_on 提取，供并行分片复用）。
        只读共享状态（klines/_karr/_ind/date_index/positions），线程安全：
        主线程等待 future 期间不写 positions；业绩分支每次新建只读 sqlite 连接。"""
        if ml_top is not None and code not in ml_top:
            return None
        if code in self.positions:
            return None
        if not sc.is_main_board(code):
            return None
        hist = self._hist_klines(code, date)
        if len(hist) < 60:
            return None
        bar = hist[-1]
        price = bar["close"]
        if price < 5 or price > 150:
            return None
        pct = (bar["close"] - hist[-2]["close"]) / hist[-2]["close"] * 100 if len(hist) > 1 else 0.0
        if pct >= C.LIMIT_UP_PCT or pct <= C.LIMIT_DOWN_PCT:
            return None
        if self.strategy == "score":
            # ★ H2：预计算指标数组按索引取分（SCORING_UNIFIED=False 时与
            #   score_final(ctx=None)=score_stock 逐位等价；跳过全量重算）。
            #   ★ I2 前提：SCORING_UNIFIED=True 时走回测重建 ctx 的完整路径（不预计算）。
            if getattr(C, "SCORING_UNIFIED", False):
                _ctx = self._bt_ctx(date)
                score, signals, _bd = sc.score_final(hist, code=code,
                                                     as_of=date, ctx=_ctx)
            else:
                _di = self.date_index.get(code)
                _i = _di.get(date) if _di else None
                if _i is None:
                    return None
                score, signals = self._score_stock_at(code, _i, date)
            if score >= self.buy_threshold:
                return {"code": code, "score": score, "signals": signals,
                        "pct": pct, "price": price}
        elif self.strategy == "board":
            if C.BOARD_MIN_PCT <= pct < C.BOARD_MAX_PCT:
                q = {"pct_chg": pct, "price": price, "high": bar["high"],
                     "low": bar["low"], "volume": bar["volume"], "turnover": 0.0}
                score, signals = sc.score_board(hist, q, hour=None)
                if score >= C.BOARD_SCORE_THRESHOLD:
                    return {"code": code, "score": score, "signals": signals,
                            "pct": pct, "price": price}
        elif self.strategy == "twothirty":
            # ★ v3.4 两点半战法：涨幅2~7% + MACD金叉 + 量比>1（日线近似版）
            if 2.0 <= pct < 7.0:
                q = {"pct_chg": pct, "price": price, "high": bar["high"],
                     "volume": bar["volume"], "vol_ratio": 0.0}
                score, signals = sc.score_twothirty(hist, q)
                if score >= 60:
                    return {"code": code, "score": score, "signals": signals,
                            "pct": pct, "price": price}
        return None

    def _score_chunk(self, chunk, date, ml_top):
        """分片评分（H3）：按 chunk 内 code 顺序产出，保证与串行逐 code 顺序一致。"""
        return [self._score_one(code, date, ml_top) for code in chunk]

    def _signals_on(self, date):
        """date 收盘后产生次日买入信号列表 [{code, score, signals, pct, price}]
        ★ H3 并行：BT_PARALLEL_ENABLED=True 时用常驻池（app/pools.py）按
        BT_SIGNAL_CHUNK 分片并行评分；结果按 chunk 序 + code 序合并 → 与串行
        逐 code 处理顺序一致 → sort(稳定) 后同分相对顺序不变（确定性硬判据）。
        回退：BT_PARALLEL_ENABLED=False 走原串行循环。"""
        out = []
        ml_top = self._ml_top_codes(date)
        codes = self.codes
        _par = bool(getattr(C, "BT_PARALLEL_ENABLED", True))
        if _par and len(codes) >= max(1, int(getattr(C, "BT_SIGNAL_CHUNK", 200) or 200)):
            _n = max(1, int(getattr(C, "BT_PARALLEL_WORKERS", 8) or 8))
            _chunk = max(1, int(getattr(C, "BT_SIGNAL_CHUNK", 200) or 200))
            _pool = pools.get_pool("bt_signals", _n)
            _chunks = [codes[i:i + _chunk] for i in range(0, len(codes), _chunk)]
            _futs = [_pool.submit(self._score_chunk, ch, date, ml_top) for ch in _chunks]
            for f in _futs:
                try:
                    for item in f.result():
                        if item is not None:
                            out.append(item)
                except Exception:
                    continue
        else:
            for code in codes:
                item = self._score_one(code, date, ml_top)
                if item is not None:
                    out.append(item)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    def _sell_price(self, code, base_px, qty, bar=None):
        """★ Phase48+49-E：卖出价与买入侧对称——base×(1−slippage) 后经
        execution.execute_price 市值分档滑点（impact=True），并钳制在
        涨跌停区间内（对齐实盘 trader._sell_px，P45 审计中-E）。"""
        px = base_px * (1 - self.slippage)
        try:
            from . import execution as ex
            mcap = self.mcap_of.get(code, 0) or 0
            px = ex.execute_price(px, "sell", mcap, amount=px * max(qty, 100),
                                  day_amount=(bar or {}).get("amount", 0) or 0,
                                  impact=True)
        except Exception:
            pass
        d = (bar or {}).get("date")
        if d:
            h = self._hist_klines(code, d)
            if len(h) >= 2 and h[-2]["close"] > 0:
                lu, ld = limit_prices(code, h[-2]["close"], self.names.get(code, ""))
                px = max(ld, min(lu, px))
        return px

    # ---------- 退出检查（T+1 安全） ----------
    def _check_exits(self, date):
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            bar = self._bar(code, date)
            if not bar:
                continue  # 停牌：无法交易
            cp = bar["close"]
            entry = pos["entry_price"]
            pnl_pct = (cp - entry) / entry
            pos["days"] += 1
            # ★ F2：peak 只用建仓后的 bar high（三处口径统一到 update_peak_after_entry）。
            #   日线回测语义：_check_exits 在买入前执行，买入当日 days=0 不进本段，
            #   此处 days 已 +1，恒为建仓后交易日 → 无建仓前污染（显式守卫防回归）。
            if pos["days"] >= 1:
                pos["peak"] = update_peak_after_entry(pos["peak"], entry,
                                                      bar["high"], ref_price=cp)
            if pos["days"] < 1:
                continue  # ★ T+1（v3.4 修正）：days=0 为买入当日不可卖；days>=1（次日）起可卖
            # ★ Phase48+49-F：跌停禁卖——当日触及跌停且收盘仍封死 → 卖单无法成交，
            #   全部退出规则顺延下一交易日（对齐实盘 trader 的跌停跳过逻辑，P45 审计中-F）
            _h48 = self._hist_klines(code, date)
            if len(_h48) >= 2 and _h48[-2]["close"] > 0:
                _lu48, _ld48 = limit_prices(code, _h48[-2]["close"],
                                            self.names.get(code, ""))
                if (_ld48 > 0 and bar["low"] <= _ld48 * 1.001
                        and cp <= _ld48 * 1.001):
                    continue   # 跌停封死：今日卖不出，顺延
            reason = None
            # ★ Phase48+49-B：board 打板仓专属退出（对齐实盘 trader.py 打板规则）
            if self.strategy == "board" and pos.get("board_trade"):
                if pos["days"] >= 2:
                    pos.pop("board_trade", None)   # 次日过后转通用退出链（当日即走常规规则）
                elif pos["days"] == 1 and len(_h48) >= 2 and _h48[-2]["close"] > 0:
                    _yc48 = _h48[-2]["close"]
                    # T+1 低开止损：low ≤ 昨收×(1+BOARD_NEXT_DAY_LOW) → max(open,触发价) 成交
                    _trig = _yc48 * (1 + C.BOARD_NEXT_DAY_LOW)
                    if bar["low"] <= _trig:
                        _fill = self._sell_price(code, max(bar["open"], _trig),
                                                 pos["qty"], bar)
                        self._sell(date, code, _fill, pos["qty"],
                                   f"打板低开止损({(_fill / entry - 1):+.1%})")
                        continue
                    # T+1 浮盈≥5% 半仓止盈（trader 同为 0.05 硬编码）
                    _hi_trig = entry * 1.05
                    if bar["high"] >= _hi_trig:
                        _half = pos["qty"] // 200 * 100
                        if _half >= 100:
                            _fill = self._sell_price(code, max(bar["open"], _hi_trig),
                                                     _half, bar)
                            self._sell(date, code, _fill, _half,
                                       f"打板半仓止盈({((_hi_trig - entry) / entry):+.1%})")
                            continue   # 剩余半仓次日由常规链接管
            # ★ v3.4 两点半战法：次日收盘强制卖出（回测最优）
            if self.next_close_sell and pos["days"] == 1:
                # ★ P2-1：卖出计滑点
                self._sell(date, code, self._sell_price(code, cp, pos["qty"], bar),
                           pos["qty"], "两点半战法:次日收盘卖")
                continue
            # ★ 4.6 阶梯止盈（附加分支，不与其他退出规则互斥）：
            #   达到台阶即分批兑现；剩余仓位继续走全清止盈/回撤/移动止损/时间止损/超时
            if C.LADDER_TP_ENABLED and pos["qty"] > 0:
                for i, (thr, frac) in enumerate(C.LADDER_TP_STEPS):
                    if i in (pos.get("ladder_sold") or []):
                        continue
                    if pnl_pct < thr:
                        continue
                    qty_total = pos["qty"]
                    qty_sell = int(qty_total * frac / 100) * 100
                    if qty_sell < 100:
                        qty_sell = qty_total  # 不足一手清空
                    qty_sell = min(qty_sell, qty_total)
                    pos["ladder_sold"] = list(pos.get("ladder_sold") or []) + [i]
                    self._sell(date, code,
                               self._sell_price(code, cp, qty_sell, bar),
                               qty_sell,
                               f"阶梯止盈(+{int(round(thr*100))}%)(+{pnl_pct:+.1%})")
                    if pos["qty"] <= 0:
                        break   # 已清仓
            # ★ 4.6 量价择时卖出（缩量新高卖半仓 / 放量滞涨清仓；附加分支）
            if C.VOLP_SELL_ENABLED and pos["qty"] > 0 and not pos.get("volp_half_done"):
                try:
                    from . import volprice_sell as vps
                    _hist = self._hist_klines(code, date)
                    if len(_hist) >= 6:
                        _vk, _vratio, _vr = vps.volp_sell_signal(_hist)
                        if _vk == "volp_surge_stall":
                            self._sell(date, code,
                                       self._sell_price(code, cp, pos["qty"], bar),
                                       pos["qty"], _vr)
                            continue   # 清仓，跳出本股
                        elif _vk == "volp_shrink_newhigh":
                            _hqty = int(pos["qty"] * C.VOLP_SELL_HALF_PCT / 100) * 100
                            if _hqty >= 100:
                                pos["volp_half_done"] = True
                                self._sell(date, code,
                                           self._sell_price(
                                               code, cp, min(_hqty, pos["qty"]), bar),
                                           min(_hqty, pos["qty"]), _vr)
                                if pos["qty"] <= 0:
                                    continue   # 半仓已清
                            # 剩余半仓继续走常规规则（已记录 volp_half_done 防重复半卖）
                except Exception:
                    pass
            # 常规退出规则（对剩余仓位评估；止损优先，全清止盈/回撤/移动止损/时间止损/超时）
            if pos["qty"] > 0:
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
                if not reason and pnl_pct < 0.05:
                    hist = self._hist_klines(code, date)
                    if _distribution_signal(hist):
                        reason = "主力出货"
                if reason:
                    # ★ P2-1 修复：回测卖出也计滑点（原零滑点，系统性高估卖出端收益）
                    self._sell(date, code, self._sell_price(code, cp, pos["qty"], bar),
                               pos["qty"], reason)

    # ---------- 主循环 ----------
    def run(self, progress_cb=None):
        self._load_data(progress_cb)
        if not self.trading_days:
            return {"error": "回测区间内无数据（检查股票池/日期/网络）"}

        for di, date in enumerate(self.trading_days):
            if progress_cb and di % 5 == 0:
                progress_cb(di, len(self.trading_days), phase="回放")
            # 1. 退出检查（T日收盘）
            self._check_exits(date)
            # 2. 买入：T-1 信号 → T 日开盘
            if len(self.positions) < self.max_pos:
                prev_date = self.trading_days[di - 1] if di > 0 else None
                if prev_date:
                    # ★ Phase27：回撤熔断状态 → 新开仓预算乘数。
                    #   PIT：状态在 t-1 收盘由总权益更新，t 日买入时只读取不修改。
                    #   halt=停止新开仓（signals 置空）；half=资金×half_mult。
                    _dd_mult = 1.0
                    if self.dd_gate:
                        if self._dd_state == "halt":
                            _dd_mult = 0.0
                            self.dd_halt_days += 1
                            self.dd_halt_dates.append(date)
                        elif self._dd_state == "half":
                            _dd_mult = float(self.dd_gate.get("half_mult") or 0.5)
                            self.dd_half_days += 1
                    # ★ Phase26：涨停生态温度计闸门（board；PIT 只用信号日 t-1 温度）：
                    #   stop 模式温度低于阈值 → 当日停止开新仓（signals 置空，退出/估值/
                    #   既有指数择时与情绪闸门逻辑完全不变）；mult 模式仅缩仓。
                    _zt_mult = 1.0
                    if self.strategy == "board" and self.zt_eco_gate:
                        try:
                            from . import zt_ecosystem as ze
                            _thr = self.zt_eco_gate.get("threshold")
                            _temp = (ze.eco_temperature(prev_date) or {}).get("temperature")
                            self.zt_gate_checked_days += 1
                            if _thr is not None and _temp is not None and _temp < _thr:
                                self.zt_gate_blocked_days += 1
                                if self.zt_eco_gate.get("mode") == "mult":
                                    _zt_mult = float(self.zt_eco_gate.get("mult")
                                                     or C.ZT_ECO_GATE_MULT)
                                    self.log_lines.append(
                                        f"{date} 涨停生态闸门×{_zt_mult}："
                                        f"t-1({prev_date})温度{_temp}<{_thr}")
                                else:
                                    _zt_mult = 0.0
                                    self.log_lines.append(
                                        f"{date} 涨停生态闸门："
                                        f"t-1({prev_date})温度{_temp}<{_thr} → 停止开新仓")
                        except Exception as _e:
                            self.log_lines.append(f"{date} 涨停生态闸门异常(不干预): {_e}")
                    signals = self._signals_on(prev_date) if (_zt_mult > 0 and _dd_mult > 0) else []
                    used = len(self.positions)
                    slots = self.max_pos - used
                    pos_cash = self.cash * self.pos_pct
                    # ★ v3.6：组合优化权重（非 equal 时按风险平价/HRP 分配单仓）
                    if self.portfolio_method != "equal" and signals:
                        cand_codes = [s["code"] for s in signals[:slots]]
                        closes_by_code = {}
                        for cc in cand_codes:
                            hist = self._hist_klines(cc, prev_date)
                            if len(hist) >= 30:
                                closes_by_code[cc] = [k["close"] for k in hist]
                        wts = portfolio.compute_weights(
                            closes_by_code, method=self.portfolio_method,
                            target_vol=C.PORTFOLIO_TARGET_VOL,
                            days=C.PORTFOLIO_LOOKBACK)
                        self.portfolio_weights = wts
                    else:
                        self.portfolio_weights = {}
                    bought = 0
                    for s in signals[:slots]:
                        if bought >= slots:
                            break
                        if self.cash < pos_cash * 0.5:
                            break
                        bar = self._bar(s["code"], date)
                        if not bar:
                            self.suspension_skips += 1  # v3.4: 信号日次日停牌/无K线
                            continue
                        open_px = bar["open"]
                        yest = self._bar(s["code"], prev_date)
                        if not yest:
                            continue
                        lu, ld = limit_prices(s["code"], yest["close"])
                        # 涨停/跌停开盘不成交
                        if open_px >= lu * 0.999 or open_px <= ld * 1.001:
                            continue
                        px = open_px * (1 + self.slippage)
                        # v3.6：组合权重 → 该股目标市值比例（否则用固定单仓比例）
                        # ★ 修复：w 是组合内占比，直接作单仓比例并受单票上限约束
                        #   （原 w*slots clamp 到 1 → 满仓单票，回测与实盘都被风控误拦）
                        w = self.portfolio_weights.get(s["code"])
                        if w:
                            w_cap = min(1.0, w, C.RISK_SINGLE_STOCK_PCT * 0.95)  # 留5%余量，防贴边浮点误拦
                            # ★ Phase15 修复：position_pct 是单仓预算上限，组合权重只决定相对分配
                            #   （原实现 w_cap 直接覆盖 pos_pct → 参数失效，敏感性矩阵三行相同）
                            pos_cash = self.cash * min(w_cap, self.pos_pct)
                        # ★ 4.6：指数择时闸门 —— 只调仓位（PIT：用信号日 prev_date 的指数状态，
                        #   不改变情绪周期与 w_cap 公式）；乘数理由记入 log_lines 供复盘
                        if self.index_timing:
                            try:
                                from . import index_timing as it
                                tim_mult, tim_reasons = it.timing_multiplier(as_of=prev_date)
                                if tim_mult < 1.0:
                                    pos_cash = pos_cash * tim_mult
                                    self.log_lines.append(
                                        f"{date} 指数择时×{tim_mult}：" + "；".join(tim_reasons))
                            except Exception:
                                pass
                        # ★ Phase16：情绪周期仓位闸门 —— PIT（用信号日 prev_date 阶段），
                        #   叠加在 min() 之后、指数乘数之后；只缩仓不扩仓，不超过 w_cap 预算
                        if C.SENTIMENT_POS_GATE:
                            try:
                                from . import sentiment_gate as sg
                                _sph = sg.phase_mult(as_of=prev_date)
                                if _sph < 1.0:
                                    pos_cash = pos_cash * _sph
                                    _ps = sg.phase_label(as_of=prev_date)
                                    self.log_lines.append(
                                        f"{date} 情绪闸门[{_ps[0] or '?'}]×{_sph}"
                                        + ("(低置信)" if _ps[2] else ""))
                            except Exception:
                                pass
                        # ★ Phase26：涨停生态温度乘数（mult 模式；stop 模式已在信号层阻断）
                        if 0 < _zt_mult < 1.0:
                            pos_cash = pos_cash * _zt_mult
                        # ★ Phase27：回撤熔断乘数（half 状态缩仓；halt 已在信号层阻断）
                        if 0 < _dd_mult < 1.0:
                            pos_cash = pos_cash * _dd_mult
                        qty = int(pos_cash / px / 100) * 100
                        if qty < 100:
                            continue
                        # ★ 4.2：撮合真实性引擎（市值分档滑点 + 涨停排队 + 停牌约束）
                        from . import execution as ex
                        # 停牌约束
                        ok_s, msg_s = ex.check_suspend(s["code"], bar=bar)
                        if not ok_s:
                            self.failed_fills += 1
                            continue
                        # 市值分档滑点
                        mcap = self.mcap_of.get(s["code"], 0) or 0
                        px = ex.execute_price(open_px, "buy", mcap,
                                              amount=px * qty,
                                              day_amount=bar.get("amount", 0) or 0,
                                              impact=True)
                        # 涨停排队成交概率（贴板时按封单强度）
                        if self.exec_prob:
                            prob, _note = ex.queue_fill_prob(
                                s["code"], px, lu, quote={"fund": 0, "float_mktcap": mcap},
                                hour=None)
                            if self.rng.random() > prob:
                                self.failed_fills += 1
                                continue
                            # 部分成交：单笔金额占当日成交额比例过高时打折
                            day_amt = bar.get("amount", 0) or (open_px * bar.get("volume", 0))
                            if day_amt > 0:
                                ratio = (px * qty) / day_amt
                                if ratio > C.EXEC_PARTIAL_CAP:
                                    fill_pct = C.EXEC_PARTIAL_CAP / ratio
                                    qty = int(qty * fill_pct / 100) * 100
                                    if qty < 100:
                                        self.failed_fills += 1
                                        continue
                        # ★ v3.7：行业敞口上限（回测约束 == 实盘约束）
                        if self.sector_limit_active and self.positions:
                            try:
                                from . import risk as rk
                                sec = rk.engine._sector_of(s["code"], self.names.get(s["code"], ""))
                                if sec:
                                    sec_mv = px * qty
                                    total_mv = self.cash + sum(
                                        p.get("qty", 0) * p.get("entry_price", 0)
                                        for p in self.positions.values())
                                    for c2, p2 in self.positions.items():
                                        if rk.engine._sector_of(c2, self.names.get(c2, "")) == sec:
                                            sec_mv += p2.get("qty", 0) * p2.get("entry_price", 0)
                                    if total_mv > 0 and sec_mv / total_mv > C.RISK_SECTOR_PCT:
                                        continue
                            except Exception:
                                pass
                        if self._buy(date, s["code"], px, qty,
                                     f"[{self.strategy}]评分{s['score']}分: " + "、".join(s["signals"][:3])):
                            bought += 1
                            if self.strategy == "board":
                                # ★ Phase48+49-B：打板仓标记（T+1 专属退出见 _check_exits）
                                self.positions[s["code"]]["board_trade"] = True
            # 3. 每日估值
            mv = 0.0
            for code, pos in self.positions.items():
                bar = self._bar(code, date)
                mv += (bar["close"] if bar else pos["entry_price"]) * pos["qty"]
            self.equity.append({"date": date, "equity": round(self.cash + mv, 2),
                                "cash": round(self.cash, 2), "mv": round(mv, 2)})
            # ★ Phase27：日终更新净值峰值与熔断状态机（次日买入时生效 → 天然 PIT）
            # ★ Phase37：恢复机制可配（default=Phase27 原逻辑；cooldown/index_ma/half_cap）
            if self.dd_gate:
                _eqv = self.cash + mv
                if _eqv > self._eq_peak:
                    self._eq_peak = _eqv
                _dd = (_eqv / self._eq_peak - 1.0) if self._eq_peak > 0 else 0.0
                _t1 = self.dd_gate.get("t1")
                _t2 = self.dd_gate.get("t2")
                _old = self._dd_state
                _rec = self._dd_recovery
                if _rec == "half_cap":
                    # R3：无 halt 状态；dd<=-T1（含原 -T2 深度）进入/维持 half，
                    # half 连续 M 个交易日后强制回 normal（若 dd 仍深，次日起重新计 M 天）
                    if self._dd_state == "normal":
                        if _dd <= -_t1:
                            self._dd_state = "half"
                            self._dd_half_streak = 0
                    else:
                        self._dd_half_streak += 1
                        if self._dd_half_cap and self._dd_half_streak >= self._dd_half_cap:
                            self._dd_state = "normal"
                            self._dd_half_streak = 0
                else:
                    if self._dd_state == "halt":
                        self._dd_halt_streak += 1
                    if self._dd_state == "halt":
                        # ★ Phase37 修正：恢复条件独立于当前 dd 深度评估——
                        #   cooldown"满 N 日无条件强制回 half"、index_ma"只看指数确认"，
                        #   二者在深度回撤（dd≤-T1/-T2）中同样必须生效。
                        if _rec == "index_ma":
                            _recovered = bool(self._dd_index_above_ma(date))
                        elif _rec == "cooldown":
                            _recovered = (_dd > -_t1 * 0.5) or (
                                self._dd_cooldown > 0
                                and self._dd_halt_streak >= self._dd_cooldown)
                        else:
                            _recovered = _dd > -_t1 * 0.5     # default（Phase27 原逻辑）
                        if _recovered:
                            self._dd_state = "half"
                            self._dd_halt_streak = 0
                        # 未恢复：halt 维持（含 dd≤-T2 深度）
                    elif _t2 is not None and _dd <= -_t2:
                        self._dd_state = "halt"               # normal/half → halt
                    elif _dd <= -_t1:
                        if self._dd_state == "normal":
                            self._dd_state = "half"           # normal→half；half 维持
                    elif self._dd_state == "half" and _dd > -_t1 * 0.5:
                        self._dd_state = "normal"
                if self._dd_state != _old:
                    self.log_lines.append(
                        f"{date} 回撤熔断[{_old}→{self._dd_state}] dd={_dd:.2%} "
                        f"(T1={_t1},T2={_t2},peak={self._eq_peak:.0f},rec={_rec})")

        # 末日清仓（按最后收盘，计滑点）
        last_date = self.trading_days[-1]
        for code in list(self.positions.keys()):
            pos = self.positions[code]
            bar = self._bar(code, last_date)
            if bar:
                self._sell(last_date, code,
                           self._sell_price(code, bar["close"], pos["qty"], bar),
                           pos["qty"], "期末清仓")

        return self._metrics()

    # ---------- 指标 ----------
    def _metrics(self):
        if len(self.equity) < 2:
            return {"error": "回测天数不足"}
        eq = [e["equity"] for e in self.equity]
        n = len(eq)
        total_ret = eq[-1] / self.capital - 1

        def annual_ret():
            days = n
            years = days / 244.0
            if years <= 0 or eq[0] <= 0:
                return 0.0
            return (eq[-1] / self.capital) ** (1 / years) - 1 if eq[-1] > 0 else -1.0

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
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        profit_factor = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)

        # 基准：池内等权平均收益
        bench = self._benchmark_curve()
        bench_ret = bench[-1] - 1 if bench else None

        # ★ v3.4：完整绩效分析（Sortino/Calmar/回撤区间/水下时间/月度收益热力图）
        # ★ 4.5：传入基准曲线计算 Alpha/Beta/信息比率（bench 含初始[1.0]，尾部对齐）
        bench_aligned = None
        if bench and len(bench) >= 2:
            b_tail = bench[1:] if len(bench) == len(self.equity) + 1 else bench
            if len(b_tail) == len(self.equity):
                bench_aligned = [(self.equity[i]["date"], b_tail[i])
                                 for i in range(len(self.equity))]
        pa = perf.analyze_equity(self.equity, self.trades, self.capital,
                                 benchmark=bench_aligned)

        return {
            "total_return": round(total_ret, 4),
            "annual_return": round(annual_ret(), 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe": round(sharpe, 3),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 2),
            "trade_count": len(self.trades),
            "buy_count": sum(1 for t in self.trades if t["side"] == "buy"),
            "sell_count": len(sells),
            "final_equity": round(eq[-1], 2),
            "benchmark_return": round(bench_ret, 4) if bench_ret is not None else None,
            "equity_curve": self.equity,
            "benchmark_curve": bench,
            "trades": self.trades[-500:],
            # v3.4 绩效分析扩展
            "performance": pa if "error" not in pa else None,
            # v3.4 回测真实性审计
            "suspension_skips": self.suspension_skips,
            "failed_fills": self.failed_fills,   # v3.8 成交模拟：未成交笔数
            "exec_prob_model": self.exec_prob,
            # Phase26：空仓天数占比 + 涨停生态闸门触发天数（对照分析用）
            "flat_day_ratio": round(sum(1 for e in self.equity if e["mv"] <= 0)
                                    / float(len(self.equity)), 4),
            "zt_gate_blocked_days": self.zt_gate_blocked_days,
            # Phase27：回撤熔断状态统计（默认关闭时恒为 0）
            "dd_half_days": self.dd_half_days,
            "dd_halt_days": self.dd_halt_days,
            "audit_notes": [
                "股票池按当前上市/活跃度选取，存在生存者偏差，真实表现可能略差",
                "前复权价格计算；分红送股可能造成历史价格失真",
                "ST/退市股已从股票池过滤；停牌日跳过交易",
            ],
        }

    def _benchmark_curve(self):
        """池内等权收益曲线（无交易成本）"""
        norm = {}
        for code, kl in self.klines.items():
            closes = {k["date"]: k["close"] for k in kl}
            norm[code] = closes
        curve = [1.0]
        prev = {}
        for e in self.equity:
            date = e["date"]
            day_ret = []
            for code in self.codes:
                cl = norm.get(code)
                if not cl:
                    continue
                if date in cl and code in prev and prev[code] and cl[date] > 0:
                    day_ret.append(cl[date] / prev[code] - 1)
            for code in self.codes:
                if date in norm.get(code, {}):
                    prev[code] = norm[code][date]
            if day_ret:
                curve.append(curve[-1] * (1 + sum(day_ret) / len(day_ret)))
            else:
                curve.append(curve[-1])
        return curve


# ============ 次日溢价统计（打板研究） ============
def board_premium(codes, names, start, end):
    """统计打板候选的次日表现：高开溢价 / 收盘溢价 / 最高溢价 / 胜率"""
    rows = []
    for code in codes:
        kl = df.fetch_kline(code, "day", 400)
        if len(kl) < 40 or not sc.is_main_board(code):
            continue
        for i in range(30, len(kl) - 1):
            d = kl[i]["date"]
            if not (start <= d <= end):
                continue
            prev = kl[i - 1]
            pct = (kl[i]["close"] - prev["close"]) / prev["close"] * 100
            if not (C.BOARD_MIN_PCT <= pct < C.BOARD_MAX_PCT):
                continue
            nxt = kl[i + 1]
            if nxt["volume"] <= 0:
                continue  # 次日停牌
            open_prem = (nxt["open"] - kl[i]["close"]) / kl[i]["close"] * 100
            close_prem = (nxt["close"] - kl[i]["close"]) / kl[i]["close"] * 100
            hi_prem = (nxt["high"] - kl[i]["close"]) / kl[i]["close"] * 100
            rows.append({
                "date": d, "next_date": nxt["date"], "code": code,
                "name": names.get(code, code), "pct": round(pct, 2),
                "open_prem": round(open_prem, 2),
                "close_prem": round(close_prem, 2),
                "high_prem": round(hi_prem, 2),
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


# ============ v3.4 / Phase13：参数敏感度扫描（单/双参数） ============
def sensitivity_scan(codes, names, start, end, strategy="score",
                     capital=100000.0, params=None, param="buy_threshold",
                     base=25, steps=(0.8, 1.0, 1.2),
                     param2=None, values2=None):
    """参数敏感度扫描（过拟合检验）。
    - 单参数（默认）：对 param 做 ±扫描，返回 [{param, value, total_return, sharpe,
      max_drawdown, win_rate, trade_count}]（旧格式，向后兼容）。
    - 双参数（param2+values2 传入）：对 param × param2 做二维扫描，返回结构
      {param, param2, values2, matrix, rows}——matrix 为 收益矩阵
      [len(values2) 行 × len(steps) 列]（行=param2，列=param，值为 total_return），
      rows 为全量平坦列表（含两参数取值）；单参数调用返回格式不变。
    ★ H3：同 codes/区间共享一次 _load_data（网格各次参数不同，数据无关）。
    """
    _shared = None
    try:
        _loader = Backtest(codes, names, start, end, capital, strategy, dict(params or {}))
        _loader._load_data()
        if _loader.klines:
            _shared = {
                "codes": list(_loader.codes), "start": start, "end": end,
                "warmup": _loader.warmup,
                "klines": _loader.klines, "date_index": _loader.date_index,
                "trading_days": list(_loader.trading_days),
                "start_idx": dict(_loader.start_idx),
                "mcap_of": dict(_loader.mcap_of),
                "_karr": _loader._karr, "_ind": _loader._ind,
                "_karr_dtype": _loader._karr_dtype,
            }
    except Exception:
        _shared = None
    if param2 is not None and values2:
        # ---- 双参数二维扫描 ----
        p2_list = list(values2)
        rows = []
        matrix = []
        for v2 in p2_list:
            row = []
            for f in steps:
                p = dict(params or {})
                p[param] = round(base * f, 2)
                p[param2] = _cv(v2)
                bt = Backtest(codes, names, start, end, capital, strategy, p,
                              _preloaded=_shared)
                try:
                    r = bt.run()
                except Exception:
                    continue
                if "error" in r:
                    row.append(None)
                    continue
                row.append(r.get("total_return"))
                rows.append({
                    "param": param, "value": round(base * f, 2),
                    "param2": param2, "value2": v2,
                    "total_return": r.get("total_return"),
                    "sharpe": r.get("sharpe"),
                    "max_drawdown": r.get("max_drawdown"),
                    "win_rate": r.get("win_rate"),
                    "trade_count": r.get("trade_count"),
                })
            matrix.append(row)
        return {
            "param": param, "param2": param2,
            "values2": p2_list,
            "matrix": matrix,          # [row=param2, col=param] 收益矩阵
            "rows": rows,
            "dual": True,
        }
    # ---- 单参数（旧行为） ----
    rows = []
    for f in steps:
        p = dict(params or {})
        p[param] = round(base * f, 2)
        bt = Backtest(codes, names, start, end, capital, strategy, p)
        try:
            r = bt.run()
        except Exception:
            continue
        if "error" in r:
            continue
        rows.append({
            "param": param, "value": p[param],
            "total_return": r.get("total_return"),
            "sharpe": r.get("sharpe"),
            "max_drawdown": r.get("max_drawdown"),
            "win_rate": r.get("win_rate"),
            "trade_count": r.get("trade_count"),
        })
    return rows


def _cv(v):
    """安全转数值（param2 值）"""
    try:
        return int(v) if float(v).is_integer() else float(v)
    except Exception:
        return v


# ============ v3.9：参数自动寻优（网格/随机搜索） ============
def optimize_params(codes, names, start, end, strategy="score",
                    capital=100000.0, params=None, grid=None,
                    method="grid", max_iters=60, metric="sharpe"):
    """核心参数自动寻优。
    grid: {param: [候选值,...]}，如 {"buy_threshold":[20,25,30], "max_positions":[2,3,4]}
    method: grid（全组合，受 max_iters 限制）| random（随机采样）
    metric: 优化目标（sharpe | total_return | calmar）
    返回 {best: {...}, results: [按指标排序的完整表], searched: n}
    """
    import itertools
    import random as _rnd
    grid = grid or {"buy_threshold": [20, 25, 30],
                    "max_positions": [2, 3, 4],
                    "position_pct": [0.2, 0.3]}
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    if method == "random" and len(combos) > max_iters:
        _rnd.seed(42)
        combos = _rnd.sample(combos, max_iters)
    elif len(combos) > max_iters:
        combos = combos[:max_iters]

    rows = []
    # ★ H3：网格共享一次数据加载（同 codes/区间/warmup）；combos 之间天然独立 → 并行
    _shared = None
    try:
        _loader = Backtest(codes, names, start, end, capital, strategy, dict(params or {}))
        _loader._load_data()
        if _loader.klines:
            _shared = {
                "codes": list(_loader.codes), "start": start, "end": end,
                "warmup": _loader.warmup,
                "klines": _loader.klines, "date_index": _loader.date_index,
                "trading_days": list(_loader.trading_days),
                "start_idx": dict(_loader.start_idx),
                "mcap_of": dict(_loader.mcap_of),
                "_karr": _loader._karr, "_ind": _loader._ind,
                "_karr_dtype": _loader._karr_dtype,
            }
    except Exception:
        _shared = None

    def _run_combo(combo):
        p = dict(params or {})
        for k, v in zip(keys, combo):
            p[k] = v
        try:
            bt = Backtest(codes, names, start, end, capital, strategy, p,
                          _preloaded=_shared)
            r = bt.run()
            if "error" in r:
                return None
            pa = r.get("performance") or {}
            score_val = (r.get("sharpe") if metric == "sharpe"
                         else r.get("total_return") if metric == "total_return"
                         else pa.get("calmar") if metric == "calmar" else r.get("sharpe"))
            return {
                "params": {k: v for k, v in zip(keys, combo)},
                "metric": round(score_val, 4) if score_val is not None else None,
                "total_return": r.get("total_return"),
                "sharpe": r.get("sharpe"),
                "max_drawdown": r.get("max_drawdown"),
                "win_rate": r.get("win_rate"),
                "trade_count": r.get("trade_count"),
                "calmar": pa.get("calmar"),
            }
        except Exception:
            return None

    _par = bool(getattr(C, "BT_PARALLEL_ENABLED", True)) and len(combos) >= 4
    if _par:
        _pool = pools.get_pool("bt_optimize",
                               max(1, int(getattr(C, "BT_PARALLEL_WORKERS", 8) or 8)))
        _futs = [_pool.submit(_run_combo, c) for c in combos]
        rows = [f.result() for f in _futs]          # 按 combos 顺序收集（确定性）
        rows = [r for r in rows if r is not None]
    else:
        rows = [_run_combo(c) for c in combos]
        rows = [r for r in rows if r is not None]
    rows.sort(key=lambda x: -(x.get("metric") if x.get("metric") is not None else -9))
    best = rows[0] if rows else None
    return {
        "best": best,
        "results": rows[:50],
        "searched": len(rows),
        "metric": metric,
        "param_keys": keys,
    }


# ============ v3.5 / Phase10：walk-forward 样本外验证（滚动切分 + 网格调参） ============
def walk_forward(codes, names, start, end, strategy="score",
                 capital=100000.0, params=None, folds=3,
                 train_days=252, test_days=126, step_days=126, anchored=False):
    """滚动训练-测试切分回测（Phase10 升级）：
    - 滚动窗口：train_days 训练 / test_days 测试 / step_days 步长；anchored=True 时训练起点固定、
      终点随折滚动（终点=anchor 起点 + i*step + train_days）。
    - 兼容旧调用：显式传 folds 时自动换算为等长滚动切分（train=test=step=total//(folds+1)），
      旧调用 walk_forward(..., folds=3) 不报错、语义等价。
    - 训练段调参升级为小网格：buy_threshold × max_positions（默认 [20,25,30]×[2,3,4]），
      选优指标 = 收益 − 0.5×|回撤|，要求 trade_count≥5，无合格候选回退默认参数。
    - 每折输出 degradation（衰减率 = 1 − OOS/IS，IS≤0 置 None）；汇总含平均衰减率。
    返回 {folds: [...], summary: {...}}（字段向后兼容：fold/train/test/best_threshold/
          is_return/oos_* 保留，新增 best_params/degradation）。
    """
    from datetime import datetime, timedelta
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d")
        d1 = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return {"error": "日期格式错误"}
    total_days = (d1 - d0).days
    if total_days < 200:
        return {"error": "区间过短（需要>=200天）"}
    # ---- 窗口换算：优先显式滚动参数；否则旧式 folds（含默认3）→ 等长滚动切分 ----
    _exp = (train_days != 252 or test_days != 126 or step_days != 126)
    if not _exp:
        # 未显式给滚动参数 → 用 folds 换算等长滚动（兼容旧调用 folds=3）
        folds = int(folds or 3)
        if folds < 2:
            return {"error": "折数过少（需要>=2折）"}
        seg = total_days // (folds + 1)
        if seg < 20:
            return {"error": f"区间过短：{folds} 折每段仅 {seg} 天"}
        train_days, test_days, step_days = seg, seg, seg
    # ---- 计算折档（时间轴）----
    result_folds = []
    oos_rets = []
    _test_days = max(1, int(test_days))
    _step_days = max(1, int(step_days))
    _train_days = max(1, int(train_days))
    # anchored: 训练起点固定 d0，终点 = d0 + train_days + i*step
    anchor_start = d0
    i = 0
    while True:
        if anchored:
            t0 = anchor_start
            t1 = anchor_start + timedelta(days=_train_days + i * _step_days)
            v0 = t1 + timedelta(days=1)
            v1 = v0 + timedelta(days=_test_days - 1)
        else:
            t0 = d0 + timedelta(days=i * _step_days)
            t1 = t0 + timedelta(days=_train_days - 1)
            v0 = t1 + timedelta(days=1)
            v1 = v0 + timedelta(days=_test_days - 1)
        if v0 > d1:
            break
        if v1 > d1:
            v1 = d1  # 末折测试段收口到 end
        if (v1 - v0).days < max(1, _test_days // 2):
            break  # 残余过短则不再开折
        t0s, t1s = t0.strftime("%Y-%m-%d"), t1.strftime("%Y-%m-%d")
        v0s, v1s = v0.strftime("%Y-%m-%d"), v1.strftime("%Y-%m-%d")
        # ---- 训练段：buy_threshold × max_positions 小网格选优 ----
        best_params, best = _wf_grid_select(codes, names, t0s, t1s, strategy,
                                            capital, params)
        # ---- 测试段：用 IS 最优参数跑 OOS ----
        bt = Backtest(codes, names, v0s, v1s, capital, strategy, best_params)
        try:
            r = bt.run()
        except Exception:
            r = {"error": str(sys_exc())}
        if "error" not in r:
            oos_rets.append(r.get("total_return", 0))
        # ---- degradation：衰减率 = 1 − OOS/IS ----
        is_ret = best.get("total_return") if best else None
        oos_ret = r.get("total_return") if "error" not in r else None
        deg = None
        if is_ret is not None and is_ret > 0 and oos_ret is not None:
            deg = 1 - oos_ret / is_ret
        elif is_ret is not None and is_ret <= 0:
            deg = None  # IS 非正，衰减率无意义
        result_folds.append({
            "fold": len(result_folds) + 1,
            "train": [t0s, t1s], "test": [v0s, v1s],
            "anchored": anchored,
            "best_threshold": (best.get("params") or {}).get("buy_threshold")
                              if best else None,
            "best_params": best.get("params") if best else None,
            "is_return": is_ret,
            "oos_return": oos_ret,
            "oos_sharpe": r.get("sharpe") if "error" not in r else None,
            "oos_max_drawdown": r.get("max_drawdown") if "error" not in r else None,
            "oos_win_rate": r.get("win_rate") if "error" not in r else None,
            "oos_trades": r.get("trade_count") if "error" not in r else None,
            "degradation": round(deg, 4) if deg is not None else None,
        })
        i += 1
        if len(result_folds) >= 20:  # 防死循环保险
            break
    if not result_folds:
        return {"error": "区间过短，无法形成任何滚动折"}
    # ---- 汇总 ----
    oos = [f["oos_return"] for f in result_folds if f["oos_return"] is not None]
    degs = [f["degradation"] for f in result_folds if f["degradation"] is not None]
    positive = sum(1 for v in oos if v > 0)
    avg_oos = sum(oos) / len(oos) if oos else None
    avg_deg = sum(degs) / len(degs) if degs else None
    stable = len(oos) >= 2 and positive / len(oos) >= 0.5 and avg_oos is not None and avg_oos > 0
    return {
        "folds": result_folds,
        "summary": {
            "folds": len(result_folds),
            "window": {"train_days": _train_days, "test_days": _test_days,
                       "step_days": _step_days, "anchored": anchored},
            "avg_oos_return": round(avg_oos, 4) if avg_oos is not None else None,
            "avg_degradation": round(avg_deg, 4) if avg_deg is not None else None,
            "positive_folds": positive,
            "stable": stable,
            "verdict": ("✅ 样本外稳健：多数折叠为正收益" if stable
                        else "⚠️ 样本外不稳定：可能存在过拟合或策略失效"),
        },
    }


def _wf_grid_select(codes, names, t0, t1, strategy, capital, params):
    """训练段小网格选优：buy_threshold × max_positions。
    选优指标 = 收益 − 0.5×|回撤|；要求 trade_count≥5；无候选回退默认 params。
    返回 (best_params, best_info)；best_info 含 params/total_return/max_drawdown。
    ★ H3：网格 9 组共享一次 _load_data（同 codes/区间/warmup），预计算一次。
    """
    p = dict(params or {})
    grid_bt = [20, 25, 30]
    grid_mp = [2, 3, 4]
    # ---- H3：共享数据（一次加载 + 预计算）；BT_NO_SHARE=1 回退逐次加载（对照测试用）----
    _shared = None
    if not os.environ.get("BT_NO_SHARE"):
        try:
            _loader = Backtest(codes, names, t0, t1, capital, strategy, dict(p))
            _loader._load_data()
            if _loader.klines:
                _shared = {
                    "codes": list(_loader.codes), "start": t0, "end": t1,
                    "warmup": _loader.warmup,
                    "klines": _loader.klines, "date_index": _loader.date_index,
                    "trading_days": list(_loader.trading_days),
                    "start_idx": dict(_loader.start_idx),
                    "mcap_of": dict(_loader.mcap_of),
                    "_karr": _loader._karr, "_ind": _loader._ind,
                    "_karr_dtype": _loader._karr_dtype,
                }
        except Exception:
            _shared = None
    rows = []
    for bt_v in grid_bt:
        for mp in grid_mp:
            pp = dict(p)
            pp["buy_threshold"] = bt_v
            pp["max_positions"] = mp
            try:
                bt = Backtest(codes, names, t0, t1, capital, strategy, pp,
                              _preloaded=_shared)
                r = bt.run()
            except Exception:
                continue
            if "error" in r:
                continue
            tc = r.get("trade_count", 0) or 0
            if tc < 5:
                continue
            rows.append({
                "params": {"buy_threshold": bt_v, "max_positions": mp},
                "total_return": r.get("total_return"),
                "max_drawdown": r.get("max_drawdown"),
                "trade_count": tc,
            })
    if not rows:
        return dict(p), None
    best = max(rows, key=lambda s: (s.get("total_return") or -9)
               - abs(s.get("max_drawdown") or 0) * 0.5)
    best_params = dict(p)
    best_params.update(best["params"])
    return best_params, best


# ============ Phase12：跨年代多窗口验证 ============
def multi_window(codes, names, windows=None, strategy="score",
                 capital=100000.0, params=None):
    """跨年代多窗口稳健性验证：
    同一套参数（固定 params）在多个年代窗口各跑一次 Backtest，
    对比不同市场环境（牛/熊/震荡）下的表现是否稳定。

    windows: [["YYYY-MM-DD","YYYY-MM-DD"], ...]；缺省用预设常覆盖牛熊震荡的 4 窗口
    （2019-20、2021-22、2023-24、近一年），本地库若无对应历史会自动跳过并标注。

    返回 {
      windows: [{window, start, end, annual_return, sharpe, max_drawdown,
                 win_rate, trade_count, total_return, skipped?, error?}],
      summary: {count, avg_annual, std_annual, positive_folds, positive_ratio,
                verdict, params}
    }
    """
    import statistics
    if not windows:
        windows = [
            ["2019-01-01", "2020-12-31"],
            ["2021-01-01", "2022-12-31"],
            ["2023-01-01", "2024-12-31"],
            ["2025-08-18", "2026-08-18"],
        ]
    out_windows = []
    for w in windows:
        try:
            w0, w1 = w[0], w[1]
        except (IndexError, TypeError):
            continue
        bt = Backtest(codes, names, w0, w1, capital, strategy, dict(params or {}))
        try:
            r = bt.run()
        except Exception:
            r = {"error": str(sys_exc())}
        if "error" in r:
            out_windows.append({"window": [w0, w1], "start": w0, "end": w1,
                                "skipped": True, "error": r.get("error", "")})
            continue
        out_windows.append({
            "window": [w0, w1], "start": w0, "end": w1,
            "annual_return": r.get("annual_return"),
            "sharpe": r.get("sharpe"),
            "max_drawdown": r.get("max_drawdown"),
            "win_rate": r.get("win_rate"),
            "trade_count": r.get("trade_count"),
            "total_return": r.get("total_return"),
        })
    ok = [x for x in out_windows if not x.get("skipped")]
    anns = [x["annual_return"] for x in ok if x.get("annual_return") is not None]
    pos = sum(1 for x in ok if (x.get("total_return") or 0) > 0)
    if anns:
        avg_ann = sum(anns) / len(anns)
        std_ann = statistics.pstdev(anns) if len(anns) > 1 else 0.0
    else:
        avg_ann, std_ann = None, None
    ratio = (pos / len(ok)) if ok else None
    # 一句话结论
    if not ok:
        verdict = "⚠️ 无有效窗口（本地库历史不足，建议补库或缩小窗口）"
    elif ratio is None:
        verdict = "⚠️ 数据不足，无法判定"
    elif avg_ann is not None and avg_ann > 0 and ratio >= 0.5 and std_ann is not None and std_ann <= 0.5:
        verdict = f"✅ 多窗口稳健：{pos}/{len(ok)} 窗口正收益，平均年化 {avg_ann:.1%}、波动 {std_ann:.1%}"
    elif avg_ann is not None and (avg_ann > 0 or ratio >= 0.5):
        verdict = f"⚠️ 存在盈利窗口但波动较高（平均年化 {avg_ann:.1%}，std {std_ann if std_ann is not None else '—'}），需谨慎"
    else:
        verdict = f"⚠️ 多数窗口亏损（{pos}/{len(ok)} 正收益），策略缺乏跨年代有效性"
    return {
        "windows": out_windows,
        "summary": {
            "count": len(ok),
            "avg_annual": round(avg_ann, 4) if avg_ann is not None else None,
            "std_annual": round(std_ann, 4) if std_ann is not None else None,
            "positive_folds": pos,
            "positive_ratio": round(ratio, 4) if ratio is not None else None,
            "verdict": verdict,
            "params": dict(params or {}),
        },
    }


def sys_exc():
    import sys
    return sys.exc_info()[1]
