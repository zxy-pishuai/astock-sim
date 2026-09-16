# Backlog 总台账（P35–P72）

> 维护规则：每个阶段完成后由执行者更新本表（状态/结论/文件归属）。
> "状态未知"的行表示该阶段非本台账维护者执行且仓库内未见对应交付物，
> 请负责人补录。最后更新：2026-09-13（D1-告警双轨/D2-审计巡检）。

| 阶段 | 主题 | 状态 | 结论 / 摘要 | 关键文件 | 执行者 |
|---|---|---|---|---|---|
| P33 | 打板动量档位下限 | ✅ 完成 | 网格 {7.0,7.5,8.0,8.5} → 8.0 达标（3/4 窗口改善、牛市 +25.57pp），默认 7.0→8.0（2026-08-25 判定） | docs/reports/board_tier.md; config.BOARD_MOMENTUM_MIN | 他人 |
| P35 | 数据修复包 | ✅ 完成 | 600262/600426 前复权重修复成功（high 跳变 2→0，无需增补排除清单）；DINIW 经新浪源回补 6,939 行 | tools/data_repair_20260825.py; data/backups/; docs/reports/data_repair.md | ox-alpha |
| P36–P38 | —— | ❓ 待补录 | 仓库内未见对应交付物 | —— | 待补录 |
| P39 | 全球市场→A股映射历史验证 | ✅ 完成 | 无规则达"保留"门槛；港股科技利多有微弱稳健正向力（建议降阈值复测）；油气/VIX 无方向信息 | tools/global_map_verify.py; data/global_map_verify.json; docs/reports/global_map_verify.md | ox-alpha |
| P40–P43 | —— | ❓ 待补录 | 竞价网格/打板组件等报告或属此区间（auction_grid / auction_w2s / board_components / true_board），编号对应关系待负责人确认 | docs/reports/{auction_grid,auction_w2s,board_components,true_board}.md | 他人 |
| P44 | PORTFOLIO_METHOD 对照 | ✅ 完成 | 维持 risk_parity（equal 触发恶化否决 -6.43pp）；equal=关闭组合优化、vol_target 常态只建 33~44% 仓、hrp 在 board 下与 rp 等效 | tools/portfolio_method_ab.py; data/bt_portfolio_ab.json; docs/reports/portfolio_ab.md | ox-alpha |
| P45 | trader↔engine 一致性审计 | ✅ 完成 | 致命×3（情绪/regime 动态闸门无回测对应、打板专属退出缺失、实盘买入七道过滤链）+ 中×5；修复前 board 回测与 score 绝对收益不可外推（详见报告置顶清单） | docs/reports/live_backtest_audit.md | ox-alpha |
| P46 | 更新后数据快照冻结 | ✅ 完成 | run_update 成功后 auto_snapshot 冻结当日快照 + 数据哨兵巡检写入 quality_alert.jsonl | app/data_snapshot.py; app/updater.py（Phase46 段） | 他人 |
| P47 | —— | ❓ 待补录 | dd_gate_v2 / index_timing_retest / exit_scan 等报告或属此区间 | docs/reports/{dd_gate_v2,index_timing_retest,exit_scan}.md | 他人 |
| P48+49 | 引擎可信度收敛 | ✅ 完成 | F 跌停禁卖顺延 + E 卖出市值分档 + B board 专属退出；board 四窗 +9.3~+30.0pp 且回撤普降；score 熊窗变差系真实性代价（旧数字被"跌停可卖、零冲击卖出"美化） | app/engine.py 三处；tools/bt_convergence_runner.py; data/bt_engine_convergence.json; docs/reports/engine_convergence.md | ox-alpha |
| P50–P56 | —— | ❓ 待补录 | min_sell_verify / qfq_triage_20260826 / acceptance_20260825 等或属此区间 | docs/reports/{min_sell_verify,qfq_triage_20260826,acceptance_20260825}.md | 他人 |
| P57 | 前瞻台账运转化 | ✅ 完成 | 锚日 2026-08-25 恢复记账（幂等验证）；计划任务 AStockForwardEvalDaily 每日 18:30；global_market 新增「港股科技±1%」实验规则（HINT_PROXY 已映射电子信息） | tools/forward_eval.py(+1行映射); tools/forward_eval_daily.cmd; app/global_market.py; data/forward_eval.jsonl; 计划任务 AStockForwardEvalDaily | ox-alpha |
| P58–P61 | —— | ❓ 待补录 | 仓库内未见对应交付物 | —— | 待补录 |
| P62 | min5 覆盖扩容 | ✅ 完成（时长目标未达） | 动态清单 1,002 只（bt_pool500∪持仓∪自选∪近90日交易∪库内496）✅≥800；存储 +20.4MB（160,720 根）✅；**稳态 ~24s ≈ 现状 3×，超 1.5× 目标**（远端按 IP 限流，16 并发反而更慢）。缓解路径：TDX 批量接口 / 清单分片隔日轮换 | app/updater.py min5 段（_min5_dynamic_codes 等）; data/p62_expand_measure.json | ox-alpha |
| P63 | 全球源加固 | ✅ 完成（GC 部分回补待续） | VIX 经 CBOE 官方源回补全史 9,257 行（1990 起）；CL 经东财 CL00Y 回补 7,029 行（1999 起）；DINIW 增量正常。主备切换与降级机制落地并实测（CL 备源 USO、DINIW 备源东财 UDI）。GC 仍 4 行：东财限流 + 新浪 GLD 端点不兼容，随每日调度自动重试 | app/updater.py 全球段（update_global 及源链）; data/forward_eval_cron.log 无关；审计事件 global_update_failed | ox-alpha |
| P64 | PIT 股票池与幸存者偏差量化 | ✅ 完成（D3 已无偏化） | 偏差方向=高估、幅度大。~~降级模式~~ **★D2(08-27) 补建 w0**：回补 2019 前日K +463.6 万行重建四窗；**★D3(08-27) 退市宇宙补全**：baostock 免费源拿到全 A 宇宙（5,549 只，退市 337）+ 摘牌前全史K线，310 只/85.96 万行入库后四窗池改写（w0 换血 14%，含海通/葛洲坝/小天鹅等被吸并巨头——偏差主体是并购退市而非垃圾股）；无偏口径八组 Δ 全负（−11~−43pp），现行池全面偏乐观结论无反例。见 pit_pool.md 附录 D2/D3 | tools/build_pit_pool.py; tools/backfill_pre2019_w0.py; tools/build_delisted_universe.py; data/{pit_pools,bt_pit_compare,delisted_universe,delisted_written_manifest,d3_pool_diff}.json; docs/reports/pit_pool.md | ox-alpha |
| P65–P69 | 影子打板 / 卖出语义对齐 / 评分权重 A/B（补录） | ✅ 补录完成 | **P65** 真实打板影子运行：`tools/shadow_board.py --record/--settle/--backtest`，纯记账不碰账户；M2 模型（p_early=0.30/p_late=0.60/cutoff 10:30/seed=42）P66 验证 20 日均值未达标 → 维持模拟。**P66** 打板评分权重 A/B：`tools/board_weight_ab.py` + `scoring.board_weights` 覆盖入口；主变体 2/4 达标 → 不落地。**P60/P61** 卖出走 execute_price(impact) 对齐与 warmup 270 修复：两开关默认 False 无漂移。**P68/P69** 夜间波与回流对照按验收流程另档（夜间波机制归 Phase76 全局闸门段）。 | tools/{shadow_board,board_weight_ab}.py; data/{shadow/,board_weight_ab.json}; docs/reports/{p60_p61_warmup_and_watch,board_weight_ab_20260826}.md; app/scoring.py; app/board_true.py | ox-alpha |
| P70 | 数据源断源演练 | ✅ 完成 | 子进程掐网全链路演练全部通过，断源手册自动生成（含各源后备援链与处置要点） | tools/drill_sources.py; docs/operations.md「数据源断源手册」 | 他人 |
| P71 | 排除扩容+xdxr 复核+冻结基线重跑（补录） | ✅ 完成 | DATA_EXCLUDE_CODES 136→**197**（剔指数 2 + xdxr 升级 21 + 哨兵全量后新真污染 42）；`data_sentinel.py` 候选上限 300→1500（1129 全量入库）；xdxr 复核 23/23 无除权全升级；三臂归因：board 干净排除效应 -1.73pp/四窗均值（近1年 +16.0pp），score -12.62pp 属幻影利润挤出；A2 比亚迪 002594 已修复（+207.1%→+0.40%，复检归零，已于 08-27 07:30 移出 DATA_EXCLUDE_CODES 197→196；批外落地当时无留痕，08-30 验收反推确认并补记）；A3 分流周常化计划任务 `A3_TRIAGE_qfq_weekly`（周日 09:00，只产建议不落地）；A4 全球薄源回补 GC 4→2589 / USDCNH 4→3079 / DJIA 1→5700，六链主备固化进 updater 全球段，哨兵 thin 全 false | app/{config.py(仅排除段),updater.py(全球段)}; tools/{xdxr_verify,qfq_triage,data_sentinel(上限行),bt_exclude_regression_bg,bt_noexcl_arm,global_thin_backfill,repair_002594}.py; data/{qfq_triage.json,xdxr_verify.json,bt_exclude_landing_regression.json,global_thin_backfill.json}; docs/reports/{exclude_landing,byd_repair_002594,global_thin_backfill}.md; 计划任务 A3_TRIAGE_qfq_weekly | ox-alpha |
| P71-A5/A6 | 台账与标注回写（补录） | ✅ 完成 | 本节 + 历史报告 P64"静态池偏乐观"标注批量回写（engine_convergence/portfolio_ab/board_tier/true_board/phase10 共 5 篇文末） | docs/backlog.md; docs/reports/*（标注） | ox-alpha |
| P72 | 台账与文档 | ✅ 完成 | 本文件 + operations.md 增补「同日 A/B 验收流程 / 快照口径 / 重启 runbook」 | docs/backlog.md; docs/operations.md | ox-alpha |
| P73 | 择时快照复验（P73） | ❌ 不通过 | 快照 4/4 齐（+08-31，`rows_kline_day 8701436`）；`--execute` 16 格/0 错误 22:51 完成（`data/bt_it_snapshot_retest.json`，snapshot_used=08-31）；score/board 均不通过（bull_ok 全 fail）：近1年 score **−14.05pp** / board **−20.28pp**，与 8/26 回滚依据（近1年 −17.57pp）方向一致 → **择时不具备重新评估价值，两开关维持 False**，`app/config.py` 零改动 | tools/index_timing_snapshot_retest.py; data/bt_it_snapshot_retest.json; docs/reports/timestop_index_reverify.md §B | 他人 |
| P77 | 时间止损T=2快照口径复验 | ✅ 完成 | T2 三臂全不通过（牛市 +0.75~22.58pp，前两窗多窗 >1pp），与 C 批"T2 是毒药"一致；T3 两复用臂反而达标（3窗、牛市 -28/-31pp）。JSON 自带 verdict 的 windows_ok/pass 与 prereg 复核不一致已披露（windows_ok 5/5、pass 3/5，2026-08-31 独立复核确认），以 judge_kit 为准；维持 T=3 | tools/time_stop_reverify.py; tools/judge_kit.py; data/bt_time_stop_reverify.json; docs/reports/timestop_index_reverify.md §A | 他人 |
| D1-告警双轨(09-13) | 告警双轨打通（quality_alert→audit→前端红条→升级） | ✅ 完成 | amount_zero_guard 连报 3 天无人知晓的痛点闭环：quality_alert 每条 WARN/CRITICAL 同步写 audit（quality_alert 事件+source+streak_days，哈希链完整）；同 event 连续 ≥2 交易日自动升级 WARN→CRITICAL + audit quality_alert_escalated + alert.notify 推送；前端红条（market 页 qa-notice，overview.quality 数据源，覆盖 amount 缺失/复权跳变/ML 陈旧/快照缺失/升级记录）；quality_alert.jsonl 512KB 轮转归档+90 天保留。验收：迷你副本库 guard 注入 CRITICAL streak=3 + 纯两天正向升级 + 哨兵三项转发 + 链完整 | app/{audit,updater,server}.py; tools/data_sentinel.py; web/{index.html,js/app.js}; docs/reports/d1_d2_alert_dual_track_20260913.md | 我 |
| D2-审计巡检(09-13) | 静默失败防护（INFO 级延后/跳过事件自动升级） | ✅ 完成 | 白名单（data_update/daily_coverage|pending/data_snapshot*/heartbeat）按交易日查连续缺失→CRITICAL；黑名单（data_snapshot_deferred/stale_first_budget_exceeded/daily_coverage_pending/amount_zero_guard/watchdog_stale）连续 ≥2 交易日→升级并写明天数；结果写 audit（audit_watch 事件）+ quality_alert.jsonl。回放 09-09~09-11：快照连续 3 天缺失 CRITICAL + stale_first_budget_exceeded/amount_zero_guard 连续 3 天升级全命中。计划任务 TianjiAuditWatch 每日 21:30（已注册，回滚 schtasks /delete /tn TianjiAuditWatch /f） | tools/audit_watch.py(+.cmd); 计划任务 TianjiAuditWatch; docs/reports/d1_d2_alert_dual_track_20260913.md | 我 |
| E4 | 关键数据"单点源"风险盘点 + 降级补偿钩子 | ✅ 完成 | 三源定论（实测）：tdx 有额、腾讯/新浪 6 字段无额、东财/腾讯 qt 有额。降级留痕 data/source_degradation.jsonl（datafeed 腾讯/新浪日K入口，_fetch_daily 与 _fetch_daily_one 两链全覆盖）；收盘补偿 _compensate_amount（主=qt.gtimg.cn 批量万元×10000 校验 target 日，备=东财 push2his 主机轮换；只补 target 日 amount，走 _kline_save 条件覆盖不碰 OHLCV）；audit source_degradation_daily 汇总（各源命中+补偿成功率）。验收：降级链 jsonl 记录 ✓、补偿后完整率 1.0 ✓、audit 汇总 ✓（mini 副本库 monkeypatch C.DB_FILE，生产未动）。遗留：指数 amount 无源未补（无消费方）、历史日回灌属 D4a、东财间歇风控 | app/{datafeed,updater}.py; data/source_degradation.jsonl; docs/reports/e4_source_singleton_20260913.md | 我 |

