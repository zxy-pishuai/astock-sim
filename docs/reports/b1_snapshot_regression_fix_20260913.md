# B1 快照三天未生成——分级落盘回归修复（2026-09-13）

## §0 结论先行
- 根因实证：`_daily_coverage()` 真实 ratio=**0.9371**（fresh=4993 / total=5328，missing=335，缺 000004/000005/000018 等退市/停牌票）——stale-first 日K"永远追不平"（total 分母含 335 只永远拉不到的票），`_daily_done` 恒 False → 旧快照段只记 `data_snapshot_deferred` **永不落盘** → 09-09/10/11 三天只有 INFO deferred、无快照。
- 修复：快照策略从"追平才落盘"改为**分级落盘**（ratio≥0.95 正常落 / 0.8~0.95 带病落 / <0.8 延后 + 22:00 强制兜底），`auto_snapshot()` 内建"当日最终兜底"（22:00 后无快照→无条件落盘 + CRITICAL `data_snapshot_forced`）。
- 三天补快照已落盘（09-09/10/11，`reconstructed=false, pit_lost=true` 占位副本，**PIT 不可复现，禁止当 PIT 基准**）。
- 生效方式：收盘主流程（服务内 run_update）需服务重启加载新代码；**未重启期间由 TianjiCloseCatchup（独立新进程，已加载新代码）22:00 后强制兜底保证当日必有快照**。

## §1 根因证据链
1. 旧代码（F4 时代，updater.py L1218-1227）：`if not _daily_done:` → 仅 `audit.record(data_snapshot_deferred, INFO)`，无任何落盘动作。
2. `_daily_done = pending_remain == 0`，而 pending_remain 来自 stale-first 日K队列——**退市/停牌票永远拉不到日K**（000004 最后日K 2026-07-13、000018 最后 2026-01-06 前停更），每轮回补重新入队 → `pending_remain>0` 永不归零。
3. 实测（2026-09-13 14:0x，只读）：ratio=0.9371，missing=335，`missing_sample=['000004','000005','000018','000022','000023',...]`。0.937<0.95 → 当日若触发收盘流程，`_daily_done=False` → 旧代码 deferred。
4. 09-09/10/11 audit 链：仅 `data_snapshot_deferred`（INFO），无 `data_snapshot`/`data_snapshot_failed`——与根因链吻合（task 书"A 道覆盖率永远追不平 + 兜底时序接不上"）。

## §2 改动清单（diff 级）
| 文件 | 改动 |
|---|---|
| `app/data_snapshot.py` | `decide_grade(ratio,has_snap,hour)` 纯函数（分级决策，可单测）；`auto_snapshot()` 重构为分级落盘 + 内建 22:00 强制兜底 + 内部记审计事件（data_snapshot / data_snapshot_partial / data_snapshot_deferred / data_snapshot_forced / data_snapshot_repaired）；`ensure_daily_final()`（22:00 兜底入口）；`create_reconstructed_snapshot(tag)`（补历史占位快照）；`latest_snapshot_path()` 跳过 pit_lost 占位（防回测误当 PIT 基准）；`_is_pit_lost()` |
| `app/updater.py` | 快照段（L1211 一带）删 `_daily_done` 二分支，改 `auto_snapshot(coverage=_cov)` 分级调用（审计事件由 auto_snapshot 内部记，不再双记）；`_close_cli` 22:00 分支（L1218 一带）加 `ensure_daily_final()` 当日最终兜底；`daily_coverage_pending` 事件 note 语义与分级落盘对齐 |
| `tools/backfill_snapshot_days.py` | 新增：补 09-09/10/11 三天占位快照（幂等，已存在跳过） |
| 计划任务 `TianjiCloseCatchup` | 触发器 Repetition.Duration 由 PT6H15M（15:45~22:00）延长至 **PT7H**（15:45~22:45）——保证 22:00 后必有触发轮执行强制兜底（22:00 分支 `_hm>=2200`） |

> 行号说明：任务书锚点 `updater.py:1035-1044` 已过时（F4/E1 改造后行号偏移），实际快照段在 L1211-1256（改动前）。

## §3 分级落盘设计
`decide_grade(ratio, has_snap, hour)` 全组合：

| 条件 | grade | 动作 | manifest | audit |
|---|---|---|---|---|
| 当日已有快照 | exists | 不动 | — | — |
| hour≥22 且无快照 | **forced** | 无条件落盘（宁可带病） | daily_done=ratio≥0.95 | `data_snapshot_forced` **CRITICAL** |
| ratio≥0.95 或未知 | ok | 落盘 | daily_done=true | `data_snapshot` INFO |
| 0.8≤ratio<0.95 | **partial** | **仍然落盘** | daily_done=false + coverage | `data_snapshot_partial` **WARN** |
| ratio<0.8 | deferred | 延后不落，22:00 兜底 | — | `data_snapshot_deferred` INFO |

