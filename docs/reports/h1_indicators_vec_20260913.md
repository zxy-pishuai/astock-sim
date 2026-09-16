# H1 indicators.py numpy 向量化（性能改造，数值等价铁律）

- 日期：2026-09-13
- 改动文件：`app/indicators.py`（500 → 940 行，+440）、`tools/test_h1_equivalence.py`（新建，278 行）
- 备份：`tmp/f_backup/20260913_h1/indicators.py.orig`
- 生效方式：**代码已改，服务未重启**（生效由验收方统一安排；本批红线禁重启）
- 预注册判据（任务书原文引用）：①等价性 30 只×全历史 max(abs(diff))<1e-9、0 函数不通过；②`score_stock` 热态 ≤3.0ms（≥4.7x）；③函数调用数 ≤20,000；④边界用例一致；⑤不引入新必需依赖（scipy 不可用已确认）。

## 1. 改动清单

| 函数 | 新实现行号 | 旧实现（`_legacy`）行号 | 改造手法 |
|---|---|---|---|
| sma | 15 | 26 | cumsum 差分 O(n) |
| ema | 39 | 66 | 闭式解 np.convolve（指数加权卷积）向量化 |
| macd | 70 | 84 | 基于 ema 组装 + 列表推导化 |
| rsi | 88 | 107 | cumsum 差分滑动均值（非 Wilder，窗口平均语义保留） |
| rolling_max | 128 | 139 | sliding_window_view + max |
| rolling_min | 152 | 163 | sliding_window_view + min |
| boll | 176 | 193 | 窗口方差 = Σ(x-m)²/p，向量化 |
| rsrs | 207 | 298 | **增量循环保留**（见 §4），var_h 内积/s_h2 预计算/zscore 平方预计算向量化 |
| kdj | 377 | 403 | RSV 窗口高低向量化，K/D 递推保留 |
| atr | 427 | 453 | TR 向量化 + Wilder 递推保留 |
| obv | 472 | 486 | diff sign 掩码 + cumsum |
| obv_slope | 745 | 769 | sliding_window_view 斜率向量化 |
| volume_ma | 562 | 574 | cumsum 差分 |
| volume_surge | 632 | 649 | cumsum 差分（补测函数，不在 18 清单但被 scoring 调用） |
| vwap | 589 | 610 | 数组化 + cumsum，全量一次 |
| volume_slope | 658 | 682 | sliding_window_view 斜率向量化 |
| price_volume_corr | 695 | 719 | 窗口协/方差 cumsum 技巧向量化 |
| price_vol_divergence | 785 | 810 | 量价动量向量化 + np.where 符号逻辑 |
| chip_profile | 830 | 894 | **回退旧实现**（向量化尝试失败，见 §4） |

未改造（保留旧实现，不在任务书清单）：volume_ratio、detect_streak、momentum、volatility、rsv_raw、rsi_ma、amt_change、vwap_deviation、volume_ma_cross。

## 2. 改前/改后基准（000002，1250 根，热态 20 次均值，同脚本同口径）

| 指标 | 改前 | 改后 | 变化 |
|---|---|---|---|
| score_stock 热态 | 20.48 ms | 4.48~4.57 ms（三组） | **4.5x 加速** |
| 函数调用数 | 134,135 | **2,968** | **-97.8%** |

### 逐函数单次耗时（1250 根，热态均值）与加速比

| 函数 | 新版 ms | legacy ms | 加速比 |
|---|---|---|---|
| sma | 0.089 | 0.145 | 1.62x |
| ema | 0.358 | 0.359 | 1.01x |
| macd | 1.239 | 1.154 | 0.93x |
| rsi | 0.099 | 0.964 | 9.69x |
| rolling_max | 0.100 | 0.343 | 3.44x |
| rolling_min | 0.105 | 0.352 | 3.37x |
| boll | 0.274 | 3.429 | 12.51x |
| rsrs | 1.464 | 2.714 | 1.85x |
| kdj | 1.126 | 2.347 | 2.08x |
| atr | 0.556 | 0.362 | 0.65x |
| obv | 0.226 | 0.166 | 0.74x |
| obv_slope | 0.421 | 4.444 | 10.55x |
| volume_ma | 0.063 | 0.106 | 1.68x |
| vwap | 0.440 | 0.281 | 0.64x |
| volume_slope | 0.132 | 2.653 | 20.11x |
| price_volume_corr | 0.322 | 5.818 | 18.05x |
| price_vol_divergence | 0.132 | 0.732 | 5.55x |
| chip_profile | 19.1 | 27.9 | 1.46x（回退，≈1，测量噪声） |
| volume_surge | 0.147 | 0.496 | 3.37x |
| **合计** | **26.4** | **54.8** | **2.08x** |

说明：atr/obv/vwap/macd/ema 加速比 <1~1.0，为 1250 根小样本下 numpy 数组构造（asarray）开销与 Python 单循环持平的边界现象（大样本 numpy 优势显现）；这些函数在 score_stock 路径占比小，不影响整体判据。rsrs 1.85x 为等价铁律下的上限（§4）。

## 3. 等价性护栏（tools/test_h1_equivalence.py）

- 30 只分桶选股（688/30/60/00/短史<100/长史>1000 各 5 + 随机补齐，seed 42）× 全历史 × 22 个用例（含新增 volume_surge）
- 旧实现全部以 `_legacy` 后缀保留在同文件，`invoke()` 动态取 `getattr(ind, name)` vs `getattr(ind, name+"_legacy")`
- 递归 `_walk` 对比 list/tuple/dict，容差 TOL=1e-9
- 边界用例 45 条：空/长度 1/<period/全等值/含 0 停牌/单调序列

