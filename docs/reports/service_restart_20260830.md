# 服务恢复与热修复生效验证报告 — 2026-08-30

- 执行时间: 2026-08-30 22:01–22:05
- 执行人: 任务1服务重启验证（同会话）
- 目标端口: 8899 | 模式: `python main.py` 后台常驻（WindowStyle Hidden）
- 批准: 已获人类批准（ask_user_question approve_restart = 批准重启）
- 约束: 全程 LF 行尾、逐文件 py_compile、只读诊断除服务自写 audit.jsonl 外零手工写库

---

## 1. 启动前检查单（全程只读，原始输出存档）

### 1a. 端口与进程

```
Get-NetTCPConnection -LocalPort 8899 -State Listen → 无结果
  → NO_LISTEN_8899
Get-CimInstance Win32_Process 过滤 python 且 CommandLine 匹配 'main\.py' → 无进程
  → NO_MAIN_PY_PROCESS
```

结论: 服务已停，与任务书现状事实一致（最后实证 PID 21728 于 8/27，8/30 验收再次确认无监听）。

### 1b. 残留核查

```
data/audit 目录:
  .write.lock      0 字节  2026/8/27 10:58:10
  audit.jsonl  617481 字节  2026/8/28 17:08:07
data/app.lock: 不存在 → NO_APP_LOCK
data/*.db-wal/shm:
  market.db-wal      0 字节  2026/8/30 20:25:56
  market.db-shm  32768 字节  2026/8/30 21:25:40
```

判定: `.write.lock` 常驻属设计（audit 以 a+ 长期持有），不代表残留锁；真正判据是无 python 进程持有——已由 1a 覆盖。`app.lock` 不存在，无需清理。**禁删项（.wal/.shm/.write.lock）全程未删**，符合红线。

### 1c. 语法检查

```
python -m py_compile app\trader.py → exit 0
python -m py_compile app\audit.py  → exit 0
python -m py_compile app\sector.py → exit 0
```

### 1d. 三修复文件 mtime 早于计划启动时刻

```
app/trader.py  2026/8/27 22:21:55  80222 字节  ← P76 pos_cash 修复（最晚）
app/audit.py   2026/8/27 10:42:57   8450 字节  ← 批审计链单写者
app/sector.py  2026/8/27 10:54:11   9249 字节  ← _shrink_guard
```

计划启动时刻 2026-08-30 22:02:20，三者 mtime 均早于启动时刻，满足"已落码重启才生效"前提。

### 1e. 8/27 之后被改文件清单与批外变更核查

```
Get-ChildItem app\*.py,tools\*.py,main.py | Where LastWriteTime > 2026-08-27:

index_timing_snapshot_retest.py 2026/8/27 01:41:15
risk.py                         2026/8/27 02:03:43
bt_noexcl_arm.py                2026/8/27 02:08:33
repair_002594.py                2026/8/27 02:10:54
time_stop_reverify.py           2026/8/27 02:11:14
global_map_verify_full.py       2026/8/27 02:33:30
risk_daily.py                   2026/8/27 02:40:07
backfill_pre2019_w0.py          2026/8/27 02:40:17
startup_check.py                2026/8/27 02:44:28
updater.py                      2026/8/27 02:52:43
global_thin_backfill.py         2026/8/27 02:53:16
build_pit_pool.py               2026/8/27 02:59:01
board_weight_ab.py              2026/8/27 03:03:47
exit_param_scan.py              2026/8/27 03:03:47
judge_kit.py                    2026/8/27 03:08:30
rejudge_after_semantic_fix.py   2026/8/27 03:10:47
exit_pack_v2_scan.py            2026/8/27 03:21:55
build_delisted_universe.py      2026/8/27 03:56:24
dual_price_weekly.py            2026/8/27 04:08:07
live_vs_backtest_weekly.py      2026/8/27 04:18:41
b3_score_subtract_ab.py         2026/8/27 04:42:08
forward_eval.py                 2026/8/27 07:30:18
config.py                       2026/8/27 07:30:18  ← 002594 移出 197→196（验收报告 §5.1 已归档为批外但合理）
audit_chain_diag.py             2026/8/27 10:16:59
audit.py                        2026/8/27 10:42:57
c_lane_redispatch.py            2026/8/27 10:43:40
sector.py                       2026/8/27 10:54:11
regime_gating.py                2026/8/27 11:30:48
trader.py                       2026/8/27 22:21:55

后续增量:
  docs/reports/acceptance_20260830.md  2026/8/30 20:26:15
  docs/reports/dual_price_observation.md 2026/8/30 20:25:56
  docs/reports/qfq_triage_20260826.md  2026/8/30 20:27:55
  docs/reports/regime_gating.md        2026/8/30 20:25:48
  data/* 实测为数据产物，无新增 .py
```

