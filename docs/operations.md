# A股模拟盘 运维手册（v4.5 天玑量化终端）

## 启动与停止
- 启动：`python main.py`（pywebview 界面 + HTTP 服务 127.0.0.1:8899）
- 服务是单进程常驻：任何 .py 代码修改后必须重启进程才能生效
- 判断是否"旧代码在跑"：看任务管理器里 python 进程的启动时间，早于最近一次代码修改即为旧代码

## 重启检查清单（大改动后）
1. 确认后台任务（补数 / 回测 / 严谨验证）已全部完成
2. 关闭界面与 python 进程，重新 `python main.py`
3. 重启后验证：行情时间戳在刷新、情绪仪表盘有数据、持仓/委托页正常

## 数据与备份
- 数据目录 `data/`：market.db（日K/资金流/涨停池/因子）、min5.db（5分钟K，约700MB）、JSON 文件（账户/风控/自选等配置）
- 备份：`python tools/backup_data.py`
  - 使用 SQLite 在线备份，服务运行时执行也安全
  - `--big` 同时备份 min5.db（较慢）
  - `--keep N` 保留最近 N 份（默认 7）
- 恢复：停服务 → 把备份目录里的文件拷回 `data/` → 重启
- 建议：每周跑一次备份；补数、大版本升级前手动备份一次
- 数据库为 WAL 模式，不要手动删除 `-wal` / `-shm` 文件

## 数据源被封 / 超时
- 东财接口内置熔断器（连续失败自动冷却 10 分钟），被封时不要手动疯狂重试
- 行情加速层 `app/tdx.py`（通达信 TCP）失败会自动降级到腾讯/新浪，不影响主流程
- 持续被风控：换网络（手机热点）再试；长批任务调大请求间隔

## 年度维护
- 每年 12 月国务院公布次年假期后，更新 `app/trading_calendar.py` 的 HOLIDAYS
  - 只登记"非周末的休市日"；A股周末恒休市（含调休补班的周末），无需登记调休
  - 若忘记更新，系统启动后会打印 `[trading_calendar] 警告：XXXX 年节假日表未登记`，该年工作日将全部按交易日处理
- 每年初检查 `app/config.py` 佣金/印花税参数是否仍符合实际

## 已知限制（2026-08-23 记录）
- `app/server.py` 的 WF 回测窗口为硬编码日期（2025-08-18 ~ 2026-08-18），不会随当前日期滚动
- 2025 年之前的日K 依赖 `tools/backfill_daily.py` 的补数结果（2026-08-23 首次补数中）
- 龙虎榜数据尚未接入；ML 选股（tools/ml_sidecar）为夜间离线生成 `data/ml_scores.json`

## 数据源断源手册（P70 演练自动生成 2026-08-26 03:45）

演练方法：`python tools/drill_sources.py`（子进程内掐断全部网络，不影响服务与本地库）。最近一次结果：全部通过。

| 数据源 | 断网后备援链 | 演练结果 |
|---|---|---|
| datafeed.fetch_quotes | TDX→腾讯→新浪→内存/名单回填，全断则空字典（优雅降级） | empty |
| datafeed.fetch_kline_day | 本地 market.db 兜底（网络死仍应有数据） | True |
| datafeed.fetch_kline_min5 | 本地 min5.db 兜底 | True |
| datafeed.get_stock_list | stock_list.json 缓存文件兜底 | True |
| news.fetch_news | 东财→新浪备，双断则降级为空/旧缓存 | empty |
| moneyflow.dragon_tiger | 熔断器快速失败 + 本地缓存回退 | empty |
| global_market.summary | 在线站点失败 → global_kline 本地表兜底 | True |
| alert_path(net_dead) | audit 落盘 + notify 内部吞异常 | ok |

处置要点：
1. 行情/K线断源属**预期内降级**：K线有本地库兜底，实时行情断网返回空——
   trader 各轮询对空行情天然跳过，无需人工干预；恢复网络后自动回到主源。