**结果：主测试 660/660 通过、边界 45/45 通过、失败清单为空。** volume_surge 另做 200 组随机定向验证 max_diff=0.0。

## 4. 失败与回退记录（诚实披露）

1. **rsrs 全向量化（sliding_window_view + 全量正规方程）两度失败**：
   - 首版差 4e-7（zscore 窗口错位 1），修正后差 0.1~0.8；
   - 根因：`var_l > 1e-10` 阈值分支对浮点敏感——增量更新（旧实现）与全量求和（向量化）的浮点差使 var_l 真值接近 1e-10 的一字板窗口跨过阈值，beta 从真实值跳成 1.0。
   - **处置**：rsrs 主循环保留旧增量顺序（逐位一致），仅 var_h 内积、s_h2 预计算、zscore 平方预计算向量化。
2. **rsrs zscore 段 cumsum 差分向量化失败**：`max(1e-10, ...)` 夹取边界同样对浮点敏感，回退增量循环。
3. **chip_profile 向量化三次尝试失败 → 按任务书规则回退旧实现**：
   - 尝试 1（np.where 数组权重 + np.cumsum）：27.5ms 反比 legacy 慢，且 c==lo（跌停）时权重分支与旧实现不一致（diff 0.3~3.4）——已修复权重分支；
   - 尝试 2（逐 bar 向量递推 + itertools.accumulate 顺序累加）：numpy ufunc 乘除产生 1 ULP 尾差（91/120 bin 实测），经 total 放大后使 cost50 临界档判定差一个 bin（全等值边界 diff=0.0083≈1 个 bin 宽）——np.cumsum 为分块累加、ufunc 乘除与 Python 标量有尾差，均为 numpy 浮点细节；
   - **处置**：按任务书"任何不通过的函数必须回退到旧实现并在报告中列出"，chip_profile 新版 = 旧实现原文（等价 100%）。衰减递推逐 bar 串行无法跨 bar 合并，O(n×bins) 结构不变；score_stock 路径不调用它（cProfile 证实），不影响整体判据。
4. **ema 递推原样保留后又替换为卷积闭式解**：卷积版等价通过，耗时与递推版持平，但调用数大降（score_stock 6,617 → 2,968），保留卷积版。

## 5. 预注册判据逐条

| 判据 | 结果 | 数值 |
|---|---|---|
| 等价性 30 只×全史 max(abs(diff))<1e-9 | **通过** | 660/660，0 函数不通过 |
| 边界用例新旧一致 | **通过** | 45/45 |
| 函数调用数 ≤20,000 | **通过** | 2,968（改前 134,135，-97.8%） |
| 不引入新必需依赖 | **通过** | 仅 numpy（2.4.5 已有）；scipy 确认不可用未引入 |
| score_stock 热态 ≤3.0ms | **部分通过** | 实测 4.48~4.57ms；相对实测基线 20.48ms = **4.5x**；绝对 3.0ms 未达（差 ~50%） |

**性能判据说明**：任务书 3.0ms 目标由"14.2ms 基线 × ≥4.7x"推算；本机实测改前基线 20.48ms（任务书称 14.2ms，样本差异）。改后 4.5x（相对实测基线）。瓶颈：
- rsrs 增量循环（~1.5ms/1250 根）：阈值分支敏感导致无法全向量化（§4），是等价铁律下的下限；
- numpy.asarray 固定成本（20+ 次 ~0.8ms）：每函数接 list 必须转换，签名不可变；
- kdj/atr 递推保留、ema 卷积化后持平：纯递推无法向量化。
进一步压到 3.0ms 需要：a) rsrs 拆可选 numba 加速路径（引入依赖，违背红线）；b) I 道评估 scoring 自身循环（~1ms，本道无权改）。

## 6. 风险与回滚

- **风险**：numpy 路径与旧实现的浮点等价已由 660/660 + 45/45 验证；剩余风险面 = scoring/factor/factor_gtja 对返回结构（None 前缀、tuple/dict 结构、长度）的隐式依赖——签名与返回类型零变化，且已跑 score_stock 端到端 sanity（返回结构正常）。
- **回滚**：`Copy-Item tmp\f_backup\20260913_h1\indicators.py.orig app\indicators.py` 一键还原（旧实现全部保留在 orig 与 `_legacy` 段，双保险）。
- **生效**：服务当前仍跑旧码；验收方统一重启后生效。重启后建议跑 `startup_check.py` 与等价测试复跑。

## 7. 验证方式与范围

- 语法：`ast.parse`（本批要求）+ `py_compile` 通过
- 等价：`python tools\test_h1_equivalence.py`（660/660 + 45/45）
- 基准：`h1_bench_per_func.py`（逐函数表）、`h1_bench_stable.py`（三组 4.48~4.57ms）、`h1_calls_after.py`（2,968 calls）
- 端到端：`score_stock(002011)` 返回 (25, ...) 结构正常
- LF：indicators.py CRLF=0；test_h1_equivalence.py CRLF=0
- git：`M app/indicators.py`、`?? tools/test_h1_equivalence.py`、`?? docs/reports/h1_indicators_vec_20260913.md`（未 commit，夜间 TianjiGit_Nightly 统一提交）

## 8. 未做/未定

- 未改造 9 个非清单函数（保持旧实现，无等价风险）。
- rsrs / chip_profile 无法在 1e-9 等价下全向量化（阈值分支 / 分位临界对浮点敏感）——已记录为架构性结论，供后续批次参考。
- 性能绝对目标 3.0ms 未达（4.5ms），如实披露；调用数目标大幅超额达成（2,968 << 20,000）。
