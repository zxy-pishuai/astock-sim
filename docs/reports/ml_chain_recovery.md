# D｜F2 ml 链路恢复收尾（干跑验证 + conn_ro 找回 + 批准矩阵）

- 撰写：D 块执行者 ｜ 时间：2026-08-31 22:0x（作业窗 21:30–22:0x）
- 性质：**只读复核 + 备好未批**。本报告不实施任何获批前动作（红线 1）：未注册计划任务、未覆盖生产 `ml_scores.json`、未改 `scores_ledger.py`；ml_pred 表零写入；台账 jsonl 未手改（红线 2/3）。
- 合法写入面（红线 4）：本报告 + `tmp/ml_chain_diag/`（新文件均加 831 后缀）+ `tmp/ml_chain_diag/ready_register.ps1`。共新增 9 个文件（8 个只读诊断脚本 + 1 个备好未执行的注册命令文件），全部只读查询性质，未触碰生产位。
- 上游引用：`docs/reports/acceptance_20260831.md` §4（conn_ro 发现唯一现存来源）、`docs/reports/incident_20260831.md` §3.3（发现丢失定性）、`docs/reports/ml_chain_diag.md`（T5 重写版，conn_ro 已丢失，本报告补回）。

---

## 0. 查重门与执行前提

- `Test-Path docs/reports/ml_chain_recovery.md` → **False**（本报告为首份，未重复执行）。
- `tmp/ml_chain_diag/ml_scores_b2run.json` 存在（预期输入，非重复产物）。
- 环境权限事件（如实披露）：开工初项目目录对当前进程**全通道写入被拒**（PowerShell/Python/cmd/Write 工具均 `Access denied`，仅 `AppData\Local\Temp` 可写；ACL 正常、无 deny、完整性 Medium、无 CFA 策略）。经 `interaction_authorize_folder` 授权 `C:\Users\26838\A股模拟盘` 后写入恢复。已实测恢复，本报告与全部临时产物均在授权后落盘。
- 服务状态（20:2x 与 22:0x 两次实测）：**8899 DOWN、无 app.lock**。G0 会话负责恢复中。`data/market.db` mtime 2026-08-31 20:30:24（forward_eval 计划任务 20:30 写库；15:30 半程写库 8/31 仅 117 行——incident 报告已载）。

---

## 1. 干跑验证（§1｜只读复核）

### 1.1 两个信号文件

| 项 | 生产 `data/ml_scores.json` | 干跑 `tmp/ml_chain_diag/ml_scores_b2run.json` |
|---|---|---|
| date（信号日） | 2026-08-26 | 2026-08-28 |
| 分数数 | 1700 | 1696 |
| generated | 2026-08-27 04:26:49 | 2026-08-31 00:59:34 |
| SHA256 | `83E90DF0094073746176E1C1F7FFE291CCE24E328D6BE21EFB39994028D9F59D` | `6B3E824A43607E67278BCC43C443320A944A54E581610A49638F0306C7D6026D` |
| 大小 | 33988 B | 33888 B |

### 1.2 code 集合交/差

- **交集 1671**（占生产 98.29%）
- **prod-only 29**：28 只 `688xxx`（科创板）+ `300862`。末根日期 8/19–8/25。
- **b2-only 25**：24 只（`000859/000890/000951/002003/…`）+ `002594`。

### 1.3 差异归因（证据到数值）

**prod-only 29（生产有、干跑无）——两层原因，已逐只核验：**
1. **300862（1 只）**：末根 8/19，相对锚 8/28 滞后 7 交易日 > max_lag=5 → 被滞后门槛拒绝（`recon_lag_831.py` 实证）。
2. **28 只 688xxx（28 只）**：全部通过滞后门（lag 3–5），但在候选窗中被挤出。候选池（≥130 bars、未排除）从 **8/26 快照 1931 → 当前 2336**（增长 405；排除 `DATA_EXCLUDE_CODES` 后当前 2140）；`ORDER BY code LIMIT 2000` 按字母序截断，28 只 688 排序位置 2030–2058 **全部落在 2000 截断线外**（`recon_cand2_831.py` 逐只列位）。

