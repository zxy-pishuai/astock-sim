# H1｜看门狗硬化 + 半死状态根治（修复型）— 交付报告

- 日期：2026-08-31（周一）晚 23:10 前后
- 项目：`C:\Users\26838\A股模拟盘`（纯标准库后端，非 git，LF）
- 服务在线状态：**PID 26940，全程未重启**（红线遵守）
- 改动性质：代码改动 **下次重启服务才生效**（见 §8）

---

## 0. 任务背景

- **08-28 半死状态**：主线程退出后端口 8899 仍监听，线程池全部报
  `cannot schedule new futures after interpreter shutdown`（审计 134 条；高频监控 / 主循环 / 打板轮询 / 后台扫描全瘫）。纯 TCP 探测识别不了（端口开着）。
- **08-31 22:18 夜间误启动**：非交易时段「开盘自动开启」把引擎误启动（审计实锤：
  `{"t":"2026-08-31 22:18:44","event":"trading_event","level":"OK","msg":"开盘自动开启交易引擎（22:18）"}`）。

四项子任务：① 4 处 ThreadPoolExecutor submit 加守护；② 新建 `tools/service_watchdog.py`（三级健康判据 + 恢复）；③ 夜间误启动守卫（09:10–15:05 窗口）；④ 两个计划任务注册命令 + 请求批准。

---

## 1. 根因分析

### 1.1 半死状态（executor 层面）

`app/trader.py` 共 **4 处** `ThreadPoolExecutor`（本次全部定位并守护）：

| 行号(改后) | 场景 | 报错来源（审计 msg → 方法行） |
|---|---|---|
| 615 | `scan_once` 全市场评分 `_score_one` | `循环异常` → `_loop`(L302) |
| 642 | 资金面信号 `moneyflow_signals`（局部 `_TPE2`） | `后台扫描异常` → `_maybe_launch_scan_async._worker`(L444) |
| 876 | 打板 K 线并行精评 `fetch_kline`（局部 `_TPE`） | `打板轮询异常` → `_board_round`(L933) |
| 1117 | 两点半战法 `_tt_score_one` | `高频监控异常` → `_watch_round`(L1465) |

**根因链条**：主线程退出 → 解释器进入收尾（CPython `_python_exit`）→ 将全部 `ThreadPoolExecutor` 置 `_shutdown=True` → 仍在运行的后台线程（主循环 / 后台扫描 / 打板轮询 / 高频监控）后续 `submit` 全部抛
`RuntimeError: cannot schedule new futures after interpreter shutdown` → 4 条业务线逐一瘫痪；
但 httpd daemon 线程 + 某未退出线程仍占用进程与端口 → **端口仍监听（半死）**。

### 1.2 夜间误启动

`app/trader.py` `enable_auto_start` 的 `_monitor` 线程（每 15s）触发条件原为 **`hm >= 915` 无上限**，
22:18 时 `hm=2218` 也满足 → 夜间误启动。`_auto_start_ts[today]` 按日去重只能防"当日重复"，
防不了"夜间首启"。

---

## 2. 改动清单（全部在合法写入面内）

### 2.1 `app/trader.py`（LF 保留；py_compile ✓）

1. **新增 `_ResilientPool` 守护类**（行 29 起，模块级，`__slots__` 三字段）：
   - `submit()` 捕获 `RuntimeError("cannot schedule new futures…")` → **重建池并重试一次**；
   - 重试仍失败则 `ex2.shutdown(wait=False)` 后上抛（由各循环既有 try/except 记异常并整轮重试）；
   - `__exit__` 对全部已建池 `shutdown(wait=False)`（4 处 `with` 块均在块内用 `as_completed` 消费完结果，语义无回归；半死/收尾时**绝不阻塞**）。
2. **4 处池构造替换**：`ThreadPoolExecutor(...)` → `_ResilientPool(...)`（行 615 / 642 / 876 / 1117），
   删除两个局部 `_TPE2` / `_TPE` 别名 import（无残留，`ThreadPoolExecutor` 仅剩顶层 import 与类内部）。
3. **夜间误启动守卫**（行 228-260 区间）：
   - 启动条件由 `hm >= 915` 改为 **`910 <= hm <= 1505`**（09:10–15:05 窗口）；
   - 窗口外当日记录一次「待明晨自动启动」（新增类属性 `_auto_pending_day`，防 15s 轮询刷屏），
     次日窗口到达后由同一守护线程自然启动；
   - 保留：交易日判断（`trading_calendar`）、`_auto_start_ts` 按日去重、`_user_stopped` 跨日自动清除。