## 遗留风险看板（跨阶段）
0. **静态池偏乐观（P64 量化，全文性声明）**：所有历史报告的静态池绝对收益数字
   均系统性高估（score 近1年 −67.7pp、board 三窗 −21.6~−25.2pp），相对性结论可用、
   绝对水平勿外推；相关 5 篇已加标注（2026-08-27），新报告按 P72 模板双列呈现。
1. **23 只票 2024-12-30 跨段污染**（P31 发现、P35 报告）：待批处理重修复，
   修复前 score 回测绝对数值持续受污染（P44/P48+49 报告均已声明）。
   同型接缝单票先例已跑通：002594（P71-A2，`tools/repair_002594.py --check/--apply`）。
2. **P45 致命-A/C 未闭环**：实盘过滤链与情绪闸门无回测对应物，属产品形态差异，
   建议以"回测标注 + 过滤链抽象注入"方式治理而非强行对齐。指数择时两开关实测 `False False`（`app/config.py:209/215`，config 文件 `mtime 2026-08-27 07:30:18`，`L215` 注释内 `2026-08-26` 为回滚决定日期，2026-08-31 订正原 mtime 误写）、运行时 `index_timing` 事件 0 条（`data/audit/audit.jsonl` 2671 行，`2026-08-26` 之后 0 条，全库 0 条，无最后出现时刻，审计 `event` 枚举见 timestop_index_reverify.md §C）；重新启用门槛 = 现行 engine 口径重测（`config L215` 原文指向 `bt_config_landing_regression.json`），候选路径 = P73 快照口径复验（`tools/index_timing_snapshot_retest.py --execute`，快照已 3/3 就绪，待今日 15:10 收盘后 08-31 新快照锚定，见该报告 §B）。
