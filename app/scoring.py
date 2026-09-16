# -*- coding: utf-8 -*-
"""评分系统 + 市场环境 + 板块分析（v3.2：板块优先真实映射，关键词 fallback）"""
from . import config as C
from . import indicators as ind
from . import sector as sector

# 板块关键词映射（v2.0 移植；后续可升级为 akshare 概念板块）
SECTOR_KEYWORDS = {
    "银行": ["银行"],
    "证券保险": ["证券", "保险", "信托", "期货"],
    "半导体芯片": ["芯片", "半导体", "微电子", "集成电路", "晶圆", "封测", "光刻"],
    "AI算力": ["算力", "数据中心", "服务器", "液冷", "光模块", "CPO"],
    "软件信创": ["软件", "信创", "办公", "操作系统", "数据库", "网络安全", "数字"],
    "消费电子": ["光电", "电子", "电路", "PCB", "面板", "显示", "触控", "传感器", "元件"],
    "新能源": ["新能源", "光伏", "风电", "储能", "电池", "锂", "钴", "镍", "氢能", "充电"],
    "汽车产业链": ["汽车", "汽配", "轮胎", "模具", "动力", "电机", "电控", "驾驶"],
    "医药生物": ["医药", "药业", "生物", "制药", "医疗", "基因", "疫苗", "诊断", "器械"],
    "军工防务": ["军工", "防务", "航天", "航空", "导航", "雷达", "兵器", "船舶"],
    "食品饮料": ["食品", "饮料", "酒", "乳", "肉", "调味", "糖", "粮", "油", "啤酒"],
    "有色资源": ["有色", "矿业", "黄金", "白银", "铜", "铝", "稀土", "钢铁", "煤炭", "资源"],
    "化工材料": ["化工", "化学", "材料", "玻纤", "碳纤维", "氟", "硅", "磷", "膜"],
    "电力能源": ["电力", "能源", "燃气", "水务", "供热", "发电", "电网"],
    "地产基建": ["地产", "房产", "建设", "建筑", "建材", "装修", "工程", "园林"],
    "机械装备": ["重工", "机械", "装备", "设备", "机床", "工具", "精密", "制造"],
    "交通物流": ["交通", "物流", "港口", "高速", "铁路", "机场", "运输", "快递"],
    "传媒游戏": ["传媒", "影视", "游戏", "出版", "广告", "文化", "动漫", "娱乐"],
    "商贸零售": ["百货", "零售", "贸易", "超市", "商业", "电商", "免税"],
    "环保": ["环保", "生态", "节能", "污水", "固废"],
    "农林牧渔": ["农业", "种业", "畜牧", "渔业", "饲料", "农药", "化肥"],
    "家电家居": ["电器", "家电", "家居", "家装", "照明", "卫浴"],
    # ★ v3.8：热门题材补充（概念板块 fallback）
    "低空经济": ["低空", "飞行汽车", "无人机", "eVTOL", "航空器"],
    "机器人": ["机器人", "人形", "减速器", "伺服", "谐波"],
    "AI应用": ["大模型", "AIGC", "多模态", "智能体", "Copilot", "AI+", "语料"],
    "算力液冷": ["液冷", "温控", "冷板", "浸没", "IDC", "算力租赁"],
    "半导体设备": ["刻蚀", "光刻机", "薄膜沉积", "离子注入", "CMP", "晶圆制造"],
    "卫星互联网": ["卫星", "星链", "北斗", "航天电子", "火箭"],
    "创新药": ["创新药", "GLP-1", "ADC", "单抗", "双抗", "CXO", "CRO", "CDMO"],
    "量子科技": ["量子", "光量子", "量子计算"],
    "数据要素": ["数据要素", "数据确权", "数据交易", "数据资产"],
    "可控核聚变": ["核聚变", "托卡马克", "聚变"],
    "固态电池": ["固态电池", "半固态", "硫化物电解质"],
    "工业母机": ["工业母机", "数控机床", "五轴", "刀具"],
    "存储芯片": ["存储芯片", "DRAM", "NAND", "HBM", "闪存"],
    "先进封装": ["先进封装", "Chiplet", "CoWoS", "2.5D", "3D封装"],
    "脑机接口": ["脑机接口", "侵入式", "神经接口"],
}
STOCK_NAME_SECTOR = {
    "通威股份": "新能源", "太极实业": "半导体芯片", "华天科技": "半导体芯片",
}


_UNIVERSE_EXCLUDED_CACHE = {"mtime": None, "codes": None}


def _universe_excluded():
    """运行时宇宙排除：config.DATA_EXCLUDE_CODES ∪ data/universe_excluded.json。
    F6 自动处置（复权跳变不可修/连续缺口>250日）写入后者；保留数据但打标记剔除。"""
    base = set(str(x) for x in getattr(C, "DATA_EXCLUDE_CODES", []) or [])
    try:
        import json as _json
        import os as _os
        path = getattr(C, "UNIVERSE_EXCLUDED_FILE", "data/universe_excluded.json")
        mt = _os.path.getmtime(path) if _os.path.exists(path) else 0
        if _UNIVERSE_EXCLUDED_CACHE["mtime"] != mt:
            codes = set()
            if mt:
                with open(path, encoding="utf-8") as f:
                    d = _json.load(f)
                for rec in (d.get("excluded", []) if isinstance(d, dict) else d):
                    if isinstance(rec, dict):
                        c = rec.get("code")
                    else:
                        c = rec
                    if c:
                        codes.add(str(c))
            _UNIVERSE_EXCLUDED_CACHE["mtime"] = mt
            _UNIVERSE_EXCLUDED_CACHE["codes"] = codes
        base |= (_UNIVERSE_EXCLUDED_CACHE["codes"] or set())
    except Exception:
        pass
    return base


def classify_sector(name, code=None):
    """板块归属：真实板块映射优先（code 已知时），名称关键词 fallback"""
    name = (name or "").strip()
    if code:
        secs = sector.sectors_of(code)
        if secs:
            return secs[0]
    if name in STOCK_NAME_SECTOR:
        return STOCK_NAME_SECTOR[name]
    for _sec, kws in SECTOR_KEYWORDS.items():
        for kw in kws:
            if kw in name:
                return _sec
    return "其他"


def is_main_board(code):
    """主板（排除创业板30/科创板68）"""
    return not (code.startswith("30") or code.startswith("68"))


def calc_breadth(stocks):
    """市场上涨占比"""
    if not stocks:
        return 0.0
    up = sum(1 for s in stocks.values() if (s.get("pct_chg", 0) or 0) > 0)
    return up / len(stocks)


