# 工程面 19+2 单验收报告（2026-09-15 补完）

> 验收对象：`docs/reports/audit_eng_20260913.md` + `tmp/dispatch_eng_20260913.md` 派出的 F1-F4 / G1-G4 / H1-H4 / I1-I4 / J1-J5 共 21 单
> 验收方式：只读取证 + 实跑复现（不采信报告自述），运行期证据取自 `data/audit/audit.jsonl`、`data/close_update_cron.log`、`tmp/watchdog.log`、`data/logs/watchdog_child_stderr_*.log`、`data/market.db`
> 说明：用户当时告知 J4/J5 未完成，实测两单均已于 09-14 上午交付，本报告一并验收

---

## §0 一句话结论

21 单中 **16 单通过、2 单部分达标、3 单证据受限**；验收过程中发现并修复 **1 个 P0（J3 遗留 NameError 导致收盘更新全线崩溃、09-14 全市场日K缺失）**、**1 个 P0（陈旧 .git/index.lock 使夜间自动提交连续两晚静默空转）**，以及 G2 看护自匹配、J3 索引重建共 3 处早前已修的问题。P0 修复后 **09-15 全交易日 11 次收盘更新全部 exit=0，零 NameError**，09-14 缺口已回填至 5000 行。

---

## §1 验收期间我做的修复（全部已落盘，需向用户披露）

| # | 文件 | 问题 | 修法 | 验证 |
|---|---|---|---|---|
| 1 | `tools/service_watchdog.py:663-683` | **G2 交付的三重确认自匹配**：`CommandLine -like '*main.py*'` 扫全部进程，承载该查询的 powershell 自身命令行含 main.py → 永不通过 → 自动恢复永久失效（09-14 00:10/00:25/00:40 三次拒绝，服务宕至 07:55） | 先用 `Name -eq 'pythonw.exe'/'python.exe'` 限定，再匹配命令行；并 `pids = [p for p in pids if p != os.getpid()]` 双保险 | `confirm_no_duplicate()` → `True | ok`；07:55:19 看护自动拉起成功 |
| 2 | `app/datafeed.py:114`、`app/datafeed.py:877`、`app/updater.py:41`、`tools/import_min5.py:31`、`tools/split_db.py:30` | **J3 索引删除不持久**：`CREATE INDEX IF NOT EXISTS` 5 处会重建已 DROP 的 `idx_kline_cp` / `idx_kmin5_c` | 5 处全部注释并写明理由；趁宕机窗口重新 DROP | `sqlite_master` 现存 14 索引，`idx_kline_cp`/`idx_kmin5_c` 均不存在；库体积 1.977GB → 1.841GB |
| 3 | `app/updater.py:20`、`app/global_market.py:25` | **P0：J3 引入 `_db.open_ro/open_rw` 但两个文件都没 import `_db`** → `_daily_max_dates()` 抛 `NameError` → `run_update()` 立刻死 → 收盘更新全崩（09-14 exit=1 共 6 次），且 `start_background_update()` 的 `except Exception: pass` 把异常吞掉，只在 `data/close_update_cron.log` 留痕 | 两文件补 `from . import db as _db`（与 earnings/server/limitup 同写法）；先确认 `app/db.py` 仅依赖 pathlib/sqlite3/threading，无循环导入 | 全库 AST 复扫：`_db` 使用者 8 个文件全部可解析，0 个缺失；`data/close_update_cron.log` 09-15 共 11 次运行 **exit=0 全绿、NameError 归零**；09-14 日K 由 3 行回填至 5000 行 |
| 4 | `tools/git_nightly.ps1` | **P0：09-13 23:51 遗留 0 字节 `.git/index.lock`** → 此后每晚 `git add -A` 失败，但脚本无条件 `exit 0` → 计划任务 Last Result=0、日志照写、**连续两晚零提交**，290+ 文件无回滚点 | 删除陈旧锁与残留 `tmp_obj_IKbjKP`（9.91MB garbage）；脚本加固：无 git 进程且锁龄 ≥10min 才清锁、add/commit 失败写 `FAIL` 行并 `exit 1`、"nothing to commit" 仍 exit 0、`[Console]::OutputEncoding=UTF8` 修日志中文乱码 | `git status --porcelain` 恢复可用（292 项）；`git add -A --dry-run` 正常列出；保持 UTF-8 BOM + LF，中文路径未乱码 |

