# K4 交付报告：主循环"停摆现场"轨迹

> 执行时间：2026-09-06 ｜ 范围：P2，只加可观测性，不改买卖判据
> 代码改动在工作区，未 commit；**改动只在下次服务重启生效**

## 首屏：git diff --stat

```
 app/trader.py | 274 +++++++++++++++++++++++++++++++++++++++++++++-------------
 1 file changed, 213 insertions(+), 61 deletions(-)
```

（新增 `tmp/pack20/K4_test_stall_trace.py` 为隔离单测。）

---

## §0 判据与查重

- 查重门：`tmp/pack20_dup.log` 追加 `K4 2026-09-05T18:34:23Z GATE-OK`（无 DUP-HIT）。
- 开工前 `git status -- app/trader.py` 确认 **NOT-M**（H4 已由 auto-commit `5f14c5a` 合入，无冲突）。
- 写白名单：`app/trader.py`、`tmp/pack20/K4_*`、`docs/reports/k4_stall_trace.md`。
- 禁碰清单全部遵守：未改 `app/config.py`、`app/audit.py`、`app/engine.py`、`app/scoring.py`、`app/tactics.py`、`app/updater.py`、`tools/service_watchdog.py`、`data/audit/*`、`data/app.lock`。
- 前端 `web/js/app.js` **未改**（理由见 §6）。

---

## §1 改动逐条

### 1.1 `__init__` 新增 8 个状态变量（`app/trader.py:148-155`）

```python
# K4：主循环阶段轨迹 + 停摆判定状态
self._lp_phase = "idle"              # 当前阶段名
self._lp_phase_since = time.time()   # 进入当前阶段时刻
self._lp_seq = 0                     # 循环轮次序号
self._lp_last_finished_phase = ""    # 上一个完成的阶段
self._lp_last_finished_at = 0.0      # 上一阶段完成时刻
self._lp_last_phase_ms = 0           # 上一阶段耗时(ms)
self._lp_stall_start = None          # 停摆开始时刻（None=未停摆）
self._lp_stall_last_warn = 0.0       # 上次停摆 WARN 时刻（节流）
```

### 1.2 新增 5 个私有辅助方法（`app/trader.py:299-410`）

| 方法 | 职责 | 异常处理 |
|---|---|---|
| `_lp_enter(phase)` | 进入阶段，记录阶段名与时刻 | try/except 静默 |
| `_lp_finish(phase)` | 完成阶段，计算耗时(ms)，重置为 idle | try/except 静默 |
| `_lp_flush()` | 原子落盘 `tmp/loop_phase.json`（临时名+`os.replace`） | try/except 静默 |
| `_lp_stall_snapshot()` | 抓 `threading.enumerate()` + `sys._current_frames()` 栈，落 `data/logs/stall_trace_<ts>.txt`，保留最近 5 份 | try/except 静默 |
| `_lp_check_stall()` | 停摆判定：阶段持续>阈值→WARN（首次+每小时节流），恢复→`loop_stall_cleared` | try/except 静默 |

**`_lp_flush` 落盘字段**（≡ K4 要求）：`phase`、`phase_since`、`loop_seq`、`last_finished_phase`、`last_finished_at`、`phase_ms`。

**`_lp_check_stall` 阈值**：`getattr(C, "LOOP_PHASE_STALL_S", 300)`，默认 300 秒（5 分钟）。**未改 `app/config.py`**，参数走 §4 裁定。

**`_lp_stall_snapshot` 代数命名**：文件名 `stall_trace_YYYYMMDD_HHMMSS.txt`，生成后扫描目录删除除最近 5 份外的旧文件。9/3 实证固定名被僵尸握持会导致恢复失败，故用时间戳代数命名。

### 1.3 `_loop()` 主循环阶段标记（`app/trader.py:548-635`）

**结构变更**：原 `while → try → except` 改为 `while → try → try(业务) → finally(flush+seq++) → except`。`finally` 确保每轮末（含 `continue` 和异常）都原子落盘，**绝不影响交易路径**（flush 内部全静默）。

**阶段边界插入**（前后片段对照）：

