# -*- coding: utf-8 -*-
"""LLM 因子挖掘循环（4.3）—— 借鉴 microsoft/RD-Agent / qlib Alpha158 / NeurIPS 2025 RD-Agent-Quant
把"研究员"变成流水线：
  1. LLM 基于当前因子表现提出新的因子表达式（白名单算子，ast 安全解析）
  2. 表达式翻译为本地纯 Python 计算（复用 indicators.py 指标）
  3. 用现有 factor_ic() 做 IC 检验（含 walk-forward 分层）
  4. 结果反馈给 LLM，迭代改进

核心设计：
  - 白名单算子：closes 序列上的 sma/ema/rank/delta/ts_max/ts_min/std/corr 等
  - ast 安全解析：只允许白名单函数 + 算术，杜绝任意代码执行
  - 因子池持久化：SQLite 记录每个候选因子的表达式与 IC 历史
"""
import ast
import json
import math
import sqlite3
import threading
import time

from . import config as C
from . import factor as fac
from . import db as _db            # ★ J3：统一连接工厂

_lock = threading.Lock()


# ==================== 白名单算子（表达式可用） ====================
def _sma(x, n):
    from . import indicators as ind
    return ind.sma(x, n)


def _rank(x, window=250):
    """横截面秩（近 window 日内相对排名，0-1）"""
    n = len(x)
    out = [None] * n
    for i in range(window - 1, n):
        seg = x[i - window + 1:i + 1]
        v = x[i]
        rank = sum(1 for s in seg if s < v) / len(seg)
        out[i] = rank
    return out


def _delta(x, n):
    out = [None] * len(x)
    for i in range(n, len(x)):
        if x[i - n] != 0:
            out[i] = x[i] / x[i - n] - 1
    return out


def _ts_max(x, n):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        out[i] = max(x[i - n + 1:i + 1])
    return out


def _ts_min(x, n):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        out[i] = min(x[i - n + 1:i + 1])
    return out


def _ts_std(x, n):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        seg = x[i - n + 1:i + 1]
        m = sum(seg) / n
        out[i] = math.sqrt(sum((s - m) ** 2 for s in seg) / n)
    return out


def _ts_corr(x, y, n):
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        sx, sy = x[i - n + 1:i + 1], y[i - n + 1:i + 1]
        mx, my = sum(sx) / n, sum(sy) / n
        num = sum((a - mx) * (b - my) for a, b in zip(sx, sy))
        dx = math.sqrt(sum((a - mx) ** 2 for a in sx))
        dy = math.sqrt(sum((b - my) ** 2 for b in sy))
        out[i] = num / (dx * dy) if dx > 0 and dy > 0 else 0.0
    return out


def _mul(x, y):
    return [a * b if a is not None and b is not None else None for a, b in zip(x, y)]


def _sub(x, y):
    return [a - b if a is not None and b is not None else None for a, b in zip(x, y)]


FUNCS = {
    "sma": _sma, "rank": _rank, "delta": _delta,
    "ts_max": _ts_max, "ts_min": _ts_min, "ts_std": _ts_std,
    "ts_corr": _ts_corr, "mul": _mul, "sub": _sub,
    "abs": lambda x: [abs(v) if v is not None else None for v in x],
    "neg": lambda x: [-v if v is not None else None for v in x],
}