> 备注：修复 3 属 **J3 单的验收漏判**——我 09-14 上午只验了 PRAGMA 生效与索引删除，没有跑 `_daily_max_dates()` 这条真实调用路径。J4 的 244 个冒烟用例也没抓到（只做 import 级检查，未调用该函数）。已在 §4 派单里补上"导入即调用"级防线。

---

## §2 逐单判定

### F 道（数据正确性）
| 单 | 判定 | 证据 |
|---|---|---|
| F1 指数字段错位 | **通过（映射）/ 新缺口** | `sh000001` 09-11 = O3910.92 H3912.32 L3852.03 C3888.11，H≥max(O,C)≥L 关系正确、量级合理，字段映射已修对。**但指数日K 停在 2026-09-11**（sh000001/sz399001/sz399006 三个都是 max=09-11、n=1918），个股已到 09-15 → 指数更新未接入收盘主更新，见 §3-P1-① |
| F2 池 churn | **通过** | 报告判据全过；09-15 无 churn 相关告警 |
| F3 / F4 | **证据受限** | 判据依赖交易日实采；09-14 因 P0 崩溃当日数据不可用，09-15 已恢复正常，建议以 09-16 的实采再复核一次 |

### G 道（可靠性）
| 单 | 判定 | 证据 |
|---|---|---|
| G1 轮转感知 | **通过** | 8 项全过；`data/audit/audit.jsonl` 现行文件 + 日期归档文件命名一致 |
| G2 kill/abort | **不通过（已由我修复）** | 三重确认自匹配导致自动恢复失效，实测宕机 00:00-07:55；修复后已能自动拉起 |
| G3 告警语义 | **部分达标** | 升级阶梯与持久化在位；但运行期字段自相矛盾：`18:09:07 CRITICAL streak_s=67201` 而 detail 写"断更 139 分钟"；`22:34:40 WARN streak_s=0` 而 detail 写"断更 60 分钟" → streak 持续被清零，阶梯实际无法逐级升级。另 `tmp/watchdog.log` 显示 15:50→18:15 看护自身空档 2.4h（机器休眠），18:15:06 记"距上次心跳 8704s"，该场景未与 streak 状态机对齐 |
| G4 stderr 编码 | **通过（实效已显现）** | 09-15 日志中文可读；断连 traceback 由 09-14 的 234 条降到 09-15 的 **0 条**（J5 前端 abort 池 + G4 双保险）。但 `ThreadingHTTPServer` 未覆写 `handle_error`，属"上游不再断"而非根治，见 §3-P2-③ |

### H 道（性能）
| 单 | 判定 | 证据 |
|---|---|---|
| H1 指标向量化 | **部分达标** | 调用数 134,136 → 400（335x）达标；热态 5.12ms 未达 ≤3.0ms；**`chip_profile` 仍 20.97ms/次未向量化**，是筹码因子路径最大单项成本 |
| H2 回测预计算 | **通过** | 6 组对照逐位一致、PIT 篡改测试通过、315s → 9s（35x） |
| H3 回测并行 | **通过（判据内）/ 两项未达** | 并行逐位一致、缓存命中、端到端 1500只×250日 WF5折 **432s**（原"跑两小时没完"已解决）；多线程 ≥2x 未达（GIL）、WF ≥3x 未达（1.37x），报告已如实披露 |
| H4 基准护栏 | **通过（护栏有效）** | `bench_score` EXIT=1、`score_stock_ms 5.92 → 9.03（+52.6%）` 正确拦截退化且未污染基线（`data/bench_baseline.json` mtime 仍为 09-13 23:04）。但基线本身是在有并发负载时采的（我空闲态实测热态 5.12ms），需在静默态重采一次，否则护栏会长期误报 |

