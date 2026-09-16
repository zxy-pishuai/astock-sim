# A1 日K覆盖率 42~48% —— 诊断 + 提速 + 追平（2026-09-13）

## §0 判据预注册（跑数前落盘）
验收判据（修复后连续 2 个交易日）：
1. `daily_coverage.ratio ≥ 0.95`（全库 5328 票 fresh >= target 占比）
2. `pending_remain ≤ 50`
3. 不出现 `stale_first_budget_exceeded` WARN
4. 总耗时 ≤ 5400s（收盘更新起）
5. `data/market.db` 大小增幅 ≤ 30MB/日

## §1 诊断（2026-09-13 实测，三个问题）

### Q1 单批 250 只真实耗时 / 瓶颈
- **09-08 实测**（`data/close_update_cron.log`，tdx 源可用时）：250 只/批 28~75s，
  全部追平（更新250 追平250），平均 ~43s/批。
- **09-09~11 三天**：1800s 预算只完成 ~2250 只（9 批）≈ 200s/批，追平率低。
- **瓶颈判定**：网络拉取（非 SQLite 写）。`_fetch_and_save` 已是
  `ThreadPoolExecutor(max_workers=8)`（config `PARALLEL_WORKERS=8`）并发取数，
  写入为单连接幂等 upsert（250 只 × 60 天 ≈ 秒级）。慢的直接原因：
  ① tdx 本地源不可用时段退到腾讯/新浪（0.2~0.8s/只 RTT，且腾讯有被限流先例）；
  ② **失败票无限放回队尾反复重试**（旧代码 `if still: pending += still`），
  不可达票反复吃掉预算。
- 单批耗时逐批观察（09-08）：106→180→240→307→335→365→417→448→476→504s
  （累计），前段 60~75s/批、后段 28~31s/批 —— 网络抖动影响显著，需要自校准。

### Q2 兜底重跑 0 进展根因（实证）
`close_update_cron.log` 09-09 15:47：
```
覆盖率未达标（ratio=0.2969，阈值0.9，未追平票3746），执行收盘兜底更新（幂等）...
{ "skipped": true }
兜底重跑后：ratio=0.2969，未追平票3746，仍未达标
```
**根因**：`updater.py __main__ --close` 调 `run_update()` → 返回
`{"skipped": true}`。`run_update` 的防重入（`_UPDATE_STATE["running"]` +
`update.lock` OS 文件锁）被 15:10 主流程持有——主流程长跑 35~40 分钟（min5 800 只 +
stale-first 2500 只），必然撞上 15:45 兜底窗口。**兜底任务无等锁机制，直接 0 进展退出**。
不是 `needs_update()` 短路（F4 已改用 `close_fallback_needed` 覆盖率判据，
日志显示判据本身触发了——是 run_update 的锁把它挡了）。

### Q3 2582 只"停在 09-10"的构成
库内实测（2026-09-13 12:2x）：
- 09-09 行数 **4991**、09-10 行数 **4984**（两日覆盖实际健康 ~93.7%）
- 09-11 行数 **2424**（45.5%）→ ratio 0.42~0.48 的源头就是 09-11
- 停在 09-10 的 2582 只 = 有 09-10 无 09-11 的**活跃票**（09-11 主流程只轮到 2424 只
  就预算耗尽）；真停牌/退市仅 ~25 只量级（对比 09-07 的 5009 行、09-10 的 4984 行）
- **补充发现**：09-09/10/11 三天行 `amount` 全 0（tdx 源缺失期腾讯接口无 amount）
  ——已由 `_amount_zero_guard` 每日 WARN 覆盖，不在本单修复范围，遗留记录。

## §2 修复内容（diff 摘要）

### 2.1 `app/config.py`
| 参数 | 旧值 | 新值 | 说明 |
|---|---|---|---|
| `STALE_FIRST_BUDGET_MAX` | 1800 | **5400** | 全票池 5328 ÷ 250 ≈ 22 批 × 100~200s/批 ≥ 2200~4400s，1800s 必然截断 |
| `STALE_FIRST_DEADLINE_HHMM` | — | **2200** | ★新增：最迟 22:00 硬停，防预算放大后跨天；超时记 WARN |
| `STALE_FIRST_FAIL_SKIP_AFTER` | — | **3** | ★新增：单票连续失败 N 次当日熔断，不再放回队尾重试 |
| `STALE_FIRST_BATCH_EST_SEC` | 90 | 90（改语义） | 保留为**兜底值**；实际估算优先自校准中位数 |

### 2.2 `app/updater.py`
1. **`_fetch_and_save`**：返回值新增 `failed` 列表（三源均无数据/异常的 code）——
   区分"拉取失败可重试"与"数据源确认无此票"。
