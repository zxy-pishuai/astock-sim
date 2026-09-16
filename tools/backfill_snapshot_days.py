# -*- coding: utf-8 -*-
"""★ B1（2026-09-13）：补生成 09-09/10/11 缺失快照（PIT 不可复现的占位副本）。

背景：09-09/10/11 三天因"日K未追平→只记 data_snapshot_deferred 永不落盘"回归
没有快照目录。这三天 market.db 的真实状态已被后续更新覆盖，**无法真正还原**——
本脚本落的是"当前库副本 + manifest 标记 reconstructed=false, pit_lost=true"，
仅供磁盘一致性，**禁止当 PIT 基准用**（data_snapshot.latest_snapshot_path() 已跳过
此类快照，回测不会误取）。

用法：python tools/backfill_snapshot_days.py [--days 2026-09-09 2026-09-10 2026-09-11]
幂等：已存在则跳过（不覆盖）。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import data_snapshot as DSN  # noqa: E402

DEFAULT_DAYS = ["2026-09-09", "2026-09-10", "2026-09-11"]


def main():
    args = sys.argv[1:]
    days = []
    if "--days" in args:
        i = args.index("--days")
        days = args[i + 1:]
    if not days:
        days = DEFAULT_DAYS
    for tag in days:
        p, created = DSN.create_reconstructed_snapshot(tag)
        print("[backfill] %s -> %s (%s)" % (tag, p, "新建" if created else "已存在跳过"))
    print("[backfill] 完成。注意：pit_lost=true，仅磁盘一致性占位，禁止当 PIT 基准。")


if __name__ == "__main__":
    main()
