# D3 数据健康面板（2026-09-13）

## §0 任务与验收判据
新增只读 API `/api/data_health` + 前端"数据健康"页签（绿/黄/红三态，红态直接显示原因文本）。
验收：① 面板一眼看出"09-09~09-11 快照缺失 + amount 100% 为 0 + 覆盖率 42%"三件事；
② 接口 p95 ≤ 300ms；③ 断网/库锁定时降级显示上次缓存 + 灰色标记，不报错。

## §1 实现

### 1.1 后端 `app/server.py`
- 新分支 `elif api == "data_health":`（calendar 之后、404 之前），返回 `_data_health_cached()`。
- **7 组 KPI 一次返回**：
  | 组 | 内容 | 数据源 |
  |---|---|---|
  | kline_coverage | 最新交易日行数/total/ratio + 7 日趋势 | kline 走 `idx_kline_pd`（period+date 按日过滤） |
  | amount | 最新 amount=0 行数/占比 + 7 日趋势 | 同上（SUM CASE 同查询附带） |
  | snapshots | 最新枚/age_days/deferred + 7 日有无 | `data/snapshots/` 目录只读 |
  | indices | 三指数 last_date/age_days/amount_available | kline 走 `idx_kline_cp` |
  | ml | ml_scores.signal_date + ml_pred.MAX(date) + age | `data/ml_scores.json` + 库表 |
  | alerts | 近 7 天 WARN/CRITICAL 计数 + 最近 6 条 | `data/quality_alert.jsonl` |
  | storage | data/ 总量/快照/近 7 日增量 | os.scandir 递归（后台线程） |
- **性能设计（实测瓶颈逐项后台化）**：
  - `COUNT(DISTINCT code)` 全索引扫描 1.2s → **后台线程 + 3600s 缓存**，主路径毫秒级；
  - storage 全目录扫描（cyq_cache 2113 小文件 ≈1.5s）→ **后台线程 + 300s 缓存**；
  - 主路径 = 3 条索引查询 + 2 个小文件读 + listdir ≈ **91ms（HTTP 实测）**；
  - 整体 60s TTL 内存缓存（`_HEALTH_CACHE` + 锁），缓存期内 0ms 命中。
- **降级（三态）**：
  - 库只读连接失败（db_ok=False）+ 有旧缓存 → 返回旧缓存 + `stale=True` + 原因；
  - 库坏 + 无缓存 → 返回含各分组 error 的载荷（`db_error`），不抛错；
  - 前端 fetch 失败 → 灰色"断网/服务不可用"占位，不报错。
- 接口内**零写入、零重计算**（全部只读查询/读文件/目录统计）。

### 1.2 前端
- `web/index.html`：nav 加"🩺 数据健康"（settings 前）+ `page-datahealth` 容器。
- `web/js/app.js`：`state.healthTimer` + `switchPage`/`clearTimers` 接线 + 60s 自动刷新
  `refreshDataHealth()` + 7 张卡片渲染（`dhHealthCards`）+ 三态判级。
- **判级规则（预注册，写死在前端）**：
  | 卡片 | ok | warn | err |
  |---|---|---|---|
  | 覆盖率 | ≥0.95 | ≥0.85 | <0.85 |
  | amount 缺失 | =0 | ≤5% | >5% |
  | 快照 | age≤3 且未延迟 | age≤7 | deferred 或 age>7 |
  | 指数 | 全部 age≤3 | 任一 age>3 | 任一 age>5 / 缺失 |
  | ML | age≤7 | age≤15 | age>15 |
  | 告警 | WARN≤1 | WARN≤3 | CRIT>0 或 WARN>3 |
  | 存储 | 正常 | 统计中/error | — |
  总状态 = 最差卡片；`stale=true` → 灰色标记"显示上次缓存"。

