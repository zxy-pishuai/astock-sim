# K1-K7 验收报告（2026-09-16 盘中）

> 派单来源：`docs/reports/acceptance_eng_20260915.md` §4（K1-K7）
> 交付报告：`K1_指数日K_20260916.md`、`K2_成交额_20260916.md`、`k3_20260916.md`、`k4_zt_history_backfill.md`、`k5_cold_start_warmup.md`、`k6_k7_20260916.md`
> 验收方式：只读取证 + 实跑复现 + 快照对拍，不采信报告自述。运行期证据取自 `data/audit/audit.jsonl`、`data/close_update_cron.log`、`tmp/watchdog.log`、`data/logs/watchdog_child_stderr_*.log`、`data/market.db`、`data/snapshots/2026-09-15/market.db`

---

## §0 一句话结论

7 单中 **4 单通过（K2/K3/K6/K7）、2 单部分达标（K1/K4）、1 单部分达标且引入 P0 连锁故障（K5）**。
验收中发现并已处置 **1 个 P1 数据回归（K1 抹掉指数成交额 1800 行，已从快照恢复）** 与 **1 个正在发生的 P0（看护盘中每 15 分钟误杀服务，12:15-13:10 连杀 3 轮）**。
冒烟测试集由 244 → **256 passed, 9 xfailed in 11.80s**。夜间自动提交已恢复（09-15 两次 auto commit）。

---

## §1 逐单判定

### K1 指数日K接入收盘主更新 —— 部分达标（引入 P1 回归，已由验收方修复）

| 判据 | 结果 | 实测证据 |
|---|---|---|
| ① 三指数 MAX(date) == 个股 | **通过** | `sh000001`/`sz399001`/`sz399006` 均 max=**2026-09-15**、n=1920（原停在 09-11、n=1918）；个股最后完整交易日同为 09-15（09-16 盘中仅 1 行） |
| ② 连续 3 交易日不回退 | **待观察** | 需 09-16/17/18 收盘后复验 |
| ③ 新增 audit 事件 | **通过** | `index_daily_updated`（09-16 00:12:59）：`n_codes=3, max_date=2026-09-15, backfilled=6, failed=0, per_code={sh000001:1920, sz399001:1920, sz399006:1920}` |
| ④ 失败必须 ERROR、禁裸 except | **通过** | `update_indices` 逐指数 try/except 记 `index_daily_update_failed`（ERROR，带 error 文本）；无 `except: pass` |
| ⑤ J4 冒烟假 HTTP 断言字段关系 | **通过** | `tests/test_k1_indices.py` 5 用例；OHLC 关系 H≥max(O,C)≥L 实测正确 |

**★ 回归（验收方发现并修复）**：`app/updater.py` 的 `update_indices` 用 `INSERT OR REPLACE` 整行覆盖，而 `fetch_index_kline` 的 `amount` **恒为 0**（腾讯 fqkline 无该字段，报告已披露此局限但**未意识到 REPLACE 会连带抹掉历史行的 amount**）。`days=600` 窗口 → A5 已回填的指数成交额被抹零 **600 行/指数 × 3 = 1800 行**，`amount>0` 覆盖率由 **100% → 68.8%**（2024-04 之后全为 0）。

处置：
1. `app/updater.py:619` 改为 UPSERT——`ON CONFLICT(code,period,date) DO UPDATE SET open/high/low/close/volume=excluded.*, amount=CASE WHEN excluded.amount>0 THEN excluded.amount ELSE kline.amount END`，即 amount 仅在新值 >0 时覆盖；
2. 从 `data/snapshots/2026-09-15/market.db` 回填 **1794 行**（仅 amount 列），三指数覆盖率恢复到 **99.9%**（n=1920、amount0=2）；
3. 复核个股行零误伤：09-15 个股仍 4994 行、amount0=1；OHLCV 未触碰。

残留：09-14、09-15 两天指数 amount 仍为 0（快照截止 09-11，无从恢复）→ 需接东财指数行情源（见 §4 K10）。
其余 `INSERT OR REPLACE INTO kline_min5`（:118、:241）无 amount 列，无同类风险。

### K2 成交额缺失根治 + 定向回填 —— 通过（本轮最干净的交付）

