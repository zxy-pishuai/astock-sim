# B块｜C 批 bt_c_lane_redispatch.json 文本重建报告

- 时间: 2026-08-31 21:55 ｜ 执行: B块（纯文本，零回测）
- 目标: `data/bt_c_lane_redispatch.json`（8/30 23:29 被事故覆盖损坏 → 已重建）
- 红线遵守: 全程仅写 4 文件（证据/中间态/目标/本报告）；未重跑任何回测；
  触碰共享文件前已查 mtime；两源皆无字段置 null 进 partial 清单；SHA256 全比对；
  `docs/backlog.md` 未动（验收方统一登记）。

---

## 0. 结论摘要

- 损坏文件 `data/bt_c_lane_redispatch.json`（mtime 2026-08-30 23:29:12，17849B）
  由任务4 首跑 `act12_t4_pit.py` OUT_JSON 误指覆盖所致（首跑事故，责任已查明，
  与在跑进程无关）：`results` 44 格全为 `BrokenProcessPool` 错误、`judgement` 全
  missing-cell、`elapsed_s=61.5`。
- 已用 **tmp/c_lane.log（8/27 原始运行日志）+ docs/reports/c_lane_redispatch.md（8/27 定稿）**
  双源交叉重建全部 44 格结果与 9 条判定，**零编造、零回测**。
- 重建后目标文件已验证：`json.loads` 通过、44 格、0 error 键、judgement=9、
  elapsed_s=7137，与报告 §1/§3 表格**逐位一致**。
- 三哈希并列（§6）：损坏态 `F8F529…` ≠ 重建态 `43411E…` ≠ 本报告（文末）。

---

## 1. 事故证据（取证留底）

损坏态三特征（均记录于证据副本 `tmp/c_lane_corrupted.json`）：

| 特征 | 实测值 |
|---|---|
| mtime | 2026-08-30 23:29:12 |
| 大小 | 17849 B |
| elapsed_s | 61.5（失败运行值） |
| results 逐格 error 键计数 | **44**（11 变体 × 4 窗全为 `{"error": "BrokenProcessPool(...)"}`） |
| judgement | 9 条，detail 全 `[{win, error: missing-cell}]`，windows_ok=0、bull_loss_pp=null、pass=false |
| 骨架完整性 | 顶层键 `generated_at/phase/meta/c4_data_gate/results/judgement/elapsed_s` 全在；`meta`（snapshot/params/variants 11）与 `c4_data_gate` **未被损坏**（损坏范围仅 results/judgement/elapsed_s） |

- 取证：`Copy-Item data\bt_c_lane_redispatch.json tmp\c_lane_corrupted.json`，
  SHA256 双文件比对**一致**（见 §6），该副本同时作为覆盖前的双保险备份。
- 覆盖目标前再次核对：目标仍为损坏态（mtime 23:29:12、hash 与证据一致）后才执行覆盖。

---

## 2. 重建方法

- **方法**: `text-reconstruction, no re-run` —— 不重跑任何回测（红线2：
  act12 回测坑位被其他会话占用）。
- **重建源 1** `tmp/c_lane.log`（12606B，mtime 2026-08-27 12:42:52，未被污染）：
  含 11 变体 × 4 窗逐格 `ret/base/loss` 与「达标窗/牛市损/pass」判定行、
  进度行（11/44 1955s、22/44 4146s、33/44 5756s、44/44 7137s）、
  末行 `saved …bt_c_lane_redispatch.json`。
- **重建源 2** `docs/reports/c_lane_redispatch.md`（7678B，mtime 2026-08-27 12:45:38）：
  §1/§3 表格逐位数值 + §4 回撤核查/交易数散点。
- **编码发现**（重要，写入留痕）: `tmp/c_lane.log` 为 **UTF-16 LE（BOM FF FE）**，
  而损坏 JSON 为 UTF-8——重建脚本须分别用 `encoding='utf-16'` 与 `utf-8` 读取。
  这也是先前 Read 工具读取该 log 失败的根因（工具按 UTF-8 处理）。
- **骨架保留**: 顶层结构与损坏文件一致；`meta` 原字段（snapshot=`data/snapshots/2026-08-26/market.db`、
  pool_n=500、params、method 原文、variants 11）与 `c4_data_gate` 全文从损坏文件原样保留；
  `meta` 内**新增** `reconstruction` 块（见目标 JSON，记录本次重建元信息）。
- **generated_at**: 原 8/27 精确值不可恢复，以 log 的 saved 时刻
  `2026-08-27 12:42:52` 为锚（`generated_at="2026-08-27 12:42:52"`），报告如实说明。
- **elapsed_s**: 用 log 累计耗时 **7137**（损坏态 61.5 是失败运行值，弃用）。

**交叉规则**（red 4）：

