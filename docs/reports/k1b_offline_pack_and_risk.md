# K1b｜没有第二介质时的降级副本策略 + 重建手册（P1）

- 日期：2026-09-05（周六）
- 块：K1b（零上下文执行）
- **结论**：本机单盘无第二介质，异盘备份（K1）停手成立。本块不假装解决盘坏，而是解决三件真实且高频得多的损失：**误删 / 逻辑损坏（写坏 JSON、撕裂、误覆盖）/ 重建无手册**。已实现 `tools/backup_key_pack.py`（极小关键包，221 members / 压缩 1.94MB / 每日 23:40 自动 / 保留 10 份）+ 三场景重建手册 + 风险登记。**同盘 keypack 只防误删/写坏，不防盘坏——盘坏=无解，等外接介质。**

---

## §0 判据与查重

- **查重门**：`tmp/pack20_dup.log` 已追加 `K1b 2026-09-05 22:46:51 GATE-OK`。
- `rg -n "backup_key_pack|keypack|k1b_offline|TianjiKeyPack" tools/ docs/reports/ tmp/pack*/` 扫描：**无命中**（K1 停手报告 `k1_backup_policy.md` 未实现任何东西，不算重复）→ GATE-OK 成立。
- **对 K1 的续作声明**：K1（`docs/reports/k1_backup_policy.md`）因单盘无第二介质判"停手"，未往 C: 拷贝任何副本。本块是 K1 的续作——不解决盘坏，而是在同盘内做"极小关键包"防误删/写坏 + 重建手册。K1 的异盘备份命令（`tools/backup_data.py --dest <路径>`）在介质就绪后仍然有效，本块不重复。
- **并行安全**：本块未碰 `tools/service_watchdog.py`、未杀/重启任何进程、未 `schtasks /change` 他人任务、未改 `app/` 全目录。新建任务名唯一 `TianjiKeyPack_Daily`。

---

## §1 三档资产与重建成本表（本块最有价值的产出）

### 档 1：不可重建（丢了=永久损失，必须进 keypack）

| 资产 | 字节 | 重建途径 | 预估耗时 | 依赖是否还在 | 丢了会瞎掉哪些功能 |
|---|---|---|---|---|---|
| `data/account.json` | 10KB | **不可重建**（模拟盘唯一账本） | — | — | 持仓/现金/成交历史全丢，模拟盘重置 |
| `data/bt_*.json`（~50 个规则卡） | ~1MB | **不可重建**（回测结论沉淀） | — | — | 所有策略推荐卡丢失，需重跑全部回测 |
| `data/experiments_index.json` | 94KB | **不可重建**（实验台账索引） | — | — | 161+ 实验记录不可检索 |
| `docs/reports/`（147 份） | 1.2MB | **不可重建**（验收/诊断/研究全档） | — | — | 全部历史结论丢失，无法追溯 |
| `data/audit/audit.jsonl` | 875KB | **不可重建**（哈希链式 append-only） | — | — | 审计链断，无法验证账户/操作真实性 |
| `data/ml_scores_ledger.jsonl` | 10KB | **不可重建**（ML 台账） | — | — | 54 行 pick/ret 记录丢失 |
| `data/qfq_factor_sample.json` | 6.3MB | 可重建但需出网（akshare） | ~30min | akshare 可用 | qfq 污染治理样本丢失 |
| `data/bt_snapshot_baseline.json` | 3.2MB | 可重建但需重跑回测 | ~1h | 快照库在 | 回测基线丢失 |

### 档 2：可重建但昂贵（丢了=花时间/出网，不进 keypack 因体积）

