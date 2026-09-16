# Phase 12 跨年代多窗口验证报告

- 目标: 用同一套参数跑多个年代窗口（牛/熊/震荡），检验策略是否跨年代稳健，避免"单窗口过拟合"。
- 改动: `app/engine.py`（新增 `multi_window`）、`app/server.py`（`mode="multi_window"`）、
  `web/index.html` + `web/js/app.js`（前端"多窗口验证"选项与结果表格）。

## 1. 接口

```python
def multi_window(codes, names, windows=None, strategy="score",
                 capital=100000.0, params=None):
```

- windows: `[["YYYY-MM-DD","YYYY-MM-DD"], ...]`；缺省预设 4 窗口（牛熊震荡全覆盖）：
  - 2019-01-01 ~ 2020-12-31（牛市）
  - 2021-01-01 ~ 2022-12-31（震荡→熊）
  - 2023-01-01 ~ 2024-12-31（震荡）
  - 2025-08-18 ~ 2026-08-18（近一年）
- 每窗口用**同一套 params** 跑 Backtest，输出: 年化/夏普/最大回撤/胜率/笔数/总收益。
- 本地库无该年代数据 → 窗口标 `skipped` + error（不卡死）。
- 汇总: count/avg_annual/std_annual(总体标准差)/positive_folds/positive_ratio/verdict(+params)。

## 2. 服务与前端

- server `_run_backtest_job` 增加 `mode=="multi_window"`，`windows` 从 payload 透传（缺省 None=预设）。
- 前端 `#btMode` 增"多窗口验证(Phase12)"；`#btMW` 结果区：verdict + 指标卡（平均年化/
  标准差/正收益占比/有效窗口数）+ 窗口表格（窗口/起止/年化/夏普/回撤/胜率/笔数/总收益），
  样式与现有 WF/回测结果一致。

## 3. 验收

### ① 30 只直调 2 窗口
- 2019-2020: SKIPPED（本地库无该年代数据）
- 2025-2026: 年化 -22.1% / 夏普 -0.73 / 回撤 -36.1% / 胜率 60.6% / 566 笔
- summary 字段齐全（count/avg_annual/std_annual/positive_folds/positive_ratio/verdict）✓

### ② API mode=multi_window（10 只，预设 4 窗口）
- 4 窗口返回；前 3 个 SKIPPED（本地库仅 2024-12 起）、近一年窗口正常出指标，
  verdict="✅ 多窗口稳健：1/1 窗口正收益"（仅 1 有效窗口，如实标注）✓

### ③ 其它 mode 回归
- normal: 完成（收益 20.4%、228 笔）✓
- walk_forward(folds=2): 2 折完成 ✓
- 冒烟 8 项 PASS ✓

## 4. 数据说明（同 Phase10 审计结论）

market.db 实际数据从 2024-12 起（约 405 根/股），无 2019~2024 历史 →
预设 4 窗口中 3 个会 SKIPPED。待补全历史数据后 multi_window 自动覆盖更多年代。

## 复现

```
GET /api/backtest   body: {"mode":"multi_window", "codes":[...], ...}
python -c "from app.engine import multi_window; print(multi_window(codes,names))"
```