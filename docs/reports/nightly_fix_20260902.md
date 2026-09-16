# nightly_fix_20260902 —— Z1：TianjiGit_Nightly 自动提交修复（0x800710E0 + 编码 bug + pack9 收编）

- 作者/时间：Z1 块 / 2026-09-02 17:20（修复+验证完成）
- 状态：**已修复并手动全链路验证通过**；今晚 23:50 为第二次考试，报告 §5 附明日验收命令
- 写入面：`tools/git_nightly.ps1`（加 BOM，本块职责内）+ 任务 `TianjiGit_Nightly`（设置修改）+ 本报告 + `tmp/z1/`
- 红线核对：仅本块 git 写者执行 add/commit ✓；未 push/remote ✓；未 rebase/amend 既有提交 ✓；未改 .gitignore ✓；未碰 data/ 与 app/ 内容 ✓（data/ 中已跟踪状态文件随 `git add -A` 收编属 nightly 既有行为）；未动进程 ✓；未出网 ✓

---

## §1 根因证据链

### 1.1 错误码 0x800710E0 解读（启动前失败）

- `Get-ScheduledTaskInfo` 实况：LastRunTime=2026/9/2 09:21:10，LastTaskResult=**2147946720 = 0x800710E0**（任务启动被拒），NextRunTime=2026/9/2 23:50:00，NumberOfMissedRuns=0。
- 0x800710E0 = 0x80000000 | 0x0710E0（Win32 4320）→ **任务"运行"动作启动被拒**，进程未拉起。
- 佐证：`tmp/git_nightly.log` 不存在 = 脚本连"写日志"那行都没执行到（进程级未启动）；且脚本末尾 `exit 0`，若脚本曾运行即使路径错也会留 exit 0 而非 0x800710E0。
- 原任务 `LogonType=InteractiveToken`（"只在用户登录时运行"）→ 09:21 补跑时刻交互令牌不可用（机器夜间睡眠/会话未就绪）→ 启动被拒。**该失败发生在脚本启动之前**。
- 事件查看器 `Microsoft-Windows-TaskScheduler/Operational` 无事件（该日志未启用），无法进一步定位 09:21 触发源；以下以可观测事实为准。

### 1.2 原任务注册原文（修复前 schtasks /query /tn ... /xml）

```xml
<Principals><Principal id="Author">
  <UserId>S-1-5-21-133865196-1391603312-3977238681-1001</UserId>
  <LogonType>InteractiveToken</LogonType>
</Principal></Principals>
<Settings>
  <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
  <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
  <!-- 无 <StartWhenAvailable>（默认 false） -->
</Settings>
<Triggers><CalendarTrigger><StartBoundary>2026-09-01T23:50:00</StartBoundary>...</CalendarTrigger></Triggers>
<Actions Context="Author">
  <Exec><Command>powershell</Command>
  <Arguments>-NoProfile -ExecutionPolicy Bypass -File "C:\Users\26838\A股模拟盘\tools\git_nightly.ps1"</Arguments>
  </Exec>
</Actions>
```

**三处缺陷**：① 无 StartWhenAvailable → 23:50 睡眠错过不补跑；② 电池限制 true → 23:50 未插电则跳过；③ 仅日历触发器、无登录兜底。

### 1.3 第二根因（独立于 0x800710E0）：脚本编码 mojibake

本地手跑 `powershell -File tools\git_nightly.ps1`（与任务同款 Windows PowerShell 5.1）暴露第二 bug：

```
Set-Location : Cannot find path 'C:\Users\26838\A鑲℃ā鎷熺洏' because it does not exist.
At C:\Users\26838\A股模拟盘\tools\git_nightly.ps1:9 char:1
```

