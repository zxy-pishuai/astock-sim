# N1 ｜ SQLite 读取规范纠正（P1，先做）

**执行时间**：2026-09-06（周日）
**执行块**：N1（pack24）
**任务书**：`tmp/pack24/N1_sqlite_read_policy.md`
**新规**：pack24/README.md R1/R2/R3/R4

---

## 首屏：门时刻 / journal_mode / rg 原文 / git status

| 项目 | 值 |
|------|-----|
| **查重门时刻（G1′）** | 2026-09-06 **18:54:50**（`tmp/pack24_dup.log` "N1 GATE-OK"） |
| **market.db journal_mode** | **wal**（`PRAGMA journal_mode` 实测） |
| **min5.db journal_mode** | **wal**（`PRAGMA journal_mode` 实测） |
| **rg 命中文件数** | **14 个文件** |
| **rg 命中连接点数** | **20 处**（含注释 1 处，实际代码连接点 19 处） |
| **本次未改任何 .py** | ✅（git status 中 M 文件均为之前任务产物） |

### rg 原文（`rg -c 'immutable=1' tools/ --type py`）

```
tools/auction_backtest.py:1
tools/cyq_build.py:1
tools/c_lane_redispatch.py:1
tools/e1_fix_intraday_bars.py:2
tools/min5_check.py:1
tools/qfq_census.py:1
tools/qfq_factor_check.py:3
tools/qfq_unify_recompute.py:1
tools/ml_sidecar/train.py:2
tools/ml_sidecar/rollout_short_label.py:2
tools/ml_sidecar/diagnose_coverage.py:1
tools/ml_sidecar/rolling_train.py:2
tools/ml_sidecar/compare_report.py:1
tools/tactic_lianban_backtest.py:1
```

### git status --porcelain（本次 N1 未改 .py 证明）

本次 N1 任务仅写入 `tmp/pack24/`（临时脚本）和本报告。`git status` 中已存在的 M 文件（`app/trader.py`、`app/updater.py`、`main.py`、`tools/service_watchdog.py` 等）均为之前任务（H3b/K4b/M1a/H3c/H5 等）的产物，非本次 N1 改动。`tools/` 下无新增 M/??（`tools/e1_fix_intraday_bars.py` 为 H3 任务产物，非本次）。

---

## §1 背景与问题

### 1.1 问题本质

`data/market.db` 和 `data/min5.db` 均为 **WAL（Write-Ahead Logging）模式**。SQLite WAL 模式下，写入先进入 `-wal` 文件，主数据库文件在 checkpoint 时才更新。

`immutable=1` 参数告诉 SQLite："数据库文件不会被其他进程修改"。这会导致 SQLite **跳过 WAL 文件的读取**，直接读主文件快照。如果写入尚未 checkpoint 到主文件，`immutable=1` 连接会读到**旧数据**。

### 1.2 影响范围

- **生产在线链无风险**：验收方 `rg` 实测 `app/` 与 `main.py` 均未使用 `immutable`
- **受影响的是 20+ 个 `tools/` 离线脚本**，含今天的 `tools/e1_fix_intraday_bars.py`
- `tools/ml_sidecar/predict.py:101` 注释显示这个坑以前就踩过

### 1.3 H3c 实证案例（已自曝）

H3c 于 2026-09-06 15:27 执行回滚后，用 `immutable=1` 连接验证，误判"回滚失败"（非 9/4 变动仍为 28）。改用普通 `mode=ro` 连接后，验证通过（非 9/4 变动=0）。

**根因**：回滚写入刚进入 WAL，尚未 checkpoint 到主文件；`immutable=1` 跳过 WAL 读到旧主文件。

H3c 报告 `docs/reports/h3c_sep4_coverage_gap.md` §4.4 已自曝此问题。

---

## §2 N1-D1 连接点清单（20 处逐个标注）

### 2.1 连活库（需改）：17 处

