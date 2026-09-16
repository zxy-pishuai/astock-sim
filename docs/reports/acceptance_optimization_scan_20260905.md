# 验收报告｜架构优化扫描（2026-09-05 周六 13:0x，验收方第二班）

**结论：撮合真实性、快照留存、审计链、风控开关四项已达生产水准，不建议动。真正值得投入的是四类——守护链自洽（pack19 已派）、行情库零副本、覆盖告警分母口径存疑、停机/停摆只能事后复活。策略面最大的一块"未收钱能力"是连板 A1 尚未进任何账本。**

## §1 已核实为"强项"（列此防误伤，别当待办）
- 撮合真实性：`app/trader.py:3` 明示 T+1、涨跌停（涨停买不进/跌停卖不出）、佣金+印花税+滑点、停牌不可交易；`app/execution.py` 有市值分档滑点、涨停排队成交概率、大单冲击成本、成交价钳制在涨跌停区间（防滑点越界产生非法成交价）。实测 `data/account.json` 26 笔成交全部带 `fee`，费用合计 308.58 元，零费单 0 笔。
- 费率参数化：`app/config.py:39` 佣金最低 5 元、`app/config.py:40` 印花税 0.0005（卖出单边），并注释了 2023-08 / 2025-2026 费率变迁。
- 快照留存：`app/data_snapshot.py:94` 有 `KEEP_N` + `_cleanup()`，实测仅存 9 份（11.9GB），不会无限增长。
- 审计链极轻：`data/audit/audit.jsonl` 仅 0.85MB，全链 heartbeat 411 条、9/4 全天 218 条事件 → P1.6 的 50MB 轮转余量很大，不是问题。
- 行情链超时：`app/datafeed.py` 全部 `_http` 带 `timeout`（默认 `C.REQUEST_TIMEOUT`，显式 10/15s），SQLite 连接 `timeout=15` + `PRAGMA busy_timeout=5000`。
- 风控旁路有纪律：`app/config.py:124` `RISK_COOLDOWN_SUSPEND_UNTIL = "2026-09-27"` 注明"过期自动恢复；提前恢复改回空串"，`app/risk.py` 有跨日自动解除与熔断基准跨日复位（P0-2）。**9/27 当天需一次风控复验**（连亏刹车自动重新咬合）。

## §2 P1 新发现：行情库零副本（唯一单点）
- `.gitignore:7` 起排除 `data/market.db*`、`data/min5.db*`、`data/snapshots/` → git nightly 只兜住账户与 JSON 状态，**兜不住 8,848,791 行日K + 5,469,424 行分钟K + 2113 票筹码缓存（142MB）**。
- `tools/backup_data.py` 已实现（`--keep` 默认 7、含 `shutil.rmtree` 清理），但**全机计划任务表里没有任何调用它的任务**（实测枚举本项目仅有 qfq_triage / dual_price_weekly / forward_eval / live_vs_backtest_weekly / ml_scores_ledger_daily / risk_daily / TianjiCloseUpdate / TianjiGit_Nightly / TianjiService_Watchdog）。
- 现有 9 份快照与生产库**同盘**（C: 已用 750.9GB / 剩 201.2GB）→ 不构成副本，盘坏即全失，重建成本以周计（E1 回补已实证吞吐之低）。
- 另有陈旧手工备份 `data/market.db.bak` 与 `data/market.db.bak.20260819_011241`（8/19 版，合计 126MB）、旧固定名 `data/logs/watchdog_child_stderr.log`（209KB，几乎全是 mootdx 的 pandas FutureWarning）→ 属可清理项，**物理删除需用户批准**。

## §3 P1 存疑：覆盖告警的分母口径可能系统性偏严
- 宇宙 `period='day'` 去重 code = 2526，但历史最好水平只有 2211（8/25-8/26，0.875），即约 315 票从不产生日 bar。
- 若真实"应覆盖分母"是 2211，则 `ratio<0.8 → CRITICAL` 实际对应真实 0.91、`<0.9 → WARN` 对应 0.996 → **9/7 收盘很可能看到长期误报的 CRITICAL**，把"E1 正在收敛"这个真信号淹成噪声。
- 处置：先归因（`tmp/pack19/H4_observability_polish.md` 第②项），**阈值/分母改判据由验收方裁定**，执行方不得自行改 `app/config.py`。

## §4 P2 四项
1. **开机后服务复活依赖"有人登录"**：`TianjiService_Watchdog` 的 `Logon Mode = Interactive only`。今天 12:11 开机、12:15 才恢复，前提是用户已登录；若某交易日开机后未登录，9:10 交易窗无人拉起服务。
2. **主循环停摆只能"死后复活"，没有现场**：`docs/reports/half_dead_loop_diag.md` 的 3.5h 停摆系事后推断；prewarm 规避了其中一条路径，但缺"最后完成阶段 + 耗时"轨迹，下次卡死仍要靠猜，而 watchdog 的重启会销毁现场。
3. **`TianjiGit_Nightly` 任务表出现 2 条同名注册**（同 next run 23:50）→ 双跑/竞争嫌疑，顺手核实清理。
4. **情绪门回测吃"最后已知值"无陈旧上限**：`app/sentiment_gate.py:34` 取 `date<=? ORDER BY date DESC LIMIT 1`，而 `qg_zt_full` 止于 2026-08-21 → 之后任一回测日都会静默拿 8/21 的阶段当当天用，无告警无计数。实盘侧走 `_phase_live()` 不受影响。

## §5 策略面：最值钱的下一步
- 现役下单路径只吃基础评分引擎；**连板 A1（V1/E3）是全项目唯一 IS/OOS 双段为正、八年逐年正、加 1% 排队成本仍正的资产，却既不下单也不记账**。
- 影子账机制已在运行（9/5 启动日志："影子日更调度已启动（盘中记账/收盘结算，影子账不碰账户）"）→ 把 A1 作为一条**影子腿**接入，是零风险且立刻产生真前瞻 OOS 样本的做法，远优于直接改 `app/config.py` 让它下真单。
- 顺带把 `qg_zt_full` 断更与 `forward_eval` 一并纳入该影子腿的前瞻评估口径。

## §6 派发
`tmp/pack20/README.md` + K1（数据副本策略）/ K2（陈旧数据守卫）/ K3（开机可用性 + 重复任务清理）/ K4（主循环停摆轨迹）。
K5（A1 影子腿）**未写块**——属方向性决策，待用户点头后验收方出任务书。

## §7 诚实披露
- 本班全程只读，未改任何生产文件（写入仅本报告 + `tmp/pack20/`）。
- §2 "重建成本以周计"是按 E1 回补实测吞吐外推，未做真实恢复演练——K1 把恢复演练列为最重要交付，没演练就不算通过。
- §3 只是口径质疑，**未证明阈值设错**；须等 9/7 首个 `daily_coverage` 真事件 + 315 票归因再裁定。
- §5 的 A1 数字来自已验收的 V1/E3（95 分），按家法 OOS 仍要打 5-7 折；影子腿的价值正是产出无前视、无选择偏差的真前瞻样本。