### I 道（回测/因子治理）
| 单 | 判定 | 证据 |
|---|---|---|
| I1 earnings | **通过** | 实测 **77,688 行**（判据 ≥2万），区间 2017-09-30 ~ 2026-12-31 |
| I2 统一评分开关 | **通过** | `SCORING_UNIFIED = False`（默认关）；注意 `app/engine.py:861` 该开关打开时会绕过 H2 预计算（性能换口径，报告已披露） |
| I3 可复现回测 | **部分达标** | `data/bt_runs.jsonl` 已落 28 条、字段齐（codes_hash/params_hash/data_fp/deps_fp/result_hash/offline_guard）；禁网守卫、date.today 清单通过。**但 --verify 路径把代码前导零吃掉**：母跑 20 只（'000001','000858','002415'…）→ 验证跑只剩 15 只且变成 '1','858','2415','2','2594'；同一母跑 `20260913_215421_c788` 的两次 verify（215427 / 215433）result_hash 分别为 `67483b32…` 与 `ca554482…` **不一致**，且都与母跑 `87540d6f…` 不同 → "3 连跑哈希相同"只在同样被污染的输入之间成立，可复现性未被真正证明 |
| I4 退出策略 | **通过** | 216 用例 0 偏差、周报对拍、reason_code 12 分支全覆盖；09-15 `exit_slippage_daily` 正常出账（09-14 为 NO_SAMPLE，因当日无卖出） |

### J 道（HTTP/前端/工程卫生）
| 单 | 判定 | 证据 |
|---|---|---|
| J1 传输治理 | **通过** | keep-alive 5/5；首屏 386KB（基线 1.49MB）；二次加载 0KB（全 304）；favicon 749B；vendor immutable |
| J2 慢接口缓存 | **部分达标** | 六条路由 SWR 生效；09-15 复测 `/api/sectors` 550/181ms、`/api/sector/flow` 301/145ms（09-14 上午实测的 3274/4763ms 回归已消失）。**但两项未达**：① `/api/tactics` 热态实测 599-920ms，非报告的 19ms；② **冷启动悬崖**——服务重启后 `_API_CACHE` 为空，`_cached_api` 的 `ent is None` 分支在请求线程里同步算（`app/server.py:783`），且冷路径无单飞（`_API_REFRESHING` 只在 SWR 分支用），实测第一次 36.9s、第二次 >60s 超时，同期 `/api/overview` p95 被拖到 6.2-18.4s。`tools/warm_cache.py` 已写好但 **未接入启动流程**（`serve()` 只做 `_warm_static_gz()`，模块级只预热 sector 与情绪历史） |
| J3 SQLite 治理 | **通过（经我修复后）** | PRAGMA 全生效（mmap 512MB / cache 128MB / temp_store MEMORY / ro=query_only / rw=synchronous NORMAL）、线程本地复用正常；索引问题已修（§1-2）。**但 J3 同时引入了 §1-3 的 P0 NameError** |
| J4 依赖锁定 + 冒烟集 | **通过（本次实跑复核）** | 我在 09-15 实跑：`244 passed, 9 xfailed in 22.66s`（判据 ≤60s、离线可跑）；`requirements.txt`/`requirements.lock`/`tools/preflight.py` 齐备。**缺口**：用例停在 import 级与纯函数级，抓不到 `_db` 未导入这类"导入成功但一调就炸"的问题 |
| J5 前端模块化 | **通过（静态核验）** | `web/js/main.js`、`core/{dom,api,state,timer}.js`、`pages/*.js` 13 个、`app.legacy.js` 回退副本、favicon 749B 全部在位；今日 stderr 断连 traceback 归零，与 J5 的 visibility 暂停 + abort 池一致。**未做浏览器真机复测**（报告自述已用 CNGC 实测，我不重复） |

---

## §3 新发现问题（按优先级）

### P0（已修，见 §1-3 / §1-4）
① `app/updater.py` + `app/global_market.py` 缺 `from . import db as _db` → 收盘更新全崩、09-14 全市场日K缺失。
② 陈旧 `.git/index.lock` → 夜间自动提交静默空转两晚。

