# -*- coding: utf-8 -*-
"""★ Phase29: 前瞻信号记账本 —— 无法回测的信号用实盘日志前瞻验证。

记账对象（收盘后时点记录，次日实现）：
  ① app/global_market.a_share_hint() 触发的映射规则 + 相关 A 股板块代理标的；
  ② app/premarket_news.sector_view() 聚合出的 top 利好/利空行业。
次日实现收益口径：板块 = sector_map.json 成分股等权平均日涨幅（market.db 本地日线，
无外部依赖）；大盘基准 = sector_map 全部成分股等权平均（指数线停更不可靠，见设计文档）。

用法（独立进程，不进服务；建议每交易日晚间运行一次，可挂系统计划任务）：
  python tools/forward_eval.py             # 记账：先结算历史待实现记录，再记今日信号
  python tools/forward_eval.py --report    # 满 60 个交易日输出命中率报告

红线：数据缺失的日子如实留空（error/pending 字段），禁止编造或回填近似值。
"""
import sys
import os
import json
import time
import sqlite3
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C              # noqa: E402
from app import global_market as gm      # noqa: E402  (只 import，不修改)
from app import premarket_news as pn     # noqa: E402  (只 import，不修改)

LEDGER = os.path.join(C.DATA_DIR, "forward_eval.jsonl")
MIN_DAYS_FOR_VERDICT = 60
AI_GRACE_SECONDS = 75        # sector_view 首次 ai_pending 时的等待上限
REPORT_TOP_N = 3             # 每侧最多记级行业数

# hint 规则 → A股板块代理（sector_map.json 的行业词表，49 个传统行业，无半导体/软件独立
# 类目，取最接近的电子/石油类目；dir=flag 的规则无方向，仅记录不判定命中率）
HINT_PROXY = {
    "半导体链": ["电子器件", "电子信息"],
    "港股科技": ["电子信息"],
    "港股科技±1%": ["电子信息"],   # ★ Phase57 实验组（P39：±2% 滤掉有效触发）
    "油气": ["石油行业"],
    "全球避险": [],
    "汇率": [],
}


def _ro_conn():
    return sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=15)


def _load_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
    return out


