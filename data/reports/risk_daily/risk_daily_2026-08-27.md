# 风控观察日报 2026-08-27

- 生成时间：2026-08-27 20:00:01｜数据源：account.json / audit.jsonl / risk_state.json / market.db（全部只读）
- 🟡 **冷却拦截暂停生效中（至 2026-09-27）**：连亏暂停不拦新买单，单日熔断仍生效；本报告为此期间的唯一风控观察面。

## 🔔 告警
- ⚠ 连亏 4 笔已达暂停线(3)，但冷却拦截已暂停——不会自动停，需人工决策

## ① 资金概览
| 现金 | 持仓市值(昨收/今收估值) | 总资产 | 当日盈亏 | 当日盈亏% | 距熔断线(5%) |
|---|---|---|---|---|---|
| 96,025.60 | 0.00 | 96,025.60 | +0.00 | +0.00% | 还剩 5.00 个百分点 |

> 口径：当日盈亏 = 已实现(sells.pnl 原样) + 存续持仓昨收→今收变动；日初基准由当前总资产反推，属观察口径，与服务内 update_daily_pnl 的开盘快照存在小幅差异。

## ② 回撤（自初始资金 ¥100,000 逐笔事件时点 MTM 净值，持仓按本地收盘估值）
- 当前回撤：5.12%　历史最大回撤：5.12%
- 曲线终值 ¥96,031.12 vs 账户总资产 ¥96,025.60（差额 +5.52，为记账/取整噪声；|差额|>1% 请人工核对）

## ③ 连亏计数
| 口径 | 数值 | 暂停线(RISK_MAX_CONSEC_LOSSES) | 冷却截止 |
|---|---|---|---|
| 引擎持久化(risk_state.json) | 4 | 3 | 2026-08-27 |
| 报告重算(account 全量逐笔) | 4 | 3 | 不适用 |

## ④ 持仓快照（估值=market.db 本地日线，无外部请求）
空仓（positions=[]）

## ⑤ 当日买卖流水（2026-08-27，共 0 笔：买 0 / 卖 0）
当日无成交。

## ⑥ audit 当日摘要（WARN/ERROR）
- 日更(15:1x)：min5 999/1002，daily 300
- `[ERROR]` alert trading_event 后台扫描异常: cannot access local variable 'pos_cash' where it is not associated with a value
- `[ERROR]` alert trading_event 后台扫描异常: cannot access local variable 'pos_cash' where it is not associated with a value
- `[ERROR]` alert trading_event 后台扫描异常: cannot access local variable 'pos_cash' where it is not associated with a value
- `[ERROR]` alert trading_event 后台扫描异常: cannot access local variable 'pos_cash' where it is not associated with a value
- `[WARN]` alert trading_event ⚠ 中 关 村(000931) 触板后回落：现价5.70 距涨停4.5%（≥0.5%），封板不牢谨防炸板
- `[ERROR]` alert trading_event 后台扫描异常: cannot access local variable 'pos_cash' where it is not associated with a value
- `[WARN]` alert trading_event 📊 旭光电子(600353) ⚠高位缩量(0.01) 冲高回落风险（涨幅7.8%量能跟不上）
- `[ERROR]` alert trading_event 后台扫描异常: cannot access local variable 'pos_cash' where it is not associated with a value
- （当日共 291 条，仅列最后 8 条）

