# Phase 46：数据治理报告 —— 复权锚点冻结、跨段污染修复、哨兵自动化

- 时间: 2026-08-26 凌晨完成（数据快照 tag=2026-08-26）
- 背景: market.db 的 kline 存"随行情每日重算的前复权价"，updater 每晚整体重写历史收盘
  （实测修订票数一天内 [66,91]→[138,156] 持续增长），跨天回测绝对收益不可比，
  一切"基线精确复现"类验收失效。Phase36/37 两份报告独立发现同一根因。

## 1. 方案对比与选型

| | 方案甲：每日整库快照（**采纳**） | 方案乙：原始价+adj_factor 列 |
|---|---|---|
| 改造点 | 新增 app/data_snapshot.py + updater 尾部 ~30 行挂钩 | 改 updater 写入逻辑 + 全部读取方兼容双模式 |
| 存储成本 | ~528MB/天 ×保留10份 ≈ 5.3GB | 近零（只加一列） |
| 可重放性 | 完美（整库状态原样冻结，含 earnings/qg 表） | 取决于读取方是否都正确合成 |
| 兼容性 | C.DB_FILE 指向快照即为 drop-in | 所有工具逐个适配 |
| 上线时间 | 当晚 | 数天 |

选甲先行上线（立刻止血），乙作为远期演进（若存储成本不可接受再切换）。

## 2. 实施

### 2.1 快照机制
- `app/data_snapshot.py`：sqlite 在线 backup API 整库冻结 → `data/snapshots/YYYYMMDD/market.db`
  （对 WAL 并发写安全，服务不中断）；manifest 记录行数/体积；保留最近 10 份自动清理；
  `latest_snapshot_path()` 供回测工具取用
- `tools/data_snapshot.py`：CLI（--keep/--force/--list）
- **updater.py 挂钩**（run_update 成功尾部，~35 行）：每日增量更新成功后
  自动生成当日快照 + 调用数据哨兵（tools/data_sentinel.py，900s 超时隔离）+
  异常摘要追加 `data/quality_alert.jsonl`——任一步失败仅记审计不影响主流程
- 首份快照: `data/snapshots/2026-08-26/market.db`（528.2MB）

### 2.2 回测使用快照口径的方法
```python
from app import config as C, engine as eng
C.DB_FILE = r"data/snapshots/2026-08-26/market.db"   # 运行时指向冻结快照
# 其余配方不变；eng.Backtest 的 _load_data 即从快照读数
```

## 3. 跨段污染修复（Phase35 遗留 23 只）

- 过程坑位两处：① 23 只在 Phase20 进度文件中被标记"已完成"被断点续跑过滤
  （备份后剔除旧记录重跑）；② venv 无 mootdx，换系统 Python 重跑
- 结果: **22/22 修复成功**（603119 无该期数据不适用），替换 40,648 行；
  接缝复查 20 只全部消除（|次日开盘/当日收盘-1|≤21%）
- 哨兵复检: 复权异常 high_severity **62 → 38**（其余为历史缺口等既有项）

## 4. 双口径基线存档（快照口径第一份基准）

同日双跑对照（score/board × 四窗口 × {活库, 快照}）：**8/8 组逐位一致**
——既验证快照是活库的忠实副本，也确认当日两者尚无差异。
**自即日起，回测类验收以最新快照口径为准。**

冻结基准（tag=2026-08-26，MOMENTUM_MIN 冻结 7.0 口径，收益/笔数）：

| 窗口 | board | score |
|---|---|---|
| 2019-20 | +2.75% / 661 | -6.21% / 1271 |
| 2021-22 | +23.34% / 790 | -37.34% / 1334 |
| 2023-24 | +7.49% / 633 | -34.55% / 1219 |
| 2025-08-18~08-21* | +39.46% / 474 | +21.63% / 701 |

★ 与更早发布基线（board -11.77/-6.65/-1.66/+27.30 等）差异巨大，系 8/25 白天
updater 又一轮前复权重写所致（本轮审计时 w0 已非零修订）——这正是本阶段要治的病，
今后以每日冻结快照终结该问题。score 牛市窗口日期范围为 2025-08-18~2026-08-21。

## 5. 哨兵自动化

- 触发链：每日收盘更新成功 → 快照 → 哨兵全量检查（42s）→ quality_alert.jsonl 追加摘要
  （high_severity / candidates_total / min5_latest / ml_pred_last）
- 首条告警已写入（修复后复检：high=38、candidates=1112、min5 最新 2026-08-25）

## 6. 局限与移交

- 快照体积 ~528MB/天，保留 10 份（--keep 可调）；若嫌大可改 kline-day 单表导出
- 方案乙（原始价+因子列）仍是长期最优解，需协调所有读取方，建议单独立项
- min5.db 未纳入快照（体积 679MB 且只追加不改写历史，漂移风险低）；如需可扩
- 交付物: `app/data_snapshot.py`、`tools/data_snapshot.py`、`tools/bt_snapshot_baseline.py`、
  `data/snapshots/2026-08-26/`、`data/snapshot_baseline_parts/`、
  `data/bt_snapshot_baseline.json`、`data/quality_alert.jsonl`、updater.py 挂钩
