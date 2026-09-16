# -*- coding: utf-8 -*-
"""★ Phase26 涨停生态情绪温度计（涨停生态 daily 指标 → 合成温度 0~100）

指标定义（全部由 kline 日线一遍扫描推导，口径与回测一致）：
  涨停判定复用 engine.limit_prices / limit_pct_of（主板10% 创业/科创20% 北交所30%，
  四舍五入到分），close == 涨停价 记涨停、close == 跌停价 记跌停。
  样本：沪深京股票（60/00/30/68 开头，库内即此范围；ETF/指数垃圾行自动排除），
        排除 DATA_EXCLUDE_CODES（复权不可靠票）、名称含 ST 的票（当前名单口径）、
        上市未满 60 个自身交易日的新股（每股跳过前 60 根 bar）。

日频分量：
  1. zt_count       涨停家数
  2. dt_count       跌停家数
  3. broken_rate    炸板率 = (high触板且收盘<板) 家数 / high触板家数
  4. max_streak     全市场最大连板高度；另记 lb_ge2/lb_ge3 连板≥2/≥3 家数
  5. yzt_ret_mean   赚钱效应：昨日涨停股今日收益均值（median 另存 yzt_ret_med）；
                    core_count = 昨日涨停且今日跌停家数（"核按钮"）
  6. seal_strength  封板强度代理：涨停股 (close-open)/open 均值

合成情绪温度 temperature ∈ [0,100]：
  - 各分量先按方向对齐（越高越热：跌停数/炸板率取负号），再做 120 交易日滚动分位
    （窗口内不足 80% 样本视为预热中 → None），杜绝未来函数；
  - 权重 = 等权起步 + 单因子 IC 检验调整：w_i ∝ max(IC_i, 0) + 0.05（归一化），
    IC 为 Spearman 秩相关（因子滚动分位 vs 次日赚钱效应 yzt_ret_mean）。
    理由：等权是无先验的中性起点；IC>0 的因子对打板次日收益有正向预测力应加权，
    IC≤0 的因子降权但不归零（保留信息多样性，防单因子失效时温度失真）。
    ⚠️ 诚实声明：权重与阈值分位基于全历史拟合，属样本内标定；
    过拟合风险由回测端"多窗口一致性 + 阈值单调性"判定规则约束（见 Phase26 报告）。

落库缓存：data/zt_eco.json（带 generated_at 与 data_max_date；kline 有更新日时
  下次调用自动重建）。重复调用走缓存，不重复扫库。

对外接口：
  build(force=False)          全量计算并写缓存，返回 {"series": [...], "meta": {...}}
  eco_temperature(as_of)      PIT：只返回 as_of 当日及以前的最新一行（无则 None）
  quantile_thresholds(qs)     历史温度分位阈值 {q: value}（供闸门网格）
"""
import json
import os
import sqlite3
import statistics
import time

from . import config as C
from .engine import limit_prices

DATA_JSON = os.path.join(C.DATA_DIR, "zt_eco.json")
ROLL_WINDOW = 120          # 滚动分位窗口（交易日）
MIN_LISTED_BARS = 60       # 新股排除：前 60 根自身日线不计
WARM_MIN_RATIO = 0.8       # 分位窗口最少样本比例
EPS = 0.005                # 价格比较容差（分级四舍五入后浮点安全余量）
STOCK_PREFIXES = ("60", "00", "30", "68")   # 库内股票代码前缀（排除 ETF/异常行）

# 分量定义: (字段, 方向符号, 说明)。方向 +1=越大越热，-1=越大越冷
FACTORS = [
    ("zt_count", +1, "涨停家数"),
    ("dt_count", -1, "跌停家数"),
    ("broken_rate", -1, "炸板率"),
    ("max_streak", +1, "连板高度"),
    ("yzt_ret_mean", +1, "昨日涨停股今日收益均值"),
    ("seal_strength", +1, "封板强度(close-open)/open"),
]


# ---------------------------------------------------------------- 名称与扫描
def _load_names():
    """{code: name}（stock_list.json 当前名单；仅用于 ST 过滤）"""
    try:
        path = os.path.join(C.DATA_DIR, "stock_list.json")
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        return {r[0]: (r[1] or "") for r in rows
                if isinstance(r, (list, tuple)) and len(r) >= 2}
    except Exception:
        return {}