def get_regime(breadth):
    """市场环境分级 → (名称, 最大持仓, 买入阈值, 单仓比例)"""
    for low, name, mp, thr, pp in C.REGIMES:
        if breadth >= low:
            return name, mp, thr, pp
    return C.REGIMES[-1][1], C.REGIMES[-1][2], C.REGIMES[-1][3], C.REGIMES[-1][4]


def calc_sector_strength(stocks):
    """板块强弱 {sector: {avg_pct, up_ratio, count, leaders}}（真实板块映射优先）"""
    sectors = {}
    for code, s in stocks.items():
        name = s.get("name", "")
        pct = s.get("pct_chg", 0) or 0
        sec = classify_sector(name, code)
        d = sectors.setdefault(sec, {"sum": 0.0, "up": 0, "stocks": []})
        d["sum"] += pct
        d["up"] += 1 if pct > 0 else 0
        d["stocks"].append((code, name, pct))
    result = {}
    for sec, d in sectors.items():
        cnt = len(d["stocks"])
        if cnt < C.HERDING_MIN_STOCKS:
            continue
        result[sec] = {
            "avg_pct": round(d["sum"] / cnt, 2),
            "up_ratio": round(d["up"] / cnt, 3),
            "count": cnt,
            "leaders": sorted(d["stocks"], key=lambda x: x[2], reverse=True)[:5],
        }
    return result


def get_top_sectors(strength, top_n=None):
    top_n = top_n or C.SECTOR_TOP_N
    filtered = {k: v for k, v in strength.items() if k != "其他"}
    return sorted(filtered.items(),
                  key=lambda x: (x[1]["avg_pct"], x[1]["up_ratio"]),
                  reverse=True)[:top_n]


def ml_score_bonus(code):
    """★ 4.6 ML 选股分（阶段4 sidecar，主系统只读）。
    读取 data/ml_scores.json（predict.py 夜间生成），按分数全市场分位给加分/减分；
    文件超过 ML_SCORE_MAX_AGE_HOURS 自动失效降级（返回 0）。
    开关 ML_SCORE_ENABLED 默认 False。返回 (bonus, signals)。
    """
    if not C.ML_SCORE_ENABLED:
        return 0, []
    try:
        import json
        import os
        import time
        path = C.ML_SCORE_FILE
        if not os.path.exists(path):
            return 0, []
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h > C.ML_SCORE_MAX_AGE_HOURS:
            return 0, [f"ML分过期({age_h:.0f}h>{C.ML_SCORE_MAX_AGE_HOURS}h)降级"]
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        scores = d.get("scores") or {}
        v = scores.get(code)
        if v is None:
            return 0, []
        vals = sorted(scores.values())
        n = len(vals)
        rank = sum(1 for x in vals if x < v)
        pct = rank / n if n else 0.0
        if pct >= 0.8:
            return C.ML_SCORE_WEIGHT, [f"ML分位{pct:.0%}(+{C.ML_SCORE_WEIGHT})"]
        if pct < 0.2:
            return C.ML_SCORE_PENALTY, [f"ML分位{pct:.0%}({C.ML_SCORE_PENALTY})"]
        return 0, []
    except Exception:
        return 0, []


# ★ Phase25: ml_pred 打分加权因子（严格 PIT：只取 date<=t 的最新预测截面）。
# 按日缓存当日 {code: score} 字典——sqlite3 只读连接、每次换日仅两次查询建缓存，
# 禁止逐票逐日查询；weight=0 时上层直接跳过，零开销。
_ML_PRED_DAY_CACHE = {"date": None, "scores": None, "last_date": None}


def _ml_pred_scores_asof(date):
    """第 t 日可用预测截面 (scores, last_date)（date<=t 的最新一日全截面）。失败返回 ({}, None)。"""
    if _ML_PRED_DAY_CACHE["date"] == date and _ML_PRED_DAY_CACHE["scores"] is not None:
        return _ML_PRED_DAY_CACHE["scores"], _ML_PRED_DAY_CACHE["last_date"]
    scores = {}
    last_date = None
    try:
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=10)
        try:
            row = conn.execute(
                "SELECT MAX(date) FROM ml_pred WHERE date<=?", (date,)).fetchone()
            if row and row[0]:
                last_date = row[0]
                scores = dict(conn.execute(
                    "SELECT code, score FROM ml_pred WHERE date=?", (row[0],)).fetchall())
        finally:
            conn.close()
    except Exception:
        scores = {}
    _ML_PRED_DAY_CACHE["date"] = date
    _ML_PRED_DAY_CACHE["scores"] = scores
    _ML_PRED_DAY_CACHE["last_date"] = last_date
    return scores, last_date


def ml_rank_bonus(code, date):
    """★ Phase25: ml_rank 加权加分（打分层，不改股票池；与旧 ml_score_bonus 完全独立）。
    严格 PIT：只查 ml_pred 中 date<=信号日 的最新一日截面（按日缓存，见 _ml_pred_scores_asof）。
    加分 = (score-0.5)*2*weight*K；无预测记录的票加 0 分（不剔除，避免覆盖偏差）。
    code: 股票代码；date: 信号日 YYYY-MM-DD（回测传 PIT 日期）。返回 (bonus, signals)。
    """
    w = float(getattr(C, "ML_RANK_WEIGHT", 0.0) or 0.0)
    if not w or not code or not date:
        return 0, []
    scores, last_date = _ml_pred_scores_asof(date)
    # ★ F6（2026-09-09）：ML 陈旧降级——截面日期距"今天"超阈值 → 中性权重。
    #   只对当下（实盘）生效：回测传 PIT 日期时 last_date 是当时最新截面，不降级。
    if last_date:
        try:
            import time as _t
            _today = _t.strftime("%Y-%m-%d")
            if last_date < _today:
                from . import trading_calendar as _tc
                _td = len(_tc.trading_days(last_date, _today)) - 1
                _stale = int(getattr(C, "ML_STALE_TDAYS", 8) or 8)
                if _td > _stale:
                    return 0, ["ML陈旧(%s距%d交易日>阈值%d)降级中性"
                               % (last_date, _td, _stale)]
        except Exception:
            pass
    s = scores.get(code)
    if s is None:
        return 0, []
    k = float(getattr(C, "ML_PRED_BONUS_SCALE", 8))
    b = round((s - 0.5) * 2.0 * w * k, 2)
    return b, [f"ML分{s:.3f}({b:+.1f})"]


