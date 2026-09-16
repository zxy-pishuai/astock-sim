# F5 单实例锁 / watchdog / 睡眠误报 / cron 撞车（2026-09-08）

## 0. 摘要
四个问题逐一修复：① 锁被纯 UI 进程劫持（端口健康=唯一权威，写锁移到 serve 前）；
② watchdog kill 后不校验（加轮询确认 + 孤儿留档）；③ 睡眠误报 CRITICAL（boot_time
每次重算，GetTickCount64 睡眠暂停即唤醒检测）；④ 晚间 cron 撞车（watchdog 侧加
20:00-21:30 窗口豁免防误杀；触发器时间调整被 UAC 拦截，命令备好待管理员执行）。
服务未重启（三个文件的改动下次启动生效）；38504 孤儿进程未动，留待用户处理。

## 1. 问题 1：锁被 UI 进程劫持（main.py）
### 根因
- `_acquire_lock` 判据 `alive and healthy` 要求 PID 存活**且**端口健康才复用；当锁内旧
  PID 已死但端口健康、或锁文件缺失时，直接清锁写自己 PID → 随后 `_is_running` 分支
  "检测到已有实例→只开窗口 return" → **app.lock 从此指向纯 UI 进程**（今日 38504 实锤）。
- `find_free_port` 在取锁前执行（main.py:169 vs 172）→ 端口决策基于过期状态。

### 修复
- `_acquire_lock` 重构为**端口健康=唯一权威**：端口健康 → `(False, 已有实例)` **绝不碰锁文件**；
  端口不健康 → `(True, 可接管)`，仅当旧 PID 已死才清锁（旧 PID 存活=僵死，保留锁、serve 前覆盖）。
- **写锁移到真正 serve 前**（`_write_lock`，在 `_is_running` 判定之后、`srv.serve()` 之前）；
  `find_free_port` 移到取锁成功之后。
- **启动自检**：serve 后检查本进程持有锁（app.lock PID==os.getpid()）且端口已监听，
  否则主动退出，杜绝"不开服务也不退"的孤儿。

### 校验（副本锁文件，不碰真实 data/app.lock；4 场景全过）
| 场景 | 结果 |
|---|---|
| 端口 8899 健康 | ok=False（已有实例）且锁文件内容原样未动 ✓ |
| 端口不健康 + 旧 PID 已死 | ok=True 且清锁 ✓ |
| 端口不健康 + 旧 PID 存活（僵死） | ok=True 且锁保留（serve 前覆盖写）✓ |
| `_write_lock` | 写当前 PID ✓ |

## 2. 问题 2：kill 后不校验（tools/service_watchdog.py）
### 修复
- `kill_pid` 改为 taskkill /F 后**轮询确认进程消失**（OpenProcess 探测，最多重试 2 次
  间隔 2s，单轮最多等 2s）。
- kill 失败（重试后仍存活）→ `CRITICAL` 记入 watchdog.log + 孤儿 PID 入状态文件
  `h1_watchdog_state.json.orphan_pids`（watchdog 只读 audit 红线不变，audit 落点归引擎侧心跳机制）。
- recover() 半死僵尸分支接入新 kill_pid。

### 校验
- 真杀测试：spawn 子进程 → `kill_pid` → taskkill rc=0、`p.poll()=1`（进程**已终止**）。
- 说明：测试中 `OpenProcess` 探测在 Popen 持有句柄时短暂误报存活（句柄未回收）；
  watchdog 不持有目标进程句柄，真实场景探测正常，轮询多轮兜底足够。

## 3. 问题 3：睡眠误报 CRITICAL（app/trader.py）
### 根因（已实证）
- `_wd_get_boot_time()` 用 `GetTickCount64`（睡眠期间暂停）推算开机时刻，但
  `_check_watchdog_heartbeat` **首次计算后缓存**（`if None` 才算）。
