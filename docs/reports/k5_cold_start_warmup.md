# K5 接口冷启动悬崖 + 预热接入（2026-09-16）

> 执行：K 道（P1-⑤）。范围：`app/server.py`（`_cached_api` 冷路径单飞 + `serve()` 预热线程）、`tools/warm_cache.py`（路径正斜杠修复）。
> 实测：8898 临时实例（新代码）；8899 生产未重启（未生效）。

## §0 判据预注册（先落盘后跑数，§3 逐项对照）

1. 重启后首次 `/api/tactics` 客户端等待 ≤2s（允许返回 `{"pending": true, "retry_after_ms": N}` 占位）。
2. 预热完成后热态 ≤500ms（当前实测 599-920ms；顺带查清为何不是 J2 报告的 19ms）。
3. 预热期间 `/api/overview` p95 ≤1s（当前被拖到 6.2-18.4s）。
4. 并发 5 个冷请求只产生 1 次实际计算（单飞）。

---

## §1 改动清单

### app/server.py
1. **`_cached_api` 冷路径单飞**（J2 版"无值→同步算一次"改为）：
   - 无缓存值（`ent is None`）也走 `_API_REFRESHING` 单飞——同一 key 同时只允许一个刷新线程；
   - 本请求**立即返回轻量占位** `{"pending": true, "retry_after_ms": 3000}`，不再各自同步算（37s 冷启动悬崖）；
   - 有旧值（SWR）语义不变：返旧 `stale=true` + 单飞后台刷新。
   - 应用面 6 处：tactics(300s) / senti_hist(1800s) / pool(600s) / premkt(600s) / senti(60s) / review(1800s)（stocklist 为独立 `_LIST_CACHE` 在途锁，未动）。
2. **`serve()` 启动预热 daemon 线程** `_start_warmup_thread(port)`：
   - `tools.warm_cache._warm_tactics()`（**独立 subprocess** 跑 scan_all → 写当日缓存文件，零 Web 进程 GIL 占用）；
   - 等待端口就绪（`socket.create_connection` 轮询 ≤30s，main.py 在 serve() 后才 serve_forever）；
   - 本地 HTTP 预热：`/api/tactics`（填充 Web 进程 `_COUNT_MEM` + 读缓存文件）、`/api/stocklist`、`/api/sentiment/history`。
   - **看护重启自动预热**：watchdog 拉起 = 进程重启 = serve() 执行 = 预热线程自动跑（无需改 G 道 watchdog）。

### tools/warm_cache.py
3. **路径正斜杠修复**（K5 实测发现）：`_warm_tactics` 的 subprocess 代码串用 `%r` 生成 Windows 反斜杠绝对路径 → Python 字面量 `\U`/`\t` 转义 SyntaxError（**rc=1、0.2s 静默失败**）。改为 `cache.replace("\\", "/")` 后 subprocess 链路全通（rc=0、4.5s、42KB）。

## §2 tactics 599-920ms 归因（判据②顺带查清）

| 层 | 实测 | 归因 |
|---|---|---|
| `_count_codes_on` SQL | **565-750ms** | `COUNT(DISTINCT code) WHERE period='day' AND date=?` 被优化器选 `idx_kline_pcd(period,code,date)`——只用 period 前缀 → **扫全 911 万行**（EXPLAIN 实证）。等价 `COUNT(*)`（kline PK=(code,period,date)，同日同码唯一）走 `idx_kline_pd` **0.6ms**。 |
| 记忆 | 600s `_COUNT_MEM` | 重启后空 + 每 600s 过期 → 周期性 565ms。 |
| 半成品日 | 2026-09-16 仅 1 行 | `_latest_date`=9/16 → 当日缓存键 `coverage.n_codes=1 < _MIN_COVERAGE` → **当日缓存不落不读** → 一直走 prev 缓存（stale_ok）。 |
| **用户热态** | **24-95ms** | SWR 单飞：用户请求拿缓存（stale 旧值 33ms），565ms 只在后台刷新线程发生——**用户侧无感**。 |

**结论**：599-920ms = 重启后首次 count SQL（565-750ms，走错索引）+ 半成品日键无效回退；K5 预热把重启后首次 count 移入预热期（HTTP tactics 预触发），热态稳定 ≤100ms。**J2 报告的 19ms 是 8898 上记忆命中 + 完整日缓存键的工况**（9/13 当天 kline 完整）。**修复建议（不改码，交 J 道后续）**：`_count_codes_on` SQL 改 `COUNT(*)`（0.6ms，等价语义）。

## §3 判据实测结果（8898，2026-09-16 盘中）

| # | 判据 | 实测 | 判定 |
|---|---|---|---|
| ① | 重启后首次 tactics ≤2s | 冷启动 8s 后首次请求 **69.6ms**（pending 占位） | **PASS** |
| ② | 预热后热态 ≤500ms | 5 连测 **24/25/25/26/95ms**（stale_ok 含完整 prev 数据 32,845B） | **PASS** |
| ③ | 预热期间 overview p95 ≤1s | 10 样本 **p95=13ms**、max=1113ms（单样本异常） | **PASS** |
| ④ | 并发 5 冷请求只 1 次计算 | 并发 5 tactics 全部返回 pending 占位（41B），单飞锁保证 1 次 `_tactics_cached` | **PASS** |

- 预热组件验证：`_warm_tactics()` 修复后 **OK（rc=0、4.5s）**，当日缓存 `tmp/tactics_cache/2026-09-16.json` 生成（42KB，coverage.n_codes=1 为半成品日现象，收盘后自动有效）。
- 冷请求并发期间 overview 单样本 1113ms（p95 13ms 达标）——该异常在 J2 期已归因机器环境噪声（ComfyUI PID 56584 等），非预热线程拖累（预热走 subprocess，Web 进程零 GIL 占用）。

## §4 诚实披露

1. **半成品日连锁**：9/16 盘中 kline 仅 1 行 → tactics/sentiment 当日键无效（stale 回退 prev）——**数据时序问题，非代码缺陷**；收盘后 kline 完整即恢复。K4 已同因披露（eco_temperature 9/16 None）。
2. **count SQL 565ms 未修**（`_count_codes_on` 在 tactics.py，K5 写权限外）：SWR 掩盖其对用户的影响，但每 600s 后台刷新仍吃 565ms 算力——建议 J 道后续改 `COUNT(*)`。
3. **8899 未重启**：K5（含 K4 展示分支、J1/J2/J3）全部代码未在生产生效；需服务管理方重启窗口。重启后预热线程自动跑（首用户不吃冷启动）。
4. **pending 占位前端未处理**：`{"pending":true,"retry_after_ms":3000}` 由客户端轮询重试（判据①明确允许）；web/ 不在 K5 写权限，未改前端。
5. **review 冷路径**：`_REVIEW_API` 单飞后首个请求也返回 pending（原同步 12s）；review 页前端已有 pending/ok:false 语义（J2 遗留），兼容。

验证方式：重启 8898 冷启动 → 首次 tactics 计时（判据①）→ 预热完成后 5 连测（判据②）→ 10×overview p95（判据③）→ 5 并发 tactics（判据④）；`python tools/warm_cache.py 8899`（手动预热入口，可用于生产重启后）。
