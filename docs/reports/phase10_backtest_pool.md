# Phase 10 回测股票池扩展报告

- 时间: 2026-08-21（数据截至 2026-08-21 收盘补库）
- 任务: 回测股票池扩展 — 新增 top500 / local_full 两种池。

## 1. 后端 `GET /api/backtest/pool`

| kind | 实现 | 返回 |
|---|---|---|
| top500 | 复用 `_stocklist_paged` 按 amount 降序取 500（30s 行情缓存） | `{codes,names,count,kind}` |
| local_full | `SELECT code,COUNT(*) FROM kline WHERE period='day' GROUP BY code HAVING c>=300` | 同上 |

- 验收①: `top500` → count=500（中际旭创/新易盛等成交额前列）；`local_full` → count=196。
- names: top500 取自行情列表；local_full 取自 `df.get_stock_list()` 映射，缺失用 code 兜底。

## 2. 前端

- `#btPool` 新增 `<option value="top500">成交额 Top500</option>`、
  `<option value="local_full">本地全量库 (K线≥300根)</option>`。
- 选中 top500/local_full → fetch `/api/backtest/pool?kind=...`。
- 大池提示 `#btPoolHint`：top500 固定文案；local_full 实时查 count 后按
  `count/82×70s` 折算预估分钟数（82 只×1 年≈70s 基准）。
- 旧池（top100/top300/watchlist/custom）代码路径未动，行为不变。

## 3. ⚠️ 数据审计发现（重要）

**market.db 实际 K 线分布与任务预估（约 1500）不符：**

| K 线数区间 | 股票数 |
|---|---|
| <50 | 8 |
| 50~100 | 151 |
| 100~200 | 5 |
| 200~300 | 1193 |
| 300~500 | 193 |
| ≥500 | 3 |

- 最长 K 线 600 根（sh000001 等指数，2024-02-29 起）；个股最早 000001 2024-12-20 起约 405 根。
- 结论：本地库为「2024 年底至今」数据（约 400 根/股），**并非公共背景所述的 2004 年长历史**。
- 因此 `local_full` 如实返回 196 只；后续补全长历史数据后该池数量自动增大（SQL 无需改动）。

## 4. 验收②③

- 新池随机 30 只（seed=42）+ 3 个月窗口（2026-05-21~08-21）mode=normal：
  完成无异常，总收益 -7.09%、回撤 -16.04%、胜率 65.42%、178 笔，指标全齐。
- 旧池行为不变（前端分支审查 + 冒烟回归）。

## 复现

```
GET http://127.0.0.1:8899/api/backtest/pool?kind=top500
GET http://127.0.0.1:8899/api/backtest/pool?kind=local_full
```