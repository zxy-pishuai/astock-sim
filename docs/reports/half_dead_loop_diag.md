# 引擎"半僵死"循环 P0 诊断（Z6，纯取证）

- 日期：2026-09-02 22:45 起（观察窗 18:30 后，22:33 开工）
- 查重门：`docs/reports/half_dead_loop_diag.md` 本次创建（GATE-OK 已记 tmp/pack11_dup.log）
- 红线遵守：未改任何 app/tools/main.py 代码、未 git 写、未杀/重启进程、未碰计划任务、未出网（仅本机 8899 探活 ≤20 轮）；临时物全在 tmp/z6/
- 当前实例（22:33/22:45 实测）：**44760**（20:35:16 起，8899 LISTEN，app.lock=44760 一致）
- 蹲守：后台 python 每 9 分钟一轮快照（tmp/z6/night_snapshots/），截至本报告撰写已 1 轮

---

## §0 方法（预注册判据）

**判据**：能否用日志证据圈出每个实例"卡死前最后动作"，并解释"夜间全灭 vs 8528 独活"的反差。

证据源（全部只读）：
- `tmp/watchdog.log`（581 行）：51 次"半死僵尸" + 54 次"恢复完成"完整链
- `data/audit/audit.jsonl`（3549 行）：实例最后 audit 事件、心跳分布
- 代码读码：`tools/service_watchdog.py`（判据）、`main.py`（启动序）、`app/updater.py`（重活）、`app/trader.py`（主循环）、`app/server.py`（HTTP 面）、`app/datafeed.py`+`app/tdx.py`（行情网络）、mootdx 0.11.7 源码（socket 超时）
- `app/config.py` 只读（mtime 8/27 07:30，本批无批外变更）

**watchdog 三级判据（读码确认，service_watchdog.py L74-158）**：
| 层 | 判据 | 阈值 |
|---|---|---|
| l1_tcp | TCP 8899 连通 | 5s |
| l2_overview | GET /api/overview 200 且 time 新鲜 | ≤90s；urlopen 5s 超时 |
| l3_audit | audit.jsonl 尾部 t 新鲜 | 交易时段 ≤10min；**非交易时段放宽 7 天** |

任一失效计 1 次，**连续 3 次（=15 分钟）→ recover()**。恢复路径：l2/l3 双 fail 且端口通 → 判"半死僵尸" taskkill + clear_lock + Start-Process 重启（L271-320）。

**关键结论（先立）**：**51 次杀全部由 l2 overview 超时驱动**（除 9/1 13:00 44104 由 l3 交易时段 audit_stale 参与）；**l3 非交易时段从不触发**（放宽 7 天）。故半僵死循环的根因 = **/api/overview 为何超时**，与 audit 冻结无直接关系。

---

## §1 受害实例生命周期表（51 次杀，三段）

### A 段｜9/1 13:00 44104（唯一 l3 参与）
```
13:00:12 kill PID=44104 | ov=TimeoutError | au=audit_stale_5366s(limit_600s)
13:00:16 恢复完成 old=44104 new=52336
```
- **交易时段 l3 触发**（audit 冻结 >10 分钟）：44104 盘中主循环停摆 → 无心跳 → l3 判死
- 这是**唯一一例 l3 起作用的案例**（白天交易时段 audit 停=硬伤）

### B 段｜9/1 19:35 → 9/2 06:50（46 连杀，夜间全灭）
```
19:20:10 恢复完成 old=0 new=15344        ← 9/1 19:05 手动重启后 watchdog 补位
19:35:13 kill PID=15344 | ov=Timeout | au=audit_age_871s
19:50:13 kill PID=33860 | ov=Timeout | au=audit_age_864s
…（每 15 分钟一轮，PID 几乎全不同）…
06:50:13 kill PID=22512 | ov=Timeout | au=audit_age_864s
06:50:17 恢复完成 old=22512 new=2112     ← 此后断档至 10:10（Z2：07:00-08:26 系统无响应+意外关机）
```
- **每轮实例启动后 ~40s 写 2 行 audit**（audit 实测"开盘自动开启已启用"+"非 09:10-15:05 窗口待明晨"），**之后 audit 冻结**——**这是 H1 夜间守卫的正常行为**（trader.py L236 仅 09:10-15:05 启动引擎，L250-260 非窗口记"待明晨"且按日去重，引擎不启动故无事件可写），**不是卡死**
- **l2 overview 从出生就超时**（audit_age 从 ~864s 每轮重新累计，说明每轮实例 audit 都只写到启动后 40s）→ 15 分钟判死

