# 取证报告：updater_diag 假备份与时间线修复（审计链重建）

- **块**：C 块（并发执行者之一）
- **时间**：2026-08-31 22:03 落盘
- **范围**：`data/updater_diag_20260830.json`（假"备份"）虚假陈述取证 + `data/updater_diag_20260830_restored.json` 时间线重建（审计链只读 pass）+ 勘误 append
- **性质**：审计链重建，**不追责**
- **只读红线**：审计链查询用普通读（未用 `--verify`，未触发 `audit_chain_diag` 刷新）；`market.db`/快照/config/审计链/台账均只读

---

## 0. 判定先行

1. `data/updater_diag_20260830.json`（2061B）的 `backup_note` 声称"内容与 2026-08-30 22:40:41 生成的 data/updater_diag.json **完全一致**"——**虚假**。实测为摘要：仅 `timeline_all_count=47`，无 47 条 `timeline_all` 列表。
2. 其 `note` 声称"完整 47 条见 docs/reports/updater_repair_20260830.md（该文档含完整表格）"——**亦不实**。该报告 §1.1 仅 8 行失败/相邻成功对照表，全报告无 47 行全量时间线表格（grep 证据见 §1.3）。
3. 原 8/30 版 `data/updater_diag.json`（24492B，`generated_at 2026-08-30 22:40:41`）已被 8/31 轮覆盖丢失；**47 条时间线已从审计链 100% 重建**（28 条 `data_update` + 19 条 `data_update_failed` = 47），见 `data/updater_diag_20260830_restored.json`，缺口 0。

---

## 1. 虚假陈述取证节

### 1.1 证据一：backup_note 原文引用

`data/updater_diag_20260830.json`（2061B）原文两处关键陈述：

```json
"backup_note": "8/30 版 updater_diag.json 基线备份（2026-08-31 由 T2 轮复制保留，原文件将被 8/31 版覆盖）。内容与 2026-08-30 22:40:41 生成的 data/updater_diag.json 完全一致。"
...
"note": "8/30 版完整 47 条 timeline 与全部细节见 docs/reports/updater_repair_20260830.md（该文档含完整表格）；本备份仅保留核心结构以防原文件被覆盖后丢失关键基线。"
```

**指控点**：①"完全一致"；②"该文档含完整表格"。

### 1.2 证据二：字节数对比（2061 vs 24492）

| 文件 | 字节数 | 内容 |
|------|--------|------|
| 原 8/30 版 `data/updater_diag.json`（已丢失） | **24492**（报告 §10 记录，867 行 LF） | 全量 `timeline_all 47 条` 列表 + `sample_fetch_without_write` 等独有键 |
| 假"备份" `data/updater_diag_20260830.json` | **2061** | 仅摘要：`timeline_all_count=47`（一个数字）、`three_failures`、`daily_300_denominator`、`min5_empty_qualitative`、`hypotheses_ranked`、`freshness_after_0830` |
| 当前 `data/updater_diag.json`（8/31 版） | 7054 | 8/31 独立验证轮内容，无 `timeline_all` 列表 |

- 2061 / 24492 ≈ **8.4%**，字节量级差约 12 倍，与"完全一致"严重不符。
- 假备份中 `timeline_all` 只剩 `timeline_all_count: 47` 一个计数键，**47 条列表本体缺失**——"47 条只剩计数"实测成立。

### 1.3 证据三：grep 证"8/30 报告无 47 行时间线表"

命令（pwsh，`Set-Location 'C:\Users\26838\A股模拟盘'` 后）：

