# 双 main.py 实例处置报告（X1｜P0）

- 报告日期：2026-09-01 10:40（处置完成时）
- 处置人：X1 并发执行者（全项目唯一有权动进程）
- 涉事进程：44104（python.exe `main.py --no-window`，00:48 起，持 8899）/ 53208（pythonw.exe `main.py`，09:39:34 起，持错位 app.lock）
- 处置方案：**方案 a（默认）——杀 53208，保留 44104**
- 状态：**已处置完成，三方一致已恢复**；14:55 前哨检查已排程（见 §7）

---

## 0. 查重门

`Test-Path docs/reports/dual_instance_fix_20260901.md` → **False**（本报告不存在）→ 不触发 dup 门，进入执行。

进程查重 `Get-CimInstance Win32_Process` 过滤 main.py → **双活确认**：44104 + 53208 同时存活 → 执行处置流程。

---

## 1. 取证包（§1 全部事实，命令输出存 `tmp/x1/`）

### 1.1 双实例资源与归属

| 项 | 44104 | 53208 |
|---|---|---|
| 可执行名 | python.exe | pythonw.exe |
| 命令行 | `main.py --no-window` | `"C:\Users\26838\A股模拟盘\main.py"` |
| 创建时间 | 2026-09-01 00:48:51 | 2026-09-01 09:39:34 |
| 父进程 | powershell.exe (PID 8724)（agent 基础设施） | **explorer.exe (PID 13612)（桌面）** |
| CPU 累计 / 内存 / 线程 / 句柄 | 151.4s / 377.5MB / 37 / 305 | 15.3s / 181.6MB / 43 / 849 |
| 主窗口 | 无（--no-window） | **"天玑量化终端 TianjiQuant"（pywebview 窗口）** |
| 8899 监听 | **唯一持有者（127.0.0.1:8899）** | 无监听端口（仅 60124-60129→202.108.253.139:80 陈旧新浪 CloseWait） |
| app.lock | — | 内容=53208，CreationTime=09:39:35（**09:39:35 前无锁文件**） |

取证文件：`tmp/x1/procs_20260901_103411.json`、`tmp/x1/ports_20260901_103411.json`、`tmp/x1/lock_20260901_103411.txt`、`tmp/x1/disposal_20260901_103952.log`。

### 1.2 交易引擎归属证据链（核心判定）

- 审计链 `data/audit/audit.jsonl` 今日唯一 `strategy_buy` = **09:38:21 605577 龙版传媒**（qty=1900 px=11.451 fee=5.44）——时间戳在 53208 诞生（09:39:34）**之前** → 属 44104。
- **09:39 之后零 `strategy_buy` / `strategy_sell`** → 53208 从未执行交易（符合其"返回早于启动引擎"的行为）。
- heartbeat 节奏（00:48:55 → 10:32 hash 链连续无断裂）由 44104 驱动；09:39 后无 53208 指纹事件。
- **结论：交易引擎执行方 = 44104 → 默认方案（杀 53208）成立。**

### 1.3 53208 启动来源（关键新发现，更正原假设）

- 53208 命令行与桌面快捷方式 **`A股模拟盘.lnk`** 目标完全一致（`pythonw.exe "C:\Users\26838\A股模拟盘\main.py"`），父进程 = explorer.exe → **人工桌面双击/桌面上下文启动**。
- **排除 watchdog 拉起**：watchdog 的 `start_service()`（tools/service_watchdog.py L249-260）用 `python.exe Python313` 非 pythonw；且 `tmp/watchdog.log` 在 **01:25→09:45 有 ~8.3h 空档**（机器冻结期，计划任务未跑），09:45 起直接报"健康 OK"（audit_age 新鲜）——**watchdog 从未触发恢复动作**。
- 原任务书"09:39 前后 watchdog 误判恢复又拉 53208"的假设不成立，实际为手工桌面启动。

### 1.4 watchdog 复盘

- 计划任务 `TianjiService_Watchdog`：State=Ready、LastRunTime 10:25:01、LastTaskResult=0、NextRunTime 10:30:00、NumberOfMissedRuns=0。
- `tmp/h1_watchdog_state.json`：{consecutive_failures:0, last_status:"ok", last_check:"2026-09-01 10:25:02"}。
- 08-31 23:45 watchdog 恢复曾清僵尸锁（旧PID=26940 新PID=0），与今日 44104 锁缺失链条相关（详见 §1.5 缺陷①）。

### 1.5 锁机制缺陷定位（取证目标 2）

`main.py` 相关代码段（**已读码，行号引用**）：
- `_acquire_lock(port)`：**L66-114**；主流程调用：**L175-185**（`port = srv.find_free_port(args.port)` L171 → `_acquire_lock(args.port)` L175 → `_is_running(args.port)` L188 → `srv.serve(...)` L199）。
- 逻辑：**无锁文件 → 直接 `open("w")` 写 PID，完全不查端口/存活**（本次 53208 即走此分支）；有锁文件 → 读 old_pid → alive（Windows OpenProcess 0x1000）→ healthy（裸 TCP connect 8899）→ `if alive and healthy: return False "已有实例运行中"`；否则删锁放行。