| 资产 | 字节 | 重建途径 | 预估耗时 | 依赖是否还在 | 丢了会瞎掉哪些功能 |
|---|---|---|---|---|---|
| `data/market.db`（kline 段） | 1458.8MB / 8,848,946 行 | `update_daily(stale_first=True)` 逐日回补 + `tools/backfill_daily.py` | **全速 1-2h / 按每日 480s 预算约 1 周**（E1 实测吞吐：stale_first 300-858 票/480s；backfill_daily 51min/6线程全量补数） | 腾讯/新浪行情接口可用（8/21-28 曾撞限流三连败） | 选股/回测/板块强度全部无历史，扫描吃多旧 bar |
| `data/min5.db` | 724.6MB / 5,469,424 行 | 分钟线回补（无专用工具，需走 datafeed 逐票拉） | **估 2-4h**（未实测） | 腾讯/新浪分钟接口可用 | 分钟级策略/竞价分析不可用 |
| `data/cyq_cache`（2113 票） | 142.3MB | `tools/cyq_build.py` 全量重建 | **668.8s ≈ 11min**（cyq_rebuild.md 实测：2423 票 3.62 票/s，0 失败；2113 票成功 / 310 票退市无数据） | 新浪换手率接口可用（东财 push2 系在本环境被阻断） | 筹码峰策略/回测不可用 |
| `tmp/v1/`（事件缓存） | 898.3MB | 从 market.db 重新生成事件（`tools/tactic_lianban_backtest.py` 等） | **估 20-40min**（未实测） | market.db 在 | 连板/首板回测需重新生成缓存 |
| `data/snapshots/`（9 份） | 11.9GB | 收盘后 auto_snapshot 自动生成（依赖收盘更新成功） | **9 个交易日**（每日 1 份） | updater 收盘更新成功（8/28 后曾断更） | 钉快照回测全部失效，回测结果不可复现 |

### 档 3：可廉价重取（丢了=几分钟恢复，上游可能死）

| 资产 | 字节 | 重建途径 | 预估耗时 | 依赖是否还在 | 丢了会瞎掉哪些功能 |
|---|---|---|---|---|---|
| `data/stock_list.json` | 160KB | `fetch_all_stocks()` 腾讯/新浪 | <1min | 接口可用 | 股票全集丢失，扫描范围为空 |
| `data/sector_map.json` | 80KB / 49 行业 2911 票 | 东财行业分类接口 | <5min | 东财接口可用（曾断更） | 板块强度/轮动策略不可用 |
| `data/listing_dates.json` | 110KB | 新浪/东财上市日期 | <5min | 接口可用 | 次新股判定失真 |
| `data/delisted_universe.json` | 649KB | 退市股清单（akshare） | ~10min | akshare 可用 | 幸存者偏差校正丢失 |
| `data/qg_zt_full`（market.db 表） | 1852 行 | 钱龙涨停接口 | — | **已断更在 2026-08-21**（上游会死的先例） | 涨停全量池停更 |
| `data/global_kline`（market.db 表） | 34,636 行 | 全球行情接口 | ~10min | 接口可用 | 全球对比/外围影响分析 |

**重建成本总览**：不可重建档合计 ~12MB（进 keypack）；可重建但昂贵档合计 ~15GB（不进 keypack，重建 1 周）；可廉价重取档合计 ~1MB（几分钟恢复，但上游会死）。

---

## §2 实现（文件:行）

### `tools/backup_key_pack.py`（新建，265 行）

| 函数 | 行 | 功能 |
|---|---|---|
| `sha256_file()` | L36 | 分块读文件算 SHA256（1MB 块，防大内存） |
| `count_lines()` | L44 | 二进制安全行数摘要（按 \n 计数） |
| `collect_members()` | L54 | 收集 FIXED_MEMBERS（26 个固定文件）+ GLOB_MEMBERS（`data/bt_*.json` + `docs/reports/*.md`），去重 |
| `account_summary()` | L77 | account.json 额外摘要：cash / trades_count / positions_count |
| `build_manifest()` | L92 | 构建 manifest.json：每个成员 path/bytes/sha256/lines + account_summary |
| `write_sha256sums()` | L119 | 写 SHA256SUMS.txt（兼容 `sha256sum -c` 格式） |
| `pack()` | L127 | 主流程：staging 拷贝 → 写 manifest+SHA256SUMS → tar.gz → 自校验 → 清理 |
| `verify_archive()` | L168 | 解包到临时目录 → 逐文件比 SHA256+size → 失败列详情 |
| `cleanup()` | L207 | 保留最近 N 份（按文件名时间戳排序），超出删最旧（复用 backup_data.py 语义） |
| `main()` | L218 | argparse：`--verify <归档>` / `--keep N`（默认 10） |

**关键设计**：
- staging 先拷贝到临时目录再打包（避免源文件在打包时被修改导致撕裂）
- tar.gz 打包用 `tar.add(staging, arcname='.')`，解包用 `filter='data'`（Python 3.12+ 安全特性，拒绝绝对路径/.. 路径）
- 自校验失败即非 0 退出并保留失败件（不自动删）
- 禁固定文件名覆盖写（每份独立时间戳命名）
- **不含任何 .db / snapshots / cyq_cache**（红线：不得把 12GB 行情库塞进 keypack）

