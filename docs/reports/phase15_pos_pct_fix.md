# Phase15 修复：position_pct 被组合权重覆盖失效

- 时间: 2026-08-23 凌晨（验收 Phase10-14 时发现）

## 根因
- `engine.py` 买入撮合：`portfolio_method != equal` 时，组合权重 `w_cap` 直接覆盖 `pos_cash`，`position_pct` 完全不参与计算。
- `trader.py` 实盘同款：`pos_cash = cash * (w_cap if w_cap else pos_pct)`。
- 后果：严谨回测双参敏感性（buy_threshold × position_pct）矩阵三行完全相同，position_pct 维度浪费 2/3 算力且无任何信息。

## 修复（回测/实盘同步）
- 语义确认：`position_pct` = 单仓预算上限（config 注释「单仓占总现金比例」，regime 自适应也返回该量）；组合权重只决定相对分配，RISK_SINGLE_STOCK_PCT=0.45 仍是硬上限。
- `engine.py`: `pos_cash = self.cash * min(w_cap, self.pos_pct)`
- `trader.py`: `pos_cash = acct["cash"] * (min(w_cap, pos_pct) if w_cap else pos_pct)`

## 验证（12 只×2026-02~08，buy_threshold × position_pct 3×3）
| position_pct | bt×0.8 | bt×1.0 | bt×1.2 |
|---|---|---|---|
| 0.2 | -4.42% | +1.24% | +2.47% |
| 0.3 | -6.32% | +1.98% | +3.76% |
| 0.4 | -9.77% | +2.54% | +4.67% |

- 修复前三行完全相同（-21.12%/-2.11%/+20.52%）；修复后随仓位放大单调放大盈亏，符合预期。
- equal 方法不受影响（portfolio_weights 为空走原路径）。

## 影响提示
- 实盘单仓尺寸今后受 regime 的 pos_pct 约束（之前被组合权重绕过），整体更保守、与回测一致。
- 运行中的服务仍是旧代码，重启服务后生效（待 Phase14 完整回测跑完再重启，避免打断）。
