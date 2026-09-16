# Phase 13 敏感性扫描双参数化报告

- 目标: 敏感度扫描从单参数扩展为双参数（参数平台区 vs 悬崖检测）。
- 改动: `app/engine.py::sensitivity_scan`（新增 param2/values2、返回矩阵，单参数格式兼容）、
  `app/server.py`（sensitivity 字段支持 param2/steps2）、
  `web/index.html` + `web/js/app.js`（双参数热力表 + 稳定性判断）。

## 1. 接口升级

```python
sensitivity_scan(..., param2=None, values2=None)
```

- 不传 param2/values2 → 单参数旧行为：返回 list（`param/value/total_return/sharpe/...
  `），向后兼容。
- 传 param2+values2 → 双参数二维扫描，返回：
  ```
  {param, param2, values2, matrix, rows, dual:true}
   matrix: [len(values2) 行 × len(steps) 列] 收益矩阵（行=param2 增、列=param 步长）
   rows:   全量平坦列表（每项含两参数取值 + 全部指标）
  ```

## 2. 服务透传

- payload `sensitivity.steps2`/`param2` → server 传 `values2`/`param2`；
  只给 steps（无 steps2）时保持单参数路径。

## 3. 前端

- 双参数（`sens.dual`）→ `#btSensTable` 渲染热力表：行=param2 值、列=param 步长，
  单元格背景按收益相对强度配色（红涨/绿跌主题），表头标注 "param2 \\ param"。
- 稳定性判断 `#btSensNote`：计算相邻档收益变化（行内与列内），
  平均变化 <5pp 且最大 <12pp → "✅ 参数平台区…稳健"；否则
  "⚠️ 参数敏感…悬崖…过拟合风险"。
- 单参数 → 旧表格不变，`#btSensNote` 隐藏。

## 4. 验收

### ① 进程内直调（12 只，2026-02~08）
- 双参数 buy_threshold(0.8/1.0/1.2) × max_positions([2,3,4])：3×3 矩阵完整，
  rows=9 条含双参数；单参数调用返回 list 且含 value 键 ✓

### ② API 双参数（8 只短窗）
- dual:true、3×3 矩阵：
  ```
  mp=2: [-41.9%, -18.0%,  -5.4%]
  mp=3: [-41.5%, -16.1%,  -2.9%]
  mp=4: [-39.9%, -17.0%,  -2.9%]
  ```
  （横向收益随 buy_threshold 上升而改善 → 参数平台/单调区，前端据此判稳定性）

### ③ API 单参数（旧行为）
- 返回 list（3 条）、含 value 键 → 旧格式不变 ✓
- 冒烟 8 项 PASS ✓

## 复现

```
POST /api/backtest  body: {mode:"normal", sensitivity:{param:"buy_threshold", base:25,
  steps:[0.8,1.0,1.2], param2:"max_positions", steps2:[2,3,4]}, ...}
```