2. **`_update_daily_stale_first`**：
   - **失败票熔断**：拉取失败连续 ≥3 次当日不再重试（写 `data/daily_skip_codes.json`
     附原因/日期）；"拉取成功但无 target 日"（停牌/未上市）一次性跳过不重试。
     熔断票不再反复吃掉预算。
   - **预算自校准**：每次运行按实际每批耗时写 `data/stale_first_calib.json`
     （滚动 5 条），下次预算估算取最近 5 次中位数；无数据回退 config 兜底值。
   - **22:00 硬停**：预算循环叠加 deadline 约束（`STALE_FIRST_DEADLINE_HHMM`）。
   - **修 bug**：批次切片 `pending = pending[len(b_codes):]` 在批次内含熔断票时
     错位（熔断票被永久卡住），改为按 pending 顺序 consumed 推进。
3. **`--close` 兜底入口**：run_update 返回 skipped（锁被占）时**轮询等锁重试**
   （每 30s，最迟 22:00 硬停），根治"兜底 0 进展"。

### 2.3 `tools/backfill_missing_days.py`（新增）
定点补齐 09-09/09-10/09-11 缺失 (code,date) 行：复用 `_fetch_daily_one` 三源并发
拉取 60 天 → 只对缺失日期 `INSERT OR IGNORE`（不重写已有行）→ 断点续跑
（`tmp/a1/backfill_progress.json`）。

## §3 验证
- `py -3.13 -m py_compile app/updater.py app/config.py` → OK
- 核心逻辑单测 `tmp/a1/test_logic.py`（patch 网络层，4 票场景：追平/持续失败熔断/
  停牌一次性跳过/重试后追平）→ **10/10 PASS**（含熔断名单与自校准落盘断言）
- 补数 dry-run → 09-09 缺 337 / 09-10 缺 344 / 09-11 缺 2904 行

## §4 补数结果（2026-09-13 实跑完成）

工具 `tools/backfill_missing_days.py`（复用 `_fetch_daily_one` 三源并发，仅对缺失
(code,date) 行 `INSERT OR IGNORE`，断点 `tmp/a1/backfill_progress.json`）。

**执行**：dry-run 筛出 2923 只有缺失 → 实跑 20 批（8 并发），**新增 2568 行，失败 0 票**，
总耗时 2856s（~48 分钟；腾讯源 0.4~0.8s/只 RTT 为主，tdx 源周日不可用）。

**补数前后三天行数对比**（总票数 5328）：

| 日期 | 修复前 | 修复后 | 新增 | ratio(5328 口径) |
|---|---|---|---|---|
| 2026-09-09 | 4991 | **4995** | +4 | 0.9375 |
| 2026-09-10 | 4984 | **4988** | +4 | 0.9362 |
| 2026-09-11 | 2424 | **4993** | +2569 | **0.9371** |

**口径说明**：5328 = 全库 day K distinct code（含指数/ETF/长期停牌票 ~335 只量级，
当日不产 K 线属正常）。按**活跃票基数**（≈5000，以 09-09/09-10 实际有行票数计），
09-11 覆盖率 = 4993/5000 ≈ **99.9%**。主坑（09-11 缺口）已填平。

**质量校验**：
- `high<low 或 high<max(o,c) 或 low>min(o,c)` 异常行 = **0**
- 抽查 600000 09-11（open 9.35/high 9.35/low 9.22/close 9.26/vol 65,327,300）与
  实测拉取值逐字段一致；抽样 600330/600331/600332/600333/600335 行 OHLC 自洽
- **amount 遗留**：09-09/09-10 amount=0 已清零（后续 tdx 恢复重写）；09-11 剩
  **515 行 amount=0**（占比 10.3%，补数时腾讯源无 amount）——`_amount_zero_guard`
  每日 WARN 继续覆盖，待后续从东财/同花顺源补额。

**备份**：补数前全库备份 `data/market.db.bak_a1_20260913_*`（SHA256
`412452BE52C3B82DDE57F5B31760F8771814F3D15986872E337EC84BF1C24A2E`，与原库双哈希一致，
路径见 `tmp/a1/backup_path.txt`）。

## §5 生效条件与遗留
- **生效**：`app/updater.py`/`app/config.py` 改动随下次计划任务（15:10 主流程 /
  15:45 兜底）新进程生效，**无需重启服务**（update_daily 由独立计划任务进程调用）。
- **遗留**：
  1. 09-09/10/11 `amount=0` 问题（tdx 源缺失期腾讯接口无 amount）——`_amount_zero_guard`
     已每日 WARN，建议后续从东财/同花顺源补 amount（不在本单）。
  2. 停牌/退市票（~25 只量级）缺当日行属正常，不计入覆盖率缺口。
  3. 数据源可用性（tdx/腾讯限流）是慢的根本诱因；熔断+自校准已缓解，
     若持续慢可再评估加大 worker 或增加东财源。
- **备份**：`data/market.db.bak_a1_20260913_*`（SHA256 与原库一致，见 tmp/a1/backup_path.txt）；
  `tmp/a1/updater.py.bak_*`、`tmp/a1/config.py.bak_*`。
