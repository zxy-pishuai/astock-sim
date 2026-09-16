# 系统缺陷与优化台账 — 执行登记（R2）

- 创建：2026-09-04 20:40
- 依据：验收方《系统缺陷与优化台账（2026-09-04，按优先级）》
- 执行：MainAgent 直办（用户"按照顺序依次执行"授权）
- 备份：`tmp/r2_backup/20260904_203848/`（9 文件，改前 SHA256 见 manifest.txt）
- 状态总览：P0 三项完成 ✓ / P1 四项完成 ✓ / P2 三项已登记建议（不实施）

---

## §0 判据与口径

- **P0.1 三选二**：选 A（后端吐旧缓存 + 后台重扫原子替换）＋ C（扫描成本减半=两卡向量化合并＋滑动窗口全向量化）。未选 B（前端 loading+60s 超时重试）——A 已消除浏览器挂断根因，B 属体验增强留待后续。
- 每项修复均：改前备份 → 改后 py_compile → 独立测试 → 与备份双哈希比对。
- 生效条件统一说明：**服务端 Python 改动在服务下次重启/重载后生效**（服务在跑 44104，不杀不重启）；前端 app.js 改动随页面刷新生效；watchdog 改动下次计划任务运行生效。

---

## §1 P0 可靠性

### P0.1 板块首刷超时（WinError 10053）✅

**根因**（profile 实测）：`/api/sectors` 实时链路上，`get_tactics_board` 冷缓存 miss 时**同步**执行 `scan_all` 全市场扫描：
- `_load_recent_klines` 冷缓存 SQLite 全表扫 kline 8.7M 行（date 无前缀索引）≈ **24s**
- 两卡 `_vectorize`＋`_vectorize_lianban` 各 96 万次切片 mean/max ≈ **13.7s**
- 合计 21-40s → 浏览器先挂断 → 前端渲染"不可用"。

**修复**（app/tactics.py）：
1. **A 吐旧缓存＋后台重扫**：`get_tactics_board` 缓存 miss 时先读上一交易日磁盘缓存（`_prev_trading_cache`，n≥1500 才认）返回（`stale_ok:true`），后台 `threading.Thread` 重扫当日并 `os.replace` 原子替换（`_RESCAN_LOCK` 防并发）。浏览器秒响应，扫描结果后台刷新。
2. **C 向量化减半**：
   - 新增 `_vectorize_both`：一次向量化同时产出首板＋连板两卡列（公共列只算一次）；
   - 滑动窗口全向量化：ma5/ma60/m3 用 cumsum、hh20 用 sliding_window_view、vbA/first7 向量化，替代逐 i 切片 mean/max。

**验证**：`tmp/r2_verify.py` 全宇宙 2132 票抽样断言 `_vectorize_both` vs 旧两函数**0 不一致**；两卡向量化耗时 **5.4s→0.93s（提速 82.9%）**；scan_all 全量段 13.7s→4.6s（提速 ~66%）。`_load_recent_klines` 24s 冷扫描由 A 的吐旧缓存兜底（用户不感知）。

**遗留建议**：~~`CREATE INDEX idx_kline_pd ON kline(period,date)`~~ **已批准并执行**（2026-09-04 21:0x）：索引创建耗时 31.3s，冷扫描实测 **23.9s→1.19s（提速 20x）**，`idx_kline_pd` 已入 sqlite_master。

### P0.2 日更完整性无告警 ✅

**根因**：`daily_coverage` 审计阈值 ratio<0.8 才 WARN；09-03 缺口 379/2354=0.839 ≥0.8 只记 INFO 静默。前端黄条仅 `complete=False`（n<1500）触发，0.84 时 n≈1975 仍 complete=True。

**修复**：
- `app/updater.py`：阈值分级——ratio<0.8 → **CRITICAL**，0.8≤ratio<0.9 → **WARN**，≥0.9 → INFO；`_daily_coverage` 补 `missing`/`missing_sample` 缺口明细随事件落 audit。
- `app/tactics.py`：scan_all coverage 补 `total`/`ratio` 口径。
- `web/js/app.js`：红条判定升级——`complete=false` **或** `ratio<0.9` 均显示"⚠数据不完整日"＋warn 样式。

