# B3 存储治理 — 只读盘点 + 快照分级 + 待删清单（2026-09-13）

> **本单红线执行情况**：全程只盘点不删除。73 项 / 8.59GB 待删清单见 §4，
> 清理命令已给出，**等用户书面确认后由 `storage_gc.py --apply --yes` 执行**。

## 1. 全景盘点（data/，只读，`tools/storage_gc.py --inventory`）

实测（2026-09-13）：**data/ 共 22.02GB**（slim 生成后 35.69GB，见 §3 说明）。清单
落盘 `data/storage_inventory.json`。>100MB 项：

| 项 | 大小 | mtime | 分类 | 判定依据 |
|---|---|---|---|---|
| data/market.db | 1.75GB | 09-13 13:50 | 必须保留 | 热库（服务在跑） |
| market.db.bak_a1_20260913_124804 | 1.75GB | 09-13 02:45 | 待确认 | A1 单备份，验收已过 |
| market.db.bak_F3_20260908 | 1.47GB | 09-08 22:50 | 待确认 | F3 验收已过；**backfill_amount.py:28 安全闸仍引用 bak_F3_\*** |
| market.db.bak_F1_20260908 | 1.47GB | 09-08 20:31 | 待确认 | F1 验收已过 |
| data/snapshots/09-08/market.db | 1.47GB | 09-08 23:00 | 必须保留 | ≤5 天全库快照 |
| data/snapshots/09-07/market.db | 1.43GB | 09-07 16:38 | 待确认 | 6 天档（slim 已生成） |
| data/snapshots/09-04~09-01×4 | 1.22~1.31GB | 09-01~04 | 待确认 | 6~30 天档（slim 已生成） |
| data/snapshots/08-31/08-27/08-26 | 0.52~1.21GB | 08-26~31 | 待确认 | 同上 |
| data/snapshots/_archive_pending_del/ | **3.79GB** | 09-01 | 待确认 | 归档暂存区（src 源+ .xz） |
| data/min5.db | 0.74GB | 09-11 19:37 | 必须保留 | 分钟线库 |
| data/cyq_turnover.parquet | 39MB | 09-03 | 必须保留 | CYQ 数据 |
| keypack/（7 枚 tar.gz） | ~15MB | 09-05~12 | 待确认 | 每日密钥包轮转 |

快照明细：**9 枚全库快照**（08-26/27/31、09-01~04、09-07/08）共 10.70GB；
**无 09-09~09-13 快照**（F4 追平事件驱动；09-11 覆盖率仅 45.5% 未追平 → 未落快照）。

### 引用盘点（grep 全仓）
- `backfill_amount.py:28/94`：`BACKUP_GLOB=market.db.bak_F3_*` —— F3 脚本安全闸，**删 bak_F3 前需先改/确认该闸**。
- `tools/snapshot_archive.py:31`：`STAGE=_archive_pending_del` —— 归档工具维护，09-08 观察期已到（7 天）。
- `app/data_snapshot.py` / `app/updater.py`：snapshots 保留机制引用（不可删目录本身）。
- 其余 `.bak`（63MB×2）无代码引用，多份报告已登记"陈旧备份可删"。

## 2. market.db 增长归因（5 天 +295MB 实测）

对比主库 vs 09-08 快照（表级行数，只读）：
- kline 行差 **+35,475 行**（主 9,120,043 vs 快照 9,084,568）→ 逻辑数据增量 ≈ **3.5MB**
  （09-09/10/11 三日 ~15k 行 + qfq 修复/F1 指数回填补行 ~20k）
- 文件差 **295MB** 远超行增量 → **主库 B-tree 页碎片**：qfq 批量 UPDATE 修复（E 块 260+ 票历史重写）
  导致页分裂后半满页未回收；快照经 `src.backup()` 在线备份输出紧凑（页按逻辑序重排）。
- `freelist_count=0`、`journal_mode=delete`：无空闲页、无 WAL 残留。

**VACUUM 建议**：可回收 ~15%（≈280MB）。磁盘剩余 **151.8GB**（C: 总 952GB），2 倍空间要求满足。
**注意**：VACUUM 为排它操作，**禁止在生产运行中执行**——需在无写窗口（如 22:30 后或周末）由人执行：
```
python -c "import sqlite3; c=sqlite3.connect('data/market.db'); c.execute('VACUUM'); c.close()"
```
（不自动跑；若频繁碎片复发可注册月度 VACUUM 任务，先看下次快照对比确认回收量。）

## 3. 快照保留分级（任务 2）

### 3.1 config.py 新增开关（默认保守，仅新增不改既有值）
```python
SNAPSHOT_KEEP_FULL_DAYS = 5        # 最近 5 天全库快照（回测 PIT 基准）
SNAPSHOT_SLIM_MIN_AGE_DAYS = 6     # 6 天起进入 slim 档
SNAPSHOT_SLIM_MAX_AGE_DAYS = 30    # 超过 30 天进入待删档（默认仅登记）
SNAPSHOT_DELETE_OLDER_THAN = None  # None=不自动删（只登记待确认清单）
SNAPSHOT_SLIM_ENABLED = True       # slim 生成开关
SNAPSHOT_SLIM_YEARS = None         # None=全史无损；3=近 3 年（有损，~450MB/枚）
```