| 阶段 | 插入位置 | 原代码 | 改后 |
|---|---|---|---|
| `auction` | 竞价轮询 | `self._auction_round()` | `_lp_enter("auction")` → `_auction_round()` → `_lp_finish("auction")` |
| `eod_report` | 日结推送 | `al.daily_report(today)` | `_lp_enter("eod_report")` → `daily_report` → `_lp_finish` |
| `eod_update` | 收盘数据更新 | `updater.start_background_update()` | `_lp_enter("eod_update")` → `start_background_update` → `_lp_finish` |
| `account_update` | 熔断基准 | `try: acct0=... except: pass` | `_lp_enter("account_update")` → 原逻辑 → `_lp_finish` |
| `scan` | 扫描取数 | `self._maybe_launch_scan_async()` | `_lp_enter("scan")` → `_maybe_launch_scan_async` → `_lp_finish` |
| `board` | 打板轮询 | `self._board_round()` | `_lp_enter("board")` → `_board_round` → `_lp_finish` |
| `exits` | 退出执行 | `self._exits_round()` | `_lp_enter("exits")` → `_exits_round` → `_lp_finish` |
| `monitor` | 自选+价格提醒 | `_watch_round()` + `_price_alert_round()` | `_lp_enter("monitor")` → 两者 → `_lp_finish` |
| `twothirty` | 两点半 | `self._twothirty_round()` | `_lp_enter("twothirty")` → `_twothirty_round` → `_lp_finish` |

**未改**：所有买卖判据、scoring 调用、下单分支、仓位系数、sleep 时长、循环节奏。diff 中 61 行删除全部是原 `_loop` 代码的重排（加阶段标记缩进），无逻辑删除。

### 1.4 `_monitor()` 加停摆判定调用（`app/trader.py:509-512`）

在 watchdog 心跳检查块之后新增：

```python
# K4：主循环停摆判定（每轮检查，内部节流不刷屏）
try:
    self._lp_check_stall()
except Exception:
    pass
```

`_monitor` 每 15 秒一轮，`_lp_check_stall` 内部纯内存比较（健康状态 0.2μs），仅在超阈值时写 audit + 抓栈。

---

## §2 五项单测原文

测试文件：`tmp/pack20/K4_test_stall_trace.py`，运行 `py -3.13 tmp/pack20/K4_test_stall_trace.py`。

**9/9 通过**（5 项测试，④⑤ 各含子检查）：

```
[PASS] ①阶段超时→首条WARN且仅1条
[PASS] ②持续90分钟→共2条(首次+每小时)
[PASS] ③恢复→loop_stall_cleared带总时长
[PASS] ④栈快照生成且代数≤5
[PASS] ④快照内容含线程信息
[PASS] ⑤loop_phase.json原子写无异常
[PASS] ⑤文件存在
[PASS] ⑤JSON字段完整
[PASS] ⑤无.tmp残留

=== 9/9 通过 ===
```

**单测设计**：
- `object.__new__(TradingEngine)` 绕过 `__init__`，手动设置 8 个状态变量。
- monkeypatch `app.trader.audit.record`（记录调用，不写真实链）+ `app.trader.time.time`（控制时间推进）。
- ① 设 `_lp_phase_since = now-400`（超 300s 阈值）→ 断言 1 条 `loop_stall` WARN、`phase=scan`、`loop_seq=42`。
- ② 模拟 0/10/.../90 分钟共 10 次检查 → 断言恰好 2 条 WARN（0min 首次 + 60min 每小时）。
- ③ 首次停摆后推进 30 分钟、更新 `_lp_phase_since` 为近期 → 断言 1 条 `loop_stall_cleared`、`streak_s≥1790`、状态已清。
- ④ 生成 7 份快照 → 断言目录中 ≤5 份、内容含线程信息、测试后清理。
- ⑤ 5 线程 × 10 次并发 `_lp_flush` → 断言无异常、文件为合法 JSON、字段完整、无 `.tmp` 残留。

**编译/冒烟**：`py -3.13 -m py_compile app/trader.py` exit=0；`import app.trader` OK，3 个新方法均存在。

---

## §3 量级回归表

### 改前基线（当前运行服务，H4 代码，K4 未重启）

| 指标 | 测量值 | 采样 |
|---|---|---|
| `/api/overview` 平均延迟 | **238.1ms**（去首次冷启动后 ~218ms） | 10 次，max 501ms（首次） |
| 单轮扫描耗时 | 秒级（后台线程，预热后缓存命中） | 历史日志"秒级完成" |

### K4 新增代码纯开销（隔离测量）

| 操作 | 单次开销 | 调用频率 | 对单轮/API 影响 |
|---|---|---|---|
| `_lp_enter` + `_lp_finish` | **0.3μs** | 每轮 ~10 次（board/exits/monitor ×10子轮） | <0.01ms |
| `_lp_flush`（写小JSON+os.replace） | **0.80ms** | 每轮末 1 次 | +0.8ms |
| `_lp_check_stall`（健康状态） | **0.2μs** | 每 15 秒 1 次（_monitor 线程，不占主循环） | 0（不在主循环路径） |
| `_lp_stall_snapshot`（停摆时） | ~1-5ms | 仅停摆首次触发 | 仅异常路径 |

### 改后估算

