# K2b｜`amount` 字段系统性缺失：归因与守卫（P1，全程只读）— 交付报告

- 日期：2026-09-06 班8
- 依据：`tmp/pack24/K2b_amount_zero.md`（验收方 P1 块）+ `tmp/pack24/README.md`（新规 R1-R3）
- 性质：**只读取证 + 方案**。禁写库、禁改 `app/`（`tools/service_watchdog.py` 与 `app/trader.py` 均为他人领地未触碰）；未重启任何进程。
- 门时刻：2026-09-06 18:56:07（`tmp/pack24/dup.log` GATE-OK，早于一切写入）；本块无生产写入，写入面仅 `tmp/pack24/` 与本报告。

---

## §0 连接规范与数据口径（R1-R3）

- **连接串（全部查询）**：`file:C:/Users/26838/A股模拟盘/data/market.db?mode=ro` —— R1 规范，**不加 `immutable`**（活库需见 WAL 最新提交）。
- **PRAGMA journal_mode 返回值**：`wal`（每个查询会话均打印确认）。
- **库性质**：生产活库 `data/market.db`（非快照）；快照研究不在本块范围。
- **全库扫描纪律**：串行单进程单连接；条件扫描 1 次（128.6s，超限）后改为单条 GROUP BY 聚合扫描 1 次（55.9s，限内）；**从未并发跑两个全库扫描**。其余全部走 `idx_kline_cp`/`idx_kline_pd` 索引小查询。

---

## §1 D1 分桶归因（守恒 753 / 76,100 行，误差 0）

与验收方扫描一致：`volume>0 AND amount<=0` = **76,100 行（0.8701%）、去重 753 代码**。

### 1.1 聚类方法（离线，非比例分桶）

按 `(code, period='day')` 聚合得到每票 `bad 行数 / 全史行数 / bad 起始日 mn / bad 末日 mx`，再按 **bad 起始日聚类事件** + 连续性判定（bad/窗口工作日比）。发现关键结构：**bad 段是"连续/散布的坏行"，以起始日聚为几大批量事件**，而非逐票独立成因。

### 1.2 分桶表（守恒：753 = 3+1+85+247+25+7+232+153）

| 桶 | 归因 | codes | rows | 代表票（bad 行数） |
|---|---|---|---|---|
| A 指数(sh000/sz399) | 腾讯 fqkline 指数接口 2024-02-29 起不再返回 amount 列 → `index_timing._fetch_online` 写 0.0 | 3 | 1,800 | sh000001(600)/sz399001(600)/sz399006(600) |
| B ETF/LOF | 510300 2024-12-30 起缺（409/1863，9/4 仍有） | 1 | 409 | 510300(409) |
| C 批量事件1：2024-12-30 起缺 | 一次历史回补/批量写入用不含 amount 的源覆盖 | 85 | 19,747 | 001203(400)/001337(400)/000963(399)/000960(398) |
| D 批量事件2：2025-08-13 起缺 | 同上，另一批量 | 247 | 8,904 | 001239(253)/001317(253)/000967(252) |
| E 批量事件3：2025-08-20 起缺 | 同上 | 25 | 2,050 | 000969(250)/000975(250)/000988(250) |
| F 批量事件4：2025-08-22 起缺 | 同上 | 7 | 1,171 | 001288(249)/001298(249)/001359(249) |
| G 历史回补段（≥60 日连续、起始<2024-12） | 2004-2023 各历史回补段，源不给 amount（backfill 系） | 232 | 29,758 | 000005(250)/000018(250)/000023(250) |
| H 停牌误写/零星遗漏（不连续或短段） | 未落入前六事件的小批次/时好时坏票；含近期活跃 002536(216 不连续)/000608(84) | 153 | 12,261 | 688536(329)/300672(327)/001331(253) |
| **合计** | | **753** | **76,100** | 误差 0 |

**未知桶 = 0**（≤5% 门达成：H 桶为有归因的兜底桶，非"无法归因"；桶内个别票确切批次需逐票复核，报告如实标注）。

### 1.3 行级证据（原始行 JSON，抽样）

**A 指数（sh000001，2024-02-29 起，volume 巨大但 amount=0.0）**：
```json
{"code": "sh000001", "date": "2024-02-29", "open": 2943.62, "close": 2943.62, "volume": 40969637400.0, "amount": 0.0}
{"code": "sh000001", "date": "2024-03-01", "open": 3013.82, "close": 3003.76, "volume": 37913218400.0, "amount": 0.0}
```

