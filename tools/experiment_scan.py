# -*- coding: utf-8 -*-
"""W5: 实验台账索引器 — 扫描 docs/reports + data/bt_*.json + docs/backlog 三源,
合并为 data/experiments_index.json 供 /api/experiments 与前端"实验台账"页消费。

只读扫描（不写任何 bt_*.json / reports），容错解析：
  - bt_*.json schema 不统一 → 尽力提取 verdict/judgement/meta, 失败标 unknown;
  - 半截文件/JSON 解析失败 → 标 running(可能被并发 writer 生成中), 优雅跳过;
  - reports/*.md → 标题 / 报告状态(status) / 结论倾向(conclusion) / 预注册 / 判据 / P编号。
用法: python tools/experiment_scan.py [--out data/experiments_index.json]
"""
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
REPORTS_DIR = os.path.join(BASE, "docs", "reports")
BACKLOG = os.path.join(BASE, "docs", "backlog.md")
OUT_DEFAULT = os.path.join(DATA_DIR, "experiments_index.json")

NOW = time.localtime()
NOW_TS = time.mktime(NOW)
DAY = 86400.0


def _fsize(f):
    try:
        return os.path.getsize(f)
    except Exception:
        return 0


def _fmtime(f):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(f)))
    except Exception:
        return ""


def _fdate(f):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(f)))
    except Exception:
        return ""