| 判据 | 结果 | 实测证据 |
|---|---|---|
| ① 09-07/09-14 amount0 ≤1% | **通过** | 09-07：50.0%（2501/5006）→ **0.0%（0 行）**；09-14：77.8%（3888/5000）→ **0.0%（1 行）** |
| ② 连续 3 交易日当日 ≤0.5% | **通过（2/3）** | 09-14 = 1 行、09-15 = 1 行（各 0.0%）；09-16 待收盘 |
| ③ `amount_zero_guard` 不再 CRITICAL | **通过** | 09-16 审计无 `quality_alert` CRITICAL、无 `amount_zero_guard` 升级 |
| ④ 幂等（重跑逐位一致） | **未复跑** | 报告声明通过；验收方不重跑写库脚本 |
| 红线：不改 OHLCV | **通过（快照对拍）** | 与 09-15 快照对比 400 只：**OHLCV 变动 = 0**；amount 变动 335 只，**全部是 0 → 非 0** |

历史残留：09-01~09-04 各 8 行（0.2%），在 ≤20 行判据内。

### K3 收盘接力达标口径修正 —— 判据①通过，②待今晚验证

| 判据 | 结果 | 实测证据 |
|---|---|---|
| ① 新口径 ratio ≥0.95 | **通过** | `coverage_universe_excluded`（09-16 00:32 与 12:38 各一条）：`total_all=5331, excluded_total=334`（停牌 24 + 退市 310 + 新股 0）→ 有效分母 4997；09-15 实际 4994 行 → **ratio=0.9994**（原口径恒卡 0.9368） |
| ② 空转保护 2 轮后触发 | **待验证** | `close_catchup_no_progress` 今日 **0 条**（需今晚 15:45-22:00 接力窗口才有证据） |
| ③ 不改写库语义 | **通过** | `run_update` 幂等 upsert 未变；今日审计无写库异常 |

小瑕疵：排除清单的"停牌"里含 `510300`（沪深300ETF）等基金代码，分类口径偏糙，不影响 ratio 结果。

### K4 情绪周期历史补数 —— 判据①②通过，③不通过

| 判据 | 结果 | 实测证据 |
|---|---|---|
| ① 历史长度达预热门槛 | **通过** | `qg_zt_full` 1852 → **1869 行**，区间 2019-01-03 ~ **2026-09-15**（原停在 08-21）；`qg_sentiment_history` 为**单行聚合表**（`period='2019_2026-09-15'`, `date_count=1869`, payload 含 `built_at/start/end/dates/phase_stats/methods/gate`，built_at=2026-09-16 00:33:30）——验收方原判据按"逐日行数"理解有误，按该表设计口径达标 |
| ② 最近 20 日情绪温度非 None | **通过** | `zt_ecosystem.recent_temperatures(days=20)` 返回 20 条、`temperature` **全部非 None**（如 09-11=42.1、09-14=59.6、09-15=33.6）；`eco_temperature('2026-09-15')` 0.03s 返回完整字典（zt_count=27, dt_count=24, broken_rate=0.4808, max_streak=5, lb_ge2=7 …） |
| ③ 回填与现算最近 5 日逐位一致 | **不通过** | 最近 8 日 `qg_zt_full.zt_count` vs `eco_temperature` 现算：**仅 1/8 一致**，回填系统性偏高 1~4 家（09-04：40 vs 38；09-07：91 vs 87；09-08：72 vs 69；09-14：55 vs 52；09-15：29 vs 27）。`max_streak` 8 日全一致 → 差异只在涨停家数的计数口径 |

**策略层影响（重要）**：判据②通过意味着 **C3 的「B4 高潮情绪闸门」现在才第一次具备实盘上线条件**（此前 `qg_sentiment_history` 仅 1 行、`qg_zt_full` 停在 08-21，`zt_ecosystem.py:22` 的 80% 样本门槛会让温度恒返回 None，闸门等于没装）。但判据③的口径分裂是新风险：`phase`（发酵/高潮）来自 `qg_zt_full`，实盘闸门读 `eco_temperature`，两套涨停家数差最多 13%（91 vs 87），**阈值若按一套标定、用另一套执行会误触发/漏触发** → 见 §4 K11。

### K5 接口冷启动悬崖 + 预热接入 —— 部分达标，且引入 P0 连锁故障