| 指标 | 改前 | 改后估算 | 劣化 | 判定 |
|---|---|---|---|---|
| 单轮扫描（主循环一轮） | 秒级 | 秒级 + <1ms | **<0.1%** | 合格 |
| `/api/overview` 响应 | 238ms | **不变**（K4 未改 API 层，_lp_flush 在主循环线程不阻塞 API） | **0%** | 合格 |

**结论**：劣化远低于 20% 阈值。`/api/overview` 完全不受影响（K4 无 API 层改动）；主循环增量 <1ms/轮（主要是 `_lp_flush` 写 200 字节 JSON）。

---

## §4 参数裁定请求

| 参数 | 当前值 | 位置 | 建议 | 理由 |
|---|---|---|---|---|
| `LOOP_PHASE_STALL_S` | **未定义**（代码兜底 300s） | `app/config.py`（待加） | 建议设 **300**（5 分钟） | 主循环正常一轮 ≤60 秒（10子轮×5s+扫描），5 分钟无阶段更新即确定停摆；设太短会在扫描慢时误报 |
| `stall_trace` 保留份数 | 硬编码 5 | `_lp_stall_snapshot` | 建议保持 5 | 每份 ~2-5KB，5 份 ≤25KB，足够回溯最近 5 次停摆 |

**本块未改 `app/config.py`**。请验收方裁定是否将 `LOOP_PHASE_STALL_S = 300` 加入 config；若不加入，代码兜底 300s 生效。

---

## §5 生效条件

- **改动只在下次服务重启生效**。当前运行的 8899 服务（**PID 35108，01:05:20 启动**，K4b 2026-09-06 更正：原稿误写 30856——30856 是 ComfyUI）仍运行 H4 代码，无 K4 轨迹。
  - 8899 属主取证（G2，Get-NetTCPConnection 原文）：
    ```
    LocalAddress  : 127.0.0.1
    LocalPort     : 8899
    OwningProcess : 35108
    ```
  - 与 `data/app.lock` = `35108` 一致。
- 重启后：
  - `tmp/loop_phase.json` 开始每轮更新（引擎运行时）。
  - `data/logs/stall_trace_*.txt` 仅在停摆时生成。
  - audit 新增事件类型 `loop_stall`（WARN）和 `loop_stall_cleared`（INFO），`verify_chain` 不受影响（只校验哈希链）。
- **未重启、未杀进程**（遵守红线）。

---

## §6 遗留与风险

### 6.1 前端 `web/js/app.js` 未改（主动放弃，理由）

K4 任务第 4 条允许"若判定风险大于收益，本条可不做"。判定如下：
- **收益低**：`tmp/loop_phase.json` 可直接通过文件系统或新增 API 端点读取；audit 事件 `loop_stall` 已进入审计链，前端"审计复盘"页已能展示 audit 事件。
- **风险中**：`app.js` 是前端单文件，改动需验证不影响既有类与渲染逻辑；当前无前端测试覆盖，回归成本高。
- **建议**：若后续需要前端展示，可新增 `/api/loop_phase` 端点（读 `tmp/loop_phase.json`）+ 前端卡片，单独成块。

### 6.2 新增 I/O 对收盘链预算的影响

- `_lp_flush` 每轮末写 `tmp/loop_phase.json`（~200 字节，0.8ms）。交易时段主循环约每 50-60 秒一轮（10 子轮×5s），即每分钟 1 次写盘，**I/O 负载可忽略**。
- 非交易时段每 30 秒一轮，同样每分钟 ≤2 次写盘。
- 收盘链（`eod_update` 阶段）触发 `updater.start_background_update()` 在独立线程，`_lp_flush` 不阻塞它。
- **风险**：若磁盘满或 `tmp/` 不可写，`_lp_flush` 内部 try/except 静默，不影响交易路径。但此时停摆轨迹也会丢失——建议监控 `tmp/loop_phase.json` 的 mtime 新鲜度作为辅助信号。

### 6.3 停摆判定与 watchdog 重启的交互

- watchdog（`service_watchdog.py`）靠 `/api/overview` 的 `time` 新鲜度判定存活，若判定死亡会重启服务。
- K4 的 `_lp_check_stall` 在 `_monitor` 线程（同一进程内）运行；若主循环停摆但 `_monitor` 线程仍活，K4 能记录 `loop_stall` + 抓栈快照，**在 watchdog 重启前保留现场**。
- 若整个进程卡死（含 `_monitor`），K4 无法记录，仍靠 watchdog 事后重启。这是进程内可观测性的固有边界。

### 6.4 `loop_seq` 从 0 开始，重启后重置

`_lp_seq` 是进程内变量，服务重启后从 0 开始。不影响停摆判定（判定用 `phase_since` 绝对时间），但跨重启的 seq 不连续。如需跨重启追踪，可落盘持久化（当前未做，P3）。