缺陷清单：

1. **弱锁非持有型**：app.lock 是纯 PID 文本文件，一次性写入、进程运行中不维护、不持有句柄 → 任何后续进程可 `open("w")` 覆盖；且 44104 在启动时写锁后不再维护，锁文件缺失无人自愈。
2. **alive 只验"PID 存活"不验身份**：不检查该 PID 是否为 main.py、是否持有目标端口 → 两个 main.py 同时存活时，新实例读到的 old_pid 若恰好是"活着但无端口"的那个（本案 53208 读 lock 时 old_pid 若为 44104 则 healthy=True 会拒绝，但实际当时**无锁文件**），存在误判空间。
3. **healthy 只做裸 TCP connect**：验证"端口通"不验证"归属者"；且 healthy 仅在 alive=True 时参与判断，alive=False 直接清锁放行，**不做端口二次核验**。
4. **"TCP 活 + lock 异主"天然检测不到**：本案终态 8899=44104 但 lock=53208，若再有新实例启动，锁逻辑读 old_pid=53208 → alive=True（当时活着）→ healthy=True（44104 在服务）→ 会误判"已有实例运行中(53208)"——**指向错误 PID**。
5. **杀锁持有者后无自愈**：44104 只在启动时写一次锁，运行中不重写；杀 53208 后锁残留 stale "53208" → 本次已手动归位（见 §3）。
6. **检查顺序错位**：`_acquire_lock`（L175）在 `_is_running`（L188）之前 → 新实例先写锁、后才发现端口已活 → 留下"新实例持锁、旧实例持端口"的错位（本案即此：53208 写锁=53208 → 见 8899 已活 → 开窗口返回，未启服务/引擎/updater）。

---

## 2. 方案选择依据（a / b）

选择 **方案 a（杀 53208）**，依据（全部来自 §1 证据）：

| 判定维度 | 结果 | 指向 |
|---|---|---|
| 交易引擎执行方 | 唯一 buy 09:38:21 在 53208 之前；09:39 后零单 | 44104 |
| 8899 端口 | 唯一 LISTEN = 44104 | 44104 |
| heartbeat 审计链 | 00:48:55→10:32 连续，44104 驱动 | 44104 |
| 53208 角色 | 无端口、无引擎、无 updater，仅 pywebview 窗口+错位锁 | 杀之无功能损失 |

**53208 不是第二交易引擎**（它在 L188 `_is_running(8899)=True` 处分之早退），故本处置的主收益=消除"锁异主"与"窗口实例持锁"的错位状态，杜绝任何残留双实例风险；同时不涉及服务重启 → **不触发任务书"标准重启命令走批准门"的场景**（该门仅适用于需重启服务的方案 b）。

---

## 3. 处置命令逐条 + 输出（方案 a）

处置时间：2026-09-01 10:34-10:40，完整日志见 `tmp/x1/disposal_20260901_103952.log`。

```
# 0) 杀前基线
Invoke-WebRequest http://127.0.0.1:8899/api/overview → HTTP 200 (time=10:34:16)
审计链末：10:32:00 heartbeat，hash 链连续（prev 链接）

# 1) 禁 watchdog（防误拉）
Disable-ScheduledTask -TaskName 'TianjiService_Watchdog' → State=Disabled

# 2) 杀 53208（默认方案 a）
Stop-Process -Id 53208 -Force → exit=True
（3s 后复核）53208 已终止

# 3) 验证 8899 仍归 44104
Get-NetTCPConnection -LocalPort 8899 -State Listen → OwningProcess=44104

# 4) 验证 /api/overview
Invoke-WebRequest http://127.0.0.1:8899/api/overview → HTTP 200 (time=10:34:58)

# 5) 观察 app.lock（读锁逻辑预期其行为）
杀后 app.lock 内容仍 = "53208"（stale，指向已杀 PID）
→ 44104 未重写锁（符合代码预期：_acquire_lock 仅在启动时调用一次，运行中不维护）

# 6) 锁归位（手动同步，恢复三方一致；44104 无自愈重写路径）
Set-Content -Path data/app.lock -Value '44104' -Encoding ASCII -NoNewline
→ 内容=44104，mtime=10:35:15

# 7) 恢复 watchdog
Enable-ScheduledTask -TaskName 'TianjiService_Watchdog' → State=Ready
（NextRunTime=10:40:00）
```

> 说明：步骤 6 为本任务范围外的**手动锁归位**（非进程处置）。理由：44104 无锁自愈路径，任务书 §3 要求"收盘前状态锁"三方一致，且 stale 锁指向已杀 PID 本身是隐患；app.lock 不在"不改 .py/config/account/审计链"红线内，操作可逆（写回 PID 文本）。已在报告明示。