# ==================== 表达式安全解析 ====================
class _SafeEval(ast.NodeVisitor):
    def __init__(self, ctx):
        self.ctx = ctx  # {"closes": [...], "volumes": [...]}

    def visit_Constant(self, node):
        return node.value

    def visit_Name(self, node):
        if node.id in self.ctx:
            return self.ctx[node.id]
        if node.id == "None":
            return None
        raise ValueError(f"未知变量 {node.id}")

    def visit_List(self, node):
        return [self.visit(e) for e in node.elts]

    def visit_Call(self, node):
        fname = node.func.id if isinstance(node.func, ast.Name) else ""
        if fname not in FUNCS:
            raise ValueError(f"未知函数 {fname}")
        args = [self.visit(a) for a in node.args]
        # 函数参数保持原样（标量 n 直接传，序列传列表）
        return FUNCS[fname](*args)

    def visit_BinOp(self, node):
        op_map = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                  ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}
        op = op_map.get(type(node.op))
        if not op:
            raise ValueError("不支持的运算符")
        left = self._broadcast(self.visit(node.left))
        right = self._broadcast(self.visit(node.right))
        return op(left, right)

    def visit_UnaryOp(self, node):
        v = self._broadcast(self.visit(node.operand))
        if isinstance(node.op, ast.USub):
            return [-x if x is not None else None for x in v]
        return v

    def _broadcast(self, v):
        if isinstance(v, list) and v and isinstance(v[0], list):
            # 嵌套列表：取第一个序列（函数参数如 sma(x, n) 的 n 已是标量则不需要）
            return v
        if isinstance(v, (int, float)):
            # 标量广播为与 closes 等长的列表
            n = len(self.ctx.get("closes", []))
            return [float(v)] * n
        return v


def evaluate_expression(expr, closes, volumes=None):
    """安全求值因子表达式。expr: 如 "delta(closes, 5)" / "rank(closes, 20)"
    返回 (values_list, ok, error)
    """
    ctx = {"closes": closes, "volumes": volumes or [0.0] * len(closes)}
    try:
        tree = ast.parse(expr, mode="eval")
        ev = _SafeEval(ctx)
        result = ev.visit(tree.body)
        if isinstance(result, list):
            return result, True, ""
        return [float(result)] * len(closes), True, ""
    except Exception as e:
        return None, False, str(e)


# ==================== 因子池持久化 ====================
def _conn():
    conn = _db.open_rw(C.DB_FILE)   # ★ J3：统一连接工厂（WAL 初始化已由 db.py 一次完成）
    conn.execute("""CREATE TABLE IF NOT EXISTS factor_pool(
        expr TEXT PRIMARY KEY, ic REAL, icir REAL, samples INT,
        created TEXT, status TEXT)""")
    return conn


def save_factor(expr, ic_result, status="candidate"):
    try:
        with _lock:
            conn = _conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO factor_pool(expr,ic,icir,samples,created,status) "
                    "VALUES(?,?,?,?,?,?)",
                    (expr, ic_result.get("ic_mean"), ic_result.get("icir"),
                     ic_result.get("samples"), time.strftime("%Y-%m-%d %H:%M:%S"), status))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def load_factor_pool(limit=50):
    try:
        with _lock:
            conn = _conn()
            try:
                cur = conn.execute(
                    "SELECT expr,ic,icir,samples,created FROM factor_pool "
                    "WHERE ic IS NOT NULL ORDER BY ABS(ic) DESC LIMIT ?", (limit,))
                return [{"expr": r[0], "ic": r[1], "icir": r[2],
                         "samples": r[3], "created": r[4]} for r in cur.fetchall()]
            finally:
                conn.close()
    except Exception:
        return []


# ==================== LLM 挖掘循环 ====================
_MINER_PROMPT = """你是 A股因子研究员。当前因子库表现如下（IC=因子值与未来5日收益的截面相关性）：

{current_factors}

{failure_feedback}
请提出 {n} 个【新的】候选因子表达式。要求：
1. 只用白名单算子：sma(序列,n) rank(序列,n) delta(序列,n) ts_max/min/std(序列,n)
   ts_corr(序列1,序列2,n) mul(a,b) sub(a,b) abs(x) neg(x)，变量仅 closes（收盘价序列）与 volumes（成交量序列）
2. 每个表达式用一行 Python 语法，如：delta(closes, 5) 或 sub(rank(closes,20), rank(volumes,20))
3. 与传统因子（动量/RSI/MACD）差异化，聚焦量价背离、时序结构
4. 输出格式：每行一个表达式，不要解释。

输出表达式："""