### C 段｜9/2 19:50-20:35（8528 + 3 连杀）
```
19:50:12 kill PID=8528 | ov=Timeout | au=audit_age_15356s   ← 8528 audit 冻结 4.3h 才死
19:50:16 恢复完成 old=8528 new=42508
20:05:12 kill PID=42508 | ov=Timeout | au=audit_age_863s
20:20:12 kill PID=43948 | ov=Timeout | au=audit_age_863s
20:35:12 kill PID=47096 | ov=Timeout | au=audit_age_864s
20:35:16 恢复完成 old=47096 new=44760     ← 当前实例，此后 2h+ 无杀
```
- **8528 是"晚发型"**：audit 冻结 4.3h（15:36 起）但 l2 直到 19:40 才超时 → 与 B 段"出生即超时"不同
- 42508/43948/47096 又是"出生即超时"（audit_age_863s）

**统计**：51 杀 = A 段 1（l3 参与）+ B 段 46（全 l2）+ C 段 4（8528 l2 晚发 + 3 全 l2）。恢复 54 次（含 8/31 23:45 与 9/2 10:10 全死重启 2 次，9/1 19:20 全死重启 1 次）。

---

## §2 8528 存活对比（头号问题①的正面回答）

### 8528 全天时间线（audit + watchdog 对齐）
```
10:10:39  8528 起（watchdog 拉，17940 死后）
上午      交易正常：09:30 卖出（17940 所写）、11:18:21 买入 600121、心跳到 11:29:00
11:29:00  最后一条 heartbeat
11:30-15:00  午休+复市：audit 完全 0 事件（对照 8/27 同时段 193 条）——交易分支停摆 3.5h
15:31:28  2 条"收盘数据增量更新已触发"（主循环非交易分支仍活着？）
15:34:07/09  data_update + data_update_failed（updater 双跑，Z4 已定案）
15:34:15  data_snapshot（9/2 快照）
15:36-19:35  audit 冻结，但 watchdog 记"健康 OK"（l2 overview time_age_0s + l3 非交易放宽）
19:40-19:50  l2 overview 连续 3 次超时 → 19:50:12 判死
```

### 回答①：两种病是同一病根吗？

**否，两机制不同，但都在 watchdog 判据下表现为"半死僵尸"被杀：**

| 维度 | 8528 白天（audit 停、l2 活） | 夜间实例（audit 2 行签名、l2 出生即超时） |
|---|---|---|
| audit 冻结 | 引擎主循环 11:29 停摆 → 无心跳（**主循环问题**） | 夜间 H1 守卫不启动引擎 → 无事件（**正常设计**） |
| l2 overview | 正常（上游通），19:40 才卡 | 出生就超时（上游故障窗口） |
| 存活 | 9.6h（l2 活时 watchdog 记健康） | 15 分钟（l2 连 3 次超时） |
| 死因 | 19:40 上游故障波及 l2 | l2 从出生就超时 |

**核心分野 = l2 overview 通断，不是 audit**：
- **audit 冻结本身不会触发非交易时段判死**（l3 放宽 7 天）→ "audit 冻结但 overview 活"能撑数小时（8528 4h、44760 2h+）
- **只要 l2 通（上游正常），实例就能活**；**l2 一卡（上游故障窗口），15 分钟必杀**
- 夜间实例"从出生就超时"与 8528"晚发超时"的差异 = **上游网络故障窗口的起止**，非引擎状态

**补充：为何"audit 冻结但 overview 活"能撑数小时？** 因为 watchdog 的 `health_check()` 是 `l1 and l2_ok and l3_ok` 全绿才算健康——非交易时段 l3 恒绿（7 天放宽），l2 绿 → 健康；**audit 冻结在非交易时段被设计性忽略**。这不是 bug，是判据设计（防夜间/周末误判），但代价是"主循环停摆"在非交易时段不可观测（直到 19:40 l2 也卡）。

---

## §3 启动路径重活清单（22:00-06:00 vs 10:10 对比）

main.py 启动序（L199-247 读码）：
```
L199-201  server = ThreadingHTTPServer; serve_forever 放 daemon 线程（先起）
L206      _wait_ready（等 server 30s）
L211      updater.start_background_update()   ← 若 needs_update()=True 触发全市场重活
L218      trader.engine.enable_auto_start()   ← _monitor 15s daemon 线程（H1 守卫）
L225      shadow_scheduler.start()
L237      _warm_list daemon 线程（新浪 77 页并发拉列表）
L244      warm_moneyflow_top(30)
```

