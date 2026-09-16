# 运维补全：三个计划任务注册 + triage 积压消化计划

- 日期：2026-08-27 ｜ 执行：ox-alpha（任务5/运维补全，工程型）
- 关联：`docs/operations.md`「重启 runbook / 计划任务清单」（已同步更新）、
  `docs/reports/qfq_triage_20260826.md`（分流基线，本次已刷新）、`docs/backlog.md`
- **红线合规声明**：
  - 未重启服务——实例 PID **21728** 自 02:21:36 持续监听 8899 至今（注册前后各验一次）；
    新任务全部改 `.cmd`/调度层面，下次服务重启才涉及任何 app 代码生效问题（本次未改 app 代码）
  - 未动现有三个任务（forward_eval / risk_daily / A3_TRIAGE_qfq_weekly State=Ready，
    NextRun 与注册前一致）
  - 未改 `app/config.py`（排除清单增补仍留给验收方）

## ① 计划任务注册证据

风格对齐 forward_eval/risk_daily：`cmd.exe /d /c <cmd>` + WorkingDirectory=项目根、
Interactive 仅当前用户、StartWhenAvailable（错过补跑）+ 电池放行、防并行。

```
TaskName:      \ml_scores_ledger_daily
Next Run Time: 2026/8/27 16:30:00        # 周一~周五（交易日近似；节假日按 ml_scores.json 锚日幂等空跑）
Status:        Ready
Logon Mode:    Interactive only

TaskName:      \live_vs_backtest_weekly
Next Run Time: 2026/8/29 9:00:00         # 每周六
Status:        Ready

TaskName:      \dual_price_weekly
Next Run Time: 2026/8/30 9:00:00         # 每周日
Status:        Ready

既有任务复核（未动）：
A3_TRIAGE_qfq_weekly: State=Ready NextRun=2026/8/30 9:00:00
forward_eval:         State=Ready NextRun=2026/8/27 20:30:00
risk_daily:           State=Ready NextRun=2026/8/27 20:00:00
```

管理命令与查询：`schtasks /query /tn <名> /v /fo LIST`；
手动试跑 `Start-ScheduledTask -TaskName <名>`；停用 `Disable-ScheduledTask`。
日志落盘：`data/ml_scores_ledger_cron.log`、`data/live_vs_backtest_weekly.log`、
`data/dual_price_weekly.log`（对应 .cmd 内 `>> 追加 2>&1`）。

### 附带发现（非本任务产物，仅记录）

运行中的服务锁文件 `data/app.lock` 在注册期间被外部操作删除（进程/端口不受影响，
main.py 下次启动会自动重建并写入 PID）。若见 "app.lock 不存在但服务在跑" 属此现象，
无需处理。

## ② triage 积压消化计划（只出计划，本轮不执行批量回填）

### 基线刷新说明

任务书数字 **1,412 候选 / 241 高优** 来自今日 04:35 最新哨兵
（P71 上限放宽至 1500 后首次全量入库）；此前 08-27 02:43 的分流基于昨晚 21:29 版
（1129/54）已过时。本轮已重跑一次**只读** `tools/qfq_triage.py`（2.65s 秒级，非回填）
把分流基线对齐到最新哨兵。新基数：

| 分类 | 数量 | 处置定性 |
|---|---|---|
| already_excluded | 625 | 无需动作 |
| suspension_seam 停牌接缝 | **189** | 可修复型：缺交易日回填 |
| exdiv_false_positive 除权疑似 | **459** | 需 xdxr 交叉复核后定论（market.db 无 xdxr 表，走 tools/xdxr_verify.py 通达信源） |
| true_qfq_pollution 真污染 | **38** | 其中 21 只建议增补 DATA_EXCLUDE_CODES（验收方落配置）；其余进单票修复队列 |
| listing_new_or_ipo 新股语义 | 101 | 无需修复，标记关闭 |
| 建议 | 合计 | 排除建议 21 只 + 回填/复核活约 650 只 |

### 优先级排序（打板策略相关 > ML 覆盖 > 其它）

**P0｜打板策略相关**（直接影响每日实盘信号与 board 归因可信度）
- 范围：`data/bt_pool.json`(500) ∪ updater 动态 min5 清单 ∪ 近 90 日交易票，
  与 suspension_seam(189) ∪ true_qfq_pollution(38) 的**交集**
