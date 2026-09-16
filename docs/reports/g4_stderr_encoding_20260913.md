# G4 子进程日志编码修复 + HTTP 异常静默（可诊断性）—— 2026-09-13

> 依据：data/logs/watchdog_child_stderr_*.log 实测乱码 + app/tactics.py:1156 已知痛点注释。
> 改动：app/server.py（_send 断开保护）、main.py（stderr UTF-8）、tools/service_watchdog.py（env+轮转）。
> **未杀/未重启生产服务**（PID 13108 在跑旧代码）——编码与 _send 保护**下次 watchdog 重启生效**。

## §0 事实核查（改前实测）

1. **mojibake 实锤**：6 份 `watchdog_child_stderr_*.log` 全部含 U+FFFD 乱码行（每份 106-199 行）：
   ```
   File "C:\Users\26838\A��ģ����\app\server.py", line 376, in do_GET
   TDX fetch_quotes_fast ���峳�(8s) ...
   ```
   根因：子进程 stderr **未 reconfigure** → 重定向文件按 locale（GBK/cp936）编码 → 读端按 UTF-8 解码 → 乱码（"A股模拟盘"→`A��ģ����`，即派单所述 `A?????`）。
2. **ConnectionAbortedError 实锤**：多份日志含 `server.py:381 do_GET → self._send(500,...) → end_headers → sendall → ConnectionAbortedError WinError 10053`（浏览器等不及慢接口主动断开 → 500 分支自身再抛 → 刷屏 traceback 淹没真实错误）。

## §1 改动清单

| 文件 | 行 | 改动 |
|---|---|---|
| app/server.py | L990-1006 | G4 块：`_client_abort_count` 计数器 + `_bump_client_abort()`（每 100 次一条 audit `event="http_client_abort"`，与 J2 api_slow 慢接口埋点互证） |
| app/server.py | L1048-1053 | `_send`：`end_headers + wfile.write` 包 `ConnectionAbortedError/ConnectionResetError/BrokenPipeError` 保护 → 计数，不抛（J1 预留接入点已合入） |
| app/server.py | L1115-1118、L1128-1131 | do_GET/do_POST 外层 except 的 `_send(500,...)` 同样保护（**发送错误响应本身不得再抛**） |
| main.py | L21-34 | stderr `reconfigure(encoding="utf-8", errors="replace")`（原只有 stdout）+ `os.environ.setdefault("PYTHONUTF8","1")` 供子进程继承 |
| tools/service_watchdog.py | L620-627、L639-641 | start_service 主/降级分支 Start-Process 前设 `$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'`（子进程继承，双保险） |
| tools/service_watchdog.py | L550-577 | `_prune_child_logs`：keep=5→10（≤20 份文件）；新增**单份 >5MB 优先删除** |

## §2 编码修复（任务 1/2）

- 根因链：main.py 只 reconfigure 了 stdout → 服务进程 stderr 按 GBK 写重定向文件 → 中文 traceback 全乱码。
- 双保险修复：①main.py 启动早期 stderr reconfigure（持久，进程自身行为）+ PYTHONUTF8（子进程继承）；
  ②watchdog 启动命令先设 env（防御——即使 main.py 未生效，环境变量也强制 UTF-8）。
- **演示证据**（tmp/g4_test/demo2.log）：reconfigure 后 stderr 重定向文件中文正常可读（无 U+FFFD）。
- **生效时机**：服务进程下次由 watchdog 重启时（恢复触发或人工重启）；当前 PID 13108 仍为旧编码行为，
  报告中如实标注——**验收"新产生日志 100% 可读"须在下次重启后复核**。

## §3 HTTP 异常静默（任务 3/4）

- `_send` 断开保护：浏览器提前断开（WinError 10053/10054/109）→ 降级为计数，**不打印 traceback**；
- do_GET/do_POST 的 except 500 分支：`_send` 自身断开也不得再抛（嵌套保护）；
- `_bump_client_abort()`：`client_abort_count` 每 100 次汇总一条
  `audit.record(kind="alert", event="http_client_abort", level="WARN", msg="客户端提前断开累计 N 次（慢接口信号，与 J2 治理互证）")`。
- **语义**：客户端断开是"接口太慢"的用户侧信号——与 J2 的 api_slow 服务侧埋点互为验证
  （若 client_abort 持续增长而 api_slow 无对应，说明慢点不在被埋点的接口；反之亦然）。

## §4 轮转（任务 5）

- `_prune_child_logs(keep=10)`：保留最近 10 代（stdout+stderr 各一文件 → **≤20 份**）；更老删除；
- **单份上限 5MB**：超限文件优先于代数清理直接删除（防失控膨胀）；实测现有日志最大 82KB，远低于上限。

## §5 预注册验收判据 → 逐条结论

| # | 判据 | 结果 |
|---|---|---|
| T1 | _send 遇 ConnectionAbortedError → 不抛、计数+1 | **通过**（假 wfile/end_headers，10 用例 0 失败） |
| T2 | _send 遇 ConnectionResetError → 不抛、计数+1 | **通过** |
| T3 | _send 遇 BrokenPipeError → 不抛、计数+1 | **通过** |
| T4 | do_GET except 500 分支自身断开 → 不抛、计数+1 | **通过** |
| T5 | 轮转 ≤20 份文件、>5MB 优先删 | **通过**（15 代 + 超限文件 → 清理后 20 份，超限已删） |
| T6 | main.py stderr reconfigure + PYTHONUTF8 | **通过**（源码断言） |
| T7 | watchdog 命令含 PYTHONIOENCODING/PYTHONUTF8 | **通过**（源码断言） |
| 编码 | 新产生 stderr 日志中文 100% 可读 | **待下次重启复核**（演示已证新行为正确；生产 PID 13108 仍旧代码） |
| 真实断开 | curl/socket 主动断开 → 无 traceback + 计数+1 | **待下次重启复核**（逻辑已单测；不连生产做破坏性测试） |

## §6 风险与回滚

- **风险**：`_send` 保护吞掉连接错误后，调用方可能误以为发送成功——但断开时响应本就不被接收，
  计数+audit 已留痕；`client_abort_count` 累计 100 才告警，避免疲劳。
- **回滚**：`git checkout HEAD -- app/server.py main.py tools/service_watchdog.py`；
  单点禁用：server.py 删 `_bump_client_abort()` 调用、main.py 删 stderr 两行、watchdog 恢复 keep=5。
- 与服务重启的关系：改动均需下次进程重启生效，属计划内（watchdog 每 5 分钟健康检查，若正常则不重启，
  直到下次恢复触发或人工重启）。

## §7 未做清单

- 未重启/未杀生产服务（红线）；未改 app/config.py、app/audit.py、app/tactics.py；
- 未清理现有 6 份乱码历史日志（保留作对照证据；轮转会在未来自然淘汰）；
- 未与 J 道合并代码（J1 的 gzip/缓存头已合入 _send，G4 只加异常保护——两单改动在 _send 不同位置，
  已按"串行"约定：J1 先落、G4 后加，git 历史可查）。
