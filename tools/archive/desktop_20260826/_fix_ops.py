import pathlib
p = pathlib.Path(r"C:\Users\26838\A股模拟盘\docs\operations.md")
t = p.read_text(encoding="utf-8")
addition = """
## 快照口径（P53 冻结基线 2026-08-26）

### 口径定义
- **快照口径**：`data/snapshots/2026-08-26/market.db`（`tools/backup_data.py` 在线备份，528MB，319万行日K）+ `data/snapshot_baseline_parts/` 的 8 窗回测（snapshot×live×{score,board}×4 窗，工具 `tools/bt_snapshot_baseline.py` + 汇总 `tools/snapshot_baseline_report.py`）→ 汇总 `data/snapshot_baseline.json`
- **活库口径**：`data/market.db` 实时库在同日同参数下的对照（同工具 `--tag live`）

### 冻结窗口与参数（发布口径）
- 窗口：2019-20 / 2021-22 / 2023-24 / 近1年（2025-08-18~2026-08-21，快照日）
- 策略：`score` 25/3/0.30/0.001 + `board` 40/2/0.25/0.001；`BOARD_MOMENTUM_MIN` **冻结 7.0**（发布口径），与实盘 8.0 不同属预期设计（Phase33 实盘已切 8.0，基线保持 7.0 以与历史可比；工具内 `C.BOARD_MOMENTUM_MIN = 7.0` 硬冻结）
- 漂移阈值：所有窗口 |Δ收益|≤0.5pp 且 |Δ回撤|≤0.5pp → 宣布冻结基线生效

### 当前对账结果（2026-08-26 07:03）
- 8 窗口 0 缺失，`max |Δ收益|=0.000pp, max |Δ回撤|=0.000pp` → **冻结基线生效**（`data/snapshot_baseline.json: pass=true, verdict=冻结基线生效`）
- 复跑：`python tools/snapshot_baseline_report.py`（聚合已落盘的 parts，断点续跑）；重算快照：`python tools/bt_snapshot_baseline.py --db <snapshot.db|market.db> --tag snapshot|live --strategy score|board`（板策略 1-5s/窗，快；分策略 8-30min/窗）

### 漂移监控
- 日常监控 `python tools/snapshot_baseline_report.py` 的 `pass` 与 `max |Δ|`；阈值外触发告警，再决定是否重做快照（每晚前复权重写会导致历史收盘漂移，属预期）

"""
if "## 快照口径" not in t:
    # Insert before "## 数据源资源分布"
    marker = "## 数据源资源分布"
    if marker in t:
        t = t.replace(marker, addition + marker)
    else:
        t = t.rstrip() + addition
    p.write_text(t, encoding="utf-8", newline="\n")
    print("operations.md snapshot section added, new len", len(t))
else:
    print("already has snapshot section")
