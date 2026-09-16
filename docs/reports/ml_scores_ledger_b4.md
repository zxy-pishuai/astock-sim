# B4：ml_scores 前瞻台账 —— 工具与机制说明

- 时间: 2026-08-27 ｜ 状态: **上线（首日已登记）** ｜ 服务未触碰

## 1. 解决什么问题

P28-A 曾实证 `score_stock` 打分对 fwd5 的 IC 为 −0.147；ML 侧在 P75/B1 后确立了
「预测力集中在隔夜腿」的认知。但所有这些结论都是**事后回算**——夜间生成的
data/ml_scores.json 从未被"事前登记、事后兑现"地跟踪过。本台账补上这块前瞻
监控：每天收盘后登记打分极端分位，T+1 出现真实 bar 后自动兑现收益，累计满
20 个已实现信号日自动出 IC 报告。

## 2. 工具与数据流

- 工具: `tools/ml_sidecar/scores_ledger.py`（纯标准库，系统 Python；compile 通过、LF）
- 调度: `tools/ml_scores_ledger_daily.cmd`（范式同 forward_eval_daily.cmd，
  日志 data/ml_scores_ledger_cron.log）。挂计划任务示例：
  `schtasks /Create /SC DAILY /ST 15:35 /TN "ml_scores_ledger" /TR "C:\...\tools\ml_scores_ledger_daily.cmd"`
  （15:35 ≈ 收盘数据更新后、次日开盘前窗口内即可，晚于当日 updater 完成更佳）
- 账本: `data/ml_scores_ledger.jsonl`，纯追加两类行：
  - `pick`: {date, code, bucket(top|bottom), rank, score, **asof**(该票自身末根bar日),
    close_asof, n_scores, generated}
  - `ret`: {date, code, ret_c2c(=close[T+1]/close[asof]−1), ret_o2c(=close/open[T+1]−1),
    gap_td(交易日间隔，1=严格次日), realized_at}
- 幂等：同信号日不重复登记（kind=pick 的 date 唯一）；已兑现的 (date,code) 不重复
  追账。实测：首 tick 追加 40 行，立即重跑追加 **0** 行。
- 口径要点：asof 用该票自身末根 bar（与 B2 滞后容忍打分诚实对齐），非全局锚日；
  停牌票顺延至 T+1 真实出现时兑现并带 gap_td 标记；头条统计只用 gap_td==1。

## 3. 上线状态（2026-08-27）

- 首个 tick 已登记：信号日 **2026-08-26**（总分 1700）top20+bottom20 = 40 行。
- 进度：已实现信号日 0/20；此后每个交易日由 cmd 触发 tick 自动登记+兑现。
- 满 20 个已实现信号日时自动生成：
  - `data/ml_scores_ledger_report.json`
  - 本目录 `ml_scores_forward.md`（滚动覆盖更新，含聚合 IC/ICIR/多空价差胜率、
    月度分解、最近 10 日明细）

## 4. 管线自检证据（--selftest-pred 25）

用 market.db `ml_pred` 最近 25 个历史截面作伪台账端到端走通同一套管线
（幂等索引/兑现 join/秩相关 IC/报告渲染全部复用正式代码路径）：

```
[selftest] 登记 1000 行 / 兑现 1000 行；不同实现日 25；join 1000
聚合: ic_mean=0.1815 icir=0.555 IC>0占比=0.72 spread_mean_bp=165.8 胜率=0.64
自检报告: data/ml_scores_ledger_report_selftest.json
```

⚠ 免责声明：自检对象是 ml_pred 的训练期自带标签口径分数（存在构造性相关），
其数字只证明**管线正确性**，不构成任何实盘预期；真实结论以自然积累 20 日后的
`ml_scores_forward.md` 为准。

## 5. 过程记录

- 自检路径曾踩"遍历中追加 ret 行"导致 KeyError('asof')（已修为快照迭代）；
- do_tick 曾有 todo list 误用 .items() 的笔误（未写盘即崩，无数据污染，已修）。