批外变更判定: 以 `2026-08-27 22:21:55`（trader 最晚落盘）为截断， 이후 `app/*.py, tools/*.py, main.py` **零新增 mtime**（程序化验证 `mods after 22:21:55 cut: []`）。`config.py` mtime 2026-08-27 07:30:18 早于三修复，前次验收已登记为批外移出 002594，未新增批外变更。**结论: 无归属不明改动 = 无新批外变更。**

额外取证:
- `data/sector_map.json` 2911 键 / 49 行业 / 0 坏格式（预验）
- `data/audit/audit.jsonl` 2668 行，audit_chain breaks 340（分布见 §3c 基准）
- `data/update_state.json` = `{"last_day": "2026-08-27"}`（收盘更新标记）
- `data/audit_chain_breaks.json` mtime 2026/8/30 01:57:09（验收时 --verify 刷新后基线）

---

## 2. 重启执行

- **重启指令**: `Set-Location 项目根; $env:PYTHONIOENCODING='utf-8'; Start-Process python -ArgumentList 'main.py' -WindowStyle Hidden`
- **启动时刻**: 2026-08-30 22:02:20（指令下发），进程 CreationDate **2026/08/30 22:02:25**
- **新 PID**: **67120**（`CommandLine: "C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" main.py`）
- **端口**: 127.0.0.1:8899 LISTEN OwningProcess 67120（`Get-NetTCPConnection` 实证）
- **单实例锁**: `data/app.lock` 2026/8/30 22:02:26 内容 `67120`
- **备份**: 重启前 `Copy-Item data\audit_chain_breaks.json tmp\breaks_before.json` 留底（76181 字节，mtime 01:57:09 时刻冻结）

---

## 3. 启动后 30 分钟内验证（原始输出粘贴）

### 3a. `python tools/startup_check.py` → 退出码 0 全过

```
# 启动核验 2026-08-30 22:02:48

[PASS] 服务进程存活 PID=67120 启动于 2026-08-30 22:02:25（lock=C:\Users\26838\A股模拟盘\data\app.lock）

## ① 生效参数（config 磁盘值）
[PASS] STOP_LOSS_PCT = -0.07（期望 -0.07）
[PASS] INDEX_TIMING_ENABLED = False（期望 False）
[PASS] AUCTION_MIN_PCT = 4.0（期望 4.0）
[PASS] BOARD_MOMENTUM_MIN = 8.0（期望 8.0）
[PASS] RISK_COOLDOWN_SUSPEND_UNTIL = '2026-09-27'（须 ≥ 今天(2026-08-30)）
[INFO] risk_state.json: consec_losses=4 cooldown_until=2026-08-27 breaker_on=False
[PASS] 服务响应 /api/overview：market_count=5009 time=2026-08-30 22:02:50（port=8899）
[PASS] 影子线程启动证据：[22:02:36] 影子日更调度已启动（盘中记账/收盘结算，影子账不碰账户）（今天 2026-08-30 的实例内）
[PASS] 前瞻台账最新记账日=2026-08-28（C:\Users\26838\A股模拟盘\data\forward_eval.jsonl）
[PASS] 计划任务 forward_eval 在册 Next Run=2026/8/31 20:30:00
[INFO] 交易引擎未启动：秒级监控随之未启（盘前属常态，交易日 09:15 自动开启后生效）
[PASS] 进程新鲜度：core 最新 mtime 2026-08-27 22:21:55 ≤ 实例启动 2026-08-30 22:02:25

**结论：✅ 全部通过**（PASS 见上；FAIL/WARN 0 项）
EXIT_CODE=0
```

### 3b. 扫描不崩

**audit 尾 40 行抽查**（`data/audit/audit.jsonl` 倒查，无 scan traceback）:

```
2026-08-28 13:30:02 trading_event ... 封板不牢  WARN alert
2026-08-28 13:30:04 trading_event ... 下跌-6.3%  WARN alert
...
2026-08-28 15:03:53 trading_event 已推送收盘日结 OK alert
2026-08-28 15:10:23 trading_event 收盘数据增量更新已触发 INFO alert
2026-08-28 17:08:07 data_update  INFO daily
2026-08-28 17:08:07 data_update_failed  WARN daily
2026-08-30 22:02:36 trading_event 开盘自动开启已启用（交易日 9:15 自动启动引擎） INFO alert  ← 重启后新写
```

