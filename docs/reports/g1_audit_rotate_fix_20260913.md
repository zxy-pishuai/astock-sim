# G1 修 audit 轮转导致的 no_t_in_tail 误杀（夜间自伤重启）—— 2026-09-13

> 依据：tmp/watchdog.log 原文复盘 + data/audit/ 目录实测 + app/audit.py rotate_daily 源码。
> 改动：tools/service_watchdog.py（轮转感知回退 / 宽限窗 / l2 新鲜豁免 / 6h 阈值）、
> tools/audit_rotate.py（轮转后写链首 marker）。**未碰生产 audit 数据、未杀任何进程**。

## §0 事实复盘（watchdog.log 原文）

**09-13 00:10-00:20 误杀（本单根因）**：

```
00:00:02  健康 OK：l1=ok | l2=time_age_0s | l3=audit_age_8938s
00:05:02  健康 OK：l1=ok | l2=time_age_0s | l3=audit_age_9238s   ← 00:05 TianjiAuditRotate 归档→空
00:10:02  异常 1/3：l1=ok | l2=time_age_0s | l3=no_t_in_tail
00:15:02  异常 2/3：... no_t_in_tail
00:20:02  异常 3/3：... no_t_in_tail  → 恢复触发 → 半死僵尸 PID=43336（overview=time_age_0s audit=no_t_in_tail）终止重启
00:25:02  健康 OK（新实例已写心跳）
```

**关键证据**：误杀全程 `l2_overview=time_age_0s`（引擎活着且在更新状态），**仅因轮转后 audit.jsonl 空（5-10 分钟空窗）被 l3 三连杀**。

**09-12 20:30/21:00/21:15 三次（真半死，非误杀）**：

```
20:30:06  异常 3/3：l2=TimeoutError:timed out | l3=audit_age_31185s → 杀 31364
21:00:06  异常 3/3：l2=TimeoutError:timed out | l3=audit_age_1749s  → 杀 38400
21:15:06  异常 3/3：l2=TimeoutError:timed out | l3=audit_age_845s   → 杀 43600
```

三次触发形态均为 **l2 urlopen 超时**（服务无响应），audit 仅旁证——属服务真病态半死
（E3 已定位：09-12 晚间服务高频半死，恢复后 15-30 分钟再死；E3 的批处理阈值放宽 3→6
已让此类恢复更保守）。**本单不改变其判定**（l2 不新鲜时不豁免 l3）。

## §1 改动清单

| 文件 | 行 | 改动 |
|---|---|---|
| tools/service_watchdog.py | L65 | `AUDIT_MAX_AGE_OUT` 7 天 → **6 小时**（依据见 §2） |
| tools/service_watchdog.py | L140-161 | 新增 `_tail_t_from_file()`：读文件尾部 8KB 提取最后 t 字段 |
| tools/service_watchdog.py | L164-180 | 新增 `latest_archived_audit()`：按名倒序取最新含 t 的轮转归档 |
| tools/service_watchdog.py | L183-216 | `check_audit()` 轮转感知：当前空/无 t → 回退归档（detail 标 `rotated_fallback_<归档>_age_<秒>`） |
| tools/service_watchdog.py | L219-231 | `health_check()` l2 新鲜豁免：l2 ok 且 l3 失败 → l3 判 `engine_active_audit_silent`（不构成死亡证据） |
| tools/service_watchdog.py | L336/L349-355 | `ROTATE_GRACE_WINDOW=((0,0,0,30),)` + `in_rotate_grace()` |
| tools/service_watchdog.py | L733-736 | main() 轮转宽限分支：00:00-00:30 内 `no_t_in_tail` 不计连续失败（只记 INFO） |
| tools/audit_rotate.py | L26-29 | rotate_daily 归档后立即 `audit.record("daily","audit_rotate_marker","INFO")` 写链首 marker（根治：空窗不再出现） |

## §2 AUDIT_MAX_AGE_OUT 收紧依据（7 天 → 6 小时）

