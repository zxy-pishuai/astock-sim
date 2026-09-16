# K11（P1）涨停家数口径统一 —— qg_zt_full 对齐 eco 权威口径 + B4 闸门复测

- 时间：2026-09-16 18:20（写库完成于 ~18:10，复测完成于 ~18:18）
- 执行：K11 块（P1）
- 状态：**已写库**（qg_zt_full 已按 eco 权威口径重算覆写，改写前备份与补救见 §4）

---

## 0. 结论先行

1. **根因坐实**：`qg_zt_full.zt_count`（backfill 口径）系统性偏高 1~4 家，近 20 交易日 **17/20 天差异**。差异票分类实测：**未封板误计 30 例（主因，pct≥9.2% 近似阈值把"收盘未封在精确涨停价"的票计入）+ 新股上市初期 5 例（缺 60 交易日过滤）**；ST/前缀/排除清单在实测窗口未触发差异。
2. **权威口径 = app.zt_ecosystem（eco_temperature 现算）**：派单建议且为实盘闸门读取方，涨停判定用 `engine.limit_prices` 精确板价（四舍五入到分）+ close 封板 + 前缀/ST/新股 60bar 过滤。已据此**重算并覆写 `qg_zt_full` 全表（1869 行，2019-01-02 ~ 2026-09-16）**，补上 09-16 行。
3. **判据全部通过**：① 最近 20 交易日两口径 `zt_count` 逐位一致（20/20）；② `max_days` 与 eco `max_streak` 全量一致（1869/1869），相对 legacy 的 725 天变小**属口径修正（去虚高），非退化**（§6 样例证明）；③ 严格 PIT（eco 单遍顺序扫描，同源抽查 3/3）；④ phase 变更清单已落盘（变更 416 天：发酵→冰点 378、高潮→发酵 35、高潮→冰点 2、退潮→冰点 1）。
4. **B4 高潮情绪闸门在新口径下仍不建议开启**：consv 乘数表 score 策略 IS +6.89pp 改善、**OOS 大牛窗口恶化 -11.20pp（超 Phase22 判据红线 ≤2pp）**；orig 更差（OOS -13.65pp）；board 策略 consv 近乎中性（±0.05pp）。**维持 `SENTIMENT_POS_GATE=False`**，由用户拍板。

---

## 1. 根因定位（两口径机制差异）

| 维度 | backfill 口径（改写前 qg_zt_full） | eco 权威口径（eco_temperature 现算） | 对差异的贡献 |
|---|---|---|---|
| 涨停判定 | close 环比涨跌幅 ≥9.2% / 19.2%（百分比近似，**无分位取整**） | `engine.limit_prices` 精确涨停价（主板±10%/创业科创±20%/北交±30%，四舍五入到分），`abs(close−板价)<0.005` 才计 | **主因**：9.2~9.98% 区间收盘未封板的票被误计 |
| ST 过滤 | 无（ST ±5% 板到不了 9.2%，实际不触发） | 排除名称含 ST | 实测未触发 |
| 新股过滤 | 无 | 上市未满 60 自身交易日跳过（MIN_LISTED_BARS=60） | **次因**：次新一字板被多计 |
| 前缀过滤 | 无（全库 kline） | 仅 60/00/30/68 | 实测未触发 |
| 触板未封 | 不计区分（pct 达标即计） | `touched/broken` 单列，`zt_count` 只计封板 | 并入"未封板"主因 |
| 北交所 | 68 开头误用 19.2% 档 | limit_prices 支持 30% | 实测未触发（样本窗口） |

**实测（tmp/k11/diag1_result.json，最近 20 交易日）**：差异日 17/20；差异票分类——**未封板 30 例、新股<60bar 5 例**；样例：2026-08-20 的 603036、2026-08-21 的 301591（未封板），2026-08-26 的 688835（新股）。

**另发现**：改写前 qg_zt_full 缺 09-16 行（K4 回填截止 09-15），本次重算补齐至库内最新交易日 09-16（当日涨停 87 家、max_days=6、高潮）。

---

## 2. 权威口径实现（代码改动）