**实测**：当前 9/4 数据 `daily_coverage = {fresh:783, total:2526, ratio:0.31}`——今晚收盘更新后首次 run_update 将记 **CRITICAL**（缺口 69%），前端板块显示红条。

### P0.3 watchdog 断更无告警 ✅

**根因**：L3 检查 audit.jsonl 尾部 t 字段，非交易时段放宽 7 天——01:20-09:15 断更 8h 属非交易时段不被捕获；无心跳文件可探测"计划任务本身是否在跑"。

**修复**（分工，遵守 watchdog 不写 audit 红线）：
- `tools/service_watchdog.py`：每次运行写心跳 `tmp/watchdog_heartbeat.ts`（`touch_heartbeat`）；自检距上次心跳 >10min → log"调度断更告警"。
- `app/trader.py`：`_monitor` 每 ~10min 低频检查心跳 mtime，超 10min → `audit.record(CRITICAL, watchdog_stale)`；心跳缺失同样 CRITICAL。覆盖"计划任务全挂、watchdog 永不运行"场景。

**验证**：心跳缺失/新鲜/超时三分支独立测试通过（monkeypatch audit 防污染真实链）；真实心跳已创建基线，避免 10min 后误报。

---

## §2 P1 数据与状态

### P1.4 limit_pool 脏数据 ✅（代码层）＋ 物理清理待批

**核查**（只读）：limit_pool 仅 `kind='zt'` 1400 行、无 `dt` 行；**实锤 4 组周末脏键**（20260822/23、20260829/30，周六日各 ~54-82 只，东财接口对周末 date 返回最近交易日快照落脏键）；zt 池 `days` 字段正常（1-5 板）。"跌停标 lbc=1"**未在库内复现**——推测指"跌停池从未拉取导致跌停票缺失"，作存疑项登记（见 §3 遗留）。

**修复**（app/limitup.py，零写库）：
- 写入侧 `_save`：周末日期不落库；
- 读侧 `_load`：历史周末脏键不读出。

**待批准**：~~`tmp/r2_clean_limitpool.ps1`（备份→删除 4 组周末脏行→复验，批准后执行）~~ **已批准并执行**（2026-09-04）：备份 272 条周末脏行 → `tmp/r2_limitpool_backup/weekend_rows.jsonl`（SHA256 `522c96610afaa9534770e53c89794aad177e9fc67019345d5fc12f9bb0dcd0e3`），删除 272 条，复验周末键残留 **0**，zt 总行 1400→1128。

### P1.5 main.py 清僵死后 app.lock 不重建 ✅

**实锤**：`_acquire_lock()` 僵死清理路径 `os.remove(_LOCK_FILE)` 后直接 return，不重写锁文件。

**修复**（main.py）：删锁后补 `open(_LOCK_FILE,"w").write(str(os.getpid()))`（两行，try/except 包裹）。

### P1.6 audit.jsonl 无轮转 ✅

**修复**（app/audit.py）：
- `_maybe_rotate()`：超 50MB 时持 `_cross_proc_lock` 内 `os.replace` 归档为 `audit_<ts>.jsonl`，轮转点为文件级链首；
- `record()` 写前检查大小触发轮转；
- `verify_chain()` 升级多文件：按名序遍历归档＋当前文件，文件切换重置链首（与轮转语义一致）。

**验证**：隔离测试（临时目录＋极小阈值）轮转触发、归档+当前 31 条链校验全绿（ok=True checked=31）。生产链断点 2026-08-16 19:07 为**历史断链**（tools/audit_chain_diag.py 基线已知），非本块引入（独立原逻辑复现一致）。

### P1.7 竞价快照 lazy ✅

**根因**：`auction_board` 09:25-09:40 首访才抓快照；09:40 后首访走降级且**降级结果写死当天快照**——用户上午不开板块永远拿降级评分。

**修复**（app/trader.py）：`_monitor` 交易日 09:26-09:39 主动调 `tactics.auction_board()`（`_auction_snap_ts` 每日去重；已有快照则零成本返回）。窗口内且无快照即抓取落盘，用户任意时间打开读到真实竞价快照。

