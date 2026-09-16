# H6 交付报告：清掉挂死的看门狗实例，恢复 5 分钟守护节拍

> 执行时间：2026-09-05 22:45 ~ 23:10 ｜ 处置人：H6 块执行 AI（人类已逐条批准结束进程）
> 结论：**守护节拍已恢复**（23:05、23:10 连续两个 5 分钟槽正常写入"健康 OK"）；服务未受影响；本块未修改任何文件内容、未重启任何服务。

---

## §0 判据与查重

**判据**：计划任务 `TianjiService_Watchdog` 每 5 分钟跑 `tools/service_watchdog.py` 做三级探活；一个挂死的看门狗实例霸占 `MultipleInstancesPolicy=IgnoreNew` 名额，使后续每轮激活被静默吞掉——服务活着但无守护。清掉挂死实例后，下一轮即恢复。

**查重门**（`tmp/pack19_dup.log` 追加）：

```
H2 2026-09-05 14:33:41 GATE-OK
H1b 2026-09-05 22:34:29 GATE-OK
H6 2026-09-05 22:45:58 GATE-OK
```

**rg 扫描** `Stop-Process|taskkill`（tools/ docs/reports/ tmp/pack*/）：命中项全部为**历史诊断/工具自身代码**——`docs/reports/half_dead_loop_diag.md`、`docs/reports/watchdog_hardening.md`（诊断/历史交付，任务明示非本块）、`tools/service_watchdog.py` 自身恢复分支的 taskkill 逻辑、以及 pack7/pack8/pack12 等历史块文档。**无任何既有运维块在做同一件事** → GATE-OK 成立。

---

## §1 现场保留（杀掉就没了——已全部落盘）

证据目录：`tmp/pack19/H6_evidence/`

| 文件 | 内容摘要 |
|---|---|
| `before_01_watchdog_log_tail5.txt` | `tmp/watchdog.log` 共 648 行；末 5 行原文：22:00:06 R2-P0.3 调度断更告警（距上次心跳 35100s）、22:00:06 异常 5/3（l1_tcp=down｜l2_overview=URLError 10061｜l3_audit=audit_age_27886s）、22:00:06 >>> 恢复触发（连续失败≥3）、22:00:08 端口 8899 关闭（全死），直接重启 |
| `before_02_heartbeat_state.txt` | `watchdog_heartbeat.ts` mtime=22:00:06、size=19，内容 `2026-09-05 22:00:06`；`h1_watchdog_state.json` mtime=22:00:06，内容 `{"consecutive_failures": 5, "last_status": "fail", "last_check": "2026-09-05 22:00:06", "restart_ts": ""}` —— 与任务背景描述完全一致 |
| `before_03_port8899_owner.txt` | 8899 Listen 属主 PID=24636 |
| `before_04_target_proc_detail.txt` | 目标 PID 22844：pythonw.exe，StartTime=2026-09-05 22:00:01，CPU 累计仅 0.1s，Handles=114，Threads=4，WorkingSet=2MB，Responding=True（Python 主线程阻塞在 I/O 上，非系统无响应） |
| `before_05_stack_note.txt` | **未取到栈**：`py -3.13 -m pip show py-spy` → Package(s) not found（未安装，本块无授权安装）；如实声明，不伪造 |
| `before_06_target_lock.txt` | 第 2 步全量进程列表与候选判定（见 §2） |
| `before_07_git_status.txt` | git 基线（见 §4 自检门） |

---

## §2 目标锁定证据（防误杀）

`Get-CimInstance Win32_Process -Filter "Name like 'python%'"` 全量 5 个 python 进程：