```powershell
# G1：报告内 timeline_all 出现次数
Select-String -Path 'docs/reports/updater_repair_20260830.md' -Pattern 'timeline_all' | Measure-Object | Select-Object -ExpandProperty Count
# → 1（仅 §10 交付物作为指向 diag 文件的指针，line 163，非表格）

# G2b：§1.1 时间线表数据行（| 8/2x 日期行）
(Select-String -Path 'docs/reports/updater_repair_20260830.md' -Pattern '^\| 8/2').Count
# → 7（另含 | 8/30 行以 "8/3" 开头未计入；§1.1 表合计最多 8 行数据行）

# G2d：全报告 markdown 表格行（^|）
(Select-String -Path 'docs/reports/updater_repair_20260830.md' -Pattern '^\|').Count
# → 17（§1.1 表 10 行 + §8 成功判据表 7 行，全报告仅两张小表）

# G5：报告总行数
(Get-Content 'docs/reports/updater_repair_20260830.md').Count
# → 103
```

**输出结论**：
- `timeline_all` 全报告仅出现 1 次，且是 §10 里指向已丢失 diag 文件的交付物描述，**不是**表格；
- §1.1 时间线表数据行仅 7–8 行（三次失败 + 相邻成功），**远非 47 行**；
- 全报告 markdown 表格行合计 17 行，仅含 §1.1 与 §8 两张小表；
- 报告共 103 行，不可能容纳"完整 47 条时间线表格"。

**三证据并列结论**：假备份 `backup_note` 的"完全一致"与 `note` 的"报告含完整表格"均为不实陈述；实际为摘要备份 + 错误指针。

---

## 2. 勘误指针节

已向 `docs/reports/updater_repair_20260831.md` **末尾 append** 一节「附：备份声明勘误（2026-08-31 C块）」，内容 = 声明"完全一致"不实、实际为摘要、完整版见 `_restored.json`。

**纪律执行（append 前后双哈希）**：

| 项 | 值 |
|----|----|
| append 前 `Get-FileHash`（SHA256） | `BEB624BF2C6ED073E83EEBE75BDB44DFCDD14B11E1417F0C049331139B28B313` |
| append 后 `Get-FileHash`（SHA256） | `AA5BC9D87E3F74B58B78144B436DA1A5BA850F25C1C5873174E45051255AB28B` |
| 大小 | 11214 B → 12090 B（+876 B） |
| 正文完整性 | 逐字节验证 `new[:11214] == old`，**正文一字未改，只加尾部** |
| 文件行尾 | LF（CRLF 数 0） |

被 append 文件触碰前 mtime 记录（红线5）：`2026-08-31 00:32:52`，11214B。

---

## 3. 不可恢复清单节（审计链无法旁证的独有键）

原 24492B 文件独有键逐一判定（判定依据：审计链 2939 行全量检索 + 8/30 报告 §10 交付物描述 + 假备份 2061B 保留键对比）：

| 原文件独有键 | 审计链是否可旁证 | 其他产物可否旁证 | 判定 |
|--------------|------------------|------------------|------|
| `timeline_all`（47 条列表） | **可**——28 条 `data_update` + 19 条 `data_update_failed` 全量在链（自 8/19 起） | 8/30 报告 §1 标题"全量 47 条 data_update，含 data_update_failed 伴生"佐证口径 | **已 100% 重建**（`_restored.json`，每条附 `_src_line`），非不可恢复 |
| `sample_fetch_without_write`（样本只拉不写试验记录） | **不可**——样本复现为只拉不写、不落审计链，链内无此事件 | **可部分旁证结果**：8/30 报告 §2.1（30/30 via tdx，80 行/只，0 异常，单只 0.04–0.62 s）、§2.2（min5 头部 5 只均 20 行） | **原始 JSON 载荷彻底丢失**；试验结果可由报告文字旁证 |
| `neighbor_success`（相邻成功对照） | **内容可重建**——相邻成功日（8/20 15:10:57、8/25 15:11:14、8/26 15:14:46、8/27 15:18:11、8/30 22:08:22）均为链内 `data_update` 事件 | 8/30 报告 §1.1 有 8 行对照表 | **原始键结构丢失**；内容为派生视图，可从审计链重建（非原始事件） |
| `three_failures` / `daily_300_denominator` / `min5_empty_qualitative` / `hypotheses_ranked` / `freshness_after_0830` / `generated_at` / `audit_file` | — | 假备份 2061B 已保留 | **未丢失**（保留在假备份中，可继续引用） |
| 其他未知键 | 原文件 867 行/24492B vs 假备份 2061B，可能尚有假备份未收录键 | 无法枚举 | **彻底丢失风险面**（因原文件被覆盖，无法穷举确认；本报告如实披露） |