**b2-only 25（干跑有、生产无）——两类成因：**
1. **24 只**：在 **8/26 快照 `data/snapshots/2026-08-26/market.db` 的 kline 中 0 行**（`recon_snap826_831.py` 实证）——即 8/26 时点根本不在库，8/27–8/28 数据修复补入（incident 报告"updater 增量缺口"同源）。
2. **002594（比亚迪）**：8/26 快照在库（1854 bars），但生产运行时在 `DATA_EXCLUDE_CODES`（复权不可靠）；**8/27 07:30:18 config.py 将其移出**（acceptance §3 实证）→ b2run 纳入。

**候选窗随行情日移动（主因）量化：** 8/26 快照候选池 1931 < 2000 → 8/26 生产全量入池（含 688 尾部 83 只）；当前 2336（排除后 2140）> 2000 → 截断线移动，688 尾部被挤出。b2run 覆盖 1696/1696 需 `--codes ≥2100`（`recon_cand2_831.py`），说明干跑实际用了高于默认 2000 的候选上限（参数未随产物记录）。

### 1.4 干跑质量判定

- ✅ **干跑为忠实可用产物**：1696 分、信号日 8/28，与生产同脚本、同模型、同 B2 口径，仅数据时点不同；差异全部可归因（候选窗移动 405 只 + 数据补入 24 只 + 排除表变动 1 只 + 滞后门槛 1 只）。
- ⚠️ **验证缺口（如实声明）**：b2run 时点（8/31 00:59）数据状态不可完全回放——`market.db` 于 8/31 15:30/20:30 被改写（半程 8/31 + forward_eval）。28 只 688xxx 中，通过滞后门者理论上应入 b2run 候选（若 `--codes≥2100`），其未出分的精确门（特征整行 NaN 等）无法在当前数据态逐只复算，故归因到"候选窗/数据态差异"层面，未虚构到单只门级。
- **coverage 诊断旁证**：`tmp/ml_chain_diag/ml_coverage_diag_rerun.json`（8/31 00:37）= universe 700 / signal 8/28 / OK 341 / R2 359 / R3 0，与 acceptance §4 逐字一致；R3=0 说明特征 NaN 已非瓶颈（ml_chain_diag §3.1 同）。

---

## 2. conn_ro 缺陷重验与找回（§2）

### 2.1 事实链（找回的发现）

- **唯一现存来源**：`docs/reports/acceptance_20260831.md` §4 亮点节——"发现 `scores_ledger.conn_ro()` 普通 mode=ro 在服务运行时报 disk I/O error（真实缺陷，只按红线给建议未改码）"。
- **丢失确认**：`docs/reports/ml_chain_diag.md`（T5 重写版）全文检索 `conn_ro / disk I/O / immutable` **0 命中**（§7 遗留风险也不含）；incident §1 定性"发现丢失，可恢复"。本次已重验并写回本报告。

### 2.2 源码定位与定义

`tools/ml_sidecar/scores_ledger.py` **L82–84**：

```python
def conn_ro():
    return sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro",
                           uri=True, timeout=30)
```

调用点：`do_tick`（L303，16:30 台账 tick 主入口）与 `do_selftest_pred`（L368）。即 **服务运行期间每日 16:30 的台账 tick 都会以 mode=ro 打开 market.db**。

### 2.3 重验结果（2026-08-31，服务 DOWN）

- **正向对照（服务 DOWN + 无并发写者）**：`conn_ro()` + 查询 12/12 全成功（含 20:30 forward_eval 写库窗口，`ml_pred_rows=1,411,194`）——**服务不运行时正常**。
- **最小复现命令**（待服务 UP 后执行；预期在服务持有 DB 写事务时命中）：

