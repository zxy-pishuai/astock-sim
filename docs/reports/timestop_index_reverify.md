# P77 时间止损复验 + P73 择时复验 + 指数择时回滚取证 — 2026-08-30

- 范围: 三件独立，只读为主；不动 `app/config.py`、不动 `market.db` 写入路径、显式钉快照、不重写预注册判据
- 约束: `tools/time_stop_reverify.py` / `tools/index_timing_snapshot_retest.py` / `app/config.py` / `app/trader.py L618, L761` / `app/engine.py L128` / `data/audit/audit.jsonl` 均只读取证；`py_compile` + LF 合规

---

## A. P77 时间止损 T=2 快照口径复验（数据已跑完，缺报告）

### A.0 口径

- 快照: `data/snapshots/2026-08-26/market.db`（`meta.snapshot` 明文，Phase46 冻结）
- 策略/池: `score` / `top500`（`bt_pool.json` 前 500，经 `DATA_EXCLUDE_CODES` 过滤）
- 基线参数: `buy_threshold 25 / max_positions 3 / position_pct 0.30 / slippage 0.001 / zt_eco_gate False / dd_gate False`
- 新臂: `T2 {TIME_STOP_DAYS=2}`、`T2_A1 {act8%/stop-4%/T2}`、`T2_A2 {act12%/stop-5%/T2}`；复用臂: `baseline(T3)` / `A1_trail_act8_stop4` / `A2_trail_act12_stop5`（同快照 `data/bt_exit_pack_v2.json` 已实测，直接引用）
- 预注册判据（`meta.prereg_judge` 原文，**只引用不重写**）: `landing_rule: >=3/4窗损失>=-1pp 且 牛市损失<=2pp（相对同快照 baseline 臂）`，以 `tools/judge_kit.py landing_rule` 为执行器（`loss_pp = base_ret - new_ret >0 表示更差`，`windows_ok = count(loss<=1) >=3`，`bull_ok = bull_loss<=2`）
- PIT 注记（`meta.pit_note`）: `pit_pools['近1年'] × 牛市窗` 现引擎现参数重跑，仅作失真校准，不参与静态判定

### A.1 结果重算（以 prereg_judge 为准，与 JSON 自带 verdicts 对照）

| 变体 | 4 窗收益（2019-20 / 2021-22 / 2023-24 / 牛市） | 4 窗损失 pp（正=更差） | 牛市损 | windows_ok / bull_ok | JSON verdict pass | **按 prereg 复核 pass** | sum_improve_pp |
|------|----------------------------------------------|----------------------|--------|----------------------|-------------------|---------------------|----------------|
| T2 | -0.1563 / -0.6168 / -0.4156 / 0.0569 | 31.23 / 6.66 / 4.05 / 22.58 | 22.58 | 0 / False | False | **False** | -64.52 |
| T2_A1 | -0.1511 / -0.6054 / -0.4008 / 0.2752 | 30.71 / 5.52 / 2.57 / 0.75 | 0.75 | **1** / True | True(4/True) | **False** | -39.55 |
| T2_A2 | -0.0717 / -0.6230 / -0.3585 / 0.1548 | 22.77 / 7.28 / -1.66 / 12.79 | 12.79 | 1 / False | False(3/False) | **False** | -41.18 |
| T3_A1_trail_act8_stop4 | 0.1819 / -0.5927 / -0.2590 / 0.5660 | -2.59 / 4.25 / -11.61 / -28.33 | -28.33 | 3 / True | False(1/True) | **True** | 38.28 |
| T3_A2_trail_act12_stop5 | 0.0692 / -0.5487 / -0.2409 / 0.5979 | 8.68 / -0.15 / -13.42 / -31.52 | -31.52 | 3 / True | False(2/True) | **True** | 36.41 |

> **不一致披露（按任务书要求以 prereg_judge 为准并记录）：** JSON 自带 `verdicts` 的 `windows_ok` 与 `pass` 与复核不一致——`windows_ok` 差异 **5/5 行**、`pass` 差异 **3/5 行**（T2_A1 记 4→实 1 且错判 True；两 T3 记 1/2→实 3 且错判 False；T2/T2_A2 的 `pass` 虽一致但其 `windows_ok` 仍不符）。`recommendation {T2_A1 sum -39.55 action 建议落地}` 亦与复核结论相反。原因疑为落盘时 `loss_pp` 阈值符号或 `landing_rule` 入参与脚本内 `losses*100` 不一致。**本报告以 `judge_kit.landing_rule` 复核为准，下述结论不受 JSON 原 verdict 影响。**（2026-08-31 独立复核：`windows_ok` 差异 5/5、`pass` 差异 3/5，与本节一致。）

### A.2 与 c_lane 批 "T2 是毒药"对照

