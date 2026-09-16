# X1 watchdog P0：输出重定向 + 启动豁免

- 日期：2026-09-02 23:5x；背景：Z6 诊断（`docs/reports/half_dead_loop_diag.md`）定案——51 次连环杀 = 上游行情源故障 + watchdog l2 把网络故障误判为引擎死亡；且重启实例无输出重定向，死亡现场不可观测。
- 范围：只改 `tools/service_watchdog.py` 一个文件，落地 Z6 §6 的 ①② 两个 P0。
- 红线遵守：未杀/重启/启动任何进程（现跑 watchdog 4656、8899 属主 44760 均未碰）；未 git 写；未出网；未碰 app/*、main.py、audit、app.lock；三级判据阈值（5s/90s/10min/7天）一字未动。

## §1 改动清单（diff 级）

### ① 输出重定向（`start_service()` + 新增 `rotate_child_logs()`）

**新增常量**（`service_watchdog.py` 常量区）：
```python
LOG_DIR = os.path.join(BASE, "data", "logs")
CHILD_STDOUT = os.path.join(LOG_DIR, "watchdog_child_stdout.log")
CHILD_STDERR = os.path.join(LOG_DIR, "watchdog_child_stderr.log")
```

**新增 `rotate_child_logs()`**：重启前把上一代子进程日志改名 `.prev`，只保留两代（`.log` 与 `.log.prev`）：
```python
def rotate_child_logs():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass
    for p in (CHILD_STDOUT, CHILD_STDERR):
        try:
            if os.path.exists(p):
                prev = p + ".prev"
                if os.path.exists(prev):
                    os.remove(prev)
                os.rename(p, prev)
        except Exception:
            pass
```

**修改 `start_service()`**（Start-Process 命令追加两个重定向参数，先轮换再启）：
```diff
     py = resolve_python()
+    rotate_child_logs()
     cmd = ("Start-Process -FilePath '%s' -ArgumentList 'main.py' "
-           "-WorkingDirectory '%s' -WindowStyle Hidden" % (py, BASE))
+           "-WorkingDirectory '%s' -WindowStyle Hidden "
+           "-RedirectStandardOutput '%s' -RedirectStandardError '%s'" %
+           (py, BASE, CHILD_STDOUT, CHILD_STDERR))
```

### ② 启动豁免（`startup_grace_applies()` + `main()` 分支）

**新增常量**：`STARTUP_GRACE = 180`（秒）。

**`load_state()`** 增加 `restart_ts` 字段读取；**新增三个函数**：
```python
def restart_age_seconds(st):
    ts = st.get("restart_ts", "")
    if not ts: return None
    try: dt = datetime.datetime.strptime(ts, TS_FMT)
    except Exception: return None
    return (datetime.datetime.now() - dt).total_seconds()

def in_startup_grace(st):
    age = restart_age_seconds(st)
    return age is not None and 0 <= age < STARTUP_GRACE

def startup_grace_applies(st, levels):
    if not in_startup_grace(st): return False
    if levels.get("l1_tcp") != "ok": return False
    if not str(levels.get("l2_overview", "")).startswith("time_stale"): return False
    if not str(levels.get("l3_audit", "")).startswith("audit_age"): return False
    return True
```

**`main()`** 增加两处：① 失败累计前插入豁免分支——豁免生效则记 `last_status="grace"`、计数保持、日志打 `启动豁免(l2)` 并 `return 0`；② `recover()` 返回 `"restarted"` 时写 `st["restart_ts"] = now_str()`（进入 180s 豁免期）。

## §2 豁免实现选型理由

选 **recover() 记 `restart_ts` 到 `tmp/h1_watchdog_state.json`（方案 a）**，不用 Get-CimInstance 取进程 CreationDate（方案 b）：
1. **零额外 PowerShell 调用**：watchdog 已频繁调 powershell（cmdline_has），方案 b 每次检查多一次 CimInstance 查询，增加延迟与失败面；方案 a 纯状态文件判断，一次读到内存。
2. **不依赖"目标进程是谁"的推断**：重启后新 PID 需反查端口才能定位（recover 里已做、有怪癖），豁免期可能在进程 PID 稳定前就开始；`restart_ts` 直接以"watchdog 发起重启的时刻"为准，与 PID 无关，语义更贴合"实例出生"。
3. **可审计**：`restart_ts` 落盘在状态文件，与 `last_status`/`last_check` 同源，日志打"启动豁免(l2)"字样，事后可回查每次重启→豁免窗口。
4. **豁免范围收紧**：只保护 `l2_overview` 的 `time_stale`（超时）一条——正是 Z6 定案的"上游源故障被 l2 误判"路径；l1 端口没起来 / l3 审计断更 / l2 返回 http 错误仍正常累计，避免豁免掩盖真死。

## §3 测试输出（全离线，`tmp/x1/test_grace.py`）

测试安全：`tmp/h1_watchdog_state.json`（现跑 watchdog 4656 活文件）**只读取样，写入一律走 tmp/x1/ 副本**；`recover` 全程 mock（`lambda: "noop"`）杜绝测试误触发真实重启；`LOG_FILE` 重定向到 `tmp/x1/test_watchdog.log`。

```
真 state 只读取样（不写回）: consecutive_failures=0 restart_ts=<无>

==== 分支1：restart_ts 新鲜(<180s)，仅 l2 超时 → 应豁免（不累计） ====
  启动豁免(l2)：实例出生 61s，l2 超时不累计（连续失败保持 2）
  返回码=0 | 连续失败=2（保持）| recover=0 | status=grace        ✅
==== 分支2：restart_ts 过期(>180s)，仅 l2 超时 → 应正常累计（不豁免） ====
  异常 3/3：l1_tcp=ok | l2_overview=time_stale_75s | l3_audit=audit_age_3s
  返回码=1 | 连续失败→触发recover | recover=1 | status=recovered  ✅
==== 分支3：新鲜 + l1 也失败 → 不应豁免（l1 不受保护） ====
  返回码=1 | 连续失败 1→2 | recover=0                                ✅
==== 分支4：新鲜 + l3 也失败 → 不应豁免（l3 不受保护） ====
  返回码=1 | 连续失败 1→2 | recover=0                                ✅
==== 分支5：新鲜 + l2 为 http 错误（非 time_stale 超时）→ 不应豁免 ====
  返回码=1 | 连续失败 1→2 | recover=0                                ✅
==== 分支6：restart_ts 缺失（老状态）→ 无豁免，正常累计 ====
  返回码=1 | 连续失败 1→2 | recover=0                                ✅
==== 分支7：新鲜 + l2 完全健康 → 走正常健康分支清零 ====
  健康 OK（连续失败已清零）| 返回码=0 | status=ok                    ✅

ALL 7 分支 PASSED ✅
```

`py -3.13 -m py_compile tools/service_watchdog.py`：通过。

## §4 生效方式说明

- **新代码何时生效**：watchdog 由计划任务每 5 分钟拉起一次（每次都是新进程读最新代码）——但**当前正在跑的 watchdog(4656) 读的是旧代码**，其循环内不会自动重载。**下次计划任务触发启动的新 watchdog 进程**即加载本改动。
- **需要谁点头**：无需人工重启任何进程即可让"下一个 watchdog 实例"生效；但若希望**立刻**让本改动接管（含立刻获得启动豁免能力），需由验收方/调度方在下次计划任务前确认 watchdog 实例确实按计划被拉起（现 4656 在跑，属预期，本块严禁为生效去杀它）。
- **生效后行为变化**：① 每次恢复重启的子进程 stdout/stderr 落入 `data/logs/watchdog_child_{stdout,stderr}.log`（两代轮换，死亡现场可观测）；② 重启后 180s 内 l2 超时不累计（防 Z6 定案的连环杀路径），日志见"启动豁免(l2)"。
