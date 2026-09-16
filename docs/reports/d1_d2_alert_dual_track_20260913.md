# D1/D2 告警双轨打通 + 静默失败防护 报告

- 日期：2026-09-13（周日）
- 任务：D1 告警双轨打通（quality_alert → audit → 前端红条 → 自动升级）+
  D2 静默失败防护（INFO 级"延后/跳过"事件自动升级 + 每日巡检）
- 状态：✅ 完成（含计划任务 TianjiAuditWatch 注册）
- ⚠ 编号说明：本批 D1/D2 与 8/27 历史 D1/D2（全球映射复验 / PIT 池补建）**同名**，
  以日期后缀区分：本报告 = D1-告警双轨(09-13) / D2-审计巡检(09-13)。

## §0 背景（痛点即任务书）

- `_amount_zero_guard` 连报三天 WARN（`ratio=1.0`，09-09/10/11）到
  `data/quality_alert.jsonl`，**三天无人知晓**——该文件不进 audit、不上前端、
  无任何自动动作；哨兵其他结果（high_severity 329、ml_pred_last 停 08-19）同。
- 快照三天没生成全靠三条 **INFO** 级 `data_snapshot_deferred`，无升级机制。
- 本轮修复把"只落文件"改为"落 audit + 前端可见 + 连续 N 天自动升级 + 有界轮转"，
  并把"必须每日出现/不应连续出现"做成每日巡检（21:30 计划任务）。

## §1 D1 改动 diff 摘要（5 文件 + web 2 处）

### app/audit.py（333→534 行，+201；py_compile rc=0，LF）
新增 4 函数 + 2 常量（复用 `audit.record()`，哈希链保持完整，**未改链机制**）：
- `_qa_read_all()`：读 quality_alert.jsonl（含归档，当前文件路径以 `_QA_FILE` 为准
  支持验收注入）
- `quality_alert_streak(event, until_date)`：同 event 在【交易日序列】上连续出现天数
  （从 until_date 往回先找最近有事件的交易日，再逐交易日数）
- `quality_alert_forward(entry, source)`：转发一条 QA 条目为 audit 事件
  （`event="quality_alert"`，带 orig_event/event_date/source/streak_days 全字段）；
  同 event 连续 ≥2 交易日且 level=WARN → 升 CRITICAL + 另记
  `quality_alert_escalated`（带连续天数）+ `alert.notify` 推送（延迟 import 防循环）；
  entry 内 event/level 键过滤改名防覆盖 record() 命名参数
- `quality_alert_rotate()`：quality_alert.jsonl 超 512KB → `os.replace` 归档
  `quality_alert_<ts>.jsonl`；清理 90 天前归档（与 audit 保留期一致）
- `quality_alert_summary(limit)`：**前端红条数据源**——聚合 5 类关键项：
  amount 缺失（含连续天数，≥2 显示 🔴）/ 复权跳变 high_severity / ML 预测陈旧
  （>25 天）/ **快照缺失（排除 B1 pit_lost 占位：manifest.pit_lost=true 不算）** /
  最近升级记录（audit quality_alert_escalated 尾部）→ `{banner_text, items, escalated}`

### app/updater.py（L388 `_amount_zero_guard`）
- 参数 `db_file/qa_file` 注入（验收走临时副本库/临时告警文件，**生产零触碰**）
- 写 quality_alert.jsonl 后调用 `audit.quality_alert_forward(alert,
  source="updater._amount_zero_guard")` + `quality_alert_rotate()`

### tools/data_sentinel.py（+42 行）
- 新 `_emit_quality_alerts(report)`：哨兵跑完判定 3 项关键事件
  （`sentinel_qfq_high_severity` / `sentinel_ml_pred_stale` / `sentinel_min5_stale`，
  均 WARN）→ 写 quality_alert.jsonl + audit 转发（独立跑也双轨）；
  main() 写 report 后调用

### app/server.py（overview 路由，L403-423）
- overview 返回加 `quality` 字段（`audit.quality_alert_summary()`，延迟 import）——
  **前端红条数据源**；异常时给空结构不破坏 overview

### web/index.html + web/js/app.js（前端红条）
- index.html：market 页顶部加 `<div id="qa-notice" class="tct-honest-note"
  style="display:none">`（复用既有样式类，未改既有规则）
- app.js：`refreshOverview()` 内 6 行——`o.quality.banner_text` 非空则显示红条
  （`textContent` 防 XSS），空则隐藏