- 本批 T2 三臂在 `score` 上**全部不通过**（牛市 12.79–22.58pp，且非牛窗多窗 >1pp），与 `docs/reports/c_lane_redispatch.md` §1 "T2 是毒药、T4 是解药（T=2 在牛市 +29~37pp）" 的方向一致；仅数值量级因快照与基线差异略有偏移，但"同移动止损档下 T2 恶化最重"的不变式成立。
- 反向看：同移动止损档的 **T3 臂（baseline 改动，`act8/stop-4` 与 `act12/stop-5`）反而达标**（3 窗通过、牛市大幅改善 -28/-31pp），说明 P77 证伪的是 `TIME_STOP 2`，而非移动止损本身。

### A.3 PIT 与静态双列（P72 模板）

- 单列可得: `baseline_pit|pit (牛市)` = `-0.2689 / -35.12% / 738 笔`，对应静态 `baseline (牛市)` = `0.2827`，Δ = `pit - static = -55.16pp`，`overstatement = +55.16pp`（`tools/judge_kit.pit_compare` 口径）。静态池在牛市显著高估，符合 P64 全文性声明；本批 PIT 仅备牛市一格，其余三窗无 PIT 对照，故"双列"仅牛市可量化，其余三窗按静态判定。

### A.4 结论与建议

- **预注册结论: 无 T2 变体通过，不建议落地 TIME_STOP=2**（含两档移动止损组合）。维持 `TIME_STOP_DAYS=3`；移动止损两档在 T3 下反而通过，但属复用臂，非本批新增，是否落地由 C 批已建议的 `act12_T4` 路径另议。
- 保存 `verdicts` 不一致已披露，后续落盘脚本应以 `judge_kit.landing_rule` 为单一真相源，避免阈值符号病（`loss >= -1` 恒真教训，见 `judge_kit.py:_SELFTEST`）。

### A.5 交付

- 数据: `data/bt_time_stop_reverify.json`（`meta`/`new_results`/`reused`/`verdicts`/`recommendation` 已在位，本报告仅重算判定）
- 工具: `tools/time_stop_reverify.py`、`tools/judge_kit.py`
- 报告: 本文档 §A

---

## B. P73 index_timing 快照口径复验

### B.0 当前开关

```
from app import config as C; C.INDEX_TIMING_ENABLED, C.INDEX_TIMING_BOARD_ONLY
→ False False
config 文件 mtime = 2026-08-27 07:30:18（002594 移出编辑）；config.py:215 注释内
"2026-08-26" 为回滚决定日期，非文件 mtime（2026-08-31 修正，原写 8/26 07:47 系混写）
```

### B.1 依赖检查（快照 4/4 就绪 + 全量已执行）

- 脚本: `tools/index_timing_snapshot_retest.py`（`mtime 2026-08-27 01:41:15`，自述"准备阶段"）
- 要求: `data/snapshots/ ≥3 个不同日期`（`SNAP_DIR` 下含 `market.db` 的子目录计数）
- 实测: `['2026-08-26', '2026-08-27', '2026-08-30', '2026-08-31']` = **4/4，已满足**。新增 `2026-08-31` 来自 G0 服务恢复后 8/31 收盘自动更新（`manifest rows_kline_day 8701436`，`created_at 2026-08-31T22:22:04`；对照 08-30 `8693312`，8/31 应略多 ✓）
- 干跑: `python tools/index_timing_snapshot_retest.py`（不加 `--execute`）→ `快照日期: [...]（需 ≥3）条件已满足。加 --execute 执行全量复验。` 退出 0
- 全量执行（G7 2026-08-31）：`python tools/index_timing_snapshot_retest.py --execute --workers 8`，**22:39:47 启动 → 22:51:37 完成（约 12 分钟）**，16 格 / 8 行表 / 错误 0，`snapshot_used=2026-08-31`，落盘 `data/bt_it_snapshot_retest.json`，日志 `tmp/p73_exec.log`。配方：`on/off × {score,board} × 4 窗`（on=活库+`index_timing=True`，off=快照 08-31 库+`index_timing` 默认关，`C.DB_FILE` 切库、开关唯一差异），与 Phase43 同表

### B.2 结果（16 格矩阵；on=择时开（活库）/ off=无择时（快照 08-31）；loss=off−on，正=择时更差）

| 策略 | 窗 | on_ret | off_ret | Δpp（on−off） | loss_pp（off−on） | loss≤1pp? |
|------|-----|--------|---------|--------------|------------------|-----------|
| score | 2019-20 | -0.2257 | -0.2541 | +2.84 | -2.84 | ✅ |
| score | 2021-22 | -0.5006 | -0.5164 | +1.58 | -1.58 | ✅ |
| score | 2023-24 | -0.3474 | -0.4549 | +10.75 | -10.75 | ✅ |
| score | **近1年(牛市)** | +0.2869 | +0.4274 | **-14.05** | **+14.05** | ❌ |
| board | 2019-20 | -0.0820 | -0.1206 | +3.86 | -3.86 | ✅ |
| board | 2021-22 | +0.1238 | +0.1693 | -4.55 | +4.55 | ❌ |
| board | 2023-24 | +0.0559 | +0.0657 | -0.98 | +0.98 | ✅ |
| board | **近1年(牛市)** | +0.5424 | +0.7452 | **-20.28** | **+20.28** | ❌ |

