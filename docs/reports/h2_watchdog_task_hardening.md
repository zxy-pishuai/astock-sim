# H2｜看门狗计划任务加固（P0，改任务设置，零代码改动）

- 日期：2026-09-05（周六，验收方第二/三班）
- 块：H2（人类已批准修改 `TianjiService_Watchdog` 运行条件）
- **结论**：设置修改**成功且正确**（四目标字段逐字核验通过、触发器/命令/运行身份未动）；修订 (a) 的"watchdog.log 连续新增 ≥2 行"证据门**暂不可满足**——挂死实例 PID 22844 霸占 IgnoreNew，ETL 对改设置前已启动的实例不追溯，本块禁杀进程（红线）→ **如实记录并归因 H1b（未合入，无进程自杀兜底）**，不误判本块失败。

---

## §0 判据与查重

- 查重门：`tmp/pack19_dup.log` 已追加 `H2 <2026-09-05 UTC> GATE-OK`。
- `rg -n "DisallowStartIfOnBatteries" tools/*.ps1 tools/*.py tmp/pack*/ docs/reports/*.md` → **无任何既有脚本已做此事**（`tmp/pack18/watchdog_task_backup.xml` 是 9/4 改前备份，非修复）→ GATE-OK 成立。
- 人类批准状态：任务书前置"人类已明确批准修改运行条件"满足（用户指令明确授权）。

## §1 改前 XML 关键片段（`schtasks /query /tn TianjiService_Watchdog /xml`）

```
<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
<StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
（无 <StartWhenAvailable> 元素）
（无 <ExecutionTimeLimit> 元素 → Set-ScheduledTask 读数为 PT72H 默认）
<LogonType>InteractiveToken</LogonType>
<Interval>PT5M</Interval>
<Command>C:\Users\26838\AppData\Local\Programs\Python\Python313\pythonw.exe</Command>
<Arguments>C:\Users\26838\A股模拟盘\tools\service_watchdog.py</Arguments>
```

改前 XML 全档：`tmp/pack19/task_backup/TianjiService_Watchdog_20260905_223350.xml`（39 行）。

## §2 改法与命令原文

实现方式：`Set-ScheduledTask`（PowerShell，任务创建者身份，无需管理员提权，一次成功）。

```powershell
$t = Get-ScheduledTask -TaskName TianjiService_Watchdog
$t.Settings.DisallowStartIfOnBatteries = $false
$t.Settings.StopIfGoingOnBatteries = $false
$t.Settings.StartWhenAvailable = $true
$t.Settings.ExecutionTimeLimit = 'PT2M'
$t.Settings.MultipleInstances = 'IgnoreNew'   # 保持
Set-ScheduledTask -InputObject $t
```

执行输出（改前 → 改后，同命令内回读）：

```
改前: Disallow=True StopIf=True SAVA=False ETL=PT72H Multi=IgnoreNew
改后: Disallow=False StopIf=False SAVA=True ETL=PT2M Multi=IgnoreNew
SET-OK
```

## §3 改后 XML diff（修订 (b)：全文 + 四字段逐字核对）

改后 XML 全档：`tmp/pack19/task_backup/TianjiService_Watchdog_after_20260905_223558.xml`。**全文**：

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>2026-08-31T23:35:31</Date>
    <Author>痞帅晓宇的新宝\26838</Author>
    <URI>\TianjiService_Watchdog</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-21-133865196-1391603312-3977238681-1001</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2026-08-31T23:35:00</StartBoundary>
      <Repetition>
        <Interval>PT5M</Interval>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>C:\Users\26838\AppData\Local\Programs\Python\Python313\pythonw.exe</Command>
      <Arguments>C:\Users\26838\A股模拟盘\tools\service_watchdog.py</Arguments>
    </Exec>
  </Actions>