### app/alert.py
- 未改文件本体；`quality_alert_forward` 升级分支延迟 import 调
  `alert.notify("WARN", "qa-escalated-<event>", ...)`（cooldown=1800，配置未开则静默）

## §2 D2 改动 diff 摘要（新工具 + 计划任务）

### tools/audit_watch.py（新，244 行；py_compile rc=0，LF）
- **白名单**（交易日必须出现，按"从窗口尾部往前连续缺失交易日数"报）：
  `data_update` / `daily_coverage`|`daily_coverage_pending`（任一）/
  `data_snapshot*`（前缀）/ `heartbeat`
- **黑名单**（不应连续出现，窗口内最长连续段 ≥2 交易日 → 升级）：
  `data_snapshot_deferred` / `stale_first_budget_exceeded` /
  `daily_coverage_pending` / `amount_zero_guard` / `watchdog_stale`
- 数据源聚合：主 audit.jsonl + `audit_*.jsonl` 归档 + quality_alert.jsonl（含归档）
  ——amount_zero_guard 历史只落在 QA 文件，两边都要读
- 结果：`audit.record("alert","audit_watch", findings=[...])` +
  每条 finding 写 quality_alert.jsonl（`audit_watch_<key>`）再走 forward 转发
- 模式：默认检查最近 7 个交易日窗口；`--from/--to` 回放；`--dry-run` 只输出不写

### tools/audit_watch.cmd（新，纯 ASCII，照抄 ml_scores_ledger_daily.cmd 模式）
```bat
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
"C:\...\Python313\python.exe" tools\audit_watch.py >>data\audit_watch_cron.log 2>&1
```

### 计划任务 TianjiAuditWatch（已注册）
- `schtasks /create /tn TianjiAuditWatch /tr "C:\Users\26838\A股模拟盘\tools\audit_watch.cmd"
  /sc daily /st 21:30 /f`（注册前查重：不存在；注册 rc=0，查询确认 Daily/21:30/路径正确）
- **回滚命令**：`schtasks /delete /tn TianjiAuditWatch /f`

## §3 验收证据

### D1 场景 A（guard 注入，tmp/d1/accept_d1.py）
- 迷你副本库（2 行 amount=0 volume>0 + 1 行正常）→
  `updater._amount_zero_guard(day='2026-09-12', db_file=mini, qa_file=tmp)`
- 结果：guard 触发（rows=2 ratio=0.6667）；临时 QA 文件 1 条；
  **audit `quality_alert` level=CRITICAL streak=3**（生产 QA 已有 09-09/10/11 三天
  真实连续 → 第 4 天触发即升级）+ **`quality_alert_escalated` streak=3** ✓
- **生产 quality_alert.jsonl 行数不变（22）**、market.db 未触碰 ✓

### D1 场景 B（纯两天正向，monkeypatch `_QA_FILE`）
- 临时 QA 文件含 test_evt 连续两天 → forward → **level=CRITICAL streak=2** +
  audit `quality_alert_escalated` streak=2 ✓（任务书"连续造两天"干净验证）

### D1 补充（tmp/d1/accept_d1b.py）
- 哨兵 `_emit_quality_alerts`：构造 report（high_severity=5/ml 陈旧/min5 陈旧）
  → QA 新增 3 条 sentinel_* 事件 + audit 转发（source=tools/data_sentinel.py）✓
- 快照缺失判定：排除 B1 pit_lost 占位后报
  "最新快照 2026-09-08（应到 2026-09-11）" ✓（修前被 09-11 占位掩盖）
- 审计链：`verify_chain` broken_at=2026-08-16（**已知历史基线**，早于本轮写入）
  → 本轮段链完整 ✓

### D2 回放验收（--from 2026-09-09 --to 2026-09-11 --dry-run）
```
[CRITICAL] data_snapshot（快照冻结） 连续 3 个交易日缺失（CRITICAL）          ← 任务书判据①
[CRITICAL] 快照延后(data_snapshot_deferred) 连续 3 个交易日（升级）           ← ②
[CRITICAL] 首批预算超时(stale_first_budget_exceeded) 连续 3 个交易日（升级）  ← ②
[CRITICAL] 覆盖未完成(daily_coverage_pending) 连续 3 个交易日（升级）
[CRITICAL] 成交额缺失(amount_zero_guard) 连续 3 个交易日（升级）              ← ③
[CRITICAL] 看门狗陈旧(watchdog_stale) 连续 2 个交易日（升级）
```
**三条必报全部命中**（另多报 daily_coverage_pending/watchdog_stale 属黑名单定义内）。