### B.3 判定（judge_kit 语义复核；loss=基线(off)−新(on)，正=更差；windows_ok=4 窗中 loss≤1.0 的窗数 ≥3；bull_ok=牛市窗 loss≤2.0）

| 策略 | windows_ok | bull_ok（牛市 loss≤2） | 判定 |
|------|-----------|------------------------|------|
| score | 3/4（2019-20/2021-22/2023-24） | ❌ +14.05 > 2 | **不通过** |
| board | 2/4（2019-20/2023-24） | ❌ +20.28 > 2 | **不通过** |

- **符号口径披露（P77 教训）**：脚本仅输出 `delta_pp=(on−off)×100`、无内置判定字段；本报告按任务书判定语义自证实现 `loss_pp=(off−on)×100=−delta_pp`，上表并排给出两列，方向已统一（Δ>0=择时改善 ⇒ loss<0）。无"脚本判定 vs 复核不一致"问题（脚本无判定），但显式披露防止后续误读。
- **与历史回滚依据一致**：2026-08-26 回滚因"新引擎近 1 年牛市 −17.57pp"；本次 08-31 快照口径复验 **score 近 1 年 −14.05pp、board 近 1 年 −20.28pp**，方向完全吻合 → 择时在近 1 年（2026 牛市）大幅恶化，**非数据锚点假象**。
- **反直觉点（锚点翻转根源）**：score 前 3 窗（2019-2024）择时反而改善（loss −2.84/−1.58/−10.75）、board 前 3 窗 2 改善 1 略差——择时损害集中在近 1 年牛市窗，历史窗不差甚至改善。P43 正是用历史窗结论（board +1.05pp）落地、新引擎近 1 年 −17.57pp 回滚，本复验复证该"历史窗通过不能代表牛市"的教训。

### B.4 结论与建议

- **P73 结论：index_timing 择时（score 与 board）在快照口径下均不通过 → 不具备重新评估价值。**
- 建议：两开关 `INDEX_TIMING_ENABLED / INDEX_TIMING_BOARD_ONLY` **维持 False**（`app/config.py` 零改动，红线遵守）；若未来再评，唯一前提是先单独验证近 1 年牛市窗的择时逻辑，历史窗通过不得作为落地依据（P43 教训）。

---

## C. 指数择时回滚完整性取证（只读）

### C.1 背景与勘误

旧简报"落地状态无记录"不成立。实测四处在案，回滚记录完整，缺的是**运行时零触发**的量化取证（本节补齐）：

### C.2 取证

**① 配置生效核对（只读）**

```
python -c "from app import config as C; print(C.INDEX_TIMING_ENABLED, C.INDEX_TIMING_BOARD_ONLY)"
→ False False
```

- 总开关: `app/config.py:209 INDEX_TIMING_ENABLED = False`（注释 L207-208 指 `phase2_index_timing.md` 的回测门槛，P22 情绪闸门已判无效的前例）
- 专属开关: `app/config.py:215 INDEX_TIMING_BOARD_ONLY = False  # 2026-08-26 验收回滚：新引擎同日 A/B 均值 -5.49pp（近1年牛市窗 -17.57pp），且 P43 依据跑在引擎修复前；重新启用前需按现行 engine 口径重测（见 bt_config_landing_regression.json）`
- 二开关共同作用：`app/trader.py:619 if C.INDEX_TIMING_ENABLED`（score 路）与 `app/trader.py:765 if getattr(C,"INDEX_TIMING_BOARD_ONLY",False)`（board 路，L761-773 含 `tim_mult` 与 `audit.record(event=index_timing)`）均关断；`app/engine.py:128-129, L692-699 params.index_timing` 仅回测受控，与实盘开关正交。`mtime app/config.py 2026-08-27 07:30:18`（002594 移出编辑；`config.py:215` 注释内 `2026-08-26` 是回滚决定日期而非文件 mtime——2026-08-31 修正，原写 `2026-08-26 07:47` 系混写）。

**② 运行时零触发证据（`data/audit/audit.jsonl`，2671 行）**