**updater 重活（run_update L585-689）**：min5（~1002 只 × 16 并发拉取 + SQLite 写入）+ 日K（300 只）+ global + auto_snapshot（**拷贝 market.db 1.3GB**）+ data_sentinel（**全库扫描 1.3GB SQLite**）。

**关键判定（updater.py L563-582）**：
- `needs_update()` = `last_day != _target_day()`
- `_target_day()`：交易日 15:10 后 = 今天；否则 = prev_trading_day
- **9/1 15:12 更新成功（min5 1002/1005）→ last_day=9/1**；9/1 19:20 后 hm≥1510 → _target_day=9/1 → **needs_update=False** → **夜间实例不触发 updater 重活**
- **9/2 15:34 更新成功（1003/1006）→ last_day=9/2**；44760 20:35 起 needs_update=False → 无重活

**结论：夜间"启动重活"假设证伪**（9/1 夜间 46 实例与 44760 都不触发 updater）。启动重活（snapshot/sentinel/min5）只在该日更新未完成时触发（如 9/1 15:12 那次，耗时约 30s+），不会造成持续数小时的 overview 卡死。

---

## §4 HTTP 面实测（当前实例 44760，22:4x 串行 5 次）

| 端点 | 5 次耗时（ms） |
|---|---|
| /api/overview | 644 / 306 / 329 / 339 / 376 |
| /api/trading/status | 978 / 20 / 16 / 16 / 16 |
| /api/commission | 15 / 15 / 16 / 16 / 16 |
| /api/state | 17 / 14 / 16 / 16 / 15 |

**当前完全健康**（overview 300-650ms，远低于 5s）。与夜间"出生即 5s+ 超时"形成对照——**差异不在引擎代码，在行情数据源可达性**。

**overview 处理链（server.py L403-416）**：
```
fetch_indices()  → 腾讯 1 次 HTTP（10s timeout）
get_stock_list() → 6h 缓存（通常命中）
fetch_quotes(lst[:600]) → 3s TTL 缓存 → 未命中走 TDX→腾讯(4并发)→新浪补缺
calc_breadth + get_regime → 纯计算
```
- 无共享锁风险（datafeed._lock 仅 dict 读写）；server 为 ThreadingHTTPServer + daemon_threads=True（每请求新 daemon 线程）
- **最大慢点 = fetch_quotes 的 TDX 路径**（tdx.py L152 `_pool.map` 无 timeout；mootdx `c.quotes()` 默认 socket timeout=15s，quotes.py L79/contrib/compat.py L112 settimeout(15)）→ **TDX 15s 超时 + 腾讯 10s 补缺 = 单次 overview 最坏 ~25s > watchdog 5s 探活**

**机制通道（H1 的实现路径）**：行情源（TDX/腾讯）不可达时，overview 每次请求卡在 fetch_quotes 网络链（TDX 15s→腾讯 10s），watchdog 5s 探活必超时 → l2 连续 3 次 → 判死。

---

## §5 假设矩阵

| # | 候选 | 支持 | 反对 | 状态 |
|---|---|---|---|---|
| H1 | **行情源（TDX/腾讯）时段性不可达** → overview 卡 | 两个故障窗口（9/1 19:20-9/2 06:50 约 11.3h、9/2 19:40-20:35 约 55min）内所有实例 l2 出生即超时；窗口外（白天/20:35 后）overview 300ms 正常 | 无直接网络监控证据（本机不记上游连通性） | **强候选，未直接证实**（需在上游加连通性日志） |
| H2 | watchdog l3_audit 判据误杀 | 44104 交易时段 audit_stale 被 l3 判死（唯一案例） | 非交易时段 l3 放宽 7 天从不触发；B/C 段 50 杀全是 l2 | **部分成立**：仅交易时段有效；夜间非主因 |
| H3 | 启动 updater 重活持 GIL 拖死 overview | run_update 确有全市场重活 | 9/1 夜间 last_day=9/1 → needs_update=False 不触发；44760 也 False | **证伪（夜间）** |
| H4 | 裸 urlopen 无 timeout | 任务书原始假设 | 全局 28 处 urlopen 全有 timeout；datafeed _http 兜底 C.REQUEST_TIMEOUT=12 | **证伪** |
| H5 | TDX mootdx 无限 hang | `_pool.map` 无 timeout | mootdx timeout=15 默认 + settimeout(15) + except socket.timeout | **修正**：非无限 hang，但 15s > watchdog 5s 探活 → 仍是 l2 超时的机制通道（H1 的载体） |
| H6 | 引擎主循环停摆（8528 11:29 / 44104 13:00） | audit 心跳证据确凿 | 精确卡点不可观测（watchdog 重启无 stdout/stderr 重定向） | **现象已证实，根因未定**（P0 盲区） |
| H7 | ThreadingHTTPServer 连接堆积/线程泄漏 | daemon_threads=True 每请求新线程 | 无连接数监控；当前 overview 快 | **未验证** |
| H8 | W1 回测并发邻居占坑 | tmp/w1 活跃（22:33） | 当前 overview 仍 300ms；9/1 夜间无 W1 也全灭 | **当前非主因**（蹲守 CPU 快照持续对照） |