### 2.2 `tools/service_watchdog.py`（新建；纯标准库，不 import app/*；LF；py_compile ✓）

三级健康判据（任务规格 + 实测校准）：

| 级别 | 判据 | 实测基线（22:53-23:07） |
|---|---|---|
| L1 | TCP 127.0.0.1:8899 可连通（5s 超时） | `ok` |
| L2 | GET `/api/overview` HTTP 200 且 `time` 字段 ≤90s（字段归属已在 `app/server.py` L403-416 确认） | `time_age_0s` |
| L3 | `data/audit/audit.jsonl` 尾部 `t` 新鲜：**交易时段**（交易日 09:30–11:30 / 13:00–15:00）≤10 分钟；**非交易时段放宽到 7 天** | 非交易 2709s → 通过 |

> **L3 时段自适应的必要性（实测证据）**：非交易时段引擎不写 `heartbeat`（`al.heartbeat("trading")`
> 仅交易时段每分钟一次），当前 22:22 后审计尾部即静止。若按字面「≤10 分钟」全天强制，夜间/周末必然误判触发恢复。
> 故：交易时段严格 10 分钟（这正是"半死"的核心探测信号——引擎死但 HTTP 活着时，心跳停 → L3 拉响）；
> 非交易时段仅做 7 天存活性粗检。**节假日白天**（工作日窗口内但无交易）由「恢复前二次复核」兜底（见下）。

恢复逻辑（连续失败 ≥3 次触发，先探 8899 防双实例）：
1. 端口关（全死）→ 直接 `Start-Process <Python313>\python.exe main.py`（hidden）。
2. 端口开但 L2/L3 不新鲜（**半死僵尸**）→ 二次复核：若此刻 L2+L3 均新鲜 → 判健康实例，**跳过恢复**（防双实例 / 防节假日误杀）；否则定位占用 8899 且命令行含 `main.py` 的本服务进程 → `taskkill /F` → 清 `data/app.lock` → 重启（**必须先清僵尸再启**：main.py 的单实例锁在端口"看似健康"时会拒新实例）。
3. 端口被**非 main.py 进程**占用 → 不终止、跳过并记录（避免误杀他进程）。

运行/落盘：
- 状态计数持久化 `tmp/h1_watchdog_state.json`（`consecutive_failures`，健康清零 / 失败累计 / 恢复后清零）；
- 每次动作追加 `tmp/watchdog.log`（UTF-8：时刻 + 三级明细 + 恢复时旧PID消失/新PID）；
- 恢复用解释器 `resolve_python()` **强制指向 `C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe`**
  （实测：PATH 首位是 Doubao 沙箱 python **无 mootdx**，运行中服务正是 Python313 带 mootdx；若误用沙箱 python 启动，服务将缺依赖）。
- 入口：默认单次完整检查（供计划任务每 5 分钟调用）；`--verify` 只读体检（不改状态、不恢复，供人工验证）。

### 2.3 `tmp/g0_autostart.ps1`（新建，纯 ASCII）

开机自启脚本（`StockService_Recovery` 调用）：`Test-Port8899`（2s 超时）→ 已 LISTEN 则退出（防双实例）→ 否则
用 Python313 全路径 `Start-Process main.py -WindowStyle Hidden`。

### 2.4 `main.py`

**未改动**（合法写入面内但无需改）：`enable_auto_start()` 调用段（L215-221）逻辑不变，
夜间守卫全部在 `trader.py` 内实现。

### 2.5 未触碰

- `app/config.py`：未改任何参数；
- 审计链（`app/audit.py` / `audit.jsonl` 写入路径与格式）：未动，watchdog 只读尾部；
- `data/market.db` / `data/min5.db`：未写。

---

## 3. 验证结果（改完后实测）

1. **语法**：`python -m py_compile` 通过（trader.py / service_watchdog.py）；全项目
   `python -m compileall -q -f app tools main.py` **exit=0**（唯一 SyntaxWarning 为未触碰的
   `tools/ml_sidecar/rolling_train.py:10` 既有 escape 警告，非本次改动）。
2. **行尾**：`app/trader.py`（bareLF 1585 / CRLF 0）、`tools/service_watchdog.py`（bareLF 342 / CRLF 0）均 **LF**；
   `tmp/watchdog.log` 为 CRLF（与本项目日志文件 `audit.jsonl` 的 CRLF 约定一致）。