def _save_jsonl(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _industry_codes(ind_needed):
    """sector_map.json 为系统原生格式 {code: [行业名...]}（app/sector.py 每日刷新）；此处反转为 {行业名: [code...]} 供结算。"""
    p = os.path.join(C.DATA_DIR, "sector_map.json")
    m = json.load(open(p, encoding="utf-8"))
    by_ind = {}
    for code, inds in m.items():
        for nm in (inds or []):
            if ind_needed is None or nm in ind_needed:
                by_ind.setdefault(nm, []).append(code)
    return by_ind, set(m.keys())


_INDUSTRY_VOCAB = None


def _industry_vocab():
    global _INDUSTRY_VOCAB
    if _INDUSTRY_VOCAB is None:
        by_ind, _codes = _industry_codes(None)
        _INDUSTRY_VOCAB = sorted(by_ind)
    return _INDUSTRY_VOCAB


def _stem(nm):
    for suf in ("板块", "行业", "概念"):
        if nm.endswith(suf) and len(nm) > len(suf):
            nm = nm[: -len(suf)]
    return nm


def _match_industry(name):
    """新闻行业名(AI/规则输出) → sector_map 词表名。唯一匹配才返回，歧义返回 None。
    匹配不上不编造：记账保留原名，结算时该行业 pct 如实留空。"""
    if not name:
        return None
    vocab = _industry_vocab()
    n = _stem(name.strip())
    exact = [v for v in vocab if _stem(v) == n]
    if len(exact) == 1:
        return exact[0]
    sub = [v for v in vocab if n and n in _stem(v)]
    if len(sub) == 1:
        return sub[0]
    sup = [v for v in vocab if _stem(v) and _stem(v) in n]
    if len(sup) == 1:
        return sup[0]
    return None


def _pct_map(conn, codes, d0, d1):
    """{code: (close_d0, close_d1)}，两日皆有有效收盘才收录"""
    out = {}
    if not codes:
        return out
    ph = ",".join("?" * len(codes))
    rows = conn.execute(
        "SELECT code, date, close FROM kline WHERE period='day' AND length(code)=6 "
        "AND code IN (%s) AND date IN (?,?)" % ph,
        tuple(codes) + (d0, d1)).fetchall()
    acc = {}
    for c, d, cl in rows:
        if cl is None or cl <= 0:
            continue
        acc.setdefault(c, {})[d] = cl
    for c, dd in acc.items():
        if d0 in dd and d1 in dd:
            out[c] = (dd[d0], dd[d1])
    return out


def _industry_pct(conn, codes_by_ind, d0, d1):
    """({行业: 等权平均涨幅%}, {code: pct%})；成分股缺数据如实少算"""
    pm = _pct_map(conn, sorted({c for cs in codes_by_ind.values() for c in cs}), d0, d1)
    pct_of = {c: (b / a - 1.0) * 100.0 for c, (a, b) in pm.items()}
    res = {}
    for ind, cs in codes_by_ind.items():
        vals = [pct_of[c] for c in cs if c in pct_of]
        res[ind] = round(sum(vals) / len(vals), 4) if vals else None
    return res, pct_of


def _settle(rows):
    """结算所有 realized 为空且次一交易日已有数据的记录。返回 (rows, settled_n)。"""
    conn = _ro_conn()
    try:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM kline WHERE period='day' AND length(code)=6 ORDER BY date")]
        dayset = set(days)
        need_inds = set()
        for r in rows:
            if r.get("realized") is None:
                for h in r.get("hint") or []:
                    need_inds.update(h.get("proxies") or [])
                need_inds.update(x.get("name") for x in (r.get("news_bull") or []))
                need_inds.update(x.get("name") for x in (r.get("news_bear") or []))
        by_ind, all_codes = _industry_codes(need_inds)
        settled = 0
        for r in rows:
            if r.get("realized") is not None:
                continue
            d0 = r.get("date")
            nxt = [d for d in days if d > d0]
            if not nxt:
                continue                      # 次一交易日还没数据，保持待实现
            d1 = nxt[0]
            ind_pct, pct_of = _industry_pct(
                conn, {k: v for k, v in by_ind.items() if k in need_inds}, d0, d1)
            bench_vals = []
            if all_codes:
                pm = _pct_map(conn, sorted(all_codes), d0, d1)
                bench_vals = [(b / a - 1.0) * 100.0 for a, b in pm.values()]
            bench = round(sum(bench_vals) / len(bench_vals), 4) if bench_vals else None

            def _fill(name):
                p = ind_pct.get(name)
                if p is None or bench is None:
                    return {"name": name, "pct": p, "excess": None, "hit": None}
                exc = round(p - bench, 4)
                return {"name": name, "pct": round(p, 4), "excess": exc, "hit": None}

            rl = {"date": d1, "bench_pct": bench, "hint": [], "news_bull": [], "news_bear": []}
            for h in r.get("hint") or []:
                proxies = h.get("proxies") or []
                vals = [_fill(x) for x in proxies]
                vals = [v for v in vals if v["pct"] is not None]
                if h.get("dir") in ("up", "down") and vals:
                    for v in vals:
                        v["hit"] = (v["pct"] > 0) if h["dir"] == "up" else (v["pct"] < 0)
                rl["hint"].append({"rule": h.get("rule"), "dir": h.get("dir"),
                                   "items": vals})
            for key in ("news_bull", "news_bear"):
                for x in r.get(key) or []:
                    v = _fill(x["name"])
                    if v["pct"] is not None and bench is not None:
                        v["hit"] = (v["pct"] > 0) if key == "news_bull" else (v["pct"] < 0)
                    rl[key].append(v)
            r["realized"] = rl
            settled += 1
        return rows, settled
    finally:
        conn.close()


def _collect_signals():
    """收盘后时点采集两类信号。失败/缺失如实留空（error 字段），不编造。"""
    sig = {"hint": [], "news_bull": [], "news_bear": [], "ai_used": False,
           "ai_pending": False, "errors": []}
    # ① 全球市场→A股映射提示
    try:
        for h in gm.a_share_hint() or []:
            sig["hint"].append({"rule": h.get("rule"), "dir": h.get("dir"),
                                "text": h.get("text", ""),
                                "proxies": HINT_PROXY.get(h.get("rule"), [])})
    except Exception as e:
        sig["errors"].append("a_share_hint: %s" % e)
    # ② 盘前新闻 top 利好/利空行业（AI 异步，给一次宽限等待）
    try:
        sv = pn.sector_view() or {}
        deadline = time.time() + AI_GRACE_SECONDS
        while sv.get("ai_pending") and time.time() < deadline:
            time.sleep(10)
            sv = pn.sector_view() or {}
        sig["ai_used"] = bool(sv.get("ai_used"))
        sig["ai_pending"] = bool(sv.get("ai_pending"))
        secs = sv.get("sectors") or []
        bulls = sorted([s for s in secs if s.get("net", 0) > 0],
                       key=lambda x: (-x["net"], -x.get("score", 0)))[:REPORT_TOP_N]
        bears = sorted([s for s in secs if s.get("net", 0) < 0],
                       key=lambda x: (x["net"], -x.get("score", 0)))[:REPORT_TOP_N]
        for s in bulls:
            nm = _match_industry(s["name"])
            sig["news_bull"].append({"name": nm or s["name"], "raw": s["name"],
                                     "matched": bool(nm), "net": s["net"],
                                     "score": s.get("score", 0), "count": s.get("count", 0)})
        for s in bears:
            nm = _match_industry(s["name"])
            sig["news_bear"].append({"name": nm or s["name"], "raw": s["name"],
                                     "matched": bool(nm), "net": s["net"],
                                     "score": s.get("score", 0), "count": s.get("count", 0)})
    except Exception as e:
        sig["errors"].append("sector_view: %s" % e)
    return sig


def cmd_record():
    conn = _ro_conn()
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM kline WHERE period='day' AND length(code)=6").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        print("本地库无日线数据，无法确定交易日锚点")
        return
    today = row[0]
    rows = _load_jsonl(LEDGER)
    if any(r.get("date") == today for r in rows):
        print("[skip] %s 已记账（锚点=本地库最后交易日）" % today)
        return
    rows, settled = _settle(rows)
    sig = _collect_signals()
    rec = {"date": today, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **sig,
           "realized": None}
    rows.append(rec)
    _save_jsonl(LEDGER, rows)
    print("[ok] %s 记账：hint=%d 条、利好行业=%d、利空行业=%d、ai_used=%s、错误=%d；"
          "本次结算历史 %d 条" % (
              today, len(sig["hint"]), len(sig["news_bull"]), len(sig["news_bear"]),
              sig["ai_used"], len(sig["errors"]), settled))


# ---------------- 报告 ----------------

def _verdict(n, hit_rate, avg_excess):
    """事先写死的判定口径（设计文档 §4）"""
    if n < MIN_DAYS_FOR_VERDICT:
        return "样本不足（<60 触发日），继续记账，暂不下结论"
    sig_edge = 1.0 / (n ** 0.5)
    if hit_rate is not None and (hit_rate - 0.5) >= sig_edge and (avg_excess or 0) > 0:
        return "有正向信号迹象（命中率显著高于抛硬币且平均超额为正），可考虑小权重进入打分系统并继续观察"
    if (avg_excess or 0) > 0:
        return "平均超额为正但方向命中率不显著，不建议单独接入打分系统"
    return "无可靠优势，不建议接入打分系统"


def cmd_report():
    rows = _load_jsonl(LEDGER)
    days = sorted({r["date"] for r in rows})
    done = [r for r in rows if r.get("realized")]
    pending = len(rows) - len(done)
    print("=" * 72)
    print("前瞻信号记账报告  记账日=%d  已实现=%d  待实现=%d%s" % (
        len(days), len(done), pending,
        "" if len(days) >= MIN_DAYS_FOR_VERDICT
        else "  ⚠ 未满 %d 交易日，以下为中期观察，结论自动降级" % MIN_DAYS_FOR_VERDICT))
    print("=" * 72)

    def agg(events, label_fn, hit_fn):
        n, hits, exs, gaps = 0, 0, [], []
        for ev in events:
            n += 1
            if ev.get("hit") is not None:
                hits += 1 if ev["hit"] else 0
            if ev.get("excess") is not None:
                exs.append(ev["excess"])
            if ev.get("pct") is not None:
                gaps.append(ev["pct"])
        hr = (hits / n) if n else None
        ae = (sum(exs) / len(exs)) if exs else None
        ap = (sum(gaps) / len(gaps)) if gaps else None
        print("%-28s 触发=%-4d 方向命中=%s  平均超额=%s  平均次日涨跌=%s" % (
            label_fn(), n,
            ("%.1f%% (%d/%d)" % (hr * 100, hits, n)) if hr is not None else "—",
            ("%+.3f%%" % ae) if ae is not None else "—",
            ("%+.3f%%" % ap) if ap is not None else "—"))
        print("%-28s 结论: %s" % ("", _verdict(n, hr, ae)))

    print("\n【① 全球市场→A股映射提示】（按规则；dir=flag 无方向只看超额）")
    by_rule = {}
    for r in done:
        for h in r["realized"].get("hint") or []:
            for it in h.get("items") or []:
                by_rule.setdefault((h["rule"], h["dir"]), []).append(it)
    if not by_rule:
        print("（暂无触发记录）")
    for (rule, dr), evs in sorted(by_rule.items()):
        agg(evs, lambda r=rule, d=dr: "%s [%s]" % (r, d), None)

    print("\n【② 盘前新闻行业】AI筛选=%s" % any(r.get("ai_used") for r in done))
    for key, lab in (("news_bull", "利好行业"), ("news_bear", "利空行业")):
        evs = [it for r in done for it in r["realized"].get(key) or []]
        if not evs:
            print("%s：（暂无记录）" % lab)
            continue
        agg(evs, lambda l=lab: "%s(合计)" % l, None)
        bynm = {}
        for it in evs:
            bynm.setdefault(it["name"], []).append(it)
        for nm, lst in sorted(bynm.items(), key=lambda kv: -len(kv[1]))[:10]:
            agg(lst, lambda n2=nm: "  · %s" % n2, None)

    print("\n口径: 板块=sector_map 成分股等权日涨幅(market.db 本地日线)；"
          "大盘基准=sector_map 全池等权；hit=次日方向与信号方向一致；"
          "超额=板块涨幅-基准涨幅。数据缺失日照实留空。")


def main():
    ap = argparse.ArgumentParser(description="Phase29 前瞻信号记账")
    ap.add_argument("--report", action="store_true", help="输出命中率报告")
    args = ap.parse_args()
    if args.report:
        cmd_report()
    else:
        cmd_record()


if __name__ == "__main__":
    main()
