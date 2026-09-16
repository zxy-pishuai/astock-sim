# -*- coding: utf-8 -*-
"""★ 4.5 AI 智能模块（接入 Mimo V2.5 / DeepSeek / Ollama）—— 借鉴 TradingAgents / ai-hedge-fund / xuanji
把散落的指标/信号交给大模型，产出"看得懂、讲得清"的实战结论。

功能：
  1. research_meeting(code)    多智能体投研会议：技术/资金/情绪/题材分析师各自分析 → 共识结论
  2. morning_brief()           盘前晨报：情绪周期+涨停梯队+资金流向+业绩榜 → 今日作战计划
  3. stock_qa(code, question)  个股 AI 问诊：结合分时/量价/资金面回答任意问题
  4. portfolio_health()        持仓组合 AI 体检：行业集中/仓位/情绪匹配度

原则：
  - 所有调用后台线程，绝不阻塞交易轮询
  - LLM 挂了自动降级为本地结构化输出
  - 纯标准库（urllib），无第三方依赖
"""
import json
import threading
import time

from . import config as C
from . import datafeed as df
from . import state as st


def _chat(system, prompt, max_tokens=4000, timeout=None):
    """统一 LLM 调用（Mimo/OpenAI 兼容/Ollama）。失败返回 None
    ★ 4.7：兼容推理模型（如 mimo-v2.5）——思考过程会消耗 max_tokens，
    若 content 为空（预算被 reasoning 耗尽）自动按 3 倍预算重试一次。"""
    try:
        cfg = st.load_llm_config()
        if cfg.get("ollama_model"):
            from . import review as rv
            return rv._ollama_chat(cfg["ollama_model"], prompt,
                                   cfg.get("ollama_url", "http://127.0.0.1:11434"))
        if cfg.get("api_key") and cfg.get("model"):
            import urllib.request
            url = cfg["api_base"].rstrip("/") + "/chat/completions"
            for budget in (max_tokens, max_tokens * 3):
                body = json.dumps({
                    "model": cfg["model"],
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": prompt}],
                    "max_tokens": budget,
                }).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + cfg["api_key"]})
                with urllib.request.urlopen(req, timeout=timeout or C.LLM_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                content = msg.get("content") or ""
                if content.strip():
                    return content
                # content 为空：推理模型可能把预算全耗在 reasoning_content 上 → 加预算重试
            return None
    except Exception:
        return None
    return None


def _quote_of(code):
    try:
        q = df.fetch_quotes([code])
        return q.get(code, {})
    except Exception:
        return {}


# ==================== 1. 多智能体投研会议 ====================
def research_meeting(code, name=""):
    """多智能体投研会议：各分析师独立分析 → 合并成共识。
    返回 {ok, report, provider}。失败降级为本地共识投票结果。
    """
    try:
        from . import scoring as sc
        from . import consensus as cs
        q = _quote_of(code)
        klines = df.fetch_kline(code, "day", 120) if q else []
        # 本地数据收集（供 LLM 分析）
        tech = ""
        if klines:
            score, sigs = sc.score_stock(klines, q, code=code)
            tech = f"评分{score}分；信号: {'、'.join(sigs[:4])}"
        fund = ""
        try:
            fb, fsig = sc.moneyflow_signals(code, name)
            fund = "、".join(fsig[:3]) or "中性"
        except Exception:
            pass
        senti = ""
        try:
            sb, ssig = sc.sentiment_bonus(code)
            senti = "、".join(ssig[:3]) or "中性"
        except Exception:
            pass
        theme = ""
        try:
            tb, tsig = sc.theme_score(code)
            theme = "、".join(tsig[:2]) or "未命中主线"
        except Exception:
            pass
        vp = ""
        if klines:
            verdict, tags = sc.vp_analysis(klines)
            vp = verdict or ""
        pct = q.get("pct_chg", 0) or 0
        price = q.get("price", 0) or 0
        volr = q.get("vol_ratio", 0) or 0

        system = ("你是一名严谨的A股短线投研团队负责人，综合多位分析师观点后给出"
                  "多空结论与仓位建议。输出简洁中文，300字内。")
        prompt = (
            f"【股票】{name}({code}) 现价{price:.2f} 涨跌{pct:+.1f}% 量比{volr:.1f}\n"
            f"【技术面】{tech}\n"
            f"【量价面】{vp}\n"
            f"【资金面】{fund}\n"
            f"【情绪面】{senti}\n"
            f"【题材面】{theme}\n\n"
            f"请以投研会议纪要形式输出：1) 多空结论（看多/看空/观望）；2) 关键依据；"
            f"3) 风险点；4) 操作建议（打板/低吸/放弃）。")
        report = _chat(system, prompt)
        if report:
            return {"ok": True, "report": report.strip(), "provider": "llm"}
        # 降级：本地共识投票
        con = cs.consensus(code, klines=klines, quote=q, name=name)
        if con:
            votes_txt = "；".join(f"{v['name']}:{v['stance']}" for v in con["votes"])
            return {"ok": True,
                    "report": f"[本地共识] {con['verdict']}\n投票明细：{votes_txt}",
                    "provider": "local"}
        return {"ok": False, "report": "数据不足", "provider": "none"}
    except Exception as e:
        return {"ok": False, "report": f"分析失败: {e}", "provider": "none"}


# ==================== 2. 盘前晨报 ====================
def morning_brief():
    """盘前晨报：情绪周期+梯队+题材+资金+业绩 → 今日作战计划。300-400字"""
    try:
        from . import sentiment as senti
        from . import limitup as lu
        from . import scoring as sc
        s = senti.cached_sentiment(max_age=300)
        phase = s.get("phase", "未知")
        zt = s.get("zt_count", 0)
        dt = s.get("dt_count", 0)
        md = s.get("max_days", 0)
        zhaban = s.get("zhaban_rate", 0)
        yz = s.get("yesterday") or {}
        yz_ret = yz.get("avg_ret_pct")
        themes = s.get("themes") or {}
        theme_txt = "、".join(f"{k}×{v}" for k, v in list(themes.items())[:5]) or "无"
        ladder = s.get("ladder") or {}
        ladder_txt = "、".join(f"{k}板×{v}" for k, v in sorted(ladder.items(), reverse=True)[:4]) or "无"
        # 业绩预增榜
        earn_txt = ""
        try:
            from . import earnings as ea
            board = ea.earnings_board(days=7)
            earn_txt = "、".join(f"{b['name']}+{b.get('amp_lower') or 0}%" for b in board[:5]) or "无"
        except Exception:
            pass
        # ★ 4.5：新闻情绪（场外情绪）
        news_txt = ""
        try:
            from . import news as nmod
            ns = nmod.market_sentiment()
            news_txt = f"新闻情绪:{ns['trend']}(分{ns['score']},利好{ns['bull_count']}利空{ns['bear_count']})"
            if ns.get("top_news"):
                news_txt += " 头条:" + ns["top_news"][0]["title"][:40]
        except Exception:
            pass
        # ★ 4.5：资金聚焦（封板资金）
        fund_txt = ""
        try:
            from . import limitup as lu
            ff = lu.fund_focus()
            if ff.get("top_seals"):
                seals = "、".join(f"{s['name']}封{s['fund']/1e7:.0f}千万" for s in ff["top_seals"][:4])
                fund_txt = f"封板资金聚焦:{seals}"
        except Exception:
            pass
        # 昨涨停溢价
        yz_txt = f"{yz_ret:+.2f}%" if yz_ret is not None else "未知"
        idx_txt = ""
        try:
            idxs = df.fetch_indices()
            idx_txt = "、".join(f"{i['name']}{i.get('pct_chg',0):+.2f}%" for i in idxs) or "无"
        except Exception:
            pass

        system = ("你是一名A股超短交易员的首席策略官，擅长情绪周期与题材轮动。"
                  "输出简洁中文盘前作战计划，400字内，含明确的操作倾向。")
        prompt = (
            f"【市场情绪】相位={phase} 情绪分={s.get('score')} 涨停{zt}家/跌停{dt}家 "
            f"空间板{md}板 炸板率{zhaban*100:.0f}% 昨涨停溢价{yz_txt}\n"
            f"【连板梯队】{ladder_txt}\n"
            f"【题材爆发】{theme_txt}\n"
            f"【指数】{idx_txt}\n"
            f"【业绩预增】{earn_txt}\n"
            f"【新闻情绪】{news_txt}\n"
            f"【资金聚焦】{fund_txt}\n\n"
            f"请给出今日盘前作战计划：1) 今日情绪判断与总体策略（进攻/防守/观望）；"
            f"2) 重点关注的题材方向；3) 操作纪律提醒（仓位/打板/止损）。")
        report = _chat(system, prompt)
        if report:
            return {"ok": True, "date": time.strftime("%Y-%m-%d"), "report": report.strip(),
                    "provider": "llm"}
        return {"ok": True, "date": time.strftime("%Y-%m-%d"),
                "report": (f"[本地晨报] 相位:{phase} 涨停{zt}/跌停{dt} 空间板{md}板 "
                           f"炸板率{zhaban*100:.0f}% 昨涨停溢价{yz_txt}\n"
                           f"题材:{theme_txt}\n业绩预增:{earn_txt}"),
                "provider": "local"}
    except Exception as e:
        return {"ok": False, "date": time.strftime("%Y-%m-%d"),
                "report": f"晨报生成失败: {e}", "provider": "none"}


# ==================== 3. 个股 AI 问诊 ====================
def stock_qa(code, question, name=""):
    """个股 AI 问诊：结合实时数据回答任意问题（为什么跌/能买吗/怎么看）。"""
    try:
        from . import scoring as sc
        from . import minute_vol as mv
        q = _quote_of(code)
        klines = df.fetch_kline(code, "day", 120) if q else []
        price = q.get("price", 0) or 0
        pct = q.get("pct_chg", 0) or 0
        volr = q.get("vol_ratio", 0) or 0
        turnover = q.get("turnover", 0) or 0
        tech = vp = ""
        if klines:
            score, sigs = sc.score_stock(klines, q, code=code)
            tech = f"评分{score}分({'、'.join(sigs[:3])})"
            verdict, _ = sc.vp_analysis(klines)
            vp = verdict or ""
        mv_txt = ""
        try:
            ratio, verdict2 = mv.minute_vol_check(code, q)
            if ratio:
                mv_txt = f"分时量比{ratio:.2f}({verdict2})"
        except Exception:
            pass
        fund = ""
        try:
            _, fsig = sc.moneyflow_signals(code, name)
            fund = "、".join(fsig[:3])
        except Exception:
            pass
        system = ("你是一名专业的A股投资顾问，回答要简洁、客观、可执行，"
                  "注明风险，不承诺收益。150字内。")
        prompt = (
            f"【股票】{name}({code}) 现价{price:.2f} 涨跌{pct:+.1f}% 量比{volr:.1f} 换手{turnover:.1f}%\n"
            f"【技术面】{tech}\n【量价面】{vp}\n【分时量】{mv_txt}\n【资金面】{fund}\n\n"
            f"用户提问：{question}\n请回答。")
        report = _chat(system, prompt)
        if report:
            return {"ok": True, "report": report.strip(), "provider": "llm"}
        return {"ok": True,
                "report": f"[本地数据] 现价{price:.2f} {pct:+.1f}% 量比{volr:.1f} | {tech} | {vp}",
                "provider": "local"}
    except Exception as e:
        return {"ok": False, "report": f"问诊失败: {e}", "provider": "none"}


# ==================== 4. 持仓组合 AI 体检 ====================
def portfolio_health():
    """持仓组合体检：行业集中度/单票仓位/情绪匹配 → 组合级建议。"""
    try:
        acct = st.load_account()
        positions = acct.get("positions", {})
        if not positions:
            return {"ok": True, "report": "当前空仓，无需体检。", "provider": "local"}
        quotes = df.fetch_quotes(list(positions.keys()))
        rows = []
        total_mv = 0.0
        for code, p in positions.items():
            q = quotes.get(code, {})
            cp = q.get("price", 0) or p["entry_price"]
            mv = cp * p["qty"]
            total_mv += mv
            rows.append(f"{p['name']}({code}) 市值{mv:,.0f} 盈亏{(cp-p['entry_price'])/p['entry_price']*100:+.1f}%")
        # 情绪匹配
        senti_txt = ""
        try:
            from . import sentiment as senti
            s = senti.cached_sentiment(max_age=300)
            senti_txt = f"当前情绪相位:{s.get('phase')}（开仓{s.get('open')}）"
        except Exception:
            pass
        system = ("你是一名组合风控顾问，检查持仓结构风险并给出调整建议。200字内。")
        prompt = (
            f"【持仓】\n" + "\n".join(rows) + f"\n总市值¥{total_mv:,.0f}\n"
            f"【情绪环境】{senti_txt}\n\n"
            f"请检查：1) 行业/题材是否过度集中；2) 单票仓位风险；3) 与当前情绪是否匹配；"
            f"4) 调整建议。")
        report = _chat(system, prompt)
        if report:
            return {"ok": True, "report": report.strip(), "provider": "llm",
                    "count": len(positions)}
        return {"ok": True,
                "report": "[本地体检] 持仓" + str(len(positions)) + "只\n" + "\n".join(rows),
                "provider": "local", "count": len(positions)}
    except Exception as e:
        return {"ok": False, "report": f"体检失败: {e}", "provider": "none"}
