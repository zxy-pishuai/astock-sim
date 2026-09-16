# E1 全量开关 ↔ 消费方 ↔ cron 三方对账（2026-09-13）

> 只读审计：**未改 app/config.py 任何开关值、未删/未改任何 cron**。所有数字实测于
> 2026-09-13（schtasks /query、config.py grep、cron 日志、watchdog.log、docs/reports）。
> 开关行号以当前 config.py 为准（并发 AI 已改动，派单行号有漂移，均以实际 grep 定位为准）。

## §1 开关 × 消费方 × 数据 × 回测结论 对账表

| # | 开关 | 行 | 默认与开启前置（config 注释原文要点） | 消费方（grep 实测读取点） | 产出数据模块/来源 | 数据当前就绪？ | 历史回测结论（docs/reports） | 建议 |
|---|---|---|---|---|---|---|---|---|
| 1 | INDEX_TIMING_ENABLED | L241 | False；实盘路径由 trader 读取；回测对比通过后才可开启 | trader.py L1127（打板买入预算乘数）；engine.py L153（回测 params）；index_timing.py | app/index_timing.py（指数 MA20/60、波动率分位、北向缓存） | 部分：指数线有；**北向披露 2024-08 起停更**（phase2 报告注"中性"） | phase2_index_timing.md（score A/B 闸门 76% 天数降仓）；**index_timing_retest.md（P43 干净数据：board 通过、score 未通过）**；INDEX_TIMING_BOARD_ONLY 2026-08-26 验收回滚（A/B −5.49pp，见 L247 注释） | **保持关闭**。score 路径 P43 判未通过；board 专属版需按现行 engine 口径重测（bt_config_landing_regression.json 前置） |
| 2 | SENTIMENT_POS_GATE | L424 | False；回测对比后决定 | trader.py L1143；engine.py L733；sentiment_gate.py L58；server.py L934（展示） | qg_zt_full 表/情绪序列（2019 起全历史，P22 已重建干净） | **就绪** | sentiment_pos_gate.md（P16：**不建议开启**，改善不显著夏普略降）；sentiment_gate_retest.md（P22 干净数据 4 窗口重测：**仍不建议开启**） | **保持关闭 / 建议明确下线**。两次回测判负；C3 入场端"高潮期"是另一用法（入场过滤≠仓位乘数），不推翻本结论 |
| 3 | ML_SCORE_ENABLED | L290 | False；无显著优势不接入（phase4） | scoring.py L161/L389（ml bonus 加分） | tools/ml_sidecar 训练 → data/ml_scores.json | **未就绪：ml_scores.json 停更 17 天（8/27 4:26 后无更新）**，超 ML_SCORE_MAX_AGE_HOURS=26 已失效降级 | phase4_ml_sidecar.md（40 日对比：ML IC −0.0009 vs score_stock −0.1475，"ML更优"但绝对值≈0 无预测力；结论=无显著优势不接入） | **保持关闭**。且上游训练管线已停更，先恢复管线或下线相关 cron（见 §2） |
| 4 | GTJA_FACTORS_ENABLED | L266 | False；接入评分前须先验证；权重已配 {"VOL变异20": -6} | scoring.py L355（GTJA bonus） | VOL变异20 = std(vol,20)/sma(vol,20)，本地日K | **就绪（零外部依赖）** | phase3_gtja_factors.md（15 因子仅 VOL变异20 达标：IC −0.0392 负向、|r|max=0.363 与现有池低相关）；validate_gtja_factors.py 验证工具在 | **可评估开启**（唯一有回测支撑的候选；负向 −6 小权重保守起步）；前置=跑 validate_gtja_factors.py 复核 + 用户拍板 |
| 5 | VOLPRICE_SIGNALS_ENABLED | L229 | False；候选信号须先回测验证再开启；**WEIGHTS={}（无入选信号）** | scoring.py L343（`ENABLED and WEIGHTS` 双条件） | 量价信号（本地日K） | WEIGHTS 空 → 即使开也无信号生效 | phase1_volprice_signals.md（4 候选全部弃用：3/5 日胜率<52% 或期望为负，市场基线 5 日胜率 48.6%） | **保持关闭 / 建议下线**（无入选信号，scoring 双条件等效关闭；框架保留即可） |
| 6 | VOLP_SELL_ENABLED | L124 | False；辅助口径，仅供研究/回测对比 | engine.py L572（回测引擎）；volprice_sell.py L28 | 日K量价卖出信号（本地） | 就绪 | 无独立成文报告（backtest_volprice_sell.py 工具在）；**min_sell_verify.md 是分钟级判负** | **保持关闭**（研究口径；分钟级已判负，日K级未成文达标，不宜开） |
| 7 | VOLP_SELL_MIN_ENABLED | L116 | False；开启前先回测/盘中验证 | trader.py L1788（每 5 秒）；minute_vol.py L195 | min5.db 分钟量比（本地） | 就绪 | **min_sell_verify.md（P32）：三条件全部不满足 → 维持关闭**（33% 改善<55%、平均改善 −1.93%、9 组网格无一通过；机理=把强势股盘中洗盘卖飞） | **建议下线 / 明确不要开**（回测判负 + 机理明确，再开无依据） |
| 8 | MINER_FEEDBACK_ENABLED | L276 | False；默认全部关闭保持原有挖掘行为 | factor_miner.py L434（失败原因回注） | LLM 因子挖掘（tools/demo_miner_feedback.py 演示） | 挖掘管线存在（演示模式跑通） | phase5_miner_feedback.md（三道闸演示：入池 1 因子 ts_std(volumes,20) IC −0.0517，拒绝 4 条） | **保持关闭**（非生产路径的实验增强；开启属实验，需先确认挖掘任务本身在跑） |
| 9 | MINER_CORR_REJECT_ENABLED | L277 | False（同上） | factor_miner.py L464（相关性>0.8 拒绝入池） | 同上 | 同上 | 同上（P5 演示含相关性拒绝 1 条） | **保持关闭**（同上） |
| 10 | MINER_WF_ENABLED | L278 | False（同上） | factor_miner.py L472（walk-forward 稳定性门禁） | 同上 | 同上 | 同上（P5 演示含 WF 判定） | **保持关闭**（同上） |
| 11 | SHADOW_BOARD_ENABLED | L365 | False；P65 影子运行只记账不下单 | **无代码读取 ENABLED 本身**（board_true.py 只读 FILL_P_EARLY/FILL_P_LATE/EARLY_CUTOFF 参数） | app/board_true.py（真打板影子，需实时盘口） | 参数在、总开关无人读 | 无成文 P65 报告 | **幽灵开关**：开启无效（无人读取）。需先确认 board_true 的启用路径（可能由别处控制）再决定接线或删除；当前 False 无害 |