2. 资金流被东财风控掐断时 moneyflow 熔断器进入只读缓存模式（非故障）。
3. 全球数据在线站失败时读 global_kline 本地积累；长期断网该表停止增长，
   恢复后自动续写。
4. 告警路径：audit.jsonl 本地落盘不受网络影响；微信/alert 推送断网时
   内部静默失败，恢复后不补发（可接受）。
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
- 数据健康度辅证：`data/quality_report.json`（每日哨兵）与 `data/quality_alert.jsonl`（更新后自动巡检摘要）

## 分流周常化（P71-A3 计划任务 2026-08-27）

- **任务**：Windows 计划任务 `A3_TRIAGE_qfq_weekly` —— 每周日 09:00 只读运行
  `python tools/qfq_triage.py`（依赖当日 `data/quality_report.json`，建议周六哨兵后即可）
- **性质**：只生成/刷新 `data/qfq_triage.json` 与 `docs/reports/qfq_triage_20260826.md`
  （每次运行覆盖同名报告并追加历史），**自动产建议清单但绝不写 app/config.py、不动 market.db**
- **落地仍走人工**：新建议是否并入 DATA_EXCLUDE_CODES 由验收方决定；惯例流程见
  `docs/reports/exclude_landing.md`（先剔除指数代码 sz/sh*，再对照接缝价抽查，最后配置单点增补）
- **管理命令**：
  - 查看：`Get-ScheduledTask -TaskName A3_TRIAGE_qfq_weekly | Get-ScheduledTaskInfo`（LastTaskResult=0 为成功）
  - 手动触发：`Start-ScheduledTask -TaskName A3_TRIAGE_qfq_weekly`
  - 停用/删除：`Disable-ScheduledTask` / `Unregister-ScheduledTask -TaskName A3_TRIAGE_qfq_weekly`
- **注册方式存档**：脚本经环境变量传中文路径（`.ps1` 文件内不得含非 ASCII 字面量，
  Windows PowerShell 会按 ANSI 误读——2026-08-27 首次注册即因中文任务名/路径乱码失败，已改 env 交接修复）
- **规则口径提醒**：报告分流四分类中 exdiv_false_positive 仅是"疑似"（<2pp 超限），升级需 xdxr 复核
  （工具 `tools/xdxr_verify.py`）；带 * 指数代码一律不入个股排除清单

## 同日 A/B 验收流程（P72 固化，适用于一切"前后对照"型实验）

1. **配方先行**：池/窗口/参数/seed/闸门状态在跑之前写死并写入结果 JSON 的
   `recipe` 字段；判定规则同样事先写死，禁止看完数据再定标准
   （范例：portfolio_ab.md §5、engine_convergence.md §2、global_map_verify.md §3）。
2. **同库同口径**：对照双方必须在同一份数据、同一股票池、同一 seed 上跑。
   底层数据当天若被其他任务更新/修复/污染，须在报告里声明"数据版本差异"，
   并把结论限定为相对比较（参考 portfolio_ab.md §2 的基线失配归因写法）。
3. **增量落盘**：长任务每完成一组就写盘（如 `data/bt_portfolio_ab.json`），
   中断可断点续跑；worker 内切换配置用运行期属性/params，不改源码。
4. **基线校验**：设一组已知目标作 sanity check；不一致时先归因
   （数据漂移 vs 配方错误），归因不了不得下结论。
5. **并行协调**：改任何共享文件前查 mtime；SQLite 写入走 WAL + 短事务；
   批量外盘请求带 UA/Referer 且间隔 ≥1s（东财/新浪按 IP 限流，重试轮换主机）。

## 重启 runbook（细化版，配合「重启检查清单」使用）

1. **改码后、重启前**
   - 对每个改动文件跑 `compile()`（或 `python -m py_compile`）；
   - 确认无后台回测/更新进程：任务管理器比对 python 进程启动时间与代码 mtime，
     或查 `data/update_state.json` 的 last_day 是否已标记；
   - 数据库改动确认已 commit；勿手删 `-wal` / `-shm`。