### 2.1 `app/zt_ecosystem.py`（写权限内）
- `_compute_daily` 返回值扩为 `(day, zt_pool)`：`zt_pool` = {date: set(code)}，eco 权威口径逐日涨停池（`close==limit_prices 精确板价` 才算）。`build()` 调用处同步适配（`day, _ = _compute_daily(...)`）。
- 新增公开函数 **`daily_zt_pool()`**：复用 `_scan_universe`（前缀/排除清单/ST 过滤）+ `_compute_daily`（新股 60bar/精确板价），与 `eco_temperature().zt_count` 完全同源。供 qg_zt_full 重算与口径诊断复用，**杜绝第二套涨停判定逻辑**。

### 2.2 `tools/backfill_zt_history.py`（写权限内）
- 新增 **`build_series_eco(eco_pool, eco_series, start)`**：以 eco 涨停池/生态序列重算 qg_zt_full 全字段——
  - `zt_count = len(pool[date])`
  - `max_days ← eco max_streak`（全史连板，含 2019 前跨年）
  - `prev_zt_premium ← eco yzt_ret_mean`（全量等权，替代旧口径"前 100 只采样"，消除旧涨停池残差）
  - `sentiment_score = round(min(100, zt_count×1.2), 1)`、`phase = _phase_of(...)`（规则不变，输入换新）
- `main()` 新增 `--caliber {legacy,eco}`：`eco` 分支调 `daily_zt_pool()` + `build(force=True)`；备份目录 eco 分支改用 `tmp/k11/`。
- 落库流程新增：**备份 → phase 变更清单写盘 → DROP+CREATE → INSERT → 重建 qg_sentiment_history 快照**（methods 注明 K11 口径）。

### 2.3 新增 `tools/k11_bt_gate.py`（B4 复测，判据⑤）
- 池 = `bt_pool.json` top500（滤 DATA_EXCLUDE_CODES）；`fetch_quotes` 置空；`BOARD_MOMENTUM_MIN=7.0` 运行时对齐（Phase21/22 基线配方）；score 25/3/0.30/0.001、board 40/2/0.25。
- 窗口：IS=2019-01-01..2023-12-31、OOS=2024-01-01..2026-08-31。
- 三开关组：off（基线）/ consv（{冰点1.0,发酵1.0,高潮0.9,退潮1.0}，Phase22 保守版）/ orig（{冰点0.85,发酵1.0,高潮0.9,退潮0.3}，Phase16 原版），**进程内 patch `C.SENTIMENT_POS_GATE/MULT`，不改 config.py**。
- 输出 `data/bt_k11_gate.json`。

---

## 3. 判据逐条结论（预注册对照）

| # | 判据 | 结果 | 证据 |
|---|---|---|---|
| ① | 最近 20 交易日两口径 zt_count 逐位一致 | **通过** | tmp/k11/verify2.py：20/20 逐位一致（2026-08-20 ~ 09-16，qg==eco） |
| ② | max_streak 保持一致、不退化 | **通过** | 全量 1869/1869 `qg.max_days == eco.max_streak`；相对 legacy 的变小为口径修正（§6） |
| ③ | 严格 PIT（不得用晚于该日信息） | **通过** | eco 两端均为单遍顺序扫描（`_compute_daily` 逐 bar 递推，只用当日及以前）；同源抽查 09-14/15/16 `qg.zt_count == len(pool[date])` 3/3 |
| ④ | 重算前后 phase 变化清单落盘供人工复核 | **通过** | `tmp/k11/phase_changes_k11.csv`（416 天变更，明细 §5） |
| ⑤ | B4 高潮情绪闸门新口径 IS/OOS 复测数字 | **完成** | §7，结论：不建议开启 |

---

## 4. 写库记录与备份（含一次执行事故的诚实披露）

### 4.1 写库
- 命令：`py -3.13 tools/backfill_zt_history.py --caliber eco`
- 结果：qg_zt_full 1869 行（2019-01-02 ~ 2026-09-16）已覆写为 eco 口径；qg_sentiment_history 快照同步重建（methods 含 K11 标注）。

