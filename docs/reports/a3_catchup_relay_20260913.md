# A3 兜底接力闭环 — 验收报告（2026-09-13）

## 0. 问题到方案（一页）

**背景事实**：`tools/close_update.cmd` 原为 15:45 一次性兜底（计划任务 `TianjiCloseUpdate`，Daily 15:45）。
09-11 主更新拖到 20:11 才完成 → 兜底 15:45 早已跑过接不上；且兜底重跑 0 进展
（主流程持 `data/update.lock` 时 `run_update` 直接 skipped）。

**本单方案**：把兜底从"15:45 一次性"升级为**覆盖率驱动的接力**——
`TianjiCloseCatchup` 计划任务在 15:45~22:00 每 30 分钟触发 `close_update.cmd`，
判据在 Python 侧（`app/updater.py --close`）：当日覆盖率 <0.95 或当日无快照才执行更新
（幂等）；达标即 no-op（无网络操作）；22:00 后不再执行并记 WARN 收尾事件。
互斥复用 `data/update.lock`（F2 跨进程 OS 文件锁，天然满足）。

## 1. 改动清单

### 1.1 `app/updater.py`（本次 A3 三处 + 入口重构）
1. **常量**：`CLOSE_CATCHUP_START=1545 / CLOSE_CATCHUP_END=2200 / CLOSE_CATCHUP_TARGET=0.95`（模块级，**未改 config.py**）。
2. **`close_fallback_needed(thr=None, require_snapshot=False)`**：thr 可覆盖达标阈值（接力目标 0.95）；
   `require_snapshot=True` 时"当日无快照"（`data_snapshot.snapshot_path()` 文件不存在）也判 needed。
   默认 thr 仍读 `config.CLOSE_FALLBACK_COVERAGE`（0.9），向后兼容。
3. **`run_update` 同进程续跑（任务 3）**：主更新（`update_daily`）结束后 `pending_remain>0` →
   同一进程内直接续跑一轮（不受 15:45 时刻约束），结果合并（`updated/covered` 累加、
   `pending_remain` 取续跑值），置 `relay_extra=True`；续跑异常置 `relay_extra_error`（不留待兜底）。
   `run_update` 全程持 `update.lock`，续跑无并发写风险。
4. **`_close_cli()`（入口重构）**：原 `__main__ --close` 整块抽为函数（便于单测）：
   A4 强杀留痕 → 非交易日 exit 0 → A3 22:00 窗口收尾（`close_catchup_deadline` WARN/INFO，不执行更新）
   → A3 判据（0.95 + 快照）→ 达标 no-op / 未达标执行（A1 等锁轮询 30s 至 22:00 硬停）→ 复检。
   A1/A4 逻辑全部保留。

### 1.2 `tools/close_update.cmd`
重写为纯 ASCII（**修复 A4 版本的中文乱码**：原文件 UTF-8 中文注释在 GBK 代码页下乱码），
行为不变（`python -m app.updater --close`），头部注释更新为接力语义 + 退出码语义。

### 1.3 计划任务（已注册，2026-09-13）
| 项 | 值 |
|---|---|
| 任务名 | `TianjiCloseCatchup` |
| 触发 | Daily 15:45 起，每 30 分钟，持续 6:15（至 22:00） |
| 命令 | `cmd /d /c "C:\Users\26838\A股模拟盘\tools\close_update.cmd"` |
| 状态 | Ready，Next Run 2026/9/13 15:45:00 |
| 旧任务 | `TianjiCloseUpdate`（15:45 一次性）**已删除**（避免 15:45 双触发） |

**注册命令**（已执行；注册前 `schtasks /query /tn TianjiCloseCatchup` 确认同名不存在）：
```powershell
cmd /c 'schtasks /create /tn "TianjiCloseCatchup" /tr "cmd /d /c \"C:\Users\26838\A股模拟盘\tools\close_update.cmd\"" /sc DAILY /st 15:45 /du 06:15 /ri 30 /f'
schtasks /delete /tn "TianjiCloseUpdate" /f
```

**回滚命令**（一键可批）：
```powershell
schtasks /delete /tn "TianjiCloseCatchup" /f
cmd /c 'schtasks /create /tn "TianjiCloseUpdate" /tr "cmd /d /c \"C:\Users\26838\A股模拟盘\tools\close_update.cmd\"" /sc DAILY /st 15:45 /f'
```

## 2. 验收证据