- 动作：逐只接缝回填（同型模板 `tools/repair_002594.py --check/--apply`，
  先 check 出差异再 apply）；真污染票单票评估"修 or 排除"
- 节奏：**每晚 ≤10 只**（check 全跑，apply 只做 check 通过的）

**P1｜ML 覆盖**（ml_sidecar 训练标签质量，影响夜间打分与前瞻台账）
- 范围：exdiv_false_positive(459) 的 xdxr 批量复核 → 确认真污染的转修复队列
- 动作：`python tools/xdxr_verify.py` 批量模式（纯本地+轻量源），每晚 ≤100 只；
  输出追加到 data/xdxr_verify.json
- 完成后：复核通过的误报闭环关闭；确认真除权的给出排除/修复双轨建议

**P2｜其它**
- listing_new_or_ipo(101)：一次性批量打标关闭（写分流 json 的 disposition 字段）
- 非交集剩余票按 severity 降序兜底
- 维护项：哨兵每周自动滚动出新候选，每周六 live_vs_backtest 之后人工过一次增量

### 每日执行窗口（盘中禁止大批量回填 = 硬约束）

| 时段 | 允许 | 说明 |
|---|---|---|
| **18:00–19:50** | 只读类：triage 刷新 / xdxr 批复核 / repair --check | 避开 16:30 台账 tick，且不写库 |
| 20:00–20:40 | （已被占用：risk_daily 20:00 / forward_eval 20:30） | 不安排批任务 |
| **01:00–05:30** | 写入类：repair --apply 接缝回填 / 单票全史补数 | SQLite WAL 单写者，与日更天然错峰 ≤10 只/晚 |
| 09:15–15:00 | 🚫 **禁止任何批量回填/修复 apply** | 盘中共享库与行情带宽让位实时交易路径 |
| 周六白天 | 大件窗口：哨兵 --full 复核、退市宇宙 refresh、周度汇总 | 服务低负载时段 |

- 并行纪律沿用 operations.md「同日 A/B 验收流程」第 5 条：动共享文件前查 mtime；
  回填写入带 manifest 幂等可重放（参照 P64-D2/D3 模板），失败次日续跑不重放已写入部分
- 进度追踪：每批把 {当日 n / 累计 n / 剩余 n} 追加到执行报告并在 backlog.md 周度更新一行；
  全部消化预计 **P0 ≈ 3~4 周、P1 复核 ≈ 5 个工作日**（并行推进时以 P0 写入窗为准）

### 本轮明确不做（等待验收方或后续任务授权）

1. 不执行任何批量回填/--apply（本任务只交计划）
2. 21 只排除建议不自动写入 config
3. audit 哈希链并发断点治理（backlog 看板 §7 附带发现）另立任务

## ③ cmd 脚本独立可运行核验

| 脚本 | py_compile | 实跑(真实目标) | 失败退出码传播 | 日志落盘 |
|---|---|---|---|---|
| ml_scores_ledger_daily.cmd | ✅ | ✅ 设计内自检 `--selftest-pred 25` exit=0（1000 登记+1000 兑现端到端，IC均值0.18/ICIR0.56，自检报告独立落盘 *\_selftest.json，真实台账零触碰）<br>另用管道探针验证 cd→python→日志链路 exit=0 | ✅ 故障注入探针 exit=2 | ✅ 重定向正常 |
| live_vs_backtest_weekly.cmd | ✅ | ✅ 完整真实跑 exit=0（W35 归因产出，json+周报 md 已写） | ✅ exit=2 | ✅ |
| dual_price_weekly.cmd | ✅ | ✅ 完整真实跑 exit=0（json 已写 + 双价观察周报已更新） | ✅ exit=2 | ✅ |

- 失败探针方法：临时副本把目标行替换为调用不存在脚本 → 三个包装均正确把
  Python 的失败码传到 cmd 退出码（schtasks LastTaskResult 将非 0），且 stderr 追加进各自 .log
- 路径核验：三个 cmd 均 `%~dp0..` 自定位项目根（fresh cmd.exe GBK 代码页下探针通过），
  文件字节纯 ASCII（0 非 ASCII 字节），Python 解释器绝对路径存在性已验
- 结果物抽查：`data/live_vs_backtest_weekly.json`、`data/dual_price_weekly.json`、
  `docs/reports/{live_vs_backtest_weekly,dual_price_observation}.md`、
  `data/ml_scores_ledger_report_selftest.json` 均为当次运行新 mtime
