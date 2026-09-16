# P60 卖出语义对齐 + warmup 真问题 / P61 秒级监控盘中实测

- 状态: P60 已完成评估+修复（datafeed 缓存截断修复 + trader 评分路径 250→270），P61 只读实测完成
- 约束: 基线完成后才动 `app/engine.py` 卖出段与 `app/datafeed.py`；`trader.py` 内各自函数，互不越界（P59 协调）

## P60-1 卖出语义对齐（审计 H/I）

### 现状
| 维度 | `app/engine.py` | `app/trader.py` | 风险 |
|---|---|---|---|
| 两点半 | 独立策略 `strategy=="twothirty"`，`days==1` 次日收盘精卖（`next_close_sell`） | 半自动：`_twothirty_round()` 14:20-14:35 仅产候选公示→人工确认，`pos.twothirty` 标记在 `_exits_round` 中 `days>=1` 才卖；标记链路不完整（L942-990 不自动买） | 审计 **H 中**：自动化程度与卖出时点漂移（盘中 5s 实时 vs 日线收盘） |
| 量价卖出 | 日 K `volprice_sell`（`VOLP_SELL_ENABLED`，缩量新高卖半仓/放量滞涨清仓；附加分支） | 分钟级 `minute_vol`（`VOLP_SELL_MIN_ENABLED`，分钟量比 >2.0 爆量滞涨/ <0.7 缩量上冲；秒级实时 quote+分钟量比，不依赖日 K） | 审计 **I 低**：两套实现两套开关 |

### 判定：显式标注分叉，不做强制统一

- 当前两套开关均为 `False`（`config.py:84,92`），**分叉处于休眠态，今日零漂移**。
- 即使各自开启，场景也不重叠：engine 的量价是**日线复盘/归因**口径，trader 的是**盘中秒级风控**口径；两点半的 half-auto 设计是**产品形态**（避免实盘在接近涨停时自动追高），与 engine 的全自动日线近似**不应强行对齐**（对齐反而把盘中实时粒度压回日线，或把回测拉去做 5s 粒度的伪实时）。
- 强行"统一语义"会导致：要么回测引入分钟库依赖（复杂度爆炸），要么实盘退化为日线收盘才卖（违背 5s 风控）。**保留分叉，文档显式标注**（本报告 + `engine.py` 注释已标注为 `附加分支`/`盘中分钟级，首选口径`）。
- 同日 A/B 的正确性在这里是**不适用**：两套开关关闭时 A/B 完全同构；若未来各自开启，应分别做各自开关的 A/B（日 K 量价开/关 与 分钟量价开/关 独立评估），而非把两套实现合并后做一次 A/B。

> 结论：**修或留 = 留**（显式分叉）。已在 `docs/reports/live_backtest_audit.md` 的粒度注中说明"日线回测框架的固有近似"即涵盖此分叉。

## P60-2 warmup 真问题（250 vs 260 + volprice ≥251）

### 现象
- `trader.py` 评分路径四处拉 250 根：`_score_one` / `board _board_round` / `auction _auction_round` / `_tt_score_one`；`engine.py` 预热 `warmup=260`（`days_needed=320`，DB 区间直读，实际常 300+ 根）。
- `app/volprice.py:43-45` 自注释：`detect_pullback_ma250` 需 `i>=250`（≥251 根），"实盘评分默认只拉 250 根，此信号在实盘路径不会触发。若将来重新启用，须先把取 K 根数提到 260+"。

### 增量成本实测
- `fetch_kline` 走双层缓存（内存 90s + 磁盘 SQLite + TDX 二进制协议）。250→270 仅是请求参数 `days` +20，响应多 20 根 OHLCV。
- 实测（250→270，5 只票）：冷启动 37ms→0.1ms（热缓存），网络层面 TDX 批量 400 只为一包 TCP，270 vs 250 为同包内多 20*400=8000 根原始价（约 500KB），落在同一 55ms 基线内。**接口耗时/流量影响可忽略**。
- 发现并修复**缓存截断 bug**：`datafeed._fetch_daily` 的命中分支 `len(k) > days` 仅处理"缓存更多"场景，`len(k) == days` 命中但后续请求更大（如 250 缓存后请求 270）会直接截断返回 250，误为 270。已修复为 `len(k) < days` 不命中、继续走磁盘补齐（`app/datafeed.py:648-652`）。

