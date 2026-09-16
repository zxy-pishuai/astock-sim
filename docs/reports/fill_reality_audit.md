# 撮合现实性审计报告（Y3）—— 流动性参与率 / 假零 turnover / 一字板语义

- 报告日期：2026-09-01（重放运行中落盘，§4 结果随运行回填）
- 执行者：Y3 并发执行者（研究型·零落地）
- 快照：`data/snapshots/2026-08-27/market.db`（只读，mode=ro；与 R3/W3 同快照同 196 口径）
- 数据：`data/bt_sizing_lab.json`（S0 基线）、`data/bt_fill_reality.json`（本审计重放）

---

## §0 预注册（落盘于首格结果之前，锁定后不改）

### 参与率档位（当日成交额占比，分母 = kline.amount）
- 档位一：**1%**（极保守：机构单常见参与上限）
- 档位二：**3%**（本审计重放采用的强制缩量上限）
- 档位三：**5%**（宽松参与上限；config 现行 EXEC_PARTIAL_CAP=0.30 远高于此）

### 判据（预注册）
1. **分布判据**：若 base 格（现役 cap=30%）成交笔中"拟参与率 >5%"占比 >10% → 回测存在**系统性流动性幻觉**（大量订单在现实中买不满）。
2. **量级判据**：乐观偏差量级 = **强制缩量重放（cap=3%）与 base（cap=30%）的 Δret（base − cap003）、ΔMDD、Δtrades**。
   - Δret ≤ 3pp → 幻觉可忽略（流动性约束对结果影响小）
   - 3pp < Δret ≤ 10pp → 中等乐观偏差，回测收益应打折披露
   - **Δret > 10pp → 严重**：回测收益显著被流动性幻觉高估
3. **辅助证据**：cap=3% 重放后 trade_count 大幅下降（>30%）或 cash_mean 大幅上升 → 印证"大量拟成交在现实中买不满（买不满就少买/放弃）"。
4. **不对称判定（P0 候选）**：若实盘 `trader.py` 缺少回测具备的参与率约束/冲击成本 → 单独标红一节。
5. 结论必须给量级；无法精确时给上下界（如 base 分布 max 在 30% 处截断 → 报"≥30%"下界）。

---

## §1 读码事实表：撮合规则全景（回测 vs 实盘，行号引用）

### 1.1 买卖价 / 涨跌停 / T+1 / 停牌 / 参与率

| 规则 | 回测 `app/engine.py`（代码原文≤5行） | 实盘 `app/trader.py`（代码原文≤5行） | 对称？ |
|---|---|---|---|
| 买入成交价 | `open_px = bar["open"]`（L672）；`px = open_px*(1+self.slippage)`（L680）→ **开盘价**（集合竞价跳空用 open 非 vwap/close） | `px = self._buy_px(q, b["price"], "buy", ...)`（L736）→ 盘中现价 | 语义对应（回测=开盘近似） |
| 卖出成交价 | `px = base_px*(1-self.slippage)` + execution.execute_price(impact=True) + 涨跌停钳制（L446-465） | `_sell_px` → `_buy_px`（L423-425, 390-403）：市值分档+impact（**P59 观察期：现采用旧口径 amount=price*1000 / impact=False**，L416-417 `BUY_PX_USE_NEW=False`） | **部分不对称**：回测恒用真实量+impact，实盘现行口径固定 1000 股+无 impact |
| 涨停买不进 | `if open_px >= lu*0.999 or open_px <= ld*1.001: continue`（L678） | `if pct >= C.LIMIT_UP_PCT...: continue`（L586）；`if px >= lu*0.999: continue`（L740） | ✓ |
| 跌停卖不出 | `if (_ld48>0 and bar["low"]<=_ld48*1.001 and cp<=_ld48*1.001): continue`（L488-490） | `if cp <= ld*1.001: self._event("跌停卖不出，跳过"); continue`（L1339-1342） | ✓ |
| 一字板判定 | 开盘=涨停 → 不成交（L678 覆盖）；封板排队概率 `queue_fill_prob`（L739-745） | 无显式一字板分支；现价≥lu 跳过（L740 覆盖开盘一字）；**盘中封板无排队概率模拟** | 回测更细 |
| T+1 | `if pos["days"] < 1: continue`（L480-481） | `if entry_date >= df._today_str(): return False,"T+1"`（L461-462） | ✓ |
| 停牌 | `ex.check_suspend(...)` → volume≤0 拦截（L726-731, execution.L77-78） | `if not q.get("price"): continue  # 无行情（停牌）`（L1222-1223） | ✓ |
| **参与率上限** | **有**：`ratio=(px*qty)/day_amt; if ratio>C.EXEC_PARTIAL_CAP(0.30): fill_pct=C.EXEC_PARTIAL_CAP/ratio; qty=int(qty*fill_pct/100)*100`（L746-755） | **无**：`qty=int(pos_cash/px/100)*100`（L742）**买满不缩量** | **不对称（P0 候选）** |
| 冲击成本 | `execute_price(..., day_amount=bar.amount, impact=True)`（L734-737）；amount/day>5% → +0.2%（execution.L90-91） | 现行旧口径 impact=False（L392）；新口径（impact=True）未启用（L417） | 回测更严 |
| 手续费 | `buy_fee/sell_fee` 佣金万2.5+印花0.05%（L54-60） | 同 `eng.buy_fee/sell_fee`（L432/468） | ✓ |