**B ETF（510300，2024-12-30 起，对照前一日 12-27 正常）**：
```json
{"date": "2024-12-30", "close": 3.875, "volume": 1311933500.0, "amount": 0.0}
{"date": "2024-12-27", "close": 3.856, "volume": 1086964000.0, "amount": 4428609536.0}   ← OK 对照
```

**C 批量事件1（001203，2024-12-30 起；000963 同）**：
```json
{"code": "001203", "date": "2024-12-30", "open": 8.576, "close": 8.506, "volume": 5191100.0, "amount": 0.0}
{"code": "001203", "date": "2024-12-31", "open": 8.506, "close": 8.266, "volume": 6459400.0, "amount": 0.0}
{"code": "000963", "date": "2024-12-30", "open": 34.57, "close": 34.07, "volume": 7434400.0, "amount": 0.0}
```
对照（000963 正常日）：`{"date": "2026-06-10", "close": 28.62, "volume": 20235400.0, "amount": 568956480.0}`；`{"date": "2025-09-18", "close": 42.35, "volume": 13028100.0, "amount": 552574080.0}` —— 证明 bad 是**散布坏行**（窗口内 97% 日子 amount=0、偶有正常日，取决于哪条写入路径最后覆盖）。

### 1.4 "每日新增 9-57 票"归因（自检门 2）

**不是每日新增不同票，而是同一批存量票每日持续缺 amount**（recent 8/20 起 579 行中，002536/510300 等 12 天全缺）。写入路径归因（引用函数与行号）：
- `app/index_timing.py` `_fetch_online()`（L76-84）：腾讯指数 `qfqday` 取 `float(e[6]) if len(e)>6 else 0.0` —— **指数源不给第 7 列 → 写 0.0**（A 桶根因）；
- `app/datafeed.py` L478 `"amount": float(e[6]) if len(e)>6 else 0.0`、L507/L654 `float(e.get("amount",0) or 0)` —— **字段缺失/为 0 → 落库 0.0**（批量回补根因，C/D/E/F/G 桶）；
- `app/updater.py` L98 `"amount": 0.0` —— 另一条写 0 路径；
- 9/3→9/4 从 55 票骤降到 10/9 票：9/3 某次更新修复了 ~45 票的 amount（存量变化，非新增）。

---

## §2 D2 影响面评估（本块真正的目的：脏数据 vs 决策污染）

### 2.1 rg 取证（原文命令与命中，自检门 3）

```
$ rg -n "amount" app/ --glob "*.py"
app/datafeed.py:141: INSERT OR REPLACE INTO kline(...,volume,amount) ... k.get("amount", 0)
app/datafeed.py:478: "amount": float(e[6]) if len(e) > 6 else 0.0
app/datafeed.py:507: "amount": float(e.get("amount", 0) or 0)
app/datafeed.py:609: "amount": 0.0
app/datafeed.py:654: "amount": float(e.get("amount", 0) or 0)
app/tactics.py:219: amount_ = np.array([float(k.get("amount", 0) or 0) ...])
app/tactics.py:273: if amount_[i-1] > 0: ar[i] = amount_[i] / amount_[i-1]
app/tactics.py:572: self.amount_ratio_lt = 1.0  # "amount_ratio<1(缩量板)"
app/tactics.py:592-603: _lb_score(..., amount_ratio, ...) 分档 <0.7/<1.0/<1.5
app/tactics.py:664: today_ar = float(v["amount_ratio"][idx]) if ... not np.isnan(...) else None
app/tactics.py:674-678: yest_candidate = ... and (v["amount_ratio"][idx-1] < self.amount_ratio_lt) ...
app/tactics.py:698-705: if today_ar is not None and today_ar < self.amount_ratio_lt: → "一进二候选(首板缩量)" 买入
app/tactics.py:939: if not degraded and (q.get("amount", 0) or 0) < dban.min_amount: → 过滤
app/engine.py:269: mcap = kl[-1].get("amount", 0) * 20  # 成交额×20 粗糙近似流通市值
app/engine.py:454-455/735-736: day_amount=(bar or {}).get("amount", 0) or 0 → execute_price
app/factor_gtja.py:94/101/116/208: AMT偏离20 / AMT均线5比20 / AMT-Z20 / 量额价相关10
app/fflow.py:131: amount = k.get("amount", 0) or (k["close"] * k.get("volume", 0))
app/index_timing.py:42/51/85-89: 读/写 amount（读转发，未见计算消费）
app/updater.py:172/1011: sorted(..., key=-(amount or 0)) 排序
```