### 4.2 ⚠️ 执行事故（如实披露）
- 首次以 `run_in_background=true` 提交时返回 `exit -1`（无 task id），**但命令实际已执行并完成写库**；随后前台再跑一次（幂等重跑，verify 时表已是 eco → 旧表代差 1.0000），**第二次的备份覆盖了第一次的备份**。
- 后果：`tmp/k11/qg_zt_full_before_k11.csv` 实际内容为 **eco 口径表**（非改写前 legacy 表）；phase_changes_k11.csv 首次生成被二次覆盖（变更 0 天）。
- **补救**：legacy 口径为确定性函数（`build_series` 从 kline 反推，PIT），以 `tmp/k11/reconstruct.py` 复现"改写前状态"（2019-01-02..2026-09-15，legacy 口径）→ **`tmp/k11/qg_zt_full_legacy_before_k11_reconstructed.csv`**（1869 行，非物理原表、为确定性复现），并据其重做 **`tmp/k11/phase_changes_k11.csv`**（§5 数字即出自重做版）。
- 回滚路径：若需回滚到 legacy 口径，`py -3.13 tools/backfill_zt_history.py --caliber legacy` 即可（K4 备份 `tmp/k4/qg_zt_full_before_k4.csv` 亦在）。

---

## 5. phase 变更清单（tmp/k11/phase_changes_k11.csv，1869 天交集）

| 迁移 | 天数 | 触发机制 |
|---|---|---|
| 发酵 → 冰点 | **378** | zt_count 下调跨 25 边界（旧口径未封板/新股虚高 → 新口径 <25 家） |
| 高潮 → 发酵 | 35 | 下调跨 60 边界或 max_days<5 |
| 高潮 → 冰点 | 2 | 同时跨 25/60 边界 |
| 退潮 → 冰点 | 1 | 涨停家数 <25 优先于退潮判定 |
| **合计** | **416** | 变更集中于 2019 前段与 2026 后半（未封板/新股密集期） |

含义：新口径下"冰点"日显著增多（此前被虚高 zt_count 掩盖），闸门 phase 分布向冰点迁移——这是 B4 复测中 orig（冰点×0.85）触发次数暴增的直接原因。

---

## 6. max_days "退化"的归因（判据②补充证明）

相对 legacy 复现，eco max_days 变小 725 天（集中于 2019 年初）。抽查（tmp/k11/streak_probe.py）证明为**旧口径虚高**：
- **2019-01-25**：legacy max_days=6 来自 **002945 华林证券（次新券商，上市未满 60 bar 的一字板 6 连）** + 600218 全柴动力（legacy 连续 pct≥9.2 但 eco 精确板价判定未封板）；eco 当日涨停 8 只全部连板 run=1 → max_days=1。
- **2019-01-09**：legacy 22 只 vs eco 11 只，多计的 600614（legacy run=5）等为未封板/新股。
- **2019-02-01**：002925 盈趣科技（次新）等被 eco 排除。

结论：**eco max_days 更小是去除"次新一字板 + 未封板误计"后的正确值**，qg_zt_full 与 eco_temperature 现已完全同源（1869/1869 一致），不构成退化。

---

## 7. B4 高潮情绪闸门 IS/OOS 复测（新口径，data/bt_k11_gate.json）

### 7.1 全表

| 策略 | 开关 | IS 总收益 | IS 触发 | OOS 总收益 | OOS 触发 |
|---|---|---|---|---|---|
| score | off（基线） | **-71.20%** | — | **+123.69%** | — |
| score | consv | -64.31%（**+6.89pp**） | 高潮×5 | +112.49%（**-11.20pp**） | 高潮×25 |
| score | orig | -66.36%（+4.84pp） | 冰点×1270/高潮×5 | +110.04%（-13.65pp） | 冰点×416/高潮×23/退潮×2 |
| board | off（基线） | **+5.01%** | — | **+89.71%** | — |
| board | consv | +5.03%（+0.02pp） | 高潮×2 | +89.66%（-0.05pp） | 高潮×16 |
| board | orig | +1.45%（-3.56pp） | 冰点×580/高潮×2 | +77.23%（-12.48pp） | 冰点×180/高潮×16/退潮×2 |

（IS=2019-01-01..2023-12-31，OOS=2024-01-01..2026-08-31；total_return 口径同 Phase21/22。）

### 7.2 判定（沿用 Phase22 预注册规则：≥3/4 窗口改善或持平且牛市窗口恶化 ≤2pp → 建议开启）

- **score**：consv IS 改善 +6.89pp、**OOS（牛市，+123.69%）恶化 -11.20pp（超红线 2pp 数倍）** → **不建议开启**。orig 同向更差。
- **board**：consv 两窗口 ±0.05pp 内（中性，无改善）→ **不建议开启**。orig 双窗口恶化。
- **结论：维持 `SENTIMENT_POS_GATE=False`，不更新 `SENTIMENT_POS_MULT`**（开关值由用户拍板，本块不改 config）。