| PID | PPID | 创建时刻 | 命令行 | 归属 |
|---|---|---|---|---|
| 31980 | 9148 | 21:57:19 | `Desktop\anthropic_proxy.py` | 无关 |
| 28136 | 15924 | 21:57:22 | `opencode-go-proxy\proxy.py` | 无关 |
| 24040 | 27484 | 22:00:00 | `ComfyUI\main.py --windows-standalone-build` | 无关（不同路径） |
| **22844** | 2728 | 22:00:01 | `pythonw.exe C:\Users\26838\A股模拟盘\tools\service_watchdog.py` | **挂死候选** |
| **24636** | 37752 | 22:00:08 | `pythonw.exe main.py` | **8899 属主，绝不触碰** |

**候选判定**：命令行含 `service_watchdog.py` 的进程数 = **恰好 1 个**（22844）→ 满足"0 个或 ≥2 个即停手"的反向条件。

**三项交叉验证（全 PASS）**：
- a) 创建时刻 22:00:01，与 `watchdog.log` 最后一轮（22:00:06/08）一致 ✓
- b) `watchdog_heartbeat.ts` mtime=22:00:06，距采集时刻（22:48）48 分钟 > 15min ✓
- c) 8899 属主 PID=24636（main.py），与候选 22844 不同 ✓

---

## §3 人类批准记录与实际命令

- 人类预先授权："结束挂死的 service_watchdog 实例"。
- 执行前（22:48）已向人类逐条复述：①目标 PID=22844（service_watchdog.py）；②挂死判定证据；③服务实例 PID=24636 不受影响；④命令原文 `Stop-Process -Id 22844 -Force`（精确 PID，禁按名/通配符/多杀）；⑤验证计划（两个槽 + API + 属主）；⑥边界（不改文件/计划任务/代码）。共 6 条。
- 人类回复：**"同意"**（2026-09-05 23:01）。
- **实际执行命令**（23:01:56）：执行前 1 秒复核 PID 22844 命令行仍含 `service_watchdog.py`（防 PID 复用）→ `Stop-Process -Id 22844 -Force`，**成功、无报错**。
- 杀后即时复核：全量 python 进程已无任何 service_watchdog.py；8899 属主仍为 24636。

---

## §4 六项前后对照（自检门）

| # | 检查项 | 杀前读数 | 杀后读数 | 判定 |
|---|---|---|---|---|
| 1 | 目标 PID 22844 是否存在 | 存在（pythonw，22:00:01 启动） | **不存在**（已结束） | PASS |
| 2 | watchdog.log 行数 / 末行时刻 | 648 行 / 22:00:08"端口 8899 关闭（全死）" | 652 行 / **23:10:02"健康 OK"** | PASS |
| 3 | watchdog_heartbeat.ts mtime | 2026-09-05 22:00:06 | **2026-09-05 23:10:02** | PASS |
| 4 | h1_watchdog_state.json 三字段 | cfail=5 / status=fail / last_check=22:00:06 | **cfail=0 / status=ok / last_check=23:10:02**（restart_ts 均空） | PASS |
| 5 | 8899 属主 | PID=24636 | PID=24636（**未发生重启**） | PASS |
| 6 | /api/overview | —（杀前未查） | HTTP 200，`time=2026-09-05 23:10:24`，与请求时刻差 **0 秒**（≤90s） | PASS |

**git status --porcelain 前后对比**：基线（22:48）与终态（23:10）相对 diff——本块新增仅 `?? tmp/pack19/H6_evidence/`（证据目录）与本次交付报告 `docs/reports/h6_clear_hung_watchdog.md`；`tmp/watchdog.log`、`tmp/watchdog_heartbeat.ts`、`tmp/h1_watchdog_state.json` 的 M 状态为 **watchdog 自身每轮写入**（属正常守护行为）；其余差异（`docs/reports/issue_ledger.md`、`data/keypack/`、`docs/reports/k1b_*`、`tmp/pack20/k1b_*`、`tools/backup_key_pack.py` 等）为**并行块 H1b/H2/K1b 的活动**，非本块所为。
**声明：本块未修改任何文件内容（.py/.js/.json/.db/.md 一律未改）、未重启/停止任何服务、未改计划任务。**

---

## §5 守护节拍恢复证明（两个 5 分钟槽）