- **证据**：`tools/git_nightly.ps1` 前 3 字节 = `23 20 3D`（`# =`，**无 UTF-8 BOM**）；PowerShell 5.1 对无 BOM 脚本按 ANSI/GBK 解码 → 脚本 L9/L19 硬编码的 `C:\Users\26838\A股模拟盘` 中文字节被误读为 `A鑲℃ā鎷熺洏` → `Set-Location` 失败 → git 在错目录运行、`Add-Content` 写不到 `tmp/git_nightly.log`。
- 即**即使任务启动成功（0x800710E0 被解决），脚本仍会在错目录跑 git 且写不出日志**。两个 bug 叠加，导致 nightly 从未成功。
- 脚本头部注释自称"ASCII-only content (no Chinese)"，与 L9/L19 实际含中文矛盾——此为 W1 脚本的既有缺陷，本次修复一并纠正。

### 1.4 完整失败链还原

1. 9/1 23:50 触发：机器睡眠 + StartWhenAvailable=false → 不执行（无 run 记录）✓ 环境速查吻合；
2. 9/2 09:21 补跑（触发源未明，Operational 日志未启用）：InteractiveToken 令牌不可用 → **0x800710E0 启动被拒**（进程未起，日志未写）；
3. 即便步骤 2 启动成功：脚本无 BOM → 中文路径乱码 → Set-Location/Add-Content 失败（本报告 1.3 实测复现）。

---

## §2 修复动作（最小改动）

### 2.1 脚本：加 UTF-8 BOM（内容一字未改，仅前置 3 字节）

- 命令：读全字节 → 若不以 `EF BB BF` 开头则前置 BOM 写回。
- 哈希：修复前 `3B7E2504489891A74ED91FB93283D3F52ADC0C199C5582E57A3B7410E65CADEA` → 修复后 `8D86001EB2A1BADD74A892C112739A3F38167FA84B4A4B6F782B9FD79E968532`；前 3 字节 `EF BB BF` 确认。
- 验证（本地手跑，BOM 后）：`powershell -NoProfile -File tools\git_nightly.ps1` → 正常 Set-Location → commit `76e1d0c auto 2026-09-02 17:16` → `tmp/git_nightly.log` 首行 `2026-09-02 17:16:07 | 76e1d0c auto 2026-09-02 17:16` → exit 0。**编码 bug 修复确认**。

### 2.2 任务设置（Set-ScheduledTask，无需密码，保持 InteractiveToken）

| 项 | 修复前 | 修复后 |
|---|---|---|
| StartWhenAvailable | False（缺失） | **True**（睡眠错过 → 唤醒后补跑） |
| DisallowStartIfOnBatteries | True | **False**（23:50 未插电也能跑） |
| StopIfGoingOnBatteries | True | **False** |
| 触发器 | 仅日历 23:50 | **日历 23:50 + LogonTrigger**（登录兜底，针对 InteractiveToken 令牌脆弱性） |

- 途中一次 `Set-ScheduledTask` 同时改 Settings+Trigger 报 0x80070005（Access denied，触发器重建需更高权限）；**仅改 Settings 成功**，触发器用单独 `New-ScheduledTaskTrigger -AtLogOn -User '26838'` 追加成功。已记录该权限边界，不改动即不踩。
- 修复后 XML 关键段（存证）：`<StartWhenAvailable>true</StartWhenAvailable>`、`<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>`、`<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>`、双 Trigger（CalendarTrigger 23:50 + LogonTrigger UserId=痞帅晓宇的新宝\26838）。

### 2.3 手动触发全链路验证（Start-ScheduledTask）

| 指标 | 值 |
|---|---|
| 触发方式 | `Start-ScheduledTask -TaskName TianjiGit_Nightly` |
| LastRunTime | 2026/9/2 17:18:43 |
| **LastTaskResult** | **0**（原 0x800710E0 → 成功） |
| 日志新增行 | `2026-09-02 17:18:45 \| afdc9e6 auto 2026-09-02 17:18` |
| 新提交 | `afdc9e6 auto 2026-09-02 17:18`（含脚本 BOM 修复） |
| 任务状态 | Ready |

**手动验证结论：任务启动器→powershell→脚本→git add/commit→日志追加 全链路成功。**

---