3. **watchdog 在线实测**（服务在线 PID 26940）：
   - `--verify`：`l1_tcp=ok | l2_overview=time_age_0s | l3_audit=audit_age_2709s` → **OK**；
   - 完整周期（计划任务路径）：健康 → 计数清零，`tmp/h1_watchdog_state.json` = `{"consecutive_failures":0,...}`；
   - 恢复子组件只读测试：`pid_on_port(8899)=[26940]`（len=1）、`cmdline_has(26940,'main.py')=True`、`read_lock_pid=26940`、
     `resolve_python()`=Python313 全路径；
   - 时段判据边界：09:29✗ / 09:30✓ / 11:30✓ / 11:31✗ / 13:00✓ / 15:00✓ / 15:01✗ / 周六✗ 全部正确。
4. **`_ResilientPool` 单测**（独立副本，同款逻辑）：
   - 场景1 正常池：**PASS**（8/8 结果正确）；
   - 场景2 池 shutdown 后 submit：**PASS**（捕获 RuntimeError → 重建 → 重试成功，结果正确）；
   - 场景3 解释器真正收尾（atexit 时刻）：**FAIL（预期）**——见 §6 边界披露。
5. **`g0_autostart.ps1` 防双实例实测**（服务在线时运行）：输出
   `(2026-08-31 23:06:28) 8899 already LISTEN, exit (anti-duplicate).`，exit=0，服务仍 PID 26940（未双开）。
6. **计划任务命令构造干跑**：两个注册命令的 Action/Trigger/Settings/Principal 对象全部构造成功
   （Trigger1=AtStartup+Delay PT60S；Trigger2=Once+Repetition PT5M），**未注册**（待批准）。

---

## 4. 夜间误启动守卫细节

- 窗口：**09:10–15:05**（`910 <= hm <= 1505`），覆盖竞价与全天交易；15:05 后（盘后/夜间）不再启动。
- 窗口外：当日仅记一次「待明晨自动启动」（`_auto_pending_day` 按日去重），不启动引擎。
- 窗口内到达：正常走「预热 updater → `self.start(auto=True)` → 记 `_auto_start_ts`」原链路。
- 22:18 场景复现推演：新逻辑下 `hm=2218` 不满足窗口 → 记「待明晨」→ 次日 09:10 起自然启动，**误启动不再发生**。
- 生效前提：**服务重启后**（见 §8）。

---

## 5. 与已有恢复机制的闭环

- G0（前序包）已恢复服务至 PID 26940；`StockService_Recovery`（开机自启）+ 每 5 分钟 watchdog 构成
  「开机自启 + 运行期三级健康监控 + 半死/全死恢复」闭环。
- 现有六件套计划任务（16:30 台账 / 20:00 风险 / 20:30 前瞻 / 各周任务）不受影响（不重叠时段；watchdog 每次 ≤1s 轻量检查）。

---

## 6. 边界披露（诚实项）

1. **解释器真正收尾时，重建池无法复活（场景3 实测）**：
   08-28 的"半死"本质是主线程退出后解释器开始收尾，此时新线程也无法创建，任何"重建池"必然再失败。
   `_ResilientPool` 的真实价值 = 把"一次致命崩溃"转化为"循环内可捕获的异常 → 整轮重试/继续"，
   消除 134 条刷屏式瘫痪；**真正的根治是 watchdog L3 审计判据 + 进程重启**（半死时心跳停 → L3 拉响 → 杀僵尸重启）。
2. **`[26940]` 显示伪影**：本报告验证过程中发现 Bash 工具输出层会吞掉含 `[26940]` 的 stdout 行
   （`[1,2,3]`/`['a']` 正常），但写入文件字节正确（`5B 32 36 39 34 30 5D`= `[26940]`），
   且 `pid_on_port` 的 len=1、元素=26940、`cmdline_has` 均验证通过——**纯显示伪影，不影响逻辑**。
3. **`__exit__` 用 `wait=False`**：与原 `with ThreadPoolExecutor`（默认 wait=True）不同；
   但 4 处块体均已在块内用 `as_completed` 消费完结果，对外语义一致，且半死时绝不阻塞调用线程。
4. **L3 阈值分时段（10 分钟交易 / 7 天非交易）** 是对任务规格"≤10 分钟"的必要工程化修正，
   否则夜间/周末必然误判（见 §2.2 实测证据）；节假日由恢复前二次复核兜底。

---

