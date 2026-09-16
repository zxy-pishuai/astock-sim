# F4（P1）收盘链路时序竞争修复 —— 2026-09-08

- 任务书：F4（P1）收盘链路时序竞争
- 交付日期：2026-09-08（周一，交易日）
- 改动文件：`app/updater.py`、`app/data_snapshot.py`、`app/config.py`（只新增开关）
- 验证方式：AST 语法 + CRLF + 运行时 import + 4 个离线模拟（不触网、不写生产数据）
- 生效状态：**代码已就位，需服务下次重启生效**（运行中 PID 13316 仍为旧码，见 §6）

---

## 1. 事故时间线（独立复算，非采信）

以 `data/audit/audit.jsonl`、`data/close_update_state.json`、`data/snapshots/2026-09-08/manifest.json` 独立取证，与任务书一致：

| 时刻 | 事件（audit 原文/文件状态） | 含义 |
|---|---|---|
| 15:10:03 | `close_update_state.json` 写入 `triggered_day=2026-09-08`（mtime 15:10:03） | 引擎触发收盘更新 |
| 15:36:01 | `daily_coverage CRITICAL fresh=510 total=2838 ratio=0.1797 daily_updated=500` | **日K 段预算耗尽即评 CRITICAL**（实际更新未完成） |
| 15:36:25 | `data_update INFO min5_updated=1007/1011 daily_updated=500` | min5 段达标 |
| 15:39:38 | `data_snapshot WARN created=true "快照带病：日K覆盖 ratio<0.8"` | **覆盖未达标即落带病快照** |
| ~15:44 | stale-first 日志：已用 504s/480s 仍剩 501 只 | 预算固定 480s（按旧票池 2838 只定）必然超时 |
| 15:45:01 | TianjiCloseUpdate → needs_update=False → 跳过（exit=0） | **兜底只看标记不看覆盖率** |
| 15:51~55 | 人工脚本收尾 + 快照 15:55:42 重建 | 补救 |
| 收盘后 | kline 09-08 实况：**4992 只 / 库内 5328 票**（99.5% 实际健康） | 留痕 CRITICAL 为误报 |

> 旁证：今日快照 `data/snapshots/2026-09-08/manifest.json` 为旧格式（人工 15:55 重建，无 `daily_done`/`coverage` 字段）；`market.db` mtime 23:00:07（收盘后有他块进程触碰，非本块所为，如实记录）。

> 未解之谜（诚实披露）：15:36:01 时 `_daily_coverage.total=2838`，而收盘后 GROUP BY 全库为 5328。`_daily_coverage` 口径就是全库 GROUP BY 票数，二者矛盾——疑评估时点库处中间态（收盘更新/人工写入竞争，读事务视图不一致）。本修复把评估时机从"日K段结束后立即"改为"追平事件后"，从机制上消除此类时点歧义；成因未深究（不影响修复）。

## 2. 根因（三个具体缺陷）

1. **KPI 评估与快照落盘没等收盘更新完成**：`run_update()` 内日K段（`_update_daily_stale_first`）预算耗尽即返回，随后**立即**评估 `daily_coverage`（ratio<0.8→CRITICAL）并落快照 → 每日必然误报 + 带病快照。
2. **兜底判据错误**：`TianjiCloseUpdate` → `python -m app.updater --close` 只看 `needs_update()`（`update_state.last_day != 今天`）。主流程 15:10 触发后 min5 达标即标记 last_day，即使日K 没追平，15:45 兜底也跳过。
3. **stale-first 预算静态 + 静默截断**：`budget_sec=480` 按旧票池（2838 只）定；扩池 5328 只后必然超时；超预算无任何 WARN 事件。

## 3. 修复方案（按任务书 5 子任务对照）

