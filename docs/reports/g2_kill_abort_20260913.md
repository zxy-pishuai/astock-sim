# G2 kill 失败即中止重启（消除双实例窗口）—— 2026-09-13

> 依据：tmp/watchdog.log 00:20 事故原文 + main.py `_acquire_lock/_write_lock` 源码审查。
> 改动：tools/service_watchdog.py。**未杀任何生产进程**（验收仅杀自启假 PID 子进程）、
> 未碰 data/app.lock 生产文件、未改 main.py（审查结论：锁语义无缺陷，修法在 watchdog 侧）。

## §0 事故链复盘（为什么出现"孤儿 + 新实例"窗口）

```
00:20:04  半死僵尸 PID=43336，终止后重启
00:20:15  CRITICAL kill_pid 失败：PID 43336 重试后仍存活（孤儿进程），已记入状态文件
00:20:23  恢复完成：旧PID=43336 新PID=52044（动作=restarted）   ← 明知 kill 失败仍启动了新实例
```

原代码 kill 失败后**只记 CRITICAL 就继续** `clear_lock() + start_service()`：
- `clear_lock()` 强删 data/app.lock → 新实例 52044 启动时 `_acquire_lock` 读不到锁、端口检查通过 → 正常接管；
- 旧 PID 43336 若仍在存活 → 与新实例并存（重复下单 / account.json 冲突 / SQLite 锁争用 / audit 交错）；
- 本次 43336 实际已被 taskkill 终止（后来自行退出的"孤儿"实为误报，见 §2），属运气未酿祸。

## §1 改动清单（tools/service_watchdog.py）

| 行 | 改动 |
|---|---|
| L246-261 | `load_state()` 扩展：保留 `orphan_pid/orphan_first_seen/orphan_pids/needs_human`（原白名单只回 4 键，孤儿状态跨轮次会丢） |
| L441-462 | `_pid_alive_wd()` 用 `GetExitCodeProcess` 判定（修复句柄残留假阳性，见 §2） |
| L488-505 | 新增 `kill_pid_tree()`：taskkill `/T /F`（杀子进程树），孤儿升级终止用 |
| L633-652 | 新增 `confirm_no_duplicate()` 三重确认：①8899 无监听 ②无 CommandLine 含 main.py 的存活进程 ③app.lock 持有者 PID 已不存在；任一不满足 → (False, 原因) |
| L697-708 | recover() kill 失败 → **`return "abort_orphan_alive"`**：记 orphan_pid/orphan_first_seen 入状态文件、**不 clear_lock、不启动** |
| L712-726 | recover() 启动前调用三重确认；不过 → `return "dup_abort"` + needs_human |
| L787-822 | main() 孤儿接管：孤儿已消失 → 清状态走正常流；仍存活 → 升级 `/T /F`；仍失败 → 只告警（needs_human）不重启 |
| L868-870 | main() 对 `start_failed/abort_orphan_alive/dup_abort` 一律 rc=1（需人工介入） |
| 3 处 subprocess | `text=True` 加 `errors="replace"`（taskkill/powershell 输出 GBK，PYTHONUTF8 环境按 utf-8 解码会抛 UnicodeDecodeError，实测复现） |

## §2 关键发现：`_pid_alive_wd` 句柄残留假阳性（00:20 误报的根因）

实验复现（自启假子进程 → taskkill）：
```
child pid 53276 alive(pre) True
taskkill rc=0, stdout="成功: 已终止 PID 53276 ... 的进程。"   ← taskkill 实际成功
alive after taskkill True                                      ← 但 OpenProcess 仍报"存活"！
```

- 原因：进程被终止后，其他进程未关闭的句柄使进程对象短暂存活，`OpenProcess`（
  PROCESS_QUERY_LIMITED_INFORMATION）仍能打开 → **假阳性**。
- 影响：00:20 的 `kill_pid 失败` CRITICAL 大概率是假象（43336 已被 taskkill 成功终止，
  但探测误报存活）→ 触发原代码"kill 失败仍强删锁 + 启动"连锁。修复后该路径不再存在。
- 修法：`GetExitCodeProcess` 判定——已终止进程返回真实退出码（≠259），存活进程返回
  `STILL_ACTIVE(259)`。实验验证：kill 后 `_pid_alive_wd` 正确返回 False。