---

## 4. 处置后验证

- 8899 监听：**44104**（连续两次独立复核一致）。
- `/api/overview`：HTTP 200（10:34:58、10:39 复验，指数行情正常加载）。
- **审计链心跳持续**：处置后新心跳 **10:35:00** 入链，随后 10:38:14 trading_event 3 条（旭光电子 -7.8% 等告警），hash 链连续 → 引擎 44104 处置后存活且活跃。
- 唯一 main.py 进程：`44104`（`Get-CimInstance` 复核）。
- watchdog：State=Ready，NextRun 10:40:00；状态文件 last_status=ok、consecutive_failures=0。

---

## 5. 三方一致性终态

复核命令（10:39:52，干净数值复核，已追加至 `tmp/x1/disposal_20260901_103952.log`）：

| 维度 | 值 |
|---|---|
| 8899 LISTEN OwningProcess | **44104** |
| data/app.lock 内容 | **44104** |
| 唯一 main.py 进程 | **44104** |
| 判定 | **三方一致 = OK** |

---

## 6. 复发风险评级

- **复发根因（本次）**：人工桌面双击 `A股模拟盘.lnk`（pythonw main.py），在旧实例 44104 已运行但**锁文件缺失**的时刻启动 → 锁机制缺陷①③⑥放行，形成"新实例持锁、旧实例持端口"错位。
- **复发可能性**：**中**。只要锁缺陷未修，任何人再次双击桌面快捷方式即可复现同类错位；且若 44104 未来重启而锁残留/缺失，`_acquire_lock` 的无锁直写分支仍可能放行第二实例。
- **风险影响**：本次 53208 是窗口进程（无引擎），实际并发拉数据/重复下单风险**未实际发生**；但若错位的是两个完整引擎实例，15:10 updater 双拉 + 重复下单风险即为现实。
- **评级：中（取决于人工操作频率）**；锁缺陷修复后降为低。

---

## 7. 收盘前状态锁（§3 前哨检查，14:55）

14:55 前哨检查已通过定时任务排程（at 2026-09-01 14:55，任务「X1收盘前哨检查」）。触发后执行：复核 8899 监听 / app.lock / 唯一 main.py 三者同 PID=44104、/api/overview 200、audit 心跳正常、watchdog State=Ready；**不一致则立即升级人类，不自行动刀**。结果将回填本节。

---

## 8. 防复发建议（只建议，不改码）

1. **锁机制修法草案**（main.py `_acquire_lock`）：
   - **无锁文件也不直接放行**：先做端口存活探测（TCP connect / HTTP 200）与 bind 探测，端口已被健康实例占用 → 转入"仅开窗口"或拒绝，而非直写 PID。
   - **alive 验证身份**：不仅 PID 存活，还应验证进程命令行含 main.py 且/或持有目标端口（OpenProcess + 端口归属交叉验证）。
   - **healthy 与 watchdog 口径一致**：改为 HTTP GET `/api/overview` 返回 200（而非裸 TCP connect），消除"端口通但服务未就绪/归属错位"盲区。
   - **stale 判定强化**：PID 不存在 → 清锁；端口健康但 lock PID ≠ 端口持有者 → 视为"锁异主"，不允许新实例简单覆盖，转告警。
   - **锁持续维护**：服务运行期间定期刷新锁（mtime/心跳 PID），退出时清理；或由 watchdog 统一维护。
2. **watchdog 判定顺序建议**：恢复动作前**先查端口 8899 归属** → 端口有健康服务 → 读 lock → lock PID ≠ 端口 PID → 记录"锁异主"告警、不动作、升级人类；仅端口死亡才走重启路径。
3. **桌面快捷方式入口**：`A股模拟盘.lnk`（pythonw main.py）是人工误启高风险入口；建议调整快捷方式为检测 8899 已活时仅开窗口（或加 `--browser`），并在快捷方式说明中标注"已运行则只开窗口"。

---

## 9. 交付物与红线核对

- 报告：`docs/reports/dual_instance_fix_20260901.md`（本文件）
- 命令输出：`tmp/x1/procs_20260901_103411.json`、`tmp/x1/ports_20260901_103411.json`、`tmp/x1/lock_20260901_103411.txt`、`tmp/x1/disposal_20260901_103952.log`
- 红线核对：仅处置 44104/53208 ✓；未碰 proxy.py(4656)/watchdog 本体/ml_sidecar/回测 ✓；先取证后动手 ✓；未改任何 .py/config ✓；未手改 account.json/审计链（审计链仅只读查询）✓；未 git ✓；未出网 ✓；app.lock 归位已明示说明 ✓。
- 未发现重复单（09:39 后零 buy/sell）→ 无上报人类事项。
