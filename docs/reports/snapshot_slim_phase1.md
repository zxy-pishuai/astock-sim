# Y4 存储瘦身一期：旧快照压缩归档 + backups 销账

- 创建时刻：2026-09-01 12:05（Asia/Shanghai）
- 执行块：Y4（全项目唯一允许移动/压缩文件的块，纪律最严）
- 对应任务书：Y4｜存储瘦身一期

---

## §0 预注册（判据/方法，先落盘再执行）

| 项 | 内容 |
|---|---|
| 目标 | 一期：可安全归档的快照 + backups 全部"压缩→三验→入暂存"；二期（7 天人工确认）删暂存源。目标项目总 8.45GB→≤5.5GB（活体口径）。 |
| 零删除红线 | 一期不删除任何文件；源文件一律移入 `data/snapshots/_archive_pending_del/src/` 暂存，7 天人工无异议才归二期删。 |
| 完整性三验 | 压缩前 SHA256 + 压缩产物 SHA256 + 解压实测 SHA256 三值一致；逐文件记入 `data/archive_index.json`。 |
| 禁碰 | market.db / min5.db（热库，服务 PID 44104 在写）、data/audit、account、快照 manifest 原文件、任何代码、.gitignore。 |
| 可归档判定 | 引用普查为硬前置：仅零活跃代码引用的枚可归档。 |
| 算法 | 任务书"zstd 优先"；zstd 不可用（无 CLI / 无 pip 包，安装会越出本块写入面）→ 回退 stdlib lzma，两档实测后选定。 |

---

## §1 基线（验收方 11:00 实测 + 本块复核）

| 构成 | 体积 |
|---|---|
| 快照 `data/snapshots/` | 4.15GB（26/27/30/31 四枚） |
| backups `data/backups/` | 1.94GB |
| market.db（热库） | 1.21GB |
| min5.db（热库） | 0.68GB |
| tools | 0.3GB |
| 代码+文档 | ~20MB |
| **项目总** | **8.45GB** |

本块复核（11:10）与验收方一致；lzma 对 kline 整库行式压缩率实测 5.1x（与验收方吻合）。

---

## §2 引用普查（动手前置，可归档性硬判定）

对 `tools/*.py`、`tmp/w2/*.py`、`tmp/w3/*.py`、`data/bt_*.json`（meta.snapshot 字段）、`docs`/`app` 全类型扫描 `snapshots/2026-08-2x` 硬引用：