| 判据 | 结果 | 实测证据 |
|---|---|---|
| ① 重启后首次 `/api/tactics` ≤2s | **不通过** | 冷路径确实改为"单飞 + 立即返回占位"（`app/server.py:774-788`），客户端等待 ≈0ms——但拿到的是 `{"pending":true,"retry_after_ms":3000}`（**41 字节**）而非数据；实测重启后 `/api/sentiment/history` **持续返回 41 字节占位**，`/api/tactics` 约 3s 后才返回 32,845 字节真数据 |
| ② 预热后热态 ≤500ms | **部分** | `/api/stocklist` 8038ms → **25ms** ✓；`/api/sectors` 26/24ms ✓；`/api/tactics` 热态 **1064~2021ms**（仍 >500ms，也仍不是 J2 报告声称的 19ms） |
| ③ 预热期间 `/api/overview` p95 ≤1s | **严重未达** | 09-16 `api_slow`：**overview 63 次 / avg 13,159ms / max 52,413ms**；stocklist 6 次 / max **113,051ms**；quotes 4 次 / max 47,593ms；minute 6 次 / max 10,382ms。验收方午休（低负载）实测 overview 三连：**15,723ms / 55,658ms / 23,019ms** |
| ④ 并发 5 个冷请求只算 1 次 | **通过（代码层）** | 冷路径已纳入 `_API_REFRESHING` 单飞，`_API_CACHE_LOCK` 内判定+置位，无竞态窗口 |

**★ 前端契约缺口（P1）**：全项目 grep `pending|retry_after_ms`，前端**零处理**（只有无关的 `ai_pending`）。K5 改了后端返回契约却没动前端 → 重启后战法/情绪历史/回测池/复盘等页会先渲染空数据，直到下一次轮询（一次性加载的页面可能一直空着）。

**★ 预热逻辑缺陷**：`_start_warmup_thread`（`app/server.py:1964-1989`）里 `_warm_http` 拿到 `{"pending":true}` 也算"预热成功"，不重试 → 对 tactics/sentiment 实际只是"触发后台计算"，不是"填充完成"。

**★ 连带 P0（盘中重启循环）**：overview 慢 → 看护 l2 探测（原 `timeout=5`）恒失败 → 连续 3 次即杀进程。`tmp/watchdog.log` 实证 09-16 12:15-13:10：
```
12:15:02  异常 1/3：l2_overview=HTTP Error 500
12:20:06  异常 2/3：l1_tcp=down（进程已死）
12:25:06  异常 3/3 → 恢复触发 → 端口全死，直接重启
12:25:11  G2 三重确认未过，拒绝启动新实例：main_py_running_pids=[21996, 37660]
12:40:11  再次拒绝启动：main_py_running_pids=[21996]
12:45~12:55  异常 1/3~3/3：l2_overview=TimeoutError（半死僵尸）
12:55:13  终止 PID=38280 后重启 → 新 PID=27532
13:00/13:05/13:10  仍 TimeoutError → 13:10 再次恢复触发
13:10:11  8899 被非 main.py 进程占用（PID=[]），不终止、跳过恢复
```
即 **55 分钟内 3 轮杀-重启、2 次拒绝启动、服务实际不可用约 30 分钟**，且发生在交易日（幸在午休 11:30-13:00 区间）。根因链：TDX `fetch_quotes_fast` 整体超时 8s → 降级腾讯/新浪 → overview 11~56s → 看护 5s 判死。

处置（验收方，两处最小改动，`tools/service_watchdog.py`）：
1. `:75` 新增 `OVERVIEW_HTTP_TIMEOUT = 90`，`:125` 的 l2 探测由 `timeout=5` 改用该常量（实测 overview 最慢 55.7s）；
2. `:413` `effective_recover_threshold`：原先只在批处理窗口/标记期放宽，现改为**只要 `l1_tcp=ok` 且 l2 属"慢"形态（`_l2_degraded`：time_stale/Timeout/10061/10060）就放宽到 6 次（约 30 分钟）**。`_l2_degraded` **不匹配 HTTP 5xx**，故真死与 500 仍走 3 次快速恢复，不掩盖故障。

验证：`13:15:02 健康 OK：l1_tcp=ok | l2_overview=time_age_0s | l3_audit=audit_age_186s（连续失败已清零）` → **重启循环已停止**。
注意：这是止血，不是根治。overview 本身的 11~56s 必须治（见 §4 K8）。

### K6 I3 可复现性修复 + J4 防线升级 —— 通过