def _db_max_date(conn):
    row = conn.execute(
        "SELECT MAX(date) FROM kline WHERE period='day'").fetchone()
    return row[0] if row else None


def _scan_universe(conn):
    """一次查询全市场日线并按股票分组流式过滤（不逐股逐日查库）。
    返回 per_code: {code: [(date, open, high, close), ...]}（按日期升序）。"""
    ex = set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])
    names = _load_names()
    per_code = {}
    for code, d, o, h, cl in conn.execute(
            "SELECT code, date, open, high, close FROM kline "
            "WHERE period='day' AND close IS NOT NULL AND close > 0 "
            "ORDER BY code, date"):
        if not code.startswith(STOCK_PREFIXES):
            continue                      # ETF/指数等非股票行
        if code in ex:
            continue                      # 复权不可靠例外票（Phase21 名单）
        if "ST" in (names.get(code) or "").upper():
            continue                      # ST 票（±5% 板，生态外样本）
        per_code.setdefault(code, []).append((d, o or cl, h or cl, cl))
    return per_code


class _DayAcc(object):
    """单日指标累加器"""
    __slots__ = ("zt", "dt", "touched", "broken", "lb2", "lb3", "max_streak",
                 "yzt_rets", "core", "seal_sum", "seal_n")

    def __init__(self):
        self.zt = self.dt = self.touched = self.broken = 0
        self.lb2 = self.lb3 = 0
        self.max_streak = 0
        self.yzt_rets = []
        self.core = 0
        self.seal_sum = 0.0
        self.seal_n = 0


def _compute_daily(per_code):
    """单遍扫描：每股顺序遍历自身 bar 序列，涨停/连板/炸板/赚钱效应一次算齐。
    昨日涨停今日收益在"相邻两根 bar"层面即可完成（该股昨板今收），
    无需跨股状态，天然 O(N)。返回 (day, zt_pool)：
      day: {date: _DayAcc}
      zt_pool: {date: set(code)}——★ K11（2026-09-16）新增：eco 权威口径逐日涨停池
      （close==limit_prices 精确板价才算），供 qg_zt_full 重算与口径诊断复用，
      避免"第二套涨停判定逻辑"。"""
    day = {}
    zt_pool = {}
    for code, bars in per_code.items():
        run = 0                       # 该股当前连续涨停天数
        prev_zt = False               # 上一根 bar 是否涨停
        for i in range(MIN_LISTED_BARS, len(bars)):
            d, o, h, cl = bars[i]
            pc = bars[i - 1][3]
            if pc <= 0:
                prev_zt = False
                run = 0
                continue
            lu, ld = limit_prices(code, pc)
            is_zt = abs(cl - lu) < EPS
            is_dt = abs(cl - ld) < EPS
            touched = h >= lu - EPS
            acc = day.get(d)
            if acc is None:
                acc = day[d] = _DayAcc()
            if touched:
                acc.touched += 1
                if not is_zt:
                    acc.broken += 1
            if is_zt:
                acc.zt += 1
                zt_pool.setdefault(d, set()).add(code)
                run += 1
                if run >= 2:
                    acc.lb2 += 1
                if run >= 3:
                    acc.lb3 += 1
                if run > acc.max_streak:
                    acc.max_streak = run
                if o > 0:
                    acc.seal_sum += (cl - o) / o
                    acc.seal_n += 1
            else:
                run = 0
            if is_dt:
                acc.dt += 1
            if prev_zt and i >= MIN_LISTED_BARS:
                # 赚钱效应：昨日涨停股今日收益；核按钮 = 昨板今跌停
                acc.yzt_rets.append(cl / pc - 1.0)
                if is_dt:
                    acc.core += 1
            prev_zt = is_zt
    return day, zt_pool


def daily_zt_pool():
    """★ K11（2026-09-16）：返回 {date: set(code)}——eco 权威口径逐日涨停池。
    与 eco_temperature().zt_count 完全同源（zt_count == len(pool[date])）：
      60/00/30/68 前缀 + 排除 DATA_EXCLUDE_CODES/ST + 上市满 60 自身交易日 +
      engine.limit_prices 精确板价（四舍五入到分）close 封板。
    供 qg_zt_full 重算与口径诊断复用，杜绝第二套涨停判定逻辑。"""
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=30)
    try:
        per_code = _scan_universe(conn)
    finally:
        conn.close()
    _, zt_pool = _compute_daily(per_code)
    return zt_pool


