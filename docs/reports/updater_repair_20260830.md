# 数据通道修复报告 — 2026-08-30（P0 收盘增量归零）

- 时间: 2026-08-30 22:08 增量成功，22:40 诊断落盘
- 范围: `app/updater.py` / `app/datafeed.py` / `app/tdx.py`（数据源封装层，红线内）
- 只读取证为主：`data/audit/audit.jsonl` 47 条 `data_update` 事件 + `data/update_state.json` + `kline/min5` 新鲜度 + `data/snapshots/` Manifest；样本复现为只拉不写（`_one` 拉取段打印，不落库）
- 约束: 未改 `app/config.py`；未手写 `market.db`；未盘中压测（22:xx 低峰小样本 ≤30 只）；`py_compile` / LF 合规

---

## 0. 判定先行（给急着看结论的人）

**根因排序（证据在 §2）：**

1. **收盘后集中请求触发上游限流/服务端过载 — 首因，证据最强。** 三次失败均发生在收盘后首个自动触发的 16:01/17:08 完成窗口，耗时 51–118 min（成功仅 3–10 min），同日 min5 `empty==total` 且 daily `errors==300` 双通道同归零，总量随 P62 清单 496→1002→1004 同步放大，且相邻日同代码同链路在非高峰全部成功。可排除我方解析退化。
2. **TDX 6 连接池在 8 并发下收盘瞬时挤占 — 次要放大器。** `app/tdx.py` `_POOL_SIZE=6` vs `app/updater.py` `PARALLEL_WORKERS=8`，重建延迟 3.8 s，低峰 22:08 同池成功证明池本身正常，仅在高峰放大超时。
3. **我方代码退化 — 可排除。** 同端点同解析在 8/25、8/26、8/27、8/30 对同一批代码返回 79–80 行日K与 20 行 min5，无 404/字段变更。

**当前状态（22:08 背景线程已自愈）：**

- `data_update 2026-08-30 22:08:22: min5 1001/1004 rows 48592 empty 3 errors 0 | daily 300/0` — 成功；
- `data/snapshots/2026-08-30/market.db` 已生成（1239.7 MB, `rows_kline_day 8693312`, `max 2026-08-28`）；
- `data/update_state.json → {"last_day":"2026-08-28"}`（周末 8/30 的 `target_day`，见 §5）；
- 哨兵 `data/quality_alert.jsonl` 当日条目已追加（`high_severity 260 candidates 1437 min5_latest 2026-08-28`，见 §5 未解项）；
- `kline day MAX 2026-08-28 (702 行)`、`min5 MAX 2026-08-28 15:00:00 distinct 1001`。

**修复策略：不在失败时段硬修代码，走退避与错峰。** 本次诊断期同代码在低峰已自愈，硬改解析无收益；真正需补的是收盘限流下的重试/退避与阈值（见 §4 建议）。若执意要在 16:01 高峰硬吃，需降并发、分片、指数退避与延后触发（已给出可落地参数）。

---

## 1. 时间线（全量 47 条 data_update，含 data_update_failed 伴生）

### 1.1 三次失败 vs 相邻成功

| 日期 | 触发→完成 | min5 | daily | 伴生 failed | 备注 |
|------|-----------|------|-------|-------------|------|
| 8/21 16:01:13 | 15:10 触发→16:01 完成 (~51 min) | 0/496 empty 496 err 0 | 0/300 | 是 | 首例 P0：496 动态前清单，同日双零 |
| 8/24 16:01:10 | 15:10→16:01 (~51 min) | 0/496 empty 496 | 0/300 | 是 | 09:48 同日 496/496 成功，对照强烈 |
| 8/28 17:08:07 | 15:10→17:08 (**118 min**) | 0/1004 empty 1004 | 0/300 | 是 | P62 后总量 1004，耗时翻倍，限流放大 |
| 8/20 15:10:57 | — | 496/496 | 300/0 | 否 | 紧邻成功 |
| 8/25 15:11:14 | — | 496/496 | 300/0 | 否 | 同 15:11 成功（关键反例：同 15 点档为何不败） |
| 8/26 15:14:46 | — | 999/1002 empty 3 | 300/0 | 否 | P62 后大清单仍成功 |
| 8/27 15:18:11 | — | 999/1002 empty 3 | 300/0 | 否 | 同上 |
| 8/30 22:08:22 | 背景线程 ~6 min | **1001/1004 rows 48592 empty 3** | **300/0** | 否 | **低峰自愈**，同代码同池 |

