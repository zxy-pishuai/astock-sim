# tools/ 脚本索引（2026-09-16 自动生成，验收方）

> 生成口径：`live` = 被 `app/`、`.cmd`、计划任务命令行引用的脚本（**不要移动**）；
> `research` = 无人引用的历史研究脚本（**可移入 `tools/research/`**，移动前复核引用）。
> 本轮只生成索引，**未移动任何文件**。

## live（100 个，24845 行）

| 脚本 | 行数 | 作用（首段 docstring） |
|---|---|---|
| `act12_t4_pit.py` | 368 | ★ 任务4 score act12_T4 PIT 复验（唯一活候选）— R1 加固版 2026-08-31 快照: data/snapshots/2026-08-27/market |
| `ak_probe.py` | 294 | W4 akshare 五路免费源探测脚本（研究·唯一出网块）。 范围：龙虎榜 / 两融 / 限售解禁 / 业绩预告·报表 / 复权因子（hfq-factor）。 纪律（docs/o |
| `apply_qfq_exclude.py` | 81 | 一键落库 QFQ 109 只建议清单到 app/config.py（需验收后显式执行，幂等，去重，LF）。 用法: python tools/apply_qfq_exclude.p |
| `auction_backtest.py` | 239 | C3｜竞价打板 / 竞价弱转强 战法回测卡（预注册口径） 主口径 = 日线近似（min5 覆盖仅 2025-08-18 起，IS 2019-2023 无分钟数据，见 §0a）。 事 |
| `auction_grid.py` | 163 | ★ Phase40：auction_intraday 参数网格（现成策略从未调参） 策略：app/engine_minute.MinuteBoardBacktest 的 aucti |
| `auction_verify.py` | 150 | ★ Phase38 保留意见处置：auction_intraday 抽样逐笔回对 背景：docs/reports/auction_grid.md §4 对 min4.0 变体（+3 |
| `auction_w2s_research.py` | 425 | ★ Phase34: 竞价弱转强策略研究（独立模拟器，只出研究结论，不做实盘/引擎集成）。 信号定义（严格 PIT，只用开盘价及以前信息）： 弱势（昨日，三选一分开测）： W1 炸 |
| `audit_chain_diag.py` | 131 | ★ 任务1-③：审计哈希链断裂诊断（一次性基线 + 可复用体检） 职责边界（红线）：只读标记历史断裂位置，绝不重写/修补 audit.jsonl。 判定口径与写入端约定一致：每天首 |
| `audit_rotate.py` | 36 | R3-E2：audit.jsonl 按日轮转（schtasks 每日 00:05 调用）。 - 调 app.audit.rotate_daily()：持 _cross_proc_l |
| `audit_watch.py` | 258 | ★ D2（2026-09-13）审计巡检：静默失败防护（INFO 级"延后/跳过"事件的自动升级） 痛点：快照三天没生成全靠三条 INFO 级 data_snapshot_defe |
| `b3_score_subtract_ab.py` | 221 | ★ B3：score_stock 负 IC 纯减法实验 —— 同日 A/B + judge_kit 判定 + PIT 双列 背景链路： P28-A factor_ic.md：18  |
| `backfill_amount.py` | 143 | ★ K2（2026-09-16）：成交额（amount）缺失定向回填工具。 扫描 kline(period='day') 中 amount IS NULL OR amount=0  |
| `backfill_daily.py` | 293 | ★ Phase15：历史日K补全 backfill（2019-01-01 起 → 今天） 目标：把 market.db kline(period='day') 从实际 2025 年 |
| `backfill_earnings_history.py` | 155 | I1 业绩预告历史回填（2019-01-01 ~ 今，按季度分页拉取入库）。 用法：py -3.13 tools/backfill_earnings_history.py [--f |
| `backfill_index_history.py` | 188 | A5（2026-09-13）三大指数日K历史回填至 2019-01-01（与个股回测口径对齐）。 背景：sh000001 仅 615 根（2024-02-29 起）、sz39900 |
| `backfill_missing_days.py` | 144 | A1 补数（2026-09-13）：定点补齐 09-09/09-10/09-11 缺失日K行。 只补缺失 (code,date) 行（INSERT OR IGNORE），不重写已有 |
| `backfill_pre2019_w0.py` | 214 | ★ D2：2019 前日K补缺（backfill_daily 的窄写扩展版） 背景：Phase15 报告称"含 2019 前的全历史一并补齐"，但 D2 核验发现库内 2019-0 |
| `backfill_snapshot_days.py` | 40 | ★ B1（2026-09-13）：补生成 09-09/10/11 缺失快照（PIT 不可复现的占位副本）。 背景：09-09/10/11 三天因"日K未追平→只记 data_sna |
| `backfill_zt_history.py` | 355 | ★ K4（2026-09-16）：情绪周期历史表补数（P1-④）。 根因（本脚本头注，证据见 docs/reports/k4_zt_history_backfill.md §1）： |
| `backtest_dd_gate_v2.py` | 352 | ★ Phase37: DD Gate v2 回测 —— 恢复机制修复 halt 吸收态。 阶段1：阈值 A(6%/10%) × recovery ∈ {default(R0), c |
| `backtest_true_board.py` | 117 | ★ Phase42: 真打板回测 —— 三档成交模型 × 同期对照。 窗口 = min5 覆盖期（2025-08-18~2026-08-21）；池 = bt_pool 前 300。 |
| `backtest_volprice.py` | 240 | ★ 阶段1 量价信号包回测：触发后 1/3/5 日期望收益与胜率（本地数据，PIT 合规） 用法：python -m tools.backtest_volprice 输出：docs |
| `backtest_volprice_sell.py` | 104 | ★ 4.6 量价择时卖出 双信号回测（纯本地 market.db，不走网络） 信号：①缩量新高 ②放量滞涨（intraday 判定放宽见函数） 对卖出信号：有效 = 触发后股价不涨 |
| `backtest_zt_eco.py` | 288 | ★ Phase26：涨停生态温度计闸门回测调度（board 策略，独立进程，不走服务 HTTP） 口径对齐 Phase21/22/25（与 bt_clean_results.jso |
| `backup_data.py` | 70 | data/ \u5907\u4efd\u5de5\u5177 \u2014\u2014 SQLite \u5728\u7ebf\u5907\u4efd\uff08\u670d\u5 |
| `backup_key_pack.py` | 272 | 极小关键包打包工具 —— 同盘防误删/防写坏，不防盘坏。 打包"不可重建"档为单个 tar.gz，落 data/keypack/，内含 SHA256SUMS.txt + manif |
| `bench_backtest.py` | 162 | H4 回测性能基准与回归护栏（离线）。 - 数据：钉快照 data/snapshots/2026-09-11/market.db（只读 URI），BT_NO_CACHE=1 强制直 |
| `bench_h2.py` | 122 | H2 改前/改后对照基准：三策略 × 两窗 + score 500 池性能窗。 用法: python tools/bench_h2.py --stage before --pool |
| `bench_h3_opt.py` | 58 | H3 optimize_params 对比：串行 vs 并行（BT_PARALLEL_ENABLED 开关）。 用法: python tools/bench_h3_opt.py [ |
| `bench_h3_wf.py` | 60 | H3 walk_forward 验证：数据共享（_wf_grid_select 一次加载）正确性与性能。 用法: python tools/bench_h3_wf.py [--po |
| `bench_score.py` | 198 | H4 score 性能基准与回归护栏（离线）。 - 输入：data/bench_fixtures.npz（3 票 × 1250 根日K，bench_fixtures_build.p |
| `board_w2s_filter_ab.py` | 153 | ★ Phase69: 竞价弱转强 v2 —— 作为 board 入场前置过滤器的同日 A/B。 思路变更（P34 独立策略已证否）：不再把弱转强当独立策略，而是测试 "昨日强势的  |
| `board_weight_ab.py` | 126 | P66 打板评分再配权验证 — 按 board_components.md 建议跑变体 基线: score_board 现权重 变体: 放量扫板 +15→8、动量启动 +10→0（ |
| `bt_convergence_runner.py` | 303 | ★ Phase48+49 回归运行器：engine 收敛修复的前后对照基线 配方（dd-off 内部自洽口径，不拿 bt_clean_results 当逐位目标）： - 池 = b |
| `bt_fingerprint.py` | 365 | I3 回测复现性护栏：结果指纹 + 禁网守卫 + 环境锁定。 用法： py -3.13 tools/bt_fingerprint.py run --codes 000001,600 |
| `bt_snapshot_baseline.py` | 74 | ★ Phase46: 快照口径 vs 活库口径 基线对照（score/board 四窗口，同日双跑）。 用法： python tools/bt_snapshot_baseline. |
| `build_delisted_universe.py` | 219 | ★ D3：退市股宇宙构建与回填（免费源管线固化版） 数据源（均实测可用，2026-08-27）： 1) baostock query_stock_basic —— 全 A 宇宙 5 |
| `build_pit_pool.py` | 359 | ★ Phase64：PIT 股票池与幸存者偏差量化 现行 bt_pool 是"2026-08 成交额 top500"的静态池——用它回测 2019~2024 存在双重 前视：幸存者 |
| `build_sentiment_history.py` | 272 | ★ Phase16：重建全历史情绪序列（2019-01-01 起），落库 qg_sentiment_history 方法（与 app/sentiment_series.py 一致） |
| `c_lane_redispatch.py` | 270 | ★ 任务3 C 通道重派（回测型）：C2 二维交互网格 / C3 高档冲高回落复验 / C4 moneyflow 卖出信号（数据门控）/ C5 T2_A1 组合跨策略稳健性。 Ph |
| `config_landing_regression.py` | 169 | ★ Phase47+50：config 落地批处理回归对照（同日 A/B，规避前复权重锚定漂移） 落地项： a) STOP_LOSS_PCT -0.05 → -0.07（Phase |
| `daily_backfill.py` | 165 | E1（pack18）一次性日K全库回补工具。 背景（docs/reports/audit_20260903.md §2 P0-1）：update_daily 默认 limit=30 |
| `data_repair_20260825.py` | 349 | ★ Phase35：数据修复包 —— Phase31 哨兵发现项的处置（可复跑） 任务1：前复权缺陷修复（600262 北方股份、600426 华鲁恒升） - 先备份两票 klin |
| `data_sentinel.py` | 715 | ★ Phase31：数据质量哨兵 —— 自动发现脏数据，而不是等它坑回测 只读检查（SQLite 全程 mode=ro），不修改任何数据库/config；输出 data/quali |
| `data_snapshot.py` | 38 | ★ Phase46: 数据快照 CLI（核心逻辑在 app/data_snapshot.py，供 updater 自动调用）。 用法： python tools/data_snap |
| `drill_sources.py` | 174 | ★ P70：数据源故障演练（脚本化断源，不影响运行中的服务） 对四类数据源做受控断源演练：行情（quotes/K线）、资金流（moneyflow）、 新闻（news）、全球（glo |
| `dual_price_weekly.py` | 365 | ★ D4：双价观察分析工具 —— buy_px_dual 事件的周度新旧口径差异报告 背景（P45 审计-D → P59 落地）：trader._buy_px 处于"双价观察期"— |
| `e1_fix_intraday_bars.py` | 177 | E1 收尾：定点重拉 9/4 13:16 盘中残缺 bar（批 6，200 票）。 流程： 1. 读批 6 票单（tmp/pack19/batch6_final.json） 2.  |
| `earnings_window_scan.py` | 202 | ★ P71：业绩披露窗口禁买研究（只读 + 新工具，同日 A/B） 假设：财报披露前后股价波动放大/信息风险高 → report_date 前 N 日对该标的 不开新仓可改善收益/ |
| `exit_param_scan.py` | 254 | ★ Phase36：退出参数一维扫描 —— 处理最大亏损桶（"止损"，720 笔净亏 -107 万） 证据：Phase30 归因 by_exit.json——止损后 5 日均值 + |
| `experiment_scan.py` | 444 | W5: 实验台账索引器 — 扫描 docs/reports + data/bt_*.json + docs/backlog 三源, 合并为 data/experiments_ind |
| `factor_ic_audit.py` | 377 | ★ Phase28-A：score_stock 打分因子 IC 审计（只读，不修改任何现有代码） 范式：tools/ml_ic_verify.py（逐月 Spearman IC / |
| `fill_reality_lab.py` | 249 | Y3 撮合现实性审计 —— 流动性参与率强制缩量重放（研究·零落地） 快照: data/snapshots/2026-08-27/market.db（只读，与 R3/W3 同快照同 |
| `forward_eval.py` | 371 | ★ Phase29: 前瞻信号记账本 —— 无法回测的信号用实盘日志前瞻验证。 记账对象（收盘后时点记录，次日实现）： ① app/global_market.a_share_hi |
| `global_thin_backfill.py` | 141 | A4 全球薄源回补工具 —— GC/USDCNH/DJIA 主备链回补 global_kline 并哨兵级复验 用法: python tools/global_thin_backf |
| `i2_three_arm.py` | 205 | I2 三臂对照回放：旧实盘口径 / 旧回测口径 / 新统一口径 同一时间窗（2026-06-01~08-31）同一 threshold，量化口径不一致的差异。 - A 旧实盘口径： |
| `import_min5.py` | 66 | v3.7 分钟数据导入工具：桌面 494 只股票一年 5 分钟 K → 独立分库 min5.db 开发期工具（需要 pandas 读 pkl），运行时模拟盘保持零依赖。 用法：py |
| `index_timing_retest.py` | 210 | ★ Phase43：指数择时复验 —— 旧结论（phase2_index_timing.md）产生于 P20/P21 数据 修复之前，对现行基线无效；情绪闸门已在 P22 按干净数 |
| `index_timing_snapshot_retest.py` | 193 | ★ Phase73：index_timing 快照口径复验（准备阶段脚本） 背景：P43/P47 的指数择时 board 结论在不同数据锚点间翻转（+1.05pp ↔ -17.57 |
| `judge_kit.py` | 173 | ★ Phase70: 统一判定库 —— 所有回测扫描工具的判定逻辑从这里 import，禁止手写。 单位约定（全库统一，违者即是 bug）： - 损失（loss）一律为【正数百分点 |
| `live_fill_calibration.py` | 204 | ★ Phase58：实盘成交标定 —— 用 audit.jsonl 真实打板成交校准 true_board.md 的 M2 先验 背景：true_board.md 的 M2 中性口 |
| `live_vs_backtest_weekly.py` | 536 | ★ D5：实盘 vs 回测周度归因周报 数据源（全部只读）： ① data/account.json —— 账户历史 trades（time/side/code/price/qty |
| `migrate_sqlite_20260913.py` | 263 | ★ J3（2026-09-13）：删除冗余索引迁移脚本。 DROP idx_kline_cp（market.db，被 PK(code,period,date) 前缀覆盖，dbsta |
| `min_sell_verify.py` | 443 | ★ Phase32：分钟级量价卖出历史验证（只读 + 新增文件，不改任何现有代码） 目的：config.VOLP_SELL_MIN_*（分钟爆量滞涨清仓 / 高位缩量上冲卖半仓）默 |
| `ml_ic_verify.py` | 164 | ★ ML 预测 IC 验证工具（纯标准库）：逐月 Spearman IC / ICIR / 十分组未来5日均收益 - 读 market.db 的 ml_pred（滚动预测分）与 k |
| `ml_shadow_portfolio.py` | 419 | W2 ml 影子组合全史回测（研究·轻算力，纯向量化，无 multiprocessing） 口径（与报告 §0 预注册一致，勿改判据）： - 只读快照 data/snapshots |
| `portfolio_method_ab.py` | 320 | ★ Phase44：PORTFOLIO_METHOD 对照 —— 默认在跑，从未被验证 config.PORTFOLIO_METHOD="risk_parity" 是默认值，但从未 |
| `preflight.py` | 193 | J4（2026-09-13）：AI 提交前门禁（preflight）。 用法： py -3.13 tools/preflight.py # 全量：语法 + 冒烟 + 禁区 + 越界 |
| `qfq_batch_repair.py` | 300 | E块 F1 qfq 跨段污染批修复工具（草案） —— 只读对账产物之一，不 apply 用途 ---- 读取 data/qfq_batch_state.json 的 queueA_ |
| `qfq_census.py` | 152 | Y1 tools: qfq 复权口径全库普查（判型 v4） 判型模型（决策口径：std_qfq vs 非 std）： - fixed（progress 修复成功 / batchA  |
| `qfq_factor_check.py` | 291 | X3 / qfq factor date-level recheck (research / read-only, NO apply) Purpose: Recompute qfq |
| `qfq_triage.py` | 243 | P54 哨兵跳变候选分流 — 只读 输入: data/quality_report.json (SQLite mode=ro，不改库不进config) 输出: data/qfq_t |
| `qfq_unify_recompute.py` | 586 | Z3 / qfq 全库因子统一重算 v2 工具（Y1 方案 C 落地执行器） 用途 ---- 把 E 块"重拉修复"（Batch B）改造为「全库因子统一重算」： 1) aksha |
| `repair_002594.py` | 201 | P71-A2 比亚迪 002594 前复权接缝修复驱动（复用 tools/repair_qfq.py 流程） 跳变: 2025-07-16 → 2025-07-17 open +2 |
| `repair_position_peak.py` | 173 | C1 一次性诊断：存量持仓污染 peak 扫描 + 历史带"峰"卖单污染量化。 只读：data/account.json、data/min5.db（URI mode=ro）。输出  |
| `repair_qfq.py` | 245 | ★ qfq 前复权缺陷修复 v4：mootdx 原始日K + 通达信 xdxr + 纯Python自实现前复权 数据源（本地可靠，已实测 300750/002837/300015  |
| `report_pit_columns.py` | 124 | ★ Phase72: 回测报告双列化 —— 静态池 vs PIT 池对照段生成器（Phase64 范式）。 输入：任意符合以下两种形态之一的回测 JSON： A) {"runs": |
| `risk_daily.py` | 354 | 风控观察日报 —— 冷却暂停期间的配套眼睛。 只读工具：仅读取 data/account.json、data/audit/audit.jsonl、data/risk_state.j |
| `service_watchdog.py` | 974 | H1｜看门狗硬化 —— 三级健康判据 + 半死状态恢复（纯标准库，不 import app/*） 用途 ---- 作为计划任务每 5 分钟运行一次（StockService_Wat |
| `shadow_board.py` | 289 | P65 真打板 M2 影子运行 — 只记账不下单不碰账户 盘中: 记录信号+假设成交（min5 触板检测 + M2 概率） 收盘后: T+1 结算到 data/shadow/（复用 |
| `sizing_lab.py` | 419 | W3 仓位科学实验室 —— vol-target / ATR 风险预算 vs 现行 3×0.30（研究·重回测·零落地） 快照: data/snapshots/2026-08-27 |
| `sizing_replay.py` | 378 | X2 离线重放 —— S1 vol-target 权重层独立实现交叉验证（只读，2 窗，零生产改动） 预注册判据（docs/reports/sizing_wiring_propos |
| `smoke_test.py` | 119 | 冒烟测试（零依赖）—— 覆盖关键路径，抓"静默吞错"类 bug 用法：python tools/smoke_test.py |
| `snapshot_archive.py` | 273 | Y4 快照/备份归档工具（一期：压缩→三验→入暂存，零删除） 纪律（红线段落）： - 一期零删除：源文件压缩后移入 data/snapshots/_archive_pending_ |
| `snapshot_baseline_report.py` | 143 | P53 基线对账与漂移监控 — 聚合 snapshot_baseline_parts 的 2*2*4 窗 输入: data/snapshot_baseline_parts/{sna |
| `startup_check.py` | 244 | 启动核验 —— 服务起来后跑一遍，确认生效参数与配套子系统全部在位。 只读工具：import app.config（磁盘当前值）、HTTP 读本机服务、读 data/ 下 JSON |
| `storage_gc.py` | 359 | B3（2026-09-13）存储治理工具 —— 默认只盘点不删除。 子命令（全部先输出 JSON 留痕，--apply 才动文件）： --inventory 只读盘点 data/： |
| `tactic_backtest.py` | 510 | W1｜「首板→回调→吃第二涨停」战法历史回测工具（只读快照库，零出网） 用法（统一 py -3.13 运行；数据缓存于 tmp/w1/）： py -3.13 tools/tacti |
| `tactic_lianban_backtest.py` | 590 | tactic_lianban_backtest.py — V1 连板梯队历史回测（pack13） 纯只读快照库；零落地。输出 data/bt_tactic_lianban.json |
| `test_f1_singleflight.py` | 156 | F1（2026-09-13）实时性隔离 验收测试。 判据（预注册）： 1. 单元级 single-flight：monkeypatch _http=sleep(3) 假实现 + t |
| `test_f2_pool_churn.py` | 138 | F2（2026-09-13）常驻线程池收敛压测（10 分钟）。 验收判据（预注册）： A. 压测结束时 threading.enumerate() 中 ThreadPoolExec |
| `test_h1_equivalence.py` | 287 | H1 数值等价性护栏：新 numpy 实现 vs _legacy 旧实现 - 30 只股票 × 全历史（主板/创业板/科创板/短史/长史/除权样本） - 对每个改造函数对比 max |
| `test_h2_pit.py` | 121 | H2 PIT 测试 + 数值等价抽查： 1) _hist_klines 返回视图严格截止 i（含），篡改 i 之后数据不影响第 i 日信号/分数； 2) _score_stock_ |
| `test_i1_pit_guard.py` | 108 | I1 未来函数护栏：静态扫描 app/ 中所有 earnings_signal( 调用点， 断言回测可达路径上的每一处都显式传了 as_of（且 as_of 非空时 offline |
| `test_i2_parity.py` | 118 | I2 实盘/回测评分一致性（parity）测试。 覆盖验收判据： 1. 旧口径对拍：engine 旧回测（score_stock 无加分）== score_final(ctx=No |
| `test_i4_exit_parity.py` | 161 | I4（2026-09-13）：卖出定价对称性自动验证。 对同一组 (base_px, qty, code, 市值档位, day_amount, 涨跌停) 输入，断言： A. exi |
| `trade_attribution_deep.py` | 723 | ★ Phase30：信号级交易归因 —— 回答"谁在赚钱、谁在亏钱" 独立进程运行（与 Phase23/26 同模式：直接 import app.engine，不走服务 HTTP） |
| `validate_gtja_factors.py` | 210 | ★ 阶段3 GTJA191 量价因子验证：IC/ICIR/分层单调性/与现有因子相关性 用法：python -m tools.validate_gtja_factors 输出：do |
| `warm_cache.py` | 94 | ★ J2（2026-09-13）：启动预热工具——tactics / sentiment/history / stocklist。 在服务启动后（或计划任务里）调用，使首个用户请求 |
| `xdxr_verify.py` | 101 | P71 xdxr 除权误报复核 — 复用 repair_qfq.py 的 mootdx xdxr 通道（系统 Python） 对 data/qfq_triage.json 中 tr |

## research（44 个，6417 行）

| 脚本 | 行数 | 作用（首段 docstring） |
|---|---|---|
| `atr_exit_backtest.py` | 224 | C4 ATR 跟踪止损 vs 固定阈值，退出链对照回测（预注册判据见 tmp/pack19/C4_atr_exit.md §0/§0a） 基线 = V1 A1 卡（收盘打缩量非一字 |
| `audit_rename_b4.py` | 53 | B4 一次性历史重命名脚本（2026-09-13，用户已确认映射表）。 只 os.replace 改名，内容零改动；先打印映射表，执行后打印 SHA256 前后比对。 执行顺序：1 |
| `backfill_amount_recent.py` | 163 | A2（2026-09-13）：回补 amount=0 且 volume>0 的日K成交额。 背景：tdx 主源熔断降级腾讯/新浪（两源均无 amount）→ 09-09 起新日期  |
| `backtest_index_timing.py` | 128 | ★ 阶段2 指数择时闸门 A/B 回测：开/关对比（策略=score，同一股票池，其余参数完全一致） 用法：python -m tools.backtest_index_timin |
| `backtest_ladder_tp.py` | 54 | ★ 4.6 阶梯止盈回测对比：LADDER_TP_ENABLED 开/关 vs 现状全清 本池：本地一年日K数据（≥320 根），score 策略，与阶段2 A/B 同口径。 输出 |
| `backtest_volprice_sell_strategy.py` | 53 | ★ 4.6 量价择时卖出 策略级 A/B：阶梯止盈常开(True) 前提下，量价卖出 开/关 本池：本地一年日K（≥320根 82 只），score 策略，与阶段2 A/B 同口径 |
| `bench_fixtures_build.py` | 60 | H4 基准集生成：从快照库只读导出 3 只标准股票 × 固定 1250 根日K → data/bench_fixtures.npz。 离线基准（bench_score/bench_ |
| `bench_mp.py` | 63 | 实测多进程评分 vs 多线程评分（Windows spawn + pickle 传参成本全计入）。 评估后再施行：若进程收益 < 0.5s 或启动成本 > 收益，倾向评分结果缓存方 |
| `bench_profile2.py` | 64 | scan_once 全环节计时（粗筛后），定位剩余热点。 |
| `bench_recall.py` | 79 | 召回评估：若评分池按 amount 粗筛到 top-N，会漏掉哪些最终高分股？ 复刻 scan_once 候选生成与评分，记录 (amount排名, score)。 |
| `bench_scan.py` | 62 | 安全端到端 scan_once 计时：拦截真实买入（check_buy 恒拒绝）， 对比 TDX 开/关 + moneyflow 熔断效果。 |
| `bench_scan_profile.py` | 61 | 带调用计时的 scan_once 压力测试：统计 moneyflow/consensus 等热点调用耗时。 |
| `board_component_attribution.py` | 298 | ★ Phase41: score_board 成分归因 —— 复用 Phase30 方法，度量打板评分每个成分。 方法： 1) 独立进程跑 board 基线四窗口（配方：bt_po |
| `bt_a1_entry_filter.py` | 225 | C3（2026-09-13）：A1 打板入场端过滤研究（只读研究脚本，不改生产代码） 背景：atr_exit.md §8 结论——救"延长持有"的方向在入场端而非 ATR 参数；  |
| `bt_a1_fill_prob.py` | 250 | C4｜A1 可成交性建模（涨停价排队成交概率）——研究脚本（零落地） 口径与判据预注册（先于结果落盘，跑完不改；报告 §0 同文）： 1. 覆盖率判据：min5 对 A1 历史信号 |
| `bt_exclude_regression.py` | 78 | P71 冻结基线重跑 — 用最新快照重跑 score+board 四窗，量化排除扩容影响 配方: bt_pool top500, fetch_quotes置空, score 25/ |
| `bt_exclude_regression_bg.py` | 64 | P71 冻结基线重跑（后台版）— score 4窗慢（500-1800s），用 bt_snapshot_baseline 风格分治 |
| `bt_ml_weight_merge.py` | 71 | ★ Phase25: 汇总 8 配置×4 窗口 → data/bt_ml_weight.json + 判定表 |
| `bt_ml_weight_worker.py` | 90 | ★ Phase25 高速回测 worker：(strategy, weight, window) 单窗口一进程。 对齐说明（关键）： - 复现 Phase21 基线的精确条件（tm |
| `bt_noexcl_arm.py` | 82 | P71-A1 干净隔离臂：无排除(DATA_EXCLUDE_CODES=[])+momentum 8.0 同快照对照 目的：把"P54冻结MOM7.0→现默认8.0"的动量口径差异 |
| `cyq_build.py` | 297 | C1｜筹码分布（CYQ）本地重建引擎 验收批文选型（C0 94/100 签发）： - 分布假设：三角分布主口径（峰=当日均价 amount/volume，面积归一，峰高 2/d）； |
| `cyq_grid.py` | 174 | C2｜筹码指标 × 现有两卡 融合回测（判据预注册后执行） 基线 = W1R 推荐格 E2_fb_open_vol_shrink + yang9_M IS=2019-01-01.. |
| `cyq_strata.py` | 240 | C2b｜筹码分位分层检验（C2 的换尺重测，全新预注册） 设计：盲买宇宙（E1）+ 五分位分层 + 买点日采样。 - 事件：event_mask(pre=7,brk=20,vol= |
| `demo_miner_feedback.py` | 97 | ★ 阶段5 LLM 因子挖掘闭环升级演示：3 轮挖掘（开关全开，provider=local 离线可测） 展示：失败原因回注下一轮提示 / 相关性>0.8 拒绝入池 / walk- |
| `exit_pack_v2_scan.py` | 164 | ★ Phase68: 卖点策略包 v2 参数研究（monkeypatch config，零 app 修改）。 候选三项（P45 审计 + P36 已扫项的快照口径复验）： A 移动 |
| `fetch_index_kline.py` | 89 | ★ 阶段2 指数日K入库：把三大指数日K写入 market.db kline 表（code='sh000001' 等） 供指数择时闸门（app/index_timing.py）离线 |
| `fix_kline_dates.py` | 141 | ★ P0-1 一次性清理：修复 kline 表 period='day' 的日期格式分裂 + 清除非日期垃圾行 问题1：旧版 tdx._bars_one 对日K也走了"'YYYY- |
| `global_map_verify.py` | 532 | ★ Phase39：全球市场→A股映射规则的历史验证 对 app/global_market.py ASHARE_MAP_RULES 的 4 条可回溯规则做历史验证 （汇率规则依赖 |
| `global_map_verify_full.py` | 415 | ★ D1：全球市场→A股映射规则的"真实全史"复验。 背景（任务书）：Phase39 复验时外部序列被源头截断——VIX 仅 2018-11 起（CBOE 在线 窗口）、WTI 降 |
| `make_icon.py` | 131 | 生成天玑量化终端应用图标（纯标准库，32-bit BMP ICO） 设计：深蓝渐变底 + 金色上升K线蜡烛 + 白色上升箭头（"天玑"定位财富坐标） |
| `min5_check.py` | 114 | C3 min5 抽验：日线近似臂在 min5 可得窗口（2025-08-18 起）的触发一致率。 方法：取 OOS 段四臂触发事件中 buy_date ∈ min5 覆盖窗口者，用 |
| `qlw_backtest.py` | 324 | 「潜力挖掘」策略复现回测（本地等效管线） 依据：用户下发交接说明书 §3/§7 + tmp/qlw/pre_registered_judgment.md（2026-09-10 预注 |
| `qlw_fetch_tencent.py` | 122 | 腾讯 fqkline 全市场拉取（说明书 §4 指定源） - 区间 2025-08-01 ~ 2026-09-10，320 根 qfq - 返回 [date, open, clos |
| `regime_gating.py` | 411 | 任务4｜大盘状态门控（研究型，只读库 + 新增数据文件，不改 app/config.py） 三臂结构： [labels] 全A等权线（bt_pool top500 剔除 DATA_ |
| `rejudge_after_semantic_fix.py` | 86 | ★ Phase70 复验收口：window_pass 语义修正后，重算全部受影响判定。 |
| `rejudge_with_judge_kit.py` | 92 | ★ Phase70 验收：迁移后用 judge_kit 重算各工具已存档判定，检测结论翻转。 |
| `repair_index_kline_fields.py` | 74 | ★ F1（2026-09-08）一次性修复：指数日K 三值轮换（high/low/close 错位）。 腾讯 fqkline 数组序 [date,open,close,high,l |
| `selfcheck.py` | 84 | C3 自检：2 臂 × 20 样本手工复算 + n 守恒。 手工复算：对每个选中的样本，直接用 kline_feat 的 open/close/volume 独立重算 apct / |
| `split_db.py` | 62 | v3.7 分库迁移脚本：kline_min5 → min5.db，VACUUM 瘦身 market.db |
| `test_f3_force_refresh.py` | 67 | F3（2026-09-13）打板行情时效保障：离线自检。 判据（预注册）： 1. force 透传：fetch_quotes_trading(codes, force=True)  |
| `test_f4_latency.py` | 101 | F4（2026-09-13）主循环相位延迟指标化：离线自检。 判据（预注册）： 1. 埋点自身开销：相位埋点（deque append + 时间戳）每轮 <= 1ms（微基准 10 |
| `test_h2_diff.py` | 64 | H2 差异定位：50 池 × score 5年窗，逐日对比 sc.score_final(ctx=None) 与 bt._score_stock_at，输出第一个不一致的 (cod |
| `test_h3_cache_invalid.py` | 80 | H3 缓存失效验证（任务书判据 J2）：改动库内数据（测试副本）→ 指纹变化 → 缓存失效重算。 步骤： 1. 复制快照 2026-09-11/market.db → tmp/h3 |
| `time_stop_reverify.py` | 234 | ★ Phase77: 时间止损 T=2 复验 —— P68 最接近达标项的预注册二次验证。 预注册内容（跑前写死，判定只用本文件常量与 judge_kit）： 新增臂： T2 :  |