# ---------------------------------------------------------------- 合成温度
def _rolling_pct(values, window=ROLL_WINDOW):
    """滚动历史分位（含当日，只用 t-window+1..t）：值在窗口内的不超过比例。
    预热不足（<80% 窗口）返回 None。"""
    out = [None] * len(values)
    for i, v in enumerate(values):
        if v is None:
            continue
        lo = max(0, i - window + 1)
        win = [x for x in values[lo:i + 1] if x is not None]
        if len(win) < window * WARM_MIN_RATIO:
            continue
        out[i] = sum(1 for x in win if x <= v) / float(len(win))
    return out


def _rank(seq):
    """平均秩（并列取平均），用于 Spearman"""
    order = sorted(range(len(seq)), key=lambda i: seq[i])
    ranks = [0.0] * len(seq)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and seq[order[j + 1]] == seq[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs, ys):
    n = min(len(xs), len(ys))
    if n < 30:
        return None
    xs, ys = xs[:n], ys[:n]
    rx, ry = _rank(xs), _rank(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def _synthesize(series):
    """滚动分位 + IC 权重 → temperature。series 元素为 dict（原位补充字段）。"""
    dates = [s["date"] for s in series]
    pct_by_factor = {}
    ics = {}
    # 次日赚钱效应目标（IC 用；末日无次日记 None）
    nxt_target = [None] * len(series)
    for i in range(len(series) - 1):
        nxt_target[i] = series[i + 1].get("yzt_ret_mean")
    aligned = {}
    for key, sign, _desc in FACTORS:
        vals = [None if s.get(key) is None else sign * s[key] for s in series]
        pct_by_factor[key] = _rolling_pct(vals)
        aligned[key] = pct_by_factor[key]
        pairs_x, pairs_y = [], []
        for i in range(len(series)):
            if pct_by_factor[key][i] is not None and nxt_target[i] is not None:
                pairs_x.append(pct_by_factor[key][i])
                pairs_y.append(nxt_target[i])
        ics[key] = _spearman(pairs_x, pairs_y)
    # 权重：等权起步，按 max(IC,0)+0.05 调整后归一化
    raw = {k: (max(ics[k], 0.0) + 0.05) if ics[k] is not None else (1.0 / len(FACTORS))
           for k, _s, _d in FACTORS}
    tot = sum(raw.values())
    weights = {k: v / tot for k, v in raw.items()}
    for i, s in enumerate(series):
        num = 0.0
        ok = True
        for key, _sign, _desc in FACTORS:
            p = pct_by_factor[key][i]
            if p is None:
                ok = False
                break
            num += weights[key] * p
        s["temperature"] = round(num * 100.0, 1) if ok else None
    meta = {
        "factor_ic": {k: (round(v, 4) if v is not None else None) for k, v in ics.items()},
        "weights": {k: round(v, 4) for k, v in weights.items()},
        "ic_target": "次日赚钱效应 yzt_ret_mean(t+1)，Spearman 秩相关",
        "weight_rule": "w_i ∝ max(IC_i,0)+0.05 归一化（等权起步+IC调整，全历史样本内标定）",
    }
    return dates, meta


# ---------------------------------------------------------------- 构建/缓存
_MEMO = {"data": None, "last_check": 0.0}   # 进程内缓存（回测逐日查询免重复读 JSON）
_CHECK_INTERVAL = 60.0    # 库更新校验节流（秒）：日频数据无需逐调用查 MAX(date)


def build(force=False, use_cache=True):
    """全量计算涨停生态日频序列与合成温度。缓存 data/zt_eco.json。"""
    if use_cache and not force:
        cached = _load_cache()
        if cached is not None:
            return cached
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=30)
    try:
        data_max_date = _db_max_date(conn)
        per_code = _scan_universe(conn)
    finally:
        conn.close()
    day, _ = _compute_daily(per_code)
    series = []
    for d in sorted(day.keys()):
        a = day[d]
        broken_rate = (a.broken / float(a.touched)) if a.touched else None
        yzt_mean = (sum(a.yzt_rets) / len(a.yzt_rets)) if a.yzt_rets else None
        yzt_med = statistics.median(a.yzt_rets) if a.yzt_rets else None
        series.append({
            "date": d,
            "zt_count": a.zt,
            "dt_count": a.dt,
            "touched": a.touched,
            "broken_rate": round(broken_rate, 4) if broken_rate is not None else None,
            "max_streak": a.max_streak,
            "lb_ge2": a.lb2,
            "lb_ge3": a.lb3,
            "yzt_ret_mean": round(yzt_mean, 6) if yzt_mean is not None else None,
            "yzt_ret_med": round(yzt_med, 6) if yzt_med is not None else None,
            "core_count": a.core,
            "seal_strength": round(a.seal_sum / a.seal_n, 6) if a.seal_n else None,
        })
    _dates, meta = _synthesize(series)
    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_max_date": data_max_date,
        "universe": {
            "codes": len(per_code),
            "prefixes": list(STOCK_PREFIXES),
            "excluded": ["DATA_EXCLUDE_CODES", "ST名称", "上市未满60交易日"],
        },
        "params": {
            "roll_window": ROLL_WINDOW,
            "min_listed_bars": MIN_LISTED_BARS,
            "limit_rule": "engine.limit_prices 10%/20%/30%（四舍五入到分）",
        },
        "meta": meta,
        "thresholds": quantile_thresholds_from(series),
        "series": series,
    }
    try:
        tmp = DATA_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp, DATA_JSON)
    except Exception:
        pass                     # 缓存写失败不影响返回（只影响下次复用）
    _MEMO["data"] = result
    _MEMO["last_check"] = time.time()
    return result