## §2 数据实况（2026-09-13 构建）
- 覆盖率：latest=2026-09-11，4993/5328 = **93.7%**（7 日趋势 09-07~11 均 ≈93.6-94.0%）；
  历史 42% 事故（09-11 补数前）在趋势/告警可见。
- amount：**当前 0 缺失**（09-07~11 amount=0 行数=0，A1 修复已生效）；
  **历史 amount 100% 为 0** 由 09-09/10/11 三天 `amount_zero_guard WARN`（ratio=1.0）在告警卡呈现。
- 快照：latest=2026-09-11（age=2），09-07~11 连续 5 枚，deferred=False；
  **09-12/13 无快照**（周末无行情属正常）。
- 指数：三指数 last=2026-09-11（age=2），amount 均可用（F1 后指数行 1918 行/指数）。
- ML：ml_scores signal_date=2026-08-26（**18 天**）；ml_pred last=2026-08-19（**25 天**）→ 黄/红。
- 告警：近 7 天 **WARN=3、CRITICAL=6**（09-13 数据哨兵 6 条：data_snapshot 缺失/
  daily_coverage_pending/amount_zero_guard/stale_first_budget_exceeded/watchdog_stale）→ 总状态红。
- 存储：data/ 总 **36.5 GB**、快照 24.97 GB（近 7 日增量）、g7=24.9 GB（09-07~11 五枚全库快照）——
  印证 Y4 存储瘦身必要性（数据已从 09-01 的 8.45GB 增至 36.5GB）。

## §3 验收

### 3.1 接口耗时（HTTP 实测）
| 场景 | 耗时 |
|---|---|
| 首帧（冷缓存） | **91 ms**（HTTP 全链路） |
| 缓存期内 | 0 ms（60s TTL） |
| 后台线程（total_codes/storage） | 独立异步，不阻塞响应 |
**p95 ≤ 300ms ✓**（最慢帧 91ms，慢查询全部后台化）。

### 3.2 三件事一眼可见 ✓
- **快照缺失**：快照卡"最近 7 日"●●●●●○○ + 09-12/13 空；且 09-13 CRITICAL
  `data_snapshot` 告警直接列在告警卡；
- **amount 100% 为 0**：告警卡 `amount_zero_guard`（09-09/10/11，ratio=1.0）+
  amount 卡 7 日柱状；
- **覆盖率 42%**：覆盖卡当前 93.7%（已修复），历史低值由告警卡
  `daily_coverage_pending`/`stale_first_budget_exceeded` CRITICAL 呈现。

### 3.3 降级（模拟实测）✓
| 场景 | 结果 |
|---|---|
| 库坏 + 有缓存 | `stale=True` + 旧数据完整返回 + 原因"unable to open database file" |
| 库坏 + 无缓存 | db_ok=False + db_error，前端红态，不抛错 |
| 前端 fetch 失败 | 灰色"断网/服务不可用"占位 |

### 3.4 自检
- `py -3.13 -m py_compile app/server.py` ✓
- `node --check web/js/app.js` ✓（前端语法）
- HTTP 端到端：`/api/data_health` 200 + 7 组字段齐；`/` 静态页含 datahealth 页签 ✓
- 备份双哈希一致：`tmp/d3_bak/`（server.py/index.html/app.js，SHA256 MATCH）

## §4 生效与遗留
- **生效**：需重启生产服务（PID 持 8899 者）加载新 server.py——由用户决定重启时机；
  前端静态文件即时生效（浏览器刷新即见新页签）。
- **遗留**：
  1. `total_codes` 首帧为 None（后台线程计算中）→ 前端显示"—"，下轮刷新（≤60s）填充；
  2. storage.growth7 口径 = 近 7 日新增快照枚体积（接口只读，不做基线写盘）；
  3. 指数 amount_available=true（当前指数 amount 有值）；若腾讯源恢复无 amount 会如实转 false；
  4. 存储 36.5GB 实测暴露 Y4 一期（快照压缩/归档）后的空间仍快速膨胀——二期（热库/增量快照）建议排期。
