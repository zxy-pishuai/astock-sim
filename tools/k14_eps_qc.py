# -*- coding: utf-8 -*-
"""K14 QC 分析器（独立工具，只读 data/vendor/k14 快照；预注册判据见 k14_eps_collect.py 文件头）

按 F4 纪律：快照日数 < 20 → 只出覆盖度/稳定性 QC，不做任何 alpha 判定。
输出：data/vendor/k14/k14_qc_latest.json + 控制台摘要。
"""
import json
import pathlib
import statistics
import sys
import time

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = BASE / "data" / "vendor" / "k14"


def load_close_map():
    """kline 最新 close（只读取自 market.db）。"""
    import sqlite3
    c = sqlite3.connect(str(BASE / "data" / "market.db"))
    rows = c.execute("select code, close from kline where date=(select max(date) from kline)").fetchall()
    c.close()
    return {r[0]: r[1] for r in rows}


def next_uncompleted_year(eps_rows, today_y):
    """预测年度里 >= 当年且未完成均值可用、排最前的年份（F1）。"""
    for r in sorted(eps_rows, key=lambda x: x["year"]):
        if int(r["year"]) >= today_y and r.get("v2_mean"):
            return r
    return None


def main():
    snaps = sorted(OUT.glob("eps_snap_*.json"))
    if not snaps:
        print("无快照")
        return
    latest = snaps[-1]
    day = latest.stem.split("_")[-1]
    snap = json.loads(latest.read_text(encoding="utf-8"))
    rows = snap["rows"]
    n_uni = snap.get("universe", len(rows))
    parsed = {c: r for c, r in rows.items() if r and r.get("eps")}
    thin = ok_pair = 0
    ymap = {}
    for code, r in parsed.items():
        nxt = next_uncompleted_year(r["eps"], int(day[:4]))
        if nxt is None:
            continue
        ni = nxt.get("n_inst")
        if not ni or ni < 3:
            thin += 1
            continue
        ok_pair += 1
        ymap[code] = {"eps_mean": nxt["v2_mean"], "n_inst": ni,
                      "ind_mean": nxt.get("v4_ind_mean")}
    closes = load_close_map()
    ey = {}
    for code, v in ymap.items():
        cl = closes.get(code)
        if cl and cl > 0 and v.get("eps_mean") and v["eps_mean"] > 0:
            ey[code] = round(v["eps_mean"] / cl * 100, 3)
    ecvals = sorted(ey.values())
    qc = {
        "day": day,
        "snapshot_count": len(snaps),
        "universe": n_uni,
        "parsed_n": len(parsed),
        "coverage_pct": round(len(parsed) / max(1, n_uni) * 100, 1),
        "thin_n_inst_removed": thin,
        "usable_pct": round(ok_pair / max(1, n_uni) * 100, 1),
        "ey_n": len(ey),
        "ey_med": round(statistics.median(list(ey.values())), 3) if ey else None,
        "ey_p10": round(ecvals[int(len(ecvals) * 0.10)], 3) if len(ecvals) > 10 else None,
        "ey_p90": round(ecvals[int(len(ecvals) * 0.90)], 3) if len(ecvals) > 10 else None,
        "phase": ("QC-only(<20日)" if len(snaps) < 20
                  else "first-IC(20-59日)" if len(snaps) < 60
                  else "provisional(>=60日)"),
    }
    if len(snaps) >= 2:
        prev = json.loads(snaps[-2].read_text(encoding="utf-8"))
        prows = {c: r for c, r in prev.get("rows", {}).items() if r and r.get("eps")}
        drifts = []
        for code in set(prows) & set(parsed):
            a = next_uncompleted_year(prows[code]["eps"], int(day[:4]))
            b = next_uncompleted_year(parsed[code]["eps"], int(day[:4]))
            if a and b and a.get("v2_mean") and b.get("v2_mean") and abs(b["v2_mean"]) > 1e-9:
                drifts.append(abs(b["v2_mean"] / a["v2_mean"] - 1))
        if drifts:
            qc["cross_day_eps_drift_med"] = round(statistics.median(drifts), 4)
    (OUT / "k14_qc_latest.json").write_text(json.dumps(qc, ensure_ascii=False, indent=2),
                                            encoding="utf-8", newline="\n")
    print(json.dumps(qc, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