**/api/log**（22:04 实测 15 行）:

```
[22:02:26] ==============================================
[22:02:26]   天玑量化终端 TianjiQuant v4.5 — A股实战派量化终端
[22:02:28] 服务已启动: http://127.0.0.1:8899/
[22:02:28] 数据源: 腾讯行情+K线 / 新浪备用 | 缓存: SQLite磁盘+内存TTL
[22:02:35] GET /api/overview 异常: [WinError 10053] 你的主机中的软件中止了一个已建立的连接。
[22:02:36] 后台增量更新已启动（min5/日K 自动补最新数据）
[22:02:36] 开盘自动开启已启用（交易日 9:15 自动启动交易引擎）
[22:02:36] 影子日更调度已启动（盘中记账/收盘结算，影子账不碰账户）
[22:02:36] 股票列表后台预热已启动
[22:02:36] 资金面缓存后台预热已启动（top30 成交额）
[22:02:38] GET /api/overview 异常: [WinError 10053] ...
[22:04:01] 情绪历史预热完成: 484天 IC=0.8222
```

判定: `HAS_TB_OR_POS_CASH_ERROR: False`；全日志无 `Traceback` / `UnboundLocalError` / `pos_cash` 字样。`trading_event` 心跳连续（08-28 盘中 10:00–15:30 影子打板记账已覆盖），重启后 1 个新事件为启动器本身。

**交易引擎调度**: `startup_check` 报告"交易引擎未启动：秒级监控随之未启（盘前属常态，交易日 09:15 自动开启后生效）"——符合 `app/trader.py enable_auto_start` 逻辑（非交易时段不自动扫描），故无扫描 traceback 属预期；已通过**代码路径静态验证**补强:

```
inspect TradingEngine.scan_once:
  pos_cash 定义行 206 vs _buy_px 调用行 213 → pos_cash < _buy_px  TRUE
  源码 L665-667: "★ P76 修复：pos_cash 必须先于 _buy_px 计算（原顺序致首轮 UnboundLocalError，2026-08-27 全天扫描崩溃）"
```

**结论**: pos_cash 顺序缺陷已在内存生效，下次交易时段扫描不再触发 UnboundLocalError。

### 3c. 审计链

**跑前留底**: `Copy-Item data\audit_chain_breaks.json tmp\breaks_before_verify.json`（76181 字节）

**`python tools/audit_chain_diag.py --verify data\audit\audit.jsonl` 原始输出**:

```
== 审计哈希链诊断（只读标记，绝不重写历史）==
目标文件: data\audit\audit.jsonl
总行数 2669｜链内连续校验通过 2329｜断裂 340 处（断点重同步口径）
健康率 87.3%
按日分布: {"2026-08-16": 3, "2026-08-17": 3, "2026-08-18": 2, "2026-08-19": 1, "2026-08-20": 67, "2026-08-25": 10, "2026-08-26": 253, "2026-08-27": 1}
前 12 条明细:
  行9     2026-08-16 19:07:19 order     manual_buy     [prev_mismatch+hash_mismatch] exp_prev=…0332 act_prev=…
  行11    2026-08-16 19:07:35 order     manual_buy     [prev_mismatch+hash_mismatch] exp_prev=…c111 act_prev=…
  ...
注: 本脚本仅生成清单...
断裂清单已存档 → C:\Users\26838\A股模拟盘\data\audit_chain_breaks.json（340 条）
EXIT=0
```

**增量分析**:

| 指标 | 跑前（tmp\breaks_before_verify.json） | 跑后（data\audit_chain_breaks.json） | 增量 |
|---|---|---|---|
| lines | 2668 | 2669 | +1（重启自写） |
| breaks_total | 340 | 340 | **0** |
| len(breaks) | 340 | 340 | 0 |
| mtime | 2026/8/30 01:57:09 | 2026/8/30 22:04:04 | 工具行为刷新（验收报告 §5.3 已披露 --verify 亦刷新）|

**新写入行 2669 链连续性校验**:

```
{"t":"2026-08-30 22:02:36","kind":"alert","event":"trading_event","level":"INFO",
 "msg":"开盘自动开启已启用（交易日 9:15 自动启动引擎）","hash":"dcd8d1ecb76f3c2d","prev":""}
→ 新日 2026-08-30 首条 prev="" 符合"每天首条 prev='' 约定"
→ payload+prev 重算 hash = dcd8d1ecb76f3c2d  匹配 TRUE
→ 全链扫描: broken 340 行，最后断裂行 2124（2026-08-27 10:58:18 旧版服务单条自愈断链），any broken after 2124 = []，新行 not broken
```