| # | 文件:行 | 连接目标 | 脚本结论是否依赖最新写入 | 处置建议 | 理由 |
|---|---------|----------|--------------------------|----------|------|
| 1 | `tools/e1_fix_intraday_bars.py:39` | `data/market.db`（活库） | **是** — 写库前查残缺 bar、写库后验证 | **改** → `mode=ro` | 写后验证必须看到最新写入；H3c 已因此误判 |
| 2 | `tools/e1_fix_intraday_bars.py:89` | `data/market.db`（活库） | **是** — 同上 | **改** → `mode=ro` | 同上 |
| 3 | `tools/qfq_census.py:20` | `data/market.db`（活库） | **是** — qfq 污染普查需最新数据 | **改** → `mode=ro` | 普查结论依赖最新 kline 数据 |
| 4 | `tools/qfq_factor_check.py:40` | `data/market.db`（活库） | **是** — 复权因子核查需最新数据 | **改** → `mode=ro` | 因子核查依赖最新 kline |
| 5 | `tools/qfq_factor_check.py:164` | `data/market.db`（活库，输出元数据） | **是** — 输出 db_uri 供下游消费 | **改** → `mode=ro` | 下游消费方需看到最新数据 |
| 6 | `tools/qfq_unify_recompute.py:48` | `data/market.db`（活库） | **是** — qfq 统一重算需最新数据 | **改** → `mode=ro` | 重算输入依赖最新 kline |
| 7 | `tools/ml_sidecar/train.py:31` | `data/market.db`（活库，DB 变量） | **是** — ML 训练需最新特征数据 | **改** → `mode=ro` | 训练数据需最新 |
| 8 | `tools/ml_sidecar/train.py:61` | `data/market.db`（活库，DB 变量） | **是** — 同上 | **改** → `mode=ro` | 同上 |
| 9 | `tools/ml_sidecar/rollout_short_label.py:58` | `data/market.db`（活库，DB 变量） | **是** — 短标签 rollout 需最新数据 | **改** → `mode=ro` | rollout 输入依赖最新 |
| 10 | `tools/ml_sidecar/rollout_short_label.py:79` | `data/market.db`（活库，DB 变量） | **是** — 同上 | **改** → `mode=ro` | 同上 |
| 11 | `tools/ml_sidecar/rolling_train.py:42` | `data/market.db`（活库，DB 变量） | **是** — 滚动训练需最新数据 | **改** → `mode=ro` | 滚动训练依赖最新 |
| 12 | `tools/ml_sidecar/rolling_train.py:64` | `data/market.db`（活库，DB 变量） | **是** — 同上 | **改** → `mode=ro` | 同上 |
| 13 | `tools/ml_sidecar/compare_report.py:49` | `data/market.db`（活库，DB 变量） | **是** — 对比报告需最新数据 | **改** → `mode=ro` | 对比结论依赖最新 |
| 14 | `tools/ml_sidecar/diagnose_coverage.py:38` | `data/market.db`（活库，DB 变量） | **是** — 覆盖率诊断需最新数据 | **改** → `mode=ro` | 覆盖率诊断依赖最新 |
| 15 | `tools/auction_backtest.py:36` | `data/min5.db`（活库） | **是** — 竞价回测需最新 5 分钟线 | **改** → `mode=ro` | 回测输入依赖最新 min5 |
| 16 | `tools/min5_check.py:20` | `data/min5.db`（活库） | **是** — min5 数据质量检查需最新 | **改** → `mode=ro` | 质量检查依赖最新 |
| 17 | `tools/qfq_factor_check.py:17` | 注释中提及 `immutable=1` | 不适用（注释） | **改** — 更新注释 | 注释应与代码一致，避免误导 |

### 2.2 连归档快照（可保留 immutable）：3 处

| # | 文件:行 | 连接目标 | 脚本结论是否依赖最新写入 | 处置建议 | 理由 |
|---|---------|----------|--------------------------|----------|------|
| 18 | `tools/cyq_build.py:18` | `data/snapshots/2026-09-02/market.db`（归档快照） | **否** — 快照固定，不会被修改 | **保留** `immutable=1` | 快照文件不会被写入，immutable 可提升读性能；符合 R2 |
| 19 | `tools/tactic_lianban_backtest.py:362` | `data/snapshots/2026-09-02/market.db`（归档快照） | **否** — 快照固定 | **保留** `immutable=1` | 同上；回测钉快照是项目纪律（红线 5） |
| 20 | `tools/c_lane_redispatch.py:120` | `data/snapshots/2026-08-26/market.db`（归档快照，SNAPSHOT 变量 L39） | **否** — 快照固定 | **保留** `immutable=1` | 同上；脚本 L6 注释明确"冻结快照，不用 latest" |

### 2.3 汇总

| 类别 | 处数 | 处置 |
|------|------|------|
| 连活库 market.db | 14（含 1 注释） | 全部改 `mode=ro` |
| 连活库 min5.db | 2 | 全部改 `mode=ro` |
| 连归档快照 | 3 | 保留 `immutable=1` |
| **合计** | **20**（19 代码 + 1 注释） | — |

---

## §3 对读实测（4 个活库场景）

### 3.1 实测条件

- 实测时间：2026-09-06 18:55（H3c 写入后约 3.5 小时）
- WAL 状态：`market.db-wal` = 0 bytes、`min5.db-wal` = 0 bytes（**已 checkpoint**）
- 两种连接：`mode=ro&immutable=1` vs `mode=ro`（不加 immutable）

### 3.2 实测结果