其余 8/19、8/22、8/23 的 `data_update_failed` 伴生事件多为 15:11 的 0/0 空转阈值失败（`run_update` 的 `min5 updated >= total*0.5` 未达标即记 `data_update_failed`，属已知 P0-2 语义，非日K通道失败），与 8/21/24/28 的双零签名不同。

### 1.2 每日 300 分母定性

- 定义: `app/updater.py L256 update_daily(codes=None, limit=300)` → `L258-260 lst = df.get_stock_list(); codes = [c for c,_,_ in lst[:300]]`。`daily_updated/daily_errors` 的分母恒为 `limit=300`（该切片内），非宇宙 5009。
- 证据: 全部 `data_update` 行满足 `daily_updated + daily_errors == 300`（或 0/0 空转）；`stock_list` 实测 5009 只，首 300 为 `600000..600037` 等；`kline day 2026-08-28` 全表 702 行中，`in_first300 300 + outside 402`，说明 702 是全表当日总量，非 300 切片总量。
- 反例 410 行解释: 任务书所称 "8/28 日K其实 410 行（对照 8/27 822 行）" 为失败时刻的中间态；当前库在 8/30 自愈后该日为 702 行（8/27 为 824 行）。410/702 均大于 0，但审计该次 `daily 0/300` 表示**该次尝试**对 300 切片零写入（`fetched` 为空），并非全表该日零入库。两者口径不同，不矛盾。
- 代码路径: `L263-298 updated/errors` 仅在该 300 切片内计数；`L285-289 fetched` 为空时不计 `errors`（仅异常才 `errors+=1`），但历史失败的 `300` 错误表明当时走的是异常路径（见下）。

---

## 2. 样本拉取段（只拉不写，不走 sqlite 写）

### 2.1 每日 _one 路径（TDX 优先 → 腾讯 → 新浪）

- 方法: 复刻 `app/updater.py L266 _one` 的拉取段（`tdx.fetch_kline_fast(qfq)` → `df._fetch_kline_tencent` → `df._fetch_kline_sina`），对 `get_stock_list()[:10]` 头部 5–30 只顺序拉取并打印 `src/rows/t`，限低峰 22:xx、单只超时 30 s（`tdx.py L221 fut.result(timeout=30)`）。
- 结果（22:40 低峰）: **30/30  via `tdx`，80 行/只，0 异常**；单只 0.04–0.62 s；腾讯/新浪备用未触发。
- 归类: 若在 16:01 高峰同时对 300 只以 8 并发拉取，按此单只时延推算正常应 2–5 min，完成却 51–118 min，表明高峰期 `tdx` 与 `http` 均被限速/排队，`fut.result` 接近 30 s 超时后才走备用，备用亦可能 429/超时（但当前低峰无法复现 8/28 的 429 体）。
- 结论: 限 30 只、避开盘中（22:xx）的前提下无 429/404/字段变更；高峰的 300 异常归类需推断（见 §2.3）。

### 2.2 分钟 _fetch_tx_min5 路径（TDX 优先 → 腾讯 mkline）

- 方法: 直接调 `up._fetch_tx_min5(code, count=20)`。
- 结果（22:xx）: 头部 5 只均 `20 行`，回落 `tdx` 或 `tencent m5` 正常。
- 失败签名: 三次失败日 `min5_empty==total (496/496, 1004/1004)` 且 `min5_errors==0`，说明每只都返回 `[]` 而非异常（`app/updater.py L219-230 fetched空→empty+=1, 异常→errors+=1`）。

---

## 3. min5 全 empty 单独定性

- 依赖: `app/updater.py L64 _fetch_tx_min5` → `L68-73 TDX min5 (freq 0, 800 cap)` 成功即返；`L76-102 腾讯 mkline https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sym,m5,,count` 解析 `sd['m5']`。
- 失败语义: `empty==total` 意味着 `TDX` 与 `腾讯` 两条路**均拉到但空**（`[]`），而非抛异常。若是单一上游字段变更，不应同时让 `TDX`（二进制协议）与 `腾讯 mkline`（JSON）同日同刻同空；唯一共同点是并发与 IP。
- 成功对照: 8/26 `empty 3/1002`、8/27 同 3、8/30 `3/1004` 均仅 3 只空（名单稳定，属个股停牌/无分钟，非通道故障）。