def mine_with_llm(current_pool=None, n=5, provider="auto", failure_feedback=""):
    """LLM 提出候选因子表达式。返回表达式列表。
    provider: auto(优先ollama→api) / local(随机生成测试)
    failure_feedback: ★ 4.6 上轮失败因子与原因（RD-Agent 式回注，避免重复提出同类表达式）
    """
    from . import state as st
    cfg = st.load_llm_config()
    current_txt = ""
    pool = current_pool or load_factor_pool(5)
    if pool:
        current_txt = "\n".join(f"- {p['expr']}: IC={p['ic']}" for p in pool)
    else:
        current_txt = "- 暂无（从零开始）"
    fb_txt = failure_feedback if failure_feedback else "- 无"
    prompt = _MINER_PROMPT.format(current_factors=current_txt, n=n,
                                  failure_feedback=fb_txt)

    if provider == "local":
        # 本地模板生成（无 LLM 时用组合模板，保证功能可测）
        # ★ 4.6 演示用：强候选与"与现有因子高重叠"样本置前，供三道闸（IC/相关性/WF）演示
        templates = [
            "ts_std(volumes, 20)",              # 强候选：量能变异（IC≈-0.03 量级）
            "ts_corr(rank(closes, 20), rank(volumes, 20), 10)",  # 与量价相关高重叠样本
            "mul(rank(closes, 60), rank(volumes, 5))",
            "ts_corr(closes, volumes, 10)",
            "delta(closes, 5)", "delta(closes, 10)", "delta(volumes, 5)",
            "sub(rank(closes, 20), rank(volumes, 20))",
            "mul(delta(closes, 5), rank(volumes, 10))",
            "ts_std(closes, 20)", "sub(ts_max(closes, 10), closes)",
            "sub(delta(closes, 5), delta(volumes, 5))",
        ]
        return templates[:n]

    try:
        if cfg.get("ollama_model"):
            from . import review as rv
            text = rv._ollama_chat(cfg["ollama_model"], prompt,
                                   cfg.get("ollama_url", "http://127.0.0.1:11434"))
        elif cfg.get("api_key") and cfg.get("model"):
            from . import review as rv
            text = rv._openai_chat(cfg["api_key"], cfg["model"], prompt,
                                   cfg.get("api_base", "https://api.deepseek.com/v1"))
        else:
            return None  # 无 LLM
        exprs = [l.strip() for l in text.split("\n") if l.strip() and "(" in l]
        return exprs[:n]
    except Exception:
        return None


def _values_of(expr, klines_by_code):
    """计算表达式在全池的因子值。返回 (values_by_code, err)"""
    values_by_code = {}
    ok_count = 0
    for code, kl in klines_by_code.items():
        if len(kl) < 60:
            continue
        closes = [k["close"] for k in kl]
        volumes = [k.get("volume", 0) or 0 for k in kl]
        vals, ok, err = evaluate_expression(expr, closes, volumes)
        if not ok:
            return None, f"表达式求值失败: {err}"
        values_by_code[code] = vals
        ok_count += 1
    if ok_count < 3:
        return None, "覆盖股票数不足"
    return values_by_code, ""


def validate_and_score(expr, klines_by_code, horizon=5, min_cross=None):
    """验证并评分一个因子表达式。返回 IC 结果 dict 或 None。"""
    values_by_code, err = _values_of(expr, klines_by_code)
    if values_by_code is None:
        return None
    return _ic_from_values(expr, values_by_code, klines_by_code, horizon, min_cross)