1. `total_return` / `loss_pp` / 达标窗 / 牛市损 / pass：**log 为准**；
2. `trades` / `max_drawdown`：log 无此列 → 从报告表格/正文散点回填，
   能引到 → 标 `verified=report-only`；引不到 → **null + 进 partial 清单**；
3. 两源同值（报告 §1 ret 与 log ret 均为同一数值的两位小数表达）→ 直接回填、来源=log+report；
4. 任何编造/估算数字一律禁止。

---

## 3. 双源对账表（44 格）

窗口序：`w0=2019-20, w1=2021-22, w2=2023-24, w3=牛市`。
`ret` 为 total_return（小数，两源一致）；`loss_pp` 为相对本策略族基线的损失 pp
（score 族对 base_score、board 族对 base_board）；正=变体更差。

### 3.1 score 族（基线 base_score，对 base_score 计 loss）

| 变体 | ret w0/w1/w2/w3 | loss_pp w0/w1/w2/w3 | 达标窗 | 牛市损 | pass | trades | max_drawdown |
|---|---|---|---|---|---|---|---|
| base_score（基线） | −0.0572/−0.5290/−0.3193/+0.2702 | — | — | — | — | w2=1240（report-only） | w1=−0.566、w2=−0.468、w3=−0.186（report-only） |
| C2_act8_T2 | −0.1163/−0.5504/−0.3186/−0.0224 | +5.91/+2.14/−0.07/+29.26 | 1 | +29.26 | **F** | null×4 | null×4 |
| C2_act8_T3 | −0.0620/−0.5938/−0.2591/+0.5121 | +0.48/+6.48/−6.02/−24.19 | 3 | −24.19 | **T** | null×4 | null×4 |
| C2_act8_T4 | +0.0108/−0.4057/−0.2292/+0.5459 | −6.80/−12.33/−9.01/−27.57 | 4 | −27.57 | **T** | null×4 | w3=−0.143（report-only） |
| C2_act12_T2 | −0.0528/−0.5471/−0.2605/−0.1014 | −0.44/+1.81/−5.88/+37.16 | 2 | +37.16 | **F** | null×4 | null×4 |
| C2_act12_T3 | −0.0820/−0.4600/−0.1753/+0.3918 | +2.48/−6.90/−14.40/−12.16 | 3 | −12.16 | **T** | null×4 | null×4 |
| C2_act12_T4 | −0.0217/−0.4322/−0.2415/+0.5166 | −3.55/−9.68/−7.78/−24.64 | 4 | −24.64 | **T** | w2=1089（report-only） | w1=−0.543、w2=−0.514、w3=−0.140（report-only） |
| C3_trig10_back5 | −0.1600/−0.5468/−0.3363/+0.2873 | +10.28/+1.78/+1.70/−1.71 | 1 | −1.71 | **F** | null×4 | null×4 |
| C3_trig12_back6 | 与 trig10 **逐位相同** | +10.28/+1.78/+1.70/−1.71 | 1 | −1.71 | **F** | null×4 | null×4 |

### 3.2 board 族（基线 base_board，对 base_board 计 loss）

| 变体 | ret w0/w1/w2/w3 | loss_pp w0/w1/w2/w3 | 达标窗 | 牛市损 | pass | trades | max_drawdown |
|---|---|---|---|---|---|---|---|
| base_board（基线） | −0.1127/+0.1693/+0.0657/+0.6310 | — | — | — | — | null×4 | null×4 |
| C5_board_T2_A1 | −0.1226/+0.2688/+0.1360/+0.5412 | +0.99/−9.95/−7.03/+8.98 | 3 | +8.98 | **F** | null×4 | null×4 |

**来源标注**：上表 `ret`/`loss_pp`/判定列 = `verified=log(+report §1/§3 逐位一致)`；
`trades`/`max_drawdown` 有值 = `verified=report-only`（§4 散点）；`null` = 两源皆无。

**与报告 §1/§3 逐位核对**（抽查关键行）：

- base_score 四窗 −5.72%/−52.90%/−31.93%/+27.02% ✓
- act8_T4 +1.08%(−6.80)/−40.57%(−12.33)/−22.92%(−9.01)/+54.59%(−27.57) ✓
- act12_T4 −2.17%(−3.55)/−43.22%(−9.68)/−24.15%(−7.78)/+51.66%(−24.64) ✓
- act8_T3/act12_T3/act8_T2/act12_T2 六窗判定与数字 ✓
- C3 两档逐位相同、四窗损失 +10.28/+1.78/+1.70/−1.71 ✓
- C5 board base −11.27/+16.93/+6.57/+63.10、T2_A1 −12.26/+26.88/+13.60/+54.12、
  loss +0.99/−9.95/−7.03/+8.98 ✓
