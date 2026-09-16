# -*- coding: utf-8 -*-
"""★ Phase58：实盘成交标定 —— 用 audit.jsonl 真实打板成交校准 true_board.md 的 M2 先验

背景：true_board.md 的 M2 中性口径（封死单按首触时间 10:30 前 0.3 / 之后 0.6，
炸板单 100% 成交）为先验设定而非实测。本工具解析 data/audit/audit.jsonl 的
实际成交记录做对照。

方法：
  1) 解析 order 记录：打板成交 = event=trading_event 且 msg 含 "[打板]" 的 BUY；
     score/手动/测试买入仅作上下文，不进 M2 校准样本。
  2) 覆盖检查：标的不在 min5.db 覆盖集内 → 标注"不可重建"剔除。
  3) 首触涨停重建：min5 当日首根 high ≥ 涨停价−0.005 的 bar（bar 时间戳为
     结束时刻，触板发生在该 bar 区间内）。
  4) 分类：未触板 / 触板前成交 / 触板后成交（后者对应 M2 概率桶）；
     按 M2 时间桶（≤10:30、>10:30）聚合。

★置信声明（事先写明）：audit.jsonl 自 08-16 起仅 1375 行、真实打板成交个位数，
样本远低于任何统计效力门槛；本任务只建立标定流程与出报告，不对 M2 参数做任何
修改，也不改 config。等实盘积累 ≥30 个封死单样本后再谈校准。

用法: python tools/live_fill_calibration.py
输出: data/attribution/live_fill_calibration.json
"""
import json
import os
import re
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C
from app.engine import limit_prices

AUDIT = os.path.join(BASE, "data", "audit", "audit.jsonl")
MIN5 = os.path.join(BASE, "data", "min5.db")
DB = os.path.join(BASE, "data", "market.db")
OUT_JSON = os.path.join(BASE, "data", "attribution",
                        "live_fill_calibration.json")
EPS = 0.005
BUCKET_SPLIT = "10:30"


def parse_fills():
    """解析 audit.jsonl 的 order 记录 → (board_fills, other_fills, excluded)"""
    board, other, excluded = [], [], []
    pat = re.compile(r"买入\s+(.+?)\((\d{6})\)\s*([\d.]+)x(\d+)股")
    for line in open(AUDIT, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("kind") != "order":
            continue
        if rec.get("event") == "test_buy":
            excluded.append({**rec, "exclude_reason": "测试记录"})
            continue
        if rec.get("event") == "manual_buy":
            excluded.append({**rec, "exclude_reason": "手动买入（非策略成交）"})
            continue
        if rec.get("level") != "BUY":
            continue
        m = pat.search(rec.get("msg") or "")
        if not m:
            excluded.append({**rec, "exclude_reason": "msg 无法解析"})
            continue
        fill = {
            "t": rec["t"], "day": rec["t"][:10],
            "fill_hhmmss": rec["t"][11:19], "name": m.group(1),
            "code": m.group(2), "price": float(m.group(3)),
            "qty": int(m.group(4)),
            "strategy": "board" if "[打板]" in rec["msg"] else "score",
        }
        (board if fill["strategy"] == "board" else other).append(fill)
    return board, other, excluded


def min5_coverage():
    conn = sqlite3.connect("file:%s?mode=ro" % MIN5, uri=True, timeout=15)
    codes = {r[0][2:] for r in conn.execute(
        "SELECT DISTINCT code FROM kline_min5")}
    conn.close()
    return codes


def prev_close(code, day):
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)
    row = conn.execute(
        "SELECT close FROM kline WHERE period='day' AND code=? AND date<? "
        "AND close>0 ORDER BY date DESC LIMIT 1", (code, day)).fetchone()
    conn.close()
    return row[0] if row else None