3. **前瞻台账样本积累中**（P57）：港股科技±1% 实验组需 60 个触发日，
   预计 2026-11 月前后可出首个判定。
4. **D1 全球映射规则全史复验完成**（2026-08-27）：VIX（9,258 根，1990 起）与
   WTI 真实 CL00Y（7,029 根，1999 起）替换 Phase39 的截断源/USO 代理后，
   同保留门槛重跑五条现行规则+港股科技±1% 变体：**0 条达"建议保留"，前瞻台账候选无新增**；
   P39 三条主结论全部复现；「港股科技」利多在 ±0.5%~±3% 全档命中稳定 57~58%、
   超额仅 +0.2~+0.5pp（<1pp 门槛），维持 P57 前瞻实验组安排。
   工具 tools/global_map_verify_full.py；结果 data/global_map_verify_full.json；
   报告 docs/reports/global_map_verify_full.md
5. **D2 2019-20 PIT 池补建完成**（2026-08-27）：核验 Phase15"含 2019 前全历史"
   表述未落库（2018 仅 12 只票）→ `tools/backfill_pre2019_w0.py` 只写 <2019-01-05
   回补 +463.6 万行（2018 覆盖 1,522 只，失败 5 只）；修复 build_pit_pool 成交额
   下界硬编码与重复 `_run_task`；w0 PIT 池建成（资格 1,508、真·窗前 60 日排序，
   与现行池重叠仅 188/500）。八组对照补齐，P64 唯一缺窗关闭。