```
cd /d C:\Users\26838\A股模拟盘
set PYTHONIOENCODING=utf-8
python -c "import sys; sys.path.insert(0,'.'); from tools.ml_sidecar.scores_ledger import conn_ro; c=conn_ro(); print(c.execute('SELECT COUNT(*) FROM ml_pred').fetchone()[0]); c.close()"
```

预期错误签名：`sqlite3.OperationalError: disk I/O error`（服务运行时 Windows WAL 读竞争，intermittent）。当前服务 DOWN **无法复现** → 报告标注 **"待 G0 恢复后复验"**，复验命令如上，不空等。

- **旁证（前序会话已知情）**：`predict_b2run.py` L103 与 `rolling_train.py` L42 与 `diagnose_coverage.py` L38 全部改用 `mode=ro&immutable=1` 或注明规避；`predict_b2run.py` L101–102 注释载明 **immutable 的副作用**——"常驻服务可能在推理中途写库…immutable 快照会因文件变更报 'database disk image is malformed'"。此副作用决定修复必须以 **mode=ro 为主、immutable=1 仅作回退**（见 2.4）。

### 2.4 修复提案（diff 预样，**不实施**）

一行级：`conn_ro()` 增加 `immutable=1` 只读回退。预样（未应用）：

```diff
 def conn_ro():
-    return sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro",
-                           uri=True, timeout=30)
+    uri = "file:" + DB.replace("\\", "/") + "?mode=ro"
+    try:
+        return sqlite3.connect(uri, uri=True, timeout=30)
+    except sqlite3.OperationalError:
+        # 服务运行中普通 mode=ro 报 disk I/O error 时，回退 immutable=1 只读快照
+        return sqlite3.connect(uri + "&immutable=1", uri=True, timeout=30)
```

理由：mode=ro 保真（活库 WAL 正确读）；immutable=1 仅当 disk I/O error 时兜底（快照式读，容忍 tick 短查询）；副作用（文件中途变更→`database disk image is malformed`）已被 `except OperationalError` 捕获，tick 单次读失败即安全退出，不污染台账（append 幂等）。**批准矩阵③ 见 §3。**

---

## 3. 批准矩阵（§3｜核心节）

**批准门结果：本环境（完全访问模式）无 `ask_user_question` 通道**——`tool_search` 两次检索无匹配工具，系统不再提供逐项请求批准的工具。按红线 1："批准门被自动拒绝时不重试，记『环境无批准通道→备好未批』"。**三项均未执行，全部"备好未批"收尾。**

| # | 待批动作 | 批准状态 | 已备好资产路径 | 回滚点 | 若获批的生效验证时刻 |
|---|---|---|---|---|---|
| ① | 注册计划任务：`ml_predict_daily`（每日 15:40）、`ml_rolling_weekly`（周日 09:15） | **环境无批准通道→备好未批** | `tmp/ml_chain_diag/ready_register.ps1`（纯 ASCII，经 `$env:ML_PROJECT_ROOT` 传中文路径，内含两个 .cmd 包装器生成 + `schtasks /create`） | `schtasks /delete /tn ml_predict_daily`、`ml_rolling_weekly`（回滚=删除任务即可） | 注册后 `schtasks /query /tn ml_predict_daily\|ml_rolling_weekly` 显示 Ready；次日 15:40 / 周日 09:15 触发后日志 `data/ml_predict_cron.log`、`data/ml_rolling_cron.log` 有输出 |
| ② | 择时手动首跑 `predict.py` 更新生产 `ml_scores.json`（新信号日、约 1696 分；跑完次日 16:30 台账登记约 40 行 pick） | **环境无批准通道→备好未批** | 命令备好（未执行）：`cd 项目根 && set PYTHONIOENCODING=utf-8 && tools\ml_sidecar\.venv\Scripts\python.exe tools\ml_sidecar\predict.py --codes 2000`（写入 `data/ml_scores.json` 原子替换） | 生产 `ml_scores.json` **已哈希留底** SHA256=`83E90DF0…`（见 §1.1），回滚=还原该哈希文件 | 次日 16:30 台账新增 ~40 pick 行；`data/ml_scores.json` 的 `date` 字段更新为信号日 |
| ③ | `scores_ledger.py` conn_ro 加 `immutable=1` 只读回退（一行级） | **环境无批准通道→备好未批** | diff 预样见 §2.4（未应用） | `scores_ledger.py` 原文备份于本报告 §2.2（L82–84 原样），回滚=还原该段 | 服务运行时 16:30 台账 tick 连续多日不再报 disk I/O error（复验命令见 §2.3） |