### 2.1 真实状态实测（2026-09-13，只读查询）
```
target_day = 2026-09-11（最近交易日）
coverage   = {fresh: 2424, total: 5328, ratio: 0.455, missing: 2904}
needed(thr=0.95, require_snapshot=True) = True
snapshot   = data/snapshots/2026-09-13/market.db 不存在
```
**09-11 收盘后仅 45.5% 票追平（2904 只待补）——A3 背景"主更新拖到 20:11 完成、兜底接不上"的直接后果仍在库内**。
今日（周日）为 A4 非交易日设计：`--close` 实测 `[A4] 非交易日...exit 0`（零副作用，未拉网、未写库、未写 audit）。
接力机制自 09-14（周一）15:45 起生效：若主更新未追平，接力任务将把 ratio 拉到 ≥0.95 并落快照。

### 2.2 模拟验收（tmp/a3_sim.py，全部通过 → `A3_SIM_ALL_OK`）
| 场景 | 模拟输入 | 断言 | 结果 |
|---|---|---|---|
| S1 未达标接力 | ratio=0.455 + 无快照 + 15:45 | 执行 run_update | ✅ 触发 |
| S2 已达标 no-op | ratio=0.991 + 快照存在 | 不调 run_update，打印"无网络操作" | ✅ |
| S3 22:00 收尾 WARN | 22:01 + 未达标 | 记 `close_catchup_deadline` WARN、不执行更新、exit 0 | ✅ |
| S3b 22:00 收尾 INFO | 22:01 + 已达标 | 记 `close_catchup_deadline` INFO | ✅ |
| S4 同进程续跑 | update_daily 首轮 pending=42 | 续跑第二轮、`relay_extra=True`、合并 updated=3042、pending→0 | ✅（update_daily 调用 2 次） |
| S5 快照判据 | ratio=0.98 但无快照 | `needed=True`（快照缺失单独触发） | ✅ |

验收方法说明：不改库、不拉网、不碰 `data/audit/*`（`audit.record` 全程 mock）；
S1-S3/S3b/S5 直接单测真实函数 `_close_cli()`/`close_fallback_needed`，
S4 单测真实 `run_update`（mock 数据源层）。真实"20:00 完成+pending>0→22:00 前达标落快照"
需等交易日实跑，模拟已验证判据与执行路径。

### 2.3 验证命令
```
py -3.13 -m py_compile app/updater.py tmp/a3_sim.py      # ✅ PY_COMPILE_OK
py -3.13 tmp/a3_sim.py                                    # ✅ A3_SIM_ALL_OK
py -3.13 -m app.updater --close                           # ✅ 非交易日 exit 0（零副作用）
schtasks /query /tn "TianjiCloseCatchup" /fo LIST /v      # ✅ Start 15:45 / Repeat 30min / Until 6:15
```

## 3. 与既有机制的关系
- **F4（09-08）**：覆盖率事件驱动（`daily_coverage_pending`/`daily_coverage`/`data_snapshot_deferred`）
  已就位；A3 在其上补"15:45~22:00 每 30 分钟接力 + 0.95 目标 + 快照判据 + 同进程续跑"。
- **A1（09-13）**：等锁轮询（30s/次、22:00 硬停 + `close_fallback_lock_wait_timeout` WARN）已落地；
  A3 保留并与其 22:00 边界一致（入口 22:00 后直接收尾退出，不再进等锁循环）。
- **A4（09-13）**：非交易日跳过 + 中断留痕已落地；A3 保留（收尾判定在非交易日之后）。
- 更新期间互斥：接力任务与主更新共用 `update.lock`（F2），无额外锁。

## 4. 未做的事（留给验收/后续）
1. 未真实执行收盘更新（今日非交易日；09-14 起由主更新 + 接力任务实跑验证）。
2. 未改 `app/config.py`（`CLOSE_FALLBACK_COVERAGE=0.9` 保留；接力目标 0.95 在 updater 模块级）。
3. 未碰 `data/audit/*`、`tmp/watchdog.log`、`data/app.lock`；未重启/未 kill 任何进程。
4. `TianjiCloseUpdate` 已删除（属本单"改兜底任务"范围），回滚命令见 §1.3。
5. 09-11 的 2904 只缺口：留给 09-14 主更新 + 接力任务闭环（若 15:10 主更新仍未追平，
   接力任务会在 22:00 前补达标并落快照；此过程将是接力机制的首个真实运行验证）。

## 5. 提交
- `git commit 12660c3`：A3: 兜底接力闭环（覆盖率驱动 15:45-22:00 + 同进程续跑 + 22:00 收尾 WARN）
  （3 files：app/updater.py、tools/close_update.cmd、tmp/a3_sim.py）