2. **重启**
   - 关闭界面与 python 进程 → `python main.py`；
   - 服务启动会自动触发后台增量更新（updater.start_background_update）。
3. **重启后验证清单**
   - 一键核验：`python tools/startup_check.py`
     （退出码 0=全部通过 / 2=有 FAIL/WARN 需关注）。覆盖六类检查：
     生效参数四项+冷却暂停开关（止损 −0.07、择时 INDEX_TIMING_ENABLED=False、
     竞价 AUCTION_MIN_PCT=4.0、动量 BOARD_MOMENTUM_MIN=8.0、
     RISK_COOLDOWN_SUSPEND_UNTIL ≥ 今天）、服务存活（/api/overview）、
     影子线程启动证据（/api/log 含"影子日更调度已启动"）、前瞻台账最新记账日
     与计划任务在册、秒级监控随引擎的启停状态、进程新鲜度
     （app/*.py 的 mtime 晚于实例启动时间 → 提醒"改码未重启，内存仍是旧值"）。
   - 手工抽查：行情时间戳在刷新；情绪仪表盘有数据；持仓/委托页正常；
     `data/update_state.json` 的 last_day 已更新到目标交易日。
   - 计划任务（2026-08-27 起六个，查询均 `schtasks /query /tn <名>`）：
      `forward_eval` 前瞻记账 每日 **20:30**（旧任务 AStockForwardEvalDaily 已删除）｜
      `risk_daily` 风控日报 每日 **20:00**｜
      `ml_scores_ledger_daily` ML 台账 tick 周一~五 **16:30**（交易日近似，节假日空跑无害）｜
      `live_vs_backtest_weekly` 实盘vs回测归因 **周六 09:00**｜
      `dual_price_weekly` 双价观察周报 **周日 09:00**｜
      `A3_TRIAGE_qfq_weekly` qfq 分流 周日 09:00。
   - 任务日志尾部无异常：`data/forward_eval_cron.log`、`data/risk_daily_cron.log`
     （risk_daily 行内 `exit=2` 表示当日报告含告警，打开
     `data/reports/risk_daily/risk_daily_<date>.md` 看告警区）。
4. **回滚**：停服务 → 从 `data/backups/` 或 `backup_data.py` 备份恢复 → 重启；
   代码回滚直接还原文件后重复第 1 步。


## 单实例锁 data/app.lock（Phase76 文档化）
- main.py 启动时写入当前进程 PID（_acquire_lock，PID 存活+端口健康双重校验）。
- 作用：防多实例/僵死进程——已有存活实例时直接复用退出；锁文件残留但 PID 已死则自动清锁重启。
- 运维：服务异常退出后残留的 app.lock 无需手工删除（下次启动自动判定）；
  若误删导致双实例，第二实例会因端口占用失败退出。

## 常驻后台与观察面（2026-08-27 E1~E4 落地）

随服务进程启动、`python main.py` 即生效（改这些文件后同样需重启进程）：

| 后台组件 | 启动证据（/api/log 或日志） | 时间窗 | 产出/查看 |
|---|---|---|---|
| 影子日更调度 `app/shadow_scheduler.py` | "影子日更调度已启动（盘中记账/收盘结算…）" | record 10:00–14:50 / settle 15:05–15:30，交易日 | `data/shadow/shadow_board.jsonl`；`python tools/shadow_board.py --status` |
| 增量更新 `app/updater.py` | "后台增量更新已启动" | 启动时 + 收盘后 | `data/update_state.json` |
| 秒级监控 Phase51（随交易引擎 start() 同拉起） | "交易引擎已启动" | 引擎运行期间 2s/次盯仓，只告警不下单 | watch_events / 推送；盘前引擎未启属常态 |

独立计划任务（不依赖服务进程）：

- **forward_eval** 每日 20:30：前瞻信号记账（幂等按本地库最后交易日锚定；
  数据缺失日照实留空，**禁止补记历史缺口**）。台账 `data/forward_eval.jsonl`，
  日志 `data/forward_eval_cron.log`。报告：`python tools/forward_eval.py --report`。
- **risk_daily** 每日 20:00：风控观察日报（只读 account+audit+market.db ro，
  不碰任何账户/交易路径）。产出 `data/reports/risk_daily/risk_daily_<date>.md`；
  当日盈亏接近熔断线(−5% 的 80% 即 −4%)或连亏达暂停线 → 报告内告警 + exit=2
  （日志行 `risk_daily exit=2`）。冷却拦截暂停期（RISK_COOLDOWN_SUSPEND_UNTIL，
  至 2026-09-27）内该报告是连亏风险的主要观察面——拦截不会自动停损，需人工决策。
- 一键启动核验：`python tools/startup_check.py`（见上方重启 runbook 第 3 步）。



---

## 版本控制 SOP（2026-09-01 起）

项目已纳入 git 本地版本控制（纯本地，无 remote；2026-09-01 基线提交 baseline-20260901）。

### ① 基线 / 回滚命令
- 查看某次提交的 diff：`git diff HEAD~1 -- <path>`（最近一次）或 `git diff baseline-20260901 -- <path>`
- 从基线恢复单个文件：`git checkout baseline-20260901 -- <path>`（例如被误改的 json / config）
- 完整回滚到基线：`git checkout baseline-20260901 -- .`（还原所有受版本控制的文件，谨慎）
- 查看历史：`git log --oneline`

### ② 执行 AI 改共享文件纪律（替代人肉 mtime 考古）
任何执行型 AI（MainAgent 或各子块）在改动共享文件（data/*.json、docs/、app/、tools/、config 等）前后，
必须把 `git status --porcelain` 的输出完整贴进自己的交付报告，作为"改了什么"的权威留痕。
提交由 nightly 自动任务统一落库，执行 AI 不手动 commit（避免碎片提交）。

### ③ 数据大文件边界声明
`data/market.db*`、`data/min5.db*`、`data/snapshots/`、`data/backups/`、`.venv/` 等 GB 级/二进制
**不在 git 版本内**（见 .gitignore）。这些文件的保护仍以文件层备份为准（快照/主库/backups 目录），
git 只覆盖代码、文档与小文本数据（json/jsonl/日志白名单）。任何依赖"git 能找回大文件"的假设都是错误的。

### ④ 夜间自动提交
计划任务 `TianjiGit_Nightly`（每日 23:50）调用 `tools/git_nightly.ps1`：
`git add -A` → `git commit -m "auto <时间>"`（无变更时吞掉非零退出，不算失败）→
末条提交追加到 `tmp/git_nightly.log`。当日所有 AI 产物的变更在每晚自动落库。

### ⑤ EOL 政策与 git 卫生（2026-09-01 X4）
- `.gitattributes`（2026-09-01 建立）：文本类（py/json/jsonl/md/html/css/js/mjs/txt/cmd/ps1/log/csv/orig）
  统一 `text eol=lf`（clean 与 checkout 均 LF）；二进制类（db/png/ico/zst/bak_*）`-text` 保字节零转换。
- 工作区 CRLF 文件按字节保持原样，git 只比较 clean 后内容 → 基线遗留的"整文件假 diff"已归零
  （证据与路线选择见 docs/reports/git_hygiene_20260901.md）。
- ⚠ 审计链哈希对行尾不敏感（app/audit.py record()/verify_chain() 对 json.dumps payload 算 sha256，
  换行仅作行分隔不入哈希）→ 行尾规范化不影响链完整性。

### ⑥ TianjiGit_Nightly 首次验收（2026-09-02 上午，X4 移交）
- 验收命令：`git log --since=yesterday --oneline` → 应见 `auto 2026-09-01 23:50` 提交；
- 若未见 auto 提交（失败排查）：查计划任务
  `schtasks /query /tn TianjiGit_Nightly /fo LIST /v`，以及 nightly 日志
  `tmp/git_nightly.log`（tools/git_nightly.ps1 每次把末条提交追加到该文件）；
- 行为基准：nightly = `git add -A` + `git commit -m "auto <时间>"`，无变更时吞掉非零退出不算失败。