### 1.2 关键补充事实

1. **参与率上限门控缺陷**：L746-755 的部分成交缩量在 `if self.exec_prob:`（L739）块内执行，**`self.exec_partial`（L132）从未被读取**——config `EXEC_PARTIAL_FILL` 开关是死配置；`EXEC_PROB_MODEL=True` 时才生效。**但现役 base 运行 `exec_prob=True`（config 默认）→ 30% 参与率上限在现役回测中确实生效**（任务书"流动性参与率无约束"的表述需修正：有约束，但 0.30 远松于 1/3/5% 现实档）。
2. **queue_fill_prob 惰化**：engine L741 传 `quote={"fund": 0, "float_mktcap": mcap}`——`fund` 硬编码 0 → execution.L49 `if fund>0` 永不触发 → 封单强度逻辑完全惰化；`turnover` 也不在 quote 中（L46=0）→ "高换手炸板"修正（L57-58）在回测永不触发。**贴板成交概率实际=0.5 固定基线**（L47）。
3. **买盘价格锚点**：回测用 `open`（集合竞价开盘价）作为成交基准，叠加市值分档滑点。一字板（开盘即涨停封死）在 L678 被拦 → 无法成交（真实）。开盘未封死、盘中封板的票按开盘价成交（真实，若开盘能买到）。
4. **实盘流动性前置过滤**：实盘 score 路径 `if (q.get("amount")<8e7 and code not in focus): continue`（L588）、board 路径 `<5e7`（L857）——**实盘有"当日成交额≥8000万"的流动性前置门槛，回测股票池无此门槛**（回测池=bt_pool/PIT 池按市值/热度筛选）。这是方向相反的"实盘更严"不对称。

---

## §2 turnover:0.0 假零排查（grep 全项目）

**假零注入点**：`app/engine.py` L429 `"low": bar["low"], "volume": bar["volume"], "turnover": 0.0`（board 信号 q）；L437-438（twothirty 信号 q 同含 `vol_ratio: 0.0`）。

**被谁消费（全项目 grep 结果）**：

| 消费点 | 读法 | 回测路径是否命中假零 | 影响 |
|---|---|---|---|
| `scoring.py` L431 `turnover = quote.get("turnover",0)`；L449-450 `5<=turnover<=15:+15 换手适中` / `3~5或15~20:+10` | score_board | **命中**（engine L429 传 0.0） | **换手加分（+15/+10）在回测 board 评分中永不触发**，最多丢 15 分/信号 |
| `execution.py` L46,57 `if turnover>25: prob-=0.1`（高换手炸板） | queue_fill_prob | 命中（quote 无 turnover→0） | 回测贴板成交概率不含高换手惩罚（但 fund=0 已惰化，见 §1.2-2） |
| `trader.py` L600 `to=q.get("turnover",0)`（实盘 _rough） | 实盘行情 | 不命中（实盘有真字段） | 无影响 |
| `trader.py` L1552 / `ai.py` L230,253 / `server.py` L299 / `datafeed.py` / `tdx.py` / `moneyflow.py` | 实盘/展示 | 不命中 | 无影响 |

**结论**：假零被消费点 = **`scoring.score_board`（回测 board 策略换手加分静默失效）** + `execution.queue_fill_prob`（已被 fund=0 惰化，叠加无影响）。score 策略（本审计对象 base_score）**不读 turnover**，不受假零影响。

---

## §3 方法与数据