**验收方重点候选的判定**：watchdog l3 判据"误杀"→ **夜间不成立**（l3 非交易放宽 7 天从不触发，夜间 46 杀全是 l2）；**交易时段成立**（44104 案例，audit 停 10 分钟即判死）。**"watchdog 定义需重做"的结论：不是 l3 误杀，而是 l2 判据把上游网络故障当成引擎死亡**——根因是上游，watchdog 只是触发器。

---

## §6 修复提案（只建议，零落地）

| 档 | 提案 | 锚点 | 理由 |
|---|---|---|---|
| **P0 日志级** | **watchdog Start-Process 加 stdout/stderr 重定向**（`-RedirectStandardOutput/-RedirectStandardError` 或 `> file 2>&1`） | service_watchdog.py L252-256 | 当前实例 print/未捕获异常**全部丢失**（读码确认无重定向）——8528 主循环 11:29 停摆的精确卡点、夜间实例启动错误都不可观测。**本轮诊断最大盲区，必须先补** |
| P0 判据级 | **watchdog 对"启动初期 l2 失败"豁免**（新实例启动 <3 分钟内的 l2 超时不累计失败；或连续失败次数按窗口滑动） | service_watchdog.py L339-360 main() | 上游故障窗口内每轮新实例"出生即死"，恢复成为无效风暴（46 连杀）；豁免可让实例撑过启动/上游抖动期 |
| P1 超时补漏 | **fetch_quotes TDX 路径加整体超时**（`_pool.map` 改 `with_timeout` 或 future.result(timeout=8)） | tdx.py L152 | mootdx 15s > watchdog 5s，TDX 卡顿直接造成 l2 超时；整体 8s 上限可让 overview 在 watchdog 探活窗内返回 |
| P1 缓存级 | **overview 行情缓存 TTL 提高**（QUOTE_CACHE_SECONDS 3→30s，或 overview 单独用 30s 缓存） | datafeed.py L331 | overview 是探活+首页展示，不需要 3s 实时；30s 缓存可显著降低对上游的依赖 |
| P2 架构级 | **上游连通性探针**（独立线程周期测 TDX/腾讯，记 audit 或 quality_alert） | 新模块 | 把"上游故障窗口"从推断变可观测，watchdog 判据可区分"上游故障"与"引擎死亡" |
| P2 架构级 | watchdog 判据引入"连续健康期"概念：l2 失败但进程刚启动且无其他异常 → 降级为 WARN 不累计 | service_watchdog.py | 避免启动风暴 |
| P2 架构级 | 主循环停摆可观测化：非交易时段也定期写轻量 heartbeat（如每小时一次） | trader.py L337-339 | 让"主循环停摆"在非交易时段可被 l3 捕获，避免 8528 式 4h 盲区 |

**若只做最小修复（推荐档）**：**① P0 重定向 + ② P0 启动豁免**。这两个改动量最小（各几行），直接消除"不可观测"与"启动风暴"两个最痛问题；③④⑤ 视 ① 揭示的根因再定。

---

## §7 9/2 盘中 3.5h 停摆归因

