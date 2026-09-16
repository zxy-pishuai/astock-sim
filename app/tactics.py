# -*- coding: utf-8 -*-
"""🎯 战法选股：首板回调 + 连板梯队（纯展示状态机，零下单路径）

判据同源铁律（防第二遍理解失真）——公式/行号抄录如下，改判据须回改工具：
  首板战法（ShoubanYangTactic）：
    - is_zt/band/chg：tmp/w1/prep_data.py L24-30
      band = 30/68→0.194, 其他→0.097；chg = close.pct_change()（票内相邻）
      is_exdiv = |chg| > band+0.02；is_zt = (chg>=band-1e-9) & ~is_exdiv & ~exdiv_fwd & ~exdiv_bwd
    - first_7 / brk_20 / vol_burst_OR / trend_ok：tmp/w1/events.py L16-30
      first_N = is_zt & (shift(1).rolling(N,min_periods=N).sum()==0)
      brk_20 = close > hh_20（hh_20 = high.shift(1).rolling(20).max()）
      vol_burst_A = vol>=2*vol_prev；vol_burst_B = vol>=1.5*vol_prev3_mean；OR=A|B
      trend_ok = ma5_vol > ma60_vol
    - E2_fb_open_vol_shrink 买点 + M 混合卖点：tools/tactic_backtest.py
      compute_paths L130-143（支撑=fb_open±2% & close>=start_price & vol<0.5*fb_vol & 不涨停→收盘买）
      _sell_path L207-213（yang=9%：未封涨停→当日收盘卖；封涨停→次日开盘卖）+ L227（14日强卖）
  连板战法（LianbanA1Tactic）：
    - is_zt/lbc/board_type/amount_ratio：tmp/v1/build_events.py L13-49
      is_zt = chg>=thr（60/00→0.097,30/68→0.194）；|chg|>band(0.117/0.214)→t-1,t,t+1 非 zt；
      seq<5 新股豁免；lbc=连续涨停计数；board_type 一字/T字/炸板/none；amount_ratio=amount/前日amount
    - A1 候选（首板缩量非一字）：tools/tactic_lianban_backtest.py §3 L176-181
      条件 = lbc==1 & board_type!='一字' & amount_ratio<1（缩量），收盘封住视为成交，卖次日开盘

规则卡（运行时读取，缺键=报错，不许硬编码）：
  data/bt_tactic_shouban.json → yang_rule.recommended（注意：另有 recommended_rule 是 E3 对照，别拿错）
  data/bt_tactic_lianban.json → recommended_rule

生产 data/market.db 只读（URI 模式）；不 import 任何下单路径（trader/portfolio/execution）。
"""
import json
import os
import sqlite3
import time

import numpy as np

_ROLE = "showcase"  # 纯展示：不 import 下单路径

# ---------------------------------------------------------------- 数据/规则加载
def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_shouban_rule():
    """读 data/bt_tactic_shouban.json → yang_rule.recommended（缺键=报错）"""
    d = _load_json(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "bt_tactic_shouban.json"))
    rule = d["yang_rule"]["recommended"]
    # 逐键校验（不许静默默认）
    rule["pool"]["pre_no_zt_days"]
    rule["first_board"]["limit_main"]
    rule["first_board"]["limit_gem"]
    rule["first_board"]["exdiv_bw_pp"]
    rule["gates"]["trend_ma_vol"]
    rule["gates"]["breakout_days"]
    rule["gates"]["volume_burst"]
    rule["entry"]["pullback_window_days"]
    rule["entry"]["buy_at"]
    rule["entry"]["support_touch_pp"]
    rule["entry"]["vol_shrink_ratio"]
    rule["target"]["yang_pct"]
    rule["target"]["sell_at"]
    rule["fallback_exit"]["max_hold_days"]
    return rule


def _load_lianban_rule():
    """读 data/bt_tactic_lianban.json → recommended_rule（缺键=报错）"""
    d = _load_json(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "bt_tactic_lianban.json"))
    rule = d["recommended_rule"]
    rule["board"]["limit_main"]
    rule["board"]["limit_gem"]
    rule["board"]["new_list_exemption_days"]
    rule["entry"]["style"]
    rule["entry"]["filters"]
    rule["exit"]["sell_at"]
    return rule


# ---------------------------------------------------------------- 股票名称（T2 新需求1）
# 名称源与引擎同源：data/stock_list.json（[code,name,price] 三元组，5014 条）。
# 模块级懒加载 + mtime 缓存（文件变更才重载）；查不到填 ""（不许猜）。
_NAME_MAP = {}
_NAME_MAP_MTIME = None


def _load_name_map():
    """懒加载 code→name 映射（mtime 缓存）。返回 dict（可能为空）。"""
    global _NAME_MAP, _NAME_MAP_MTIME
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "stock_list.json")
    try:
        mt = os.path.getmtime(path)
    except Exception:
        return _NAME_MAP
    if _NAME_MAP_MTIME == mt:
        return _NAME_MAP
    m = {}
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        for row in rows:
            if isinstance(row, (list, tuple)) and len(row) >= 2 and row[0] is not None:
                m[str(row[0])] = str(row[1] or "")
    except Exception:
        pass
    _NAME_MAP, _NAME_MAP_MTIME = m, mt
    return m


def _stock_name(code):
    """code → 名称；查不到返回 ""（不猜）。"""
    return _load_name_map().get(str(code), "")


def _load_jingjia_rule():
    """读 data/bt_tactic_jingjia.json（竞价双战法阈值卡，缺键=报错）"""
    d = _load_json(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "bt_tactic_jingjia.json"))
    dban = d["jingjia_daban"]
    dban["score_threshold"]
    dban["min_pct"]
    dban["max_pct"]
    dban["min_amount"]
    dban["top_n"]
    dw = d["jingjia_w2s"]
    dw["gap_min_pp"]
    dw["gap_max_pp"]
    dw["candidate_enabled"]
    return d


# ---------------------------------------------------------------- 向量化判据
def _is_gem(code):
    return str(code)[:2] in ("30", "68")


def _vectorize(klines, limit_main=0.097, limit_gem=0.194, exdiv_bw_pp=2.0):
    """首板战法判据列（与 tmp/w1/prep_data.py + events.py 同公式）。klines 按日期升序。

    硬编码下传（T1R）：band=limit_main/limit_gem（主板/创业板涨停阈值）、
    is_exdiv 的 +exdiv_bw_pp/100 均由调用方从规则卡传入；默认值仅供验收脚本
    tmp/t1v2.py 无参直调与向后兼容，生产路径必由类传参。
    """
    n = len(klines)
    out = {
        "code": [k.get("code", "") for k in klines],
        "date": [k["date"] for k in klines],
        "open": np.array([float(k["open"]) for k in klines], dtype=np.float64),
        "high": np.array([float(k["high"]) for k in klines], dtype=np.float64),
        "low": np.array([float(k["low"]) for k in klines], dtype=np.float64),
        "close": np.array([float(k["close"]) for k in klines], dtype=np.float64),
        "volume": np.array([float(k["volume"]) for k in klines], dtype=np.float64),
    }
    band = np.array([limit_gem if _is_gem(k.get("code", "")) else limit_main for k in klines],
                    dtype=np.float64)
    out["band"] = band
    chg = np.full(n, np.nan)
    for i in range(1, n):
        if out["close"][i - 1] > 0:
            chg[i] = out["close"][i] / out["close"][i - 1] - 1.0
    out["chg"] = chg
    is_exdiv = np.abs(chg) > band + exdiv_bw_pp / 100.0
    exdiv_fwd = np.r_[is_exdiv[1:], False]
    exdiv_bwd = np.r_[False, is_exdiv[:-1]]
    out["is_zt"] = (chg >= band - 1e-9) & ~is_exdiv & ~exdiv_fwd & ~exdiv_bwd
    # ma5_vol / ma60_vol（rolling 满窗）
    ma5 = np.full(n, np.nan)
    ma60 = np.full(n, np.nan)
    for i in range(n):
        if i >= 4:
            ma5[i] = out["volume"][i - 4:i + 1].mean()
        if i >= 59:
            ma60[i] = out["volume"][i - 59:i + 1].mean()
    out["trend_ok"] = ma5 > ma60
    # hh_20（前一日为止 20 日最高）→ brk_20
    hh20 = np.full(n, np.nan)
    for i in range(n):
        if i >= 20:
            hh20[i] = out["high"][i - 20:i].max()
    out["brk_20"] = out["close"] > hh20
    # vol_burst
    vbA = np.zeros(n, bool)
    vbB = np.zeros(n, bool)
    for i in range(n):
        if i >= 1 and out["volume"][i - 1] > 0:
            vbA[i] = out["volume"][i] >= 2.0 * out["volume"][i - 1]
        if i >= 3:
            m3 = out["volume"][i - 3:i].mean()
            if m3 > 0:
                vbB[i] = out["volume"][i] >= 1.5 * m3
    out["vol_burst_OR"] = vbA | vbB
    # first_7（前 7 交易日无涨停）
    first7 = np.zeros(n, bool)
    for i in range(n):
        if i >= 7 and out["is_zt"][i]:
            first7[i] = (out["is_zt"][i - 7:i].sum() == 0)
    out["first_7"] = first7
    out["is_first_event"] = out["first_7"] & out["trend_ok"] & out["brk_20"] & out["vol_burst_OR"]
    return out


