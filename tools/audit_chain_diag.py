# -*- coding: utf-8 -*-
"""★ 任务1-③：审计哈希链断裂诊断（一次性基线 + 可复用体检）

职责边界（红线）：只读标记历史断裂位置，绝不重写/修补 audit.jsonl。
判定口径与写入端约定一致：每天首条 expected prev=""；此后 prev=上一条 hash；
hash 由 payload(sort_keys)+prev 重算比对。断点处重同步继续验后续行。

用法:
  python tools/audit_chain_diag.py                 # 打印汇总+前 N 条明细
  python tools/audit_chain_diag.py --json PATH    # 断裂清单落盘（默认 data/audit_chain_breaks.json）
  python tools/audit_chain_diag.py --verify FILE  # 对任意审计 jsonl 做同口径体检（用于修复验证）
"""
import argparse
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_AUDIT = os.path.join(BASE, "data", "audit", "audit.jsonl")
DEFAULT_JSON = os.path.join(BASE, "data", "audit_chain_breaks.json")


def scan(path):
    """返回 {"lines","checked","breaks_total","breaks":[...],"by_day":{},
             "resumed_after_break"}。断点处重同步继续验。"""
    res = {"file": path, "lines": 0, "checked": 0, "breaks_total": 0,
           "breaks": [], "by_day": {}, "resumed_after_break": 0}
    if not os.path.exists(path):
        res["error"] = "file-missing"
        return res
    prev = ""
    cur_day = ""
    with open(path, encoding="utf-8") as f:
        for i, ln in enumerate(f, 1):
            ln = ln.strip()
            if not ln:
                continue
            res["lines"] += 1
            try:
                r = json.loads(ln)
            except Exception:
                res["breaks"].append({"line": i, "nature": "bad_json"})
                continue
            t = r.get("t", "")
            d = t[:10]
            if d != cur_day:
                cur_day = d
                prev = ""                    # 与写入端/校验端同款跨天复位
            payload = {k: v for k, v in r.items()
                       if k not in ("hash", "prev")}
            expect = hashlib.sha256(
                (json.dumps(payload, ensure_ascii=False, sort_keys=True)
                 + "|" + prev).encode("utf-8")).hexdigest()[:16]
            prev_ok = r.get("prev") == prev
            hash_ok = r.get("hash") == expect
            if not (prev_ok and hash_ok):
                nature = []
                if not prev_ok:
                    nature.append("prev_mismatch")
                if not hash_ok:
                    nature.append("hash_mismatch")
                res["breaks"].append({
                    "line": i, "t": t, "kind": r.get("kind"),
                    "event": r.get("event"), "nature": "+".join(nature),
                    "expected_prev_tail4": prev[-4:] if prev else "",
                    "actual_prev_tail4": (r.get("prev") or "")[-4:],
                })
                res["breaks_total"] += 1
                res["by_day"][d] = res["by_day"].get(d, 0) + 1
                prev = r.get("hash", "")     # 重同步：以后续记录自身链继续验
                res["resumed_after_break"] += 1
            else:
                res["checked"] += 1
                prev = r["hash"]
    return res


def main():
    ap = argparse.ArgumentParser(description="审计哈希链断裂诊断（只读标记）")
    ap.add_argument("--json", default=DEFAULT_JSON,
                    help="断裂清单输出路径（--no-json 关闭）")
    ap.add_argument("--no-json", action="store_true")
    ap.add_argument("--limit", type=int, default=12,
                    help="终端明细最多显示条数")
    ap.add_argument("--verify", default=None,
                    help="对指定审计 jsonl 体检（默认用主审计文件）")
    a = ap.parse_args()

    target = a.verify or DEFAULT_AUDIT
    res = scan(target)

    print("== 审计哈希链诊断（只读标记，绝不重写历史）==")
    print("目标文件:", res.get("file"))
    if res.get("error"):
        print("  ", res["error"])
        return
    print("总行数 %d｜链内连续校验通过 %d｜断裂 %d 处（断点重同步口径）"
          % (res["lines"], res["checked"], res["breaks_total"]))
    print("健康率 %.1f%%" % (
        res["checked"] / max(res["lines"] - sum(
            1 for b in res["breaks"] if b["nature"] == "bad_json"), 1) * 100))
    if res["by_day"]:
        print("按日分布:", json.dumps(res["by_day"],
                                      ensure_ascii=False))
    print("前 %d 条明细:" % min(a.limit, len(res["breaks"])))
    for b in res["breaks"][:a.limit]:
        print("  行%-5d %s %-9s %-14s [%s] exp_prev=…%s act_prev=…%s" % (
            b.get("line", "-"), b.get("t", "?"), b.get("kind") or "-",
            b.get("event") or "-", b.get("nature"),
            b.get("expected_prev_tail4", ""), b.get("actual_prev_tail4", "")))
    print("\n注: 本脚本仅生成清单。历史断裂代表并发写时代的链路事实，")
    print("    按任务红线不做任何修补/重写；修复后新写入的健康度用同一")
    print("    口径复查（--verify）对比本基线即可。")

    if not a.no_json and res["breaks"]:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print("断裂清单已存档 → %s（%d 条）" % (a.json, len(res["breaks"])))


if __name__ == "__main__":
    main()