6. **D3 退市股宇宙补全**（2026-08-27）：免费源三渠道探明可用（baostock 权威清单
   +摘牌前全史K线 / akshare 交易所终止上市表交叉验证 / mootdx 不适用）；
   baostock 单位与库内 ratio=1.0000 校准。310 只/85.96 万行入库（幂等可重放，
   回滚模板在 manifest），四窗池改写——w0 换血 14%，无偏估计八组 Δ 全负、
   无反例；残余偏差仅剩 2005 前退市且无K线的 27 只。
   `tools/build_delisted_universe.py --refresh` 可随新退市事件增量维护。
7. **D4 双价观察分析工具落地**（2026-08-27）：`tools/dual_price_weekly.py`
   （dry-run/--write/--selftest，聚合口径自检通过）+ `dual_price_weekly.cmd`。
   **存量 dry-run=0 条事件**——判定"无定价发生"而非信号被吞：采集器健康
   （1875 行/0 坏行/心跳连续），但观察期内策略自动买卖路径零触发，且 manual_buy/
   test_buy 走 state.py 定价不入观察流；分叉充要条件=真实 qty 且占当日成交额>5%。
   **附带发现**：audit 哈希链断点 339/1875（多进程并发写所致，集中 08-20/08-26），
   建议后续 record 改读文件尾作 prev；切换评审参考线已预注册在周报文档首节。
   报告 docs/reports/dual_price_observation.md（周表将随事件自动追加）。