**矛盾/注意项**：派单行号（L106/114/219/231/256/266/267/268/280/355/414）与当前 config.py 实际行号（L116/124/229/241/266/276/277/278/290/365/424）整体偏移 8~13 行——并发 AI（B3 等）插入了 SNAPSHOT_* 段所致，**无开关值被改动**（grep 实测 11 个开关全为 False，与派单一致）。

## §2 cron 对账表（触发 / 耗时 / 产出 / 是否被 True 开关消费）

| 任务 | 当前时刻 | 最近运行 | 退出码 | 脚本 | 产出文件（最近 mtime） | 产出被消费？ | 判定 |
|---|---|---|---|---|---|---|---|
| ml_scores_ledger_daily | **16:30**（F5 错峰后；Last Run 仍是旧时刻 9/11 19:23） | 9/11 19:23 | −1073741510（Ctrl+C） | scores_ledger.py | ml_scores_ledger.jsonl（**9/7 16:30 后无新行**）；ml_scores.json 8/27 停更 | **无**：ML_SCORE_ENABLED=False；且上游 ml_scores.json 停更 → ledger 无新 pick 可登记（幂等 no-op） | **纯浪费（当前）**：数据源停更导致空转；9/11 运行被中断（见 §3） |
| risk_daily | 20:00 | 9/12 20:00:01 | 0（正常） | risk_daily.py | data/reports/risk_daily/risk_daily_2026-09-12.md | 人工风控观察（非开关路径；RISK 熔断由引擎内建） | **非浪费**（合规观察面，秒级轻量） |
| forward_eval | 20:30 | 9/12 20:30:01 | −1073741510（Ctrl+C） | forward_eval.py | forward_eval.jsonl（**9/10 20:31 后无新行**） | 独立前瞻台账（无法回测信号的验证），与开关无关 | **部分浪费**：9/11、9/12 连续被中断，3 个交易日未成功记账 |

**ML 链路整体状态**：ml_scores.json（训练产物）8/27 停更 → ml_scores_ledger 无源可登记（9/8 起幂等空转）→ ML_SCORE_ENABLED=False 无消费方。**整条 ML 训练+台账链路目前空转**，建议人工确认 ml_sidecar 训练任务的调度是否已失效（本块未查其触发方，属未做清单）。

## §3 资源占用与晚间重叠（与 E3 交叉数据，2026-09-07~13 实测）