### P1（未修，建议派单）
① **指数日K 停在 2026-09-11**：`sh000001`/`sz399001`/`sz399006` 三个指数 max(date)=09-11、n=1918，个股已到 09-15。收盘主更新不覆盖指数 → 所有以指数日K为输入的择时/情绪闸门/回撤基准都在用 4 天前的数据（且 09-14/09-15 两天大盘变动完全看不到）。F1 只修了字段映射，没修更新链路。
② **成交额缺失率异常**：`amount=0` 占比 09-03~09-06、09-08~09-11 均为 0.0-0.2%，但 **09-07=50.0%（2501/5006）、09-14=77.8%（3888/5000）、09-15=9.0%（451/4994）**。09-14 是崩溃后回填造成的（回填源不带成交额）；09-07 与 09-15 说明常规链路也有间歇性缺额。放量突破/量比/量价因子全部依赖 amount，这三天信号不可信。`amount_zero_guard` 已 CRITICAL 连击 3 日。
③ **收盘接力永不达标、每 30 分钟空转重跑**：09-15 共 11 次运行，`ratio` 恒定 0.9368、`pending_remain` 恒定 337，22:00 关窗时记 WARN 收尾。337 只大概率是停牌/退市/新股（当日本就无行情），被算进分母 → 阈值 0.95 结构性不可达，白跑 11 轮全量更新。日志里还出现 `^C^C`（A4 中断留痕）。
④ **情绪周期表几乎为空**：`qg_sentiment_history` 只有 **1 行**，`qg_zt_full` max=2026-08-21（滞后 3 周+）。而 `app/zt_ecosystem.py:22` 明确"窗口内不足 80% 样本视为预热中 → None" → 实盘情绪温度大概率长期返回 None，C3 验证通过的"B4 高潮情绪闸门"在实盘可能恒不生效。
⑤ **`_cached_api` 冷启动悬崖 + 冷路径无单飞**（详见 §2-J2）：看护每天会重启服务，重启后第一批用户请求必然吃 37s+，并在 GIL 上拖垮 `/api/overview`（实测 p95 18.4s）。`tools/warm_cache.py` 已具备能力但没接进 `serve()`。
⑥ **I3 --verify 路径吃掉股票代码前导零**，导致验证跑与母跑不是同一个股票池，可复现性结论无效（详见 §2-I3）。

### P2（工程卫生）
① `app/config.py` 中 `BACKTEST_OFFLINE = True` 重复定义两次（:746、:754），I 道追加块有重复。
② G3 `streak_s` 与 detail 文案不自洽（0 vs 60 分钟、67201s vs 139 分钟），升级阶梯形同虚设。
③ `app/server.py:1917` 直接用 `ThreadingHTTPServer`，未覆写 `handle_error` → 客户端提前断开时 socketserver 仍会打整段 traceback（09-14 单日 234 条、日志 344KB）。今日归零是靠前端不再断开，不是服务端根治。
④ `data/close_update_cron.log` 已 1.25MB 且无轮转（`.gitignore` 里 `*.log` 被忽略，但本地无上限）。
⑤ `global_kline` 只有 `market IN ('fx','us')` 两类、共 34,684 行；日/韩/欧/台/港股无本地历史落盘 → 外盘板块断源时无兜底（与用户"全球市场比较粗糙"的观感一致）。

---

## §4 建议派单（可直接投喂 AI，写权限互不重叠）