## 7. 一键可批：两个计划任务注册命令（待用户批准）

> ⚠️ **状态更新（23:50 实时）**：用户已批准。`TianjiService_Watchdog` **注册成功并已端到端验证**（见 §11）；
> `StockService_Recovery` **注册受阻——需管理员权限**（非管理员用户无法创建 `onstart`/`onlogon` 触发器，
> `Register-ScheduledTask` 与 `schtasks` 双通道均实测 `Access is denied`），见 §11 的待办与替代方案。
> 下方命令保留为完整方案原文（含已改用 schtasks 通道的版本）。

**任务 1：StockService_Recovery（开机自启，AtStartup 延迟 60s）**
```powershell
$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\Users\26838\A股模拟盘\tmp\g0_autostart.ps1`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT60S'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "StockService_Recovery" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "A股模拟盘服务开机自启（延迟60s，防双实例）"
```

**任务 2：TianjiService_Watchdog（看门狗，每 5 分钟）**
```powershell
$action = New-ScheduledTaskAction -Execute "C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe" -Argument "`"C:\Users\26838\A股模拟盘\tools\service_watchdog.py`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "TianjiService_Watchdog" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "A股模拟盘服务三级健康看门狗（每5分钟）"
```

> **实际注册采用 schtasks 通道**（`Register-ScheduledTask` cmdlet 对非管理员报 `0x80070005 Access denied`，
> 而 schtasks 对 `minute` 触发可无管理员创建）：
> ```powershell
> schtasks /create /tn "TianjiService_Watchdog" /tr "\"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe\" \"C:\Users\26838\A股模拟盘\tools\service_watchdog.py\"" /sc minute /mo 5 /it /f
> ```
> 注册结果：**SUCCESS**（§11 已端到端验证）。

---

## 8. 生效时点声明（重要）

- ⚠️ **状态更新（23:50 实时）**：服务 23:07-23:36 间**自然宕机**，watchdog 于 **23:45 自动恢复**（新实例 PID 28260）。
  新实例启动晚于 trader.py 修改（22:52），因此 **H1 的 4 处 executor 守护 + 夜间守卫已随此次 watchdog 重启生效**（已由
  审计 23:45:12「非 09:10-15:05 窗口，待明晨自动启动」实锤确认）——"下次重启才生效"的保留条款在本次真实恢复中已自然解除。
- `tools/service_watchdog.py`：**已生效**（注册为计划任务每 5 分钟自动跑，且已实际完成一次真实恢复）。

---

## 9. 本块未做的事清单（留给验收 / 后续）

1. **未 apply 重启（红线遵守）**：我没有手动重启服务；但服务 **23:07-23:36 自然宕机**，watchdog 已自动恢复（§11）——
   非我所为的重启，且新代码随之生效。
2. **StockService_Recovery（开机自启）计划任务未注册成功**：需管理员权限（非管理员无法创建 onstart/onlogon 触发器）。
   待办见 §11「下一步」。
3. **未改 `app/config.py`**：参数零改动。
4. **未动审计链**：`audit.py` / `audit.jsonl` 写入路径与格式未改（watchdog 只读）。
5. **未更新 backlog/看板**：看板状态更新属验收流程。
6. **恢复路径端到端已实测**：原报告所列"未做恢复路径真实触发测试"缺口，已由本次真实宕机 + watchdog 自动恢复闭合（§11）。

## 10. 交付物清单

| 文件 | 状态 |
|---|---|
| `app/trader.py`（改） | 4 处 executor 守护 + 夜间守卫；LF；py_compile ✓ |
| `tools/service_watchdog.py`（新） | 三级健康 + 半死恢复；LF；py_compile ✓；在线实测健康 |
| `tmp/g0_autostart.ps1`（新） | 开机自启防双实例；防双实例路径实测通过 |
| `tmp/watchdog.log` / `tmp/h1_watchdog_state.json`（新） | 运行日志与状态 |
| `docs/reports/watchdog_hardening.md`（本文件） | 交付报告 |
| `tmp/h1_*.py`（测试脚本） | 验证证据（验收后可删） |

---

## 11. 批准后补录：计划任务注册 + 真实宕机自动恢复（23:36–23:50 实时事件）

### 11.1 批准 → 注册结果

用户 23:2x 批准两个计划任务注册。执行结果：