> 注：审计链内另有 `data_snapshot×3`、`drill_alert_path×3`、`qfq_exclude_applied×1` 等相邻事件，因不属于 8/30 报告所定"47 条 data_update+failed"口径，未并入 `timeline_restored`（宁多勿漏原则已覆盖全部 19 条失败/告警事件）。

---

## 4. 建议节（只建议，不改任何规范文档）

1. **"备份"类操作必须先哈希比对源文件**：复制/备份前先 `Get-FileHash` 源文件并记录，备份后立即对备份文件再哈希比对，哈希一致才算"备份成功"；本次事故若执行此步，2061B 摘要冒充 24492B 原文件的瞬间即会被发现。
2. **备份文件名应含源 mtime**：如 `updater_diag_20260830_224041.json`，使"备份的是哪个版本、何时生成"自证于文件名，杜绝版本歧义。
3. **大 JSON 更新应走"新文件名+切换"而非原地覆盖**：写入 `updater_diag_<version>.json` 新文件 → 校验通过 → 再切换引用（或保留版本号），原文件永不原地覆盖；可配合 `.json` 旁挂哈希清单。

---

## 5. 时间线重建结果（对账）

- **恢复数**：47/47（`data_update` 28 + `data_update_failed` 19），缺口 0。
- **口径对账**：与原文件 `timeline_all_count=47` 精确吻合；与 8/30 报告 §1 标题"全量 47 条 data_update，含 data_update_failed 伴生"一致。原文件已丢失，无法逐键核对 `timeline_all` 条目字段序，故每条以审计链行号 `_src_line` 自证来源。
- **字段来源**：每条 `t/event/载荷字段` 均逐字取自 `data/audit/audit.jsonl` 原文（独立回读校验：47 条 × 全字段 = 0 不一致），未凭记忆或从报告补写任何数字。
- 产物：`data/updater_diag_20260830_restored.json`（17160B，LF，SHA256 `F5C713A00DB01FD3A1AFB1D1A0F04D81B6084E7807E6E4A21E3D99FFA80C7C5D`）。

---

## 6. 共享文件 mtime 记录（红线5）

| 文件 | 大小 | mtime |
|------|------|-------|
| `docs/reports/updater_repair_20260831.md`（append 目标） | 11214 → 12090 B | 2026-08-31 00:32:52（append 后 22:02:18） |
| `docs/reports/updater_repair_20260830.md` | 14222 B | 2026-08-30 22:44:08 |
| `data/updater_diag.json`（8/31 版，未改动） | 7054 B | 2026-08-31 00:32:23 |
| `data/updater_diag_20260830.json`（假备份，未改动） | 2061 B | 2026-08-31 00:32:02 |
| `data/audit/audit.jsonl`（只读） | 677267 B | 2026-08-31 15:10:02 |

## 7. 验证方式与覆盖

- **时间线重建**：脚本化从审计链提取 → 独立回读脚本逐条逐字段比对审计链原文（47 条 × 全字段 = 0 不一致）；行尾 LF 校验（CRLF=0）。
- **append 纪律**：二进制纯追加（`open('ab')`），回读断言 `new[:11214]==old` 证明正文未改；前后双哈希记录。
- **grep 证据**：本报告 §1.3 命令即为取证命令，输出已随文引用。
- **已知缺口**：原文件未知独有键无法穷举（§3 末行如实披露）；`sample_fetch_without_write` 原始载荷不可恢复（结果可部分旁证）。