def score_stock(klines, quote=None, code=None, as_of=None):
    """多维度评分（v2.0 移植）。
    klines: 日K列表，最后一根为信号日（已收盘的完整K线）。
    quote: 可选，{name, price, pct_chg, ...} 用于补充。
    code: ★ 股票代码（业绩/资金面信号需要；不传则跳过这些维度）
    as_of: ★ 信号日（YYYY-MM-DD，回测用 PIT 防未来函数；None=今天）
    返回 (score, signals)
    """
    # ★ F6（2026-09-09）：历史长度门 = 因子最长窗口（MA250/年线/250日新高需 250 根）；
    #   低于上限剔除（原 60 根语义保留为下限）。回测/实盘共用此函数 → 票池同构。
    _min_bars = max(60, int(getattr(C, "MIN_HISTORY_BARS", 250) or 250))
    if len(klines) < _min_bars:
        return 0, ["历史不足"]
    if code and code in _universe_excluded():
        return 0, ["universe_excluded"]
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    highs = [k["high"] for k in klines]
    opens = [k["open"] for k in klines]
    i = len(closes) - 1
    c, o, h = closes[i], opens[i], highs[i]
    prev = closes[i - 1] if i > 0 else c

    ma5 = ind.sma(closes, 5)
    ma10 = ind.sma(closes, 10)
    ma20 = ind.sma(closes, 20)
    vma5 = ind.sma(volumes, 5)
    dif, dea, hist = ind.macd(closes, C.MACD_FAST, C.MACD_SLOW, C.MACD_SIGNAL)
    rsi = ind.rsi(closes, C.RSI_PERIOD)
    h20 = ind.rolling_max(highs, 20)
    rs = ind.rsrs(klines, C.RSRS_WINDOW, C.RSRS_ZSCORE)

    pct = (c - prev) / prev if prev else 0.0
    vr = volumes[i] / vma5[i] if vma5[i] else 1.0
    # ★ Phase28-B：审计证据化权重覆盖（config.FACTOR_WEIGHT_OVERRIDE，默认 None=现状）
    W = getattr(C, "FACTOR_WEIGHT_OVERRIDE", None) or C.SCORE_WEIGHTS
    total, sig = 0, []

    if c > o and pct >= 0.02 and vr >= C.VOL_RATIO_HIGH:
        total += W["volume_price"]; sig.append(f"量价齐升(+{W['volume_price']})")
    if h20[i] and h >= h20[i] and vr >= C.VOL_RATIO_BREAKOUT:
        total += W["breakout"]; sig.append(f"放量突破20日新高(+{W['breakout']})")
    if ma5[i] and ma10[i] and ma20[i] and ma5[i] > ma10[i] > ma20[i]:
        total += W["ma_bullish"]; sig.append(f"均线多头(+{W['ma_bullish']})")
    if rs[i] is not None and rs[i] > C.RSRS_BUY_THRESHOLD:
        total += W["rsrs_bullish"]; sig.append(f"RSRS看涨({rs[i]:.2f},+{W['rsrs_bullish']})")
    if dif[i] and dea[i] and dif[i-1] and dea[i-1] and dif[i-1] <= dea[i-1] and dif[i] > dea[i]:
        total += W["macd_golden"]; sig.append(f"MACD金叉(+{W['macd_golden']})")
    if rsi[i] and rsi[i-1] and rsi[i-1] < C.RSI_OVERSOLD and rsi[i] > rsi[i-1]:
        total += W["rsi_oversold"]; sig.append(f"RSI超卖反弹(+{W['rsi_oversold']})")
    if ma5[i] and ma10[i] and ma5[i] > ma10[i] and vr <= C.VOLUME_SHRINK_RATIO and c >= ma10[i]:
        total += W["volume_shrink"]; sig.append(f"缩量回踩(+{W['volume_shrink']})")
    if ma20[i] and 0 < c - ma20[i] <= ma20[i] * 0.02 and prev < c:
        total += W["pullback_ma"]; sig.append(f"回踩20日线企稳(+{W['pullback_ma']})")
    if ma5[i] and ma10[i] and ma5[i-1] and ma10[i-1] and ma5[i-1] <= ma10[i-1] and ma5[i] > ma10[i]:
        total += W["ma_cross"]; sig.append(f"MA5/10金叉(+{W['ma_cross']})")
    if h20[i] and c >= h20[i] * 0.995:
        total += W["turtle_break"]; sig.append(f"海龟20日突破(+{W['turtle_break']})")
    if rs[i] is not None and rs[i-1] is not None and rs[i] > rs[i-1] > 0:
        total += W["rsrs_accel"]; sig.append(f"RSRS加速(+{W['rsrs_accel']})")
    if pct > 0.05 and vr > 3.0 and c < h * 0.98:
        total += W["penalty_stall"]; sig.append(f"⚠高位放量滞涨({W['penalty_stall']})")
    if rsi[i] and rsi[i] > C.RSI_OVERBOUGHT:
        total += W["penalty_overbought"]; sig.append(f"⚠RSI超买({rsi[i]:.0f},{W['penalty_overbought']})")

    # ★ 4.4 量价深度评分（量价是决定股价的唯二因素）
    vp_vwap = ind.vwap(klines)
    vp_corr = ind.price_volume_corr(closes, volumes, 10)
    vp_slope = ind.volume_slope(volumes, 5)
    vp_surge = ind.volume_surge(volumes, 5)
    vp_div = ind.price_vol_divergence(closes, volumes, 10, 10)
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

    # ★ 4.6 量价信号包（阶段1）：默认关闭；入选信号经回测验证后由 config.VOLPRICE_WEIGHTS 配权重
    #   与回测共用 app/volprice.py 同一实现；命中信号写入 signals 列表（买入时随 reason 进 audit 事件流）
    if C.VOLPRICE_SIGNALS_ENABLED and C.VOLPRICE_WEIGHTS:
        try:
            from . import volprice as vp
            for key, desc in vp.detect_signals(klines):
                w = C.VOLPRICE_WEIGHTS.get(key)
                if w:
                    total += w
                    sig.append(f"{desc}(+{w})")
        except Exception:
            pass

    # ★ 4.6 GTJA191 量价因子（阶段3）：默认关闭；入选因子按 IC 方向给固定小权重
    if C.GTJA_FACTORS_ENABLED and C.GTJA_FACTOR_WEIGHTS:
        try:
            from . import factor as fac
            for fname, w in C.GTJA_FACTOR_WEIGHTS.items():
                vals, ok = fac.compute_factor(fname, klines)
                if not ok or not vals or vals[-1] is None:
                    continue
                v = vals[-1]
                # VOL变异20：负 IC 因子，仅在触发阈值（变异>1 高度不稳定）时减分
                if fname == "VOL变异20" and v > C.GTJA_VOLCV_HIGH:
                    total += w
                    sig.append(f"量能变异{v:.2f}({w})")
        except Exception:
            pass

    # ★ 4.5 业绩事件驱动（PIT 合规：按公告日生效，防未来函数）
    # ★ 修复：score_stock 需传 code（原来 code 不存在 → NameError 被吞，业绩信号从未生效）
    #        as_of 传信号日（回测用），确保只用当日已公告的业绩
    # ★ I1（2026-09-13）：as_of 非空=回测 PIT 上下文 → 显式 offline=True 只读本地库
    #        （回测禁网、零写库）；as_of=None=实盘 → 联网拉最新公告（行为不变）。
    try:
        if code:
            from . import earnings as ea
            e_bonus, e_sig = ea.earnings_signal(code, as_of=as_of,
                                                offline=bool(as_of))
            if e_bonus:
                total += e_bonus
                sig.extend(e_sig)
    except Exception as e:
        # ★ 工程：关键评分子项记日志（不再静默吞掉）
        try:
            import logging
            logging.getLogger("tianji").warning("业绩信号异常 code=%s: %s", code, e)
        except Exception:
            pass

    # ★ 4.6 ML 选股（阶段4；默认关闭，文件超 26h 自动降级）
    if C.ML_SCORE_ENABLED:
        try:
            _mb, _msig = ml_score_bonus(code)
            if _mb:
                total += _mb
                sig.extend(_msig)
        except Exception:
            pass

    # ★ Phase25: ml_rank 加权因子（严格 PIT：date=信号日；默认 weight=0 时整段跳过）
    if getattr(C, "ML_RANK_WEIGHT", 0):
        try:
            _pb, _psig = ml_rank_bonus(code, as_of or (klines[-1].get("date") if klines else None))
            if _pb:
                total += _pb
                sig.extend(_psig)
        except Exception:
            pass
    return max(0, total), sig