- §4 回撤核查：act8_T4/act12_T4 牛市 MDD −14.3%/−14.0% vs 基线 −18.6%；
  2023-24 act12_T4 −51.4% vs −46.8%；2021-22 −54.3% vs −56.6% ✓
- §4 交易数：2023-24 1240→1089 ✓

---

## 4. partial 清单（两源皆无 → null，不编造）

| 字段 | 已知格 | null 格 | 已知来源 |
|---|---|---|---|
| trades | 2（base_score.w2=1240、C2_act12_T4.w2=1089） | **42** | report §4「交易数仅温和收缩（如 2023-24：1240→1089）」 |
| max_drawdown | 7（base_score.w1/w2/w3、C2_act8_T4.w3、C2_act12_T4.w1/w2/w3） | **37** | report §4「回撤核查」段落散点 |

说明：log 未输出 trades/MDD 列，报告亦未给全 44 格明细，故仅能回填上述散点；
其余格一律 `null`（JSON 中即为 null），不估算、不默认。

---

## 5. 一致性自检（任务书第 5 条，全部通过）

1. **act8_T4 Δ** = [−6.80, −12.33, −9.01, −27.57]pp（任务书原文「+3.55/+9.68/+7.78/+24.64」
   实为 **act12_T4 的 −loss**，即 act12_T4 Δ=[−3.55,−9.68,−7.78,−24.64]pp 的相反数；
   act8_T4 自身 Δ 为 −6.80/−12.33/−9.01/−27.57，与 log 判定行一致）。
2. **base_score 四窗** = [−5.72, −52.90, −31.93, +27.02]% ✓
3. **报告 §1 表逐位一致** ✓（见 §3 核对节）
4. **loss 自洽**：judgement 每格 loss_pp 与「基线 ret − 变体 ret」反推差异 ≤0.011pp；
   score 族对 base_score、board 族（C5_board_T2_A1）对 base_board——全部吻合。
5. **三哈希并列**：损坏态 ≠ 重建态 ≠ 本报告（见 §6）；损坏态与重建态相同即判失败——已确认不同。

---

## 6. 三哈希（SHA256）

| 文件 | SHA256 |
|---|---|
| 损坏态证据 `tmp/c_lane_corrupted.json`（= 原目标损坏态） | `F8F529E591951CDD59BE6502E405CBCB46A0A123A0D41A358753D79A11902967` |
| 重建后目标 `data/bt_c_lane_redispatch.json`（= 重建中间态 Move 后） | `43411E9ED6C765B3291C7C73644F68BFCF15550EAE6AB71F1C9844C7D8C71547` |
| 本报告 `docs/reports/c_lane_rebuild_20260831.md` | `B6143AD418E4211E71C44192140E93F4358B102DE78DF5A8C1F2AB668AC3F202` |

- 损坏态 ≠ 重建态（`True`，证明确实完成了内容替换）；
- 取证副本与目标损坏态一致（`True`）→ 证据链闭合。
- 本报告校验：内容定稿版 SHA256 = `B6143AD418E4211E71C44192140E93F4358B102DE78DF5A8C1F2AB668AC3F202`
  （自引用哈希固有特性：嵌入哈希行本身会使文件字节级哈希变动，故本报告内记录的是
  不含最后嵌入行的内容定稿版校验值；交付时刻字节级权威哈希以交付说明为准）；
  行尾 CRLF=0（LF，项目约定）。

---

## 7. 过程与环境备注（留痕）

- **共享文件 mtime 核验**：作业前确认目标仍为损坏态（23:29:12）且
  `docs/reports/c_lane_rebuild_20260831.md` 不存在（查重门通过，非 DUP-SKIP）。
- **环境事件**：作业中曾出现工具通道连续超时（shell 无响应），以及**新建文件被环境级
  写封锁**（Bash 与 Write 工具均无法在用户目录新建文件，仅可改已有文件/写 OS 临时目录）；
  经 `interaction_authorize_folder` 重新授权项目目录后写权限恢复，任务继续。此事件与
  协调日志记载的工具通道不稳定一致，不影响重建数据正确性（数据全部来自源文件，
  与写盘通道无关）。
- **编码留痕**：`tmp/c_lane.log` 为 UTF-16 LE（BOM FF FE）——已在上文 §2 记录。
- **backlog**：按红线不自行改动 `docs/backlog.md`，由验收方统一登记。

---

*报告完。目标 JSON 顶层结构（generated_at/phase/meta/c4_data_gate/results/judgement/elapsed_s）
与损坏前一致；results 44 格格式 `{variant,strategy,widx,window,total_return,trades,max_drawdown}`；
judgement 9 条 `{variant,strategy,detail[{win,loss_pp}],windows_ok,bull_loss_pp,pass}`；
meta 含 `reconstruction` 块记录本次重建。*