8. **D5 实盘vs回测周度归因落地**（2026-08-27）：`tools/live_vs_backtest_weekly.py`
   （--selftest 6/6 通过；account.trades 全量重放 MTM 净值 + 同周 score/board
   回测基准，全部只读）。首期两窗口：W34 实盘 −3.43% vs score −0.40%（gap −3.03pp，
   样本仅 4 笔不作策略判定）；W35 手动单 −0.56%。全程已实现 −3,935.54/总收益
   −3.97%，费用拖累仅 −0.15% 非主因。**审计覆盖缺口量化**：策略买入 0/4、卖出
   0/5 有事件——成交全集只在 account.json；建议后续 trader._buy/_sell 接入统一
   审计。gap 解读纪律预注册：连续 ≥4 周同向负 gap 才立项复盘。
   报告 docs/reports/live_vs_backtest_weekly.md。
9. **任务1｜审计链统一完成**（2026-08-27）：① trader._buy/_sell 成交路径接入
   strategy_buy(order)/strategy_sell(position) 事件（字段对齐 manual_* 语义，
   交易逻辑零改动）；② audit.record 重构为"进程锁+跨进程 .write.lock 字节锁+
   写前重读磁盘尾 prev+fsync"单写者语义；③ tools/audit_chain_diag.py 只读诊断
   （历史 339 处断链存档 data/audit_chain_breaks.json，绝不修补）。
   4 进程并发实测：strategy_* 100/100 连续入链零断裂；唯一新增断链来自未重启的
   旧版服务（行 2124，损害止步单条自愈）。155 个项目 py 编译通过。
   **附带发现**：scan_once 内 pos_cash 先用后赋（L655 vs L664）存量缺陷疑为
   近期策略自动交易零触发的直接原因——触碰交易逻辑留待专项修复。
   报告 docs/reports/audit_unify.md。
10. **五任务批次验收 + 任务4 整改**（2026-08-30）：验收报告
    `docs/reports/acceptance_20260830.md` —— 任务1/2/3/5 ✅、任务4 ⚠️→✅（整改后）。
    核心发现：① 8/27 07:30 批外移出 002594（197→196）当时无留痕，致任务4 基线
    (197)与门控臂(196)宇宙不一致——以 C 批同日 base 去混淆后四判定全部不变
    （"全部不落地"稳健），报告单窗 Δ 最多带 ~14pp 宇宙偏差（regime_gating.md
    附录 A）；② 审计链 340=339+1（行 2124 已声明自愈断链），其后零新增；
    ③ 8899 服务已停（批外状态，计划任务独立冷启动不受影响）。
    整改：regime_gating.md §1 更正 + 附录 A、`bt_regime_gating.json` 增
    `universe_confound` 块、本表 P71 行移出记录补记。