- 时间线：12:05 服务启动 → 12:15 首次算 boot_time≈12:05（缓存）→ 15:58 睡眠、
  20:14 唤醒 → 20:15 检查时心跳 mtime(15:55) **>** 缓存 boot_time(12:05) → 2b 判据
  不命中 → 误报 CRITICAL stale（实锤：System 日志 Kernel-Power 42 / Power-Troubleshooter 1）。

### 修复
- **每次检查重算 boot_time**：GetTickCount64 睡眠暂停 → 唤醒后 `time.time()-uptime`
  即"最近一次开机/唤醒时刻"，等价于派单"单调时钟跳变检测"。心跳 mtime 早于该时刻 →
  `INFO reason=boot_or_sleep`（不进 CRITICAL 状态机）。

### 校验（公式模拟）
- 唤醒后重算 boot_time=20:10（唤醒时刻）；心跳 15:55 < 20:10 → INFO ✓；
  旧实现（缓存 12:05）→ 不命中 → CRITICAL（=昨晚误报复现）✓。

## 4. 问题 4：晚间 cron 撞车 → l2 超时 → 误杀前科
### 修复（watchdog 侧，已完成）
- 新增 `CRON_WINDOWS=(20:00, 21:30)` + `cron_grace_applies`：**窗口内仅 l2_overview
  time_stale 不累计连续失败**（l1/l3、l2-http 仍计）——防 2026-09-01 20:45/50/55
  三连超时 → 误判僵尸 → kill+重启重演。
- main() 新增 cron 豁免分支（在启动豁免之后），日志打 `cron窗口豁免(l2)`。

### 触发器错峰（受 UAC 拦截，待管理员执行）
- 目标：forward_eval 20:30 → **21:00**（与 risk_daily 20:00-20:20 执行窗口错开 40 分钟；
  ml_scores_ledger_daily 触发器仅 16:30 Weekly，晚间 20:20 那次为非计划触发）。
- 实测：当前会话非管理员（`IsInRole(Administrator)=False`，UAC EnableLUA=1），
  `schtasks /Change /TN forward_eval /ST 21:00` 弹密码提示无法完成。
- **待执行命令**（管理员 PowerShell 或计划任务 UI）：
  `schtasks /Change /TN forward_eval /ST 21:00`
- 在管理员执行前，watchdog 侧 20:00-21:30 窗口豁免已生效 → 即使撞车也不会误杀。

## 5. 问题 7：watchdog 退出码语义（tools/service_watchdog.py）
- 修改前：`rc = 1 if not ok else 0` → 每次健康检查异常都返回 1 → 污染 Task Scheduler
  LastTaskResult（今日 20:45 那次 LastTaskResult=1 误导排查）。
- 修改后：健康 / 服务异常（已记日志）/ 启动豁免 / cron 豁免 / 恢复执行 → **0**；
  仅 `recover()` 返回 `start_failed`（恢复失败需人工介入）→ **1**；
  自身故障兜底保持 TIMEOUT-BAIL `os._exit(3)`。

## 6. 校验汇总
- `py -3.13 -m py_compile main.py tools/service_watchdog.py app/trader.py` 通过。
- main.py 锁 4 场景 / watchdog in_cron_window 6 点 / cron_grace_applies 3 断言 /
  kill_pid 真杀验证 / trader boot_time 公式——全部通过（输出见上文各节）。

## 7. 生效方式与遗留
- **生效条件**：main.py / service_watchdog.py / trader.py 改动需**服务与 watchdog 下次
  启动才生效**；当前运行中的 13108（服务）与 watchdog 读旧代码。由用户决定重启时机，
  本单未自行重启任何进程。
- **38504 孤儿**：未自行 kill，留待用户处理（其已无锁无端口，不影响交易；下次
  watchdog 自然重启或人工清理均可）。
- **cron 触发器**：forward_eval 时间调整待管理员执行（命令见 §4）。
- 已知窗口 20:00-21:30 若未来 cron 时间再变，需同步更新 `CRON_WINDOWS`。

## 8. 涉及文件
- 改：`main.py`、`tools/service_watchdog.py`、`app/trader.py`
- 报告：本文件