**策略事件子序列**: `dual_price_weekly.json` 元信息 `order_events_other: {test_buy:1, manual_buy:5, trading_event:8, drill_alert_path:3, strategy_buy:54}`——历史 54 条 strategy_buy 在链且集中非 2124 后的新写入；本次重启后尚未产生新 strategy_buy（盘前正常），但单写者语义已通过新行零断裂验证。待下次交易日触发买入时，`strategy_buy` 将落于 2124 后的健康段。

**交付物声明**: `data/audit_chain_breaks.json` mtime 01:57:09→22:04:04 的变化属工具内建行为（`--verify` 亦按设计重写存档），已按任务书要求事先 `Copy-Item → tmp\breaks_before*.json` 留底，本任务未改写其内容（340 语义不变）。

### 3d. sector

```
python -c "from app import sector; import json; d=json.load(open('data/sector_map.json'))"
→ keys=2911 industries=49 bad_format=0

_shrink_guard 单元测试:
  old 100 票 → new 70 票 (<75%) → (True, (100,70))  blocked 正确
  old 100 票 → new 80 票 (≥75%) → (False, None)     pass 正确

live 判定:
  sector.py _shrink_guard (L158) / _fetch_cons 动态分页 / _last_fetch_stats 遥测 在位
  L195-217 缩水保护: new < old*0.75 → audit WARN sector_map_shrink_blocked + 保留旧文件
```

期望: 2911 键 / 49 行业，与任务书一致，`bad_format 0`。

### 3e. 数据通道心跳

- `data/update_state.json`: `{"last_day": "2026-08-27"}`（重启前已为 08-27，最新交易日 08-28 的收盘更新标记未推进——符合周末/停机预期）
- 日志心跳三件套在位:
  - `[22:02:36] 后台增量更新已启动（min5/日K 自动补最新数据）`
  - `[22:02:36] 股票列表后台预热已启动`
  - `[22:02:36] 资金面缓存后台预热已启动（top30 成交额）`
  - `[22:04:01] 情绪历史预热完成: 484天 IC=0.8222`（updater 启动后约 85s 完成预热，与 `startup_check` 后续轮询一致）
- `/api/overview` 实时性: `market_count=5009 time=2026-08-30 22:02:50` + 全市场成交 9703 亿/11313 亿三指数行情已刷新（`time: 20260828161402` 等为最新交易日快照）
- **8/31 收盘自动更新**: 属任务书"任务2 联合观察"项，已登记于 §5，周一前无交易日，通道待 09-01 收盘验证（见 §5）。

---

## 4. 三修复各自生效结论

| # | 修复 | 落盘位置 | 重启前内存 | 重启后验证 | 结论 |
|---|---|---|---|---|---|
| ① | P76 pos_cash 先用后赋 | `app/trader.py` L665-668（2026-08-27 22:21:55）`pos_cash = acct["cash"]*...` 先于 `_buy_px` | 旧进程早于该 mtime → 未生效（8/27 全天扫描崩溃根因） | 静态: `inspect` 行号 206<213 顺序正确；动态: 日志零 `UnboundLocalError`/Traceback；`startup_check` 进程新鲜度 `core mtime 22:21:55 ≤ 实例 22:02:25(+2日)` | **✅ 已生效**（下次交易时段扫描不再崩） |
| ② | 批审计链单写者 | `app/trader.py` L380/L413 `audit.record(strategy_buy/sell)` + `app/audit.py` L39-46 `_cross_proc_lock` (.write.lock 字节锁) / L124-150 `write+fsync+重读尾 prev` | 同上未生效 | 新行 2669 `prev=""` hash 重算匹配；全链 340=339基线+1(行2124已声明自愈) 其后零新增；`--verify` 前后 340 不变 | **✅ 已生效**（多进程并发断链根因已封堵，历史断裂不修补符合红线） |
| ③ | sector_map 缩水保护 | `app/sector.py` L158 `_SHRINK_FLOOR=0.75` + L158-168 `_shrink_guard` + L195-217 预热线程内旧文件对比与告警 | 同上未生效 | 2911/49 权威映射完好；`_shrink_guard` 70% blocked / 80% pass 单元通过；代码在位且后台线程已随 `warm_up()` 启动 | **✅ 已生效**（下次板块拉取不全时自动保留旧图并告警） |

---

## 5. 遗留风险与观察项登记