**槽 1（23:05）**——`watchdog.log` 新增 3 行（原文）：
```
2026-09-05 23:05:02  陈旧计数复位：consecutive_failures=5（>=3）→ 0（上一轮可能未正常收尾）
2026-09-05 23:05:02  R2-P0.3 调度断更告警：距上次心跳 3897s(>600s)，计划任务可能漏跑/延迟
2026-09-05 23:05:02  健康 OK：l1_tcp=ok | l2_overview=time_age_0s | l3_audit=audit_age_275s（连续失败已清零）
```

**槽 2（23:10）**——`watchdog.log` 新增 1 行（原文）：
```
2026-09-05 23:10:02  健康 OK：l1_tcp=ok | l2_overview=time_age_0s | l3_audit=audit_age_575s（连续失败已清零）
```

观察记录存 `tmp/pack19/H6_evidence/after_02_poll.log`（23:03~23:05 逐 30s 快照：杀后至 23:04:34 仍为挂死冻结态 new_lines=0，23:05:04 出现 3 行新日志并 cfail 归零）与 `after_03_poll2.log`（23:06~23:10 连续快照，23:10:09 确认槽 2）。另存 `after_01_kill_result.txt`、`after_04_git_status.txt`。

**结论：挂死实例清除后，计划任务下一轮激活即恢复执行并正常收尾，守护节拍已恢复。**

---

## §6 剩余风险

1. **恢复分支仍会挂（主风险）**：根因在 `start_service()` 内 `subprocess.run([... powershell ... Start-Process ...], capture_output=True, timeout=30)`——超时杀 powershell 后，第二次无超时 `communicate()` 因孙进程 `main.py` 继承被捕获管道写端而永不 EOF，watchdog 永久挂起。**只要下一次真正触发服务恢复，仍会复现**（历史三例：9/3 11:35、9/5 12:15、9/5 22:00）。须等 **H1b**（代码修复，并行中）落地。
2. **计划任务条件**：`DisallowStartIfOnBatteries` 等任务条件属 **H2** 范围；本次 23:05 槽正常触发，未发现计划任务本身异常。
3. **l3 audit 年龄**：槽内 audit_age 275s/575s 均 < 600s 阈值，属正常（audit 低频写入），无风险。
4. **历史告警噪音**：23:05 槽出现"调度断更告警（3897s）"——系挂死期间（22:00~23:05 约 65 分钟）累积的补告警，属预期现象，下一轮起自动消失。

---

## §7 移交给 H1b 的证据清单

H1b（看门狗死锁代码修复）的根因输入：

1. `tmp/pack19/H6_evidence/before_04_target_proc_detail.txt` —— 挂死进程终态画像：CPU 0.1s、Handles=114、Threads=4、WorkingSet=2MB、Responding=True（阻塞在 I/O 而非崩溃）。
2. `tmp/pack19/H6_evidence/before_01_watchdog_log_tail5.txt` —— 挂死最后一轮完整日志（22:00:06 三级失败 → 恢复触发 → 22:00:08"直接重启"后无任何输出）。
3. `tmp/pack19/H6_evidence/before_02_heartbeat_state.txt` —— 心跳/状态冻结于 22:00:06，`restart_ts=""`、`consecutive_failures=5`（复现判据：恢复分支 + 状态永久冻结）。
4. `tmp/pack19/H6_evidence/before_05_stack_note.txt` —— **未取到栈**（py-spy 未安装）；如需栈证据，建议 H1b 在修复前于受控环境复现并安装 py-spy 取栈。
5. 时序佐证：9/5 22:00:01 实例启动 → 22:00:06 失败计数 → 22:00:08 进入恢复分支 → 挂死至 23:01:56 被本块清除（期间 23:05 前的所有计划任务激活被 IgnoreNew 吞掉，与 12:15 的 41 分钟挂死、9/3 11:35 同型）。

---

*报告由 H6 块生成，全部读数均有 `tmp/pack19/H6_evidence/` 原始文件可溯源。*