### `data/keypack/`（新建目录，人类已批准）

- 首份归档：`keypack_20260905_225256.tar.gz`（手动打包）
- 第二份归档：`keypack_20260905_225331.tar.gz`（任务触发，证明调度可用）
- 每份内含：`manifest.json` + `SHA256SUMS.txt` + 221 个成员文件

### `TianjiKeyPack_Daily`（新建计划任务）

| 字段 | 值 |
|---|---|
| 触发 | 每日 23:40（在 23:50 git nightly 之前） |
| 动作 | `C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe tools\backup_key_pack.py` |
| 工作目录 | `C:\Users\26838\A股模拟盘` |
| StartWhenAvailable | True |
| DisallowStartIfOnBatteries | False |
| ExecutionTimeLimit | PT10M |
| MultipleInstances | IgnoreNew |
| 注册前 XML 留档 | 无同名旧任务（fresh register） |

---

## §3 五项自检门输出

### 门 1：py_compile + LF

```
py -3.13 -m py_compile tools/backup_key_pack.py  →  OK（无输出）
CRLF=0（LF 行尾，项目约定）
```

### 门 2：真实打包 + 自校验 + 独立 --verify

```
=== 首次真实打包 ===
packed: data\keypack\keypack_20260905_225256.tar.gz (1983.1 KB, 221 members)
VERIFY OK: 221 members, all SHA256/size match (manifest generated_at=2026-09-05 22:52:56)

=== 独立 --verify ===
archive: data\keypack\keypack_20260905_225256.tar.gz
VERIFY OK: 221 members, all SHA256/size match (manifest generated_at=2026-09-05 22:52:56)
```

**manifest.json 摘要**（第二份，任务触发生成）：
- generated_at: 2026-09-05 22:53:31
- member_count: 221
- total_bytes: 15,112,963（14,758.8 KB 原始 / 压缩后 1,983.1 KB）
- account_summary: cash=47455.10, trades_count=26, positions_count=2
- 分类：data 73 files 13.2MB / docs 148 files 1.2MB
- 前 5 大成员：qfq_factor_sample.json 6.3MB / bt_snapshot_baseline.json 3.2MB / audit.jsonl 0.9MB / qfq_triage.json 0.8MB / delisted_universe.json 0.6MB

### 门 3：篡改测试（证明不是橡皮图章）

```
=== 篡改测试 ===
source: keypack_20260905_225153.tar.gz
tamper target: data\account.json（改第一个字节）
VERIFY FAILED (1 failures):
  data/account.json: SHA256 mismatch (expected cfd7493253bf8339, got 2dbdf7a9650110c7)
verify result (expect False): False
PASS: tampered archive correctly rejected
```

### 门 4：清理测试（12 份 → 保留 10 份）

```
=== 清理测试 ===
existing archives: 1
created 12 fake archives
pruned: keypack_20260905_225153.tar.gz（最旧真实归档，被假归档挤出）
pruned: keypack_FAKE_1788619866.tar.gz
pruned: keypack_FAKE_1788619867.tar.gz
remaining fake archives: 10
PASS: cleanup correctly kept 10 of 12, pruned 2 oldest
```

⚠ 清理测试副作用：把真实归档 keypack_20260905_225153.tar.gz 也删了（它比假归档旧）。已通过后续重新打包（225256 + 225331）补回，当前 keypack 目录有 2 份真实归档。

### 门 5：体积门 + 任务触发证据

**体积门**：最终归档 `keypack_20260905_225331.tar.gz` = **1,983.1 KB = 1.94 MB**，远 <50MB 门槛。原始成员 14.4MB（压缩率 13.5%，JSON/MD 文本压缩效果好）。无需分卷。

**任务触发证据**：
```
before: 1 archives
after: 2 archives（新增 keypack_20260905_225331.tar.gz）
LastRunTime: 2026/9/5 22:53:30
LastTaskResult: 0（成功）
Next Run Time: 2026/9/5 23:40:00
```

---

## §4 手册要点摘录（`docs/reports/k1b_restore_runbook.md`）

### 场景 A：account.json 被写坏/误删（约 11 分钟 + 人工批准）