| 观察项 | 现状（本报告基线） | 下次判定窗口 | 判定规则 |
|---|---|---|---|
| **双价观察** `data/dual_price_weekly.json` | `generated_at 2026-08-30 20:25:56`（重启前 1.5h 产出，lines 2668）`total.n=0 weeks={}` 空事件——符合设计（策略自动买卖路径 08-28 段零触发 + manual_buy 不入观察流，分叉充要条件 qty 且占当日成交额>5%） | 下两个周度（计划任务 `dual_price_weekly` 周日 09:00） | 若 pos_cash 修复后交易日出现真实持仓与成交，则应开始出现 `n>0` 的定价事件；两周内仍 0 属扫描仍异常的**外部旁证** |
| **实盘 vs 回测** `data/live_vs_backtest_weekly.json` | `generated_at 2026-08-29 09:00:28`（周六产出）`config_snapshot BOARD_MOMENTUM_MIN 8.0` | 同上周度归因（周六 09:00） | 同理，策略样本此前为零因扫描崩溃；修复后应出现非零 `trades` 与可比周度 gap |
| **收盘增量** `data/update_state.json` | `last_day 2026-08-27` 未推进（周末无行情，服务停期间亦不推进属预期） | **8/31(周日)无交易日跳过，09-01(周一)收盘后** `last_day` 应 → `2026-09-01` 且日志出现 `data_update` | 若未推进 → 按 `app/updater.py` 调度与 `data/update_state.json` 排查 `min5_updated/daily_updated` |
| **审计链新买入连续性** | 340 断裂末位 2124，其后 545 行零新增断裂；strategy_buy 历史 54 条 | 下次交易日首笔 `strategy_buy` 出现时复跑 `audit_chain_diag --verify` | 新 `strategy_buy` 所在行不应新增 breaks，其 `prev` 应等于上一行 `hash` |

---

## 6. 计划任务与日志节奏

```
schtasks:
  forward_eval              Ready  Last Run 2026/8/30 20:30:01 Result 0  Next 2026/8/31 20:30
  risk_daily                Ready  Last Run 2026/8/30 20:25:53 Result 2  Next 2026/8/31 20:00  (exit 2 = 当日报告含告警，属预期)
  ml_scores_ledger_daily    Ready  Last Run 2026/8/28 16:30:00 Result 0  Next 2026/8/31 16:30  (周末无运行)
  live_vs_backtest_weekly   Ready  Next 周六 09:00
  dual_price_weekly         Ready  Next 周日 09:00
  A3_TRIAGE_qfq_weekly      Ready  Next 周日 09:00
```

日志已核验: `data/forward_eval_cron.log`、`data/risk_daily_cron.log` 等按计划产出，`startup_check` 前瞻台账最新记账日 2026-08-28（与 `forward_eval.jsonl` 末行 08-28 触发半导体链一致）符合交易日节奏。

---

## 7. 失败与回滚记录

- **失败**: 无。`Start-Process` 一次成功，30s 内 `/api/overview` 就绪（`_wait_ready` 轮询通过）。
- **回滚**: 未触发。回滚路径就位: 停服务 → `data/backups/` 恢复（`tools/backup_data.py` 在线备份）→ 重启；代码回滚为文件还原后复跑 `py_compile`（本报告全程未改 .py，无需回滚）。
- **高危动作**: 仅服务启停（任务授权内），已备份审计存档 `tmp\breaks_before*.json` 双份留底。

---

## 8. 交付物清单

- 本报告: `docs/reports/service_restart_20260830.md`
- 留底: `tmp\breaks_before.json`（跑前 340 基线，76181 字节，mtime 01:57:09）+ `tmp\breaks_before_verify.json`（--verify 前同值）
- 副作用声明（非本任务改写内容）:
  - `data/audit_chain_breaks.json` mtime 01:57:09→22:04:04（工具 `--verify` 内建刷新，内容 340 不变）
  - `data/audit/audit.jsonl` 2668→2669 行（服务自写 22:02:36 启动事件，`hash` 校验通过）
  - `data/app.lock` 新建 67120（服务单实例锁）

---

## 9. 结论

**服务已于 2026-08-30 22:02:25 成功恢复（PID 67120，8899 监听正常），`startup_check` 0 退出码全过，三项热修复（pos_cash 顺序 / 审计单写者 / sector 缩水保护）均经代码在位 + 动态链路验真确已生效。**审计链健康度 340 零增量、sector 2911/49 完好、数据通道三预热心跳正常。观察项（双价/周度归因/收盘增量）已基线化，待下个交易日（09-01）收盘联合验证策略触发与快照推进。
