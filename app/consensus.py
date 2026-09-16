# -*- coding: utf-8 -*-
"""★ 4.5 多智能体决策：多源信号投票（借鉴 ai-hedge-fund 分析师分工 / TradingAgents 多空辩论）
不用 LLM 也能拿到"多空博弈"价值的纯标准库方案：
把 6 个独立信号源当作"分析师"，各自独立出多空观点，再合并成共识分与分歧度。

信号源（复用现有模块，零新数据源）：
  1. 技术面分析师（score_stock 评分 → 看多/看空/中性）
  2. 量价分析师   （vp_analysis 量价健康度）
  3. 资金面分析师 （moneyflow_signals：龙虎榜/两融/北向/主力/席位）
  4. 情绪分析师   （sentiment_bonus：市场情绪相位）
  5. 题材分析师   （theme_score：涨停原因题材命中主线）
  6. 业绩分析师   （earnings_signal：业绩预增/预减）

输出：
  consensus(code, klines, quote) → {vote, score, divergence, votes[], verdict}
  - vote: 看多 / 看空 / 分歧（分歧=多空势均力敌，实战提示谨慎）
  - divergence: 0~1 分歧度（0=全体一致，1=完全对立）
  - verdict: 一句话结论
"""
from . import config as C


def _bucket(score):
    """把连续评分桶化成多空观点：>0 看多，<0 看空，=0 中性"""
    if score > 0:
        return "看多", 1
    if score < 0:
        return "看空", -1
    return "中性", 0


def consensus(code, klines=None, quote=None, name=""):
    """多源信号投票。返回 dict 或 None（数据不足）。
    klines: 日K（可选，缺时技术面/量价分析师弃权）。
    quote: 实时行情（可选）。
    """
    votes = []   # [{name, stance, score, detail}]

    # 1) 技术面分析师（评分）
    if klines and len(klines) >= 20:
        try:
            from . import scoring as sc
            score, sigs = sc.score_stock(klines, quote)
            stance, sgn = _bucket(score - C.BUY_SCORE_THRESHOLD)
            votes.append({"name": "技术面", "stance": stance, "score": sgn,
                          "detail": f"{score}分"})
        except Exception:
            pass
    # 2) 量价分析师（量价健康度）
    if klines and len(klines) >= 20:
        try:
            from . import scoring as sc
            verdict, tags = sc.vp_analysis(klines)
            stance = "看多" if any("齐升" in t or "放量" in t or "VWAP上" in t for t in tags) else (
                "看空" if any("背离" in t or "萎缩" in t or "破VWAP" in t for t in tags) else "中性")
            votes.append({"name": "量价", "stance": stance,
                          "score": 1 if stance == "看多" else (-1 if stance == "看空" else 0),
                          "detail": verdict})
        except Exception:
            pass
    # 3) 资金面分析师（龙虎榜/两融/北向/主力/席位）
    if code:
        try:
            from . import scoring as sc
            bonus, sigs = sc.moneyflow_signals(code, name)
            stance, sgn = _bucket(bonus)
            votes.append({"name": "资金面", "stance": stance, "score": sgn,
                          "detail": "、".join(sigs[:2]) or "中性"})
        except Exception:
            pass
    # 4) 情绪分析师（市场相位）
    if code:
        try:
            from . import scoring as sc
            bonus, sigs = sc.sentiment_bonus(code)
            stance, sgn = _bucket(bonus)
            votes.append({"name": "情绪", "stance": stance, "score": sgn,
                          "detail": "、".join(sigs[:2]) or "中性"})
        except Exception:
            pass
    # 5) 题材分析师（涨停原因题材命中）
    if code:
        try:
            from . import scoring as sc
            bonus, sigs = sc.theme_score(code)
            stance, sgn = _bucket(bonus)
            votes.append({"name": "题材", "stance": stance, "score": sgn,
                          "detail": "、".join(sigs[:2]) or "未命中主线"})
        except Exception:
            pass
    # 6) 业绩分析师（预增/预减）
    if code:
        try:
            from . import earnings as ea
            bonus, sigs = ea.earnings_signal(code)
            stance, sgn = _bucket(bonus)
            votes.append({"name": "业绩", "stance": stance, "score": sgn,
                          "detail": "、".join(sigs[:1]) or "无预告"})
        except Exception:
            pass

    if not votes:
        return None

    active = [v for v in votes if v["stance"] != "中性"]
    longs = sum(1 for v in active if v["stance"] == "看多")
    shorts = sum(1 for v in active if v["stance"] == "看空")
    n = len(votes)

    # 净得分（看多+1 / 看空-1 / 中性0）
    net = sum(v["score"] for v in votes)
    # 分歧度：多空对立的程度（1=完全对立，0=全体一致）
    divergence = 0.0
    if active:
        divergence = round(1.0 - abs(longs - shorts) / len(active), 2)

    if net > 0:
        vote = "看多"
    elif net < 0:
        vote = "看空"
    else:
        vote = "分歧"

    # 结论
    if vote == "分歧":
        verdict = (f"多空分歧({longs}多 vs {shorts}空)，市场博弈激烈，"
                   f"谨慎或等方向明确再动" if active else "信号中性，观望")
    elif vote == "看多":
        verdict = (f"{net}路信号看多({longs}多{shorts}空)，"
                   f"{'分歧度' + str(divergence) + '提示仍有博弈' if divergence >= 0.5 else '方向较一致'}")
    else:
        verdict = (f"{abs(net)}路信号看空({longs}多{shorts}空)，"
                   f"{'注意超跌反弹机会' if divergence >= 0.5 else '风险为主'}")

    return {
        "vote": vote,
        "net_score": net,
        "longs": longs,
        "shorts": shorts,
        "neutral": n - longs - shorts,
        "divergence": divergence,
        "votes": votes,
        "verdict": verdict,
    }