## §3 pack9 全家入库证明

- 收编前脏文件数：**52**（任务书预估 49，实测 52：多出的为运行时状态与 Z3 新文件）。
- 收编提交：`a36c4c6 pack9 deliverables: Y1-Y5 + acceptance`（**125 files changed, +362822/-360**；含 `data/qfq_census_result.json` 415KB、`data/qfq_factor_sample.json` 6.2MB、`tmp/y1/` 4MB、Y1-Y5 报告+工具全套）。
- 收编后脏文件数：**0**（随后 Z3 块新出 2 个 diag 脚本，非本块范围，留待 23:50 nightly 自动收编）。
- git log 全列（5 提交）：
  ```
  afdc9e6 auto 2026-09-02 17:18        ← Z1 手动验证提交（含 BOM 修复）
  76e1d0c auto 2026-09-02 17:16        ← Z1 本地手跑提交
  a36c4c6 pack9 deliverables: Y1-Y5 + acceptance   ← pack9 全家（本块收编）
  4eec67e hygiene: eol policy + pack6/pack7 deliverables
  9aeb556 baseline 2026-09-01: post-incident full tree
  ```
- tmp/ 处置判定：`tmp/` 已被跟踪（git ls-files tmp/ = 189 文件），按任务书接受之并记账；未改 .gitignore；未用 rm --cached；新增 tmp 子目录随 `git add -A` 入库（体积大头 tmp/y1 4MB 已入库，tmp/x3/raw 0.55MB）。`tmp/git_nightly.log` 因 `*.log` 忽略规则不入库（仍正常追加写入）。
- 附：多次 commit 中 `CRLF will be replaced by LF` 警告为 W1 eol 策略（.gitattributes）对既有 CRLF 工作副本的归一化副作用，非本块引入，已随提交固化。

---

## §4 复发风险与建议（不改码）

1. **InteractiveToken 依赖交互会话**：若 23:50 用户已注销，任务仍会跳过（StartWhenAvailable 补跑需在下次登录触发）。本项目为交易工作机，23:50 通常在线；LogonTrigger 已作兜底。
2. **无 BOM 回归风险**：任何工具重写 `tools/git_nightly.ps1`（尤其用 UTF-8 无 BOM 写出）都会复现中文乱码。建议后续将脚本内硬编码路径改为 `$PSScriptRoot` 派生（彻底去掉中文字面量），本块因"最小改动"原则未实施，列为提案。
3. **建议（非本块实施）**：将来若需在"注销态"也跑，需改 `LogonType=Password`（要密码，走人类决策）或迁到 SYSTEM + 文件级提交。

---

## §5 明日（9/3）验收命令

今晚 23:50 第二次考试由任务自行触发（机器在线的正常触发，或睡眠后 StartWhenAvailable 补跑，或登录兜底）。**明日 9/3 任意时刻执行以下命令验收**：

```powershell
Set-Location 'C:\Users\26838\A股模拟盘'
git log --oneline          # 第 1 条（最新）应为：auto 2026-09-02 23:5x
Test-Path tmp/git_nightly.log   # 应为 True，且末行时间戳为 2026-09-02 23:5x
(Get-ScheduledTaskInfo -TaskName TianjiGit_Nightly).LastTaskResult   # 应为 0
```

判定标准：`git log --oneline` 最新提交为 `auto 2026-09-02 23:5x` 且 `tmp/git_nightly.log` 末行时间戳为 23:5x 且 LastTaskResult=0 → 修复验收通过。

---

## §6 资源注记

- 本地手跑（诊断+验证）：`powershell -NoProfile -File tools\git_nightly.ps1`，全程 <1s。
- Start-ScheduledTask 全链路：触发→完成约 2s（17:18:43 触发，17:18:45 日志落盘）。
- 临时物：`tmp/z1/`（含本报告引用之诊断输出）。本报告即最终交付。
- 已验证未受影响：进程（未动任何 python/服务，Z2 在管）；8899 服务与本修复无关。
