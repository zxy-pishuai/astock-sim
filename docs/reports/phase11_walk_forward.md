# Phase 11 walk_forward 升级报告

- 目标: 解决原 walk_forward "只调一个参数、机械对半切" 的问题。
- 改动: `app/engine.py`（新签名 + 滚动切分 + 网格调参 + degradation）、`app/server.py`（透传）。

## 1. 滚动切分

新签名：
```python
walk_forward(codes, names, start, end, strategy="score", capital=100000.0,
             params=None, folds=3, train_days=252, test_days=126,
             step_days=126, anchored=False)
```

- 未显式传滚动参数（train_days=252 且 test_days=126 且 step_days=126）→ 旧式 folds 换算：
  `train_days=test_days=step_days=total_days//(folds+1)`，兼容旧调用 `folds=3`。
- 显式传滚动参数 → 按 train/test/step 滚动；`anchored=True` 训练起点固定、终点滚动。
- 末折测试段收口 `end`；残余 < 测试段一半不再开折；≤20 折防死循环保险。

## 2. 训练段网格调参

`_wf_grid_select`: buy_threshold∈[20,25,30] × max_positions∈[2,3,4]（9 组合），
选优 `收益−0.5×|回撤|`，`trade_count≥5`，无候选回退默认 params。

## 3. degradation 衰减率

`degradation = 1 − OOS/IS`；IS≤0 → None。汇总含 avg_degradation。

## 4. 验收实测（30 只 × 2025-06-21~2026-08-21，14 个月）

### folds=2（等长滚动换算）
- 折1: train 06-21~11-09, test 11-10~03-31, best {bt:20,mp:4}, IS=0.1504, OOS=-0.2336, deg=2.553
- 折2: train 11-10~03-31, test 04-01~08-20, best {bt:30,mp:4}, IS=-0.1625, OOS=0.1054, deg=None(IS≤0)
- summary: avg_oos=-6.4%, avg_deg=2.55, positive 1/2, verdict ⚠️不稳定

### folds=3（旧式兼容）
- 3 折，无异常。修复了初版"folds=3 只出 1 折"的换算歧义（folds=默认3 与"未传"同值，
  现以"是否显式传滚动参数"为准判别）。

### anchored=True + train200/test90/step90
- 3 折，训练起点均固定 2025-06-21，终点滚动 2026-01-07→04-07→07-06，末折测试收口 08-21。

### /api/backtest mode=walk_forward
- payload 传 wf_folds=2 + wf_train_days=150/wf_test_days=80/wf_step_days=80/wf_anchored=false
  → 2 折跑通，degradation/best_params 正常返回。

## 5. 回归

冒烟测试 8 项全 PASS；服务重启加载新代码，/api/overview 正常。

## 复现

```python
from app import engine as eng
eng.walk_forward(codes, names, "2025-06-21", "2026-08-21", folds=2)          # 旧式
eng.walk_forward(codes, names, start, end, train_days=200, test_days=90,
                 step_days=90, anchored=True)                                  # 滚动
```