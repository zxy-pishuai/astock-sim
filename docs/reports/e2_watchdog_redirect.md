# E2 报告：watchdog 重定向唯一文件名 + 失败降级（P0）

> 日期：2026-09-03 ｜ 改动文件：`tools/service_watchdog.py`（唯一）
> 背景：`docs/reports/acceptance_pack15_20260903.md` 现场连带结论节 + `docs/reports/audit_20260903.md` §2 P0-2
> 根因：B 块重定向用固定文件名（`watchdog_child_stdout.log`），09-03 01:17:39 实测僵尸进程握持该文件句柄 → `Start-Process` 重定向失败 → 恢复启动失败 → 日志断更 8 小时。缺陷至今在码里。

## §1 改动摘要（diff 级）

| 位置 | 改动 |
|---|---|
| 顶部 imports | +`import re`（解析唯一日志名代数） |
| L59-61 常量区 | 删除固定名 `CHILD_STDOUT` / `CHILD_STDERR`（原 L59-60）；注释说明根因与替代方案 |
| `rotate_child_logs()`（原 L290-305） | **整函数删除**——旧 `.prev` 轮换逻辑被"按次唯一文件名 + 保留 5 代"替代（存废自定 → 删除，因为固定名机制已不存在，.prev 语义失效） |
| 新增 `_child_log_name(kind)` | 按次唯一日志名：`watchdog_child_<stdout|stderr>_<YYYYmmdd_HHMMSS>_<seq>.log`；`seq` 用于同一秒多次启动的区分（第 1 代 seq=1），撞名自动递增 |
| 新增 `_prune_child_logs(keep=5)` | 按文件名 `(ts, seq)` 分组判定代数，只保留最近 5 代（stdout+stderr 同代各一文件），更老删除；异常吞掉不影响恢复 |
| `start_service()` | 重构：生成唯一名 → 带重定向 `Start-Process`；失败/异常 → 记日志"重定向不可用，降级启动（不带重定向）" → **立即重试不带重定向**。恢复动作永不因重定向失败而阻塞（铁律）。 |
| `STARTUP_GRACE` / 三级判据 | **一字未动**（L57 常量、L208-222 豁免逻辑、L153-162 判据） |

行数：409 → 444（+35）。`py_compile` rc=0；CRLF=0（LF）。

## §2 命名与保留策略

- **命名**：`watchdog_child_stdout_<YYYYmmdd_HHMMSS>_<seq>.log` / `watchdog_child_stderr_<...>_<seq>.log`。
  - `<seq>`：同一秒内多次启动（如连续恢复触发）时从 1 递增，保证不撞名；
  - 文件名按 `(ts, seq)` 字典序即可反映"代"的新旧，`_prune_child_logs` 直接排序截断。
- **保留**：最近 **5 代**（每代 stdout+stderr 两文件，共 ≤10 文件）。更老的由 `_prune_child_logs` 在每次 `start_service` 前清理。
- **历史遗留**：旧固定名 `watchdog_child_stdout.log(.prev)` 为 09-03 事故现场保留物，新代码不再写入、prune 不认旧命名，故不动它们（保留死亡现场可观测）。

## §3 句柄锁模拟测试输出（tmp/e2/test_redirect_fallback.py）

真实场景模拟：起 Python 子进程**握持旧固定名文件句柄** → 跑真实 `start_service()` 逻辑（用 tmp/e2/fakeproj 的假 `main.py` 作 Start-Process 目标，monkeypatch `BASE`/`resolve_python`，**不碰真 main.py / 8899**）。

```
A) 已起握持进程 PID=55332 锁住固定名 watchdog_child_stdout.log
A) start_service(唯一名+重定向) -> True | 目标进程启动(marker=True)
A) 新增日志（唯一名，非固定名）: ['watchdog_child_stderr_20260903_233546_1.log', 'watchdog_child_stdout_20260903_233546_1.log']
2026-09-03 23:35:50  重定向启动异常 模拟重定向失败(句柄锁)，重定向不可用，降级启动（不带重定向）
B) start_service(重定向失败->降级) -> True | 目标进程启动(marker=True)
B) 调用[0]: 带重定向
B) 调用[1]: 无重定向(降级)
B) watchdog.log 已记录降级: ['2026-09-03 23:35:50  重定向启动异常 模拟重定向失败(句柄锁)，重定向不可用，降级启动（不带重定向）']
== 全部断言通过 ==
清理完成（只杀自起握持进程 + 删自生文件）
```

- **测试 A（修复核心）**：僵尸握持**固定名**不再阻塞——`start_service` 用按次唯一文件名，带重定向正常启动，目标进程 marker 落盘。09-03 故障模式（固定名被锁 → 整体失败）从根上消除。
- **测试 B（降级铁律）**：即使带重定向因任何原因失败（模拟句柄锁抛异常）→ 记录"降级启动"日志 → **不带重定向重试 → 目标进程照样启动成功**（marker=True）。
- 断言：A 唯一名生成、B 恰为"1 次带重定向失败 + 1 次降级"、watchdog.log 记降级、目标进程均启动。
- 清理：仅杀自起握持进程 PID 55332、删自生唯一日志与 marker；`data/logs` 无测试残留；未动 watchdog 11468 与任何既有进程。

## §4 生效条件与风险

- **生效条件**：改动在 **watchdog 下一次启动**后加载（红线禁止本轮重启 watchdog）。当前 `watchdog 11468`（11:35 启动）为老代码进程，仍在运行；其下次执行/重启即用新逻辑。本块不注册任务、不改计划任务。
- **降级路径权衡（已声明）**：不带重定向启动时，子进程 stdout 继承 PowerShell 被 `subprocess.run(capture_output=True)` 捕获的管道——若子进程大量写控制台且无人读可能阻塞。本项目服务输出量小、且重定向正常时不会走此路径；降级是"重定向不可用时的最后手段"，铁律优先保证恢复启动成功。报告如实披露此权衡。
- 未触碰：`app/*`、`main.py`、git、网络、既有进程/8899。

## §5 复验清单

- `py -3.13 -m py_compile tools/service_watchdog.py`：PASS（rc=0）
- 句柄锁 + 降级模拟测试：PASS（A/B 全断言通过）
- CRLF=0（LF）；行数 409→444
- 无残留：`data/logs` 仅旧历史文件、无测试新文件；无残留握持进程；watchdog 11468 未被误杀