### D2 真跑（schtasks /run 手动触发一次）
- audit 尾部出现 `quality_alert` CRITICAL（orig_event=audit_watch_*，source=
  tools/audit_watch.py）✓
- quality_alert.jsonl 追加 6 条 `audit_watch_*` CRITICAL（快照缺失 3 天 +
  5 个黑名单升级）✓
- `data/audit_watch_cron.log` 输出（UTF-8 合法，终端显示乱码为 PS 编码显示问题，
  与既有五份 cron.log 一致）✓

## §4 轮转与升级语义

- 升级：同 event 连续 ≥2 个**交易日**（用 trading_calendar 序列，跨周末自动跳过
  非交易日）→ WARN→CRITICAL + audit escalated + 推送（配置了通知渠道时）。
- 轮转：quality_alert.jsonl >512KB 归档（audit 为 50MB，按 1% 量级配比），
  归档保留 90 天（与 audit.rotate_daily keep_days=90 一致）。

## §5 生效条件与遗留

- **生效**：updater/audit/server 改动**下次服务重启生效**（运行中 8899 内存为旧版）；
  前端红条需重启后可见。`tools/audit_watch.py` 与计划任务**即时生效**（已真跑验证）。
- **前端红条手测清单**（无头环境无法肉眼验证）：重启 8899 → 打开行情中心 →
  顶部应见红条"🔴 成交额缺失连续 3 天（amount=0 占比 100%）| ⚠ 复权跳变高风险…
  | ⚠ 快照缺失（最新 2026-09-08…）"；quality_alert.jsonl 清空/无关键项时红条隐藏。
- **遗留**：① 哨兵摘要条目（high_severity 等 raw 值）仍由 updater 摘要段直接写
  quality_alert.jsonl（无 level，不转发 audit）——与哨兵 emit 的事件条目并存，
  属设计（快照摘要 + 判定事件）；② audit_watch 每日 21:30 跑，若 21:30 时服务
  正在跑收盘更新（15:10 触发，通常 ≤2h 完成，最迟 ~17:30）不冲突；
  ③ 历史 amount_zero_guard 三天（09-09/10/11）在 D1 上线后**下次触发即升级**
  （streak 含历史），前端红条当前已显示 🔴。
- **验收痕迹披露**：① 哨兵函数级测试（构造 report：high_severity=5 假值）曾向
  生产 quality_alert.jsonl 写入 6 条 sentinel_* 测试条目——**已清理**
  （文件恢复 22 行，含 D2 真跑 6 条 audit_watch_* 真实发现与全部历史条目）；
  audit 侧对应 6 条 `quality_alert` 转发（16:06:28/16:07:17，level=WARN）为
  append-only 哈希链**不可删除**，留档（不影响前端红条——summary 的 escalated
  只查升级事件，sentinel 转发未升级）。② **market.db mtime 2026-09-13 16:07:32
  有单次变化**（+4096 字节，约 1 页）：本块全部 SQLite 连接为 mode=ro 或
  tmp/d1 迷你临时库，**未写生产库**；该次写入无对应 audit 事件（非 updater 路径），
  疑似并发块或运行中服务行为，已静止（复测 mtime 未再变化）——交验收方核查。

## §6 文件清单（改动前后行数与 mtime）

| 文件 | 改动 | 行数 | mtime |
|---|---|---|---|
| app/audit.py | +4 函数+2 常量 | 333→534 | 09-13 16:07:02 |
| app/updater.py | _amount_zero_guard 参数+转发+轮转 | 1326 | 09-13 15:59:13 |
| tools/data_sentinel.py | +_emit_quality_alerts | 660→678 | 09-13 16:00:03 |
| app/server.py | overview +quality 字段 | 1225→1491（含 W5 并发累计） | 09-13 16:00:12 |
| tools/audit_watch.py | 新 | 244 | 09-13 16:01:44 |
| tools/audit_watch.cmd | 新 | 7 | 09-13 16:02:14 |
| web/index.html | +qa-notice div | 881→889 | 09-13 16:01:13 |
| web/js/app.js | refreshOverview +6 行 | 2617 | 09-13 16:02:26 |
| docs/backlog.md | +2 行登记 | 97→99 | 09-13 16:08:07 |

全部 py_compile rc=0、LF（CRLF=0）。backlog 登记为
"D1-告警双轨(09-13)/D2-审计巡检(09-13)"（日期后缀区分历史同名编号）。