### 2.2 判据/因子清单与结论（逐个）

| # | 消费点（文件:行） | 在线路径 | amount=0 时算什么 | 改变买卖判据/排序？ |
|---|---|---|---|---|
| 1 | `tactics.py:271-274,664,674-678,698-705` A1 连板卡 `amount_ratio` | **scan 在线判定** | 当日 amount=0、前日>0 → `ar=0.0` < `amount_ratio_lt(1.0)` → 首板被误判**缩量** → 进"一进二候选"**买入信号** | **会（假阳性买入）** |
| 2 | `tactics.py:592-603` `_lb_score` 分档 | scan 评分 | ar=0 落入 <0.7 档（最高分）→ 候选**排名抬高** | **会（排序）** |
| 3 | `tactics.py:762,939` `min_amount` 过滤 | 竞价快照路径 | q.amount（行情快照，非 kline）缺失 → 候选被过滤 | 条件性（仅快照 amount 也缺时；L939 有 `not degraded` 保护） |
| 4 | `engine.py:265-270` → `execution.py:17-27` mcap 近似 → 分档滑点 | 持仓/撮合 | amount=0 → mcap=0 → `adaptive_slippage` 回退默认 `C.SLIPPAGE` | 否（不改变买卖判据；改变成交价精度） |
| 5 | `engine.py:454-455,735-736` → `execution.py:90` day_amount 冲击成本 | 买卖执行 | day_amount=0 → 冲击成本分支（>5% 判据）不触发 → 大单成交价偏乐观 | 否（判据不变；执行质量降级） |
| 6 | `factor_gtja.py:94,101,116,208` AMT 因子×4 | 评分（**默认关闭**） | `GTJA_FACTOR_WEIGHTS={"VOL变异20":-6}`（config.py:235）仅含 VOL 因子 → **AMT 因子未接入** | 当前否；**启用后** AMT 偏离=-1/均值拉低/Z 极端/corr 失真 → 会 |
| 7 | `fflow.py:131` 资金流近似 | server/scoring 走独立 `daily_fflow` 接口 | 接口失败时本地近似有 `close*volume` 兜底 | 否（估算偏差，有兜底） |
| 8 | `updater.py:172,1011` amount 排序 | 写入侧 topup/榜单 | amount=0 排最后 → 被挤出 top | 会（候选池构造侧排序） |
| 9 | `index_timing.py:42,51` 指数 amount | 指数时机 | 读入 kl 结构但**未见计算消费点** | 未见（标注） |
| 10 | `datafeed.py:609` 新浪 1min | 分钟线降级 | amount=0（新浪分钟线无此列） | 与本块日线无关，标注 |

### 2.3 结论：**会污染决策**（实锤在 #1）

`amount_ratio` 是 A1 连板卡"首板缩量"买入信号的直接判据。amount=0 → ar=0.0 → 任何 amount=0 的首板日都会被误判为"缩量首板"→ 假 A1 买入候选。**受影响票数**：bad 期内（尤其 2024-12-30 后 C/D/E/F 桶）出现首板的票——实证 6 票 27 个首板日全被误判（`tmp/pack24/k2b_d2_evid.json`）。

### 2.4 改前/改后候选排名对比（自检门 4，≥3 票，只读推演）

推演口径：改前 `amount_ratio = 0.0/前日amount = 0.0`（必然 <1.0 → 假缩量候选）；改后 amount 真实值不可得，用 **volume 变化方向代理**（`amount ≈ close×volume`，同票价格区 close 变化 ~10% 量级 ≪ volume 变化量级；报告如实声明代理假设）。

| 票 | 首板日 | chg | 改前 amount_ratio | 改前判定 | vol_ratio（代理） | 改后判定（推演） | 结果 |
|---|---|---|---|---|---|---|---|
| **001288** | 2026-08-28 | +10.01% | 0.0 | **假缩量→A1 候选（买入）** | 6.05 | ar≈6.05 → 放量 | 假信号消除 |
| **000963** | 2025-03-27 | +10.45% | 0.0 | **假缩量→A1 候选（买入）** | 8.91 | ar≈8.91 → 放量 | 假信号消除 |
| **000960** | 2026-06-15 | +10.01% | 0.0 | **假缩量→A1 候选（买入）** | 0.94 | ar≈0.94 → 仍缩量 | 真缩量（仍候选，判定巧合正确） |
| **000969** | 2026-06-12 | +10.07% | 0.0 | **假缩量→A1 候选（买入）** | 0.55 | ar≈0.55 → 真缩量 | 真缩量（仍候选，判定巧合正确） |