---

## §3 P2 长期 — 登记建议（未实施）

| # | 项 | 建议 | 状态 |
|---|---|---|---|
| P2.1 | 快照/生产库 qfq 方言双源漂移 | 统一数据层或明确"历史用快照、当日用生产"；建议建 `qfq_dialect` 元数据表登记每票方言类型 | 待规划 |
| P2.2 | C1 遗留（cyq_rebuild 数字回写/东财 P1 对表 10 票/cyq_strata 通达信 5 票人工读数） | 待用户提供通达信读数后由对应块回填 | 待用户 |
| P2.3 | 竞价双战法（打板/弱转强）未回测 | 建议排 C 系回测卡，先于筹码峰；判据=竞价高开 gap 区间×晋级率 | 建议 |

## §4 遗留与待批准

1. ~~limit_pool 物理清理~~ **已执行**（见 §2 P1.4）。
2. ~~kline 加索引~~ **已执行**（见 §1 P0.1）。
3. **存疑项**："跌停标 lbc=1"未复现，需验收方补充现场（可能指跌停池缺失）。
4. **生效提醒**：服务下次重启后 P0/P1 代码改动全部生效；watchdog 下次计划任务运行写入首个真实心跳。

## §5 验证记录

- 全部改动文件 py_compile 通过；`app.*`/`main` 全量 import 冒烟通过。
- `tmp/r2_verify.py`：两卡合并逐位一致 0 不一致 + 提速 82.9%（P0.1C）。
- watchdog 心跳三分支独立测试通过（P0.3）。
- audit 轮转隔离测试通过（P1.6）。
- limitup 周末清洗门隔离测试通过（P1.4）。
- 改前（备份）vs 改后 SHA256 双对比：8 文件 CHANGED、server.py SAME（未动）。

---

## §6 风险登记：行情库零副本（单盘）

- **登记日期**：2026-09-05（K1b 块）
- **风险等级**：P1（高）—— 盘故障 = 行情数据全灭，重建成本以周计
- **影响面**：
  - `data/market.db` 1458.8MB（kline 8,848,946 行 + ml_pred 1,411,194 行 + 9 表）
  - `data/min5.db` 724.6MB（kline_min5 5,469,424 行）
  - `data/snapshots/` 9 份共 11.9GB（与生产库同盘，不是副本）
  - `data/cyq_cache` 142.3MB / 2113 票
  - 回测钉快照全部失效、ML 预测落库丢失、板块强度无历史
- **当前缓解**：
  - `data/keypack/` 极小关键包（每日 23:40 自动，保留 10 份）—— 只防**误删/写坏**，含 account.json / bt_*.json / experiments_index / docs/reports / audit.jsonl 等不可重建档（221 members / 1.94MB），**不含行情库 .db**
  - git nightly（23:50）—— 兜住代码 + JSON 状态（含 account.json），`.gitignore` 排除 market.db*/min5.db*/snapshots/
  - `tools/backup_data.py` 已存在（sqlite backup API）但无任务调用、无目标介质
- **未覆盖**：
  - **盘坏 / NVMe 掉电磨损 / 控制器故障** —— 同盘 keypack 与源同灭，无法恢复
  - **整盘失效** —— 当前无解，需外接介质（见 K1 §2 就绪命令）
  - **恢复演练** —— 未做过真实恢复演练（无副本可演练）
- **重建成本**（E1 实测吞吐外推）：
  - stale_first 当日覆盖：~300-858 票/480s 预算 → 回到可用水平约 3-5 个交易日
  - 全量历史回补（2019-至今 884 万行）：backfill_daily 实测 51min/6线程 → 全速约 1-2 小时 / 按每日预算约 1 周
  - min5.db 重建：未实测，估 2-4 小时
- **一句话结论**：**接一块外接 SSD 即可让 K1 的异盘副本 + 恢复演练一次做完**（首次备份约 30 分钟，恢复演练约 1 小时），是当前性价比最高的风险消除动作。
- **决策请求**：请人类决定 —— (a) 挂接外接盘/NAS 后继续 K1 异盘备份；(b) 接受单盘现状并依赖 keypack 防误删；(c) 其他方向。