| 判据 | 结果 | 实测证据 |
|---|---|---|
| ① verify 与母跑 codes 相同且 hash 一致 | **通过** | 台账 `data/bt_runs.jsonl`（34 条）：00:10 先复现缺陷（codes 出现 `'1'`）→ 00:14 `K6-P1-fix` 后 codes 全六位合规；母跑 `20260916_001417_b4d6` 与两次 verify（`00:15:12` / `00:16:19`）**codes 相同、`result_hash` 三者逐位一致 = `b23fedb378c84f5c`** → 可复现性首次被真正证明（09-13 那对 `67483b32` vs `ca554482` 的矛盾记录按声明不回改） |
| ② 注入"删 `_db`"立刻变红 | **通过** | 用例内 `del updater._db` → NameError 断言成立；恢复后可调。覆盖 `updater._daily_max_dates`、`updater.close_fallback_needed`、`global_market` 读库函数——正是上一轮 P0 的三个漏网点 |
| ③ 全套 ≤60s、离线 | **通过** | 验收方实跑：**256 passed, 9 xfailed in 11.80s**（上一轮 244 passed → +12 用例）；conftest 禁网守卫在位 |

小瑕疵：可复现性验证的股票池只有 2 只（`'000001'`,`'600519'`），足以证明前导零修复，但对"回测可复现"的覆盖面偏弱，建议后续用 20 只 + 250 日再跑一次三连同。

### K7 工程卫生打包 —— 4 项全部落地（1 项待长期观察）

| 子项 | 结果 | 实测证据 |
|---|---|---|
| K7-1 config 重复定义 | **通过** | AST 全扫 `app/config.py`：**重复定义常量 0 个**；`BACKTEST_OFFLINE` 出现 **1 次**（原 2 次）、`SCORING_UNIFIED` 1 次；另有钉死用例 |
| K7-2 `handle_error` 覆写 | **通过** | `app/server.py:1939` 新增 `class Server(ThreadingHTTPServer)`、`:1947` 覆写 `handle_error`、`:1958` `serve()` 已改用 `Server`；连接断开类静默并计入 `http_client_abort`，其余异常仍 `super().handle_error` |
| K7-3 G3 streak 口径 | **通过** | 今日事件已变为 `watchdog 心跳早于最近开机/唤醒时刻（704 分钟前），疑似关机/睡眠`（`streak_s=None`）——不再出现 09-14 的 `streak_s=0` 却写"断更 60 分钟"、09-15 的 `streak_s=60514` 却写"断更 178 分钟"这类自相矛盾；休眠/关机与真死可区分 |
| K7-4 日志轮转 ≤30 天 | **通过（已有一次真实触发）** | `tmp/watchdog.log` 09-16 00:30:31：`K7-4 日志轮转：close_update_cron.log 超 5MB，归档为 close_update_cron_20260916_003031.log`。当前 `close_update_cron.log` 已重新长到 2.7MB（阈值 5MB 未触发，属正常）；`data/logs` 下 10 个 stderr 共 0.7MB，40 天清理规则未到条件 |

---

## §2 验收方本轮处置清单（需向用户披露）

| # | 对象 | 动作 | 验证 |
|---|---|---|---|
| 1 | `app/updater.py:619` | K1 的 `INSERT OR REPLACE` → `ON CONFLICT DO UPDATE`（amount 仅在新值>0 时覆盖，OHLCV 照常更新） | `py_compile` + `ast.parse` 通过；其余 2 处 `INSERT OR REPLACE` 属 `kline_min5`（无 amount 列），无同类风险 |
| 2 | `data/market.db` | 从 `data/snapshots/2026-09-15/market.db` 回填指数 amount **1794 行**（**仅 amount 列**，仅 3 个指数代码，仅 live 值为 0/NULL 的行） | 三指数 amount0：600 → **2**，覆盖率 68.8% → **99.9%**；09-15 个股仍 4994 行、amount0=1，OHLCV 零变动 |
| 3 | `tools/service_watchdog.py:75,125` | l2 探测读超时 5s → **90s**（新常量 `OVERVIEW_HTTP_TIMEOUT`） | `py_compile` 通过 |
| 4 | `tools/service_watchdog.py:413` | `effective_recover_threshold`：l1=ok 且 l2 属"慢"形态即放宽到 6 次（原仅限批处理窗口） | `13:15:02 健康 OK …（连续失败已清零）`，盘中重启循环停止 |

未触碰：`data/account.json`、`data/app.lock`、`data/audit/*` 写入语义；未执行 git commit（夜间 23:50 自动提交，09-15 已验证恢复正常）。