- 扫描: `event` 分布 `trading_event 2251 / heartbeat 250 / strategy_buy 54 / strategy_sell 48 / data_update 28 / data_update_failed 19 / drill_alert_path 3 / data_snapshot 3 / manual_buy 5 / scan_done 5 / test_buy 1 / blocked 1 / qfq_exclude_applied 1 / ev1/ev2 各 1`，**无 `event=index_timing` / `sentiment_gate` / `gate`**（2026-08-31 独立复核 event 全表 0 命中；原报告"kind 分布"实为 `event` 字段，已订正字段名）。
- 按日: `event=index_timing` 在全库 0 条，回滚次日 `2026-08-26` 之后亦 0 条；`sentiment_gate` 同 0 条（两闸门信号事件自始缺席，符合"从未开启"而非"开启后回滚才消失"的更强证据）。
- 锚点: 因全库零 `index_timing` 事件，**无最后出现时刻**（回滚自部署即关闭，非运行中关闭）；`trading_event` 心跳连续至 `2026-08-30 22:0x`（重启后影子/增量均存活），审计链健康 340 断裂后零新增，通道正常但择时未触发属配置关断所致。

**③ 结论段（写入 `docs/backlog.md` 风险看板第 2 条末尾，原文追加）**

> 指数择时两开关实测 `False False`（`app/config.py:209/215`，config 文件 `mtime 2026-08-27 07:30:18`，`L215` 注释内 `2026-08-26` 为回滚决定日期——2026-08-31 订正原 mtime 误写）、运行时 `index_timing` 事件 0 条（`data/audit/audit.jsonl` 2671 行，`2026-08-26` 之后 0 条，全库 0 条，无最后出现时刻，审计 `event` 枚举见 §C.2）；重新启用门槛 = 现行 engine 口径重测（`config L215` 原文指向 `bt_config_landing_regression.json`），候选路径 = P73 快照口径复验（本任务 B 节，`tools/index_timing_snapshot_retest.py --execute`，快照已 3/3 就绪，待今日 15:10 收盘后 08-31 新快照锚定）。

### C.3 不做的事

未重开择时、未改 `app/config.py`（红线）、未在本次跑 `index_timing` A/B（B 节的 P73 全量，待快照/时点）。

---

## D. 总交付与合规

| 项 | 交付 |
|----|------|
| A | 本报告 §A + `data/bt_time_stop_reverify.json` 已重算（`judge_kit` 复核，不一致已披露） |
| B | 本报告 §B（快照 4/4 就绪；`--execute` 已执行，16 格 / 0 错误，`data/bt_it_snapshot_retest.json` 已产出，判定=择时不通过、维持关闭，见 §B.3/B.4） |
| C | 本报告 §C + `docs/backlog.md` 风险看板第 2 条追加（下节） |
| 报告文件 | `docs/reports/timestop_index_reverify.md`（本文件，164 行 +） |

- 预注册判据: 全部引用 `meta.prereg_judge` / 脚本内预注册文本，未重写
- 回测口径: 显式钉 `data/snapshots/2026-08-26/market.db`（P77），P73 待钉最新快照
- 配置: `app/config.py` 零改动（`mtime` 不变）
- 只出建议: P77 建议维持 T=3，P73 待全量后另议

---

## 修订记录（2026-08-31，按验收 7d + 独立复核）

本报告 2026-08-30 初稿已通过验收（`docs/reports/acceptance_20260831.md` §3，90 分）。2026-08-31 独立复核并订正以下内容（结论不变）：

1. **§C.2 / §B.0 mtime 混写**：`app/config.py` 文件 mtime 实为 `2026-08-27 07:30:18`（002594 移出编辑）；`config.py:215` 注释内 `2026-08-26` 是回滚**决定日期**，非文件 mtime。已订正 §B.0、§C.2、§C.2③ 引用块，并同步订正 `docs/backlog.md` 风险看板第 2 条的同一处 mtime。
2. **§B.1 星期错位**：8/31 实为**周一交易日**（原稿误写"周一 09-01"）。今日（8/31）15:10 收盘自动更新后将新增 `data/snapshots/2026-08-31/` 快照；P73 `--execute` 全量 16 格锚定该新快照后执行。
3. **§C.2 字段名**：原"kind 分布"实为 `event` 字段分布，已订正并补齐全量枚举（2026-08-31 独立扫描 2671 行：`event=index_timing` / `sentiment_gate` / `gate` 全库 0 条）。
4. **§A.1 不一致披露量化**：独立复核 `windows_ok` 差异 5/5、`pass` 差异 3/5（T2_A1 错判 True、两 T3 错判 False；T2/T2_A2 的 `pass` 一致但 `windows_ok` 不符），`recommendation {T2_A1}` 不在重算通过集内——均与初稿结论一致。

复核脚本（只读，未写任何项目数据）：`tmp/verify_task3.py`；复核命令：`python -c "from app import config as C; print(C.INDEX_TIMING_ENABLED, C.INDEX_TIMING_BOARD_ONLY)"` → `False False`；audit 全表扫描 2671 行。