- 基线：`data/bt_sizing_lab.json` → `matrix.s0["2023-24"]`：**ret=-0.4829, mdd=-0.5211, trade_count=1228, n_codes=426（PIT）**（该 JSON 未存 trades 明细 → 本审计的分布统计从本工具 base 重放获取，口径 2023-24 单窗，偏差已在 §4 说明）。
- 重放工具：`tools/fill_reality_lab.py`（继承 R1 工程骨架：OUT 路径断言 / 逐格增量落盘 / 幂等续跑 / workers≤2 / ProcessPool 断裂回退；monkey-patch `config.EXEC_PARTIAL_CAP`，**零生产改动、不改 app/*.py**、快照 mode=ro 只读、不出网）。
- 两格（2023-24 窗 × S0，seed 42 相同 → 差异只来自参与率上限）：
  - **base**：cap=0.30（现役默认）→ 校验应复现 sizing_lab S0（误差 >1pp 需解释）
  - **cap003**：cap=0.03（参与率≤3% 强制缩量，买不满就少买不补单）
- 参与率分母 = 当日成交额（kline.amount）；分子 = 成交笔 amount。

---

## §4 结果（2026-09-01 11:27 重放完成）

### 4.1 重放两格（2023-24 窗 × S0，seed 42，差异仅 EXEC_PARTIAL_CAP）

| 格 | cap | total_return | max_drawdown | trade_count | n_buys | failed_fills | cash_mean | elapsed |
|---|---|---|---|---|---|---|---|---|
| base | 0.30 | **-0.4829** | **-0.5211** | 1228 | 529 | 1 | 30878.01 | 612s |
| cap003 | 0.03 | **-0.4829** | **-0.5211** | 1228 | 529 | 1 | 30878.01 | 574s |

- **Δret = 0.0pp，ΔMDD = 0.0pp，Δtrades = 0，Δcash = 0.0** —— cap 从 30% 收紧到 3% **完全无效**。
- **基准确认**：base 复现 bt_sizing_lab S0 2023-24（-0.4829/-0.5211/1228 笔/426 池）**0.0pp 误差** → 环境与口径与 W3 一致，重放可信。

### 4.2 参与率分布（base 格 529 笔买入）

| 指标 | 值 |
|---|---|
| n_ratios | 529（均有当日 amount 分母） |
| P50 | 0.0% |
| P90 | 0.01% |
| P95 | 0.01% |
| **max** | **0.02%** |
| >1% | **0.0%** |
| >3% | 0.0% |
| >5% | 0.0% |
| 截断于 30% cap | 0 笔（30% cap 从未触发；failfill=1 来自 queue_fill_prob，非 cap） |

**分布判据结论：拟参与率 >5% 的成交笔占比 = 0% < 10% 阈值 → 本池/本仓位下不存在系统性流动性幻觉。**

### 4.3 为什么是 null：池流动性画像（PIT 2023-24 池，498 只有效股）

- 每股中位日成交额：**min=3366万，p10=1.03亿，p50=2.94亿** —— 池内全是中大盘流动性充裕标的。
- 单笔 ~30k 订单参与率触发阈值反推（30k/X）：>1% 需成交额 <300万、>3% 需 <100万、>5% 需 <60万、>30% 需 <10万 → **池内 0/498 只股满足任一时点**。
- 实际下单量更小（risk_parity 权重 + 各类乘数缩仓），max 实际参与率 0.02%。

---

## §5 结论（量级 + 边界）

1. **乐观偏差量级 ≈ 0 pp**：任务书假设的"流动性参与率无约束 → 回测收益被幻觉高估"在本策略（3×0.30 × base_score）× 本池（PIT top500 流动性筛选）上**不成立**——不是引擎撮合问题，是池构造的副产品（池里没有小票）。
2. **边界条件（何时会绑定）**：若股票池扩展至中位日成交额 <3000万 的小盘股（如全市场小盘池），30k 订单参与率将超 1%，现行 30% cap 会绑定并可能带来乐观偏差；<60万 成交额时超 5%（>10% 成交笔占比的判据阈值可能触发）。**该结论不可外推到小盘池**。
3. **其他真实问题（独立于 null 结果）**：
   - **turnover 假零**：`score_board` 读 `quote["turnover"]`（L431,449-450）换手加分（+15/+10）在回测 board 策略**永不触发** → board 回测评分系统性偏低、与实盘口径不一致（§2 详表）。
   - **queue_fill_prob 惰化**：engine L741 `fund=0` 硬编码 → 封单强度逻辑永不执行；贴板成交概率 = 0.5 固定基线（含 0.1 高换手惩罚也不触发）。
   - **一字板语义**：开盘=涨停 → 不成交（L678，真实）；开盘未封死按 open 成交；贴板（距涨停 <0.5%）走 queue_fill_prob 0.5 概率。

### §5.1 回测/实盘不对称（P0 候选，标黄——本池下实际影响≈0，扩展池后升为真 P0）

| 不对称 | 回测 | 实盘 | 影响 |
|---|---|---|---|
| 参与率缩量 | 有（cap=0.30，L746-755） | **无**（L742 买满） | 本池下 cap 不绑定→影响≈0；方向=实盘更乐观 |
| 冲击成本 | 恒 impact=True（真实量+当日额） | 现行旧口径 impact=False（P59 观察期，L392/416-417） | 本池下 amount/day<5% 永不触发 impact→影响≈0；方向=实盘更乐观 |
| 流动性前置过滤 | 无（靠池构造隐含） | **amount≥8000万**（score L588）/ ≥5000万（board L857） | 方向=实盘更严（非乐观偏差） |
| 涨停/跌停/T+1/停牌 | L678/L488/L480/L726 | L586,L740/L1339/L461/L1222 | 对称 ✓ |

**结论**：当前无"实盘比回测更乐观到足以改变结论"的不对称（P0 未触发）；但**参与率缩量与 impact 两项不对称在池扩展到小盘股后即为真实 P0**（实盘会比回测更乐观地成交小票大单），建议随池扩展同步启用 `BUY_PX_USE_NEW=True` 并给实盘加参与率缩量。

---

## §6 修复建议（提案级，不实施）

1. **turnover 假零补真**：engine L429 board 信号 q 的 `"turnover": 0.0` → 用 `bar.get("amount",0)/...` 换算真实换手（当日成交额/流通市值，需 mcap），或标注"回测无换手→换手加分不计"并在 score_board 显式跳过（当前是静默 0 分）。最小改动：`score_board` 对 `turnover<=0` 时跳过换手加分并记入 signals"回测无换手数据"。
2. **queue_fill_prob 数据化**：engine L741 `quote={"fund": 0, ...}` → fund 从当日封单/涨停池真实数据（回测可从"涨停且未炸板"的 bar 推断封单代理量），否则删掉惰化分支、明确 0.5 基线为"无数据默认"并在报告注明。
3. **参与率参数化（供小盘池场景）**：`EXEC_PARTIAL_CAP` 已存在，仅需在池扩展时收紧（0.30→0.03/0.05）并开启实盘侧同款缩量；建议实盘 `_buy` 前加 `if px*qty > day_amount*CAP: qty=int(CAP*day_amount/px/100)*100` 的对称检查。
4. **一字板语义补法**：现逻辑只判"开盘=涨停"，建议补"全天封死"判定（open==high==low==close==lu）→ 直接不成交（现已被开盘涨停拦截覆盖，语义上已满足）；对"开盘未封、盘中封板"维持 open 成交（真实）。
5. **启用实盘新口径**：`BUY_PX_USE_NEW=True`（真实委托量+impact）使实盘与回测执行约束一致（P59 观察期结束条件由验收方定）。

---

## §7 资源占用注记

- 重放工具 `tools/fill_reality_lab.py`，2 格并行（ProcessPool workers=2），每格 574-612s，总墙钟 ~10min；失败填充 1 次（queue_fill_prob）。
- 数据产物：`data/bt_fill_reality.json`（results: base/cap003/delta + participation 分布）。
- 验证脚本：`tmp/y3/verify_amount.py`（amount 单位=元，均价≈close）、`tmp/y3/pool_liq2.py`（池流动性画像）、`tmp/y3/find_trader.py`。
- 零生产改动：未改 app/*.py；monkey-patch 仅运行时 `config.EXEC_PARTIAL_CAP`；快照 mode=ro 只读；未出网；未 git。

---

## §8 交付物与红线核对

- 报告：`docs/reports/fill_reality_audit.md`（本文件）
- 数据：`data/bt_fill_reality.json`
- 工具：`tools/fill_reality_lab.py`
- 日志/脚本：`tmp/y3/fill_reality_111718.log`、`tmp/y3/verify_amount.py`、`tmp/y3/pool_liq2.py`、`tmp/y3/find_trader.py`
- 红线核对：零生产改动 ✓（只 monkey-patch config）；未重启/杀进程 ✓；未 git ✓；未出网 ✓；快照只读 ✓；写入面=预注册三件+tmp/y3 ✓；预注册先行落盘（§0 于 11:17 前落盘，首格结果 11:27 才出）✓。
- 偏差说明：任务书"用 bt_sizing_lab s0 四窗 trades 列表"——该 JSON 未存 trades 明细，改为从 base 重放（2023-24 单窗 529 笔买入）取分布，口径一致、单窗样本充分；分布判据按预注册 §0 执行。