## §3 app.lock 单实例语义审查（main.py）

**结论：`_acquire_lock`/`_write_lock` 无 race，锁语义本身健全**；00:20 双实例窗口是
watchdog 的 `clear_lock()` 强删所致，不是锁实现缺陷。

- **获取**：`_acquire_lock(port)`——端口健康=唯一权威（有服务在跑即拒绝）；端口不健康时读锁
  旧 PID：已死→清锁可接管；存活→"接管"（不删锁，serve 前覆盖写）。**无 TOCTOU race**（写锁
  时机 = serve 前，端口检查与写锁之间由单进程顺序保证）。
- **释放**：进程退出不主动删锁；陈旧锁由下一实例/守护方按"旧 PID 已死"清理。**无释放 race**。
- **陈旧判定**：`_pid_alive` 与 watchdog 的 `_pid_alive_wd` 同源——**同样受句柄残留假阳性影响**
  （main.py L185 附近）。若 watchdog 不删锁，而旧实例实际已死，新实例 `_acquire_lock` 读到
  "旧 PID 存活"（假阳性）→ 走"接管"分支（端口不健康）→ serve 前覆盖写锁 ✓ 不阻塞。
- **修法（已随 G2 落地，watchdog 侧）**：kill 失败不再 clear_lock；启动前三重确认；假阳性消除后
  `_acquire_lock` 的"旧 PID 存活"判定更可靠。**main.py 无需改动**（避免与并发 AI 冲突）。

## §4 预注册验收判据 → 逐条结论（tmp/g2_test/test_g2.py，13 用例 0 失败）

| # | 判据 | 结果 |
|---|---|---|
| T1 | confirm_no_duplicate 干净场景（端口空闲+无 main.py+无锁）→ (True,"ok") | **通过** |
| T2 | 端口监听 → 拒绝 `port_8899_listening` | **通过** |
| T3 | 存在 main.py 进程（模拟 CIM）→ 拒绝 `main_py_running_pids` | **通过** |
| T4 | lock 持有者存活 → 拒绝 `lock_holder_alive` | **通过** |
| T5 | recover kill 失败 → `abort_orphan_alive`、start_service **0 次**、状态含 orphan_pid+首见时间 | **通过** |
| T6 | recover 三重确认失败 → `dup_abort`、start_service **0 次** | **通过** |
| T7 | main 孤儿接管：真活假 PID（自启 `python -c sleep 60`）→ 升级 /T /F 成功 → 状态清除、rc=0 | **通过** |
| T8 | kill_pid_tree 不存在 PID → True | **通过** |

生产只读体检：`py tools/service_watchdog.py --verify` → `OK | l1=ok | l2=time_age_0s | l3=audit_age_1148s`。

## §5 上线后前瞻验收（30 天）

1. `tmp/h1_watchdog_state.json` 中 `orphan_pid` 一旦出现，`watchdog.log` 必须同日有
   `CRITICAL G2 kill_pid 失败：PID <id> ...` 行且状态含 `orphan_first_seen`（可定位具体 PID/时刻）；
2. `needs_human` 一旦出现（orphan 升级 /T /F 仍失败 或 三重确认未过），人工处置后手动清除；
3. 30 天内 audit/orphan 相关 CRITICAL 出现时，每次都能在 watchdog.log + 状态文件定位到
   具体进程与原因（本单已保证两个落点都带 PID 与时间）。

## §6 风险与回滚

- **风险**：kill 失败中止恢复后，若端口持续被半死进程占用且 /T /F 也杀不掉 → 服务停摆直至
  人工介入（needs_human 告警）。这是"宁可停摆不可双实例"的取舍（双实例=重复下单等实盘危害）。
- **回滚**：`git checkout HEAD -- tools/service_watchdog.py`（恢复原 kill 失败仅记 CRITICAL 的
  行为——不推荐，双实例窗口回归），改后 py_compile + --verify + 重跑 test_g2.py。

## §7 未做清单

- 未改 main.py（锁语义审查结论无缺陷；`_pid_alive` 的假阳性可由 G2 的 watchdog 侧修复
  覆盖——若后续要让 main.py 独立判定更稳，可同步 GetExitCodeProcess 化，本单不越界）；
- 未改 app/config.py、app/audit.py 及任何 data 文件；未杀/未重启生产服务。