- **l2_overview 超时 7 天共 115 次**，恢复触发 **15 次**（13 次完成重启）——服务半死高频发作，**不是偶发**。
- 晚间批处理窗口（19:00-21:00）内的 l2 异常：09-08 2 次（20:25/20:45）、09-10 2 次（20:10/21:55）、09-11 4 次（19:23/19:30/21:00，其中 **19:23 l1 down 直接触发恢复**）、09-12 18 次（**19:35~21:40 密集，20:30/21:00/21:30 三次恢复**）。
- **09-11 19:23 ml_scores_ledger 运行（19:23:49）与 watchdog 恢复（19:23:54 触发）直接重叠**，ledger 被 Ctrl+C（日志尾 `^C`，退出码 0xC000013A）。
- **09-12 20:30 forward_eval 运行（20:30:01）与 watchdog 恢复（20:30:14 半死僵尸 kill）直接重叠**，forward_eval 被 Ctrl+C。
- 单任务耗时：risk_daily 秒级（20:00:02 完成 exit=0）；forward_eval/ledger 各在 1-10 分钟内被中断（未观察到正常完成样例）。
- 重叠根因（§E3 详）：09-12 晚间服务在 20:30 前已病态（audit_age 31185s=8.6h 不更新、l2 全 TimeoutError），**F5 豁免窗口 (20:00-21:30) 只认 `time_stale` 不认 `TimeoutError` → 豁免失效**，20:20 起每 5 分钟连续计数，20:30 触发杀+重启；批处理恰好落在恢复动作时刻被波及。

## §4 决策清单（每行：建议 + 预期收益 + 风险 + 前置工作）

| 开关/任务 | 建议 | 预期收益 | 风险 | 前置工作 |
|---|---|---|---|---|
| INDEX_TIMING_ENABLED | **关闭**（维持） | — | board 版若开：需按现行 engine 重测（旧测在引擎修复前，已回滚） | 若要开 board 专属：跑 bt_config_landing_regression 口径重测 |
| SENTIMENT_POS_GATE | **关闭**（建议明示下线） | 省分支判断（微） | 无 | 无（两次回测已闭环） |
| ML_SCORE_ENABLED | **关闭**（维持） | — | 若开：依赖已停更 17 天的 ml_scores.json，ML 分无预测力 | 恢复 ml_sidecar 训练管线 → 重跑 phase4 对比 → 再议 |
| GTJA_FACTORS_ENABLED | **可评估开启** | 评分层新增负向减分因子（量能不稳定票 −6 分），补强风控 | 权重 -6 影响面=全池 score；VOL变异20 IC 仅 −0.0392（弱） | 跑 validate_gtja_factors.py 复核 → 用户拍板 → 灰度观察 |
| VOLPRICE_SIGNALS_ENABLED | **下线**（无入选信号） | 消除死代码路径 | 无 | 无（框架保留，未来新信号达标再启） |
| VOLP_SELL_ENABLED | **关闭**（维持） | — | 开=未成文达标的卖出逻辑进实盘 | 如需：先成文日K量价卖出回测（工具已有） |
| VOLP_SELL_MIN_ENABLED | **下线**（回测判负） | 消除误导性配置项 | 无 | 无（P32 已闭环） |
| MINER_* 三闸 | **关闭**（维持） | — | 开=实验路径进挖掘管线 | 需先确认挖掘任务在跑 + 出正式 A/B |
| SHADOW_BOARD_ENABLED | **接线或删除** | 消除幽灵开关 | 无 | 确认 board_true.py 启用路径后决定 |
| ml_scores_ledger_daily | **人工确认是否暂停** | 消除"每天算没人用"空转 + 9/11 中断隐患 | 暂停=ML 前瞻台账断档（当前本就无源） | 先恢复 ml_scores.json 训练管线，或接受暂停 |
| risk_daily | **保留** | 风控观察面 | 无 | 无 |
| forward_eval | **保留但修中断** | 恢复前瞻台账连续性 | 9/11/9/12 中断已丢 3 日记录（幂等可补） | E3 错峰/标记机制落地后观察 |

## §5 红线与未做清单

- ✅ 未改 app/config.py 任何开关值；未删/未改任何 cron 任务（仅复核时刻表）；未动 watchdog/批处理脚本（E3 单负责）。
- 未做：ml_sidecar 训练任务触发方排查（ml_scores.json 停更根因）；GTJA 开启的 validate 复核执行；INDEX_TIMING board 版重测；SHADOW_BOARD 启用路径确认。
- 所有开关值、行号、mtime、退出码均为 2026-09-13 实测，可复现（grep/schtasks/日志路径已在文中）。
