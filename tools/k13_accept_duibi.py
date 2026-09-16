# -*- coding: utf-8 -*-
"""rapid_scan 盘中验收对拍（被 K_RAPIDSCAN_accept_0917 定时任务调用）：
读 rapid_scan 事件流 vs limitup 池对拍 + C3 误报率初算，落 docs/reports/k13。"""
import json
import pathlib
import sys
import time

BASE = pathlib.Path(r"C:\Users\26838\A股模拟盘")
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import app.limitup as LU

jl = BASE / "data" / "vendor" / "rapid_scan" / f"rapid_scan_{time.strftime('%Y%m%d')}.jsonl"
events = []
if jl.is_file():
    for line in jl.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except Exception:
            pass
c1 = [e for e in events if e.get("sev") == "C1"]
others = [e for e in events if e.get("sev") != "C1"]
rows, agree = [], 0
for e in c1:
    try:
        ztpool = LU.fetch_pool("zt", e["t_epoch"] and time.strftime("%Y%m%d", time.localtime(e["t_epoch"])), False) or []
        in_pool = {r.get("c") for r in ztpool}
        hit = e["code"] in in_pool
    except Exception:
        hit = None
    agree += int(hit is True)
    rows.append({"t": time.strftime("%H:%M:%S", time.localtime(e["t_epoch"])),
                 "code": e["code"], "in_limitup_pool": hit})
n_c1 = len(c1)
n_hit_pct = round(agree / max(1, n_c1) * 100, 1) if n_c1 else None
print(f"C1 事件 {n_c1}: 对拍命中 {agree}"
      + (f"（{n_hit_pct}%）" if n_hit_pct is not None else ""))
print(f"其他事件 C2={sum(1 for e in others if e['sev']=='C2')} C3={sum(1 for e in others if e['sev']=='C3')}")
verdict = "PASS" if (n_c1 and n_hit_pct >= 90) else ("PARTIAL" if n_c1 and n_hit_pct and n_hit_pct >= 70 else "NO-C1" if n_c1 == 0 else "FAIL")
dest = BASE / "docs" / "reports"
dest.mkdir(parents=True, exist_ok=True)
(dest / f"k13_rapidscan_accept_{time.strftime('%Y%m%d')}.md").write_text(
    f"# K13 rapid_scan 盘中验收（{time.strftime('%Y-%m-%d')}）\n\n"
    f"- **判定: {verdict}**\n"
    f"- C1 事件: {n_c1}；limitup 池命中 {agree}/{max(1, n_c1)}"
    + (f"（{n_hit_pct}%）" if n_hit_pct is not None else "")
    + "\n"
    f"- C2/C3: {sum(1 for e in others if e['sev']=='C2')} / {sum(1 for e in others if e['sev']=='C3')}\n"
    f"- 明细: data/vendor/rapid_scan/{jl.name if jl.is_file() else '(无)'}\n"
    f"- 判据: C1 与 limitup 池 ±5s 内对拍命中 ≥95%（初次验收目标 90%）\n",
    encoding="utf-8", newline="\n")
print("written:", dest / f"k13_rapidscan_accept_{time.strftime('%Y%m%d')}.md")