def seal_quality_bonus(code, quote=None):
    """★ Phase17 封板质量子分（从涨停池数据取 fbt/fund/ltsz/zbc）。
    权重源自 config.BOARD_SEAL_QUALITY_WEIGHTS；权重全 0 时返回 (0, [])（与现状一致）。
    - 首封时间：≤10:00 加 / 10:00-13:30 中性 / ≥14:30 尾盘减
    - 封单资金/流通市值：≥P80(1.9%)强 / ≥P50(0.9%)中 / 弱
    - 炸板次数：0 加 / 1 中性 / ≥2 减
    连板高度已有 leader_score/score_board 逻辑，不重复计分。
    返回 (bonus, [signals])；code 不在涨停池 → (0, [])。
    """
    W = getattr(C, "BOARD_SEAL_QUALITY_WEIGHTS", {}) or {}
    w_fbt, w_ratio, w_zbc = W.get("first_seal", 0), W.get("seal_ratio", 0), W.get("zhaban", 0)
    if not (w_fbt or w_ratio or w_zbc):
        return 0, []
    try:
        from . import limitup as lu
        zt = lu.limit_up_pool()
        row = next((r for r in zt if r.get("code") == code), None)
        if not row:
            return 0, []
        bonus = 0
        sig = []
        # 1) 首封时间
        fbt = float(row.get("fbt", 0) or 0)
        if w_fbt and fbt > 0:
            if fbt <= C.BOARD_FBT_EARLY:
                bonus += w_fbt; sig.append(f"首封今日早({_fmt_fbt(fbt)},+{w_fbt})")
            elif fbt >= C.BOARD_FBT_LATE:
                bonus -= w_fbt; sig.append(f"尾盘板({_fmt_fbt(fbt)},-{w_fbt})")
            # 10:00-14:30 中性
        # 2) 封单资金/流通市值
        fund = float(row.get("fund", 0) or 0)
        ltsz = float(row.get("ltsz", 0) or 0)
        if w_ratio and fund > 0 and ltsz > 0:
            ratio = fund / ltsz
            if ratio >= C.BOARD_SEAL_RATIO_HIGH:
                bonus += w_ratio; sig.append(f"封单资金比强({ratio*100:.1f}%,+{w_ratio})")
            elif ratio >= C.BOARD_SEAL_RATIO_MID:
                bonus += int(w_ratio * 0.6); sig.append(f"封单资金比中({ratio*100:.1f}%,+{int(w_ratio*0.6)})")
        # 3) 炸板次数
        zbc = int(float(row.get("zbc", 0) or 0))
        if w_zbc:
            if zbc == C.BOARD_ZBC_GOOD:
                bonus += w_zbc; sig.append(f"零炸板(+{w_zbc})")
            elif zbc >= 2:
                bonus -= w_zbc; sig.append(f"多次炸板({zbc}次,-{w_zbc})")
        return bonus, sig
    except Exception:
        return 0, []


def _fmt_fbt(fbt):
    """首封时间 HHMMSS → HH:MM"""
    try:
        v = int(fbt)
        return f"{v // 10000:02d}:{(v % 10000) // 100:02d}"
    except (ValueError, TypeError):
        return str(fbt)


