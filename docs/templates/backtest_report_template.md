# 回测报告模板（Phase72 双列化范式）

> 使用规则：任何对外呈现的回测结论必须包含本模板全部段落；
> 「静态池 vs PIT 池」段用 `tools/report_pit_columns.py` 自动生成，缺失窗口如实标"未构建"。

```markdown
# <阶段号+名称>回测报告

- 时间 / 执行者 / 工具脚本（可复跑路径）
- 数据口径：★ 冻结快照 data/snapshots/<日期>/market.db（Phase46），
  或声明"活库当日态"并附 snapshot_baseline_report 一致性结论

## 1. 假设与配方（逐条，无一隐藏）
- 池/参数/滑点/费用/T+1/仓位/退出集/未复刻项……

## 2. 基线对齐
- 对照目标 + 对齐方式（冻结快照 / 同日现跑基线）
- 不一致处的根因证据（数据修订审计 / 配方差异），禁止静默跳过

## 3. 结果总表
| 变体 | W1 | W2 | W3 | 牛市 | 达标窗 | 胜率 |

## 4. 静态池 vs PIT 池对照
<由 tools/report_pit_columns.py 生成，粘贴于此>

## 5. 判定（预注册规则原文 + 逐条核验）
- 判定逻辑 import 自 tools/judge_kit.py（禁止手写符号比较）

## 6. 结论（诚实版）
- 达标 → 建议落地（交验收方）；不达标 → 如实关闭/维持现状

## 7. 局限声明
- 幸存者偏差（P64 量化）/ 复权重锚定 / 粒度近似 / 参数先验……
```

判定函数速查（详见 tools/judge_kit.py docstring，单位：损失=正数 pp）：
- `window_pass(loss_pp_list, max_loss_pp=-1.0, min_windows=3)`
- `bull_ok(bull_loss_pp, max_loss_pp=2.0)`
- `occupancy_ok(halt_days, half_days, total_days, half_weight=0.5, cap=0.40)`
- `seed_stability(returns_by_seed)` / `pit_compare(static_ret, pit_ret)`
- `landing_rule(loss_pp_list, bull_loss_pp)` —— 常用"落地建议"打包规则
