# F4（P1）收盘链路时序竞争 —— 补做报告（2026-09-09）

规格书：`docs/reports/audit_20260908.md`（P1-1 收盘链路时序：15:36 CRITICAL ratio=0.1797 误报 /
带病快照 / 兜底判据错误 / stale-first 预算静默截断）。
本轮性质：**F4 代码已在 9da181a（2026-09-08 23:50 auto）完整落地**
（`app/updater.py` +235、`app/data_snapshot.py` +52）；本批核查逐条对表 + 交付任务书
要求报告名 `f4_closechain.md`（旧报告 `f4_close_chain_20260908.md` 为本批前交付，可对照）。

## 1. 三缺陷逐条对表（代码在位证据）

### 缺陷① KPI/快照未等收盘更新完成 → 事件驱动（✅ 在库）
- `app/updater.py` L924-957：日K段结束后评估覆盖 KPI，**`_daily_done`（stale-first 追平）为假 → 记
  `INFO daily_coverage_pending`（含 pending_remain/ratio，注明"延后重评、不落快照、由兜底任务按覆盖率接力"），
  不评 CRITICAL/WARN**；追平后才按 R2 阈值分级（ratio<0.8 CRITICAL / <0.9 WARN / ≥0.9 INFO）。
  → 09-08 15:36 那种"更新未完成即 CRITICAL 0.1797"不会再产生。
- `app/updater.py` L1032-1050：快照落盘同样由追平驱动——未追平记 `INFO data_snapshot_deferred`；
  追平后 `data_snapshot.auto_snapshot(coverage=_cov, daily_done=True)` 落盘。
- `app/data_snapshot.py` L113-147：`auto_snapshot(coverage, daily_done)`——当日已有带病快照时
  强制重建 + 补发 **`data_snapshot_repaired`** 事件（任务④ ✅）；manifest 落 `daily_done`/`coverage` 字段（L64-91）。

### 缺陷② 兜底任务判据 triggered_day → 覆盖率（✅ 在库）
- `app/updater.py` L823-831 `close_fallback_needed()`：**当日覆盖率 < CLOSE_FALLBACK_COVERAGE(=0.9)
  或存在未追平票（库内 max<target）→ 强制重跑**，全程 mode=ro 只读判定，幂等。
- 兜底任务 `tools/close_update.cmd` → `python -m app.updater --close` → L1141-1153 用
  `close_fallback_needed()` 结果（`_needed/_ratio/_pending/_thr`）决策，不再只看 triggered_day。

### 缺陷③ stale-first 预算动态化 + 超时 WARN（✅ 在库）
- `app/updater.py` L396-421 `_update_daily_stale_first`：预算 = `n_batch × STALE_FIRST_BATCH_EST_SEC`
  夹在 `[STALE_FIRST_BUDGET_MIN(=600), STALE_FIRST_BUDGET_MAX]`——按待处理票数估算，票池 5328 只不再用旧 480s。
- L460 一带：预算耗尽仍有剩余 → **强制写 `WARN stale_first_budget_exceeded`**（含剩余票数/已用时长），不再静默截断。

## 2. 验证

- 静态核对：updater/data_snapshot 均 ast.parse 通过、CRLF=0（updater 1183 行 / data_snapshot 159 行）。
- 场景推演（按规格书模拟，代码路径级）：
  - **"更新未完成时触发 KPI"**：stale-first 返回 `pending_remain>0` → `_daily_done=False` →
    `daily_coverage_pending` INFO（**非 CRITICAL**）；快照走 `data_snapshot_deferred`。✅ 无 CRITICAL 误报。
  - **"主流程漏跑"**：`close_fallback_needed()` 对库内当日 max(date)<target 或覆盖率<0.9 返回 True →
    `--close` 兜底强制重跑。✅ 与"今日是否触发过"无关。
- 复算锚点：09-08 收盘链时间线（audit_20260908.md §P1-1）→ 在事件驱动下 15:36 应输出
  `daily_coverage_pending`（fresh=510/total=2838/ratio=0.1797/pending_remain>0），15:44 后追平才评 INFO；快照不再带病落盘。

## 3. 备份与交付

- 备份：`tmp/f_backup/20260909_210444/updater.py`（267C638C...）、`data_snapshot.py`（C7A276A4...），9/9 一致。
- 本批未改 updater/data_snapshot（代码已在 9da181a）；本报告为补交付件。
- git diff --stat（本批）：updater/data_snapshot **0 改动**（F4 代码在 9da181a，+235/+52）。
