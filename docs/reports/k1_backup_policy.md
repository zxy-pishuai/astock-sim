# K1｜行情库真实副本（P1）——介质勘查停手报告

- 日期：2026-09-05（周六，验收方第二班）
- 块：K1（零上下文执行）
- **结论（停手）**：本机**只有一块物理盘**，**无任何第二介质**。按任务第 1 步指令**立即停手**——任何备份都是同盘自欺，**未往 C: 拷贝任何副本**，交回人类决策。

---

## §0 判据与查重

- 查重门：`tmp/pack20_dup.log` 已追加 `K1 <2026-09-05 UTC> GATE-OK`。
- `rg -n "backup" tools/ docs/reports/ tmp/pack*/` 扫描：`tools/backup_data.py` 已存在（sqlite backup API，`--keep` 默认 7），**全机计划任务表无任何调用它的任务**（与验收方背景一致）；近期报告仅提及该工具存在性，**无人正在实施副本策略** → GATE-OK 成立。
- 派发时机核对：`docs/reports/watchdog_hardening.md` 存在（pack19 H1 已交卷），本块可执行。但执行在介质勘查处停手。

## §1 介质勘查结论（停手依据，数字与命令输出逐字一致）

**物理盘枚举（`Get-PhysicalDisk`）**：

| 盘 | BusType | MediaType | 大小 |
|---|---|---|---|
| Disk 0 `KPEGYMTBW2U001TCJD` | NVMe | SSD | 953.9GB |

**`Get-Disk`**：仅 `Disk 0`（NVMe，953.9GB）——**本机只有一块物理盘**。

**逻辑卷（`Get-Volume`）**：

| 盘符 | 标签 | 大小 | 剩余 |
|---|---|---|---|
| C: | Windows | 952.1GB | 186.7GB |
| （无盘符） | Recovery | 0.5GB | 0.5GB |
| （无盘符） | （EFI 类） | 1.0GB | 0.1GB |

**网络映射（`Get-PSDrive`）**：仅 `C | C:\`（本地盘）——**无任何 `\\server\share` 映射**；无外接盘、无 NAS。

**结论**：除 C: 外**没有任何可写数据卷**。Recovery/EFI 分区不可写且非数据介质。按 K1 任务第 1 步："若只有 C: 一块物理盘，立即停手……不得退化成往 C: 再拷一份"→ **本块停手**。C: 剩余 186.7GB 不足容纳现状行情库总资产（market.db 1458.8MB + min5.db 724.6MB + cyq_cache 142.3MB + snapshots 11.9GB 同盘），且**同盘拷贝在故障（盘坏/掉电）时与源同灭**，不构成副本。

## §2 实现与命令原文（未执行，等人类提供介质）

因 §1 停手，`tools/backup_data.py --dest <路径>` 异盘备份**未实施**。人类提供第二介质（外接 USB/NVMe 盘或 NAS 映射盘，挂载为 D:/E: 或网络路径）后，就绪方案如下（命令原文，届时逐字执行）：

```
# 首次异盘备份（SQLite 在线备份 API，禁止 copy 正在写的 .db）
python tools/backup_data.py --dest D:\tianji_backup
# 产物：<D:\tianji_backup>\<时间戳>\market.db + 全部 JSON + SHA256SUMS.txt + manifest.json
```

`tools/backup_data.py` 已内置 sqlite `backup` API（非文件 copy，规避撕裂副本）；`--big` 同时备份 min5.db（~725MB，较慢）。首建备份目录路径需人类确认（写白名单要求）。

## §3 首次备份 manifest（无——停手，无备份产物）

无首次备份。待介质就绪后按 §2 执行，manifest 将含：源库行数快照（`kline` 8,848,791 / `kline_min5` 5,469,424）+ 文件字节数 + 耗时 + SHA256SUMS.txt。

## §4 恢复演练比对表（未执行——无副本可恢复）

任务要求：RESTORE 到 `tmp/pack20/restore_test/`，只读比对 `kline` 行数 / `COUNT(DISTINCT code)` / 最近 5 个交易日逐日期覆盖数 / 随机 20 票×5 日收盘价，全一致才通过；**禁把副本写回任何生产路径**。此演练在介质就绪后与 §2 同步执行，比对项与通过标准已预注册。

## §5 任务注册三字段读数（未执行）

任务 `TianjiDataBackup_Weekly`（每周日 10:00）**未注册**（无备份目标，注册无意义）。就绪后注册参数（预注册）：`StartWhenAvailable=true`、`DisallowStartIfOnBatteries=false`、`ExecutionTimeLimit=PT2H`；注册前先导出同名任务 XML 留档（若已存在）。

## §6 待批准清理清单（只登记，未删除——物理删除属红线）

| 路径 | 大小（实测） | mtime | 说明 |
|---|---|---|---|
| `data/market.db.bak` | 63.0MB | 2026-08-18 23:43 | 8/18 陈旧备份，同盘无副本价值 |
| `data/market.db.bak.20260819_011241` | 63.4MB | 2026-08-19 00:26 | 8/19 陈旧备份（合计 126.4MB） |
| `data/logs/watchdog_child_stderr.log` | 0.2MB（209KB） | 2026-09-05 02:26 | 旧固定名 stderr 日志 |

**以上均未删除，列入待批准清单**；批准后由人类或指定执行者物理删除。

## §7 未覆盖风险（停手状态下的如实声明）

1. **无任何异盘/异地副本**：行情库（market.db 1458.8MB + min5.db 724.6MB）当前仅存在于 C: 单盘，盘故障=全灭。`data/snapshots/` 9 份 11.9GB 与生产库同盘，**不是副本**。
2. **介质寿命**：单 NVMe 无冗余，写入磨损/掉电风险无法对冲。
3. **恢复耗时 SLA**：未演练，恢复耗时未知（待介质就绪后测）。
4. **git nightly 兜不住行情库**（.gitignore 排除 market.db*/min5.db*/snapshots/），与背景一致，进一步强化异盘副本的必要性。
5. **决策请求**：请人类决定——(a) 挂接外接盘/NAS 后继续 K1；(b) 接受单盘现状并登记风险；(c) 其他方向。

---

**本块未做的事**：未备份、未建 `tools/backup_verify.py`（无介质可演练）、未注册任务、未删除任何文件（清理项仅登记）。生产库全程只读（`file:...?mode=ro&immutable=1` 未触碰——本块甚至未打开任何 .db），未改 `app/config.py`，未重启/未杀任何进程，未动 `data/audit/*`。
