# -*- coding: utf-8 -*-
"""LLM 辅助复盘（v3.5）—— 借鉴 xuanji AI 复盘闭环 / trading-review-wiki 交割单复盘
导出"当日复盘包"（交易流水+持仓+盈亏+板块表现+市场背景），
用标准库 urllib 调 LLM API（Ollama 本地 / OpenAI 兼容 / Server酱无关）生成自然语言复盘报告。
保持零第三方依赖。
"""
import json
import os
import time
import urllib.parse
import urllib.request

from . import config as C
from . import datafeed as df
from . import state as st


def build_review_pack(day=None):
    """收集当日复盘数据包（纯数据，不含 LLM）"""
    day = day or time.strftime("%Y-%m-%d")
    acct = st.load_account()
    trades = [t for t in acct.get("trades", [])
              if t.get("time", "").startswith(day)]
    # 持仓快照
    positions = acct.get("positions", {})
    pos_rows = []
    mv = 0.0
    codes = list(positions.keys())
    quotes = df.fetch_quotes(codes) if codes else {}
    for code, p in positions.items():
        q = quotes.get(code, {})
        cp = q.get("price", 0) or p["entry_price"]
        pnl = (cp - p["entry_price"]) * p["qty"]
        mv += cp * p["qty"]
        pos_rows.append({
            "code": code, "name": p.get("name", code),
            "qty": p["qty"], "entry_price": p["entry_price"], "price": cp,
            "pnl": round(pnl, 2),
            "pnl_pct": round((cp / p["entry_price"] - 1) * 100, 2) if p["entry_price"] else 0,
            "days": p.get("days", 0),
            "board_trade": bool(p.get("board_trade", False)),
        })
    # 当日交易盈亏合计
    day_pnl = sum((t.get("pnl") or 0) for t in trades if t.get("pnl") is not None)
    total = acct.get("cash", 0) + mv
    # 市场背景
    market = {"indices": [], "breadth": None}
    try:
        idx = df.fetch_indices()
        market["indices"] = [{"name": i["name"], "price": i.get("price"),
                              "pct_chg": i.get("pct_chg")} for i in idx]
    except Exception:
        pass
    try:
        lst = df.get_stock_list()
        codes = [c for c, _, _ in lst[:600]]
        quotes = df.fetch_quotes(codes)
        up = sum(1 for q in quotes.values() if (q.get("pct_chg") or 0) > 0)
        market["breadth"] = round(up / len(quotes), 3) if quotes else None
    except Exception:
        pass

    pack = {
        "day": day,
        "account": {
            "cash": round(acct.get("cash", 0), 2),
            "mv": round(mv, 2),
            "total": round(total, 2),
            "day_pnl": round(day_pnl, 2),
        },
        "trades": trades,
        "positions": pos_rows,
        "market": market,
    }
    return pack


def _ollama_chat(model, prompt, base_url="http://127.0.0.1:11434"):
    """调本地 Ollama（/api/chat，keep-alive）"""
    url = base_url.rstrip("/") + "/api/chat"
    body = json.dumps({"model": model, "messages": [
        {"role": "system", "content": "你是一名严谨的 A 股短线交易复盘助手，输出简洁中文复盘。"},
        {"role": "user", "content": prompt},
    ], "stream": False}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=C.LLM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("message", {}).get("content", "")


def _openai_chat(api_key, model, prompt, base_url="https://api.deepseek.com/v1"):
    """调 OpenAI 兼容接口（DeepSeek/OpenAI/通义等）"""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "messages": [
        {"role": "system", "content": "你是一名严谨的 A 股短线交易复盘助手，输出简洁中文复盘。"},
        {"role": "user", "content": prompt},
    ]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=C.LLM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def generate_review(day=None, save=True):
    """生成复盘报告。
    配置优先级：Ollama（本地）> OpenAI 兼容 API（llm_api_key+llm_model）。
    返回 {"ok": bool, "report": str, "provider": str}
    """
    pack = build_review_pack(day)
    day = pack["day"]
    trades = pack["trades"]
    sells = [t for t in trades if t.get("side") == "sell"]
    buys = [t for t in trades if t.get("side") == "buy"]
    wins = [t for t in sells if (t.get("pnl") or 0) > 0]
    summary_lines = [
        f"当日共 {len(trades)} 笔交易（买{len(buys)}/卖{len(sells)}），已实现盈亏 ¥{pack['account']['day_pnl']:+,.0f}",
    ]
    for t in trades[-15:]:
        pnl = "" if t.get("pnl") is None else f" 盈亏{t['pnl']:+,.0f}"
        summary_lines.append(f"- {t.get('time','')} {t.get('side')} {t.get('name','')}({t.get('code','')}) "
                             f"{t.get('price',0):.2f}x{t.get('qty',0)}{pnl} 原因:{t.get('reason','')}")
    idx_lines = "、".join(f"{i['name']}{i.get('pct_chg',0):+.2f}%" for i in pack["market"]["indices"]) or "无"
    pos_lines = "；".join(f"{p['name']}({p['code']}) {p['pnl_pct']:+.1f}%" for p in pack["positions"]) or "空仓"
    prompt = (
        f"【日期】{day}\n"
        f"【账户】总资产 ¥{pack['account']['total']:,.0f}，当日已实现盈亏 ¥{pack['account']['day_pnl']:+,.0f}\n"
        f"【指数】{idx_lines}\n"
        f"【当日交易】\n" + "\n".join(summary_lines[1:]) + "\n"
        f"【当前持仓】{pos_lines}\n\n"
        f"请给出：1) 当日操作总结与盈亏归因；2) 买卖时点、止损纪律的执行质量评价；"
        f"3) 明日注意事项与改进点。控制在 300 字内。"
    )
    cfg = st.load_llm_config()
    provider = ""
    report = ""
    # 1) 本地 Ollama
    if cfg.get("ollama_model"):
        try:
            report = _ollama_chat(cfg["ollama_model"], prompt,
                                  cfg.get("ollama_url", "http://127.0.0.1:11434"))
            provider = "ollama"
        except Exception:
            report = ""
    # 2) OpenAI 兼容 API
    if not report and cfg.get("api_key") and cfg.get("model"):
        try:
            report = _openai_chat(cfg["api_key"], cfg["model"], prompt,
                                  cfg.get("api_base", "https://api.deepseek.com/v1"))
            provider = "api"
        except Exception as e:
            report = ""
            err = str(e)
    if not report:
        # 3) 无 LLM：本地结构化总结（不依赖外部）
        report = ("（未配置 LLM，输出结构化复盘）\n" + "\n".join(summary_lines) +
                  f"\n当前持仓：{pos_lines}")
        provider = "local"
    if save:
        try:
            out_dir = os.path.join(C.DATA_DIR, "reviews")
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, f"review_{day}.md"), "w", encoding="utf-8") as f:
                f.write(f"# 复盘 {day}（{provider}）\n\n{report}\n")
        except Exception:
            pass
    return {"ok": True, "report": report, "provider": provider,
            "day": day, "pack": pack}