def score_board(klines, quote, hour=None, board_weights=None):
    """打板专项评分（v2.0 移植）。
    hour: 信号时刻的小时数（如 10.5=10:30），回测传收盘后 None 或 15.0。
    board_weights: 可选权重覆盖（P66 验证用；None=现值，不改行为）.
        覆盖键: momentum_9/8.5/8/7, vol_huge/sweep/mild, turnover_mid/ok,
                streak0/1/2, buyer_strong, amp_penalty.
        例: {"vol_sweep": 8, "momentum_7": 0} 降权放量扫板/剔除动量启动档。
    """
    if len(klines) < 20:
        return 0, ["数据不足"]
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    highs = [k["high"] for k in klines]
    pct = quote.get("pct_chg", 0) or 0
    price = quote.get("price", 0) or 0
    high = quote.get("high", 0) or 0
    low = quote.get("low", 0) or 0
    vol = quote.get("volume", 0) or 0
    turnover = quote.get("turnover", 0) or 0
    total, sig = 0, []

    # ★ Phase33：动量档位下限（config.BOARD_MOMENTUM_MIN，默认 7.0=维持现状）。
    #   昨日涨幅低于该阈值的票不给任何动量分；各档原始边界不变。
    _bw = board_weights or {}
    _mm = getattr(C, "BOARD_MOMENTUM_MIN", 7.0)
    if pct >= 9.0 and pct >= _mm: total += _bw.get("momentum_9", 25); sig.append(f"涨停动量极强(+{_bw.get('momentum_9', 25)},{pct:.1f}%)")
    elif pct >= 8.5 and pct >= _mm: total += _bw.get("momentum_8_5", 20); sig.append(f"涨停动量很强(+{_bw.get('momentum_8_5', 20)},{pct:.1f}%)")
    elif pct >= 8.0 and pct >= _mm: total += _bw.get("momentum_8", 15); sig.append(f"涨停动量较强(+{_bw.get('momentum_8', 15)},{pct:.1f}%)")
    elif pct >= 7.0 and pct >= _mm: total += _bw.get("momentum_7", 10); sig.append(f"涨停动量启动(+{_bw.get('momentum_7', 10)},{pct:.1f}%)")

    vma5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else (sum(volumes[:-1]) / max(len(volumes) - 1, 1) if len(volumes) > 1 else 0)
    vr = vol / vma5 if vma5 > 0 else 1.0
    if vr >= 3.0: total += _bw.get("vol_huge", 20); sig.append(f"巨量封板(+{_bw.get('vol_huge', 20)},量比{vr:.1f})")
    elif vr >= 2.0: total += _bw.get("vol_sweep", 15); sig.append(f"放量扫板(+{_bw.get('vol_sweep', 15)},量比{vr:.1f})")
    elif vr >= 1.5: total += _bw.get("vol_mild", 10); sig.append(f"温和放量(+{_bw.get('vol_mild', 10)},量比{vr:.1f})")

    if 5 <= turnover <= 15: total += 15; sig.append(f"换手适中(+15,{turnover:.1f}%)")
    elif 3 <= turnover < 5 or 15 < turnover <= 20: total += 10; sig.append(f"换手可接受(+10,{turnover:.1f}%)")

    streak = ind.detect_streak(klines)
    if streak == 0: total += 20; sig.append("首板优先(+20)")
    elif streak == 1: total += 10; sig.append("二板接力(+10)")
    elif streak == 2: total += 5; sig.append("三板博弈(+5)")

    if high > 0 and price >= high * 0.995:
        total += 5; sig.append("买方强势(+5)")

    if hour is not None:
        if hour < 10.5: total += 10; sig.append("早盘封板(+10)")
        elif hour < 11.5: total += 5; sig.append("午前封板(+5)")
        elif hour >= 14.0: total -= 10; sig.append("尾盘慎入(-10)")

    if high > 0 and low > 0:
        amp = (high - low) / (high + low) * 200
        if amp > 8: total -= 10; sig.append(f"振幅过大(-10,{amp:.1f}%)")

    # ⑧-⑩ 打板增强（v3.3，实时行情字段；回测无字段自动跳过）
    outer = quote.get("outer_vol", 0) or 0
    inner = quote.get("inner_vol", 0) or 0
    if outer + inner > 0:
        oratio = outer / (outer + inner)
        if oratio >= C.BOARD_OUTER_RATIO_HIGH:
            total += 8; sig.append(f"外盘主动买入(+8,{oratio:.0%})")
        elif oratio >= C.BOARD_OUTER_RATIO_MID:
            total += 4; sig.append(f"买方略占优(+4,{oratio:.0%})")
    # ★D-S2（2026-09-16，验收方）：盘口失衡升级——优先十档全量（tdx 提供 bid1~5/ask1~5），
    #   无五档数据（腾讯一档源）自动回退 bid1/ask1 原口径，行为不变。
    bid_vol = sum(quote.get("bid%d_vol" % k, 0) or 0 for k in range(1, 6)) \
        or (quote.get("bid1_vol", 0) or 0)
    ask_vol = sum(quote.get("ask%d_vol" % k, 0) or 0 for k in range(1, 6)) \
        or (quote.get("ask1_vol", 0) or 0)
    if bid_vol > 0 and ask_vol > 0:
        b_ratio = bid_vol / ask_vol
        if b_ratio >= C.BOARD_BID_ASK_HIGH:
            total += 8; sig.append(f"买盘强(+8,买一/卖一{b_ratio:.1f})")
        elif b_ratio >= C.BOARD_BID_ASK_MID:
            total += 5; sig.append(f"买盘占优(+5,买一/卖一{b_ratio:.1f})")
    bid_px = quote.get("bid1_price", 0) or 0
    mktcap = quote.get("float_mktcap", 0) or 0
    if bid_px > 0 and mktcap > 0:
        seal = bid_px * bid_vol / mktcap
        if seal >= C.BOARD_SEAL_PCT_HIGH:
            total += 10; sig.append(f"封单强(+10,{seal*100:.2f}%)")
        elif seal >= C.BOARD_SEAL_PCT_MID:
            total += 5; sig.append(f"封单可(+5,{seal*100:.2f}%)")

    # ★ 4.4 封板量价健康度：缩量封板=惜售（好），天量+分歧=风险（差）
    if len(volumes) >= 15:
        closes_b = [k["close"] for k in klines]
        vols_b = [k["volume"] for k in klines]
        vp_div = ind.price_vol_divergence(closes_b, vols_b, 10, 10)
        vp_surge_b = ind.volume_surge(vols_b, 5)
        di = len(vp_div) - 1
        if vp_div[di] is not None and vp_div[di] < 0 and vp_surge_b[di] is not None and vp_surge_b[di] < 1.0:
            total += 10; sig.append(f"缩量封板惜售(+10,量比{vp_surge_b[di]:.1f})")
        elif vp_div[di] is not None and vp_div[di] > 0.05:
            total -= 8; sig.append(f"⚠封板分歧加大(-8)")
    # ★ Phase17 封板质量子分（权重保守；权重 0 时零影响）
    try:
        _code = quote.get("code") if quote else None
        if _code:
            _qbonus, _qsig = seal_quality_bonus(_code, quote)
            if _qbonus:
                total += _qbonus
                sig.extend(_qsig)
    except Exception:
        pass
    return max(0, total), sig


