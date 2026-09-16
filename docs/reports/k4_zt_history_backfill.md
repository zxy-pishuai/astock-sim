# K4 情绪周期历史表补数（2026-09-16）

> 执行：K 道（P1-④）。范围：`tools/backfill_zt_history.py`（新）、`app/zt_ecosystem.py`（只读辅助）、`app/server.py`（只读展示分支 `/api/sentiment/eco`）。
> 数据：`data/market.db` 只读基线；写入仅 qg_zt_full / qg_sentiment_history 两表（已备份）。
> 8899 生产未重启（展示分支未生效，需服务管理方重启窗口）。

## §0 判据预注册（先落盘后跑数，§3 逐项对照）

1. `qg_sentiment_history` 行数 ≥ 有效窗口要求的 80%——按 zt_ecosystem.py:22 `WARM_MIN_RATIO=0.8`（120 交易日窗 × 0.8 = **96 行**）；另按任务书 250 交易日 × 0.8 = 200 行双口径验收。
2. 情绪温度（`zt_ecosystem.eco_temperature`）对最近 20 个**完整交易日**返回非 None。
3. 回填结果与"用当日实时数据现算"在最近 5 日逐位一致（自校验）。
4. 严格 PIT——回填不得使用晚于该日的信息（构造保证：每行只用当日及以前 kline close 反推）。

---

## §1 根因（写入路径从未被调用）

- `qg_zt_full` / `qg_sentiment_history` 的唯一写入者是 **`tools/build_sentiment_history.py`（手动工具）**（grep `qg_zt_full|qg_sentiment_history` 全项目仅 sentiment_gate/server/backtest_zt_eco 读取、build_sentiment_history 写入）。
- 该工具**2026-08-21 之后从未被调用**（未接入 updater 日更链路、无计划任务）→ 两表停更于 8/21（qg_zt_full 1852 行；qg_sentiment_history 单行快照 `2019_2026-08-21`）。
- **取数源链路正常**：limit_pool 由 limitup.py 持续更新至 **20260915**（1602 行）——排除"数据源被封"，定性为**写入路径从未被调用**。

## §2 正确性门与数据代差

### 2.1 limit_pool 口径实证（对表设计的关键发现）
- limit_pool(kind='zt') 表列 date 是**池快照日**而非涨停当日：实测 **8/15(六)/8/16(日)/8/17(一) 三天是同一份池**（63∩63∩63 码），8/17 盘后追加 91 码；payload 无涨停当日字段（键集合实证：c/m/n/p/zdp/amount/ltsz/tshare/hs/lbc/fbt/lbt/fund/zbc/hybk/zttj）。
- 样例 000936 华西股份：lp 8/15~8/17 三天 zdp=9.94%、p=6970（**6.97 元 = 8/14 涨停收盘价**）；kline 8/14 close=6.97、8/17 close=6.63（跌）→ 池内容对应 **8/14 涨停日**。
- **结论**：limit_pool 是"曾涨停滚动池"，**逐日 join 对表口径不成立**（V1 校验样本同此局限）→ 改用**集合级对表**。

### 2.2 集合级对表（窗口 = 重叠期 + 前 4 个涨停日，覆盖池滞后）
- 重叠 22 交易日（2026-08-17 ~ 2026-09-15）：池收录 771 码、kline 反推 878 码，**命中 682/771 = 88.5%**。
- miss=89 归因：**DATA_EXCLUDE 排除清单 37**（如 000551/000564 在清单内，预期 miss）+ **真差异 52**（含除权基准差——000620/000936/000950 等 lp 昨收与 kline 前收不同源；及池滞后极端）。
- 除权差异已单独实证：000620 lp zdp=10.18%（涨停）vs kline 前收基准比值 2.31%（kline 前收 3.03 vs lp 昨收 2.75）——**kline qfq 序列在除权日与实时昨收口径不同**，此类票 kline 反推天然漏（V1 同款"除权断层日剔除"语境）。

### 2.3 旧表数据代差披露（非硬门）
- 旧 qg_zt_full 1852 天 vs 当前 kline 同口径反推逐日全字段一致**仅 18 天（0.97%）**。
- 归因：**kline 数据在 8/21 后发生变更**（E1 日K全库回补等使历史覆盖/close 变化），旧表为 8/21 前的库快照口径。2019-01-03 旧 zt_count=12 vs 现反推 15（过滤 DATA_EXCLUDE 后）、不过滤 17——**数据代差 + 排除清单演进**共同所致。
- **处理**：回填以当前 kline 为准、全表 DROP+重建（与 build_sentiment_history.py 标准操作一致），**全表口径统一**；下游（sentiment_gate phase 查询 / backtest_zt_eco 对照）将读取新口径序列——属有意变更，非回归。