### 单 K1｜指数日K接入收盘主更新（P1-①）
写权限：`app/updater.py`、`app/datafeed.py`
1. 定位指数日K的现有取数函数（F1 修过的腾讯 fqkline 6 字段映射路径），确认它当前只被实时行情调用、未进收盘增量更新。
2. 在 `run_update()` 里增加指数补更步骤，覆盖 `sh000001`/`sz399001`/`sz399006`（以及系统实际引用的全部指数代码——先 grep 出清单再定），写入 `kline WHERE period='day'`，与个股同一次事务、同样幂等 upsert。
3. 补历史缺口：09-12 之后所有缺失交易日全部回填。
4. 判据：① 三个指数 `MAX(date)` == 个股 `MAX(date)`；② 连续 3 个交易日复验不回退；③ 新增 `audit` 事件 `index_daily_updated`（含 n_codes / max_date / 缺口回填条数）；④ 失败必须 `audit.record(level='ERROR')`，**禁止 `except Exception: pass`**。
5. 加一条 J4 冒烟用例：调用真实的指数补更函数（可用 monkeypatch 的假 HTTP），断言写库行数与字段关系 H≥max(O,C)≥L。

### 单 K2｜成交额缺失根治 + 定向回填（P1-②）
写权限：`app/updater.py`、`app/datafeed.py`、新增 `tools/backfill_amount.py`
1. 先归因：对 09-07 / 09-14 / 09-15 各取 20 只 amount=0 的票，逐源（腾讯/东财/TDX/mootdx）实测哪个源能给 amount，确认是"源缺字段"还是"字段错位"还是"回填路径没带 amount"。
2. 写 `tools/backfill_amount.py`：按日期区间扫描 `amount IS NULL OR amount=0` 且 `volume>0` 的行，用可用源定向补齐；只 UPDATE amount 列，不动 OHLCV；带 `--dry-run` 与逐日汇总。
3. 主链路加固：`update_daily` 写入前若 amount 缺失，走"备用源补额"分支；仍缺则记 `kline_amount_missing`（已有事件）并在日结里给出当日缺额率。
4. 判据：① 回填后 09-07/09-14 的 amount0 占比 ≤1%；② 连续 3 个交易日当日 amount0 ≤0.5%；③ `amount_zero_guard` 不再 CRITICAL；④ 回填脚本对同一区间重跑结果逐位一致（幂等）。
5. 红线：不得修改任何 OHLCV；不得改前复权锚定；回填前后各做一次 `data/snapshots` 快照。

### 单 K3｜收盘接力达标口径修正（P1-③）
写权限：`app/updater.py`
1. `_daily_coverage()` / `close_fallback_needed()` 的分母改为"当日应有行情的票"：剔除停牌（当日无成交）、退市（`data/delisted_*` 清单）、以及上市日 > 目标日的新股。
2. 把剔除明细落到 `audit`（`coverage_universe_excluded`，含分类计数），使 ratio 可解释。
3. 接力空转保护：同一交易日连续 2 轮 `pending_remain` 完全不变 → 停止重跑，改记 `close_catchup_no_progress`（WARN），不再每 30 分钟烧一轮全量更新。
4. 判据：① 09-15 口径重算后 ratio ≥0.95 且 `pending_remain` 落在"确实无数据"的票上；② 空转保护能在 2 轮后触发；③ 不改 `run_update` 的写库语义（幂等性回归用例通过）。

### 单 K4｜情绪周期历史表补数（P1-④）
写权限：`app/zt_ecosystem.py`、新增 `tools/backfill_zt_history.py`、`app/server.py` 只读展示分支
1. 查清 `qg_zt_full` 为何停在 2026-08-21、`qg_sentiment_history` 为何只有 1 行（是取数源被封、还是写入路径从未被调用）。
2. 用本地 `limit_pool`（max=20260915、1599 行）+ `kline` 反推历史涨停池，回填 `qg_zt_full` 至 ≥250 个交易日；据此重建 `qg_sentiment_history`。
3. 判据：① `qg_sentiment_history` 行数 ≥ 有效窗口要求的 80%（`zt_ecosystem.py:22` 的预热门槛）；② 情绪温度对最近 20 个交易日返回非 None；③ 回填结果与"用当日实时数据现算"在最近 5 日逐位一致（自校验）；④ 严格 PIT——回填时不得使用晚于该日的信息。

