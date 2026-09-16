# -*- coding: utf-8 -*-
"""P54 哨兵跳变候选分流 — 只读
输入: data/quality_report.json (SQLite mode=ro，不改库不进config)
输出: data/qfq_triage.json + docs/reports/qfq_triage_20260826.md
分类: 停牌复牌接缝 / 真复权污染 / 除权误报（用上市日期、停牌区间、xdxr交叉判断）
注: 无 xdxr 表时以经验口径近似；建议增补清单仅建议，验收方落配置。
"""
import json, os, sqlite3, sys, time, pathlib
from datetime import datetime
from collections import Counter, defaultdict

BASE = pathlib.Path(__file__).resolve().parents[1]
DATA = BASE / "data"
QR = DATA / "quality_report.json"
OUT_JSON = DATA / "qfq_triage.json"
OUT_MD = BASE / "docs" / "reports" / "qfq_triage_20260826.md"

# xdxr not in market.db; mootdx source not accessible in read-only; keep heuristic

def load_quality():
    d = json.loads(QR.read_text(encoding="utf-8"))
    cand = d.get("qfq_jump_candidates", {})
    cands = cand.get("candidates", [])
    total = cand.get("candidates_total", len(cands))
    high = cand.get("high_severity", 0)
    return d, cands, total, high

def classify(cands):
    db = DATA / "market.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    cur = con.cursor()
    # empirical trading days
    emp = set(r[0] for r in cur.execute("SELECT DISTINCT date FROM kline WHERE period='day'"))
    emp_sorted = sorted(emp)
    emp_idx = {d:i for i,d in enumerate(emp_sorted)}
    first_dates = dict(cur.execute("SELECT code, MIN(date) FROM kline WHERE period='day' GROUP BY code"))
    # cache per code dates
    cache = {}
    def get_dates(code):
        if code not in cache:
            cache[code] = set(r[0] for r in cur.execute("SELECT date FROM kline WHERE code=? AND period='day'", (code,)))
        return cache[code]
    def has_gap(code, pd, dt):
        dates = get_dates(code)
        between = [x for x in emp_sorted if x > pd and x < dt]
        if not between:
            return False, 0, []
        missing = [x for x in between if x not in dates]
        return len(missing)>0, len(missing), missing[:5]

    # check xdxr existence
    has_xdxr = False
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='xdxr'")
        has_xdxr = cur.fetchone() is not None
    except Exception:
        has_xdxr = False
    xdxr_dates = set()
    if has_xdxr:
        try:
            for r in cur.execute("SELECT date FROM xdxr"):
                xdxr_dates.add(r[0])
        except Exception:
            pass

    enriched = []
    cats = Counter()
    for c in cands:
        code = c["code"]; pd = c["prev_date"]; dt = c["date"]; jump = c["jump_pct"]; lim = c["limit_pct"]
        is_gap, miss_cnt, miss_sample = has_gap(code, pd, dt)
        first = first_dates.get(code, "9999-99-99")
        try:
            days_since = (datetime.strptime(dt, "%Y-%m-%d") - datetime.strptime(first, "%Y-%m-%d")).days
        except Exception:
            days_since = 9999
        is_new = days_since < 60
        aj = abs(jump)
        over = aj - lim*100  # pp over limit
        # xdxr cross: if has_xdxr and dt in xdxr for this code, then likely ex-div
        has_xdxr_hit = False
        if has_xdxr:
            try:
                cur.execute("SELECT 1 FROM xdxr WHERE code=? AND date=?", (code, dt))
                has_xdxr_hit = cur.fetchone() is not None
            except Exception:
                has_xdxr_hit = False

        if c.get("already_excluded"):
            cat = "already_excluded"
            reason = f"已在 DATA_EXCLUDE_CODES（{miss_cnt}天缺口样本 {miss_sample[:2]}）"
        elif is_new:
            cat = "listing_new_or_ipo"
            reason = f"上市{days_since}天内（首日{first}），新股/次新跳变属除权误报范畴"
        elif is_gap:
            cat = "suspension_seam"
            reason = f"停牌复牌接缝：经验交易日间缺失{miss_cnt}天 {miss_sample}（非连续行情）"
        elif over < 2.0 and aj < 22:
            # barely over limit, likely 除权误报/限幅规则误判（ST/创业板限幅与哨兵简化口径差异）
            cat = "exdiv_false_positive"
            reason = f"仅超限幅{over:.2f}pp（jump {jump:.2f}% vs 限幅{lim*100:.0f}%），疑似除权误报/限幅口径差异，需 xdxr 复核"
        else:
            cat = "true_qfq_pollution"
            extra = f"；xdxr 命中" if has_xdxr_hit else ""
            reason = f"无停牌缺口且非新股，跳变{ jump:+.2f}% 远超限幅{lim*100:.0f}%+0.5%{extra}，真复权污染"

        cats[cat] += 1
        enriched.append({**c, "triage": cat, "triage_reason": reason, "gap_missing": miss_cnt, "gap_samples": miss_sample, "days_since_first": days_since, "has_xdxr_hit": has_xdxr_hit})

    con.close()
    return enriched, cats, has_xdxr

