# -*- coding: utf-8 -*-
"""★ Phase72: 回测报告双列化 —— 静态池 vs PIT 池对照段生成器（Phase64 范式）。

输入：任意符合以下两种形态之一的回测 JSON：
  A) {"runs": [{"strategy":..., "widx":..., "total_return":...}, ...]}
  B) {"runs": {"<标签>": {...同上字段...}}, ...}
PIT 对照值来自 data/bt_pit_compare.json（Phase64 实测；缺失窗口如实标"未构建"）。

用法：
  python tools/report_pit_columns.py --input data/bt_conv_after.json [--strategy score] \
      [--out section.md]
输出：markdown 双列表格（stdout 或追加到 --out）。
"""
import sys
import os
import json
import argparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import config as C   # noqa: E402

PIT_FILE = os.path.join(C.DATA_DIR, "bt_pit_compare.json")
WN = ["2019-20", "2021-22", "2023-24", "近1年"]


def _iter_runs(static):
    if not isinstance(static, dict):
        raise ValueError("输入 JSON 须为对象")
    runs = static.get("runs")
    if isinstance(runs, list):
        for r in runs:
            yield r.get("strategy", "?"), int(r.get("widx", -1)), r
    elif isinstance(runs, dict):
        for label, rec in runs.items():
            strat = rec.get("strategy") or ("board" if "board" in label else "score")
            widx = rec.get("widx")
            yield label, (int(widx) if widx is not None else -1), rec
    else:
        raise ValueError("JSON 无 runs 字段")


WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
WN = ["2019-20", "2021-22", "2023-24", "近1年(牛)"]


def load_pit():
    if not os.path.isfile(PIT_FILE):
        return {}
    d = json.load(open(PIT_FILE, encoding="utf-8"))
    out = {}
    for r in d.get("runs", []):
        variant = str(r.get("variant", ""))
        if not variant.lower().startswith("pit"):
            continue
        out[(r.get("strategy"), int(r.get("widx", -1)))] = r.get("total_return")
    return out


def extract_static(path, strategy_filter=None):
    d = json.load(open(path, encoding="utf-8"))
    rows = []
    for strat, widx, rec in _iter_runs(d):
        if strategy_filter and strat != strategy_filter:
            continue
        ret = rec.get("total_return")
        if ret is None or widx is None or not (0 <= widx < len(WN)):
            continue
        rows.append((strat, widx, float(ret)))
    # 同键去重（保留最后）
    dedup = {}
    for s, w, v in rows:
        dedup[(s, w)] = v
    return dedup


def main():
    ap = argparse.ArgumentParser(description="静态池 vs PIT 池 双列对照段生成")
    ap.add_argument("--input", required=True, help="回测结果 JSON")
    ap.add_argument("--strategy", default=None, help="仅输出指定策略")
    ap.add_argument("--title", default="静态池 vs PIT 池对照（Phase64 口径）")
    ap.add_argument("--out", default=None, help="追加写入的 md 文件（缺省打印 stdout）")
    a = ap.parse_args()

    static_map = extract_static(a.input, a.strategy)
    pit_map = load_pit()
    lines = []
    lines.append("### %s" % a.title)
    lines.append("")
    lines.append("| 策略 | 窗口 | 静态池收益 | PIT 池收益 | Δ(pp) | 高估? |")
    lines.append("|---|---|---|---|---|---|")
    for (strat, wi), sret in sorted(static_map.items()):
        pkey = (strat, wi)
        pret = pit_map.get(pkey)
        if pret is None:
            lines.append("| %s | %s | %+.2f%% | 未构建 | — | — |"
                         % (strat, WN[wi], sret * 100))
            continue
        delta = (pret - sret) * 100
        lines.append("| %s | %s | %+.2f%% | %+.2f%% | %+.2f | %s |"
                     % (strat, WN[wi], sret * 100, pret * 100, delta,
                        "是（前视高估）" if delta < 0 else "否"))
    missing_pools = [wn for wi, wn in enumerate(WN)
                     if all((s, wi) not in pit_map for s in ("score", "board"))
                     and any((s, wi) in static_map for s in ("score", "board"))]
    if missing_pools:
        lines.append("")
        lines.append("> 注：窗口 [%s] 的 PIT 池尚未构建（见 Phase64 §2 局限），"
                     "如实标注为未构建。" % "、".join(missing_pools))
    section = "\n".join(lines)
    if a.out:
        with open(a.out, "a", encoding="utf-8", newline="\n") as f:
            f.write(section + "\n\n")
        print("[ok] 已追加到", a.out)
    else:
        print(section)


if __name__ == "__main__":
    main()
