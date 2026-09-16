# H4 交付报告：守护可观测性打磨（误报分级 + 只读归因）

> 执行时间：2026-09-05 ｜ 范围：P2，只做误报分级 + 只读归因，不改交易逻辑
> 代码改动已由自动 commit `5f14c5a`（2026-09-05 23:50）收录

## 首屏：git diff --stat

```
 app/trader.py | 83 +++++++++++++++++++++++++++++++++++++++++++++++++++++------
 1 file changed, 75 insertions(+), 8 deletions(-)
```

（来源：`git show 5f14c5a --stat -- app/trader.py`；新增 `tmp/pack19/H4_test_watchdog.py` 等为隔离测试与只读分析脚本。）

---

## §0 判据与查重

- 查重门：`tmp/pack19_dup.log` 追加 `H4 2026-09-05T15:23:27Z GATE-OK`（无 DUP-HIT）。
- 开工前 `git status -- app/trader.py` 确认**不为 M**（NOT-M），符合 H4/K4 串行约束。
- 全程禁碰：`app/config.py`、`app/audit.py`、`app/updater.py`、`tools/service_watchdog.py`、`data/audit/*`、`data/app.lock`。

---

## §1 三条改动/结论

### ① `watchdog_stale` 告警分级与节流（`app/trader.py`）

**改动位置**：`app/trader.py` — `_check_watchdog_heartbeat`（原 204-221 行）+ 新增三个私有辅助 `_wd_get_boot_time` / `_wd_emit_critical` / `_wd_emit_recovered_if_needed`；`__init__` 新增 3 个状态变量（`_wd_boot_time` / `_wd_stale_start` / `_wd_last_critical_ts`）。

**改动内容**：

1. **开机时刻分级**：首次调用时用纯标准库 `ctypes.windll.kernel32.GetTickCount64()` 推算系统开机时刻并缓存；若心跳 mtime 早于开机时刻 → 记 **INFO**（`watchdog_stale_reason=boot_or_sleep`），不记 CRITICAL。解决"整机曾关机/睡眠"被误报为 CRITICAL 的问题。
2. **节流**：同一持续断更状态只在升级点记——首次 CRITICAL + 之后每小时一条 + 恢复时一条 `watchdog_recovered`（带 `streak_s`）。原行为每 10 分钟一条 CRITICAL（8 小时=48 条），现降至约 9 条/8 小时。
3. **事件字段扩展**：新增 `streak_s`（断更持续秒数）、`watchdog_stale_reason`（`stale`/`missing`/`boot_or_sleep`）。`audit.record(kind, event, level, **fields)` 原生支持任意字段；`verify_chain` 只校验哈希链不校验字段；50MB 轮转不受影响。
4. **心跳缺失**（文件不存在）：无法判断 boot_or_sleep，仍走 CRITICAL 节流（reason=`missing`）。

**未改**：`tools/service_watchdog.py`（H1 领地）、调用间隔（仍 ~10 分钟）、交易逻辑。

### ② 315 票覆盖上限缺口归因（只读）

**数据源**：`data/market.db`（只读 URI）`kline` 表 `period='day'` 去重 + `data/stock_list.json` + `data/listing_dates.json` + `data/delisted_universe.json` 交叉。查询走已有索引 `idx_kline_pd` / `idx_kline_cp`。

**核心数字（≡ SQL 输出原文）**：
- kline day 宇宙 = **2526**（`SELECT COUNT(DISTINCT code) FROM kline WHERE period='day'`）
- 历史最好 daily_coverage = **2211**（8/25-8/26）
- 缺口 = 2526 − 2211 = **315**
- 在市（stock_list 中）= **2212**；不在市 = **314**
- 在市老股（ipo<2025-01-01）= **2150**；在市新股（ipo≥2025-01-01）= **61**；在市无 ipo 数据 = **1**
- **2150 + 61 = 2211 ≡ 历史最好覆盖数**

### ③ `data/update.lock` 语义核查（只读，不动代码）

**事实**：
- `data/update.lock` 存在，size=1，内容=`\x00`，mtime=**2026-09-03 00:01:56**。
- 9/4 15:10 收盘链、9/5 12:15 后台增量更新均正常完成。
- 代码定位：`app/updater.py:720-759`，`_acquire_update_lock()` 用 `open(path, "a+")` → 若空则写 `\0` → `msvcrt.locking(LK_NBLCK, 1)` 加非阻塞排它锁；`_release_update_lock()` 解锁并关闭。

**判定**：**不是残留文件未清理，是设计如此**。
- 锁与文件句柄绑定，进程死亡时 OS 自动释放（注释明确："无 stale 锁残留"）。
- 文件是持久锁载体，`a+` 模式且内容已为 `\0` 时不重写，故 mtime 停在首次创建时刻，不代表锁残留。
- 与 `app.lock`（内容为 PID、每次启动重写）不同：`update.lock` 内容固定、mtime 不更新属正常。