def score_auction(q, klines, sector_burst, auction_breadth):
    """竞价打板评分（v2.0 移植，公示参考用）"""
    if len(klines) < 20:
        return 0, ["数据不足"]
    op = q.get("open", 0) or q.get("open_price", 0) or 0  # v3.4: datafeed 字段名为 open（兼容旧字段）
    yc = q.get("yest_close", 0) or 0
    av = q.get("volume", 0) or 0
    amount = q.get("amount", 0) or 0
    price = q.get("price", 0) or 0
    apct = (op - yc) / yc * 100 if yc > 0 else 0.0
    yest_vol = klines[-1]["volume"] if klines else 0
    vr = av / yest_vol if yest_vol > 0 else 0.0
    total, sig = 0, []

    if 3.0 <= apct <= 7.0: total += 20; sig.append(f"竞价高开黄金区(+20,{apct:.1f}%)")
    elif 2.0 <= apct < 3.0 or 7.0 < apct <= 9.0: total += 12; sig.append(f"竞价高开较强(+12,{apct:.1f}%)")
    elif 1.0 <= apct < 2.0: total += 6; sig.append(f"竞价温和高开(+6,{apct:.1f}%)")
    elif 9.0 < apct <= 9.8: total += 10; sig.append(f"竞价逼近涨停(+10,{apct:.1f}%)")

    if vr >= 0.10: total += 25; sig.append(f"竞价爆量抢筹(+25,量比{vr:.0%})")
    elif vr >= 0.05: total += 18; sig.append(f"竞价放量(+18,量比{vr:.0%})")
    elif vr >= 0.03: total += 12; sig.append(f"竞价温和放量(+12,量比{vr:.0%})")
    elif vr >= 0.015: total += 6; sig.append(f"竞价略放量(+6,量比{vr:.0%})")

    if len(klines) >= 2:
        yc2, pc2 = klines[-1]["close"], klines[-2]["close"]
        if pc2 > 0 and yc2 >= pc2 * 1.095:
            total += 15; sig.append("昨日涨停(+15)")
            if klines[-1]["open"] > 0 and abs(klines[-1]["open"] - yc2) / yc2 < 0.01:
                total += 5; sig.append("昨日一字板(+5)")

    streak = ind.detect_streak(klines)
    if streak == 0: total += 20; sig.append("首板基因(+20)")
    elif streak == 1: total += 12; sig.append("二板接力(+12)")
    elif streak == 2: total += 5; sig.append("三板博弈(+5)")
    elif streak >= 3: total -= 10; sig.append(f"高位板慎入(-10,{streak}板)")

    if amount >= 30000000: total += 10; sig.append(f"竞价额充足(+10,{amount/1e4:.0f}万)")
    elif amount >= 10000000: total += 5; sig.append(f"竞价额一般(+5,{amount/1e4:.0f}万)")

    sector = classify_sector(q.get("name", ""))
    bc = sector_burst.get(sector, 0)
    if bc >= 3: total += 15; sig.append(f"板块竞价共振(+15,{sector}×{bc})")
    elif bc >= 1: total += 8; sig.append(f"板块竞价同涨(+8,{sector})")

    if auction_breadth >= 0.60: total += 10; sig.append(f"大盘竞价强势(+10,{auction_breadth:.0%})")
    elif auction_breadth >= 0.40: total += 5; sig.append(f"大盘竞价中性(+5,{auction_breadth:.0%})")
    elif auction_breadth < 0.30: total -= 10; sig.append(f"大盘竞价弱(-10,{auction_breadth:.0%})")

    if 0 < price < 20: total += 5; sig.append("低价基因(+5)")
    elif price > 50: total -= 5; sig.append("高价股减分(-5)")

    # ★ 4.5：龙虎榜席位联动（三维验证：席位净买 + 竞价强度 + 板块）
    try:
        from . import moneyflow as mf
        seat = mf.seat_analysis(q.get("code", ""))
        if seat:
            inst, hot = seat.get("inst_net") or 0, seat.get("hot_net") or 0
            if inst > 0 and hot > 0:
                total += 12; sig.append(f"机构+游资合力(+12,{seat['date']})")
            elif inst > 0:
                total += 8; sig.append(f"机构净买确认(+8,{inst/1e4:.0f}万)")
            elif hot > 0:
                total += 5; sig.append(f"游资净买(+5,{hot/1e4:.0f}万)")
            elif inst < 0 or hot < 0:
                total -= 8; sig.append(f"⚠席位净卖出(-8,{seat['date']})")
    except Exception:
        pass
    return max(0, total), sig


def score_twothirty(klines, quote=None):
    """两点半战法（v3.4，依据一年期回测结论落地）：
    涨幅 2~7% + 5分钟MACD金叉 + 量比>1 → 次日收盘卖。
    日线近似版：用日K判断 MACD 金叉与量比，实时行情判断当日涨幅。
    返回 (score, signals)；score>=60 视为满足战法条件。
    """
    if len(klines) < 40:
        return 0, ["数据不足"]
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    i = len(closes) - 1
    c = closes[i]
    prev = closes[i - 1] if i > 0 else c
    pct = (c - prev) / prev * 100 if prev else 0.0
    q = quote or {}
    pct_live = q.get("pct_chg", 0) or 0
    # 涨幅取实时（盘中14:30）或日K（回测收盘）
    pct_use = pct_live if pct_live else pct
    total, sig = 0, []

    if 2.0 <= pct_use <= 7.0:
        total += 35; sig.append(f"涨幅2~7%黄金区(+35,{pct_use:.1f}%)")
    elif pct_use < 2.0:
        return 0, [f"涨幅不足({pct_use:.1f}%)"]
    else:
        return 0, [f"涨幅过高({pct_use:.1f}%)"]

    # MACD 金叉状态（盘中处于金叉；当日刚上穿加分）
    dif, dea, hist = ind.macd(closes, C.MACD_FAST, C.MACD_SLOW, C.MACD_SIGNAL)
    di, da = dif[i], dea[i]
    if di is None or da is None:
        return 0, ["MACD数据不足"]
    if di > da:
        just_now = (dif[i-1] is not None and dea[i-1] is not None and dif[i-1] <= dea[i-1])
        total += 35 if just_now else 25
        sig.append(f"MACD金叉状态(+{35 if just_now else 25},{'当日上穿' if just_now else '持续'})")
    else:
        return 0, ["MACD未金叉"]

    # 量比>1（实时量比优先，回测用成交量比）
    vr = q.get("vol_ratio", 0) or 0
    if not vr and len(volumes) >= 6:
        avg5 = sum(volumes[-6:-1]) / 5
        vr = volumes[i] / avg5 if avg5 > 0 else 1.0
    if vr >= 1.0:
        total += 30; sig.append(f"量比放大(+30,{vr:.2f})")
    else:
        total += 0; sig.append(f"量比不足({vr:.2f})")

    # 加分项：非ST/主板已由调用方过滤；非一字板
    hi = q.get("high", 0) or klines[i]["high"]
    if hi > 0 and c >= hi * 0.995:
        total -= 20; sig.append("⚠尾盘触板(-20)")
    return max(0, total), sig