| 任务 | 注册通道 | 结果 | 说明 |
|---|---|---|---|
| `TianjiService_Watchdog` | `schtasks /sc minute /mo 5 /it /f` | **SUCCESS** | cmdlet `Register-ScheduledTask` 对非管理员报 `0x80070005`；schtasks 的 `minute` 触发无需管理员 |
| `StockService_Recovery` | cmdlet + schtasks `onstart`/`onlogon` 双通道 | **Access denied** | 非管理员（IsAdmin=False）无法创建开机/登录触发器类任务；见 11.4 下一步 |

### 11.2 真实宕机事件（watchdog 首秀）

- **23:07** 服务仍健康（watchdog 实测 `l1_tcp=ok`）。
- **23:07–23:36** 服务（PID 26940）**自然宕机**：8899 无监听、进程消失、app.lock 残留旧 PID；
  无崩溃事件（Application 日志仅无关的 mscorsvw）、无重启事件（System 1074/41/6005/6006 均无）——**无声死亡**，
  与本项目当日反复出现的"服务莫名宕机"同型。
- **watchdog 按设计接力**（每 5 分钟，连续失败 ≥3 触发恢复）：
  - `23:36:05` 异常 **1/3**（l1=down, l2=连接拒绝, l3=audit 4440s）
  - `23:40:05` 异常 **2/3**
  - `23:45:05` 异常 **3/3** → `>>> 恢复触发`
  - `23:45:07` 端口关闭（全死）→ 直接重启（`Start-Process Python313\python.exe main.py` hidden）
  - `23:45:08` 新进程 **PID 28260** 启动（命令行 `"C:\...\Python313\python.exe" main.py`）
  - `23:47` 实测：8899 LISTEN=28260，`/api/overview` **HTTP 200 且 time 新鲜** → 恢复成功
  - `tmp/h1_watchdog_state.json`：`consecutive_failures=0, last_status="recovered"`

**端到端闭合**：原报告 §9 所列"恢复路径未做真实触发测试"的缺口，已由本次真实宕机 + 计划任务自动恢复闭合——
三级判据、失败计数、3 连败触发、防双实例、全死直接重启、状态落盘全链路验证通过。

### 11.3 新代码已随恢复生效（夜间守卫实测）

新实例 28260 启动晚于 trader.py 修改（22:52），加载的是**新代码**。审计实锤：
`{"t":"2026-08-31 23:45:12","msg":"开盘自动开启：23:45 非 09:10-15:05 窗口，待明晨自动启动"}`——
23:45 时 `hm=2345`，新守卫正确**拒绝**启动引擎（旧代码 `hm>=915` 会误启动），并记录"待明晨"。
**夜间误启动守卫 live 且行为正确。**

### 11.4 发现：main.py 清僵尸锁后不重写新 PID（既有怪癖，未改）

`main.py:_acquire_lock` 在发现旧锁 PID 已死（僵尸）时 `os.remove(_LOCK_FILE)` 后直接 return，
**不写新 PID** → 恢复后 `data/app.lock` 缺失（本次实测确认）。影响仅限 watchdog 无法从锁文件确认新 PID
（首次恢复日志记"新PID=0"）。已在本块内改进 watchdog：恢复确认增加**端口反查回退**
（`pid_on_port` + `cmdline_has(main.py)`，排除旧 PID），实测返回 28260 正确。main.py 本身未改（超范围）。

### 11.5 下一步（需用户决定，2 选 1 恢复"开机自启"）

**✅ 已实施：用户选择方案 B（无需管理员）**——2026-09-01 00:03 创建启动文件夹快捷方式并验证：

- 快捷方式：`C:\Users\26838\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\StockService_Recovery.lnk`
- Target=`powershell.exe`，Args=`-NoProfile -ExecutionPolicy Bypass -File "C:\Users\26838\A股模拟盘\tmp\g0_autostart.ps1"`，
  WorkingDirectory=`C:\Users\26838\A股模拟盘`，WindowStyle=7（最小化）
- 实测：直接运行该 ps1 完整路径 → 60s 延迟后探测 8899 → 服务在线 → 输出
  `(2026-09-01 00:03:20) 8899 already LISTEN, exit (anti-duplicate).`，exit=0 → **防双实例路径正确**
- 即：用户下次登录 Windows 时，脚本自动等待 60s → 若服务未起则后台启动（Python313 main.py hidden），
  若已起则跳过；未来服务掉线期间的重启场景由 TianjiService_Watchdog 每 5 分钟兜底。

> 方案 A（管理员 schtasks onlogon 注册）仍保留作备选，若日后想要"系统级开机（未登录也）自启"再切 A。
