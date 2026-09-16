# W4｜akshare 五路免费源探测与接入设计（研究·唯一出网块）

> 状态：**完成**。五路全部可用（0 strikes），数据落盘 `data/ak_probe_results.json` + `tmp/w4/raw/`。
> 本块只验证数据可用性与接入设计，**未修库、未落地、未注册任务**。

## 0. 红绿灯总表

| 路 | akshare 函数 | 可用 | 历史起点(实测) | 单样本规模(实测) | 关键字段 | 单次耗时 |
|---|---|---|---|---|---|---|
| ① 龙虎榜 | `stock_lhb_detail_em` | 🟢 | ≥2010-01-04 | 51~71 只/日 | 净买额/上榜原因/**上榜后1-10日** | 0.6~0.8s |
| ② 两融-沪 | `stock_margin_detail_sse` | 🟢 | ≥2011-01-04 | 1775~1999 只/日 | 融资余额/融资买入/融券余量 | 0.8~1.0s |
| ② 两融-深 | `stock_margin_detail_szse` | 🟢 | ≥2011-01-04 | 1842~2101 只/日 | 融资余额/融券余额/融资融券余额 | 1.0~1.8s |
| ③ 限售解禁 | `stock_restricted_release_queue_em` / `_detail_em` | 🟢 | ≥2010-01-04 | 个股全量批次 + 周度 27 只 | **解禁时间**/实际解禁数量/市值/占比 | 0.2~0.5s |
| ④ 业绩预告 | `stock_yjyg_em` | 🟢 | ≥2010-03-31 | 628~4885 只/报告期 | 预测数值/预告类型/**公告日期(PIT)** | 1.1~4.1s |
| ④ 业绩报表 | `stock_yjbb_em` | 🟢 | ≥2010-03-31 | 5895~11445 只/报告期 | 净利润/同比增长/**最新公告日期** | 6.2~10.6s |
| ⑤ 复权因子 | `stock_zh_a_daily(adjust="hfq-factor")` | 🟢 | 上市日起(全历史) | 因子变更日表 + 505 日 raw | **hfq_factor**/未复权 close | 0.3~0.4s |

## 1. 执行记录与纪律

- **时间**：2026-09-01 00:32~00:37（凌晨窗口，东财系接口相对宽松，时机合规）。
- **限流自律**：并发=1、请求间隔 ≥1s、单请求 socket 超时 30s；正式探测 17 次 + 历史起点 6 次 = 23 次请求，**0 strikes、0 失败**。
- **写入面**（红线限定 4 个）：`tools/ak_probe.py`（可重复跑）、`data/ak_probe_results.json`、`docs/reports/ak_source_probe.md`（本报告）、`tmp/w4/raw/`（17 个样本原始 CSV）。
- **红线**：未改 `app/config.py`、未写 `market.db`/`min5.db`/任何既有 data/ 文件、零落地、未注册计划任务、未碰 W5 的 web/ 与 server.py。

## 2. 五路证据节

> 每路给出：字段样例（截断）、历史起点实测、更新频率、与库内已有数据一致性抽检。

### 2.1 ① 龙虎榜
- **字段样例**（`lhb_20260828.csv`，前 2 行，21 列）：`序号/代码/名称/上榜日/解读/收盘价/涨跌幅/龙虎榜净买额/龙虎榜买入额/龙虎榜卖出额/龙虎榜成交额/市场总成交额/净买额占总成交比/成交额占总成交比/换手率/流通市值/上榜原因/上榜后1日/上榜后2日/上榜后5日/上榜后10日`。
  例：`000017 深中华A 上榜日2026-08-28 净买额541万 上榜原因"日涨幅偏离值达到7%的前5只证券"`。