- 7 天过宽：意味着"服务半死但 audit 未超龄"的判定窗口最长一周，l3 判据在夜间实际失效；
- 6 小时依据：audit 心跳只在引擎活跃写链时出现；非交易时段最长正常静默=周末
  （周五 15:00 → 周一 09:30 ≈ 66h），必然触发 audit_stale——**但此时 l2 overview 新鲜
  （引擎活着）→ `engine_active_audit_silent` 豁免，不误杀**；
- 而 l2 也停更（真半死）时，l2+l3 双信号在 6 小时内即可触发恢复（原 7 天窗口下 l3
  全程不参与）；**收紧是强化半死检出，靠豁免消除误杀面**。
- 交易时段判据不变（10 分钟，引擎交易时段写心跳）。

## §3 预注册验收判据 → 逐条结论（tmp/g1_test/test_g1.py，11 用例 0 失败）

| # | 判据 | 结果 |
|---|---|---|
| T1 | 当前空 + 归档有新 t → check_audit 返回 True/`rotated_fallback` | **通过**（`rotated_fallback_audit_20260912.jsonl_age_3600s`） |
| T2 | 当前空 + 无归档 → `no_t_in_tail`（不谎报） | **通过** |
| T3 | 归档 t 超 6h（非交易）→ `audit_stale`（阈值得效） | **通过**（`audit_stale_36000s(limit_21600s)`） |
| T4 | 宽限窗：00:10 在窗内 / 01:00 窗外 | **通过**（True / False） |
| T5 | l3 fail + l2 ok → all_ok=True、l3=`engine_active_audit_silent` | **通过** |
| T6 | l3 fail + l2 fail（TimeoutError）→ all_ok=False、l3 不豁免 | **通过**（真半死仍可捕获） |
| T7 | 端到端 00:05 轮转模拟（空当前+归档新t+l2新鲜）→ all_ok=True | **通过**（`rotated_fallback...age_122s`） |
| T8 | main 级 dry-run：预置计数 2 → 健康清零、rc=0、不触发恢复 | **通过**（计数 2→0，无恢复动作） |

生产只读体检：`py tools/service_watchdog.py --verify` → `OK | l1=ok | l2=time_age_0s |
l3=audit_age_7784s`（非交易窗，6h 内正常通过，未误报）。

## §4 上线后前瞻验收（需 7 个自然日）

1. 连续 7 个自然日（含周末）`tmp/watchdog.log` "恢复触发"次数 = 0（除非有真实故障，
   需在报告中举证）；
2. `data/logs/watchdog_child_stderr_*.log` 新增文件数 ≤ 1 个/天；
3. 每日 00:05 轮转后 00:10/00:15 检查：应见 `rotated_fallback` 或 `轮转宽限(l3)` 日志，
   不再出现 `异常 1/3 ... no_t_in_tail`。

## §5 风险与回滚

- **风险 1**：l2 新鲜豁免 l3 后，若"overview time 正常但 audit 链真断"的罕见场景，
  l3 不再报警——但该场景下服务可正常响应（l2 新鲜），不构成半死，watchdog 职责
  （保活）不受损；audit 链完整性由 verify_chain 独立检查（app/audit.py），不在 watchdog 范围。
- **风险 2**：轮转 marker 写入失败（rotate 脚本异常）→ 空窗仍在，但轮转感知回退+宽限窗
  双兜底仍避免误杀（T1/T4/T8 已证）。
- **回滚**：`git checkout HEAD -- tools/service_watchdog.py tools/audit_rotate.py`
  （AUDIT_MAX_AGE_OUT 恢复 7 天、回退/宽限/豁免分支移除、marker 移除），改后 py_compile +
  --verify + 重跑 test_g1.py 即可恢复验证。

## §6 未做清单 / 遗留

- 未改 app/audit.py（有并发 AI 改动在途，本单零侵入：marker 由 tools/audit_rotate.py 调用
  audit.record 完成，链格式完整）；
- 未改 app/config.py（watchdog 不 import app/*，阈值常量留在 watchdog.py 内，可配元组
  ROTATE_GRACE_WINDOW 已注明）；
- 09-12 晚间三次真半死的服务侧根因（内存/线程/上游源）属专项，不在本单；
- 计划任务 TianjiAuditRotate 时刻（00:05）与审计数据未动。