### 单 K5｜接口冷启动悬崖 + 预热接入（P1-⑤）
写权限：`app/server.py`（仅 `_cached_api` / `serve()` / 启动预热线程）、`tools/warm_cache.py`
1. `_cached_api` 冷路径加单飞：`ent is None` 时也走 `_API_REFRESHING`，同一 key 只允许一个线程计算，其余请求立即返回轻量占位（`{"pending": true, "retry_after_ms": N}`）而不是各自同步算 37s。
2. `serve()` 启动后拉一个 daemon 线程调用 `tools/warm_cache.py` 的 `_warm_tactics()` + `/api/stocklist` + `/api/sentiment/history` 预热（tactics 必须走独立 subprocess，别在 Web 进程里算）。
3. 看护重启后（`tools/service_watchdog.py` 拉起完成）自动触发一次预热，避免"每次重启第一个用户吃冷启动"。
4. 判据：① 重启后首次 `/api/tactics` 客户端等待 ≤2s（允许返回 pending 占位）；② 预热完成后热态 ≤500ms（当前实测 599-920ms，顺带查清为什么不是报告里的 19ms）；③ 预热期间 `/api/overview` p95 ≤1s（当前被拖到 6.2-18.4s）；④ 并发 5 个冷请求只产生 1 次实际计算（用计数器断言）。

### 单 K6｜I3 可复现性修复 + J4 防线升级（P1-⑥ / P2-J4）
写权限：I3 的回测 CLI 入口、`tests/`
1. 定位 `--verify` 路径把 `'000001'` 变成 `'1'` 的地方（大概率是 `int()`/`%d`/numpy 整型数组/`json` 数字往返），改为全程字符串并保留前导零；`codes_hash` 用规范化后的字符串列表计算。
2. 重跑 I3 的 3 连跑 + verify：母跑与验证跑必须 **同一 codes 列表**，`result_hash` 三者逐位一致；把新记录追加进 `data/bt_runs.jsonl`。
3. J4 冒烟集加一类"导入即调用"用例：对 `app/` 每个模块，挑出其公开只读函数中无副作用的若干个（白名单维护），用最小 fixture 真调一次，任何 `NameError`/`AttributeError`/`ImportError` 直接判失败。至少必须覆盖 `updater._daily_max_dates`、`updater.close_fallback_needed`、`global_market` 的读库函数（这三个正是本次 P0 的漏网点）。
4. 判据：① verify 与母跑 codes 完全相同且 hash 一致；② 新用例能在注入"删掉 `from . import db as _db`"后立刻变红（自证有效）；③ 全套仍 ≤60s、离线可跑。

### 单 K7｜工程卫生打包（P2 全部）
写权限：`app/config.py`、`app/server.py`（仅 `handle_error`）、`app/audit.py`（仅 G3 streak 字段）、`tools/*` 日志轮转
1. `app/config.py`：删掉重复的 `BACKTEST_OFFLINE`（:746 / :754 保留一处），清理 I 道重复追加块；加一条 J4 用例断言"配置模块内同名常量不得重复定义"（AST 扫描）。
2. `app/server.py`：`class Server(ThreadingHTTPServer)` 覆写 `handle_error`，对 `ConnectionAbortedError`/`ConnectionResetError`/`BrokenPipeError` 静默（计数进 `http_client_abort` 已有事件），其余异常照旧打印。
3. G3：修 `streak_s` 与 detail 文案不自洽——统一以"上次心跳到现在的秒数"为 streak，机器休眠导致的空档要能与"看护真死"区分（可用 `tmp/h1_watchdog_state.json` 里的墙钟与单调钟双记）。
4. 日志轮转：`data/close_update_cron.log`（1.25MB）与 `data/logs/watchdog_child_stderr_*.log` 加按大小/天数轮转，保留 ≤30 天。
5. 判据：① 人工制造一次客户端提前断开，stderr 不再出现 traceback 但 `http_client_abort` 计数 +1；② 配置重复定义用例通过；③ 轮转后旧文件按规则归档且不丢当日日志。

---

## §5 运行期证据快照（09-14 / 09-15）

