# B2 回测工具快照新鲜度断言（2026-09-13）

> 状态：✅ 完成（含 B1/B2 并发合并）

## §0 任务与背景

`tools/bt_exclude_regression.py`、`tools/board_w2s_filter_ab.py`、`tools/bt_convergence_runner.py`
此前用 `data_snapshot.latest_snapshot_path()` 可能拿到 5 天前的旧快照**静默回测**。
本任务给快照新鲜度加断言：`max_age_days=3` 超期返回 None，三工具显式拒绝运行并提示
"先补快照"；所有回测结果 JSON 强制带 `snapshot_tag`/`snapshot_age_days`/`snapshot_coverage`。

## §1 改动 diff 摘要

### app/data_snapshot.py（mtime 14:19:04，330 行，LF，py_compile rc=0）

- 新增 `snapshot_info(path)`：读 manifest 明细 → `{tag, path, created_at, rows_kline_day,
  size_mb, daily_done, coverage, age_days}`。manifest 缺失/旧格式容错（明细字段 None），
  age_days 按目录名 tag 与今天自然日差。
- `latest_snapshot_path(max_age_days=3)`：**B1+B2 合并版**——
  - ★ B1 语义：跳过 `pit_lost` 重建占位（防止回测误当 PIT 基准）；
  - ★ B2 语义：最新可用快照超 `max_age_days` 天返回 **None**；`max_age_days=None` 不校验。

### tools/bt_exclude_regression.py（mtime 14:05:58，74 行，LF，rc=0）

- L13-24：新鲜度检查——`snap = latest_snapshot_path(max_age_days=3)`；None →
  打印 `[B2] 无可用快照或最新快照超期（>3 天，含无快照），拒绝运行——请先补快照
  （python tools/data_snapshot.py --force 或等服务收盘自动生成）` + `sys.exit(2)`。
- L61+：out dict 加 `snapshot_tag`/`snapshot_age_days`/`snapshot_coverage`。

### tools/board_w2s_filter_ab.py（mtime 14:06:29，142 行，LF，rc=0）

- L91/97：main 内 `latest_snapshot_path(max_age_days=3)`，超期 exit(2)（删原"回退活库"分支）；
- L130：out `meta` 加 snapshot 三字段。

### tools/bt_convergence_runner.py（mtime 14:07:00，292 行，LF，rc=0）

- `_resolve_db()`（L59/66）：最大新鲜度检查 + exit(2)；模块级 `SNAP_INFO`；
- main 开头打印 `[B2] snapshot_tag=...`；
- 两个 dump（phase67_seeds / before-after 模式）各加 snapshot 三字段。

## §2 并发冲突与合并（关键）

**现象**：验收②三次启动（convergence + 两重工具）全部
`TypeError: latest_snapshot_path() got an unexpected keyword argument 'max_age_days'`，
而磁盘文件明明有该参数。

**根因**：并发块 B1（同日）基于含 B2 改动的文件继续追加，在文件**后部新增了第二个**
`def latest_snapshot_path():`（无参 + pit_lost 跳过）——**后定义覆盖前定义**，模块加载
后 `latest_snapshot_path` 是无参版。`snapshot_info` 保留，`_is_pit_lost`/
`create_reconstructed_snapshot` 为 B1 新增。

**合并动作**（唯一一次工具性合并，之后 20s+ 内容级复查稳定）：
1. 将 L76 的 B2 版升级为 B1+B2 双语义版（保留 `max_age_days` 参数 + 加 `_is_pit_lost` 跳过）；
2. 删除文件后部 B1 的重复定义。

**合并后验证**：`def latest_snapshot_path` 全文件唯一（=1）；import 签名
`['max_age_days']`；`_is_pit_lost`/`snapshot_info` 均在；py_compile rc=0；LF。

## §3 验收①：超期拒绝（快照目录恢复 09-13→09-08 后复跑）

环境事实：09-09/10/11 为 **B1 生成的 pit_lost 重建占位**（manifest created=2026-09-13T14:13、
pit_lost=True，B2 逻辑正确跳过）；最新真实可用快照=2026-09-08（5 天，超期）。

| 工具 | 输出 | exit |
|---|---|---|
| bt_exclude_regression.py | `[B2] 无可用快照或最新快照超期（>3 天，含无快照），拒绝运行——请先补快照...` | 2 |
| board_w2s_filter_ab.py | 同上 | 2 |
| bt_convergence_runner.py | 同上（需 --out 参数正常路径） | 2 |

拒绝路径**不写结果 JSON**（Test-Path conv_reject.json = False）。
`latest_snapshot_path(max_age_days=3)`=None；`max_age_days=None` 返回 09-08（不校验时正常）。

## §4 验收②：新鲜路径（临时将 09-08 快照改名 2026-09-13 模拟 age=0）

- `latest_snapshot_path(max_age_days=3)` 返回 `...snapshots\2026-09-13\market.db`（age=0）。
- bt_exclude_regression 观察输出：`[regression] snapshot_tag=2026-09-13 age=0d coverage={}`
  `excluded=196` 后正常进入回测（观察后终止防污染）。
- board_w2s_filter_ab 观察输出：`[db] 使用冻结快照: ...2026-09-13... tag=2026-09-13 age=0d
  coverage={}`，正常跑出 `[base 2019-20] ret=-0.1206 ... [filt] ret=+0.1303 Δ=+25.09pp`（终止防污染）。
- bt_convergence_runner 完整跑（`--seeds 42 --configs mom8 --out tmp/b2/conv_fresh.json`）：
  4 窗回测全部跑出，JSON 顶层键含
  `snapshot_tag=2026-09-13`、`snapshot_age_days=0`、`snapshot_coverage={}`。
- 生产 JSON 无污染：`data/bt_exclude_landing_regression.json` / `data/attribution/board_w2s_filter_ab.json`
  观察进程均在进入回测前终止，未写盘。

## §5 快照目录恢复

验收②结束后立即 `Rename-Item data\snapshots\2026-09-13 "2026-09-08"`；残留检查
Test-Path = False。目录最终态：08-26/08-27/08-30/08-31/09-01~09-04/09-07~09-11 +
_archive_pending_del（无 market.db，不干扰）。

## §6 工具既有 bug 披露（与 B2 无关）

bt_convergence_runner 汇总打印（L238 附近）在完整跑结束时抛
`TypeError: must be real number, not NoneType`（by_seed 聚合含 None）——
发生在回测完成、JSON dump 之后，不影响结果落盘。**B2 改动未触及该段**（B2 diff 仅
_resolve_db/SNAP_INFO/dump 三字段）。建议后续单独处置。

## §7 生效条件

- 三个回测工具为独立脚本，改动即时生效（下次直接运行即用新新鲜度断言）。
- `app/data_snapshot.py` 同时被服务端引用——运行中的 8899 进程内存中为旧版，
  本改动只影响回测工具路径，**无需重启服务**；下次服务重启自动加载新逻辑。
- 当前环境无新鲜快照（09-11 为 pit_lost 占位、09-08 超期）→ 三工具按设计拒绝运行，
  **先补快照（等服务收盘自动生成或 `python tools/data_snapshot.py --force`）**。