def _vectorize_both(klines, limit_main=0.097, limit_gem=0.194, exdiv_bw_pp=2.0):
    """R2-P0.1C：一次向量化同时产出首板(_vectorize)与连板(_vectorize_lianban)两卡列。
    公共列（code/date/OHLC/vol/amount/chg/prev_close/band/is10/exband/is_exdiv）只算一次，
    两套 is_zt 判据（首板 chg>=band-1e-9；连板 chg>=band 且 seq<5 新股豁免）与专属列
    各自独立计算——与 _vectorize / _vectorize_lianban 逐位一致（tmp/r2_verify.py 回归断言）。
    返回 {"sb": {首板列}, "lb": {连板列}}。"""
    n = len(klines)
    code = [k.get("code", "") for k in klines]
    date = [k["date"] for k in klines]
    open_ = np.array([float(k["open"]) for k in klines], dtype=np.float64)
    high_ = np.array([float(k["high"]) for k in klines], dtype=np.float64)
    low_ = np.array([float(k["low"]) for k in klines], dtype=np.float64)
    close_ = np.array([float(k["close"]) for k in klines], dtype=np.float64)
    volume_ = np.array([float(k["volume"]) for k in klines], dtype=np.float64)
    amount_ = np.array([float(k.get("amount", 0) or 0) for k in klines], dtype=np.float64)
    is10 = np.array([str(c)[:2] in ("60", "00") for c in code])
    band = np.where(is10, limit_main, limit_gem)
    exband = np.where(is10, limit_main + exdiv_bw_pp / 100.0, limit_gem + exdiv_bw_pp / 100.0)
    chg = np.full(n, np.nan)
    prev_close = np.full(n, np.nan)
    for i in range(1, n):
        if close_[i - 1] > 0:
            prev_close[i] = close_[i - 1]
            chg[i] = close_[i] / close_[i - 1] - 1.0
    is_exdiv = np.abs(chg) > exband
    exdiv_fwd = np.r_[is_exdiv[1:], False]
    exdiv_bwd = np.r_[False, is_exdiv[:-1]]
    # ---- 两套 is_zt（判据逐位复刻：首板 chg>=band-1e-9；连板 chg>=band + seq<5 豁免）----
    sb_zt = (chg >= band - 1e-9) & ~is_exdiv & ~exdiv_fwd & ~exdiv_bwd
    lb_zt = chg >= band
    lb_zt = lb_zt & ~is_exdiv & ~exdiv_fwd & ~exdiv_bwd
    seq = np.arange(n)
    lb_zt = lb_zt & ~(seq < 5)
    # ---- R2-P0.1C 核心：滑动窗口全向量化（cumsum / sliding_window_view），
    #      替代逐 i 切片 mean/max（原 96 万次 numpy mean 调用，见 tmp/r2_verify.py profile）。
    #      与 _vectorize / _vectorize_lianban 逐位一致（回归断言验证）。----
    csum = np.concatenate([[0.0], np.cumsum(volume_)])
    ma5 = np.full(n, np.nan)
    ma60 = np.full(n, np.nan)
    if n >= 5:
        ma5[4:] = (csum[5:] - csum[:n - 4]) / 5.0
    if n >= 60:
        ma60[59:] = (csum[60:] - csum[:n - 59]) / 60.0
    hh20 = np.full(n, np.nan)
    if n >= 21:
        _w = np.lib.stride_tricks.sliding_window_view(high_, 20)[:-1]   # 第 i-20 行 = high[i-20:i]
        hh20[20:] = _w.max(axis=1)
    vbA = np.zeros(n, bool)
    if n >= 2:
        vbA[1:] = volume_[1:] >= 2.0 * volume_[:-1]
    vbB = np.zeros(n, bool)
    if n >= 4:
        m3 = (csum[3:n] - csum[:n - 3]) / 3.0      # i>=3: mean(volume[i-3:i])
        vbB[3:] = volume_[3:] >= 1.5 * m3
    first7 = np.zeros(n, bool)
    if n >= 8:
        _cszt = np.concatenate([[0], np.cumsum(sb_zt.astype(np.int64))])
        first7[7:] = ((_cszt[7:n] - _cszt[:n - 7]) == 0) & sb_zt[7:]
    lbc = np.zeros(n, int)
    run = 0
    for i in range(n):
        if lb_zt[i]:
            run += 1
            lbc[i] = run
        else:
            run = 0
    ar = np.full(n, np.nan)
    for i in range(1, n):
        if amount_[i - 1] > 0 and amount_[i] > 0:
            ar[i] = amount_[i] / amount_[i - 1]
    bt = np.array(["none"] * n)
    for i in range(n):
        if not lb_zt[i]:
            continue
        if low_[i] == high_[i]:
            bt[i] = "一字"
        elif high_[i] == close_[i]:
            bt[i] = "T字回封"
    trend_ok = ma5 > ma60
    brk_20 = close_ > hh20
    vol_burst_OR = vbA | vbB
    is_first_event = first7 & trend_ok & brk_20 & vol_burst_OR
    sb = {
        "code": code, "date": date, "open": open_, "high": high_, "low": low_,
        "close": close_, "volume": volume_, "band": band, "chg": chg,
        "is_zt": sb_zt, "trend_ok": trend_ok, "brk_20": brk_20,
        "vol_burst_OR": vol_burst_OR, "first_7": first7, "is_first_event": is_first_event,
    }
    lb = {
        "code": code, "date": date, "open": open_, "high": high_, "low": low_,
        "close": close_, "volume": volume_, "amount": amount_, "chg": chg,
        "prev_close": prev_close, "seq": seq, "is_zt": lb_zt, "lbc": lbc,
        "amount_ratio": ar, "board_type": bt,
    }
    return {"sb": sb, "lb": lb}