def moneyflow_signals(code, name="", as_of=None):
    """资金面信号（v3.8）—— 龙虎榜/两融/北向持股。
    ★ I1（2026-09-13）：新增 as_of 参数并显式传给 earnings_signal——
       护栏要求"回测可达路径显式传 as_of"；本函数当前仅实盘路径调用
       （ai/consensus/server/updater），as_of 默认 None=今天语义不变。
    返回 (bonus, signals)：bonus 为加分（可负），signals 为描述列表。
    数据源不可用时静默返回 (0, [])。
    """
    bonus = 0
    sig = []
    try:
        from . import moneyflow as mf
        # 1) 龙虎榜：近期上榜且净买入 → 强信号
        lhb = mf.dragon_tiger_of(code, days=10)
        if lhb:
            latest = lhb[0]
            net = latest.get("net_amt") or 0
            if net > 0:
                bonus += 8
                sig.append(f"龙虎榜净买入(+8,{net/1e4:.0f}万)")
            elif net < 0:
                bonus -= 4
                sig.append(f"龙虎榜净卖出(-4,{net/1e4:.0f}万)")
            else:
                bonus += 2
                sig.append(f"龙虎榜上榜(+2)")
        # ★ 4.5：龙虎榜席位深度（机构/游资合力，比单纯净买额更准）
        try:
            seat = mf.seat_analysis(code)
            if seat:
                inst, hot = seat.get("inst_net") or 0, seat.get("hot_net") or 0
                if inst > 0 and hot > 0:
                    bonus += 10
                    sig.append(f"机构+游资合力({seat['verdict']},+10)")
                elif inst > 0:
                    bonus += 8
                    sig.append(f"机构净买{inst/1e4:.0f}万(+8)")
                elif hot > 0:
                    bonus += 5
                    sig.append(f"游资净买{hot/1e4:.0f}万(+5)")
                elif inst < 0 or hot < 0:
                    bonus -= 6
                    sig.append(f"⚠席位净卖出({seat['verdict']},-6)")
        except Exception:
            pass
        # 2) 两融：融资余额增长 → 杠杆资金看多
        mr = mf.margin_change_ratio(code, days=5)
        if mr is not None:
            if mr > 0.02:
                bonus += 6
                sig.append(f"融资余额增长(+6,{mr*100:.1f}%)")
            elif mr < -0.02:
                bonus -= 3
                sig.append(f"融资余额下降(-3,{mr*100:.1f}%)")
        # 3) 北向持股：增持 → 外资看多（季度数据，信号较弱）
        nc = mf.northbound_change(code)
        if nc is not None:
            if nc > 0:
                bonus += 3
                sig.append(f"北向增持(+3,{nc*100:.1f}%)")
            elif nc < 0:
                bonus -= 2
                sig.append(f"北向减持(-2,{nc*100:.1f}%)")
        # ★ 4.0：主力资金流（东财接口，被封时本地量价近似）
        try:
            from . import fflow as ff
            trend, fscore, fdesc = ff.main_inflow_trend(code, days=5)
            if fscore > 0:
                bonus += min(8, fscore * 2 + 2)
                sig.append(f"主力{fdesc}(+{min(8, fscore*2+2)})")
            elif fscore < 0:
                bonus += fscore * 2
                sig.append(f"主力{fdesc}({fscore*2})")
        except Exception:
            pass
        # ★ 4.5：业绩事件驱动（预增=题材催化，PIT 按公告日）
        # ★ I1：显式传 as_of（护栏要求）；offline=bool(as_of) 同 score_stock 语义
        try:
            from . import earnings as ea
            e_bonus, e_sig = ea.earnings_signal(code, as_of=as_of,
                                                offline=bool(as_of))
            if e_bonus:
                bonus += e_bonus
                sig.extend(e_sig)
        except Exception:
            pass
    except Exception:
        pass
    return bonus, sig


def sentiment_bonus(code=None):
    """市场情绪加分（4.0 实战体系）—— 打板/扫板评分用。
    高潮期 + 空间板高 + 昨日涨停溢价正 → 加分；冰点/退潮 → 大减分。
    返回 (bonus, signals)；数据不可用时 (0, [])。
    """
    bonus = 0
    sig = []
    try:
        from . import sentiment as senti
        s = senti.cached_sentiment(max_age=120)
        phase = s.get("phase", "")
        score = s.get("score", 50) or 50
        # 相位加分
        if phase == "高潮":
            bonus += 12
            sig.append(f"情绪高潮(+12)")
        elif phase == "发酵":
            bonus += 6
            sig.append(f"情绪发酵(+6)")
        elif phase == "退潮":
            bonus -= 15
            sig.append(f"情绪退潮(-15)")
        elif phase == "冰点":
            bonus -= 20
            sig.append(f"情绪冰点(-20)")
        # 空间板高度（情绪强弱的另一信号）
        md = s.get("max_days", 0) or 0
        if md >= 5:
            bonus += 5
            sig.append(f"空间板{md}板(+5)")
        elif md <= 2 and phase in ("发酵", "高潮"):
            bonus -= 5
            sig.append(f"空间板仅{md}板(-5)")
        # 昨日涨停溢价（赚钱效应温度计）
        yz = s.get("yesterday") or {}
        avg = yz.get("avg_ret")
        if avg is not None:
            if avg > 0.02:
                bonus += 5
                sig.append(f"昨涨停溢价+{yz.get('avg_ret_pct')}%(+5)")
            elif avg < -0.02:
                bonus -= 8
                sig.append(f"昨涨停溢价{yz.get('avg_ret_pct')}%(-8)")
        return bonus, sig
    except Exception:
        return 0, []


def leader_score(code, name="", quote=None, klines=None):
    """龙头质量打分（4.0，参考 dragon-quant 四维）：
    带动性 / 抗跌性 / 领涨性 / 资金承接性 → 识别板块龙头。
    返回 (score, signals)；数据不足时 (0, [])。
    """
    score = 0
    sig = []
    try:
        from . import limitup as lu
        from . import datafeed as df
        q = quote or {}
        # 1) 领涨性：是否在今日涨停池 + 封板时间早（早封板 = 领涨）
        zt = lu.limit_up_pool()
        in_pool = next((r for r in zt if r["code"] == code), None)
        if in_pool:
            score += 20
            sig.append(f"涨停在池(+20,{in_pool.get('days',1)}板)")
            fbt = in_pool.get("fbt", 0) or 0
            if fbt:
                if fbt < 100000:
                    score += 15; sig.append("早盘首封(+15,领涨性强)")
                elif fbt < 113000:
                    score += 8; sig.append(f"午前封板(+8)")
                elif fbt >= 140000:
                    score -= 5; sig.append("尾盘封板(-5,跟随)")
        # 2) 资金承接性：封单资金 / 换手结构
        if in_pool:
            fund = in_pool.get("fund", 0) or 0
            if fund > 1e8:
                score += 10; sig.append(f"封单强(+10,{fund/1e8:.1f}亿)")
            elif fund > 3e7:
                score += 5; sig.append(f"封单可(+5,{fund/1e7:.0f}千万)")
        # 3) 带动性：所属行业在涨停池中家数（板块共振）
        if in_pool:
            sec = in_pool.get("sector", "") or ""
            pool_secs = {}
            for r in zt:
                pool_secs[r.get("sector", "")] = pool_secs.get(r.get("sector", ""), 0) + 1
            cnt = pool_secs.get(sec, 0)
            if cnt >= 3:
                score += 10; sig.append(f"板块共振(+10,{sec}×{cnt})")
            elif cnt >= 2:
                score += 5; sig.append(f"板块联动(+5,{sec}×{cnt})")
        # 4) 抗跌性：连板高度保护（高板 = 经过分歧检验）
        if in_pool:
            days = in_pool.get("days", 1) or 1
            if days >= 3:
                score += 10; sig.append(f"高标{days}板(+10,抗分歧)")
            elif days == 2:
                score += 5; sig.append(f"2板接力(+5)")
        # ★ 4.1：题材驱动 —— 该股涨停原因涉及的主题在今日爆发度（最强主线加分）
        try:
            tscore, tsig = theme_score(code)
            if tscore:
                score += tscore
                sig.extend(tsig)
        except Exception:
            pass
        return score, sig
    except Exception:
        return 0, []


