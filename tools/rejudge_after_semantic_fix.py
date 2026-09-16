# -*- coding: utf-8 -*-
"""★ Phase70 复验收口：window_pass 语义修正后，重算全部受影响判定。"""
import sys
import os
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
import judge_kit as jk   # noqa: E402

DATA = os.path.join(BASE, "data")
report = []


def note(s):
    report.append(s)
    print(s, flush=True)


# ---- 1) exit_pack_v2 ----
d = json.load(open(os.path.join(DATA, "bt_exit_pack_v2.json"), encoding="utf-8"))
note("== exit_pack_v2 ==")
for j in d["judgement"]:
    det = j["detail"]
    losses = [x["loss_pp"] for x in det]
    bull = next((x["loss_pp"] for x in det if x["win"] == "牛市"), None)
    _, w_ok = jk.window_pass(losses, tol_loss_pp=1.0, min_windows=3)
    b_ok = jk.bull_ok(bull, max_loss_pp=2.0)
    new_pass = bool(w_ok and b_ok)
    flips = "" if new_pass == j["pass"] else "  ← 翻转(旧 %s)" % j["pass"]
    note("  %-24s 达标窗=%d 牛市=%+.2fpp new_pass=%s%s"
         % (j["variant"], w_ok, bull if bull is not None else float("nan"),
            new_pass, flips))
    j["pass"] = new_pass
d["judge_note"] = "Phase70 window_pass 语义修正后重算"

# ---- 2) dd_gate v2 ----
d = json.load(open(os.path.join(DATA, "bt_dd_gate_v2.json"), encoding="utf-8"))
note("== dd_gate_v2 ==")
for phase in ("judgement_phase1", "judgement_phase2"):
    for lab, j in d[phase].items():
        for s in ("score", "board"):
            det = j[s]["detail"]
            losses = [x["loss_pp"] for x in det]
            _, w_ok = jk.window_pass(losses, tol_loss_pp=1.0, min_windows=3)
            b = next((x for x in det if x["win"].startswith("牛")), None)
            bull = abs(b["loss_pp"]) if b and b["loss_pp"] > 0 else \
                -(b["loss_pp"]) if b else None   # 占位（dd_gate 无 bull 单列规则）
            # dd_gate 的窗口口径本就不用 window_pass（多条件结构），此处仅统计演示
        note("  %-16s c1=%s c2=%s c3=%s c4=%s pass=%s（结构未用 window_pass，不受影响）"
             % (lab, j["score"]["c1_mdd_ge3of4"], j["score"]["c2_calmar_ge3of4"],
                j["score"]["c3_loss_le3pp"], j["score"]["c4_occupancy_le40pct"],
                j["score"]["pass"]))

# ---- 3) board_weight_ab ----
d = json.load(open(os.path.join(DATA, "board_weight_ab.json"), encoding="utf-8"))
pr = d["prereg"]
losses = [-x for x in pr["deltas_pp"]]
_, nw = jk.window_pass(losses, tol_loss_pp=0.5, min_windows=3)
new_verdict = "建议落地" if (nw >= 3 and pr["mean_delta_pp"] > 0) else "不建议落地"
flips = "" if new_verdict == pr["verdict"] else "  ← 翻转(旧 %s)" % pr["verdict"]
note("== board_weight_ab ==\n  not_worse=%d mean=%+.2f new_verdict=%s%s"
     % (nw, pr["mean_delta_pp"], new_verdict, flips))

# ---- 4) exit_scan ----
d = json.load(open(os.path.join(DATA, "bt_exit_scan.json"), encoding="utf-8"))
note("== exit_scan ==")
for v in d.get("variants", []):
    dd = v.get("deltas_pp") or []
    if len(dd) < 4:
        continue
    losses = [-x for x in dd]     # Δ 收益取负 = 损失
    _, w_ok = jk.window_pass(losses, tol_loss_pp=1.0, min_windows=3)
    bull_loss = -dd[3]
    b_ok = jk.bull_ok(bull_loss, max_loss_pp=2.0)
    cons = v.get("consistent_triggers", True)
    new_pass = bool(w_ok and b_ok and cons)
    flips = "" if new_pass == v.get("pass") else "  ← 翻转(旧 %s)" % v.get("pass")
    note("  S=%.2f T=%d old=%s new=%s%s"
         % (v.get("stop", 0), v.get("time_stop", 0), v.get("pass"), new_pass, flips))

open(os.path.join(DATA, "bt_exit_pack_v2.json"), "w", encoding="utf-8",
     newline="\n").write(json.dumps(d, ensure_ascii=False, indent=1))
print("\n[done] exit_pack_v2 判定已按修正语义回写")