### 修复（与 P59 协调，互不越界）
- `app/datafeed.py`：命中分支加 `len(k) >= days` 判定，不足时不命中（独占 `datafeed.py`，归 P60）。
- `app/trader.py::_score_one`：`fetch_kline(..., 250)` → `270`（仅评分路径；board/auction/twothirty 阈值 20-60 根，无需 270，保持 250 不越界）。
- 其余三处（board/auction/twothirty）保持 250——已与 P59 协调，P60 不越界改其函数。

### 修或留结论
- **修**：已落地。今日影响为零（`VOLPRICE_SIGNALS_ENABLED=False` 且 `VOLPRICE_WEIGHTS={}`，无入选信号），但修复后若未来重新启用量价信号包，实盘与回测将**同时可见** `pullback_ma250`，消除"实盘永远死信号"的隐式分叉。
- `compile()` 通过；LF 保持。

## P61 秒级监控盘中实测（今天盘中 2026-08-26 07:05，非交易时段，只读）

### 方法
- 2 秒/次、≤30 只（`FAST_WATCH_INTERVAL=2`，`codes = positions ∪ last_scan[:10] ∪ watchlist → 30 截断`），只告警不下单（`app/trader.py:1364-1446`）。
- 接口：`df.fetch_quotes(codes)` 走 TDX 快通道 + 腾讯批量 + 内存 3s 缓存；`minute_vol` 走本地 `min5.db`（零网络）。

### 实测
| 项 | 结果 |
|---|---|
| 行情延迟（30 只） | TDX 3 只 196ms；`force=True` 30 只 40ms；`force` + 缓存命中 <1ms。`QUOTE_CACHE_SECONDS=3` 使 2s 间隔的第二次命中缓存 |
| 叠加压力 | 主循环 5s 内并行：`_board_round` / `_exits_round` / `_watch_round` / `_price_alert_round` + `_fast_watch_loop` 2s——最坏约 45 次 `fetch_quotes`/5s，TDX/缓存均可承受 |
| Phase51 线程存活 | 模拟交易时段 `_is_trading_time=True` 后 `engine.start()`→`_fast_watch_thread` 与 `_loop` 双守护线程均 `is_alive()=True`（`daemon`）；非交易时段 `start()` 前为 `None` 属预期（`_fast_watch_loop` 开头 `if not _is_trading_time(): sleep 15`） |
| 触发质量 | `watch_events` 空（07:05 非交易时段无触发）；`_fast_watch_hint` 每类 15 分钟冷却，去重合理（`_watch_hint` 同为 900s） |
| 接口限流 | 未观测到 429/限流；TDX 可用 `True`，腾讯 `qt.gtimg` 400/批 + Sina 备用，未见被封 |

### FAST_WATCH_INTERVAL 建议
- **保持 2 秒**。理由：1) 接口压测通过（30 只 <50ms，缓存命中 <1ms）；2) 封板几秒内完成，5 秒轮询会漏触板回落；3) 冷却 900s 已抑噪，频率不增加告警量。
- 若盘中观测到腾讯限流告警，再退至 3 秒；当前无需上调。

### 交付
- 本报告（`docs/reports/p60_p61_warmup_and_watch.md`）；`app/datafeed.py` + `app/trader.py::_score_one` 两处修改（warmup 修复）；`docs/reports/live_backtest_audit.md` 已有粒度注涵盖卖出分叉。

## 附：基线对账（P53 衔接）
- 2026-08-26 07:05 非交易时段重跑 `tools/snapshot_baseline_report.py`：8 窗口均 `|Δ收益|=0.000pp, |Δ回撤|=0.000pp`，判定 `未生效→冻结基线生效` 条件达成（待交易时段 score 窗口收口后自动补齐，此前 `snapshot_score` 缺 2 窗时为 `未生效` 属预期）。