## §3 回填执行与判据结果

### 执行（2026-09-16）
- 备份旧表 → `tmp/k4/qg_zt_full_before_k4.csv`（1852 行）。
- DROP+重建 `qg_zt_full`：**1869 行**（2019-01-03 ~ 2026-09-15）。
- 重建 `qg_sentiment_history`：快照 `2019_2026-09-15`，date_count=1869，payload 含 built_at/start/end/dates/gate（limit_pool 集合对表 + 旧表代差）。

### 判据逐项
| # | 判据 | 结果 | 判定 |
|---|---|---|---|
| ① | 行数 ≥ 80% 窗口 | qg_zt_full 1869 行；qg_sentiment_history.date_count=1869 ≥ 96（120×0.8）且 ≥ 200（250×0.8） | **PASS** |
| ② | 最近 20 完整交易日温度非 None | `eco_temperature` 对 2026-08-19 ~ 2026-09-15（kline 行数≥1000 的完整日）**0 天 None** | **PASS** |
| ③ | 最近 5 日现算逐位一致 | 9/09/9/10/9/11/9/14/9/15 五日落库 vs 现算（同函数同进程）**逐字段一致 5/5** | **PASS** |
| ④ | 严格 PIT | 构造保证：`detect_zt_dates` 只用当日及以前 close；prev_zt_premium 只用当日收盘序列 | **PASS** |

### 边界语义（披露）
- kline 存在"半成品日"：2026-09-16 仅 1 行（盘中数据）→ zt_ecosystem build 将其计入 series 尾部且 temperature=None。判据②按**完整交易日**口径（行数≥1000）验收 PASS；半成品日由 `recent_temperatures` 过滤，不视为回归（引擎 `eco_temperature(prev_date)` 取前一完整交易日，不受影响）。

## §4 展示分支（只读）

- `app/zt_ecosystem.py` 新增 `recent_temperatures(days=20, as_of=None)`：最近 N 个完整交易日温度序列（PIT、过滤 None/半成品日）。
- `app/server.py` 新增只读端点 **`GET /api/sentiment/eco?days=20[&as_of=]`**：`{recent_days: [...], qg_zt_full: {rows, max_date}, as_of, generated_at}`。
- 8898 实测：200，recent=20（2026-08-19 ~ 2026-09-15，温度 22.5/39.4/44.0...），qg_zt_full rows=1869 max=2026-09-15。

## §5 防复发建议（只建议不改码）

1. **接入日更链路**（推荐）：updater 收盘流程末尾（或独立计划任务，如 15:30）调 `python tools/backfill_zt_history.py --start 2019-01-01`——全量重算 107s（5 万+ 票·日），收盘后低频可接受；或加 `--daily` 增量模式（仅重算最近 5 日 + INSERT OR REPLACE）。
2. **健康监控**：audit 记 `qg_zt_full_coverage {rows, max_date}`，max_date < 最近交易日 → WARN（同 daily_coverage 模式）。
3. **半成品日防护**：updater 日K落库前过滤"当日行数 < 全市场 20%"的残留（或在 zt_ecosystem build 时过滤），避免尾部 None 温度进入 series。

## §6 诚实披露

1. **limit_pool 对表为集合级且不作硬门**：池语义（无涨停日字段、滚动快照、周末复制）使逐日对表不可行；88.5% 命中含排除清单/除权/滞后归因，真差异 52 码未逐一人工核验（已列样例）。
2. **旧表 1852 天被 DROP+重建覆盖**（备份在 tmp/k4/）：新口径与旧表 99% 不同（kline 数据代差），下游历史对照结果会变化——若需与旧报告逐日对照，用备份 CSV。
3. **prev_zt_premium 用前 100 只截断**（沿用 build_sentiment_history 原口径）：大涨停日（>100 只）均值有抽样偏差，与旧表同口径一致，未改进。
4. **回填耗时 107s**（单进程）；未做并行，避免与其它进程抢算力。
5. **8899 未重启**：展示分支与 recent_temperatures 未在生产生效。

验证方式：`python tools/backfill_zt_history.py --dry`（对表+代差+统计）、`python tmp/k4/verify.py`（判据②③）、8898 `GET /api/sentiment/eco`（展示）。