---

## §3 遗留问题（按优先级）

**P0**
① `/api/overview` 11~56s：TDX `fetch_quotes_fast` 8s 整体超时后降级腾讯/新浪，overview 同步等全链路。这是今日盘中故障的根因，也是 09-13 以来 `api_slow` 的绝对榜首（09-14：1238 次/avg 1483ms；09-15：59 次/avg 3916ms/max 43596ms；09-16：63 次/avg 13159ms/max 52413ms）。
② 前端不认 `{"pending":true}` 占位符 → 重启后相关页面空白。

**P1**
③ 看护"僵尸进程"处置缺口：端口 down 但 main.py 进程仍存活时，只会"拒绝启动、等下一轮"（今日 12:25、12:40 各浪费 15 分钟），应清理僵尸后再启。
④ 指数 amount 缺 09-14/09-15 两天（K1 源不带 amount，快照也无从恢复）。
⑤ 涨停家数双口径：`qg_zt_full.zt_count`（回填）比 `eco_temperature`（现算）系统性高 1~4 家，8 日中 7 日不一致 → 直接影响 C3「B4 高潮情绪闸门」的阈值标定与实盘执行一致性。
⑥ `/api/tactics` 热态 1~2s（J2 报告称 19ms，K5 判据要求 ≤500ms），两次验收均未复现报告数字。
⑦ K3 判据②（`close_catchup_no_progress` 空转保护）、K1 判据②（连续 3 日不回退）需今晚收盘后复验。

**P2**
⑧ `qg_sentiment_history` 为单行聚合表，`recent_temperatures` 返回的 `zt_count`/`phase` 均为 None（只有 temperature）→ 前端若展示这两个字段会空。
⑨ K3 排除清单把 ETF（如 `510300`）归入"停牌"，分类口径需细化。
⑩ K6 可复现性验证池仅 2 只，覆盖面偏弱。

---

## §4 下一轮派单建议（K8-K12，写权限互不重叠）

### K8（P0）｜`/api/overview` 根治
写权限：`app/server.py`（overview 分支）、`app/datafeed.py`（行情缓存层）
1. 先分段计时归因：把 overview 内部各段（指数行情 / 涨跌家数 / 持仓市值 / 情绪 / 板块）耗时打点，落 `audit` 事件 `overview_phase_latency`，跑一个交易日看谁是主凶。
2. 行情层加 SWR 短缓存（3~5s）：TDX 降级/超时期间直接返回上一份缓存 + `stale=true`，**绝不在请求线程里同步等 8s×N 批**。
3. 涨跌家数、板块统计等重计算移出请求线程（后台定时算 + 内存快照）。
4. 判据：① 盘中 overview p95 ≤1s、max ≤3s；② TDX 人为降级时 overview 仍 ≤1s（返回 stale）；③ `api_slow` 中 overview 单日 ≤10 条；④ 看护 l2 探测连续 1 小时 0 失败；⑤ 数据新鲜度不得退化——`time` 字段滞后 ≤10s，超过则显式标 `stale`。

### K9（P0）｜前端消费 `pending` 占位
写权限：`web/js/core/api.js`、`web/js/pages/*.js`（仅加载态处理）
1. `fetchJson` 统一识别 `{pending:true, retry_after_ms:N}`：自动按 N 毫秒重试（上限 5 次 / 30s），期间渲染骨架屏（`.skeleton` 已由 J5 提供），不覆盖已有数据。
2. 重试耗尽 → 显示 `.err-banner`（J5 已提供）+ "点此重试"，**不留空白页**。
3. 覆盖全部走 `_cached_api` 的路由：tactics / sentiment/history / backtest/pool / premarket / sentiment / review。
4. 判据：① 重启服务后立刻打开这 6 个页面，均不出现空白（骨架屏 → 数据）；② 真机浏览器验证（CNGC 或等价），控制台无未捕获异常；③ 占位响应不被当成"空数据"渲染成"0 只/无记录"。

