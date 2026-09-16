# A4 报告：非交易日不跑收盘更新 + 强杀留痕

> 日期：2026-09-13 ｜ 改动文件：`app/updater.py`（--close 入口）、`tools/close_update.cmd`
> ⚠ 并发合并：A1/A3 块（2026-09-13）同日也在改这两个文件（updater.py 加 A1 等锁重试、
> A3 兜底接力化；close_update.cmd 合写为英文注释 LF 版）。A4 语义在合并后完整保留
> （见 §1、§3 兼容性验证）。
> 触发事实：`TianjiCloseUpdate` 09-12（周六）15:45:01 执行，`LastTaskResult=3221225786`
> （0xC000013A = STATUS_CONTROL_C_EXIT，被 Ctrl+C/控制台关闭终止）；09-11、09-12 两次
> 运行都没写 exit 行到 `data/close_update_cron.log`（强杀时 cmd 收尾 echo 来不及执行）。

## §1 改动摘要（diff 级，含并发合并）

### `app/updater.py`（A4 净 +48 行；文件当前 1456 行，含 A1/A3 的 +224）

| 位置 | 改动 |
|---|---|
| 新增模块级 `_register_close_interrupt_trace()` | 注册 `SIGINT`(Ctrl+C=2) / `SIGBREAK`(控制台关闭=21) 处理：被终止时向 stdout（cmd 已重定向到 `close_update_cron.log`）写一行带时间戳的中断记录（`[A4] ... close_update 被中断 signal=N (0xC000013A=被Ctrl+C/控制台关闭终止)，中断留痕`）再 `exit 130`。测试与 __main__ 共用同一函数。 |
| `--close` 入口最前面（`if _a.close:` 首行，**A3 逻辑之前**） | ① 先调 `_register_close_interrupt_trace()`（任何路径被强杀都有日志证据）；② 交易日历判定 `trading_calendar.is_trading_day(今天)`——非交易日直接 `print("[A4] <date> 非交易日（周末/节假日），收盘更新跳过（无网络/DB 操作，exit 0）")` 并 `exit 0`，**不做任何网络/DB 操作**（不触达 `close_fallback_needed` 的只读 DB 查询、不触达 `run_update` 的网络拉取）。 |

兼容性验证（A4 与 A1/A3 顺序）：`if _a.close:` → A4 非交易日判定 → A3 的 22:00 窗口判断（`_hm >= CLOSE_CATCHUP_END`，常量定义于 L972）→ F4/A3 兜底判据（`close_fallback_needed(thr=..., require_snapshot=True)`，新签名定义于 L976）→ A1 等锁重试。非交易日不触达 A3 引用；交易日路径 A3 引用完整（py_compile rc=0 + 常量/签名实测在位）。

### `tools/close_update.cmd`（A1/A3 合写版，保留 A4 全部语义）

| 位置 | 改动/状态 |
|---|---|
| 头部注释 | A3 接力化说明 + A4 说明（非交易日 exit 0、SIGINT/SIGBREAK 留痕、0xC000013A 注解）+ 退出码语义——**英文注释**（A1/A3 合写时改英文：cmd 按系统 GBK 码页解析文件，UTF-8 中文注释/echo 会导致批处理解析错乱，实测 12:44 LF+中文版崩 `'remental' is not recognized`） |
| 调用与收尾 | `python -m app.updater --close` → RC 判断：`3221225786` → 记 `exit=0xC000013A (3221225786, terminated by Ctrl+C/console close)`；否则记 `exit=<RC>`；`exit /b %RC%` |
| 换行 | LF（项目约定 CRLF=0；英文注释版实测 cmd 正常解析运行，12:50 验收通过） |

## §2 退出码语义（已写入 .cmd 注释与 python 注释）

| 退出码 | 语义 |
|---|---|
| 0 | 正常 / 无需更新（含**非交易日跳过**） |
| 1 | 更新失败 |
| 130 | python 侧信号 handler 主动退出（中断留痕已写） |
| 0xC000013A（3221225786） | 被 Ctrl+C / 控制台关闭终止（OS 级，Task Scheduler 视角） |

## §3 验收证据

### ① 非交易日跳过（今天 09-13 周日，手动触发真实 cmd——最终合并版，12:50 重跑）

```
cmd exit=0 耗时=313ms
日志尾部：
[A4] 2026-09-13 非交易日（周末/节假日），收盘更新跳过（无网络/DB 操作，exit 0）
[周日 2026/09/13 12:50:45.15] close_update exit=0
```

- `exit 0` + 313ms（无网络等待）✓
- 日志出现 `[A4] ... 非交易日 ... 跳过` ✓
- 无网络/DB：代码路径 `is_trading_day` 只读内存常量+本地 json；输出无"覆盖率"字样（未进 `close_fallback_needed` 的 DB 查询）；`exit 0` 由 cmd 收尾行确认 ✓

### ② 强杀留痕（`GenerateConsoleCtrlEvent` 精确模拟 0xC000013A 类中断）

目标进程注册真实 `_register_close_interrupt_trace()`、stdout 重定向日志（模拟 cmd 的 `>>`），长驻模拟"正在执行收盘更新"。

**CTRL_BREAK（控制台关闭语义，signal=21）**：
```
READY pid=60024
[A4] 2026-09-13 12:46:53 close_update 被中断 signal=21 (0xC000013A=被Ctrl+C/控制台关闭终止)，中断留痕
```
目标进程已退出 ✓（该轮同时把共享控制台的 PowerShell 一起终止——同控台语义，证据仍成立）

**CTRL_C（Ctrl+C 语义，signal=2）**：
```
READY pid=70320
[A4] 2026-09-13 12:47:40 close_update 被中断 signal=2 (0xC000013A=被Ctrl+C/控制台关闭终止)，中断留痕
```
目标进程已退出 ✓（独立控制台，未误伤）

## §4 局限与说明

1. **硬杀不可捕获**：`taskkill /F`（TerminateProcess）任何用户态代码都来不及执行——但 0xC000013A 一定来自控制台关闭/Ctrl+C 类事件（可被 SIGBREAK/SIGINT 捕获），故本修复覆盖事故真实成因。
2. **cmd 收尾的边界**：强杀若连同 cmd.exe 整树一起终止，cmd 的 RC echo 来不及执行——由 python 侧信号 handler 兜底（任务书"双保险"语义）；若 cmd 存活，RC echo 正常补写（含 0xC000013A 注解）。
3. **批处理编码教训**：cmd.exe 按系统 GBK 码页解析批处理，UTF-8 中文注释/echo 会导致解析错乱（实测 12:44 LF+中文版崩）。合并版改英文注释规避；`close_update_cron.log` 历史仍有 GBK/UTF-8 混写乱码（未处理，不在本任务范围）。
4. 未触碰：`app/config.py`、市场库、计划任务注册、服务进程。

## §5 复验清单

- `py -3.13 -m py_compile app/updater.py`：PASS（rc=0，最终 1456 行版）
- updater.py：CRLF=0（LF）；close_update.cmd：LF（英文注释版实测 cmd 正常解析）
- 验收①：非交易日手动触发（最终合并版重跑）→ exit 0、313ms、日志"非交易日跳过"、无网络/DB ✓
- 验收②：CTRL_BREAK(signal=21) 与 CTRL_C(signal=2) 均写中断留痕并退出 ✓
- 并发兼容：A4 判定在 A3 逻辑最前端；A3 常量/新签名实测在位；py_compile 通过 ✓
- 无残留测试进程（test_kill/kill_sender 均已清）✓