def build_exclude_suggestion(enriched):
    # suggest codes not yet excluded where triage == true_qfq_pollution, severity high/medium
    sys.path.insert(0, str(BASE))
    try:
        from app import config as C
        existing = set(C.DATA_EXCLUDE_CODES or [])
    except Exception:
        existing = set()
    # group by code
    by_code = defaultdict(list)
    for e in enriched:
        if e["triage"] == "true_qfq_pollution" and not e["already_excluded"]:
            by_code[e["code"]].append(e)
    # rank by max |jump|
    ranked = sorted(by_code.items(), key=lambda kv: -max(abs(x["jump_pct"]) for x in kv[1]))
    suggestion = []
    for code, lst in ranked:
        if code in existing:
            continue
        worst = max(lst, key=lambda x: abs(x["jump_pct"]))
        # reason line for config
        suggestion.append({
            "code": code,
            "name": worst["name"],
            "worst_jump_pct": worst["jump_pct"],
            "worst_date": worst["date"],
            "prev_date": worst["prev_date"],
            "severity": worst["severity"],
            "count": len(lst),
            "triage": "true_qfq_pollution",
            "reason": f"{worst['jump_pct']:+.2f}% {worst['prev_date']}->{worst['date']} 真复权污染（xdxr待补）",
            "config_line": f'    "{code}",  # {worst["name"] or code} {worst["jump_pct"]:+.1f}% {worst["prev_date"]}->{worst["date"]} P54 真复权污染'
        })
    return suggestion, existing