def _g(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _snap_short(snap):
    """快照全路径 → YYYY-MM-DD"""
    if not snap:
        return None
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", str(snap))
    return m.group(1) if m else str(snap)[:20]


def _judge_status(verdict):
    """从 verdict/judgement dict 判落地状态: passed/failed/unknown"""
    if not isinstance(verdict, dict):
        return "unknown"
    if verdict.get("enabled") is True:
        return "passed"
    if verdict.get("enabled") is False:
        return "failed"
    if verdict.get("passed") is True:
        return "passed"
    if verdict.get("passed") is False:
        return "failed"
    if verdict.get("pass") is True:
        return "passed"
    if verdict.get("pass") is False:
        return "failed"
    # PIT/偏无偏口径: any_window_gt10pp 提示幸存者偏差 → failed(反哺声明库)
    if verdict.get("any_window_gt10pp") is True:
        return "failed"
    rec = str(verdict.get("recommendation", "") or "")
    if "落地" in rec and "不" not in rec[:8]:
        return "passed"
    if "不建议" in rec or "暂缓" in rec or "不落地" in rec or "否定" in rec:
        return "failed"
    return "unknown"


def _judgement_list_status(jl):
    if not isinstance(jl, list):
        return "unknown", 0, 0
    passed = 0
    total = 0
    for it in jl:
        if not isinstance(it, dict):
            continue
        total += 1
        if it.get("pass") is True or it.get("passed") is True:
            passed += 1
    if total == 0:
        return "unknown", 0, 0
    if passed == total:
        return "passed", passed, total
    if passed == 0:
        return "failed", passed, total
    return "partial", passed, total


def _pit_arr_pass(arr):
    """judgement_pit/static 数组第一个含 pass 的元素; 返回 (pass_bool|None, 摘要)"""
    if not isinstance(arr, list):
        return None, ""
    for it in arr:
        if isinstance(it, dict) and ("pass" in it or "passed" in it):
            p = it.get("pass", it.get("passed"))
            return bool(p), json.dumps(it, ensure_ascii=False)[:200]
    return None, ""


def _pick_key_numbers(d):
    """提取关键数字（windows_ok/bull/delta/overstatement 等），尽力而为"""
    kn = {}
    v = _g(d, "verdict") or _g(d, "judgement")
    if isinstance(v, dict):
        for k in ("windows_ok", "n_windows", "bull_ok", "bull_loss", "bull_delta_pp",
                  "n_windows_gt10pp", "any_window_gt10pp", "recommended", "enabled",
                  "wins_ok", "bull_loss", "pseudo"):
            if v.get(k) is not None:
                kn[k] = v[k]
    jl = d.get("judgement")
    if isinstance(jl, list) and jl:
        status, passed, total = _judgement_list_status(jl)
        if total:
            kn["judge_pass"] = "%d/%d" % (passed, total)
    for col in ("judgement_pit", "judgement_static"):
        arr = d.get(col)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    for k in ("pass", "wins_ok", "bull_loss", "pseudo"):
                        if it.get(k) is not None:
                            tag = "%s_%s" % (col.replace("judgement_", ""), k)
                            if tag not in kn:
                                kn[tag] = it[k]
    # table 型(如 it_snapshot_retest): 提取 delta_pp 摘要
    tb = d.get("table")
    if isinstance(tb, list) and tb and not kn:
        deltas = []
        for row in tb:
            if isinstance(row, dict) and row.get("delta_pp") is not None:
                deltas.append(round(float(row["delta_pp"]), 2))
        if deltas:
            kn["n_cells"] = len(tb)
            kn["delta_min"] = min(deltas)
            kn["delta_max"] = max(deltas)
    for col in ("overstatement", "beautify"):
        v2 = d.get(col)
        if isinstance(v2, dict):
            for k, val in v2.items():
                if isinstance(val, (int, float, str)) and k not in kn:
                    kn[col + "." + k] = val
    return kn


def scan_bt_json():
    rows = {}
    n_err = 0
    if not os.path.isdir(DATA_DIR):
        return rows, 0
    for fn in sorted(os.listdir(DATA_DIR)):
        if not (fn.startswith("bt_") and fn.endswith(".json")):
            continue
        path = os.path.join(DATA_DIR, fn)
        eid = fn[:-5]
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            n_err += 1
            rows[eid] = {
                "id": eid, "type": "bt_json", "name": eid,
                "date": _fdate(path), "mtime": _fmtime(path),
                "snapshot": None, "status": "running",
                "status_hint": "JSON 解析失败（可能并发生成中）",
                "verdict": "", "conclusion": "unknown", "key_numbers": {},
                "p_numbers": [], "source": os.path.join("data", fn),
                "md_path": None, "size": _fsize(path),
            }
            continue
        if not isinstance(d, dict):
            n_err += 1
            continue
        cd, ct = d.get("cells_done"), d.get("cells_total")
        running = isinstance(cd, int) and isinstance(ct, int) and cd < ct

        status = "unknown"
        verdict = d.get("verdict")
        if isinstance(verdict, dict):
            status = _judge_status(verdict)
        elif isinstance(verdict, list):
            st, _, _ = _judgement_list_status(verdict)
            status = st if st != "unknown" else status
        # PIT 判据列优先（P72 模板: pit 为准）
        pit_pass, pit_sum = _pit_arr_pass(d.get("judgement_pit"))
        if pit_pass is not None and status == "unknown":
            status = "passed" if pit_pass else "failed"
        if status == "unknown" and "judgement" in d:
            jl = d.get("judgement")
            if isinstance(jl, dict):
                status = _judge_status(jl)
            elif isinstance(jl, list):
                st, _, _ = _judgement_list_status(jl)
                status = st if st != "unknown" else status
        if running:
            status = "running"

        vtext = ""
        if isinstance(verdict, dict):
            bits = []
            for k in ("rule", "recommendation", "reason", "detail"):
                if verdict.get(k):
                    bits.append(str(verdict[k]))
            vtext = " | ".join(x for x in bits if x)[:300]
        elif isinstance(verdict, list):
            bits = [str(x.get("name", "")) + (":pass=" + str(x.get("pass")) if "pass" in x else "")
                    for x in verdict if isinstance(x, dict)]
            vtext = "; ".join(bits)[:300]
        if not vtext and pit_sum:
            vtext = "PIT判定: " + pit_sum[:150]

        snap = _g(d, "meta", "snapshot") or _g(d, "meta", "snapshot_name") or d.get("snapshot_used")
        gen = d.get("generated_at") or _g(d, "meta", "generated_at") or _fmtime(path)
        rows[eid] = {
            "id": eid, "type": "bt_json", "name": eid,
            "date": (gen or _fdate(path))[:10],
            "mtime": gen or _fmtime(path),
            "snapshot": _snap_short(snap),
            "status": status,
            "status_hint": "cells %s/%s" % (cd, ct) if isinstance(cd, int) else "",
            "verdict": vtext,
            "conclusion": status,
            "key_numbers": _pick_key_numbers(d),
            "p_numbers": [],
            "source": os.path.join("data", fn), "md_path": None,
            "size": _fsize(path),
        }
    return rows, n_err


# ---------------- reports/*.md ----------------

def _report_status(text, title):
    """报告自身状态（完成/pending/unknown），不是结论"""
    # 状态行优先
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("> 状态") or s.startswith("**状态"):
            if "完成" in s or "通过" in s:
                return "passed"
            if "待" in s or "挂起" in s:
                return "pending"
            if "不通过" in s or "否决" in s:
                return "failed"
    if any(k in title for k in ("交付报告", "完成版", "验收报告", "交付")):
        return "passed"
    head = text[:1500]
    if re.search(r"待执行|挂起|待全量|就绪待|⏳|待.*执行", head):
        return "pending"
    return "unknown"


def _report_conclusion(text):
    """报告结论倾向: 从 结论/判定/判据 相关段提取通过与否"""
    # 取含"结论"或"判定"或"通过/不通过"的密集行
    lines = text.splitlines()
    seg = "\n".join(lines)
    # 结论节优先
    m = re.search(r"(结论[^\n]{0,30}|##\s*[0-9A-Z.]*[结]论[^\n]{0,30})", seg)
    if m:
        pass
    probe = text[:6000]
    if re.search(r"不通过|否决|不建议落地|证伪|不具备.*价值|暂缓", probe):
        return "failed"
    if re.search(r"通过|达标|建议落地|可落地|✅ 完成", probe[:4000]):
        return "passed"
    return "unknown"


def scan_reports():
    rows = {}
    if not os.path.isdir(REPORTS_DIR):
        return rows
    for fn in sorted(os.listdir(REPORTS_DIR)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(REPORTS_DIR, fn)
        eid = fn[:-3]
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        lines = text.splitlines()
        title = next((l[2:].strip() for l in lines if l.startswith("# ")), eid)
        prereg = ""
        m = re.search(r"预注册[^\n]{0,120}?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})", text)
        if m:
            prereg = m.group(1)
        elif re.search(r"预注册先于评估", text):
            prereg = "（预注册先于评估）"
        judge_line = ""
        for line in lines:
            if re.search(r"判据|≥3/4|3/4 ?窗|loss|windows_ok|landing_rule|PIT", line):
                judge_line = line.strip()[:180]
                break
        pnums = sorted(set(re.findall(r"P\d+", text)))
        rows[eid] = {
            "id": eid, "type": "report", "name": title,
            "date": _fdate(path), "mtime": _fmtime(path),
            "snapshot": None, "status": _report_status(text, title),
            "status_hint": "",
            "verdict": judge_line, "prereg": prereg,
            "conclusion": _report_conclusion(text),
            "key_numbers": {}, "p_numbers": pnums,
            "source": os.path.join("docs", "reports", fn),
            "md_path": os.path.join("docs", "reports", fn),
            "size": _fsize(path),
        }
    return rows


# ---------------- backlog.md ----------------

def scan_backlog():
    """解析 P 任务表 + 遗留风险看板（编号列表）"""
    rows = []
    risks = []
    try:
        lines = open(BACKLOG, encoding="utf-8").read().splitlines()
    except Exception:
        return rows, risks
    in_risk = False
    for line in lines:
        if line.startswith("## "):
            in_risk = "风险" in line
            continue
        if in_risk:
            s = line.strip()
            if not s:
                continue
            if re.match(r"^\d+\.", s) or s.startswith("|"):
                risks.append(s[:400])
            continue
        s = line.strip()
        if not s.startswith("|") or "---" in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4:
            continue
        pid, topic, state, summary = cells[0], cells[1], cells[2], cells[3]
        if not re.match(r"^P\d", pid):
            continue
        rows.append({
            "id": "backlog_" + re.sub(r"\W+", "_", pid),
            "type": "backlog", "name": "%s %s" % (pid, topic),
            "pid": pid, "topic": topic, "state": state, "summary": summary[:300],
            "status": ("passed" if "完成" in state else
                       "failed" if ("不通过" in state or "否决" in state) else
                       "pending" if ("待" in state or "⏳" in state) else "unknown"),
            "conclusion": ("passed" if "完成" in state else
                           "failed" if ("不通过" in state or "否决" in state) else "unknown"),
            "p_numbers": re.findall(r"P\d+", pid),
            "source": "docs/backlog.md",
            "md_path": None, "date": "", "mtime": _fmtime(BACKLOG),
            "verdict": summary[:300], "key_numbers": {},
        })
    return rows, risks


# ---------------- 合并 ----------------

def merge(bt, reports, backlog_rows, risks):
    exps = []
    for row in bt.values():
        exps.append(row)
    for row in reports.values():
        exps.append(row)
    for row in backlog_rows:
        exps.append(row)
    recent = 0
    for r in exps:
        try:
            mt = time.mktime(time.strptime(r.get("mtime", ""), "%Y-%m-%d %H:%M:%S"))
            if NOW_TS - mt <= 7 * DAY:
                recent += 1
        except Exception:
            pass
    counts = {
        "experiments": len(exps),
        "bt_json": len(bt), "reports": len(reports), "backlog": len(backlog_rows),
        "passed": sum(1 for r in exps if r["status"] == "passed"),
        "failed": sum(1 for r in exps if r["status"] == "failed"),
        "running": sum(1 for r in exps if r["status"] == "running"),
        "pending": sum(1 for r in exps if r["status"] == "pending"),
        "partial": sum(1 for r in exps if r["status"] == "partial"),
        "recent7": recent,
    }
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_root": BASE,
        "counts": counts,
        "experiments": exps,
        "backlog_risks": risks,
    }


def main():
    out = OUT_DEFAULT
    if len(sys.argv) > 1 and sys.argv[1] == "--out":
        out = sys.argv[2]
    t0 = time.time()
    bt, bt_err = scan_bt_json()
    reports = scan_reports()
    backlog_rows, risks = scan_backlog()
    doc = merge(bt, reports, backlog_rows, risks)
    doc["scan_ms"] = int((time.time() - t0) * 1000)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    c = doc["counts"]
    print("索引完成: 实验=%d (bt=%d report=%d backlog=%d) | ✅=%d ❌=%d ⏳=%d 近7日=%d 风险=%d | %.0fms%s" % (
        c["experiments"], c["bt_json"], c["reports"], c["backlog"],
        c["passed"], c["failed"], c["running"] + c["pending"], c["recent7"],
        len(risks), doc["scan_ms"],
        (" | bt解析失败%d" % bt_err) if bt_err else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
