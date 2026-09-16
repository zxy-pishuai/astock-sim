# -*- coding: utf-8 -*-
"""回测报告导出（v3.9）—— 回测结果存档为 HTML/JSON，支持历史对比
每次回测完成后可保存为报告，Web 端列出历史报告并对比。
纯标准库实现。
"""
import json
import os
import time

from . import config as C

_REPORT_DIR = os.path.join(C.DATA_DIR, "reports")


def _ensure_dir():
    os.makedirs(_REPORT_DIR, exist_ok=True)


def save_report(result, meta):
    """保存回测报告。meta: {strategy, codes, names, start, end, capital, params, mode}
    返回 {id, path, html_path}
    """
    _ensure_dir()
    rid = time.strftime("%Y%m%d_%H%M%S") + "_" + str(int(time.time() * 1000) % 1000)
    doc = {
        "id": rid,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "meta": meta,
        "result": result,
    }
    # JSON 存档
    jpath = os.path.join(_REPORT_DIR, f"{rid}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    # HTML 导出
    hpath = os.path.join(_REPORT_DIR, f"{rid}.html")
    html = _render_html(doc)
    with open(hpath, "w", encoding="utf-8") as f:
        f.write(html)
    return {"id": rid, "path": jpath, "html_path": hpath}


def list_reports(limit=50):
    """历史报告列表（按保存时间倒序）"""
    _ensure_dir()
    out = []
    for fn in sorted(os.listdir(_REPORT_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_REPORT_DIR, fn), "r", encoding="utf-8") as f:
                doc = json.load(f)
            meta = doc.get("meta", {})
            r = doc.get("result", {})
            out.append({
                "id": doc.get("id", fn[:-5]),
                "saved_at": doc.get("saved_at", ""),
                "strategy": meta.get("strategy", ""),
                "mode": meta.get("mode", "normal"),
                "start": meta.get("start", ""),
                "end": meta.get("end", ""),
                "codes": len(meta.get("codes", [])),
                "capital": meta.get("capital", 100000),
                "total_return": r.get("total_return"),
                "sharpe": r.get("sharpe"),
                "max_drawdown": r.get("max_drawdown"),
                "trade_count": r.get("trade_count"),
                "win_rate": r.get("win_rate"),
            })
        except Exception:
            continue
    return out[:limit]


def load_report(rid):
    """读取单份报告"""
    _ensure_dir()
    path = os.path.join(_REPORT_DIR, f"{rid}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compare(report_ids):
    """对比多份报告的关键指标。返回行列表"""
    rows = []
    for rid in report_ids:
        doc = load_report(rid)
        if not doc:
            continue
        meta = doc.get("meta", {})
        r = doc.get("result", {})
        rows.append({
            "id": rid,
            "saved_at": doc.get("saved_at", ""),
            "strategy": meta.get("strategy", ""),
            "mode": meta.get("mode", "normal"),
            "total_return": r.get("total_return"),
            "sharpe": r.get("sharpe"),
            "max_drawdown": r.get("max_drawdown"),
            "win_rate": r.get("win_rate"),
            "trade_count": r.get("trade_count"),
            "params": meta.get("params", {}),
        })
    return rows


def _render_html(doc):
    """渲染单份回测报告 HTML（自包含、可离线查看）"""
    meta = doc.get("meta", {})
    r = doc.get("result", {})
    pa = r.get("performance") or {}
    m = lambda v, d=2: ("—" if v is None else f"{v:.{d}f}")
    pct = lambda v: ("—" if v is None else f"{v*100:.2f}%")
    # 指标卡
    cards = [
        ("总收益", pct(r.get("total_return"))),
        ("年化收益", pct(r.get("annual_return"))),
        ("最大回撤", pct(r.get("max_drawdown"))),
        ("夏普", m(r.get("sharpe"))),
        ("Sortino", m(pa.get("sortino"))),
        ("Calmar", m(pa.get("calmar"))),
        ("胜率", pct(r.get("win_rate"))),
        ("盈亏比", m(r.get("profit_factor"))),
        ("交易笔数", str(r.get("trade_count", 0))),
        ("期末资产", f"¥{r.get('final_equity', 0):,.0f}"),
        ("停牌跳过", str(r.get("suspension_skips", 0))),
        ("未成交", str(r.get("failed_fills", 0))),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in cards)
    # 交易明细
    trades = (r.get("trades") or [])[-50:]
    _rows = []
    for t in trades:
        pnl_txt = "" if t.get("pnl") is None else "{:+,.0f}".format(t["pnl"])
        _rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{:.2f}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                t.get("date", ""), "买" if t.get("side") == "buy" else "卖",
                t.get("code", ""), t.get("name", ""),
                t.get("price", 0), t.get("qty", 0),
                pnl_txt, str(t.get("reason", ""))[:30]))
    rows_html = "".join(_rows)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>回测报告 {doc.get('saved_at','')}</title>
<style>
body{{font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:#0b0e14;color:#d6dde8;padding:24px}}
h1{{font-size:18px}} .meta{{color:#8b96a8;font-size:12px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}}
.card{{background:#151a24;border:1px solid #2c3650;border-radius:8px;padding:10px 14px}}
.k{{color:#8b96a8;font-size:11px}} .v{{font-size:16px;font-weight:600;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}}
th,td{{padding:5px 8px;border-bottom:1px solid #232b3b;text-align:left}}
th{{color:#8b96a8;font-weight:500}}
</style></head><body>
<h1>📊 回测报告</h1>
<div class="meta">保存于 {doc.get('saved_at','')} · 策略 {meta.get('strategy','')} · 模式 {meta.get('mode','')} · 区间 {meta.get('start','')} ~ {meta.get('end','')} · 股票池 {len(meta.get('codes',[]))} 只 · 初始资金 ¥{meta.get('capital',100000):,.0f}</div>
<div class="grid">{cards_html}</div>
<h2 style="font-size:15px">交易明细（最近 {len(trades)} 笔）</h2>
<table><thead><tr><th>日期</th><th>方向</th><th>代码</th><th>名称</th><th>价格</th><th>数量</th><th>盈亏</th><th>原因</th></tr></thead>
<tbody>{rows_html}</tbody></table>
</body></html>"""