</Task>
```

**四目标字段逐字核对（修订 (b)）**：

| 字段 | 改前 | 改后 | 判定 |
|---|---|---|---|
| `DisallowStartIfOnBatteries` | true | **false** | ✓ |
| `StopIfGoingOnBatteries` | true | **false** | ✓ |
| `StartWhenAvailable` | （无元素） | **true** | ✓ |
| `ExecutionTimeLimit` | （无元素，默认 PT72H） | **PT2M** | ✓ |

**未变动字段核对**：触发器（TimeTrigger StartBoundary=2026-08-31T23:35:00 + Repetition Interval=**PT5M**）未动；命令（pythonw.exe + service_watchdog.py）未动；运行身份（InteractiveToken + SID）未动；`MultipleInstancesPolicy` 保持 **IgnoreNew** ✓。

**说明**：`UseUnifiedSchedulingEngine=true` 与 `<Task version="1.3">`（原 1.2）为 `Set-ScheduledTask` API 重写任务的系统默认增量，非手动设置字段；触发器/命令/身份/频率均未受其影响。

**改后三字段读数**（`/fo LIST /v`）：

```
Status:          Running
Last Run Time:   2026/9/5 22:45:01
Next Run Time:   2026/9/5 22:50:00
```

**Last Result（IgnoreNew 拒绝证据）**：`-2147020576` = `0x800710E0`（Win32 4320，拒绝类），结合下述 PID 存活 + 零新增行 → 任务实例被拒启动（IgnoreNew 策略下旧实例霸占）。

## §4 两周期日志增行证据（修订 (a)——受阻，如实记录并归因）

**观察基线**（改设置时刻 22:33）：`tmp/watchdog.log` 总行数 648；9/5 仅两批日志（12:15 4 行、22:00 4 行）；**末行 = 2026-09-05 22:00:08「端口 8899 关闭（全死），直接重启」**——22:00 实例写 4 行后卡死，22:05-22:30 六次激活零写入（挂死实例霸占 IgnoreNew 的直接证据）。

**改后观察**（22:33 设置生效 → 22:46，覆盖 22:35/22:40/22:45 三个 5 分钟周期）：

| 时刻 | watchdog.log 行数 | 新增 | watchdog 进程 |
|---|---|---|---|
| 22:33（改设置） | 648 | — | PID 22844（22:00:01 启动，挂死） |
| 22:41 | 648 | 0 | PID 22844 仍在 |
| 22:46 | 648 | 0 | PID 22844 仍在 |

**三件套记录（修订 (a) 要求）**：
- 强杀发生时刻：**未发生**（本块禁杀进程红线；ETL 对改设置前启动的实例不追溯——22:00:01 启动的 PID 22844 已运行 46 分钟 > PT2M 仍存活，证明任务服务不重新评估运行中实例的 ETL）；
- 新实例 PID：**无**（22:35/22:40/22:45 三次激活均被 IgnoreNew 拒绝，Last Result=0x800710E0）；
- 新增行时刻：**无**。

**归因**（按任务书："若 H1 未合入且上一实例仍卡死，则会因 IgnoreNew 而无新行——此时如实记录并把现象归因给 H1，不要误判本块失败"）：**H1b 未合入**（`tools/service_watchdog.py` 零改动，无进程自杀兜底）→ 挂死实例无人终止 → 新实例无法启动。**本块设置本身已正确生效**；待 H1b 合入（自杀兜底）或人工强杀 PID 22844 后，22:50+ 激活即可启动新实例写入日志，届时"新增 ≥2 行"证据门可闭合。

## §5 其他任务审计表（只审计，未动任何任务）

| 任务 | ExecutionTimeLimit | 触发器类型 | 电池/SAVA | 备注 |
|---|---|---|---|---|
| risk_daily | PT1H | CalendarTrigger 每日 20:00 | — | ✓ |
| forward_eval | PT2H | CalendarTrigger 每日 20:30 | — | ✓ |
| ml_scores_ledger_daily | PT2H | CalendarTrigger 每周 16:30 | — | ✓ |
| dual_price_weekly | PT3H | CalendarTrigger 每周日 09:00 | — | ✓ |
| live_vs_backtest_weekly | PT3H | CalendarTrigger 每周六 09:00 | — | ✓ |
| TianjiGit_Nightly | **（无 ETL 元素）** | CalendarTrigger 每日 23:50 + LogonTrigger | — | 唯一缺 ETL 者（W1 建），建议后续补 |
| TianjiCloseUpdate | PT1H | CalendarTrigger 每日 15:45 | **Disallow=true / StopIf=true / SAVA=true** | 见下 |
| A3_TRIAGE_qfq_weekly | PT1H | CalendarTrigger 每周日 09:00 | false/false/SAVA=true | 见下 |

**close_update / qfq_triage 异常说明**：任务书第 4 步称二者"本次查询返回空值"。实测 `schtasks /query /tn close_update.cmd` 与 `/tn qfq_triage` 均报 `ERROR: The system cannot find the file specified`——**原因是任务名不匹配，非任务失能**：
- `close_update.cmd` 的真实任务名 = **`TianjiCloseUpdate`**（存在，Ready，每日 15:45，ETL=PT1H）；
- `qfq_triage` 的真实任务名 = **`A3_TRIAGE_qfq_weekly`**（存在，Ready，每周日 09:00，ETL=PT1H）。
- 两任务均**未失能**；额外发现 `TianjiCloseUpdate` 仍带 `DisallowStartIfOnBatteries=true + StopIfGoingOnBatteries=true`（与看门狗改前同型），是否加固属后续决策（本次只审计）。

## §6 未覆盖风险

1. **电池耗尽收口**：`DisallowStartIfOnBatteries=false` 后看门狗在电池下也会启动；若电池耗尽关机，仍无日志写入（SAVA 仅在错过计划后开机补跑，不解决耗电关机中的盲区）。
2. **Sleep/休眠期间失明**：任务计划程序在 Sleep 期间不激活；恢复后靠 SAVA 补跑一次（已有），期间引擎若已死仍有一段空窗。
3. **挂死实例残留**：PID 22844 未被 ETL 终止（不追溯运行中实例），需 H1b 合入或人工强杀后才恢复写入——**已列为待批准项**。
4. **拔电/插电场景**：未实测（无法安全断电），依赖上述机制；本机为笔记本（ChassisTypes=10），电池场景真实存在。
5. **ETL=PT2M 对正常慢路径的风险**：若 watchdog 单轮健康检查 + 恢复动作偶发超过 2 分钟，会被任务服务强杀（下次激活重来）——按设计意图接受（配合 H1b 自杀兜底后单轮应在秒级）。

---

**本块未做的事**：未改任何代码文件（零 .py/.js/.json/.db 改动）；未新建计划任务；未改其他任务触发时间；未杀/未重启任何进程（PID 22844 原样保留）；未用 runas 或任何提权绕过。
