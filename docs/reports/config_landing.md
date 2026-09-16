# Phase 47+50+51：建议落地批处理、auction 抽样复核、持仓票提频监控

- 时间: 2026-08-25 深夜 ~ 08-26（本报告由本智能体撰写；期间另一并行 AI 完成了
  qfq 修复/引擎收敛验证并调整了一项开关，见 §2.2）
- 授权改动范围核验：仅 app/config.py、app/trader.py、tools/data 新增文件；✅

## 1. 落地项 a：STOP_LOSS_PCT -0.05 → -0.07 ✅ 已落地并回归验证

- config.py 已改（含 Phase36 依据与日期注释）
- 同日 A/B 回归（bt_pool top500 / fetch_quotes 置空 / score 25/3/0.30/0.001）：

| 窗口 | pre(-0.05) | post(-0.07) | Δ |
|---|---|---|---|
| W1 | −27.84% | −19.09% | **+8.75** |
| W2 | −41.06% | −37.34% | **+3.72** |
| W3 | −36.38% | −34.55% | **+1.83** |
| W4(牛) | −1.12% | +20.22% | **+21.34** |

4/4 窗口改善、牛市 +21.34pp、平均 +8.91pp —— 与 Phase36 扫描结论同向，
落地有效。（首轮回归曾因 worker 未 monkeypatch `C.STOP_LOSS_PCT` 得到假 Δ=0，
已修复重跑；engine 无同名 params 支持，属已知陷阱。）

## 2. 落地项 b：指数择时 board 专属 —— 代码接线完成，开关被并行任务置 False

### 2.1 本智能体完成的部分
- config 新增 `INDEX_TIMING_BOARD_ONLY = True`（全局 `INDEX_TIMING_ENABLED`
  维持 False，score 路径零影响）
- trader.py `_board_round` 接线：`timing_multiplier()` 只作用于打板开仓预算
  （pool/per_stock 只缩不扩，异常降级不拦截）
- 回测等价口径回归（params.index_timing A/B）**连续两天显示不稳定**：
  - 8/25 数据态：board 牛市窗口 Δ=−22.84pp
  - 8/26 凌晨数据态：Δ=−17.57pp
  - 均与 Phase43 判定日的 +1.05pp 方向相反——前复权日间重锚定使该结论
    在数据态间翻转

### 2.2 并行 AI 的处置（尊重其 config 写权限）
- `INDEX_TIMING_BOARD_ONLY` 被改为 **False**（当前生效状态）
- 其同步产出了 qfq 修复进度、引擎收敛对照（data/bt_conv_before/after.json、
  bt_engine_convergence.json）等工件，与本智能体的漂移发现互相印证：
  **数据锚点未治理前，指数择时的落地收益不可稳定复现**
- 最终裁定权在验收方：代码接线保留（开关一拨即用），开启时机建议等
  复权锚点治理完成后再按 Phase43 范式复验一次

## 3. Phase51：持仓票高频监控线程 ✅

- trader.py `start()` 启动守护线程 `_fast_watch_loop`（主循环全池 5 秒轮询不变）
- 监控集 = 持仓 ∪ 自选 ∪ 最近扫描候选（≤30 只），默认 2 秒/次
 （`config.FAST_WATCH_INTERVAL`，接口限流实测后再定终频）
- 检测：a) 触板后回落 ≥BOARD_BREAK_DROP；b) 持仓票移动止损 /
  冲高回落条件（复用 pos.peak 与既有阈值常量）
- 只产出告警（watch_events + `_notify`），不自动下单；每票每类 15 分钟冷却；
  异常完全隔离（try/except 风格与 _loop 一致）

## 4. auction_intraday 抽样逐笔回对 ✅（Phase38 保留意见处置）

- 工具: tools/auction_verify.py；结果: data/attribution/auction_verify.json
- 重跑 min4.0 变体取得全部 386 笔买入，等距抽样 40 笔回对当日 min5 分时：

| 核查项 | 违规数 | 说明 |
|---|---|---|
| C1 一字板漏拦 | 0 | 引擎拦截有效 |
| C2 无对手价（bar 零成交） | 0 | — |
| C3 买入价越出 bar [low,high] 区间 | **6（15%）** | 回测价在该 bar 未出现过 |
| C4 买入 bar 即封死 | 0 | — |

**结论：85% 抽样交易在真实排队约束下可成交**。+389.7% 的成交可行性大体成立，
但 ①15% 价格越界需打折、②单牛市窗、③生存者偏差池三项保留仍然成立；
min4.0 参数维持"报告推荐、暂不落 config"。

## 5. 交付物清单

| 文件 | 说明 |
|---|---|
| app/config.py | STOP=-0.07、INDEX_TIMING_BOARD_ONLY（现为 False，见 §2.2）、FAST_WATCH_INTERVAL=2 |
| app/trader.py | board 择时接线 + 高频监控线程（最小改动，compile 通过） |
| tools/auction_verify.py / data/attribution/auction_verify.json | 抽样回对工具与结果 |
| tools/config_landing_regression.py / data/bt_config_landing_regression.json | 同日 A/B 回归 |
| docs/reports/config_landing.md | 本报告 |