def _ic_from_values(expr, values_by_code, klines_by_code, horizon, min_cross=None):
    """对表达式因子值做截面 IC 检验（复用 factor.factor_ic 的统计逻辑）"""
    if min_cross is None:
        min_cross = max(3, len(klines_by_code) // 2)
    daily = {}
    for code, kl in klines_by_code.items():
        vals = values_by_code.get(code)
        if not vals or len(vals) != len(kl):
            continue
        closes = [k["close"] for k in kl]
        for i in range(len(kl) - horizon):
            if vals[i] is None or closes[i + horizon] <= 0 or closes[i] <= 0:
                continue
            fwd = closes[i + horizon] / closes[i] - 1
            d = kl[i]["date"]
            daily.setdefault(d, []).append((vals[i], fwd))
    ics = []
    for d, pairs in daily.items():
        if len(pairs) < min_cross:
            continue
        ic = fac.spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            ics.append(ic)
    if len(ics) < 3:
        return None
    mean = sum(ics) / len(ics)
    std = math.sqrt(sum((x - mean) ** 2 for x in ics) / len(ics)) if len(ics) > 1 else 0.0
    pos = sum(1 for x in ics if x > 0)
    return {
        "name": expr, "samples": len(ics),
        "ic_mean": round(mean, 4), "ic_std": round(std, 4),
        "icir": round(mean / std, 4) if std > 0 else 0.0,
        "positive_ratio": round(pos / len(ics), 3),
        "horizon": horizon,
    }


# ==================== ★ 4.6 闭环升级：相关性拒斥 + walk-forward 门禁（RD-Agent 思路） ====================
def _factor_corr_with_pool(expr, values_by_code, klines_by_code, bar=80, max_codes=300):
    """新因子与因子池（现有 factor.FACTORS 全部因子）的时序相关：逐股对齐尾段 bar 根，
    跨股平均 |r|，取与池内因子的最大值。返回 (最相似因子名, 最大|r|)
    """
    from . import factor as fac
    codes = list(klines_by_code.keys())[:max_codes]
    best_name, best_r = "", 0.0
    for pname in fac.FACTORS.keys():
        rs = []
        for code in codes:
            kl = klines_by_code.get(code)
            vals_expr = values_by_code.get(code)
            if not kl or not vals_expr or len(vals_expr) != len(kl):
                continue
            pvals, ok = fac.compute_factor(pname, kl)
            if not ok:
                continue
            xs, ys = [], []
            for i in range(len(kl) - bar, len(kl)):
                if vals_expr[i] is not None and pvals[i] is not None:
                    xs.append(vals_expr[i]); ys.append(pvals[i])
            if len(xs) < 20:
                continue
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            num = sum((xs[j] - mx) * (ys[j] - my) for j in range(len(xs)))
            dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
            dy = math.sqrt(sum((y - my) ** 2 for y in ys))
            if dx > 0 and dy > 0:
                rs.append(abs(num / (dx * dy)))
        if rs:
            m = sum(rs) / len(rs)
            if m > best_r:
                best_r, best_name = m, pname
    return best_name, best_r


def walk_forward_ic(expr, values_by_code, klines_by_code, horizon=5, folds=None):
    """walk-forward 稳定性验证：按日期把样本切成 folds 段，各段独立算截面 IC。
    稳定判定：≥MINER_WF_MIN_STABLE 折与整体方向一致，且整体 |平均IC| ≥ 0.01。
    返回 {"folds_ic": [...], "mean_ic": float, "stable": bool, "positive_folds": n} 或 None（样本不足）
    """
    folds = folds or C.MINER_WF_FOLDS
    all_dates = sorted({k["date"] for kl in klines_by_code.values() for k in kl})
    if len(all_dates) < folds * 20:
        return None
    seg = len(all_dates) // folds
    bounds = [(all_dates[f * seg],
               all_dates[min((f + 1) * seg, len(all_dates)) - 1]) for f in range(folds)]
    fold_ics = []
    for d0, d1 in bounds:
        pairs = []
        for code, kl in klines_by_code.items():
            vals = values_by_code.get(code)
            if not vals or len(vals) != len(kl):
                continue
            closes = [k["close"] for k in kl]
            for i in range(len(kl) - horizon):
                if d0 <= kl[i]["date"] <= d1:
                    if vals[i] is None or closes[i + horizon] <= 0 or closes[i] <= 0:
                        continue
                    pairs.append((vals[i], closes[i + horizon] / closes[i] - 1))
        if len(pairs) < 50:
            continue
        ic = fac.spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            fold_ics.append(round(ic, 4))
    if len(fold_ics) < 2:
        return None
    mean = sum(fold_ics) / len(fold_ics)
    pos = sum(1 for x in fold_ics if x > 0)
    neg = sum(1 for x in fold_ics if x < 0)
    same_dir = max(pos, neg)
    stable = same_dir >= C.MINER_WF_MIN_STABLE and abs(mean) >= 0.01
    return {"folds_ic": fold_ics, "mean_ic": round(mean, 4),
            "stable": stable, "positive_folds": pos}


def run_mining_round(klines_by_code, n=5, horizon=5, provider="auto", feedback=None):
    """一轮挖掘：LLM提表达式 → 全部验证IC → 三道闸（IC门槛/相关性拒斥/walk-forward）→ 入库。
    feedback: 上轮失败记录 [{expr, reason}]，MINER_FEEDBACK_ENABLED 时回注入提示。
    返回 {"candidates": [...已入池], "rejected": [{expr, reason}], "feedback": [...],
          "best": {...}, "round_prompt": str}
    ★ 4.6 升级（config 开关控制；全部关闭时行为与旧版一致）
    """
    feedback = feedback or []
    fb_txt = ""
    if C.MINER_FEEDBACK_ENABLED and feedback:
        fb_txt = ("上一轮失败因子与原因（请避免提出近似表达式）：\n"
                  + "\n".join(f"- {f.get('expr')} :: {f.get('reason')}"
                              for f in feedback[-20:]))
    exprs = mine_with_llm(provider=provider, n=n, failure_feedback=fb_txt)
    if not exprs:
        return {"error": "无 LLM 且无候选", "candidates": [], "rejected": [],
                "feedback": feedback, "round_prompt": fb_txt}
    admitted, rejected, new_fb = [], [], []
    for expr in exprs:
        values_by_code, err = _values_of(expr, klines_by_code)
        if values_by_code is None:
            reason = f"表达式无效: {err}"
            new_fb.append({"expr": expr, "reason": reason})
            rejected.append({"expr": expr, "reason": reason})
            continue
        r = _ic_from_values(expr, values_by_code, klines_by_code, horizon)
        if r is None:
            reason = "IC 样本不足"
            new_fb.append({"expr": expr, "reason": reason})
            rejected.append({"expr": expr, "reason": reason})
            continue
        icm = r.get("ic_mean") or 0
        # 闸1：IC 门槛
        if abs(icm) < C.MINER_IC_MIN:
            reason = f"IC={icm:+.4f} 低于门槛|{C.MINER_IC_MIN}|"
            new_fb.append({"expr": expr, "reason": reason})
            rejected.append({"expr": expr, "reason": reason})
            continue
        # 闸2：与因子池相关性拒斥
        if C.MINER_CORR_REJECT_ENABLED:
            pname, pr = _factor_corr_with_pool(expr, values_by_code, klines_by_code)
            if pr > C.MINER_CORR_MAX:
                reason = f"与因子池[{pname}]相关性{pr:.2f}>{C.MINER_CORR_MAX}，拒绝入池"
                new_fb.append({"expr": expr, "reason": reason})
                rejected.append({"expr": expr, "reason": reason})
                continue
        # 闸3：walk-forward 稳定性
        if C.MINER_WF_ENABLED:
            wf = walk_forward_ic(expr, values_by_code, klines_by_code, horizon)
            if wf is None:
                reason = "walk-forward 样本不足"
                new_fb.append({"expr": expr, "reason": reason})
                rejected.append({"expr": expr, "reason": reason})
                continue
            if not wf["stable"]:
                reason = (f"walk-forward 不稳定 folds={wf['folds_ic']} "
                          f"同向{wf['positive_folds']}/{len(wf['folds_ic'])}折")
                new_fb.append({"expr": expr, "reason": reason})
                rejected.append({"expr": expr, "reason": reason})
                continue
            r["wf_folds"] = wf["folds_ic"]
        save_factor(expr, r, status="admitted")   # 三道闸全过 → 入池
        admitted.append(r)
    admitted.sort(key=lambda x: -abs(x.get("ic_mean") or 0))
    # feedback 跨轮累积（本轮新失败 + 上轮传入），供下一轮提示回注
    return {"candidates": admitted, "rejected": rejected,
            "feedback": feedback + new_fb, "best": admitted[0] if admitted else None,
            "round_prompt": fb_txt}