### 7.3 与 Phase22（旧口径）对比与解释
- 旧口径（Phase22，2026-08-23）：score 近一年窗口（牛）恶化 2.66pp → 关闭。
- 新口径（K11）：score OOS 恶化扩大到 **-11.20pp（consv）**。原因：新口径下"高潮"日判定更严（精确板价），高潮 ×0.9 缩仓在 OOS 大牛段（2024-2026-08，+123.69%）触发 25 次全部拖累收益；orig 更因冰点日暴增（phase 迁移 378 天）在 OOS 触发 441 次缩仓。
- **口径统一后闸门反而更明确地不该开**——"高潮缩仓"在牛市中系统性损失，与 Phase26 温度闸门（ZT_ECO_GATE）结论同向（识别情绪≠可交易）。

### 7.4 局限声明
- `trading_calendar` 仅登记 [2025,2026] 节假日，历史年份按工作日近似（引擎警告）。**与 Phase21/22/26 各轮回测同引擎同口径，横向可比**，但 IS 段交易日的绝对精度受此影响。
- 池为 `bt_pool.json` top500（2026 快照口径），历史窗口存在轻微幸存者偏差（项目既有口径，非本块引入）。

---

## 8. 风险与回滚

| 项 | 说明 |
|---|---|
| 写库范围 | 仅 qg_zt_full（DROP+CREATE 重建，1869 行）+ qg_sentiment_history（单行快照）；未动 kline/limit_pool/其他表 |
| 备份 | `tmp/k11/qg_zt_full_legacy_before_k11_reconstructed.csv`（legacy 确定性复现）；`tmp/k4/qg_zt_full_before_k4.csv`（更早基线） |
| 回滚 | `py -3.13 tools/backfill_zt_history.py --caliber legacy` 恢复 legacy 口径（再跑 eco 即恢复新口径，可反复切换，均 PIT） |
| 对消费方 | sentiment_gate.phase_mult 读 qg_zt_full（回测）；实盘读 sentiment.cached_sentiment 实时 phase（未受影响，但 eco 口径与 sentiment_series 的相位一致性需另行核对——见未做项） |
| zt_eco.json | 已被 `build(force=True)` 刷新（data_max_date=09-16），与库一致，无副作用 |

---

## 9. 本块未做的事（留给验收/后续）

1. **未改 `app/config.py`**：`SENTIMENT_POS_GATE`（L414）、`SENTIMENT_POS_MULT` 保持现状，开启/关闭由用户拍板（本块只给数字）。
2. **未动实盘链路**：`app/sentiment.py::cached_sentiment` 实时 phase 与 qg_zt_full 历史口径的一致性未核对（实盘闸门走 sentiment_series 还是 eco 需另行确认——`SENTIMENT_POS_GATE` 开启前建议先对齐）。
3. **未跑 4 窗口复测**：B4 本次按任务要求跑 IS/OOS 两窗口；如需与 Phase22 四窗口严格同构对比，可复用 `tools/k11_bt_gate.py` 改窗口。
4. **未 commit**：改动文件留待夜间 `TianjiGit_Nightly` 统一提交（按并发纪律）。
5. **未清理**：tmp/k11/ 下诊断/补救脚本与中间产物保留备查。

---

## 10. 交付物清单

| 产物 | 路径 |
|---|---|
| 报告 | docs/reports/k11_zt_count_unify_20260916.md（本文件） |
| 代码改动 | app/zt_ecosystem.py（`daily_zt_pool`/`_compute_daily` 扩展）、tools/backfill_zt_history.py（`--caliber eco`/`build_series_eco`/phase 清单）、tools/k11_bt_gate.py（新增） |
| 复测数据 | data/bt_k11_gate.json |
| phase 变更清单 | tmp/k11/phase_changes_k11.csv |
| 改写前复现 | tmp/k11/qg_zt_full_legacy_before_k11_reconstructed.csv |
| 诊断/验证 | tmp/k11/diag1_result.json、tmp/k11/verify2.py（判据①②③）、tmp/k11/streak_probe.py、tmp/k11/reconstruct.py |
| 写库日志 | tmp/k11/（PowerShell 任务输出见本次会话） |