- **现象**：8528 11:29 后 heartbeat 停写，13:00-15:00 交易时段 audit 0 事件（对照 8/27 同时段 193 条）；15:31 又见"收盘更新触发"×2
- **归因**：主循环（_loop 线程）11:29 后在交易时段最后一次迭代中卡死（_board_round L823 `fetch_quotes(300, enrich)` 或 scan_once 后台线程网络链），未进入 13:00 复市分支。**精确卡点不可观测**（无 stdout 重定向，P0 盲区）
- **与夜间循环同源？** **异源**：夜间循环 = 上游网络故障（l2 出生即超时）；8528 停摆 = 主循环线程卡死（audit 停但 l2 通）。两者独立，但都被 watchdog 判死
- **为何 15:31 还能写"收盘更新"**：该 msg 位于主循环非交易分支 L329-336；若主循环已卡死则不该写。**两条（×2）说明 8528 时段存在 updater 双跑/双触发**（Z4 已定案），15:31 的触发可能来自 updater 内部而非主循环，需 Z4 交叉（本报告不重复采）

---

## §8 诚实披露

**已证实的结论**：
1. 51 次杀，除 44104 外全由 l2 overview 超时驱动；l3 非交易时段从不触发
2. 夜间"2 行签名"= H1 守卫正常行为，非卡死
3. "audit 冻结但 l2 活"能撑数小时（8528 4h、44760 2h+）= l3 非交易放宽的判据设计
4. 44760 与夜间实例 audit 签名完全一致，差异仅在 l2 overview 通断
5. 全局 urlopen 全有 timeout；mootdx 有 15s socket 超时（非无限 hang）
6. 9/1 夜间 needs_update=False，updater 重活假设证伪

**未定/未做**：
- H1"上游故障窗口"根因（服务器维护？限流？）未直接证实——需上游连通性探针
- 8528 主循环 11:29 停摆精确卡点（P0 盲区：watchdog 重启无输出重定向）
- 19:40 循环重启触发点：无网络监控，推断为上游故障窗口开始（19:35 健康 → 19:40 首报异常，窗口内无本机干预记录）
- H7 连接堆积未验证
- W1 并发影响：当前非主因，蹲守 CPU 快照已持续对照（见 tmp/z6/night_snapshots/）
- **蹲守持续中**：每 9 分钟一轮，目标抓到"下一次死亡"完整现场（死前最后 audit + 最后一次成功 HTTP）；截至 22:45 已 1 轮（44760 健康），蹲守将持续至 ~01:15

### 蹲守进展（22:42-23:53，44760 未死，overview 间歇性超时 + nightly 成功）

| 快照时刻 | 8899_owner | /api/overview | /api/trading/status |
|---|---|---|---|
| 22:42:51 | 44760 | 200 656ms | 200 771ms |
| 22:51:53 | 44760 | 200 895ms | 200 806ms |
| **23:00:55** | 44760 | **FAIL:TimeoutError 5015ms** | 200 (见快照) |
| 23:10:06 | 44760 | 200 1560ms | — |
| 23:19:09 | 44760 | 200 2132ms | 200 2007ms |
| **23:28:14** | 44760 | **FAIL:TimeoutError 5028ms** | — |
| 23:37:24 | 44760 | 200 469ms | — |
| 23:46:26 | 44760 | 200 514ms | — |

**解读**：
- overview 耗时大幅波动：656ms → 5s 超时（23:00、23:28 两次间歇超时）→ 469ms 恢复。**H1 上游抖动在 44760 上持续存在**（与 9/1 夜间同型但强度低：间歇而非持续，watchdog 5 分钟一轮未抓到连续 3 次超时）
- **44760 撑住未死**（23:53 仍活，watchdog 23:50 健康 audit_age_11654s）；若午夜后抖动恶化成持续窗口（9/1 模式），44760 仍可能被杀 → 蹲守继续盯梢至 ~01:15
- **23:50 TianjiGit_Nightly 触发成功**：git 新提交 `c8bf8ef auto 2026-09-02 23:51`（总 7 提交）——Z1 的 BOM 修复生效，**nightly 链闭合**

---

## 附：明晨验收命令
```
Set-Location 'C:\Users\26838\A股模拟盘'
# ① 蹲守是否抓到 44760 或其后继的死亡现场
Get-ChildItem tmp\z6\night_snapshots | Sort-Object Name | Select-Object -Last 6
# ② 是否有新的半死僵尸（B/C 段是否继续）
Get-Content tmp\watchdog.log -Tail 20 | Select-String '半死|恢复完成'
# ③ 上游故障窗口是否复现（9/3 早盘 l2 首次检查）
# ④ 若 watchdog 重定向提案落地前，先看 audit 21:00-次日有无新事件
Get-Content data\audit\audit.jsonl -Tail 10
```