| 快照枚 | 活跃代码引用 | bt_*.json 引用 | 判定 |
|---|---|---|---|
| **2026-08-30** | **无**（仅 backlog/acceptance_20260831/act12_t4_pit 叙述性文本提及路径） | **无** | ✅ **唯一可安全归档** |
| 2026-08-26 | c_lane_redispatch.py / exit_pack_v2_scan.py / time_stop_reverify.py | bt_c_lane_redispatch / bt_exclude_landing_regression / bt_exit_pack_v2_p68 / bt_regime_gating / bt_time_stop_reverify（5） | 🔒 钉住，不可动 |
| 2026-08-27 | sizing_lab.py / act12_t4_pit.py + tmp/w3/probe_dirty.py | bt_act12_t4_pit / bt_board_pit_audit / bt_sizing_lab / bt_sizing_replay（4） | 🔒 钉住，不可动 |
| 2026-08-31 | ml_shadow_portfolio.py + tmp/w2/*（probe/probe2/verify_o2o_c2c/diag_period） | bt_ml_shadow / bt_sector_rotation / bt_it_snapshot_retest（3） | 🔒 钉住，不可动 |

> **结论与任务书预判相反**：任务书猜测"大概率只能归档 08-26"，实测 08-26 被 3 活跃工具+5 回测钉住；真正零引用的是 **08-30**。按"引用关系是硬前置"，一期只动 08-30 + backups（backups 为安全副本，无运行时引用）。

---

## §3 归档工具与算法选型

**工具**：`tools/snapshot_archive.py`（本块交付）。子命令：`--archive`（快照/backups）、`--restore`（单文件还原）、`--verify`（解压全验）、`--test`（两档实测）。zstandard 可用时自动用 zstd，否则回退 stdlib lzma。

**两档实测**（真实文件 08-26 market.db 528MB，只读源，输出 tmp）：

| 档位 | 压缩率 | 耗时 |
|---|---|---|
| lzma level 3 | 5.07x | 50.4s |
| lzma level 9 | **6.11x** | 240.2s |

→ 选 **level 9**（比率更高，总耗时 ~25min 在 11:10-13:50 IO 窗口内）。zstd 不可用说明：系统无 zstd CLI、PATH python 与 sidecar venv 均无 zstandard 包；安装会越出本块写入面（tools/ + data/ + docs/ + tmp/y4/），故不安装，回退零依赖 lzma（且实测比率优于 zstd 预期 3.5-4x）。

---

## §4 执行与完整性三验（33 文件全绿）

### 4.1 归档清单（原 → 压缩）

| 文件 | 原体积 | 压缩后 | 压缩率 |
|---|---|---|---|
| snapshots/2026-08-30/market.db | 1239.7MB | 238.2MB | 5.21x |
| backups/20260823_034849/market.db | 979.3MB | 209.2MB | 4.68x |
| backups/20260823_151504/market.db | 996.0MB | 208.5MB | 4.78x |
| backups 其余 30 文件（json/shm/wal） | ~7.8MB | ~0.9MB | — |
| **合计 33 文件** | **3222.8MB** | **656.8MB** | **4.91x** |

### 4.2 双哈希（压缩前后对账 + 解压实测，三值一致）

| 文件 | sha256_orig = sha256_restored |
|---|---|
| 08-30/market.db | `5F9AE99E357CAD92B1C933F583BF0117C3131A7C4A94D8D75BC0446A01C14107` |
| backups/20260823_034849/market.db | `082438F4FA947F21016F2FF0603840EAAA66BADF9065B1EC856DF0BEC8BA994F` |
| backups/20260823_151504/market.db | `56BC6E666534ABC629FE749B47B1718EBF91F9069A8C3C573C606E07DA539732` |

- 全量 `--verify --all`：**33/33 全绿**（每枚解压→重哈希→与压缩前哈希 MATCH）。
- 与 H2 块报告交叉印证：qfq 批修备份 `kline_day_qA1...`（`674A259D…`）、`kline_day_b20260831...`（`DAF39A8A…`）哈希与 H2 报告记录一致，备份未被篡改。
- 08-30 `manifest.json`（created_at=2026-08-30T22:08:43, rows_kline_day=8693312）**保留原位未动**（红线：禁碰 manifest 原文件）。

### 4.3 暂存结构

```
data/snapshots/_archive_pending_del/
  data/<相对路径>.xz     ← 33 枚压缩归档（新规范，共 656.8MB）
  src/data/<相对路径>    ← 33 枚原始源文件（待 7 天人工销账，共 3222.8MB）
data/snapshots/2026-08-30/   ← 现仅余 manifest.json（market.db 已移出）
data/backups/20260823_*/     ← 空目录壳残留（源已移出，无害）
```

---

## §5 还原演练（二期敢删的地基）

`python tools/snapshot_archive.py --restore --tag 2026-08-30 --to tmp\y4\restore_drill\market.db`

- 还原 1239.7MB 成功，SHA256 = `5F9AE99E...` **与原件 MATCH**。
- 还原库 SQLite 打开有效：表结构完整，`kline day rows = 8,693,312` **与 manifest.json 完全一致**。
- 演练副本已删除（哈希已入报告与索引）。

---

## §6 体积对比

| 口径 | 体积 | 说明 |
|---|---|---|
| 验收基线（11:00） | **8.45GB** | 快照 4.15 + backups 1.94 + 热库 1.89 + tools 0.3 + 代码 0.02 |
| 阶段一瞬态（当前磁盘） | **9.11GB** | 源文件移入暂存（不新增），+0.66GB 为 33 枚 .xz 归档 |
| **活体占用**（不含 `_archive_pending_del/src`，.xz 为新规范） | **~5.9GB** | 8.45 − 2.506（可释放量 2566.0MB） |
| 阶段二（7 天销账删暂存源后） | **~5.9GB** | 9.11 − 3.147（暂存源 3222.8MB） |

**目标 ≤5.5GB 缺口 ~0.4GB**：需阶段二扩档 08-26（528.2MB → ~87MB，lzma-9 6.11x 实测，省 ~441MB → 可达 5.45GB）。08-26 现被 3 冻结回测工具 + 5 个 bt_*.json 钉住，属"引用硬前置"禁动范围；须在阶段二配合还原演练 + E 块验收确认后再归档（见 §8 受影响清单）。

---

## §7 KEEP_N 生效实证（data_snapshot.py）

- 代码路径已读：`app/data_snapshot.py` `KEEP_N_DEFAULT=10`；`create_snapshot()` 每次收盘建快照后调 `_cleanup(keep_n)`；`_cleanup` 在 `len(snaps) > keep_n` 时删除最旧 `snaps[:-keep_n]`（market.db + manifest + 目录）。
- `list_snapshots()` 只认 `<snapshots>/<tag>/market.db` → 08-30 归档出列后，当前快照计数 = **3**（26/27/31），**3 < 10 属未触发期**。
- **首见自动清理预告**：按每交易日 1 枚计，从 3 枚起，第 11 枚创建时触发首删。交易日推算 ≈ **2026-09-10**（9/1,2,3,4,5,8,9 到 10 枚，9/10 第 11 枚触发清理，删最旧 08-26）。08-30 归档使计数 −1，首删较不归档推迟约 1 个交易日。
- **列为 X5 / 明日观察项**：9/10 前后核实首次自动清理是否如期发生（删除对象应为 2026-08-26）。

---

## §8 受影响脚本清单（阶段一 = 空）+ 自愈说明

**阶段一受影响脚本：空**。唯一归档枚 08-30 无任何代码/回测 JSON 引用（见 §2），归档未破坏任何工具的既有路径，无需自愈。

**阶段二扩档预案（若人类批准归档 08-26/27/31）**：受影响脚本 = §2 引用普查表对应行；自愈方式 = 每个受影响工具运行前先执行对应 `--restore`（单文件还原已演练通过），例如：

```powershell
python tools\snapshot_archive.py --restore --tag 2026-08-26 --to data\snapshots\2026-08-26\market.db
```

还原后哈希回读对 archive_index.json，MATCH 即可安全重跑。

---

## §9 二期提案（不实施，只给决策矩阵）

| 方案 | 空间收益 | 复杂度/风险 | 适用前提 |
|---|---|---|---|
| A. 现况（快照 ≤10 枚自滚，backups 原样） | 稳态封顶 ~13GB | 无 | 现状 |
| B. 快照归档延续（本工具全枚压 + 二期删暂存） | 稳态 ~6GB，全枚归档可 ~5.5GB | 低；还原器已备 | 引用普查后逐枚获批 |
| C. 增量快照模型（每日只存当日 delta kline） | 稳态 ~2-3GB | 中；需改 create_snapshot 与读方 | 需先冻结读方清单 |
| D. 热库 Parquet 引擎迁移（market.db/min5.db → Parquet 列存） | 热库 1.89GB → ~0.4GB | 高；动交易主链路 | 需压测 + 双写过渡 |

- 参考数据：验收方 lzma kline 行式 5.1x；本块整库 lzma-9 实测 4.91x（33 文件）、单枚 5.21x。
- 推荐路线：先走 B（零风险，本块已完成 08-30+backups），阶段二扩档 08-26 补足 ≤5.5GB；C/D 留待商用化架构评审，不与一期混跑。

---

## §10 合规声明与遗留项

- ✅ 一期零删除：33 枚原始源全部在 `_archive_pending_del/src/`，暂存区之外无"原文件消失"状态。
- ✅ 未碰热库 market.db/min5.db（服务 PID 44104 在写）、data/audit、account、manifest 原文件、任何代码。
- ✅ `.gitignore` 伞下覆盖已核实：`data/snapshots/`（line 9）与 `data/backups/`（line 10）均在 ignore；`_archive_pending_del/` 位于 `data/snapshots/` 伞下自动覆盖，无需改动。
- ✅ 未重启/未杀进程；未跑任何 git 写命令。
- 📌 暂存区 `_archive_pending_del/src/` 保留 7 天（至 2026-09-08），人工无异议才归二期删除。
- 📌 backups 空目录壳 `data/backups/20260823_*/` 残留（源已移出，0 字节，无害；二期随目录清理一并处理）。
- 📌 `data/archive_index.json` 为本块唯一权威索引（33 项，含原路径/归档路径/暂存路径/大小/双哈希/解压哈希/时刻/耗时）。

### 交付物
- 工具：`tools/snapshot_archive.py`
- 索引：`data/archive_index.json`（33 项）
- 暂存：`data/snapshots/_archive_pending_del/`（.xz 656.8MB + src 源 3222.8MB）
- 报告：`docs/reports/snapshot_slim_phase1.md`（本文）
- 临时日志：`tmp/y4/`