def _vectorize_lianban(klines, limit_main=0.097, limit_gem=0.194, exdiv_bw_pp=2.0):
    """连板战法判据列（与 tmp/v1/build_events.py L13-49 同公式）。klines 按日期升序。

    硬编码下传（T1R）：thr=limit_main/limit_gem、exband=limit+exdiv_bw_pp/100
    （0.117=0.097+0.02 / 0.214=0.194+0.02）由调用方从规则卡传入；默认值仅供验收脚本
    tmp/t1v2.py 无参直调与向后兼容，生产路径必由类传参。
    板型（board_type）仅对涨停日（is_zt）定义——非涨停日一律 "none"。
    """
    n = len(klines)
    out = {
        "code": [k.get("code", "") for k in klines],
        "date": [k["date"] for k in klines],
        "open": np.array([float(k["open"]) for k in klines], dtype=np.float64),
        "high": np.array([float(k["high"]) for k in klines], dtype=np.float64),
        "low": np.array([float(k["low"]) for k in klines], dtype=np.float64),
        "close": np.array([float(k["close"]) for k in klines], dtype=np.float64),
        "volume": np.array([float(k["volume"]) for k in klines], dtype=np.float64),
        "amount": np.array([float(k.get("amount", 0) or 0) for k in klines], dtype=np.float64),
    }
    n2 = len(klines)
    is10 = np.array([str(k.get("code", ""))[:2] in ("60", "00") for k in klines])
    thr = np.where(is10, limit_main, limit_gem)
    exband = np.where(is10, limit_main + exdiv_bw_pp / 100.0, limit_gem + exdiv_bw_pp / 100.0)
    chg = np.full(n2, np.nan)
    prev_close = np.full(n2, np.nan)
    for i in range(1, n2):
        if out["close"][i - 1] > 0:
            prev_close[i] = out["close"][i - 1]
            chg[i] = out["close"][i] / out["close"][i - 1] - 1.0
    out["chg"] = chg
    out["prev_close"] = prev_close
    is_zt = chg >= thr
    exdiv = (np.abs(chg) > exband) & ~np.isnan(chg)
    is_zt = is_zt & ~exdiv
    is_zt = is_zt & ~np.r_[exdiv[1:], False]
    is_zt = is_zt & ~np.r_[False, exdiv[:-1]]
    # 新股豁免 seq<5
    seq = np.arange(n2)
    is_zt = is_zt & ~(seq < 5)
    out["seq"] = seq
    # lbc 连续涨停计数
    lbc = np.zeros(n2, int)
    run = 0
    for i in range(n2):
        if is_zt[i]:
            run += 1
            lbc[i] = run
        else:
            run = 0
    out["is_zt"] = is_zt
    out["lbc"] = lbc
    # amount_ratio = amount / 前日 amount
    ar = np.full(n2, np.nan)
    for i in range(1, n2):
        if out["amount"][i - 1] > 0 and out["amount"][i] > 0:
            ar[i] = out["amount"][i] / out["amount"][i - 1]
    out["amount_ratio"] = ar
    # board_type（仅涨停日有效；is_zt 前置使"炸板未封"分支不可达，已删除——T1R）
    zt_hi = np.where(is10, prev_close * 1.10, prev_close * 1.20)
    bt = np.array(["none"] * n2)
    for i in range(n2):
        if not is_zt[i]:
            continue
        if out["low"][i] == out["high"][i]:
            bt[i] = "一字"
        elif out["high"][i] == out["close"][i]:
            bt[i] = "T字回封"
        else:
            bt[i] = "none"
    out["board_type"] = bt
    return out


def _find_idx_le(dates, date):
    """返回 dates 中 <= date 的最大下标（date 未到 → None）"""
    idx = None
    for i, d in enumerate(dates):
        if d <= date:
            idx = i
        else:
            break
    return idx


# ================================================================ 首板回调战法
class ShoubanYangTactic(object):
    """首板→回调→≥9%大阳/涨停兑现（yang_rule.recommended，纯展示）"""

    NAME = "首板回调"
    RULE_SOURCE = "data/bt_tactic_shouban.json → yang_rule.recommended"

    def __init__(self, rule=None):
        rule = rule or _load_shouban_rule()
        self.rule = rule
        self.pre_no_zt = rule["pool"]["pre_no_zt_days"]          # 7
        self.limit_main = rule["first_board"]["limit_main"]      # 0.097
        self.limit_gem = rule["first_board"]["limit_gem"]        # 0.194
        self.exdiv_bw_pp = rule["first_board"]["exdiv_bw_pp"]    # 2.0
        self.trend_ma_vol = rule["gates"]["trend_ma_vol"]        # True
        self.breakout_days = rule["gates"]["breakout_days"]      # 20
        self.volume_burst = rule["gates"]["volume_burst"]        # "OR"
        self.pullback = rule["entry"]["pullback_window_days"]    # 10
        self.buy_at = rule["entry"]["buy_at"]                    # "close"
        self.yang_pct = rule["target"]["yang_pct"] / 100.0       # 0.09
        self.max_hold = rule["fallback_exit"]["max_hold_days"]   # 14
        # 硬编码下传（T1R）：触板/缩量阈值从规则卡读，不再散落代码
        self.touch_pct = rule["entry"]["support_touch_pp"] / 100.0   # 2.0 → 0.02
        self.vol_shrink = rule["entry"]["vol_shrink_ratio"]          # 0.5
        # 构造断言：新键与既有文本字段一致性（缺键=报错，禁止静默默认）
        if abs(self.touch_pct - 0.02) > 1e-9:
            raise ValueError("首板规则 entry.support_touch_pp 异常: %r" % rule["entry"]["support_touch_pp"])
        if abs(self.vol_shrink - 0.5) > 1e-9:
            raise ValueError("首板规则 entry.vol_shrink_ratio 异常: %r" % rule["entry"]["vol_shrink_ratio"])
        if not (0 < self.touch_pct < 0.1 and 0 < self.vol_shrink <= 1):
            raise ValueError("首板规则 entry 阈值越界: touch_pct=%r vol_shrink=%r"
                             % (self.touch_pct, self.vol_shrink))
        # 防拿错卡：板块必须用 E2_fb_open_vol_shrink + M 卖点（E3 对照在 recommended_rule）
        if rule["entry"].get("buy_kind") != "E2_fb_open_vol_shrink":
            raise ValueError("首板规则 entry.buy_kind 非 E2_fb_open_vol_shrink: %r"
                             % rule["entry"].get("buy_kind"))
        if "M" not in str(rule["target"].get("sell_at", "")):
            raise ValueError("首板规则 target.sell_at 非 M 混合卖点: %r" % rule["target"].get("sell_at"))

    def status(self, code, date, klines):
        """查询日状态。klines: 该票按日期升序的日K dict 列表。返回 {stage,detail,signals,updated}"""
        if not klines:
            return self._out("不在池", "无K线数据", [])
        v = _vectorize_both(klines, self.limit_main, self.limit_gem, self.exdiv_bw_pp)["sb"]
        idx = _find_idx_le(v["date"], date)
        if idx is None:
            return self._out("不在池", "查询日无历史K线", [])
        ev = self._covering_event(v, idx)
        if ev is None:
            return self._out("不在池", "近 90 日无进行中的首板事件（首板三关/回调/持有窗口已结束）", [])
        return self._event_stage(v, ev, idx)

    # ---- 内部 ----
    def _covering_event(self, v, idx):
        """找 idx 前最近、且其事件窗口覆盖 idx 的首板事件。返回 {t, buy} 或 None"""
        n = len(v["date"])
        for t in range(idx, -1, -1):
            if not bool(v["is_first_event"][t]):
                continue
            buy = self._find_buy(v, t)
            win_end = (buy + self.max_hold) if buy is not None else (t + self.pullback)
            if idx <= min(win_end, n - 1):
                return {"t": t, "buy": buy}
            return None  # 最近的事件已结束，更早的更不覆盖
        return None

    def _find_buy(self, v, t):
        """回调窗口内第一个 E2_fb_open_vol_shrink 触发日（与 compute_paths L130-143 一致）"""
        n = len(v["date"])
        fb_open = v["open"][t]
        fb_vol = v["volume"][t]
        band_t = v["band"][t]
        start_price = v["close"][t - 1] if t > 0 else fb_open
        for b in range(t + 1, min(t + 1 + self.pullback, n)):
            if abs(v["low"][b] - fb_open) / fb_open <= self.touch_pct \
                    and v["close"][b] >= start_price:
                if not (v["volume"][b] < self.vol_shrink * fb_vol):
                    continue
                if v["chg"][b] >= band_t - 1e-9:
                    continue  # 当日封涨停买不进 → 跳过该日继续找
                return b
        return None

    def _event_stage(self, v, ev, idx):
        t = ev["t"]
        buy = ev["buy"]
        n = len(v["date"])
        fb_open = v["open"][t]
        fb_vol = v["volume"][t]
        band_t = v["band"][t]
        if idx == t:
            return self._out("首板确认",
                             "首板确认（前%d日无涨停+趋势+创%d日新高+爆量）" % (self.pre_no_zt, self.breakout_days),
                             [{"name": "首板日", "value": v["date"][t]}], score=30.0)
        if buy is None:
            if t < idx <= min(t + self.pullback, n - 1):
                k = idx - t
                # 回调日打分：k 越小分越高 + 量越接近 0.5×首板量分越高
                vol_r = v["volume"][idx] / fb_vol if fb_vol > 0 else 99
                shrink_pts = max(0.0, 10.0 - max(0.0, vol_r - 0.5) * 20.0)
                sc = 40.0 + max(0.0, 10.0 - k) + shrink_pts
                return self._out("回调第k日",
                                 "首板后第%d个交易日，等待回踩首板开盘±2%%且量缩至<50%%首板量（未触发）" % k,
                                 [{"name": "回调", "value": "k=%d" % k},
                                  {"name": "首板开盘", "value": "%.2f" % fb_open},
                                  {"name": "量比", "value": "%.2f" % vol_r}], score=sc)
            return self._out("不在池", "回调窗口（%d日）结束未触发买点" % self.pullback, [], score=0.0)
        # 有买点
        if idx == buy:
            vol_r = v["volume"][buy] / fb_vol if fb_vol > 0 else 99
            sc = 85.0 + max(0.0, 15.0 * (1.0 - min(vol_r, 0.5) / 0.5))
            return self._out("买点触发(收盘买)",
                             "回踩首板开盘±2%%+量缩<50%%首板量，当日收盘买（未封涨停）",
                             [{"name": "买点日", "value": v["date"][buy]},
                              {"name": "收盘价", "value": "%.2f" % v["close"][buy]},
                              {"name": "量比", "value": "%.2f" % vol_r}], score=sc)
        if idx < buy:
            k = idx - t
            return self._out("回调第k日",
                             "首板后第%d个交易日，买点未到" % k,
                             [{"name": "回调", "value": "k=%d" % k}], score=40.0 + max(0.0, 10.0 - k))
        # 持有期（idx > buy）
        hold_days = idx - buy + 1
        yj = None
        for j in range(buy, min(idx + 1, n)):
            if v["chg"][j] >= self.yang_pct:
                yj = j
                break
        if yj is not None and yj == idx:
            if v["is_zt"][yj]:
                # 封涨停 → 次日开盘卖；当日即数据末位则"待次日开盘"（T1R：不得落"未板落袋"文案）
                if yj + 1 < n:
                    return self._out("兑现-封板格局(次日开盘卖)",
                                     "第%d日 chg=%.2f%% 封涨停 → 次日开盘卖（吃隔夜溢价）" % (hold_days, v["chg"][yj] * 100),
                                     [{"name": "大阳日", "value": v["date"][yj]},
                                      {"name": "次日开盘卖", "value": v["date"][yj + 1]}], score=30.0)
                return self._out("兑现-封板格局(次日开盘卖)",
                                 "第%d日 chg=%.2f%% 封涨停，但已到最后交易日，待次日开盘卖" % (hold_days, v["chg"][yj] * 100),
                                 [{"name": "大阳日", "value": v["date"][yj]},
                                  {"name": "次日开盘卖", "value": "待次日开盘"}], score=30.0)
            return self._out("兑现-未板落袋",
                             "第%d日 chg=%.2f%% ≥9%% 未封涨停 → 当日收盘落袋" % (hold_days, v["chg"][yj] * 100),
                             [{"name": "兑现日", "value": v["date"][yj]}], score=25.0)
        if yj is not None and yj < idx:
            # 已兑现（最终态展示）
            if v["is_zt"][yj] and yj + 1 < n:
                return self._out("兑现-封板格局(次日开盘卖)",
                                 "已于 %s 封涨停，%s 次日开盘卖出" % (v["date"][yj], v["date"][yj + 1]),
                                 [{"name": "卖出日", "value": v["date"][yj + 1]}], score=30.0)
            return self._out("兑现-未板落袋",
                             "已于 %s 未封涨停当日收盘卖出" % v["date"][yj],
                             [{"name": "卖出日", "value": v["date"][yj]}], score=25.0)
        if hold_days >= self.max_hold:
            return self._out("失效(14日)",
                             "持有 %d 日无 ≥%.0f%% 大阳，收盘强卖（不止损）" % (hold_days, self.yang_pct * 100),
                             [{"name": "失效日", "value": v["date"][idx]}], score=10.0)
        sc = 70.0 + max(0.0, (self.max_hold - hold_days) * 1.5)
        return self._out("持有中(等≥9%大阳,第j日)",
                         "已买入第 %d 个持有日，等 ≥%.0f%% 大阳（M混合卖点）" % (hold_days, self.yang_pct * 100),
                         [{"name": "持有", "value": "第%d日" % hold_days},
                          {"name": "目标", "value": "≥%.0f%%" % (self.yang_pct * 100)}], score=sc)

    def _out(self, stage, detail, signals, score=0.0):
        return {"stage": stage, "detail": detail, "signals": signals,
                "score": round(float(score), 1), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}