1. **A1 确认损坏**：`json.load` 测试 + cash/trades/positions 数值检查
2. **A2 从 keypack 还原到 tmp/restore_test/**：解包 + `--verify` 校验
3. **A3 与 git nightly 交叉核对**：`git log -p data/account.json` 找最后正常版本，对比 cash/trades/positions
4. **A4 对账判据**（必须通过）：cash>=0 / 所有 trades qty&price>0 / 所有 positions qty>0 / 最后一笔交易时间与审计链当日事件可对应
5. **A5 人工批准后覆盖生产**：先备份损坏件 → 覆盖 → 验证

### 场景 B：market.db 日K段损坏/快照全丢（约 10 分钟 + 回补 3-5 交易日~1 周）

1. **B1 确认损坏**：`PRAGMA integrity_check` + kline 行数/max_date + 快照目录检查
2. **B2 最小可跑通路径**：备份损坏件 → 建空库（只建表结构）→ `update_daily(stale_first=True)` 触发回补（走 updater 路径，禁手工 INSERT）
3. **B3 回补耗时**：stale_first 当日覆盖 3-5 个交易日（300-858 票/480s）；全量历史 1 周（按每日预算）/ 1-2h（全速 backfill）
4. **B4 期间影响**：扫描吃多旧 bar / 板块红条持续亮（coverage<0.8 CRITICAL）/ 回测不可用（无快照）/ ML 预测不可用
5. **B5 验证进度**：每日查 `latest date` + `covered_codes/2526 ratio`

### 场景 C：整盘失效（当前无解，需外接介质）

- **结论**：同盘 keypack 在盘坏时与源同灭，无法恢复
- **外接后第一步**（引用 K1 §2）：`py -3.13 tools\backup_data.py --dest D:\tianji_backup`（SQLite online backup API，禁 copy 正在写的 .db）
- **恢复演练**（介质就绪后必须做）：RESTORE 到 tmp/ → 只读比对 kline 行数/distinct code/最近 5 交易日覆盖/随机 20 票×5 日收盘价
- **外接方案建议**：最小 1TB USB SSD（¥400-600）/ 推荐 2TB NVMe 移动硬盘 + 每日自动 / 进阶 NAS + 异地

---

## §5 风险登记追加原文（`docs/reports/issue_ledger.md` §6，仅末尾追加）

> ## §6 风险登记：行情库零副本（单盘）
>
> - **登记日期**：2026-09-05（K1b 块）
> - **风险等级**：P1（高）—— 盘故障 = 行情数据全灭，重建成本以周计
> - **影响面**：market.db 1458.8MB（kline 8,848,946 行 + ml_pred 1,411,194 行）/ min5.db 724.6MB（5,469,424 行）/ snapshots 9 份 11.9GB（同盘非副本）/ cyq_cache 142.3MB 2113 票
> - **当前缓解**：keypack 极小关键包（每日 23:40，保留 10 份，只防误删/写坏，不含 .db）+ git nightly（兜代码+JSON，.gitignore 排除行情库）+ backup_data.py 已存在但无任务/无介质
> - **未覆盖**：盘坏/NVMe 掉电磨损/控制器故障（同盘同灭）/ 整盘失效（无解）/ 恢复演练（未做）
> - **重建成本**：stale_first 当日覆盖 3-5 交易日 / 全量历史 1 周（按每日预算）/ min5 估 2-4h
> - **一句话结论**：**接一块外接 SSD 即可让 K1 的异盘副本 + 恢复演练一次做完**（首次备份约 30 分钟，恢复演练约 1 小时）
> - **决策请求**：(a) 挂接外接盘后继续 K1；(b) 接受单盘现状依赖 keypack；(c) 其他方向

追加后 issue_ledger.md 从 118 行增至 ~160 行，CRLF=0（LF 行尾）。既有 §1-§5 未删改。

---

## §6 本块仍未解决什么（诚实声明）

1. **盘坏 = 无解**：同盘 keypack 在 NVMe 控制器故障/掉电磨损/整盘失效时与源同灭，无法恢复。这是物理介质限制，不是代码能解决的。**必须外接第二介质才能消除此风险。**
2. **恢复演练未做**：无副本可演练（K1 停手）。keypack 的还原流程在手册里写了，但未真实执行过"从 keypack 还原 account.json 并验证服务可启动"的端到端演练。建议在下次维护窗口做一次。
3. **min5.db 重建耗时未实测**：估 2-4 小时，无专用回补工具。若 min5.db 损坏，重建路径需临时开发。
4. **上游会死**：qg_zt_full 已断更在 2026-08-21（先例）。sector_map/stock_list 等"可廉价重取"档依赖上游接口可用性，若上游永久下线则升格为"不可重建"。
5. **keypack 不含代码**：代码由 git nightly 兜住（23:50，在 keypack 之后 10 分钟）。若 git nightly 失败，keypack 不含最新代码。建议 keypack 与 git nightly 的时间间隔（10 分钟）足够小，风险可接受。
6. **同盘备份的物理限制**：keypack 与源在同一块 NVMe 上，若该盘有坏块，keypack 也可能受影响。虽然 tar.gz + SHA256 可检测损坏，但无法恢复。

---

## §7 遗留

1. **外接介质决策**：待人类批准后执行 K1 异盘备份 + 恢复演练（issue_ledger §6 决策请求）。
2. **keypack 还原端到端演练**：建议下次维护窗口执行"从 keypack 还原 account.json → 验证服务启动 → 验证持仓/现金自洽"。
3. **min5.db 回补工具**：当前无专用工具，若 min5.db 损坏需临时开发。建议排期开发 `tools/backfill_min5.py`。
4. **陈旧备份清理**：`data/market.db.bak`（63MB，8/18）+ `data/market.db.bak.20260819_011241`（63.4MB，8/19）合计 126MB，同盘无副本价值。K1 §6 已登记待批准，本块未删除（物理删除需人类批准）。
5. **keypack 成员清单评审**：当前 FIXED_MEMBERS 含 26 个文件 + 2 个通配。建议人类评审是否需要增减（如 `data/qfq_factor_sample.json` 6.3MB 是否值得进 keypack，还是可廉价重取）。

---

## 交付物清单

| 产物 | 路径 | 大小 | 说明 |
|---|---|---|---|
| 打包工具 | `tools/backup_key_pack.py` | 8.2KB / 265 行 | 新建，py_compile OK，LF=0 |
| 关键包目录 | `data/keypack/` | 2 份归档 / 3.9MB | 人类已批准新建，每日 23:40 自动 |
| 归档 1 | `data/keypack/keypack_20260905_225256.tar.gz` | 1.94MB | 手动打包，221 members |
| 归档 2 | `data/keypack/keypack_20260905_225331.tar.gz` | 1.94MB | 任务触发，证明调度可用 |
| 重建手册 | `docs/reports/k1b_restore_runbook.md` | 7.4KB | 场景 A/B/C 可照抄执行 |
| 交付报告 | `docs/reports/k1b_offline_pack_and_risk.md` | 本文件 | — |
| 风险登记 | `docs/reports/issue_ledger.md` §6 | 追加 ~42 行 | 仅末尾追加，既有 §1-§5 未删改 |
| 计划任务 | `TianjiKeyPack_Daily` | — | 每日 23:40，5 项设置全符合 |
| 自检脚本 | `tmp/pack20/k1b_selftest.py` | 3.5KB | 篡改测试 + 清理测试 |
| 探针脚本 | `tmp/pack20/k1b_probe.py` / `k1b_search_e1.py` | — | 资产探查 + E1 吞吐搜索 |
| 查重日志 | `tmp/pack20_dup.log` | 追加 1 行 | K1b GATE-OK 2026-09-05 22:46:51 |

**红线遵守自查**：
- ✅ 未把 12GB 行情库塞进 keypack（keypack 仅 1.94MB，不含任何 .db/snapshots/cyq_cache）
- ✅ 诚实标注"同盘只防误删与写坏、不防盘坏"（报告 §0/§6/手册场景 C 多处声明）
- ✅ 生产库一律只读 URI（`file:...?mode=ro&immutable=1`，查询限时 <1s）
- ✅ 未碰 `tools/service_watchdog.py`
- ✅ 未任何进程操作（未杀/未重启）
- ✅ 未改 `app/` 全目录
- ✅ `issue_ledger.md` 仅末尾追加（既有 §1-§5 未删改，CRLF=0）
- ✅ 未物理删除任何既有文件（陈旧 .bak 仅登记未删）
- ✅ 写入面不超白名单