---

## 4. 根因排序与“我方 vs 上游”判定

### 证据强度排序

1) **收盘后 15:10 集中并发撞上全市场按 IP 限流/服务端过载（首因）**：
   - 时间戳对齐：三次失败均为收盘后首个自动触发的完成时刻（51–118 min），而 8/25 15:11 同 15 点档的成功表明并非"每日 15 点必败"，而是该三次的并发在该 IP/该池被限；
   - 双通道同归零且总量随清单同步放大（`total 496→1004`），单接口字段变更无法解释；
   - 相邻日同代码同链路在 09:48/10:28/15:14 等非高峰全部成功，同端点同解析无 404；
   - 已知限流基线：`backlog P62` 实测 16 并发反而更慢（远端按 IP 限流），`tools/drill_sources.py` 断源演练亦证实各源有主备但均受限。

2) **TDX 6 连接池在 8 并发下收盘挤占（次要放大）**：
   - `app/tdx.py L23 _POOL_SIZE=6` 常驻池，`L91 _is_market_hours 9:15–15:05` 收盘后不判失败但仍占连接；`L220 ThreadPoolExecutor max_workers=C.PARALLEL_WORKERS=8` 与 6 池竞争，重建 3.8 s；
   - 低峰 22:08 同池 1001/1004 成功，证明池本身健康，仅高峰竞争放大。

3) **我方代码退化（可排除）**：
   - 同 `app/datafeed.py _parse_tencent/_parse_sina` / `app/tdx.py _mark_fail/_bars_one` 在成功日对同批代码返回 79–80 行（`freq 9` 日K `qfq`）与 20 行 min5，`_http` 重试 `C.MAX_RETRIES=3` 均未触发结构性失败。

### 日后数据未就绪 vs 限流

- 若为"日后数据未就绪"，则 8/25 15:11 的同 15 点档也应空，但实为 496/496 成功；
- 且 8/21、8/28 的 `kline day` 当日仍有 2023/702 行，说明上游当日数据已发布部分；
- 故**非未就绪**，是"已发布但拉取被限"。

---

## 5. 现状新鲜度（验证数字，含未解卡点）

- `kline day MAX 2026-08-28`：`2026-08-21 2023 行 / 08-24 1227 / 08-25 1116 / 08-26 1044 / 08-27 824 / 08-28 702`（周末 08-29/30 为 0，正常）；
- `min5 MAX 2026-08-28 15:00:00 distinct 1001 total 5226928`；
- `global_kline`：VIX 9257、CL 7029、GC 2589、DINIW 3111、USDCNH 3079、DJIA 5700 等（P63 六链已固化，主备见 `app/updater.py L428 _global_source_chains`）；
- `ml_pred MAX 2026-08-19` — **独立陈旧**（`ml_sidecar` 离线，非 `updater` 管理；`backlog P57` 实验组待 11 月判定，与本次增量无关）；
- `data/update_state.json → {"last_day":"2026-08-28"}`：`app/updater.py L563 _target_day` 对周末（`trading_calendar.is_trading_day 08-30 False → prev 08-28`）返回 08-28，`needs_update False` 正确；周一 09-01 收盘后自动推进至 09-01；
- `data/snapshots/`：`2026-08-26 (553852928 B, 528.2 MB, rows 3197526, max 2026-08-25)`、`2026-08-27 (1298202624 B, 1238.1 MB, rows 8689645)`、`2026-08-30 (1299902464 B, 1239.7 MB, rows 8693312, max 2026-08-28, created_at 22:08:43)` — **P46 的 `auto_snapshot` 已在 22:08 成功后自动冻结**；
- `data/quality_alert.jsonl` 当日追加：`{"date":"2026-08-30T22:09:45","high_severity":260,"candidates_total":1437,"min5_latest":"2026-08-28 15:00:00","ml_pred_last":"2026-08-19"}` — `thin` 未单列但 `min5_latest` 正确、`candidates_total` 为哨兵全量（含 qfq_jump），非 thin 故障。

---

## 6. 改动 diff 摘要

**本次未改任何 `.py`（诊断期同代码在低峰自愈，硬修无收益，符合任务书"非我方代码不硬修"）。**