# ================================================================ 连板梯队战法
class LianbanA1Tactic(object):
    """连板梯队（A1 打板：首板缩量非一字，收盘买→次日开盘卖，纯隔夜）"""

    NAME = "连板梯队"
    RULE_SOURCE = "data/bt_tactic_lianban.json → recommended_rule"

    def __init__(self, rule=None):
        rule = rule or _load_lianban_rule()
        self.rule = rule
        board = rule["board"]
        self.limit_main = board["limit_main"]                  # 0.097
        self.limit_gem = board["limit_gem"]                    # 0.194
        self.new_exempt = board["new_list_exemption_days"]     # 5
        # exband = limit + exdiv_bw_pp/100（0.117=0.097+0.02 / 0.214=0.194+0.02）。
        # 连板卡 board 段无该键 → .get 默认 2.0（与首板卡同口径，T1R 已声明默认项）
        self.exdiv_bw_pp = float(board.get("exdiv_bw_pp", 2.0))
        filters = rule["entry"].get("filters") or {}
        self.yizi_exclude = bool(rule["entry"].get("yizi_exclude", True))
        self.amount_ratio_lt = 1.0  # "amount_ratio<1(缩量板)"
        self.sealed_at_close = bool(filters.get("sealed_at_close", True))
        self.sell_at = rule["exit"]["sell_at"]                 # "next_open"
        if rule["entry"].get("style") != "A1":
            raise ValueError("连板规则 entry.style 非 A1: %r" % rule["entry"].get("style"))

    @staticmethod
    def _ladder_of(lbc, is_break=False):
        """梯队子板块。T2：五板及以上合并"五板+"一档（高度板仅观察）。"""
        if is_break:
            return "断板"
        if lbc == 1: return "一进二"
        if lbc == 2: return "二进三"
        if lbc == 3: return "三进四"
        if lbc == 4: return "四进五"
        return "五板+"   # lbc>=5 合并一档

    # T2（新需求2）：lbc>=2 观察档的本地晋级率参考（V1 回测，仅背景信息，不构成候选）
    PROMO_REF = {2: ("二进三", 33.02), 3: ("三进四", 46.43), 4: ("四进五", 53.77)}

    def _lb_score(self, lbc, board_type, amount_ratio, is_a1=False, is_break=False):
        if is_break:
            return 20.0
        base = min(lbc * 12.0, 60.0)
        bt_pts = {"一字": 20.0, "T字回封": 12.0, "炸板未封": -15.0}.get(board_type, 6.0)
        if amount_ratio is None or np.isnan(amount_ratio):
            ar_pts = 0.0
        elif amount_ratio < 0.7:
            ar_pts = 18.0
        elif amount_ratio < 1.0:
            ar_pts = 12.0
        elif amount_ratio < 1.5:
            ar_pts = 5.0
        else:
            ar_pts = -8.0
        a1_pts = 15.0 if is_a1 else 0.0
        sc = base + bt_pts + ar_pts + a1_pts
        if board_type == "一字":
            sc = min(sc, 60.0)  # 一字买不进，不给太高
        return max(0.0, min(100.0, sc))

    def _observe_stage(self, lbc, today_bt, today_ar, gem_note=False):
        """lbc>=2 的高度板观察档：全梯队展示、带晋级率背景、score 压到 40 以下（灰系）。
        数据诚实：V1 规则卡只有 A1（首板缩量非一字隔夜）是正期望，二板及以上追买全网格负期望
        （本地 -0.55%~-1.22%，外部社区"次日追买=接盘 年化-24.5%"同向）——故只展示不给候选徽章。
        gem_note（E3）：20cm（创业板/科创板，30/68）未做拆分回测验证，仅观察不构成候选。"""
        name, promo = self.PROMO_REF.get(lbc, (None, None))
        if name:
            title = name
            promo_s = "；本地晋级率 %.2f%%（V1，仅参考）" % promo
        elif lbc == 1:
            title = "首板"
            promo_s = ""
        else:
            title = "五板+"
            promo_s = "（V1 高度板样本少，仅参考）"
        sc = min(self._lb_score(lbc, today_bt, today_ar), 39.5)   # ★ score 上限 40 以下 → 前端灰系
        gem = "；20cm 未回测验证，仅观察" if gem_note else ""
        detail = ("lbc=%d %s 仅观察（A1只做首板缩量隔夜）%s%s；无候选徽章——"
                  "次日追买非正期望，不构成候选" % (lbc, title, promo_s, gem))
        return {"stage": "%s(仅观察)" % title, "detail": detail,
                "signals": [{"name": "lbc", "value": str(lbc)},
                            {"name": "板型", "value": today_bt}],
                "score": round(sc, 1), "ladder": title,
                "lbc": lbc, "board_type": today_bt, "amount_ratio": today_ar,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S")}

    def status(self, code, date, klines):
        """查询日状态。klines: 该票按日期升序的日K dict 列表。
        返回 {stage,detail,signals,score,ladder,lbc,board_type,amount_ratio,updated}
        （lbc/board_type/amount_ratio 一并返回，供 scan 复用，避免第三次向量化——T1R）
        T2（新需求2）：今日涨停 lbc>=1 的票**全部进列表**，stage 按 ladder 细分；
        首板 tab 的 candidates 语义不变（回调/持有/兑现/失效全保留）。"""
        if not klines:
            return self._out("不在梯队", "无K线数据", [], score=0.0)
        # ★ E3（20cm 堵口）：10cm = 沪主板60/深主板00；30/68（创业板/科创板）及北交所
        #    8/4/920 为 20cm/30cm 板，未做拆分回测验证 → 不进 A1 候选，仅观察。
        is_10cm = str(code)[:2] in ("60", "00")
        v = _vectorize_both(klines, self.limit_main, self.limit_gem, self.exdiv_bw_pp)["lb"]
        idx = _find_idx_le(v["date"], date)
        if idx is None or idx < 1:
            return self._out("不在梯队", "无历史K线", [], score=0.0)
        # ★ T2 守卫：查询日必须有当日 bar，否则停牌/数据未更新到查询日的票会把历史涨停
        #    bar 误当"今日涨停"（生产库 per-code 最新日不一致，733 票停在 08-21 等）。
        #    今日无行情 = 不虚构今日涨停态，如实"不在梯队"。
        if v["date"][idx] != date:
            return self._out("不在梯队",
                             "今日无行情/数据未更新到查询日(%s)，不虚构今日涨停态" % date,
                             [], score=0.0)
        today_zt = bool(v["is_zt"][idx])
        today_lbc = int(v["lbc"][idx])
        today_bt = v["board_type"][idx]
        today_ar = float(v["amount_ratio"][idx]) if idx >= 1 and not np.isnan(v["amount_ratio"][idx]) else None

        def _enrich(r):
            r["ladder"] = ("断板" if r["stage"] == "断板(次日开盘已卖)"
                           else self._ladder_of(today_lbc))
            r["lbc"] = today_lbc
            r["board_type"] = today_bt
            r["amount_ratio"] = today_ar
            return r

        yest_candidate = (bool(v["is_zt"][idx - 1])
                          and int(v["lbc"][idx - 1]) == 1
                          and v["board_type"][idx - 1] != "一字"
                          and (v["amount_ratio"][idx - 1] < self.amount_ratio_lt)
                          and is_10cm)   # ★ E3：20cm 昨日不成候选（未拆分回测）
        if not today_zt:
            if yest_candidate:
                sc = self._lb_score(today_lbc, today_bt, today_ar, is_break=True)
                return _enrich(self._out("断板(次日开盘已卖)",
                                 "昨日候选首板收盘买入，今日开盘卖出但断板（未连板）",
                                 [{"name": "昨日", "value": v["date"][idx - 1]},
                                  {"name": "今日", "value": v["date"][idx]}], score=sc))
            return _enrich(self._out("不在梯队", "今日非涨停", [], score=0.0))
        # 今日涨停 lbc>=1 → 全部进列表（T2）
        if today_lbc == 1:
            if not is_10cm:
                # ★ E3：20cm 首板不进候选（未拆分回测验证）→ 观察档
                return _enrich(self._observe_stage(1, today_bt, today_ar, gem_note=True))
            if today_bt == "一字":
                sc = self._lb_score(1, "一字", today_ar)
                return _enrich(self._out("一进二不候选(放量或一字)",
                                 "首板一字板，排不进不参与（非 A1 候选）",
                                 [{"name": "lbc", "value": "1"},
                                  {"name": "板型", "value": "一字"}], score=sc))
            if today_ar is not None and today_ar < self.amount_ratio_lt:
                sc = self._lb_score(1, today_bt, today_ar, is_a1=True)
                return _enrich(self._out("一进二候选(首板缩量)",
                                 "首板缩量(当日amount<前日)+非一字+收盘封住 → 收盘买入，等次日开盘卖",
                                 [{"name": "lbc", "value": "1"},
                                  {"name": "量比", "value": "%.2f" % today_ar},
                                  {"name": "板型", "value": today_bt},
                                  {"name": "打法", "value": "已打板(隔夜持有)"}], score=sc))
            sc = self._lb_score(1, today_bt, today_ar)
            return _enrich(self._out("一进二不候选(放量或一字)",
                             "首板放量（量比≥1），非 A1 缩量候选",
                             [{"name": "lbc", "value": "1"},
                              {"name": "量比", "value": "%.2f" % (today_ar if today_ar is not None else -1)},
                              {"name": "板型", "value": today_bt}], score=sc))
        if yest_candidate:
            # 昨日候选买入 → 今日连板延续 → 开盘卖出兑现
            sc = self._lb_score(today_lbc, today_bt, today_ar)
            return _enrich(self._out("二板兑现",
                             "昨日候选首板收盘买入（隔夜持有），今日开盘卖出兑现（连板成功）",
                             [{"name": "lbc", "value": str(today_lbc)},
                              {"name": "板型", "value": today_bt}], score=sc))
        # lbc>=2 且非昨日候选 → 高度板观察档（T2：全展示、灰系、score≤40）
        return _enrich(self._observe_stage(today_lbc, today_bt, today_ar, gem_note=not is_10cm))

    def _out(self, stage, detail, signals, score=0.0):
        return {"stage": stage, "detail": detail, "signals": signals,
                "score": round(float(score), 1), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}


# ================================================================ 竞价双战法（T2 新需求3，公示参考，零下单）
def _score_auction_no_net(q, klines, sec_burst, breadth):
    """调 score_auction 但抑制内嵌 moneyflow（东财 datacenter 网络，红线禁出网）。
    运行时注入 app.moneyflow.seat_analysis=None → 龙虎榜席位加分不参与竞价展示；
    try/finally 恢复，不改 scoring.py。与任务 4a 输入字段清单（apct/vr/额/板块/宽度）一致。"""
    from . import moneyflow as _mf
    from . import scoring as _sc
    _orig = _mf.seat_analysis
    try:
        _mf.seat_analysis = lambda code, date=None: None
        return _sc.score_auction(q, klines, sec_burst, breadth)
    finally:
        _mf.seat_analysis = _orig


class JingjiaDabanTactic(object):
    """竞价打板（公示参考）。判据=app/scoring.py score_auction（L519-587，v2.0 移植）。
    喂参照 app/trader.py _auction_round()（L1035-1089）：open/昨收/量/额/板块共振/大盘宽度。
    纯展示，零下单路径。"""

    NAME = "竞价打板"
    RULE_SOURCE = "data/bt_tactic_jingjia.json → jingjia_daban"

    def __init__(self, rule=None):
        rule = rule or _load_jingjia_rule()
        self.rule = rule["jingjia_daban"]
        # N 从 config 既有常量读（AUCTION_SCORE_THRESHOLD=40）；无则按卡值兜底
        try:
            from . import config as C
            self.score_thr = float(getattr(C, "AUCTION_SCORE_THRESHOLD",
                                           self.rule["score_threshold"]))
        except Exception:
            self.score_thr = float(self.rule["score_threshold"])
        self.min_pct = float(self.rule["min_pct"])
        self.max_pct = float(self.rule["max_pct"])
        self.min_amount = float(self.rule["min_amount"])
        self.top_n = int(self.rule["top_n"])
        # 构造断言：config 阈值与卡一致（防漂移；config 有既有常量则必须等于卡值）
        if abs(self.score_thr - float(self.rule["score_threshold"])) > 1e-9:
            raise ValueError("竞价打板 score_threshold 与卡不一致: config=%r card=%r"
                             % (self.score_thr, self.rule["score_threshold"]))

    def stage_of(self, score, degraded):
        if degraded:
            return "竞价快照缺失(降级)"
        if score >= self.score_thr:
            return "竞价候选(≥%d分)" % int(self.score_thr)
        return "竞价观察"

    def status(self, code, quote, klines, sec_burst, breadth, degraded=False):
        """竞价评分（单票）。quote=实时行情 dict；klines=截至昨日日K（本地只读）。"""
        try:
            score, signals = _score_auction_no_net(quote, klines, sec_burst, breadth)
        except Exception as e:
            score, signals = 0, ["评分异常: %s" % e]
        apct = None
        yc = quote.get("yest_close", 0) or 0
        op = quote.get("open", 0) or quote.get("open_price", 0) or 0
        if yc > 0 and op > 0:
            apct = (op - yc) / yc * 100.0
        return {
            "code": str(code), "name": _stock_name(code),
            "stage": self.stage_of(score, degraded),
            "detail": ("；".join(signals[:6]) if signals else "无信号"),
            "signals": [{"name": "竞价涨幅", "value": "%.2f%%" % apct} if apct is not None
                        else {"name": "竞价涨幅", "value": "-"},
                        {"name": "评分", "value": str(round(score, 1))}],
            "score": round(float(score), 1),
            "apct": round(apct, 2) if apct is not None else None,
            "vr": round(float(quote.get("volume", 0) or 0), 4),
            "amount": quote.get("amount", 0) or 0,
            "price": quote.get("price", 0) or 0,
            "degraded": bool(degraded),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


class JingjiaRuozhuanqiangTactic(object):
    """竞价弱转强（公示参考）。判据=P69 口径（tools/board_w2s_filter_ab.py）：昨日涨停池
    （tactics 自有 is_zt 重建，不依赖 limit_pool）+ 今日竞价高开 gap∈[gap_min,gap_max]。
    研究结论：P34 独立策略 9 变体全不达标不成立、P69 board 前置过滤 pass=false（无增益）
    → 本实现只给"观察"灰系，不发候选徽章（数据诚实；candidate_enabled=false）。"""

    NAME = "竞价弱转强"
    RULE_SOURCE = "data/bt_tactic_jingjia.json → jingjia_w2s"

    def __init__(self, rule=None):
        rule = rule or _load_jingjia_rule()
        self.rule = rule["jingjia_w2s"]
        self.gap_min = float(self.rule["gap_min_pp"])
        self.gap_max = float(self.rule["gap_max_pp"])
        self.candidate_enabled = bool(self.rule.get("candidate_enabled", False))

    def status(self, code, yest_zt, today_open, yest_close):
        """昨日涨停池内单票：今日竞价高开 gap 判定。非昨日涨停返回 None（不进板）。"""
        if not yest_zt:
            return None
        gap = None
        if yest_close and today_open:
            gap = (today_open / yest_close - 1.0) * 100.0
        if gap is not None and self.gap_min <= gap <= self.gap_max:
            sc = 38.0   # 观察档灰系（≤40）
            stage = "弱转强观察"
            detail = ("昨日涨停 + 今日竞价高开 %.2f%% ∈ [+%g%%,+%g%%] 弱转强信号触发；"
                      "研究结论 P34 独立策略不成立 / P69 前置过滤无增益 → 仅观察，无候选徽章"
                      % (gap, self.gap_min, self.gap_max))
        else:
            sc = 15.0
            stage = "弱转强未触发"
            detail = ("昨日涨停但今日竞价高开 %s 未落 [+%g%%,+%g%%] 弱转强区间，信号未触发"
                      % ("%.2f%%" % gap if gap is not None else "N/A",
                         self.gap_min, self.gap_max))
        return {
            "code": str(code), "name": _stock_name(code), "stage": stage, "detail": detail,
            "signals": [{"name": "竞价涨幅", "value": "%.2f%%" % gap if gap is not None else "-"}],
            "score": sc, "gap_pp": round(gap, 2) if gap is not None else None,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


# ================================================================ 竞价快照时序（T2 4a/4b）
_AUCTION_WIN_START = 925   # 09:25
_AUCTION_WIN_END = 940     # 09:40


def _now_hm():
    """当前 HHMM 整数（测试可 monkeypatch）。"""
    return int(time.strftime("%H")) * 100 + int(time.strftime("%M"))


def _today_str():
    """当前自然日（测试可 monkeypatch）。"""
    return time.strftime("%Y-%m-%d")


def _auction_snapshot_path(d):
    return os.path.join(_CACHE_DIR, "auction_%s.json" % d)


def _yest_zt_set(by_code, yday):
    """昨日（yday）涨停池：用 _vectorize_lianban 重建 is_zt（与连板判据同源，不依赖 limit_pool）。"""
    out = set()
    for code, kl in by_code.items():
        if len(kl) < 8:
            continue
        if kl[-1]["date"] != yday:
            continue
        v = _vectorize_lianban(kl[-20:])
        if bool(v["is_zt"][-1]):
            out.add(code)
    return out


def _empty_auction_payload(d, state, note):
    """空态/未到窗口的竞价板块骨架（两卡 statuses 为空，前端可渲染）。"""
    m = _load_jingjia_rule()["meta"]["honest_note"]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "auction_date": d, "state": state, "note": note, "generated_at": now,
        "tactics": [
            {"name": JingjiaDabanTactic.NAME, "rule_source": JingjiaDabanTactic.RULE_SOURCE,
             "honest_note": m, "stages_summary": {}, "statuses": []},
            {"name": JingjiaRuozhuanqiangTactic.NAME,
             "rule_source": JingjiaRuozhuanqiangTactic.RULE_SOURCE,
             "honest_note": m, "stages_summary": {}, "statuses": []},
        ],
    }


def _auction_capture(d, db_path, degraded):
    """抓竞价数据（快照或降级）。degraded=True → 竞价量/额不可复得置 0，仅高开口径评分。
    报价源优先本地 DB（今日 bar 已存在 → 零网络）；否则行情 fetch（显式标注：竞价快照/降级
    路径，复用引擎既有源 datafeed，只读，零下单）。"""
    from . import config as C
    from . import trading_calendar as tcal
    from . import datafeed as df
    from . import scoring as _sc
    yday = tcal.prev_trading_day(d) or d
    rows_all = _load_recent_klines(db_path, d)          # 截至今日（含今日 bar，若已日更）
    by_all = _group_by_code(rows_all)
    # score_auction 的 klines 语义=昨日为末根 → 严格取 date<d
    by_code = {}
    for code, kl in by_all.items():
        by_code[code] = [k for k in kl if k["date"] < d]
    # 报价源：今日 bar 已在本地库 → 直接用 open/昨收（零网络降级）
    db_today = {code: kl[-1] for code, kl in by_all.items() if kl and kl[-1]["date"] == d}
    stocks = None
    if db_today:
        stocks = {}
        for code, bar in db_today.items():
            bl = by_code.get(code) or []
            yc = float(bl[-1]["close"] or 0) if bl else 0.0
            stocks[code] = {"code": code, "name": _stock_name(code),
                            "open": float(bar["open"] or 0), "yest_close": yc,
                            "volume": 0, "amount": 0, "price": float(bar["close"] or 0)}
    if not stocks:
        stocks = df.fetch_all_stocks()                  # 网络（窗口快照/盘中降级，显式标注）
    if not stocks:
        return _empty_auction_payload(d, "no_quotes", "行情源无数据")
    dban = JingjiaDabanTactic()
    w2s = JingjiaRuozhuanqiangTactic()
    cands = []
    for code, q in stocks.items():
        if not _sc.is_main_board(code):
            continue
        op = q.get("open", 0) or q.get("open_price", 0) or 0
        yc = q.get("yest_close", 0) or 0
        if op <= 0 or yc <= 0:
            continue
        apct = (op - yc) / yc * 100.0
        if apct < dban.min_pct or apct >= dban.max_pct:
            continue
        if not degraded and (q.get("amount", 0) or 0) < dban.min_amount:
            continue
        cands.append((code, q))
    sec_burst = {}
    for code, q in cands:
        sec = _sc.classify_sector(q.get("name", ""), code)
        sec_burst[sec] = sec_burst.get(sec, 0) + 1
    up = sum(1 for s in stocks.values()
             if (s.get("open", 0) or s.get("open_price", 0) or 0) > 0
             and (s.get("yest_close", 0) or 0) > 0
             and (s.get("open") or s.get("open_price", 0)) > s.get("yest_close", 0))
    tot = sum(1 for s in stocks.values()
              if (s.get("open", 0) or s.get("open_price", 0) or 0) > 0
              and (s.get("yest_close", 0) or 0) > 0)
    ab = up / tot if tot > 0 else 0.5
    yzt = _yest_zt_set(by_code, yday)                  # w2s 宇宙 = 昨日涨停池
    dstatus, wstatus = [], []
    for code, q in cands:
        kl = by_code.get(code) or []
        if len(kl) < 20:
            continue
        qq = dict(q)
        if degraded:
            qq["volume"] = 0                           # 竞价量当日不可复得 → 置 0（vr 不计）
            qq["amount"] = 0                           # 竞价额同理
        dstatus.append(dban.status(code, qq, kl, sec_burst, ab, degraded=degraded))
    for code in sorted(yzt):
        q = stocks.get(code)
        kl = by_code.get(code) or []
        if not q or len(kl) < 8:
            continue
        today_open = q.get("open", 0) or q.get("open_price", 0) or 0
        yc = q.get("yest_close", 0) or 0
        rec = w2s.status(code, True, today_open, yc)
        if rec:
            wstatus.append(rec)
    dstatus.sort(key=lambda x: x.get("score", 0), reverse=True)
    dstatus = dstatus[:dban.top_n]
    wstatus.sort(key=lambda x: x.get("score", 0), reverse=True)

    def _summary(entries):
        c = {}
        for e in entries:
            c[e["stage"]] = c.get(e["stage"], 0) + 1
        return c

    m = _load_jingjia_rule()["meta"]["honest_note"]
    return {
        "auction_date": d, "state": "degraded" if degraded else "snapshot",
        "note": ("09:25-09:40 竞价快照（完整评分字段）" if not degraded
                 else "09:40 后无快照：竞价量/额当日不可复得，仅高开口径降级评分"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tactics": [
            {"name": dban.NAME, "rule_source": dban.RULE_SOURCE,
             "honest_note": m, "stages_summary": _summary(dstatus), "statuses": dstatus},
            {"name": w2s.NAME, "rule_source": w2s.RULE_SOURCE,
             "honest_note": m, "stages_summary": _summary(wstatus), "statuses": wstatus},
        ],
    }


def auction_board(date=None, db_path=None):
    """竞价双战法板块（公示参考，纯展示）。
    快照纪律：09:25-09:40 首访抓竞价快照存 tmp/tactics_cache/auction_{date}.json（完整评分字段）；
    此后全天读快照；09:40 后无快照 → 降级评分（open/昨收重算 apct，"竞价量缺失"徽章）；
    周末/节假日空态。一律只读行情，零下单路径。"""
    from . import config as C
    from . import trading_calendar as tcal
    db_path = db_path or C.DB_FILE
    d = date or _today_str()
    if not tcal.is_trading_day(d):
        return _empty_auction_payload(d, "closed", "非交易日，竞价板块空态")
    snap = _auction_snapshot_path(d)
    if os.path.exists(snap):
        try:
            with open(snap, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass   # 半截/损坏 → 重抓
    hm = _now_hm()
    if date is None and _AUCTION_WIN_START <= hm <= _AUCTION_WIN_END:
        pl = _auction_capture(d, db_path, degraded=False)   # 窗口内首访 → 抓快照
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(snap, "w", encoding="utf-8") as f:
                json.dump(pl, f, ensure_ascii=False)
        except Exception:
            pass
        return pl
    if date is None and hm < _AUCTION_WIN_START:
        return _empty_auction_payload(d, "pre_window", "未到竞价时段（09:25 起）")
    # 09:40 后无快照（或显式指定 date 的测试）→ 降级；结果写回快照文件防重复拉行情
    pl = _auction_capture(d, db_path, degraded=True)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(snap, "w", encoding="utf-8") as f:
            json.dump(pl, f, ensure_ascii=False)
    except Exception:
        pass
    return pl


# ================================================================ 全市场扫描
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tmp", "tactics_cache")
_MIN_COVERAGE = 1500   # 最新交易日票数 <1500 → "数据不完整日"黄条 + 结果降级
_LOOKBACK_DAYS = 140   # 覆盖约 90 交易日的自然日回看


def _latest_date(db_path):
    con = sqlite3.connect("%s?mode=ro" % _as_uri(db_path), uri=True, timeout=30)
    try:
        row = con.execute("SELECT MAX(date) FROM kline WHERE period='day'").fetchone()
        return (row[0] or "")[:10]
    finally:
        con.close()


def _as_uri(db_path):
    """绝对路径 → file:// URI（中文路径安全，避免 raw 路径 URI 解析失败）"""
    from pathlib import Path
    return Path(db_path).as_uri()


def _load_recent_klines(db_path, date):
    """只读拉取近 _LOOKBACK_DAYS 日全市场日K（60/00/30/68 前缀）。返回 dict 列表"""
    import datetime as _dt
    d0 = _dt.datetime.strptime(date, "%Y-%m-%d") - _dt.timedelta(days=_LOOKBACK_DAYS)
    d0s = d0.strftime("%Y-%m-%d")
    con = sqlite3.connect("%s?mode=ro" % _as_uri(db_path), uri=True, timeout=60)
    try:
        rows = con.execute(
            "SELECT code,date,open,high,low,close,volume,amount FROM kline "
            "WHERE period='day' AND date>=? AND substr(code,1,2) IN ('60','00','30','68') "
            "ORDER BY code,date", (d0s,)).fetchall()
    finally:
        con.close()
    out = []
    for code, date_, o, h, lo, c, vol, amt in rows:
        out.append({"code": code, "date": date_, "open": o, "high": h, "low": lo,
                    "close": c, "volume": vol, "amount": amt})
    return out


def _group_by_code(rows):
    g = {}
    for k in rows:
        g.setdefault(k["code"], []).append(k)
    return g


def _prev_trading_cache(d, db_path=None):
    """R2-P0.1A：找 d 之前最近一个有磁盘缓存(>=_MIN_COVERAGE)的交易日缓存作 stale 兜底。
    只读；返回 dict 或 None。不校验当日指纹（旧日 n_codes 与今日不同属预期）。"""
    try:
        _dir = _CACHE_DIR
        if not os.path.isdir(_dir):
            return None
        files = []
        for fn in os.listdir(_dir):
            if fn.endswith(".json") and not fn.endswith(".tmp.json"):
                files.append(fn[:-5])
        files.sort()
        for pd in reversed(files):
            if pd < d:
                try:
                    with open(os.path.join(_dir, pd + ".json"), encoding="utf-8") as f:
                        cpl = json.load(f)
                    if int((cpl.get("coverage") or {}).get("n_codes", -1)) >= _MIN_COVERAGE:
                        return cpl
                except Exception:
                    continue
    except Exception:
        return None
    return None


# R2-P0.1A：当日后台重扫防并发（模块级，单进程多线程共享）
_RESCAN_LOCK = {}


# ★ J2（2026-09-13）：_count_codes_on 记忆缓存——该 SQL（COUNT DISTINCT 全库日K表）
#   实测 ~916ms 且在 GIL 上执行；get_tactics_board 每次请求都调（数据指纹），
#   web 层 300s SWR 后台刷新会撞上它 → 并发接口劣化 30 倍。
#   记忆窗口 600s（> web 层刷新间隔 300s）：刷新时指纹恒命中（~0ms），GIL 隔离达标。
#   数据完整性语义：收盘数据更新后 ≤10 分钟内指纹延迟刷新，且"旧值偏低"只会触发
#   保守重扫（宁多扫不误用），不会漏判。（仅缓存/调度部分，不涉及战法判据公式）
_COUNT_MEM = {"key": None, "ts": 0.0, "n": 0}


def _count_codes_on(db_path, d, _max_age=600.0):
    """只读：最新交易日 d 的票数（数据指纹，600s 记忆缓存）。"""
    key = (db_path, d)
    now = time.time()
    if _COUNT_MEM["key"] == key and now - _COUNT_MEM["ts"] < _max_age:
        return _COUNT_MEM["n"]
    con = sqlite3.connect("%s?mode=ro" % _as_uri(db_path), uri=True, timeout=30)
    try:
        row = con.execute(
            "SELECT COUNT(DISTINCT code) FROM kline WHERE period='day' AND date=? "
            "AND substr(code,1,2) IN ('60','00','30','68')", (d,)).fetchone()
        n = int(row[0] or 0)
    finally:
        con.close()
    _COUNT_MEM.update(key=key, ts=time.time(), n=n)
    return n


def get_tactics_board(date=None, db_path=None):
    """板块入口：首板回调 + 连板梯队（日期级缓存）+ 竞价双战法（独立快照时序）。
    T1R 缓存指纹：命中缓存前校验缓存内 coverage.n_codes 与当前库一致；
    n_codes < 1500（数据不完整日）不落缓存、不读当日缓存，并返回 "stale_ok": true。
    T2：缓存仅存首板/连板 base；竞价板每次实时求值（auction 有独立快照/窗口逻辑，
    不随 board 缓存冻结——避免"早于窗口的访问把空竞价缓存一整天"）。"""
    from . import config as C
    db_path = db_path or C.DB_FILE
    d = date or _latest_date(db_path)
    cur_n = _count_codes_on(db_path, d)          # 数据指纹
    cache = os.path.join(_CACHE_DIR, "%s.json" % d)
    pl = None
    if cur_n >= _MIN_COVERAGE and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                cpl = json.load(f)
            # 缓存内票数与当前库一致才算命中（杜绝"09-03 只扫到 5 票被缓存一整天"）
            if int(cpl.get("coverage", {}).get("n_codes", -1)) == cur_n:
                pl = cpl
        except Exception:
            pass  # 半截/损坏 → 重扫
    if pl is None:
        # R2-P0.1A：当日缓存 miss → 先吐上一交易日旧缓存（前端秒响应），
        # 后台重扫当日并原子替换（写 tmp 再 rename）。
        # ★ J2（2026-09-13）：重扫从"同进程线程"改为"独立 subprocess"——
        #   scan_all 为 ~4-20s 纯 Python/numpy，线程版抢 GIL 使紧随 warm 请求劣化 13 倍；
        #   subprocess 在独立进程跑（import 成本 ~0.35s < 2s 阈值，无需常驻 sidecar），
        #   Web 进程零阻塞、零 GIL 争用。缓存文件仍由子进程写 tmp+rename 原子替换。
        if not _RESCAN_LOCK.get(d):
            _prev = _prev_trading_cache(d, db_path)
            if _prev is not None:
                _RESCAN_LOCK[d] = True
                pl = dict(_prev)
                pl["coverage"] = dict(pl.get("coverage") or {})
                pl["coverage"]["stale_from"] = _prev.get("coverage", {}).get("latest_date", "")
                pl["stale_ok"] = True
                def _rescan():
                    import subprocess
                    import sys as _sys
                    try:
                        _tmp = cache + ".tmp.json"
                        _code = ("from app.tactics import scan_all; import json, os;"
                                 "f = scan_all(date=%r, db_path=%r);"
                                 "if f['coverage']['n_codes'] >= %d:"
                                 "open(%r, 'w', encoding='utf-8').write(json.dumps(f, ensure_ascii=False));"
                                 "os.replace(%r, %r)"
                                 % (d, db_path, _MIN_COVERAGE, _tmp, _tmp, cache))
                        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                        subprocess.run([_sys.executable, "-c", _code], cwd=_root,
                                       timeout=240, capture_output=True)
                    except Exception:
                        pass
                    finally:
                        _RESCAN_LOCK.pop(d, None)
                threading.Thread(target=_rescan, daemon=True).start()
        if pl is None:
            pl = scan_all(date=d, db_path=db_path)
        if pl["coverage"]["n_codes"] >= _MIN_COVERAGE:
            # 数据完整日才落缓存（只存 base 两卡）；已在后台重扫时不重复同步写
            if not pl.get("stale_ok"):
                try:
                    os.makedirs(_CACHE_DIR, exist_ok=True)
                    with open(cache, "w", encoding="utf-8") as f:
                        json.dump(pl, f, ensure_ascii=False)
                except Exception:
                    pass  # 缓存写失败不影响主结果
        else:
            # 数据不完整日：不落缓存、标记 stale_ok（前端可提示"非完整快照"）
            pl["stale_ok"] = True
    # base 保证只有首板/连板两卡（旧缓存若含竞价卡则截断，防重复合并）
    pl["tactics"] = pl.get("tactics", [])[:2]
    # T2：并入竞价双战法（每次实时求值；内部有独立快照/窗口逻辑）
    try:
        ab = auction_board(db_path=db_path)
        pl["tactics"] = pl.get("tactics", []) + ab.get("tactics", [])
        pl["auction"] = {k: v for k, v in ab.items() if k != "tactics"}
    except Exception:
        pass  # 竞价板失败不影响主板块
    pl["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return pl


def scan_all(date=None, db_path=None):
    """全市场日频扫描（一条 SQL 拉近 90 交易日，向量化过滤后逐票 status）。"""
    from . import config as C
    db_path = db_path or C.DB_FILE
    d = date or _latest_date(db_path)
    rows = _load_recent_klines(db_path, d)
    by_code = _group_by_code(rows)
    # coverage：最新交易日票数 + 全池基准（R2-P0.2 补 ratio 口径，前端按 <0.9 红条）
    latest_rows = [k for k in rows if k["date"] == d]
    n_codes = len(latest_rows)
    total = len(by_code)
    coverage = {"latest_date": d, "n_codes": n_codes, "total": total,
                "ratio": round(n_codes / total, 4) if total else 0.0,
                "complete": bool(n_codes >= _MIN_COVERAGE)}
    # 逐票 status（T1R：删候选预过滤——不再用 win_start=len-14 预筛，对每只
    # len(kl)>=8 的票直接 status()，按 stage 收录；795 票全跑 ~4s，成本不增）
    shouban = ShoubanYangTactic()
    lianban = LianbanA1Tactic()
    tactics_out = []
    st_list = []
    for code, kl in by_code.items():
        if len(kl) < 8:
            continue
        try:
            st = shouban.status(code, d, kl)
            lb = lianban.status(code, d, kl)
        except Exception as e:
            st = {"stage": "ERROR", "detail": "计算异常: %s" % e, "signals": [], "score": 0.0, "updated": ""}
            lb = {"stage": "ERROR", "detail": "计算异常: %s" % e, "signals": [], "score": 0.0,
                  "ladder": "观察", "lbc": 0, "board_type": "none", "amount_ratio": None, "updated": ""}
        if st.get("stage") != "不在池":
            st_list.append({"code": code, "name": _stock_name(code), **st})
        # 连板合并（lbc/板型/ladder 由 status 返回，T1R：复用一次向量化，不再第三次 _vectorize_lianban）
        if lb.get("stage") != "不在梯队":
            tactics_out.append({"tactic": "lianban", "code": code, "name": _stock_name(code), **lb})
    # 按得分降序（高分排前）
    st_list.sort(key=lambda x: x.get("score", 0), reverse=True)
    tactics_out.sort(key=lambda x: x.get("score", 0), reverse=True)
    shouban_st = st_list
    # 组装
    def _stages_summary(name, entries):
        cnt = {}
        for e in entries:
            cnt[e["stage"]] = cnt.get(e["stage"], 0) + 1
        return cnt
    return {
        "tactics": [
            {"name": shouban.NAME, "rule_source": shouban.RULE_SOURCE,
             "stages_summary": _stages_summary(shouban.NAME, shouban_st),
             "statuses": shouban_st},
            {"name": lianban.NAME, "rule_source": lianban.RULE_SOURCE,
             "stages_summary": _stages_summary(lianban.NAME, tactics_out),
             "statuses": tactics_out},
        ],
        "coverage": coverage,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