- `data/audit/audit.jsonl`：1921 条（09-13:196 / 09-14:1727）。事件覆盖 `api_slow`(1491)、`trading_event`(135)、`loop_phase_latency`(54)、`heartbeat`(44)、`api_slow_summary`(40)、`quality_alert`(28)、`buy_px_dual`(72)、`strategy_buy`(2)、`strategy_sell`(3)、`http_client_abort`(2)、`close_catchup_deadline`(2)、`data_snapshot_forced`(1)、`snapshot_cleanup_tiered`(1)、`exit_slippage_daily`(4)。
- **未出现的埋点**：`board_decision_latency`（`app/trader.py:1655` 已实现，但门槛是"300s 窗口且样本 ≥20"，两日均未触发）、`quote_channel_degraded`（`app/datafeed.py:542`，无降级属正常）。
- `api_slow` 热点（09-13~09-14 合计）：`overview` 1238 次 / avg 1483ms / max 19887ms；`kline` 52 次 / avg 18698ms / max 30611ms；`score` 41 次 / avg 2400ms；`sentiment/gate` 37 次；`moneyflow` 29 次；`limitup` 19 次；`sectors` 14 次 / max 12125ms；`tactics` 6 次 / max 36844ms；`minute` 3 次 / max 18573ms；`ai/morning` 3 次 / max 44434ms（LLM，属预期）。
- 09-15 收盘更新：11 次运行全部 exit=0，ratio=0.9368、pending=337；`data/snapshots/2026-09-15/market.db` 已落盘。
- 09-14 交易：09:47:46 买入 2 笔（000722 湖南发展 15.586×1100、603936 博敏电子 21.612×600），全天无卖出；`exit_slippage_daily` NO_SAMPLE。
- 服务进程：09-15 10:20:11 起新实例，stderr 6.4KB、断连 traceback 0 条。

---

*报告生成：2026-09-15，验收方。修复项均为最小改动，未触碰 `data/account.json`、`data/app.lock`、`data/audit/*` 写入语义，未执行 git commit。*


---

## §6 追加：上一轮 A-E 道 22 单的复验（用 09-14 / 09-15 实测数据）

> 上一轮验收记录：`docs/reports/acceptance_dispatch22_20260913.md`（93/100 有条件通过，20 单通过、A1/B1 待交易日实测）与 `docs/reports/acceptance_abcde_20260913.md`。
> 本节用 09-15 收盘后的真实数据兑现当时的"挂账（下轮亲验）"清单，并对被推翻的结论做降级。

### 6.1 挂账项逐条兑现

| 挂账项 | 当时结论 | 09-14 / 09-15 实测 | 复验判定 |
|---|---|---|---|
| A1 覆盖率连续 2 日 ≥0.95 | ⏳ 待实测 | 09-14 日K 仅 3 行（J3 NameError，非 A1 之过）；09-15 修复后 `ratio=0.9368`、`pending=337`，11 轮接力均未过线 | **不通过**（口径缺陷见 K3；A1 的等锁/批次修复本身仍成立） |
| A2 amount=0 残留 | ✅ 09-09 起残留 0 行 | 09-14 = **3888/5000（77.8%）**、09-15 = **451/4994（9.0%）**；09-15 单日 `kline_amount_missing` **800 条**、`amount_missing_by_source` 40 条、`source_degradation_daily` 11 条 | **回归（推翻）** → K2 |
| A5 指数日K | ✅ 三指数各 1918 行、amount>0=100%、high<low=0 | 历史回填仍成立（1918 行、字段关系正确）；但 `MAX(date)` 三个都停在 **2026-09-11**，个股已到 09-15 | **历史通过 / 日常更新缺口** → K1 |
| B1 快照连续性 | ✅/⏳ 三日齐 | `data/snapshots/` 目录 09-08、09-09、09-10、09-11、09-14、09-15 **连续齐备** | **通过**；但注意 6.2-③ |
| D3 `/api/data_health` p95 ≤300ms | ⚠️ 我方单次 1.1s 待复测 | 09-15 全天 `api_slow` 榜单里 **data_health 已不再出现**；上榜的只剩 `overview`：59 次 / avg **3916ms** / max **43596ms** | **data_health 通过**；瓶颈转移到 overview → K5 |
| E3 调度错峰 + G3 告警阶梯 | ✅ 间隔 ≥20min、阈值 3→6 | 09-15 15:23:15 `watchdog_stale CRITICAL streak_s=60514`（16.8h）而 detail 写"断更 178 分钟"——与 09-14 的 `streak_s=0` vs "断更 60 分钟" 同型矛盾，**两天复现** | **部分通过**（错峰成立；streak 语义仍错）→ K7-③ |
| E2 的 J1「R2 判定差异天数」 | 挂账 | 本轮 I4 已做 216 用例 0 偏差 + 周报对拍，覆盖同一问题 | **通过（由 I4 兑现）** |
| C4 的 S3 保守臂数字 | 挂账 | `data/bt_runs.jsonl` 可复现性被 I3 的前导零缺陷污染（§2-I3） | **仍挂账**，须待 K6 修完重跑 |
| C3 的 B4 PASS 臂判据复刻 | 挂账 | 未复刻；且发现 **前置阻塞**（见 6.2-①） | **仍挂账** |
| B4 历史归档重命名 | 待批 | 未执行（`audit_20260910.jsonl` 首条仍是 2026-08-16） | **待你批** |
| B3 待删清单 8.59GB | 待批 | 未执行（正确，删除需批准） | **待你批** |