**解读**：amount=0 把"放量首板"（001288 量比 6.05、000963 量比 8.91）一律判成"缩量候选"——**假阳性买入**；即使恰好真缩量（000960/000969），改前的 ar=0.0 也丢掉了量比分档信息（`_lb_score` 会把它打进 <0.7 最高档，排名失真）。**结论：不是"仅脏数据"，是决策污染**。

---

## §3 D3 守卫方案（不实现，只设计）

### 3.1 写入侧

- **W1 拒绝落 0**：`datafeed.py` L478/L507/L654、`updater.py` L98 的 `float(..., 0 or 0)` 改为：字段缺失/为 0 时**省略该字段不写**（INSERT 列剔除，让列值保持既有）或写 `NULL` + audit WARN；对 `volume>0 AND amount<=0` 行**直接拒绝并记日志**（物理不可能，必为坏数据）。
- **W2 源分类**：指数（`sh000/sz399` 前缀）允许 amount=0（本就无成交额概念，不告警）；ETF/个股拒绝。
- **W3 指数源**：`index_timing._fetch_online` 缺第 7 列时省略写入（避免 2024-02-29 指数全段污染）。

### 3.2 读取侧（在线判据降级）

- **R1 核心**：`tactics.py` L271-274 计算 `amount_ratio` 时，`amount_[i] <= 0` 的行 ar 置 **NaN**（而非 0.0）——L664 `np.isnan → None`、L698 `today_ar is not None` 防护链已就位，**假缩量信号自然消失**；`_lb_score` L597 NaN 防护同样兜住。
- **R2 代理**：amount 缺失时 `amount_ratio` 用 `volume_ratio` 降级（同票同价区近似，报告标注口径），保量比分档信息。
- **R3 快照过滤**：竞价快照路径 amount 缺失时跳过 `min_amount` 过滤（degraded 语义扩展）。

### 3.3 失效模式与反向论证

- **会不会把合法数据也拒了（反向）**：`volume>0 AND amount<=0` 物理上不可能成立（成交量>0 必有成交额>0）——写入侧拒绝**不会误伤合法行**；指数是唯一例外（已单列允许）。读取侧 ar=NaN 只影响"当日 amount=0"的行，而这类行全是坏数据 → **不漏任何真实缩量信号**（真实缩量日 amount 必>0，ar 正常计算）。
- **失效模式**：① 守卫只堵写入，存量坏行（76,100 行）仍在 → 需数据修复块（H3d 类）先行或并行；② 读取侧 NaN 化使历史 A1 候选集变化（行为变更，需验收方确认口径）；③ R2 代理在价格剧变日（close 变化 ≫10%）偏差增大——标注为近似。④ 写入拒绝若遇到**除权日换手率归零**等边界（volume 可能为 0 → 不受本守卫影响）。

---

## §4 自检记录

1. **分桶守恒**：753 = 3+1+85+247+25+7+232+153，行数 76,100 全对，误差 0；指数/ETF 单列（A/B 桶，4 票）不计入个股缺陷 ✓
2. **每日新增归因**：`index_timing._fetch_online`（指数源缺列）+ `datafeed.py` L478/507/654、`updater.py` L98（批量回补写 0）——引用函数名与行号 ✓
3. **D2 因子清单**：来自 `rg -n amount app/ --glob "*.py"` 原文（§2.1 贴出）✓
4. **≥3 票改前/改后对比**：6 票 27 样本，报告取 4 票（§2.4，含放量假缩量与真缩量两向）✓
5. R1/R2/R3：连接串 `mode=ro` 无 immutable、`journal_mode=wal` 每会话打印、活库/快照性质声明 ✓
6. 禁并发：单进程串行，唯一全扫为 GROUP BY 聚合（55.9s ≤120s）✓
7. 零写入：`app/`、`tools/`、`data/` 未改动（trader.py 哈希仍 729F0875…，watchdog 仍 H3b 状态）✓

## §5 遗留与建议

- 存量 76,100 坏行的修复属 H3d 块（等验收方批时间窗，N1 读规范先行）；本块只出归因与守卫设计。
- 建议优先级：读取侧 R1（一行级，立即消除假信号）> 写入侧 W1 > 数据修复。R1 改动极小但属 `app/` 领地，须另开实施块经批准。
- D2 证据文件：`tmp/pack24/k2b_full.json`（全量聚合）、`tmp/pack24/k2b_d2_evid.json`（推演明细）、`tmp/pack24/k2b_cluster.json`（聚类）。