### K10（P1）｜指数成交额接源 + 回填
写权限：`app/datafeed.py`、`app/updater.py`（仅 `update_indices`）、新增 `tools/backfill_index_amount.py`
1. 接东财指数行情（push2 系）取指数 `amount`，与腾讯 fqkline 的 OHLCV 合并；沿用现有熔断器模式，独立熔断不影响 OHLCV 主路径。
2. 回填 09-14、09-15（及后续任何 amount=0 的指数日）。
3. 判据：① 三指数最近 10 个交易日 amount>0 覆盖率 100%；② 与交易所公布的两市成交额抽样对拍误差 ≤1%；③ 写库仍走 UPSERT（不得回退成 `INSERT OR REPLACE`）；④ 新增 J4 冒烟用例：断言"新值为 0 时不得覆盖库内非 0 amount"。

### K11（P1）｜涨停家数口径统一（解锁 C3 闸门上线）
写权限：`app/zt_ecosystem.py`、`tools/backfill_zt_history.py`
1. 定位 `qg_zt_full.zt_count` 与 `eco_temperature().zt_count` 的计数差异（回填系统性偏高 1~4 家）：是否含 ST/新股/退市、是否用 `limit_pool` 代理、涨停价取整方式、是否计"触板未封"。
2. 选定唯一权威口径（建议以 `eco_temperature` 现算为准，因为它就是实盘闸门读的），据此重算并覆写 `qg_zt_full` 的 `zt_count`/`sentiment_score`/`phase`。
3. 判据：① 最近 20 个交易日两口径 `zt_count` **逐位一致**；② `max_streak` 保持一致（现已一致，不得退化）；③ 重算严格 PIT（不得用晚于该日的信息）；④ 重算前后 `phase` 变化清单落盘供人工复核；⑤ 完成后给出「B4 高潮情绪闸门」在新口径下的 IS/OOS 复测数字，供用户决定是否上线。

### K12（P1）｜看护僵尸进程处置 + 恢复可观测
写权限：`tools/service_watchdog.py`
1. 补上缺失的分支：**端口 down 但存在 main.py 进程**（今日 12:25 `main_py_running_pids=[21996, 37660]`、12:40 `[21996]`）→ 应判定为僵尸，走"轮询确认 kill → 清 `data/app.lock` → 重启"，而不是"拒绝启动、等下一轮"（今日因此白等 30 分钟）。
2. kill 后必须轮询确认进程真死（F5 已有该能力，复用），并校验 `data/app.lock` 内 PID 与实际存活进程一致（今日曾出现 lock=18972 而实际进程为 34096 的错配）。
3. 每次恢复动作落 `audit`（`watchdog_recovered` 已有，补 `action`/`reason`/`pids_killed`/`downtime_s`），并新增 `watchdog_refused`（拒绝启动）事件——今日两次拒绝只在 log 里，审计流看不到。
4. 判据：① 构造"端口 down + 僵尸 main.py"场景，看护一轮内完成清理并恢复（≤60s）；② 构造"lock PID 与实进程不符"场景，能自愈；③ 恢复/拒绝两类动作均有 audit 事件；④ 不得因本改动放宽真死判据（l1 down 仍 3 次即恢复）。

---

## §5 运行期证据快照（09-16 盘中）

- 审计：09-16 共 114 条。事件：`api_slow` 83、`trading_event` 18、`coverage_universe_excluded` 8、`api_slow_summary` 2、`watchdog_stale` 2、`index_daily_updated` 1；**CRITICAL/ERROR 0 条**。
- `api_slow`（09-16）：overview 63/avg 13159/max 52413ms；stocklist 6/avg 28773/max 113051ms；minute 6/avg 5287ms；quotes 4/avg 18618/max 47593ms；tactics 1/20924ms；kline 1/6611ms；score 1/5134ms；sectors 1/15286ms。
- 未出现：`close_catchup_no_progress`、`daily_coverage`、`data_update`、`stale_first_budget_exceeded`（均在今晚收盘链路，尚无证据）。
- 服务：13:10:15 起 PID 38180，`data/app.lock`=38180 一致；13:15:02 看护判定健康 OK。
- 当前实例 stderr 反复出现 `TDX fetch_quotes_fast 整体超时(8s) 批数=N → 降级腾讯/新浪补缺` → 上游行情源降级是 overview 慢的直接原因。
- 冒烟测试：`256 passed, 9 xfailed in 11.80s`。
- 夜间自动提交：`cee6d26 | 09-15 23:31 auto`、`1818e38 | 09-15 23:50 auto`（上一轮修复后已恢复正常）。

---

*报告生成：2026-09-16 13:20（盘中），验收方。本轮 4 处处置均为最小改动，未触碰账户/审计写入语义，未执行 git commit。*