- **历史起点**：`stock_lhb_detail_em(20100104,20100104)` 返回 15 行 → 至少回溯 2010。
- **更新频率**：东财 T 日盘后日更（2024-03-15 与 2026-08-28 两截面均有完整数据）。
- **一致性抽检（vs 库内 limit_pool）**：2026-08-28 龙虎榜 51 只，库内涨停池（limit_pool, `date=20260828`）52 只，**交集 12** → **增量 39 只（76%）**。增量来自换手率榜/涨幅偏离/连续涨幅偏离等非涨停维度 → 龙虎榜是与涨停池**正交的新资金面维度**。

### 2.2 ② 两融（SSE + SZSE）
- **字段样例**（沪 9 列）：`信用交易日期/标的证券代码/标的证券简称/融资余额/融资买入额/融资偿还额/融券余量/融券卖出量/融券偿还量`；深 8 列：`证券代码/证券简称/融资买入额/融资余额/融券卖出量/融券余量/融券余额/融资融券余额`。
  例：SSE `510050 50ETF 融资余额14.24亿`；SZSE `000001 平安银行 融资余额46.30亿 融资融券余额47.10亿`。
- **历史起点**：`stock_margin_detail_sse(20110104)` 50 行、`_szse(20110104)` 40 行 → 至少回溯 2011（两融制度 2010-03 上线后）。
- **更新频率**：交易所日更（T+1 披露口径）。
- **一致性抽检（vs 库内 moneyflow rzrq）**：现役 `moneyflow kind='rzrq'` 仅 1871 行、2023-04-21~2026-08-28（稀疏覆盖）；akshare 提供**全市场 ~4000 只/日**（沪 ~2000 + 深 ~2100）。→ akshare 两融路是现有 rzrq 的**全量补全来源**（可横向对齐同一股票同日的融资余额做交叉验证）。

### 2.3 ③ 限售解禁
- **字段样例**（个股批次 13 列 / 周详情 12 列）：`解禁时间/解禁股东数/解禁数量/实际解禁数量/未解禁数量/实际解禁数量市值/占总市值比例/占流通市值比例/解禁前一交易日收盘价/限售股类型/解禁前20日涨跌幅/解禁后20日涨跌幅`。
  例：`600000` 上市以来 4 批解禁全量（含 2020-09-04 定向增发机构配售 12.48 亿股，占比 4.25%）；周详情 `002157 正邦科技 2026-08-24 解禁 22.36万股 其他类型`。
- **历史起点**：`stock_restricted_release_detail_em(20100104,20100108)` 8 行 → 至少回溯 2010。
- **更新频率**：东财日更（详情按日期段，批次按个股全量）。

### 2.4 ④ 业绩预告 / 业绩报表
- **字段样例**：
  - 预告 `yjyg`（11 列）：`序号/股票代码/股票简称/预测指标/业绩变动/预测数值/业绩变动幅度/业绩变动原因/预告类型/上年同期值/公告日期`。例：`600187 *ST国中 2026-08-19 公告，预测 2026H1 净利润盈利 265~315 万，类型"扭亏"`。
  - 报表 `yjbb`（16 列）：`...每股收益/营业总收入/净利润/同比增长/净资产收益率/销售毛利率/所处行业/最新公告日期`。例：`601091 沈鼓集团 2026H1 净利润 2.80 亿 同比-2.28% 最新公告2026-09-01`。
- **历史起点**：`yjyg_em(20100331)` 281 行、`yjbb_em(20100331)` 2313 行 → 至少回溯 2010。
- **更新频率**：按报告期批量（季报/半年报/年报披露季），东财持续更新。
- **PIT 抽检（重点）**：
  - **业绩预告 `公告日期` 100% 非空**（628/628、4885/4885），且落在合理披露窗：2024Q1 预告区间 2024-01-31~2024-05-06；2026H1 预告区间 2026-04-27~2026-08-19 → **PIT 可用**（公告日即信息公开展，事件可在该日对齐）。
  - ⚠ **业绩报表 `最新公告日期` 是最新修订日**：2024Q1 报表的最新公告日期可达 2026-08-29（后续修订），2026H1 报表到 2026-09-01 —— **不是首披露日**。严格 PIT 需以 `yjyg.公告日期` 为准，或对 yjbb 记录"首见日"。