def first_touch(code, day):
    """返回 (touch_hhmm, touched) 或 None（无数据）"""
    sym = ("sh" if code.startswith("6") else "sz") + code
    pc = prev_close(code, day)
    if not pc or pc <= 0:
        return None
    lu, _ld = limit_prices(code, pc)
    conn = sqlite3.connect("file:%s?mode=ro" % MIN5, uri=True, timeout=15)
    rows = conn.execute(
        "SELECT date, high FROM kline_min5 WHERE code=? AND date LIKE ? "
        "ORDER BY date", (sym, day + "%")).fetchall()
    conn.close()
    if not rows:
        return None
    for d, h in rows:
        if h is not None and h >= lu - EPS:
            return {"touch_hhmm": d[11:16], "touched": True, "limit_up": lu}
    return {"touch_hhmm": None, "touched": False, "limit_up": lu}


def bucket(hhmm):
    if not hhmm:
        return None
    return "le_1030" if hhmm <= BUCKET_SPLIT else "gt_1030"


def main():
    t0 = time.time()
    board_fills, other_fills, excluded = parse_fills()
    cov = min5_coverage()
    print("打板成交 %d 笔 | 其他策略买入 %d 笔 | 排除 %d 条 | min5 覆盖 %d 只" % (
        len(board_fills), len(other_fills), len(excluded), len(cov)), flush=True)

    calibrated, unrebuildable = [], []
    for f in board_fills:
        if f["code"] not in cov or f["day"] < "2025-08-18":
            unrebuildable.append({**f, "reason": "不可重建（min5 未覆盖该标的/日期）"})
            continue
        ft = first_touch(f["code"], f["day"])
        if ft is None:
            unrebuildable.append({**f, "reason": "不可重建（无日线昨收或无当日分时）"})
            continue
        if not ft["touched"]:
            cls = "未触板（炸板/未封 → M2 假设 100% 成交）"
            touch_bucket = None
        elif f["fill_hhmmss"][:5] <= ft["touch_hhmm"]:
            cls = "触板前成交（排队先于封板，M2 意义下应可成交）"
            touch_bucket = bucket(ft["touch_hhmm"])
        else:
            cls = "触板后成交（封死单 → M2 概率桶适用）"
            touch_bucket = bucket(ft["touch_hhmm"])
        calibrated.append({**f, "limit_up": ft["limit_up"],
                           "touch_hhmm": ft["touch_hhmm"],
                           "touched": ft["touched"], "class": cls,
                           "touch_bucket": touch_bucket,
                           "fill_bucket": bucket(f["fill_hhmmss"][:5])})
        print("  %s %s(%s) 成交@%s | 首触@%s | %s" % (
            f["day"], f["name"], f["code"], f["fill_hhmmss"][:5],
            ft["touch_hhmm"] or "—", cls), flush=True)

    # M2 桶对照（样本极小，仅流程演示）
    sealed = [c for c in calibrated if c["class"].startswith("触板后")]
    obs = {b: sum(1 for s in sealed
                  if (s["touch_bucket"] == b)) for b in ("le_1030", "gt_1030")}
    m2_prior = {"le_1030": 0.3, "gt_1030": 0.6}
    confidence = {
        "statement": "本批次打板成交样本 n=%d（其中封死单 %d），远低于统计效力门槛；"
                     "观测占比不构成对 M2 先验（0.3/0.6）的检验，仅建立标定流程。"
                     "建议积累 ≥30 个封死单样本后重跑本工具再谈参数修订。" % (
                         len(calibrated), len(sealed)),
        "min_sample_for_calibration": 30,
        "current_sealed_sample": len(sealed),
    }
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "58",
        "m2_prior": {"sealed_le_1030": 0.3, "sealed_gt_1030": 0.6,
                     "broken": 1.0,
                     "source": "docs/reports/true_board.md（先验设定，非实测）"},
        "audit_stats": {"kinds": "见 parsed", "order_lines": len(board_fills)
                        + len(other_fills) + len(excluded)},
        "board_fills_calibrated": calibrated,
        "observed_sealed_by_touch_bucket": obs,
        "m2_vs_observed_note": "n 过小无法比较；本表仅为后续积累数据的模板",
        "unrebuildable_excluded": unrebuildable,
        "other_strategy_fills_context": other_fills,
        "excluded_records": excluded,
        "confidence_statement": confidence,
        "no_param_change": True,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("\n置信声明:", confidence["statement"], flush=True)
    print("已写出 %s | 总耗时 %.0fs" % (OUT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