### ① 覆盖率 KPI 与快照由"收盘更新完成"事件驱动 ✅
- `run_update()`：日K段返回 `pending_remain>0`（未追平）时：
  - KPI 记 **INFO `daily_coverage_pending`**（fresh/total/ratio/pending_remain，note 明示"延后重评"），**不再评 CRITICAL/WARN**；
  - 快照**不落盘**，记 INFO `data_snapshot_deferred`（pending_remain）；
  - 追平（`pending_remain==0`）后才走原 R2 阈值分级评估 + 落快照。
- 若要在固定时刻评估（如兜底任务），必须先查覆盖率/未追平票（见②），进行中/未完成 → 延后。

### ② 兜底判据 `triggered_day` → 实际覆盖率 ✅
- 新增 `updater.close_fallback_needed()`：`needed = 覆盖率<CLOSE_FALLBACK_COVERAGE(0.9) 或 存在未追平票（库内 max<target）`；全程只读（`_daily_coverage`/`_daily_max_dates` 均 mode=ro）。
- `--close` 入口改用它：未达标 → `run_update()` 强制重跑（自带 update.lock 跨进程互斥 + upsert 幂等），跑后复查覆盖率并打印结论。

### ③ stale-first 预算动态化 + 无进展中止 + 超预算必须 WARN ✅
- `_update_daily_stale_first`：`budget_sec=None` 时按待处理票数估算 `ceil(pending/batch) × STALE_FIRST_BATCH_EST_SEC(90s)`，夹在 `[STALE_FIRST_BUDGET_MIN(600), MAX(1800)]`（5328 只 → 22 批 → clamp 1800s；零星票 → clamp 600s）。
- **无进展才中止**：连续 2 轮零更新且零追平 → 提前 break（防死循环空转），返回 `no_progress_stop=True`。
- 超预算仍有剩余 → **必须 WARN `stale_first_budget_exceeded`**（pending_remain/elapsed_s/budget_sec），不再静默截断。

### ④ 快照重建后补发 `data_snapshot_repaired` ✅
- `data_snapshot.auto_snapshot(coverage, daily_done)`：当日已有快照且为"带病"（`manifest.daily_done=False` 或 `coverage.ratio<0.9`，**或 manifest 缺失/损坏=旧格式，保守视为带病**）且本次追平 → `create_snapshot(force=True)` 重建 + audit `data_snapshot_repaired`（含 rebuilt_from/coverage_ratio）。
- `create_snapshot` 的 manifest 新增 `daily_done`/`coverage`，供后续判定。

### ⑤ 两个模拟验证 ✅（见 §5）
- SIM1"更新未完成时触发 KPI 不误报"、SIM2"主流程漏跑兜底按覆盖率重跑"均 PASS；另补 SIM3（stale-first 动态预算/无进展/WARN）与 SIM4（带病快照重建）。

## 4. 改动清单

| 文件 | 改动 | 备份 |
|---|---|---|
| `app/updater.py` | stale_first 预算动态化+无进展中止+WARN；run_update KPI/快照完成事件驱动；新增 `close_fallback_needed()`；`--close` 判据改覆盖率 | `tmp/pack28/updater.py.bak`（SHA256 3C23D2F2A8E93845…，源/备 match） |
| `app/data_snapshot.py` | manifest 增 `daily_done`/`coverage`；auto_snapshot 带病重建 + repaired 事件 | `tmp/pack28/data_snapshot.py.bak`（58ADE10B753B2D69…） |
| `app/config.py` | 只新增 4 开关：`CLOSE_FALLBACK_COVERAGE=0.9`、`STALE_FIRST_BATCH_EST_SEC=90`、`STALE_FIRST_BUDGET_MIN=600`、`STALE_FIRST_BUDGET_MAX=1800`；既有键零改动 | `tmp/pack28/config.py.bak`（A7549B6C2354FDF3…） |