- 当日已有带病快照（daily_done=false 或 coverage.ratio<0.9）且本次 ratio≥0.95 → 重建 + `data_snapshot_repaired`（F4 语义保留）。
- **"data_snapshot_deferred 不再单独出现"**：deferred 后必有当日 forced（22:00 兜底）或后续 partial/ok（覆盖率追上）——审计链闭环。

## §4 22:00 当日最终兜底
- 触发链：TianjiCloseCatchup 每 30 分钟独立进程触发 `tools/close_update.cmd` → `python updater.py --close` → `_close_cli()`；22:00 后（`_hm>=2200`）先调 `ensure_daily_final()`（→ auto_snapshot hour≥22 → forced 无条件落盘 + CRITICAL），再记 `close_catchup_deadline` 收尾。
- 该 CLI 为**独立新进程**，每次触发加载磁盘最新代码——**不依赖服务重启**，22:00 兜底对运行中的旧服务同样生效。
- 服务重启后，15:10 主流程 run_update 直接用新代码分级落盘（当前真实 ratio=0.937 → partial 带病落盘），更早拿到快照。

## §5 三天补快照（PIT 不可复现声明）
- 09-09/10/11 三天的 market.db 真实状态已被后续更新覆盖，**无法真正还原，不伪造**。
- 已落 `data/snapshots/2026-09-09|10|11/market.db` = **当前库副本** + manifest 标记 `reconstructed=false, pit_lost=true, note="...禁止当 PIT 基准"`，仅供磁盘一致性。
- `latest_snapshot_path()` 已跳过 pit_lost 占位——回测工具不会误取占位副本当 PIT 基准。
- **目录状态异常记录（触碰前查 mtime，不擅动）**：快照目录现有 08-26/08-27/08-31/09-01/09-02/09-03/09-04/09-07/09-09/09-10/09-11/09-13 共 12 个；**08-30 目录缺失、09-13 目录内容为 09-08 快照的副本**（manifest created_at=2026-09-08T15:55:42 旧格式无 daily_done/coverage，目录 mtime 2026-09-08 23:00:07）——疑似既有归档操作（`_archive_pending_del` 机制）所为，非本次改动造成，未触碰，留待归档方核验。

## §6 自检与验证
- 单测 `tmp/b1/test_autosnap.py`：**9/9 通过**——decide_grade 全组合（exists/forced/ok/partial/deferred）+ 集成（mock 库/审计/时间）：ok 落盘 daily_done=true、partial 落盘 daily_done=false+WARN、deferred 不落 + 22:00 forced 落盘+CRITICAL + 幂等 exists、latest 跳过 pit_lost。
- 真实探测 `tmp/b1/probe_real.py`（只读）：ratio=0.9371 → 当前决策 exists（今日 09-13 已有 09-13 目录）；**下一个交易日收盘将走 partial（0.8≤0.937<0.95）→ 带病落盘**——修复目标行为。
- `py_compile`：updater.py / data_snapshot.py / backfill_snapshot_days.py 全部通过；`import app.updater` OK。
- 补快照实跑：09-09/10/11 三天 market.db + manifest 已生成（各 1795.3MB，rows_kline_day=9015368）。

## §7 验收（连续 3 个交易日）
- 当前 2026-09-13（周日），最快验收窗口 **09-14/15/16（周一/二/三）**。
- 验收命令（每个交易日 15:30 后）：
  ```powershell
  Get-ChildItem data\snapshots -Directory | Sort-Object Name | Select-Object -Last 3   # 每天有新目录
  Get-Content data\snapshots\YYYY-MM-DD\manifest.json                                  # 含 daily_done+coverage
  Select-String data\audit\audit.jsonl -Pattern 'data_snapshot_(partial|forced|deferred)'   # deferred 后必有 partial/forced
  ```
- 判据：连续 3 个交易日每天有快照目录+manifest；`data_snapshot_deferred` 不再单独出现（要么当日 partial/ok 落盘，要么 22:00 forced 落盘）。

## §8 诚实披露
1. **服务未重启**：8899 运行中的服务内存里仍是旧 updater 代码，15:10 主流程快照行为在重启前不变化；但 22:00 兜底（独立进程）保证当日有快照。建议由有权进程管理者（X1 通道）在合适时机重启服务加载新代码。
2. **08-30 目录缺失/09-13 目录为 09-08 副本**：非本次改动所致，未追查未修改，已记录待归档方核验。
3. **补的三天快照是占位副本**：PIT 不可复现是数据事实，任何回测不得以 09-09/10/11 快照作为当日数据基准（latest_snapshot_path 已防）。
4. **keep_n=10 保留策略**：新快照落盘触发 `_cleanup` 会按既有策略清理最旧快照（含可能清理 08-26 等），属设计内行为。
5. 非交易日（今日 09-13 周日）`_close_cli` A4 分支直接 exit 0，22:00 兜底只在交易日生效（非交易日无需快照）。