### 2.5 ⑤ 复权因子（重点节：qfq 跨段污染实锤）
- **字段样例**：`stock_zh_a_daily(adjust="hfq-factor")` 返回 `date + hfq_factor`（仅除权除息**变更日**，600000 近 2 年 29 条、002130 近 2 年 21 条；`adjust=""` 返回 `date/open/high/low/close/volume/amount/outstanding_share/turnover` 未复权日线 505 行）。
- **历史起点**：因子表回溯至上市日（600000 1999-11-10=1.0，含 1900-01-01 基准行）→ **全历史**。
- **更新频率**：日更（raw 为全量交易日）。
- **qfq 重算抽检**（方法：`qfq(t) = raw_close(t) × hfq_factor(t) / hfq_factor(最新)`）：
  - **600000（对照）**：现役 kline 与 akshare-raw 对比，**2024-08~2025-07-16 段恒偏移 +4.724%**，2025-07-17 后 = raw → 除息日 **2025-07-16 存在 ~4.7% 伪阶跃**：现役 kline `14.12→13.44`，akshare 重算 qfq `12.87→12.83`（连续）。全窗 505 日中 **231 日偏差 >0.5%**。
  - **002130（污染票，quality_report 记录 2025-08-14 +579% 跳变）**：现役 kline **2025 年仅 94 行**，缺失 **2025-08-01~08-13 整段**（跳变断裂段），max date 仅 2026-08-26；akshare 提供完整连续 raw（2025-08 在 22~25 元区间）+ 正确因子。
  - **结论**：akshare hfq-factor 路是 **qfq 跨段污染的正解数据源**（检测 + 修复口径），与 E 块治理方案衔接。**本块只验证可用性，未修库**；修复动作留给获批后的批次。

## 3. 接入设计节

> 统一原则：**独立库 `data/alt_data.db`**，不碰主库；更新统一放 **21:30**（避开现役 6 个计划任务窗口 15:10/16:30/20:00/20:30 及周末 catch-up）；全部只读消费。

### 3.1 新表 DDL 草案（data/alt_data.db，均含日期守卫）

```sql
-- ① 龙虎榜（按日覆盖，日更全量替换当日）
CREATE TABLE lhb_daily (
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, reason TEXT,
  net_buy REAL, buy_amt REAL, sell_amt REAL, turnover_amt REAL,
  mkt_amt REAL, turnover_rate REAL, fwd_ret_1d REAL, fwd_ret_2d REAL,
  fwd_ret_5d REAL, fwd_ret_10d REAL,
  PRIMARY KEY (date, code, reason)
);
-- ② 两融（按日覆盖；SSE/SZSE 两源合表，字段对齐）
CREATE TABLE margin_daily (
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  rz_bal REAL, rz_buy REAL, rq_vol REAL, rq_bal REAL, rzrq_bal REAL,
  market TEXT CHECK (market IN ('SH','SZ')),
  PRIMARY KEY (date, code, market)
);
-- ③ 限售解禁（批次全量 + 周详情，按解禁日覆盖）
CREATE TABLE unlock_plan (
  unlock_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  unlock_qty REAL, actual_qty REAL, mkt_val REAL,
  pct_total REAL, pct_float REAL, lock_type TEXT, prev_close REAL,
  PRIMARY KEY (unlock_date, code, lock_type)
);
-- ④ 业绩预告（报告期快照，PIT 用公告日期做事件日）
CREATE TABLE earnings_forecast (
  period TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  metric TEXT, forecast_text TEXT, forecast_val REAL, change_pct REAL,
  fcast_type TEXT, prev_val REAL, announce_date TEXT NOT NULL,
  PRIMARY KEY (period, code, metric)
);
-- ④b 业绩报表（报告期快照；最新公告日期=修订日，PIT 慎用）
CREATE TABLE earnings_report (
  period TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  eps REAL, revenue REAL, revenue_yoy REAL, net_profit REAL, np_yoy REAL,
  roe REAL, announce_date TEXT,
  PRIMARY KEY (period, code)
);
-- ⑤ 复权因子（变更日表，全历史累积；raw 日线按需入 kline 系）
CREATE TABLE adj_factor (
  code TEXT NOT NULL, eff_date TEXT NOT NULL, hfq_factor REAL,
  PRIMARY KEY (code, eff_date)
);
```