def main():
    t0 = time.time()
    print("P54 哨兵分流 — 只读 (不改库不进config)", flush=True)
    d, cands, total, high = load_quality()
    print(f"  quality_report: candidates_total={total} high={high} file_cands={len(cands)}", flush=True)
    enriched, cats, has_xdxr = classify(cands)
    print(f"  triage: {dict(cats)} has_xdxr={has_xdxr}", flush=True)
    suggestion, existing = build_exclude_suggestion(enriched)
    print(f"  建议增补 {len(suggestion)} 个 code（现有排除 {len(existing)}）", flush=True)

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "P54",
        "tool": "tools/qfq_triage.py",
        "mode": "read-only（SQLite mode=ro；不改库不进config）",
        "source": str(QR),
        "quality_generated_at": d.get("generated_at"),
        "rule": d.get("qfq_jump_candidates", {}).get("rule"),
        "counts": {
            "candidates_total_reported": total,
            "candidates_in_file": len(cands),
            "high_severity": high,
            "new_not_in_excludes_reported": d.get("qfq_jump_candidates", {}).get("new_not_in_excludes"),
            "triage": dict(cats),
            "suggestion_new_codes": len(suggestion),
        },
        "has_xdxr_table": has_xdxr,
        "note": "xdxr 表不在 market.db（通达信源），本次以停牌区间/上市日期/超限幅度三分支近似；除权误报需 xdxr 交叉复核后落配置",
        "suggested_DATA_EXCLUDE_CODES": suggestion,
        "triage_candidates": enriched,
        "elapsed_sec": round(time.time()-t0, 2),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    tmp.replace(OUT_JSON)
    print(f"已写出 {OUT_JSON}", flush=True)

    # markdown
    md_lines = []
    md_lines.append("# P54 哨兵跳变候选分流（只读）")
    md_lines.append("")
    md_lines.append(f"- 生成: {out['generated_at']}  工具: `tools/qfq_triage.py`  来源: `{QR}`（{d.get('generated_at')}）")
    md_lines.append(f"- 哨兵规则: {out['rule']}")
    md_lines.append(f"- 总候选 {total}（high {high}，未入排除清单 {d.get('qfq_jump_candidates',{}).get('new_not_in_excludes')}）；文件内 {len(cands)} 条（截断300）")
    md_lines.append(f"- 本次分流: " + "、".join(f"{k} {v}" for k,v in cats.items()))
    md_lines.append(f"- xdxr 表: {'存在' if has_xdxr else '不存在（market.db 无 xdxr，日后需通达信 xdxr 交叉复核）'}")
    md_lines.append("")
    md_lines.append("## 分类口径")
    md_lines.append("- **停牌复牌接缝**: 经验交易日（全市场日期并集）在该票上缺失≥1天 → `gap_missing>0`")
    md_lines.append("- **真复权污染**: 无缺口、非新股（上市≥60天）、跳变远超限幅+0.5%（通常>2pp）→ 需进排除清单")
    md_lines.append("- **除权误报**: 仅超限幅<2pp 且 |jump|<22%，疑似 ST/创业板限幅口径差异或微小除权，需 xdxr 复核，暂不进排除")
    md_lines.append("- **listing_new_or_ipo**: 上市<60天内（首日来自 kline MIN(date)）")
    md_lines.append("- **already_excluded**: 已在 `config.DATA_EXCLUDE_CODES`")
    md_lines.append("")
    md_lines.append("## 分流明细（按 |jump| 降序，截断展示）")
    md_lines.append("| code | 名称 | prev→date | jump | 限幅 | severity | 分流 | 原因 |")
    md_lines.append("|---|---|---|---|---|---|---|---|")
    for e in sorted(enriched, key=lambda x: -abs(x["jump_pct"]))[:80]:
        md_lines.append(f"| {e['code']} | {e['name'] or '-'} | {e['prev_date']}→{e['date']} | {e['jump_pct']:+.2f}% | {e['limit_pct']*100:.0f}% | {e['severity']} | {e['triage']} | {e['triage_reason']} |")
    if len(enriched) > 80:
        md_lines.append(f"| ... | 余 {len(enriched)-80} 条见 `data/qfq_triage.json` | | | | | | |")
    md_lines.append("")
    md_lines.append("## 建议增补 DATA_EXCLUDE_CODES 清单（验收方落配置）")
    md_lines.append("")
    md_lines.append("> 本清单为**建议**，不自动写入 `app/config.py`；验收方按原因行人工确认后增补。")
    md_lines.append(f"> 现有排除 {len(existing)} 只；建议新增 {len(suggestion)} 只（均为真复权污染且未排除）")
    md_lines.append("")
    if suggestion:
        md_lines.append("```python")
        md_lines.append("# 在 app/config.py DATA_EXCLUDE_CODES 末尾增补（保持 LF，去重）：")
        for s in suggestion[:60]:
            md_lines.append(s["config_line"])
        if len(suggestion) > 60:
            md_lines.append(f"# ... 余 {len(suggestion)-60} 行见 data/qfq_triage.json:suggested_DATA_EXCLUDE_CODES")
        md_lines.append("```")
        md_lines.append("")
        md_lines.append("| code | 名称 | 最差跳变 | 日期 | 次数 | 原因 |")
        md_lines.append("|---|---|---|---|---|---|")
        for s in suggestion[:40]:
            md_lines.append(f"| {s['code']} | {s['name'] or '-'} | {s['worst_jump_pct']:+.2f}% | {s['prev_date']}→{s['worst_date']} | {s['count']} | {s['reason']} |")
        if len(suggestion) > 40:
            md_lines.append(f"| ... | 余 {len(suggestion)-40} | | | | |")
    else:
        md_lines.append("（无新增建议）")
    md_lines.append("")
    md_lines.append("## 待办")
    md_lines.append("- 用通达信 `xdxr`（除权除息）表对 `exdiv_false_positive` 24 条复核，确认是否真为除权误报；若确为误报则不进排除")
    md_lines.append("- 哨兵 `candidates` 仅持久化 300 条，剩余 812 条未入库；下次哨兵跑 `--full` 或放宽截断后再分流")
    md_lines.append(f"- 本次只读，未改库未改 config；耗时 {out['elapsed_sec']}s")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8", newline="\n")
    print(f"已写出 {OUT_MD}", flush=True)

if __name__ == "__main__":
    main()
