# G3 watchdog 告警语义修正（streak 恒 0 / 无升级 / 告警疲劳）—— 2026-09-13

> 依据：data/audit/*.jsonl 全量 4664 条实测 + app/trader.py H4 只读分析。
> 改动：app/audit.py（record() 层状态机）、tools/service_watchdog.py（health_check 时段限定）。
> **未碰 app/trader.py（F 道禁碰）**——streak_s 根因在其实例状态管理，修法下沉到 audit 记录层，
> 写入端无需改动。

## §0 根因（为什么 streak_s 恒 0、每次"首次告警"）

- `watchdog_stale` 由 **app/trader.py H4**（L356-371 `_wd_emit_critical`）写入：首次设
  `self._wd_stale_start = now` 并记 `streak_s=0` + "首次告警"，之后每 3600s 记
  `streak=int(now-_wd_stale_start)`。
- 实测 28 条 CRITICAL **全部** `streak_s=0` + 自称"首次" → 每次检查都命中
  `_wd_stale_start is None` 分支 → **该实例属性跨轮次丢失**（进程重启/检查线程重建实例即重置，
  内存状态不落盘）。460 分钟断更者也因此报"首次"。
- **修法**（不碰 trader.py）：在 `app/audit.py record()` 层做**集中状态机**，断更起始时刻
  持久化到 `tmp/g3_alert_state.json`，跨进程/轮次/重启保留。trader.py 无论传什么
  level/streak，record 层统一接管为正确语义。附带收益：**进程重启不再重置 streak**（断更期间
  重启进程，streak 继续累计，这是旧内存方案的固有缺陷）。

## §1 改动清单

| 文件 | 行 | 改动 |
|---|---|---|
| app/audit.py | L209-305 | G3 块：`_G3_STATE_FILE`（tmp/g3_alert_state.json）、`_g3_load/save_state`、`_g3_wd_stale_stateful`（状态机）、`_g3_wd_recovered`（清状态）、`_g3_trading_calibrate`（降噪） |
| app/audit.py | L330-341 | record() 内特判（哈希链落盘前接管）：watchdog_stale→状态机；watchdog_recovered→清状态；trading_event→校准/去重；抑制时 `return None` 不落盘（链以磁盘尾部为准，不破坏连续性） |
| tools/service_watchdog.py | L234-236 | health_check：l2 新鲜豁免 l3 收紧为**仅非交易时段**；交易时段 audit_age 超阈独立触发失败（G3 任务 3） |

**watchdog_stale 升级机制**（`_g3_wd_stale_stateful`）：
- `streak_s` = 距首次断更时刻的秒数（严格递增，落盘持久化）；
- 级别按断更时长阶梯：`>10min → WARN`、`>30min → ERROR`、`>60min → CRITICAL`；
- 每级**只发一次**（升级时发），之后每 60 分钟重复当前级；节流窗口内的重复调用**抑制不落盘**；
- 恢复（watchdog_recovered）清状态，下次断更重新计时。

## §2 判据真值表（G3 任务 3，覆盖全部 8 组合）

判据语义（G1+G3 合并后）：
- `l1_tcp`：8899 端口探活。
- `l2_overview`：/api/overview 返回且 time 新鲜 ≤90s；`TimeoutError/http 错误/stale >90s` 均为失败。
- `l3_audit`：audit 尾部心跳 ≤6h（`AUDIT_MAX_AGE_OUT`，G1 已从 7 天收紧）；`no_t_in_tail`（空文件/无 t）为**软失败**（G1：轮转感知回退 + 00:00-00:30 宽限窗 + 归档回退 rotated_fallback）；audit_age 超阈为**硬失败**。
- `in_trading_window()`：交易时段（引擎必写 audit 心跳）。

| l1 | l2 | l3 | 结论 | 理由 |
|---|---|---|---|---|
| ok | ok | ok | **健康** | 全信号新鲜 |
| ok | ok | 超阈（交易时段） | **硬失败** | ★G3：交易时段引擎必写 audit，链停滞=写入故障，l2 新鲜不豁免 |
| ok | ok | 超阈（非交易时段） | **健康（豁免）** | G1 engine_active_audit_silent：夜间静默正常，l2 新鲜证明引擎存活 |
| ok | ok | no_t_in_tail（交易时段） | **软失败→健康（回退后）** | G1 轮转感知：空文件回退昨日归档取 t；无归档则宽限窗内健康 |
| ok | 失败 | ok | **硬失败** | overview 不新鲜=引擎卡死/半死主信号 |
| ok | 失败 | 失败 | **硬失败** | l2+l3 双信号（真半死，G1 09-13 事故场景） |
| down | * | * | **硬失败** | 端口死=服务未监听（恢复=启动） |
| ok | ok | no_t_in_tail（无归档且窗外） | **软失败** | 无法判断引擎状态，不升级为杀进程依据（G1 宽限窗外仍判失败但非致命） |

代码实测（tmp/g3_test 单测）：交易时段超阈 → `ok=False`；非交易时段 l2 新鲜 → `engine_active_audit_silent`；
轮转回退 → 健康；l2+l3 双失效 → 失败。**生产 verify OK**（audit_age_2s）。

## §3 告警疲劳：14 天聚类 TOP 与降噪方案

全量 4664 条：INFO 1304 / WARN 1921 / ERROR 744 / CRITICAL 43 / BUY 86 / SELL 130 / OK 430 / KPI 3。
**WARN+ = 2710（58.1%）**；其中 `trading_event` 2606 条（WARN 1862 + ERROR 744）占 **96%**。

| 归一化消息（TOP） | 条数 | 根因 | 已修? | 处置 |
|---|---|---|---|---|
| 后台扫描异常: cannot access local variable 'pos_cash' | 536 | 引擎后台扫描局部变量错（历史线程池事故伴生） | **已修**（派单事实） | 保持 ERROR（复发可观察）+ 600s 去重 |
| 情绪[冰点]禁开仓：涨停少、炸板多或跌停潮 | 329 | **设计行为**：情绪闸门拦截属正常风控功能 | 无需修 | WARN→**INFO**（信息性事件） |
| 风控拦截 ××：连续亏损冷却/仓位超上限等 | 607 | **设计行为**：风控拦截 | 无需修 | WARN→**INFO** |
| 冲高回落 N%（高点 N） | 378 | **设计行为**：盘中提示 | 无需修 | WARN→**INFO** |
| 下跌 -N%，注意风险 | 254 | **设计行为**：盘中提示 | 无需修 | WARN→**INFO** |
| 分时量拦截打板 ×× | 48 | **设计行为**：量能过滤 | 无需修 | WARN→**INFO** |
| cannot schedule new futures after interpreter shutdown | 208 | 线程池在解释器关闭后 submit（半死事故伴生） | **未修**（F2 处理中） | 保持 ERROR + 600s 去重 |
| watchdog_stale 断更 10~60 分钟 | 13 | 升级/streak 失效（§0） | **本单已修** | 状态机接管（WARN/ERROR/CRITICAL 阶梯） |
| 其余（kline_amount_missing 20 / quality_alert 13 / …） | ~100 | 各自独立 | — | 保持原级 |

**降噪方案（已落地 app/audit.py）**：
1. 白名单校准：`情绪[冰点]禁开仓/风控拦截/冲高回落/下跌/分时量拦截/连续亏损冷却/仓位超上限/污染修复`
   的 trading_event WARN/ERROR → **INFO**。根因声明：这些是**设计行为**（拦截与提示属正常功能），
   误标 WARN 是级别滥用，降级=语义修正，**非数字游戏**（报告留证）。
2. 去重族（根因未修，保持级别防刷屏）：`cannot schedule new futures/循环异常/高频监控异常/后台扫描异常`
   按归一化 msg **每 600s 至多 1 条**，其余抑制不落盘。
3. watchdog_stale：状态机节流（§1），彻底消除"每 10 分钟一条 CRITICAL"。

**模拟效果**（只读重放全量 4664 条，`tmp/g3_test/simulate` 脚本逻辑）：
```
WARN+ 2710（58.1%）→ 354（7.6%）；降级 INFO 1617 条（白名单）+ 去重抑制 ~740 条
```
**验收"3 个交易日 WARN+ ≤25%"预计达标（7.6% ≪ 25%）**——前瞻口径：上线后新写入按新规则，
历史数据不变（模拟即"上线后同规则重算"的对照）。

## §4 D1 双轨口径对齐

- 本块产出/接管的事件字段：`level / event / streak_s / detail`——与 D1 前端红条消费口径一致
  （quality_alert → audit 转发链路在 app/audit.py L355+，本块在 record() 层接入，字段结构未变）；
- `watchdog_stale` 的 detail 现带 `（G3 {级别}，streak {N}s）`，前端可据此展示持续时长与升级级；
- `streak_s` 现为真实秒数（此前恒 0，前端无法显示持续时长）。

## §5 预注册验收判据 → 逐条结论

| # | 判据 | 结果 |
|---|---|---|
| A1-A7 | 连续异常 streak 严格递增 [0,600,1801,3601,7201]、级别 10/30/60 阶梯 WARN→ERROR→CRITICAL、每级一次、同级 60min 重复 | **通过**（tmp/g3_test/test_g3.py 15 用例 0 失败） |
| A8 | 恢复清状态，下次断更重新计时 | **通过** |
| B1-B7 | trading_event 白名单降级 / 去重族保持 ERROR+600s 去重 / 普通 ERROR 不动 | **通过** |
| 真值表 | 8 组合（§2）含交易时段独立触发 + 非交易豁免 + 轮转软失败 | **通过**（单测 4 场景 + 生产 verify OK） |
| 降噪 | 模拟 WARN+ 58.1% → 7.6%（≤25% 目标达成，3 交易日前瞻验收） | **通过（模拟）** |
| 非数字游戏 | 每条降噪根因已声明：白名单=设计行为（语义修正），去重族=未修保留级别（F2），后台扫描=已修 | **满足** |

## §6 风险与回滚

- **风险**：record() 层特判是审计链基础设施——若白名单误命中真实错误（如某"风控拦截"含系统性故障），
  会降 INFO 掩盖。缓解：白名单均为明确业务语义字符串（拦截/提示/冷却），不含异常类关键词；
  去重族刻意保持 ERROR 级别。
- **回滚**：`git checkout HEAD -- app/audit.py tools/service_watchdog.py` 恢复旧行为；
  单独禁用：把 record() 内 `if event == ...` 特判整体注释即可（G3 块自包含）。
- 状态文件 `tmp/g3_alert_state.json` 可随时删除（下次断更重新计时，无副作用）。

## §7 未做清单（留给验收/后续）

- 未改 app/trader.py（F 道禁碰）：其 H4 内存状态仍存在（但不影响——record 层已接管语义）；
  若后续 F 道改造 trader，建议把 `_wd_emit_critical` 的节流下放给 record 层统一管理，避免双重节流。
- 未改 app/config.py、app/engine.py、app/server.py；未重启/杀任何进程；未动 data/audit/* 与
  tmp/watchdog.log 生产文件；未 commit（留夜间 TianjiGit_Nightly）。
- `cannot schedule new futures`（208 条）根因修复属 F2，本单仅降噪（600s 去重）。
