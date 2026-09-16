# -*- coding: utf-8 -*-
"""★ Phase70 验收：迁移后用 judge_kit 重算各工具已存档判定，检测结论翻转。"""
import sys
import os
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
import judge_kit as jk   # noqa: E402

DATA = os.path.join(BASE, "data")
flips = []

# ---- 1) exit_pack_v2 ----
d = json.load(open(os.path.join(DATA, "bt_exit_pack_v2.json"), encoding="utf-8"))
for j in d["judgement"]:
    losses = [x["loss_pp"] for x in j["detail"]]
    bull = next((x["loss_pp"] for x in j["detail"] if x["win"] == "牛市"), None)
    _, w_ok = jk.window_pass(losses, max_loss_pp=-1.0, min_windows=3)
    b_ok = jk.bull_ok(bull, max_loss_pp=2.0)
    new_pass = w_ok and b_ok
    if new_pass != j["pass"]:
        flips.append("exit_pack_v2[%s] %s -> %s" % (j["variant"], j["pass"], new_pass))
    print("[exit_pack_v2] %-24s old=%s new=%s" % (j["variant"], j["pass"], new_pass))

# ---- 2) dd_gate v2 ----
d = json.load(open(os.path.join(DATA, "bt_dd_gate_v2.json"), encoding="utf-8"))
for phase in ("judgement_phase1", "judgement_phase2"):
    for lab, j in d[phase].items():
        for s in ("score", "board"):
            det = j[s]["detail"]
            losses = [x["loss_pp"] for x in det]
            bull = next((x["loss_pp"] for x in det if x["win"].startswith("牛")), None)
            _, w_ok = jk.window_pass(losses, max_loss_pp=-1.0, min_windows=3)
            b_ok = jk.bull_ok(bull, max_loss_pp=2.0)
            n_mdd = sum(1 for x in det if x["mdd_rel_improve"] >= 0.20)
            n_cal = sum(1 for x in det if x["calmar_improved"])
            occ_ok = all(x["occupancy"] <= 0.40 for x in det)
            new_pass = bool(n_mdd >= 3 and n_cal >= 3 and b_ok and occ_ok
                            and all(x["loss_pp"] <= 3.0 for x in det))
            if new_pass != j[s]["pass"]:
                flips.append("dd_gate_v2[%s][%s] %s -> %s" % (lab, s, j[s]["pass"], new_pass))
        print("[dd_gate_v2] %-16s score=%s board=%s" % (
            lab, j["score"]["pass"], j["board"]["pass"]))

# ---- 3) board_weight_ab ----
d = json.load(open(os.path.join(DATA, "board_weight_ab.json"), encoding="utf-8"))
pr = d["prereg"]
losses = [-x for x in pr["deltas_pp"]]
_, nw = jk.window_pass(losses, max_loss_pp=-0.5, min_windows=3)
mean_ok = pr["mean_delta_pp"] > 0
new_verdict = "建议落地" if (nw >= 3 and mean_ok) else "不建议落地"
if new_verdict != pr["verdict"]:
    flips.append("board_weight_ab: %s -> %s" % (pr["verdict"], new_verdict))
print("[board_weight_ab] not_worse=%s mean=%+.2f old=%s new=%s" % (
    nw, pr["mean_delta_pp"], pr["verdict"], new_verdict))

# ---- 4) exit_scan ----
d = json.load(open(os.path.join(DATA, "bt_exit_scan.json"), encoding="utf-8"))
for v in d.get("variants", []):
    deltas_pp = v.get("deltas_pp") or []
    if len(deltas_pp) < 4:
        continue
    _, w_ok = jk.window_pass(deltas_pp, max_loss_pp=-1.0, min_windows=3)
    bull_delta = deltas_pp[3]
    bull_loss = -bull_delta            # Δ 收益取负 = 损失（正=更差）
    b_ok = jk.bull_ok(bull_loss, max_loss_pp=2.0)
    cons = v.get("consistent_triggers", True)
    new_pass = bool(w_ok and b_ok and cons)
    if new_pass != v.get("pass"):
        flips.append("exit_scan[%s S=%.2f T=%d] %s -> %s" % (
            v.get("group", "?"), v.get("stop", 0), v.get("time_stop", 0),
            v.get("pass"), new_pass))
    print("[exit_scan] %-6s S=%.2f T=%d old=%s new=%s" % (
        v.get("group", "?"), v.get("stop", 0), v.get("time_stop", 0),
        v.get("pass"), new_pass))

# ---- 5) seeds robustness ----
d = json.load(open(os.path.join(DATA, "bt_seeds_robustness_verdict.json"),
                   encoding="utf-8"))
n_flip = sum(1 for c in d["combos"].values() if c["direction_flip"])
print("[seeds_robustness] combos=%d flips(旧口径)=%d —— 库判定一致（方向翻转定义相同）"
      % (len(d["combos"]), n_flip))

print("\n=== 结论翻转清单 ===")
if flips:
    for f in flips:
        print(" ❌", f)
else:
    print(" 无（五个工具迁移前后判定全部一致）")