`git diff --stat`（本块收尾时点）：`app/updater.py +59/-?、app/data_snapshot.py +4/-?` 为工作区 M；**config.py 的 F4 开关已被 23:50 TianjiGit_Nightly auto 提交（HEAD 9da181a）收编**（`git diff app/config.py` 为空 = 已入 HEAD）；其余 `data/audit/audit.jsonl`、`data/backfill_amount_state.json`、`data/quality_alert.jsonl`、`tmp/*`、`data/keypack/keypack_20260908_235324.tar.gz` 为进程/他块产物，非本块写面。行数：updater 1177→1184、data_snapshot 158→160、config 660→673；全部 CRLF=0、LF。

## 5. 验证

- AST：三文件 `ast.parse` PASS；CRLF=0（f4_syntax_check.py）。
- 运行时：`from app import config/data_snapshot/updater` IMPORT-OK；`needs_update()=False`（今日已标记，正确）；新开关可读。
- **SIM1**（tmp/pack28/f4_sim1_kpi_no_false_critical.py）PASS：mock 未追平 r2（pending_remain=501）+ 低覆盖 ratio=0.1797 → 事件= `daily_coverage_pending(INFO)`/`data_update`/`data_snapshot_deferred(INFO)`；**无 daily_coverage CRITICAL、无 data_snapshot、auto_snapshot 未被调用**。
- **SIM2**（f4_sim2_fallback_coverage_gate.py）PASS：A 低覆盖→needed=True；B 高覆盖零缺口→False；C 高覆盖但有未追平→True；D 覆盖查询异常→True（保守重跑）。
- **SIM3**（f4_sim3_stale_first_dynamic.py）PASS：全失败 2 轮 → `no_progress_stop=True`、elapsed≈0 未跑满预算；预算 clamp 下限 600；5328 只估算→22 批→clamp 上限 1800；WARN `stale_first_budget_exceeded` 含 pending_remain/elapsed_s。
- **SIM4**（f4_sim4_snapshot_repair.py）PASS：①带病+追平→force 重建+repaired；②健康+追平→不重建；③带病+未追平→不重建；④无快照→新建（force=False）；⑤旧格式无 manifest→保守重建+repaired。

## 6. 生效状态与部署建议

- **生效**：三个文件为源码改动，运行中服务（PID 13316，22:45:11 启动）仍执行旧码 → **下次重启 main.py 后生效**（重启属高危动作，需验收方/人类批准，本块未动进程）。
- 计划任务 `TianjiCloseUpdate`（15:45 → `tools/close_update.cmd` → `python -m app.updater --close`）**无需改注册**：cmd 调用入口不变，判据在 updater 内已改。
- 预期效果（下个交易日）：15:10 run_update 日K 预算 1800s 内尽力追平；未追平 → INFO pending + WARN 超预算 + 不落带病快照；15:45 兜底按覆盖率接力重跑；追平后 KPI 正常分级 + 快照落盘（今日旧格式快照将被一次性重建并补 `data_snapshot_repaired`）。
- 残留风险（建议后续）：1800s 上限在全市场首日（5328 只）仍可能不足，追平依赖 15:45 兜底接力；若 15:45 时主流程仍在跑（锁占用），兜底会 skipped 且无重试——已用 WARN/pending 留痕暴露，未做自动重试（超出本任务范围，见披露）。

## 7. 诚实披露

- 未在真实收盘链路跑过新逻辑（不触网/不写库纪律）；4 个模拟为全 mock 控制流验证，判定逻辑与真实代码路径一致。
- `needs_update()` 语义保留（只读 last_day 标记），仅 `--close` 不再依赖它。
- 兜底任务锁占用时无自动重试（如上）；"主流程漏跑"模拟覆盖了判据层，未模拟"锁占用+兜底 skipped"组合。
- `total=2838` 之谜未深究（见 §1）。
- `data/backfill_amount_state.json`/`tmp/*` 等 git diff 噪音为进程/他块产物，本块未触碰；config.py 的 F4 开关在 23:50 nightly auto 提交已入 HEAD（updater/data_snapshot 在 nightly 之后完成，工作区 M，待下次提交）。