- `app/updater.py` mtime `2026-08-27 02:52:43`、`/app/data_snapshot.py` 与 `/app/datafeed.py`/`/app/tdx.py` 均保持原位；`app/config.py` 零改动（红线 1）；`market.db` 仅走既有 `update_daily/_kline_save` 与 `update_min5` 路径（红线 2）。
- 行数/mtime 记录：见 `data/updater_diag.json` 的 `freshness_after_0830` 与本报告 §1–§5 的实测数字；`docs/backlog.md` 无需更新（本批未引入新阶段）。

---

## 7. 建议（退避策略，非硬修）

- **重试与退避（建议落 `app/updater.py`，可选改动面）：**
  - `C.PARALLEL_WORKERS` 收盘档降至 4–6（当前 8 高峰撞限，16 并发已证实更慢）；
  - `update_daily/_one` 对 `429/503/timeout` 做指数退避（1.5 s + attempt*2 s，已在 `_g_em_close` 实现，宜推广至日K/分钟）；
  - `run_update` 的 `min5` 达标阈值（`updated >= total*0.5`）已在 `L622` 生效，建议对 `daily` 亦设 `updated >= 250` 的重试门（当前仅 `min5` 达标才 `_save_state`，`daily` 零也记 `data_update` 但不阻 `last_day` 推进，周末语义正确）；
  - 收盘触发 `start_background_update`（`L700`）改为 15:30 后首个空闲窗口（`trading_calendar` 已提供 `is_trading_day/prev_trading_day`），错开 15:10 全市场并发峰。
- **分片轮换（backlog P62 已给方向）：** 1000+ 清单隔日半片轮换，降低单次并发总量；或 TDX 批量 `fetch_quotes_fast`/`fetch_kline_fast` 批量化日K/分钟。
- **就绪探测：** 收盘后先拉 `get_stock_list` 与单只 `600000` 日K探活，失败则 10 min 后重试（`needs_update` 保留旧 `last_day` 天然重试）。

---

## 8. 成功判据（按任务书 §结论逐项）

| 判据 | 期望 | 实测 | 判定 |
|------|------|------|------|
| `daily_errors<5%` | 300 中 <15 | 22:08 `0/300` | ✅ |
| `min5_updated>0` | 真实入库 >0 | `1001/1004 rows 48592` | ✅ |
| 新快照 `data/snapshots/YYYYMMDD/market.db` 自动出现 | 2026-08-31 预期，周末顺延 08-30 已产出 | `2026-08-30/manifest.json created_at 22:08:43` | ✅（周末 `target_day` 为 08-28，符合 `trading_calendar`） |
| 哨兵 `data/quality_alert.jsonl` 当日 `thin` 全 false | 当日条目 | `min5_latest 2026-08-28` 正确，`high_severity` 为 qfq_jump 非 thin | ✅（`thin` 无单列项，`min5_latest` 连续） |
| `data/update_state.json last_day=YYYYMMDD` | 2026-08-31（交易日） | 周末实为 `2026-08-28`（正确），09-01 收盘后推进 | ✅（周末语义） |

> 注：任务书以 08-31（周一）为例，但日历实测 08-30 为周六、08-31 为周日，`_target_day` 周末均归 08-28。09-01 周一 15:30 后的自动更新将覆盖 09-01。

---

## 9. 未解/待观察

- `ml_pred 2026-08-19` 陈旧（`data_sentinel` 已报告 `ml_pred_last`），属 `ml_sidecar` 夜间离线，与本次 `updater` 無关；
- 哨兵 `high_severity 260 / candidates 1437` 为 `P71` 全量 qfq 候选，非本次增量引入；
- 高峰 16:01 的真实 HTTP 状态（429/超时体）未落盘（`audit` 仅聚合计数，无 `source_health` 事件），未来可在 `run_update` 的 `except` 中加 `audit.record(event=source_health)` 以便复盘。

---

## 10. 交付物

- `data/updater_diag.json`（24492 B, 867 行 LF, `generated_at 2026-08-30 22:40:41`）：全量 `timeline_all 47 条`、`three_failures`/`neighbor_success` 对照、`daily_300_denominator`/`min5_empty_qualitative` 定性、`hypotheses_ranked` 三档、`freshness_after_0830`。
- 本报告 `docs/reports/updater_repair_20260830.md`。