### 3.2 更新时点与容量估算

| 路 | 建议更新时点 | 增量节奏 | 年容量估算 |
|---|---|---|---|
| 龙虎榜 | 每日 21:30 | T 日盘后全量替换当日 | ~60 只/日 × ~250 日 ≈ **1.5 万行/年** |
| 两融 | 每日 21:30 | 沪深 2 源各一请求 | ~4100 只/日 × 250 日 ≈ **205 万行/年**（每行 ~100B，≈200MB/年，可控） |
| 限售解禁 | 每日 21:30 | 批次全量 + 周详情 | 全市场批次 ~几千行 + 周详情 ~100 行/周 ≈ **<1 万行/年** |
| 业绩预告/报表 | 披露季每日 21:30 | 4 报告期/年，逐期拉取 | ~1.1 万行/期 × 4 ≈ **4.4 万行/年** |
| 复权因子 | 每日 21:30 | 全市场变更日增量 | ~5000 只 × 年均 ~5 次 ≈ **2.5 万行/年**（变更日表，体量极小） |

### 3.3 消费方（哪些因子研究/风控能用上）

| 路 | 消费方 |
|---|---|
| 龙虎榜 | 资金面因子（净买额/机构席位/上榜后收益）、情绪面、与涨停池交叉验证（增量 76% 正交） |
| 两融 | 杠杆资金因子（融资余额变化率/融资买入强度）、risk 监控（杠杆资金异动）、与现有 rzrq 交叉验证 |
| 限售解禁 | 供给压力因子（解禁市值/占流通比）、事件研究（解禁前后窗口）、风控（解禁日临近预警） |
| 业绩 | 基本面因子（业绩预告超预期/扭亏/变动幅度）、**PIT 事件对齐**（公告日期） |
| 复权因子 | **qfq 污染治理正解**（E 块衔接：检测跨段跳变 + 提供修复口径）、复权价正确性基准、新策略回测复权 |

### 3.4 同步任务注册命令（备批，本块不注册）

见 `tmp/w4/ready_register.ps1` —— 每日 21:30 增量拉取 cron 模板，指向**尚未实现的** `tools/ak_sync.py` 骨架（实现留给获批后的下一批，本块不建半成品工具）。

## 4. 合规记录（红线核查）

- 未改 `app/config.py`、未写任何 .db（全部只读 URI `mode=ro&immutable=1`）、零落地、未注册计划任务、未碰 W5 的 web/ 与 server.py；
- 限流自律全过：并发=1、间隔 ≥1s、30s 超时、23 次请求 0 strikes、0 硬刷；
- 工具 `tools/ak_probe.py` LF、`py_compile` 通过；`--dry/--routes/--history` 三模式可重复跑，多路重跑**合并**而非覆盖（保留历史探测段）；
- 原始样本 19 个 CSV 全落 `tmp/w4/raw/`，可复核。

## 附录：写入面文件与哈希

| 文件 | 操作 | 说明 |
|---|---|---|
| `tools/ak_probe.py` | 探测脚本（可重复跑） | py_compile OK，CRLF=0 |
| `data/ak_probe_results.json` | 五路结果 + 历史起点 | 23 次请求证据全在 |
| `docs/reports/ak_source_probe.md` | 本报告 | — |
| `tmp/w4/raw/*.csv` | 17 个样本原始落地 | 字段样例/抽检可复核 |
| `tmp/w4/ready_register.ps1` | 同步注册模板（备批） | 指向未实现的 ak_sync.py 骨架 |