def _load_cache():
    now = time.time()
    hit = _MEMO.get("data")
    if hit is not None and now - _MEMO.get("last_check", 0.0) < _CHECK_INTERVAL:
        return hit              # 节流：间隔内直接用进程内缓存（日频数据足够新鲜）
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=10)
        try:
            cur_max = _db_max_date(conn)
        finally:
            conn.close()
        _MEMO["last_check"] = now
        if hit is not None and hit.get("data_max_date") == cur_max:
            return hit
        with open(DATA_JSON, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("data_max_date") == cur_max:
            _MEMO["data"] = cached
            return cached
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- PIT 接口
def eco_temperature(as_of):
    """PIT 温度查询：只看 as_of 当日及以前，返回最新一行 dict（无则 None）。
    行含 date/temperature 及全部原始分量；temperature=None 表示预热期。"""
    data = build()
    series = data["series"]
    lo, hi, ans = 0, len(series) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid]["date"] <= as_of:
            ans = series[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def quantile_thresholds_from(series, qs=(0.2, 0.3, 0.4, 0.5)):
    """历史温度分位阈值（全历史；供网格与默认配置）。"""
    temps = sorted(s["temperature"] for s in series if s.get("temperature") is not None)
    if not temps:
        return {}
    out = {}
    n = len(temps)
    for q in qs:
        idx = min(n - 1, max(0, int(round(q * (n - 1)))))
        out[str(q)] = temps[idx]
    return out


def gate_snapshot(as_of=None):
    """调试/报告辅助：as_of 当日温度与原始分量快照"""
    return eco_temperature(as_of) if as_of else None


def recent_temperatures(days=20, as_of=None):
    """★ K4 只读辅助：最近 N 个完整交易日温度序列 [{date, temperature}, ...]
    （PIT：只取 as_of 当日及以前；过滤半成品日/预热期 None 行）。
    供 server.py 只读展示分支 /api/sentiment/eco 调用。"""
    try:
        data = build()
    except Exception:
        return []
    series = data.get("series") or []
    out = []
    for s in reversed(series):
        d = s.get("date") or ""
        if as_of and d > as_of:
            continue
        t = s.get("temperature")
        if t is None:
            continue          # 半成品日（当日样本不足）/预热期
        out.append({"date": d, "temperature": round(t, 2)})
        if len(out) >= days:
            break
    out.reverse()
    return out