def theme_score(code):
    """题材强度加分（4.1）：个股涨停原因里的题材若属今日爆发主线 → 加分。
    返回 (bonus, signals)。数据不可用返回 (0, [])。
    """
    try:
        from . import limitup as lu
        reasons = lu.limit_up_reasons()
        themes = lu.theme_burst()
        if not themes:
            return 0, []
        # 找该股的涨停原因
        mine = next((r for r in reasons if r["code"] == code), None)
        if not mine:
            return 0, []
        reason = mine.get("reason", "") or ""
        bonus = 0
        sig = []
        for part in reason.split("+"):
            part = part.strip()
            cnt = themes.get(part, 0)
            if cnt >= 5:
                bonus += 12
                sig.append(f"主线题材[{part}](+12,涨停{cnt}家)")
            elif cnt >= 3:
                bonus += 8
                sig.append(f"强题材[{part}](+8,涨停{cnt}家)")
            elif cnt >= 2:
                bonus += 4
                sig.append(f"题材发酵[{part}](+4,涨停{cnt}家)")
        return bonus, sig
    except Exception:
        return 0, []


def vp_analysis(klines):
    """★ 4.4 量价综合诊断：一行文本 + 量价状态标记，供实时监控/自选观察。
    返回 (verdict, tags)。klines 为空或不足返回 (None, [])。
    """
    try:
        if not klines or len(klines) < 20:
            return None, []
        closes = [k["close"] for k in klines]
        volumes = [k["volume"] for k in klines]
        i = len(closes) - 1
        vw = ind.vwap(klines)
        corr = ind.price_volume_corr(closes, volumes, 10)
        slope = ind.volume_slope(volumes, 5)
        surge = ind.volume_surge(volumes, 5)
        div = ind.price_vol_divergence(closes, volumes, 10, 10)
        tags = []
        vw_tag = "价在VWAP上" if vw[i] and closes[i] > vw[i] else ("价破VWAP" if vw[i] else "")
        if vw_tag:
            tags.append(vw_tag)
        if corr[i] is not None and corr[i] >= C.VP_CORR_GOOD:
            tags.append(f"量价齐升({corr[i]:.2f})")
        elif corr[i] is not None and corr[i] < C.VP_CORR_BAD:
            tags.append(f"量价背离({corr[i]:.2f})")
        if slope[i] is not None and slope[i] > C.VP_VOL_SLOPE_GOOD:
            tags.append("量能递增")
        elif slope[i] is not None and slope[i] < -C.VP_VOL_SLOPE_GOOD:
            tags.append("量能萎缩")
        if surge[i] is not None and surge[i] >= C.VP_SURGE_GOOD:
            tags.append(f"放量{surge[i]:.1f}x")
        if div[i] is not None and div[i] > C.VP_DIVERGENCE_BAD:
            tags.append("⚠量价背离")
        # 一句话结论
        if tags:
            verdict = "、".join(tags)
        else:
            verdict = "量价平淡"
        return verdict, tags
    except Exception:
        return None, []



# ==== I2 统一评分入口（2026-09-13）====
def prescreen(candidates, focus=None, leaders=None, top=80, quiet_top=20):
    """候选集动量粗筛（I2：从 trader.scan_once 的 _rough 抽成共用，实盘/回测同一候选口径）。
    candidates: list[(code, quote)]，quote 需含 pct_chg/vol_ratio/turnover/amount。
    两级：强度分 top80 + 今日平淡高额 top20 + focus/leaders 必精评。
    返回 pool:set（code 集合）。
    """
    def _rough(q):
        pct = q.get("pct_chg", 0) or 0
        vr = q.get("vol_ratio", 0) or 0
        to = q.get("turnover", 0) or 0
        am = q.get("amount", 0) or 0
        return (pct + min(5.0, vr) * 2 + min(20.0, to) * 0.8
                + min(3.0, (am / 1e7) ** 0.5) * 2)
    ordered = sorted(candidates, key=lambda x: -_rough(x[1]))
    pool = {c for c, _ in ordered[:top]}
    quiet = [(c, q) for c, q in ordered[top:]
             if (q.get("pct_chg", 0) or 0) < 1 and (q.get("vol_ratio", 0) or 0) < 1.5]
    quiet.sort(key=lambda x: -(x[1].get("amount", 0) or 0))
    pool |= {c for c, _ in quiet[:quiet_top]}
    pool |= (focus or set()) | (leaders or set())
    return pool


def score_final(klines, quote=None, code=None, name="", as_of=None, ctx=None,
                mf_bonus=0, mf_signals=None):
    """★ 唯一评分入口（I2，单一事实来源）。
    依序：基础 score_stock（含业绩 PIT bonus）→ ctx 加分（自选/板块抱团/板块龙头）
          → 资金面 mf_bonus（由调用方注入，回测不注入——历史资金面无 PIT 版本）。
    ctx: {focus:set, strong_sectors:set, leaders:set}；None=无加分（=旧回测口径）。
    mf_bonus/mf_signals: 资金面精评注入（trader 池内重排用）。
    返回 (score, signals, breakdown)；breakdown 每项含 item/score/signals，可审计对拍。
    """
    base_score, base_sig = score_stock(klines, quote=quote, code=code, as_of=as_of)
    breakdown = [{"item": "base", "score": base_score, "signals": list(base_sig)}]
    score = base_score
    signals = list(base_sig)
    if ctx:
        focus = ctx.get("focus") or set()
        if code and code in focus:
            score += 10
            signals.append("重点关注(+10)")
            breakdown.append({"item": "focus", "score": 10, "signals": ["重点关注(+10)"]})
        strong = ctx.get("strong_sectors") or set()
        if strong:
            sec = classify_sector(name, code)
            if sec in strong:
                score += 10
                signals.append("板块抱团:%s(+10)" % sec)
                breakdown.append({"item": "sector", "score": 10,
                                  "signals": ["板块抱团:%s(+10)" % sec]})
        leaders = ctx.get("leaders") or set()
        if code and code in leaders:
            score += 5
            signals.append("板块龙头(+5)")
            breakdown.append({"item": "leader", "score": 5, "signals": ["板块龙头(+5)"]})
    if mf_bonus:
        score += mf_bonus
        signals.extend(mf_signals or [])
        breakdown.append({"item": "moneyflow", "score": mf_bonus,
                          "signals": list(mf_signals or [])})
    return score, signals, breakdown