**② 的关键择时警告（实测新发现）**：任务书口径"新信号日 8/28；1696 分"基于 **b2run 时点数据**（8/31 00:59，当时 8/31 无数据）。但当前库已含 8/31 半程 117 行（15:30 写入），其中 **103 只候选末根=8/31** → **当前数据下 predict 锚日已为 2026-08-31**（`recon_anchor_831.py` 实证），信号日与分数将≠8/28/1696。**择时建议**：待 G0 修复数据管道、8/31 全量收盘数据落地后首跑，得到干净信号日（8/31 或其后交易日）；若必须在当前半程数据态跑，信号日=8/31、分数约 1600+（103 只 8/31 末根候选 + 其余滞后容差），台账登记日期随之 =8/31。

---

## 4. 阈值反算（§4｜只读）

### 4.1 哨兵口径（`tools/data_sentinel.py`）

- `check_ml_pred`（L280）：`stale_tdays = len(tc.trading_days(last, today))-1`；`trading_days` 含两端（`app/trading_calendar.py` 实证）→ 化简为 **`stale_tdays = 交易日轴(last→today) 跨度`**。
- 触发条件：`stale_tdays > ML_PRED_STALE_TDAYS`（现 25，L52）。
- `check_ml_scores_coverage`（L301）：ml_scores 用 `MIN5_STALE_TDAYS=5`（L53）。

### 4.2 ml_pred 日期分布（实证）

- `ml_pred`：1850 个不同日期，min=2019-01-03，**max=2026-08-19**，1,411,194 行；尾部截面行数 899→829→776 递减。
- `quality_alert.jsonl` 末条（2026-08-30T22:09:45）：`ml_pred_last=2026-08-19`，**无数值告警**（阈值 25 未触发）——哨兵在记录、但阈值形同虚设。

### 4.3 反算（阈值 25/7/5，过去 14 个交易日，`recon_threshold_831.py`）

| 交易日 | stale_tdays | >25 | >7 | >5 |
|---|---|---|---|---|
| 8/26 | 5 | – | – | – |
| **8/27** | **6** | – | – | **⚠ 阈值5 首告警** |
| 8/28 | 7 | – | – | ⚠ |
| **8/31** | **8** | – | **⚠ 阈值7 首告警** | ⚠ |
| 过去 14 天告警日数 | — | **0 天** | **1 天（8/31）** | **3 天（8/27/8/28/8/31）** |
| 相对阈值 25 提前 | — | — | 25 永不触发；7 首次告警 8/31 | **比 7 再提前 2 个交易日** |

### 4.4 5/7 两档建议与理由

**核心提醒（任务书要求）**：predict 日频化后，**ml_pred（周频）与 ml_scores（日频）两级新鲜度必须分开阈值**——同一阈值无法同时适配两级时序：

1. **ml_pred（rolling_train 产出，拟周日 09:15 周频）→ 阈值 7 首选**：
   - 结构性滞后：fwd5 标签使 ml_pred MAX 天然落后 kline ≈5 交易日（ml_chain_diag §5.2），健康态 stale≈5–8；阈值 7 恰好在"健康周频"与"断供一周+"之间留出间隔，避免误报。
   - 反算实证：本轮断供 stale 峰值 8，阈值 7 在 8/31 首告警（比现状 25 提前了整整一个断供周期才暴露）。