### 6.2 今天新暴露、且影响 A-E 既有结论的三件事

① **C3 的"B4 高潮情绪闸门"目前无法上线**：C3 研究报告 IS +0.80pp / OOS +0.69pp、10 臂里唯一 PASS，你手上还压着"是否上线"的批准。但实测 `qg_sentiment_history` **只有 1 行**、`qg_zt_full` 停在 **2026-08-21**，而 `app/zt_ecosystem.py:22` 规定"窗口内不足 80% 样本视为预热中 → 返回 None"。**即使现在批准接线，闸门在实盘也会恒返回 None、等于没装**。顺序必须是 K4（补情绪历史）→ 再复刻 C3 判据 → 才谈上线。

② **`audit_watch` 的快照告警口径与磁盘事实不一致**：21:30 的 CRITICAL 写"data_snapshot（快照冻结）连续 4 个交易日缺失"，但 `data/snapshots/2026-09-08 ~ 09-15` 目录**一个不缺**。真实情况是**正常快照路径连续 4 个交易日没走通**，全靠 22:00 的 `data_snapshot_forced`（B1 兜底，CRITICAL 级）无条件落盘救回来，同时 `data_snapshot_deferred` 连续 3 日升级。结论：**快照"有"但"带病"**——告警文字应改成"正常路径缺失/由强制兜底代偿"，否则会长期误读为数据丢失；根因（正常路径为何一直 deferred）需要单独查。

③ **`/api/overview` 已成为唯一且严重的瓶颈**：09-15 全天 59 次超阈值，平均 3.9s，**最长 43.6s**，而它是前端主轮询接口。09-13~09-14 合计 1238 次 / avg 1483ms / max 19887ms。J2 治理了六条路由却没碰 overview，K5 必须把它一并纳入（先做分段计时归因，再决定是缓存、拆接口还是异步化）。

### 6.3 两轮合计的账目状态

- **A-E 道 22 单**：17 单维持通过；A2 **推翻**（回归）；A1 **不通过**（口径缺陷）；A5 **降级**（历史通过/日常缺口）；B1 通过但带病；D3 通过（瓶颈转移）；C4-S3、C3-B4、B4 归档、B3 删除 **4 项仍挂账/待批**。
- **F-J 道 21 单**：16 通过、H1/J2 部分达标、F1/F3/F4 证据受限（详见 §2）。
- **验收方自身修复 4 处**（§1），其中 2 处为 P0。
- **未执行的待批动作 2 项**：B3 删除 8.59GB、B4 历史归档重命名。
- **新增派单 7 张**：K1-K7（§4），写权限互不重叠，可并行。