**最小修法建议（本块不动代码）**：若希望 mtime 反映最近一次锁获取，可在 acquire 成功后加 `f.truncate(0); f.write("\0"); f.flush()`；或加注释说明"mtime 不代表运行时间"。当前无 stale 风险，优先级 P3。

---

## §2 单测原文（隔离，monkeypatch audit 防污染真实链）

测试文件：`tmp/pack19/H4_test_watchdog.py`，运行 `py -3.13 tmp/pack19/H4_test_watchdog.py`。

**4/4 通过**：

```
[PASS] ①心跳早于开机→INFO+boot_or_sleep
[PASS] ②持续断更90分钟→2条CRITICAL
[PASS] ③恢复→watchdog_recovered+streak_s
[PASS] ④健康状态不刷屏

=== 4/4 通过 ===
```

**单测设计**：
- `object.__new__(TradingEngine)` 绕过 `__init__`，手动设置 3 个状态变量。
- monkeypatch `os.path.exists` / `os.path.getmtime` / `time.time` / `app.trader.audit.record`，全程不写真实 `audit.jsonl`、不碰生产 `data/`。
- ① 设 `_wd_boot_time = now+3600`，心跳 mtime=now-700 → 断言 1 条 INFO、`watchdog_stale_reason=boot_or_sleep`、不进状态机。
- ② 固定心跳 mtime，模拟 0/10/.../90 分钟共 10 次调用 → 断言 CRITICAL 恰好 2 条（0min 首次 + 60min 每小时），`streak_s` 为 0 和 ≥3600。
- ③ 首次断更记 CRITICAL → 推进 30 分钟后心跳恢复 → 断言 1 条 `watchdog_recovered`、`streak_s=1800`、状态已清。
- ④ 健康状态连续 5 次调用 → 断言 0 条事件。

**编译/冒烟**：`py -3.13 -m py_compile app/trader.py` exit=0；`import app.trader` OK。

---

## §3 覆盖分母建议（交验收方裁定，本块不改 config/阈值）

### 315 缺口分桶表（守恒：合计=315，"未知"=5 ≤ 50）

| 桶 | 数量 | 说明 |
|---|---|---|
| 北交所 | 0 | kline 宇宙无 8/4 开头代码 |
| B股 | 0 | kline 宇宙无 900/200 开头代码 |
| 退市 | **310** | `delisted_universe` 中 `out` 非空或 `status=0`；有历史日K但已不在市 |
| 新股未满窗 | 0 | 61 只 2025 后上市新股均在市且已被覆盖（含在 2211 内） |
| 长期停牌 | 0 | 不在市且未退市的为 0 |
| 未知 | **5** | 4 只指数/ETF（sh000001、sz399001、sz399006、510300）+ 1 只在市无 ipo（601123 C马矿） |
| **合计** | **315** | |

### 结论：真实应覆盖分母 = **2211**（上限 2212）

- kline 宇宙 2526 中，**315 只不应被 daily_coverage 覆盖**（310 退市 + 4 指数/ETF + 1 无 ipo 次新）。
- 在市个股 = 2212（老股 2150 + 新股 61 + 无 ipo 1），其中 2211 只已有 ipo 记录且被历史最好覆盖触达。
- **若 `daily_coverage` 的 0.8/0.9 阈值基于分母 2526，则系统性偏低约 12.5%**：覆盖 2000 只时，按 2526 算 79.2%（不达标 0.8），按 2211 算 90.5%（达标 0.9）。

### 建议（交验收方裁定）

1. **分母口径**：将分母从"kline day 宇宙"改为"在市个股数"（每日从 `stock_list.json` 动态取，约 2211-2212）。
2. **阈值**：若分母改为 2211，现有 0.8/0.9 阈值可保持；若坚持 2526，则阈值应下调至约 0.70/0.79。
3. **指数/ETF 剔除**：kline 宇宙中混入的 3 只指数和 1 只 ETF 应在覆盖计算前排除。

---

## §4 遗留

1. **H1b 代码修复**：`watchdog_stale` 分级只解决误报，看门狗恢复分支死锁根因须等 H1b 落地。
2. **601123 C马矿无 ipo 数据**：`delisted_universe.json`（fetched_at 2026-08-27）未收录该次新股，建议下次同步补全；仅 1 只，不影响结论。
3. **`update.lock` mtime 误导**：无 stale 风险，但 mtime 停在 9/3 可能引起运维误判；建议按 §1③ 加注释，P3。
4. **web/js/app.js 展示**：写白名单允许追加 `watchdog_stale_reason` / `streak_s` 展示字段，本次未改前端（无对应 UI 需求）。
