# H1b｜watchdog 恢复分支死锁根治（返工交付报告）

- 日期：2026-09-05 22:5x
- 项目：`C:\Users\26838\A股模拟盘`（纯标准库，git 可用只读）
- 上一轮：H1 判 0 分未交付（四项交付物全缺、生产文件零改动）——本块按硬门逐项交付
- 改动文件：`tools/service_watchdog.py`（唯一）
- 进程纪律：未杀/未重启/未启动任何进程；未碰 schtasks、app/*、main.py、data/audit、data/app.lock、data/logs 既有文件、tmp/watchdog.log

---

## §0 判据与查重

**查重门（tmp/pack19_dup.log）**：`H1b 2026-09-05 22:39:xx GATE-OK`（docs/reports/h1b_watchdog_deadlock.md 不存在，放行）。

**开工前 git 检查**：`git status --porcelain -- tools/service_watchdog.py` → 空输出（文件非 M，无人并行改动），放行。

**`rg -n "^# (H1|H1b)" docs/reports/` 命中原文**：

```
docs/reports/watchdog_hardening.md:1:# H1｜看门狗硬化 + 半死状态根治（修复型）— 交付报告
```

该报告（2026-08-31 00:03 前后，8/31 那轮 H1）内容为三级判据首建与 ThreadPoolExecutor 守护，**与本块的"start_service 管道死锁"无关**，仅标题撞名（H1→H1b 即因此改号）。

与 `docs/reports/e2_watchdog_redirect.md`（9/3，固定文件名句柄锁）声明区别：E2 修的是**重定向目标文件名被僵尸握持**导致 Start-Process 失败；本块修的是**`capture_output=True` 管道被孙进程继承写端 → 超时 kill 后 `communicate()` 无超时等待 → 永久挂起**。两者是独立故障模式，本块未回退 E2 的唯一文件名+降级逻辑（见 §2 diff，start_service 保留 E2 重定向与降级框架）。

---

## §1 根因取证

验收方三次现场（今日 12:15 / 22:00 开机后）复现：watchdog 成功拉起 main.py（8899 健康），但自身在写下"端口 8899 关闭（全死），直接重启"后**永久挂起**；`h1_watchdog_state.json` 的 `consecutive_failures` 走到 3→4→5，`restart_ts` 始终为空 → `STARTUP_GRACE=180s` 豁免从未生效；`MultipleInstancesPolicy=IgnoreNew` 吞掉后续每 5 分钟激活 → 守护永久失效。

**代码级取证（本次独立复核，支持验收方判断）**：

- 改前 `start_service()`（L378-391）两处 `subprocess.run(["powershell", ...], capture_output=True, timeout=30)`。
- Python 3.13 `subprocess.run` 超时路径源码：`TimeoutExpired` → `process.kill()` → **无超时 `communicate()`** 等待管道 EOF。只要由 powershell `Start-Process` 拉起的孙进程 `main.py` 继承了 stdout/stderr 管道写端，EOF 永不发生 → 永久挂起。
- 本块自检 2 以三层复刻（父 Popen→middle→grandchild 继承管道写端）实证该机制：线程栈实锤卡在 `communicate → _communicate → self.stdout_thread.join`（见 §3 自检 2 证据）。
- `main()` 无总时限兜底、`recover()` 无 try/finally 状态复位、陈旧计数不归零——三者叠加使一次挂起即永久失守。

结论：**已证实**（代码+复现+线程栈三重证据，与验收方一致）。

---

## §2 改动逐条（git diff 原文）

改动 6 处（对应修复要求 1-6），diff 统计 `122 insertions(+), 59 deletions(-)`，完整 diff 见 `tmp/pack19/H1b_evidence/git_diff.txt`（261 行），关键 hunk 原文：

```diff
@@ import 区
 import socket
 import subprocess
+import threading
 
@@ +_run_cmd（新增，L363-385）
+def _run_cmd(cmd, timeout=20):
+    """H1b①②：启动子进程的唯二安全通道——绝不捕获管道（DEVNULL），wait(timeout) 有界，
+    超时 kill 后 wait(5) 收尸再返回 (-99, 'timeout')。
+    铁律：恢复动作永远不许被重定向/输出收集阻塞。
+    Windows 下 close_fds=True 安全且为默认（无 pipe 继承，DEVNULL 走 NUL 设备，
+    孙进程不会持有任何管道写端）。返回 (rc, detail)。"""
+    proc = subprocess.Popen(
+        ["powershell", "-NoProfile", "-Command", cmd],
+        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
+        creationflags=CREATE_NO_WINDOW, close_fds=True)
+    try:
+        rc = proc.wait(timeout=timeout)
+    except subprocess.TimeoutExpired:
+        try:
+            proc.kill()
+        except Exception:
+            pass
+        try:
+            proc.wait(timeout=5)          # 收尸：有界，绝不无超时等待
+        except Exception:
+            pass
+        return -99, "timeout>%ds" % timeout
+    return rc, ("ok" if rc == 0 else "rc=%s" % rc)

@@ start_service()（原 L374-396 重写为 DEVNULL 通道，保留 E2 重定向+降级）
-    try:
-        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
-                           capture_output=True, timeout=30,
-                           creationflags=CREATE_NO_WINDOW)
-        if r.returncode == 0:
-            return True
-        log("重定向启动 rc=%s，重定向不可用，降级启动（不带重定向）" % r.returncode)
-    except Exception as e:
-        log("重定向启动异常 %s，重定向不可用，降级启动（不带重定向）" % e)
-    # 降级：不带重定向重试（铁律：恢复动作永不因重定向失败而阻塞）
-    try:
-        subprocess.run(["powershell", "-NoProfile", "-Command",
-                        "Start-Process -FilePath '%s' -ArgumentList 'main.py' "
-                        "-WorkingDirectory '%s' -WindowStyle Hidden" % (py, BASE)],
-                       capture_output=True, timeout=30,
-                       creationflags=CREATE_NO_WINDOW)
-        return True
-    except Exception as e:
-        log("启动命令执行失败: %s" % e)
-        return False
+    # H1b①②：绝不用 capture_output（管道被孙进程 main.py 继承写端 → 超时 kill 后
+    #   communicate() 无超时等待 = 永久挂起，今日 12:15/22:00 两次事故根因）。
+    rc, d = _run_cmd(cmd, timeout=20)
+    if rc == 0:
+        return True
+    log("重定向启动 %s（rc=%s），重定向不可用，降级启动（不带重定向）" % (d, rc))
+    # 降级：不带重定向重试（铁律：恢复动作永不因重定向失败而阻塞）
+    rc2, d2 = _run_cmd(
+        "Start-Process -FilePath '%s' -ArgumentList 'main.py' "
+        "-WorkingDirectory '%s' -WindowStyle Hidden" % (py, BASE), timeout=20)
+    if rc2 == 0:
+        return True
+    log("降级启动 %s（rc=%s），启动命令执行失败" % (d2, rc2))
+    return False

@@ recover()（各出口补耗时日志，H1b③）
-    log("服务实际健康（overview=%s audit=%s），跳过恢复（防双实例）" % (ov_d, au_d))
+    log("服务实际健康（overview=%s audit=%s），跳过恢复（防双实例，耗时=%.1fs）"
+        % (ov_d, au_d, time.time() - _t0))
...
-    log("8899 被非 main.py 进程占用（PID=%s），不终止、跳过恢复" % pid_on_port(PORT))
+    log("8899 被非 main.py 进程占用（PID=%s），不终止、跳过恢复（耗时=%.1fs）"
+        % (pid_on_port(PORT), time.time() - _t0))
...
-    log("恢复启动执行失败，请人工介入" )
+    log("恢复启动执行失败，请人工介入（耗时=%.1fs）" % (time.time() - _t0))
...
-    log("恢复完成：旧PID=%s 新PID=%s（动作=%s）" % (old_pid, new_pid, action))
+    log("恢复完成：旧PID=%s 新PID=%s（动作=%s，耗时=%.1fs）"
+        % (old_pid, new_pid, action, time.time() - _t0))

@@ main()（H1b⑤ timer 前移 + H1b④ 复位 + try/finally + 收尾心跳）
     args = ap.parse_args()
 
+    # H1b⑤ 进程级自退兜底：总时限 90s，未知阻塞 → log TIMEOUT-BAIL 后 os._exit(3)
+    #   （与 H2 的 ExecutionTimeLimit=PT2M 互为双保险；daemon 线程不阻塞主流程。
+    #   必须在本函数任何可能阻塞的调用（含 health_check）之前启动——自检3曾证明
+    #   原位置在 health_check 阻塞时永远执行不到，兜底失效）。
+    def _bail():
+        log("TIMEOUT-BAIL watchdog 总时限超时（>90s），os._exit(3)")
+        try:
+            os._exit(3)
+        except Exception:
+            pass
+    _bail_timer = threading.Timer(90.0, _bail)
+    _bail_timer.daemon = True
+    _bail_timer.start()
+
     ok, levels = health_check()
...
     st = load_state()
+    # H1b④ 陈旧计数复位：上一轮若卡死/异常未收尾，加载到 >=THRESHOLD 先归零再 log
+    #   ——日志中永不得再出现 "异常 N/3"（N>3）。
+    if st.get("consecutive_failures", 0) >= RECOVER_THRESHOLD:
+        log("陈旧计数复位：consecutive_failures=%d（>=%d）→ 0（上一轮可能未正常收尾）"
+            % (st.get("consecutive_failures", 0), RECOVER_THRESHOLD))
+        st["consecutive_failures"] = 0
+
+    rc = 1 if not ok else 0
+    try:
         # R2-P0.3：运行即写心跳；...
         if ok:
             ...
             st["consecutive_failures"] = 0
             st["last_status"] = "ok"
             st["last_check"] = now_str()
             save_state(st)
             log("健康 OK：%s（连续失败已清零）" % detail)
-            return 0
+            rc = 0
+            return 0
...
-    return 1 if not ok else 0
+    finally:
+        # H1b④ 状态必复位：即使 recover() 抛异常也保存状态（恢复后计数已清）
+        try:
+            save_state(st)
+        except Exception:
+            pass
+        # H1b⑥ touch_heartbeat 语义保持"运行即写"，收尾再写一次，失败不影响退出
+        try:
+            touch_heartbeat()
+        except Exception:
+            pass
+    return rc
```

**逐条对照修复要求**：

| 要求 | 落地 |
|---|---|
| 1 不捕获管道 | `start_service` 两处全走 `_run_cmd`（DEVNULL）；`close_fds=True` 结论：Windows 3.13 下安全且为默认（无 pipe，DEVNULL 走 NUL 设备，孙进程不持管道写端），已显式传入 |
| 2 恢复链路硬上限 | `_run_cmd`：`wait(timeout)` 超时 → `kill()` → `wait(timeout=5)` 收尸，**绝不无超时等待**；recover 无任何无界调用 |
| 3 每出口落日志 | recover 四出口（skip_healthy/skip_foreign/start_failed/restarted）全部含 旧PID/新PID/耗时；`log("恢复完成：…")` 有界出现 |
| 4 状态必复位 | `main()` try/finally 保证 save_state；启动时陈旧计数 ≥THRESHOLD 先归零并 log；日志不会出现"异常 N/3"(N>3) |
| 5 进程级自退兜底 | `main()` 开头 90s `threading.Timer` → `log("TIMEOUT-BAIL …")` → `os._exit(3)`（**置于 health_check 之前**，自检 3 证明原位置失效已修正） |
| 6 心跳语义 | `touch_heartbeat()` 运行即写（try 内）+ finally 收尾再写，失败不影响退出 |
| 7 纯标准库/阈值不动 | 仅加 `import threading`；三级判据阈值、RECOVER_THRESHOLD=3、STARTUP_GRACE=180 取值未动 |

---

## §3 七项自检门输出

证据文件：`tmp/pack19/H1b_evidence/selfcheck_2345.txt`、`selfcheck2f_6.txt`、`selfcheck_2345.json`、`selfcheck2_frames.json`、`selfcheck6_verify.txt`。

| 门 | 命令/方法 | 结果 |
|---|---|---|
| 1 py_compile | `py -3.13 -m py_compile tools/service_watchdog.py` | 通过（exit 0，补丁⑥后复验） |
| 2 死锁反证 | 三层复刻（父 Popen→middle→grandchild 继承管道写端） | **改前 HUNG**：线程栈 `communicate → _communicate → self.stdout_thread.join`（孙进程持管道写端）；**改后**同场景 0.5s 返回 rc=0；超时分支 `Start-Sleep 10s`+timeout=3 → **3.1s 返回 rc=-99**（绝不无超时等待） |
| 3 TIMEOUT-BAIL | 子进程 monkeypatch `health_check` 阻塞 300s | exit_code=**3**、elapsed=**90.2s**、日志含 `TIMEOUT-BAIL watchdog 总时限超时（>90s），os._exit(3)` |
| 4 计数复位 | 隔离状态文件预置 `consecutive_failures=5` | 日志含 `陈旧计数复位：consecutive_failures=5（>=3）→ 0`；无"异常 6/3" |
| 5 恢复日志完整性 | 模拟端口全死→start_service 成功→新 PID 67890 | 日志含 `恢复完成：旧PID=12345 新PID=67890（动作=restarted，耗时=1.0s）`；隔离状态文件 `restart_ts=2026-09-05 22:44:07` 已落盘 |
| 6 外部性回归 | 真实 `--verify` | rc=0，`verify: OK | l1_tcp=ok | l2_overview=time_age_0s | l3_audit=audit_age_254s`；真实状态文件 SHA256 前后一致（`0507c6a9…1eca`）；不写状态、不恢复、不 touch 心跳 |
| 7 全程隔离 | 状态/心跳/日志全 monkeypatch 到 `tmp/pack19/H1b_evidence/isolation/` | 未触发真实恢复、未重启服务、未碰真实状态文件 |

---

## §4 外部性回归

- `watchdog.log` 格式：`log()` 函数本块未动（diff 无其 hunk），仍为 `YYYY-MM-DD HH:MM:SS  <msg>` 每轮一次追加；下游 `trader._monitor` 依赖的心跳文件 `tmp/watchdog_heartbeat.ts` 语义不变（运行即写+finally 收尾再写）。
- `--verify` 只读模式：不写状态、不恢复、不写心跳（自检 6 哈希证据）。
- R2-P0.3 心跳告警逻辑（`R2-P0.3 调度断更告警`）保留原样。
- E2 唯一文件名重定向与降级框架保留（diff 内可见）。
- 三处残留 `capture_output=True`（L262/284/294）均为 **netstat 只读探测**（`cmdline_has`/`pid_on_port`），必须读输出解析 PID、无孙进程、有 `timeout=15`——不属本块"启动子进程"死锁模式，保留。

---

## §5 生效条件

`tools/service_watchdog.py` 改动**下次计划任务运行即生效**（schtasks 每次运行读新文件，无需重启服务/引擎）。已确认现跑 watchdog 实例读的是旧代码——本块未杀/未重启任何进程，旧实例下次自然被 schtasks 新轮替换。完整生效链条：下次 watchdog 运行 → TIMEOUT-BAIL/DEVNULL 兜底全部在线。

---

## §6 未解与风险

1. **仍需 H2 的 `ExecutionTimeLimit=PT2M` 做双保险**：本块 TIMEOUT-BAIL 是进程内兜底（90s 自退），H2 是调度层硬时限（schtasks 2 分钟强杀）——两者互为备份，缺 H2 时若 Python 自身被不可杀阻塞（极端），仍有 IgnoreNew 名额风险。
2. **`STARTUP_GRACE` 从未生效的历史**：今日两次事故中 `restart_ts` 为空导致豁免从未生效——本块已通过"恢复完成→restart_ts 落盘（try/finally 保证）"修复，但需下个真实恢复场景实证。
3. **Start-Process 成功路径 rc=0 但 main.py 未起**：`start_service` 只保证 Start-Process 命令本身成功，服务健康由后续 30s 新 PID 轮询兜底（既有逻辑保留）；若 30s 内 main.py 未写锁且端口反查也失败，`new_pid=0` 已记录在日志（不会挂死，下一轮重判）。
4. **netstat 探测的 15s 超时**：极端网络下 `pid_on_port`/`cmdline_has` 各自最多 15s，在 90s 总时限内（探测在主线程，健康检查+恢复预算 ~75s < 90s）——若实测发现某环境 netstat 慢导致 TIMEOUT-BAIL 误触发，可下调其 timeout，本块未动。

---

## §7 交付物清单（A/B/C/D 自查）

| 项 | 路径 | 状态 |
|---|---|---|
| A 备份 | `tmp/pack19/backup/service_watchdog.py.20260905_223600`（改前 SHA256 `194EECC4…F857`；改后 `8045449E…DDA86`，双哈希已录） | ✓ |
| B 代码改动 | `tools/service_watchdog.py`（`git diff` 122+/59-，完整 261 行存 `tmp/pack19/H1b_evidence/git_diff.txt`；**启动子进程路径已无 capture_output**） | ✓ |
| C 报告 | `docs/reports/h1b_watchdog_deadlock.md`（本文件） | ✓ |
| D 取证 | `tmp/pack19/H1b_evidence/`：git_diff.txt / selfcheck_2345.txt+json / selfcheck2f_6.txt / selfcheck2_frames.json / selfcheck6_verify.txt | ✓ |

附带：查重日志 `tmp/pack19_dup.log`（GATE-OK）；补丁脚本 `tmp/r2_patch_h1b*.py`（一次性，非白名单产物，仅留档）。