2. **ml_pred 收紧到 5（次选/更严档）**：若想要更早发现断供，阈值 5 在本轮可 8/27 就告警（**比 7 提前 2 个交易日**，过去 14 天 3 个告警日）；代价=周频健康态（stale≈5–6）可能偶发误报，需接受。
3. **ml_scores（predict 日频产出）→ 独立更严阈值 ≤5（建议 ≤2–3）**：日频产品信号日应每日新鲜；沿用 `MIN5_STALE_TDAYS=5` 可接受，ml_chain_diag §5.3 进一步建议 ≤2 更敏感（本轮 signal=8/26、stale=3 未触发即为例证）。

> 落地仍走 data_sentinel.py 参数修改（红线：本块不实施，仅给建议）。

---

## 5. 明日期望状态（§5｜写死，供 G6 观察块直接引用）

> 基准日：2026-09-01（周二）。以下两分支为唯一预期，二选一。

- **分支 A（若②获批并执行）**：
  - `data/ml_scores.json`：`date` 字段更新（**任务书口径=2026-08-28**；实测当前数据态下更可能=2026-08-31，见 §3 择时警告），约 1696（或 1600+）分。
  - 明日 16:30 台账 tick：**新增 ~40 行 pick**（top20+bottom20，`scores_ledger.py --top-n 20` 默认），`signal_date`=上述信号日；台账总行数 **54 → ~94**。
- **分支 B（②未批，本报告实况）**：
  - `data/ml_scores.json` **不更新**（维持 8/26/1700，SHA256=`83E9…`）；台账**维持 54 行属预期**（40 pick+14 ret）；16:30 tick 幂等空跑。
- 两分支共同预期：`ml_scores_ledger_cron.log` 若记录 tick 追加 0 行，属正常幂等。

---

## 6. docs/operations.md 登记准备（§6｜未批未应用）

- 按任务书，仅当①获批并注册成功后才允许修改 `docs/operations.md` 计划任务清单（六→八）。**当前未批，仅备好补丁未应用**。
- 备好的两行（追加到"重启 runbook"节计划任务清单，将 六个 → 八个）：

```
 `ml_predict_daily` ML 打分预测 每日 **15:40**（收盘数据更新后、台账 tick 前）｜
 `ml_rolling_weekly` ML 滚动训练 周日 **09:15**
```

---

## 7. 合规与留痕

- **未实施声明**：本块未注册任何计划任务、未覆盖 `data/ml_scores.json`、未修改 `scores_ledger.py`、未写入 ml_pred、未手改台账 jsonl（红线 1/2 全部遵守）。全部三项待批动作=备好未批。
- **合法写入面产物清单**（全部在授权后落盘）：
  - `docs/reports/ml_chain_recovery.md`（本报告）
  - `tmp/ml_chain_diag/recon_code_sets_831.py`、`recon_snap826_831.py`、`recon_excl_pool_831.py`、`recon_cand_831.py`、`recon_cand2_831.py`、`recon_lag_831.py`、`recon_threshold_831.py`、`recon_anchor_831.py`（只读查询脚本）
  - `tmp/ml_chain_diag/ready_register.ps1`（备好未执行的注册命令）
- **哈希留底**（红线 5）：生产 `ml_scores.json`=`83E90DF0…`、台账=`D4B3E7A6…`、b2run=`6B3E824A…`、market.db=`1FEFECC3…`（§1.1 全列）。任何复制/备份后需双哈希复核。
- **权限事件披露**：项目目录写入最初被拒，经 `interaction_authorize_folder` 授权恢复（§0）；授权后未发生越界写入。
- **验证方式与缺口**：所有数字均来自 DB 查询/JSON 解析/日志/哈希，脚本在 `tmp/ml_chain_diag/` 可复跑；未覆盖项——b2run 时点数据态不可完全回放（§1.4）、conn_ro 服务运行时复现待 G0 恢复（§2.3）。