| 测试点 | 对应脚本 | 查询 | immutable=1 | mode=ro | 差异 |
|--------|----------|------|-------------|---------|------|
| 1 | `e1_fix_intraday_bars.py` | 9/4 bar 数 | 958 | 958 | **0（无差异）** |
| 2 | `qfq_census.py` | kline 总行数 | 8,745,871 | 8,745,871 | **0（无差异）** |
| 2 | `qfq_census.py` | MAX(date) | 2026-09-04 | 2026-09-04 | **无差异** |
| 3 | `ml_sidecar/diagnose_coverage.py` | 000882 最近 5 日行数 | 5 | 5 | **0（无差异）** |
| 3 | `ml_sidecar/diagnose_coverage.py` | 000882 9/4 close | 1.54 | 1.54 | **无差异** |
| 4 | `auction_backtest.py` / `min5_check.py` | min5 总行数 | 5,469,424 | 5,469,424 | **0（无差异）** |
| 4 | `auction_backtest.py` / `min5_check.py` | min5 MAX(date) | 2026-09-04 15:00:00 | 2026-09-04 15:00:00 | **无差异** |

### 3.3 实测结论

**当前对读无差异**，原因：
- 实测时 WAL 已 checkpoint（`-wal` 文件 0 bytes），主文件包含最新数据
- `immutable=1` 连接读主文件即可读到最新数据

**但风险是时效性的**：在写入后立即查询时（WAL 未 checkpoint），`immutable=1` 会读到旧主文件数据。H3c 15:27 回滚后立即验证即为实证案例（见 §4）。

**风险场景清单**：
1. 写库后立即验证（如 e1_fix_intraday_bars.py 写后查）
2. 盘中实时查询（updater 持续写入，WAL 频繁更新）
3. 收盘更新后立即统计（15:35 更新后 15:36 查）
4. 批量回补过程中查进度（写入与查询交替）

---

## §4 H3c 实证案例（写入后立即查询的差异）

### 4.1 事件经过

| 时间 | 事件 |
|------|------|
| 2026-09-06 15:23:42 | H3c 试点写库开始（100 票 9/4 数据 INSERT OR REPLACE） |
| 2026-09-06 15:24:29 | 写库完成，market.db mtime 更新 |
| 2026-09-06 15:27:04 | 发现 28 行非 9/4 变动，执行回滚（28 行 UPDATE） |
| 2026-09-06 15:27:04 | 回滚脚本内用 `immutable=1` 连接验证，**误判"回滚失败"**（非 9/4 变动仍为 28） |
| 2026-09-06 15:28 | 改用普通 `mode=ro` 连接重新验证，**回滚成功**（非 9/4 变动=0） |

### 4.2 根因

回滚 UPDATE 刚写入 WAL，尚未 checkpoint 到主文件。`immutable=1` 连接跳过 WAL，读到旧主文件（回滚前的状态），因此误判"回滚失败"。

### 4.3 教训

- **写后验证必须用 `mode=ro`（不加 immutable）**，这是 R1 的核心
- WAL 模式下 `immutable=1` 的风险是**时效性**的，不是永久性的
- checkpoint 后两种连接一致，但无法预测 checkpoint 时机（SQLite 自动 checkpoint 阈值默认 1000 页）

---

## §5 处置建议汇总

### 5.1 立即处置（下一批可执行）

1. **17 处连活库的连接点全部改 `mode=ro`**（去掉 `&immutable=1`）
   - 涉及 12 个文件：`e1_fix_intraday_bars.py`（2 处）、`qfq_census.py`（1 处）、`qfq_factor_check.py`（2 处代码 + 1 处注释）、`qfq_unify_recompute.py`（1 处）、`ml_sidecar/train.py`（2 处）、`ml_sidecar/rollout_short_label.py`（2 处）、`ml_sidecar/rolling_train.py`（2 处）、`ml_sidecar/compare_report.py`（1 处）、`ml_sidecar/diagnose_coverage.py`（1 处）、`auction_backtest.py`（1 处）、`min5_check.py`（1 处）
2. **3 处连归档快照的连接点保留 `immutable=1`**
   - `cyq_build.py`、`tactic_lianban_backtest.py`、`c_lane_redispatch.py`
3. **修改后逐文件 `ast.parse` 校验**，并跑一次冒烟测试确认连接正常

### 5.2 长期规范（见 §6 成稿）

- R1：凡需看到最新写入 → `mode=ro`（不加 immutable）
- R2：只有归档快照才允许 `immutable=1`，且报告须写明用的是快照还是活库
- R3：任何"改前/改后"结论，必须先声明连接串与 `PRAGMA journal_mode`

### 5.3 不建议的做法