### 3.2 `data_snapshot.py:_cleanup` 分级（幂等）
保留最近 5 天全库 → 更老且已有 slim → 删全库版 → 更老无 slim → 保留登记（等 slim 生成）
→ >30 天且开关开启才删。`keep_n` 旧参数兼容保留（以 full_days 语义为准）。

### 3.3 slim 生成与**预期修正（重要）**
`storage_gc.py --slim` 已为 8 枚 6~30 天档快照生成 `market.slim.db`（ATTACH + CTAS 只留
kline day + 索引 + 内容校验，8 枚全部行数完整、maxdate 正确，独立回读验证）。

**实测瘦身效果与派单预期不符**：kline day 全史 8.7M 行 ≈ 1.17GB/枚（压缩比 0.94），
去掉的 min5（90k 行）+ 小表仅省 5~20%——**"300~500MB"目标需要裁行**（近 3 年 ≈450MB）。
派单预期基于"kline day 可压到 300~500MB"的假设，实测行数是刚性下限。
→ 新增 `SNAPSHOT_SLIM_YEARS` 开关（默认 None=全史无损，保守不破坏历史 PIT 回放；
  如需磁盘优先可设 3 后重跑 `--slim`）。**裁行与否属用户决策**。
slim 单测通过（`tmp/b3_slim_test.py`：全史 3 行 / 近 3 年 2 行）。

**磁盘影响**：slim 与全库并存期 data/ 为 35.69GB；下次快照落盘时 `_cleanup` 自动删
被 slim 覆盖的全库版 → 回落至 ~27GB（全库 5 枚 + slim 8 枚 + 主库等），此后每日滚动。

## 4. 待删清单（73 项 / 8.59GB，等用户确认）

| 组 | 项数 | 大小 | 说明 |
|---|---|---|---|
| `.bak_*` 大备份（a1/F1/F3） | 3 | 4.69GB | 验收已过；F3 备份删除前需先解除 backfill_amount.py 安全闸（或确认 F3 永久完成） |
| `_archive_pending_del/`（src 源 + .xz） | 66 | 3.85GB | 09-01 建，09-08 观察期到（snapshot_slim_phase1 约定） |
| 陈旧 `.bak`/`.bak.20260819` | 2 | 126MB | 8/18-19 手工备份，多报告已登记可删 |
| `-shm/-wal` 碎片 | 2 | ~32KB | F1 备份 WAL 残留 |

**确认后执行**：`python tools/storage_gc.py --apply --yes`（当前拒绝无 --yes 的 apply）。
回滚预案：`_archive_pending_del` 内 `.xz` 均为三验归档（archive_index.json 可 restore）；
`.bak_*` 删除后如需要可从未删快照重建，无额外风险。

## 5. 每周存储 KPI（任务 5）

`storage_gc.py --kpi`：data/ 总大小 / 快照数 / 最大单文件 / 7 日增幅 →
audit 事件 `storage_weekly_kpi`（kind=storage）+ `data/storage_kpi_history.json` 基线。
首条（2026-09-13）：22.02GB / 快照 9 枚（10.70GB）/ 最大 market.db 1.75GB / 7 日增幅=首次无基线。
**计划任务注册命令（可选，待批准）**：
```
schtasks /create /tn "TianjiStorageWeekly" /tr "cmd /c cd /d C:\Users\26838\A股模拟盘 && python tools\storage_gc.py --kpi" /sc WEEKLY /d SUN /st 09:30 /f
```

## 6. 验证与自检
- `py -3.13 -m py_compile app/config.py app/data_snapshot.py tools/storage_gc.py tmp/b3_slim_test.py` ✅
- `tmp/b3_slim_test.py` → `B3_SLIM_TEST_OK` ✅
- 8 枚 slim 独立回读：行数 3.2M~8.77M、maxdate 与快照日一致 ✅
- `--inventory` 全量清单已落 `data/storage_inventory.json` ✅
- KPI audit 事件已写（storage_weekly_kpi）✅

## 7. 未做清单（留给验收/后续）
1. **未删除任何文件**（待用户确认 §4 清单）。
2. 未执行 VACUUM（排它操作，需无写窗口由人执行，命令见 §2）。
3. `SNAPSHOT_SLIM_YEARS` 保持 None（全史无损）；裁行需用户决策。
4. 未注册 `TianjiStorageWeekly`（命令已给出，待批准）。
5. 未改 `backfill_amount.py` 安全闸（删 bak_F3 前置条件）。
6. `data_snapshot.py` 存在两个 `latest_snapshot_path` 定义（L76/L310 并发合并痕迹，
   后定义生效）——非本单范围，登记待后续统一。
