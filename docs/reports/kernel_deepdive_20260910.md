# 内核深究报告｜组合管理 + 退出链 + 仓位（验收方 2026-09-10）

> **复核修正（用户质疑后二次核查）**：K5 "无题材集中度管理"**撤回**——`risk.py:
> sector_exposure/sector_pct_limit`（L226/348）+ `engine.py:786` 板块限仓 +
> `RISK_SINGLE_STOCK_PCT=0.45` 均已存在，核查失误致歉。K1 `combine()` 实为展示
> 接口（唯一调用方 `server.py:98`，不驱动下单）——"Bug 级"**降级为 P2 展示口径
> 打标**。K4 的 IS 窗口/权重问题仍成立（组合收益口径）。K2 阈值全系回测调参
> 产物，按 C 系预注册对待的原结论维持，但"固定百分比"措辞改为"固定百分比
> 阈值（经回测选定）"，不暗示拍脑袋。以下正文保留核查结论，K5 段删除。

## K1（Bug 级）· `combine()` 的"组合收益"是纸面加权，不是组合回测

- 证据：`multistrategy.py:87-97`——每个策略用**全额 capital**独立跑回测，再
  `port_ret = Σ total_return_s × weight_s`。文件自己在 L92 承认"简化…严格做法
  需统一逐日合并"。
- 三处失真：①现金被用了 3 次（100k 本金同时支撑三个策略的全仓位，实盘做不到）；
  ②跨策略同票/同题材重复暴露零核算（score 和 board 同时买一只票=双倍仓位，
  报告里看不见）；③权重来自单期样本内 sharpe/收益加权（见 K4），是已知的高
  过拟合放大器（[IS–WFA–OOS 协议](https://ar5iv.labs.arxiv.org/html/2603.09219)，
  [交叉验证与泛化误差](https://palomar.home.ece.ust.hk/MAFS5310_lectures/slides_backtesting.pdf)）。
- 修法：统一组合账本（一个现金池、逐日合并、一次撮合日历）；权重改样本外/
  walk-forward 风险调整收益 + 相关性惩罚（[Walk Forward Correlation](https://podcasts.apple.com/ro/podcast/053-martyn-tinsley-2-of-2-walk-forward-correlation/id1703013320?i=1000769572662)）；
  在修好前，`portfolio` 输出必须打标"结构近似，不可直接指导分仓"。

## K2（P2 研究项，但可能是你盈亏比 0.73 的最大杠杆）· 退出链活在"固定百分比"世界

- 证据：止损 −7% / 止盈 +8% / 移动止损激活 +4% 回撤 −3% / 冲高触发 +3% 回落 2%
  ——全部固定百分比（`config.py:49-56`），**没有任何一条按个股自身波动率归一化**。
- 与你的 R1 证据咬合：平均盈利 +¥178 vs 平均亏损 −¥245、亏损单拿 14.4 天>
  盈利单 10.3 天——固定百分比止损在 20cm 高波票上是噪音线，在低波票上又太松；
  同一套数字对所有票不是"纪律"，是"一刀切"。外部共识解是波动率自适应跟踪止损
  （Chandelier：22 日高 − k×ATR，[SystemTrader 实测](https://articles.stockcharts.com/article/articles-arthurhill-2016-12-systemtrader---testing-a-mean-reverion-system-with-the-chandelier-exit-spy-qqq-ijr---rsi5/)，
  [ATR/Chandelier/Keltner 比较](https://volatilitybox.com/research/volatility-adjusted-stop-losses/)，
  [FMZ 实现](https://github.com/fmzquant/strategies/blob/a6c574f3e01d8c982c2b99b84effc7f3aaa42871/Chandelier%20Exit.md)）。
- 修法（必须走 C 系预注册回测，不许直接换上）：engine 加 ATR 跟踪止损选项
  （默认关），与现行固定规则做 IS/OOS 对照臂。

## K3（P2）· 仓位四乘数连乘，无下限保护，且"跳过"不留痕

- 证据：`engine.py:718-750`——指数择时×情绪×涨停生态×回撤熔断四个 <1 乘数顺序
  连乘（如 0.7⁴≈0.24，意图仓位被缩到尘埃），且 `qty<100 → continue`（L751-753）
  直接跳过：**信号说买、回测记"没买"**，跳过率不进任何绩效口径。
- 修法：记录"意图仓位 vs 实际仓位"分布 + 跳过率进绩效；联合门控 vs 顺序门控做
  对照；实盘侧确认同一 skip 语义（否则回测/实盘分叉）。

## K4（P2，与 K1 同批修）· `_score_metrics` 奖励高 beta，兜底是断崖

- 证据：`score = sharpe×0.5 + ret×2.0 − dd×0.8`（L49）——ret（非年化、非超额，
  权重 2.0）在牛市 IS 窗口里主导一切，最高 beta 策略拿最多钱；无单策略权重上限；
  全员 ≤0 分时**等权兜底**（L81-82）——恰恰在所有策略都烂的时候平均下注，本该
  是现金/降仓。
- 修法：单策略权重上限（如 ≤60%）+ 现金作为显式分配候选 + 超额年化收益 +
  相关对惩罚。

## K5（P3）· 无题材集中度管理

- 系统自己有题材共振信号（竞价板块共振、涨停生态），意味着信号天然聚类到热门
  题材；但组合层只有单票上限（RISK_SINGLE_STOCK_PCT），无题材暴露核算。
- 修法：逐日题材暴露报表 + 上限。

## 处置建议

K1 打标（1 行注释级改动）可进下一修复单；K2 值得一张 C 系预注册对照单（与你
0.73 的亏钱方式直接相关）；K3/K4 与 K1 同批重构；K5 观察项。