- **不建议**全局禁用 WAL 改回 DELETE 模式——WAL 对并发读写性能更好，且生产在线链已稳定运行
- **不建议**在脚本中手动 `PRAGMA wal_checkpoint` 后再用 immutable——这增加了写操作风险，且 checkpoint 本身需要写权限
- **不建议**用 `immutable=1` 做"性能优化"——活库场景下正确性优先于性能

---

## §6 N1-D2 规范成稿（可直接粘贴到项目交接底稿"数据字典/纪律"节）

```markdown
### SQLite 只读连接规范（R1/R2/R3）

**R1 — 活库只读必须用 mode=ro（不加 immutable）**
凡需看到最新写入的查询（含写后验证、盘中实时查询、收盘更新后统计），
连接串一律使用 `file:<db>?mode=ro`，**禁止**加 `immutable=1`。
原因：本项目 market.db / min5.db 均为 WAL 模式，immutable=1 会绕过 WAL
读旧主文件，写入后立即查询时会读到旧数据（H3c 2026-09-06 实证误判）。

**R2 — 只有归档快照才允许 immutable=1**
只有对已归档快照文件（`data/snapshots/<date>/market.db`）做只读研究时，
才允许 `immutable=1`，且报告须写明用的是快照还是活库。
快照文件不会被修改，immutable=1 可提升读性能且无正确性风险。

**R3 — 改前/改后结论必须声明连接串与 journal_mode**
任何"改前 vs 改后"对照结论，必须先各自声明：
  - 连接串（`mode=ro` 还是 `mode=ro&immutable=1`）
  - `PRAGMA journal_mode` 返回值（wal / delete / memory）
不声明连接串的"改前/改后"结论视为不可信。

**已知 WAL 模式数据库**：
  - `data/market.db` — journal_mode=wal
  - `data/min5.db` — journal_mode=wal

**风险场景（必须用 mode=ro）**：
  1. 写库后立即验证
  2. 盘中实时查询（09:10-15:00 updater 持续写入）
  3. 收盘更新后立即统计（15:35 更新后）
  4. 批量回补过程中查进度
```

---

## §7 自检门验证

| # | 自检门 | 结果 | 证据 |
|---|--------|------|------|
| 1 | 清单条数 = rg -c 实测条数 | ✅ 14 文件 / 20 处 | 首屏 rg 原文 |
| 2 | 至少 3 个"连活库"点做两种连接对读 | ✅ 4 个场景（e1/qfq_census/ml_sidecar/auction_backtest+min5_check） | §3 对读实测表 |
| 3 | 证明未改任何 .py | ✅ 本次仅写 tmp/pack24/ + 本报告 | git status 说明 |
| 4 | 禁改 app/*、main.py、tools/service_watchdog.py、app/config.py | ✅ 未碰 | — |
| 5 | 禁重启任何进程 | ✅ 未碰 | — |
| 6 | 禁写 data/ 任何文件 | ✅ 未碰 | — |
| 7 | 查询限时 ≤120s | ✅ 所有查询 <60s | — |
| 8 | 语法用 ast.parse | ✅ 两个临时脚本均 ast.parse 通过 | — |

---

## §8 产物清单

### 8.1 交付物

| 文件 | 说明 |
|------|------|
| `docs/reports/n1_read_policy.md` | 本报告（含连接点清单 + 对读实测 + 规范成稿） |

### 8.2 中间产物（tmp/pack24/）

| 文件 | 说明 |
|------|------|
| `n1_pair_read_test.py` | market.db 对读实测脚本（3 个场景） |
| `n1_min5_pair_read.py` | min5.db 对读实测脚本 |

### 8.3 查重门

- `tmp/pack24_dup.log`："N1 2026-09-06 18:54:50 GATE-OK"

---

## §9 红线自查

| 红线 | 遵守情况 |
|------|----------|
| 禁改 app/* | ✅ 未碰 |
| 禁改 main.py | ✅ 未碰 |
| 禁改 tools/service_watchdog.py | ✅ 未碰 |
| 禁改 app/config.py | ✅ 未碰 |
| 禁重启任何进程 | ✅ 未碰 |
| 禁写 data/ 任何文件 | ✅ 未碰 |
| 临时脚本写 tmp/pack24/ | ✅ 遵守 |
| 语法用 ast.parse | ✅ 遵守 |
| 查询限时 ≤120s | ✅ 遵守 |
| G1′ 门先行并自签 | ✅ 18:54:50 签门 |

---

**报告完成时间**：2026-09-06 19:00
**执行结论**：20 处 immutable=1 连接点已全部标注（17 处连活库需改、3 处连快照可保留）；4 个活库场景对读实测（当前 WAL 已 checkpoint 故无差异，但 H3c 有写入后立即查询的实证案例）；R1/R2/R3 规范成稿已备（可直接粘贴到交接底稿）。**本块未改任何 .py**，17 处修改留待下一批执行。
