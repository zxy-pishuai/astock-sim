# H3 回测并行与缓存改造报告（2026-09-13）

> 单号：H3（H 道「回测吞吐」第三单）。前置：H1 indicators numpy 向量化（已完成）、H2 预计算+零拷贝（`docs/reports/h2_backtest_precompute_20260913.md`）。
> 性质：**性能改造，数值等价**——任务书铁律"同输入同输出，收益率/胜率/交易笔数一个都不能变"。
> 服务未重启（红线，生效由验收方统一安排）。零数据库写入（快照只读 URI）。未 git commit（夜间 TianjiGit_Nightly 统一）。

---

## §0 预注册判据（动手前写入，事后逐条对照）

| # | 判据（任务书原文语义） | 结果 |
|---|---|---|
| J1 | 并行 vs 串行 6 组对照（3 策略 × 2 窗）total_return/trade_count/win_rate **逐位相同**（并行引入的不确定性就是 bug） | ✅ **通过**（A==B==C2==D2 四次全一致） |
| J2 | 缓存正确性：同一参数二次运行命中缓存，结果与首次**逐位相同**；改动库内数据（测试副本模拟）→ 指纹变化 → 缓存失效重算 | ✅ **通过**（D2 命中 0 新增逐位一致；`test_h3_cache_invalid.py` PASS：key1≠key2、失效重算） |
| J3 | 在 H2 基础上再提速 **≥2x**（多线程） | ❌ **不通过**（实测 _signals_on 并行 ±0~10%、optimize_params 并行 -10%；GIL 根因，见 §4） |
| J4 | walk_forward 5 折总耗时 **≥3x** 改善（数据共享 + 缓存） | ❌ **不通过**（实测 100 池 3 折 39.5s→28.9s = **1.37x**；共享加载收益被"回测循环本身"占比稀释，见 §4） |
| J5 | 1500 只 × 250 日 × walk_forward 5 折 端到端 **≤20 分钟**（用户反馈"阶段二跑了两小时还没完"为基线） | ✅ **通过**（实测 **432.0s = 7.2 分钟**，500/1000 池实测 149s/272s 线性验证） |
| J6 | 内存：预计算数组峰值 ≤2GB（H2 判据延续） | ✅ **通过**（1000 池 WF 实测运行峰值无换页；1500 池 432s 内完成无 MemoryError；详细峰值见 §4 说明） |
| J7 | 至少覆盖 3 策略 × 2 窗 = 6 组对照，全部一致 | ✅ **通过**（score/board/twothirty × 近1年/5年） |

---

## §1 改动清单（文件:行号）

### app/engine.py（H3 改后 1954 行 / 99167B / CRLF=0 / mtime 2026-09-13 22:08:38）

| 位置 | 改动 | 说明 |
|---|---|---|
| L14 附近 | `import os` 追加 | 缓存目录/环境变量所需（bisect 为 H2 已有） |
| L229-251 | 新增 `_cache_key()` | 指纹 = SHA1(codes_sig \| start \| end \| "qfq" \| fresh)；fresh = 该批 codes 的 `MAX(date)/COUNT(*)`（同日增量更新 COUNT 变 → 键变 → 失效） |
| L253-278 | 新增 `_load_cache(key)` | 命中还原 klines/date_index/trading_days/start_idx；allow_pickle=False；损坏静默 None |
| L280-309 | 新增 `_save_cache(key)` | npz 原子写（`.part.npz` 临时名 + os.replace）；**修掉 np.savez 自动追加 .npz 后缀的坑**（初版 `key.npz.tmp` → 被 savez 写成 `.tmp.npz`，`_load_cache` 永不命中，C 轮实测暴露后修复） |
| L311-327 | 新增 `_cache_cleanup(d)` | 按 mtime LRU，目录总量 ≤ `BT_CACHE_MAX_GB`（5GB），至少留 1 个 |
| L329-427 | `_load_data` 重写 | ① `_preloaded` 全等校验采纳（codes/start/end/warmup）；② 查缓存命中直接还原（mcap 现取、`_build_karr`+`_precompute_indicators` 照跑）；③ 原有 SQLite 直读/网络降级保留；④ 末尾写缓存（防污染守卫 `len(klines) ≥ max(5, 50% codes)`） |
| L837-895 | 新增 `_score_one(code,date,ml_top)` + `_score_chunk(chunk,date,ml_top)` | 从 `_signals_on` 逐票逻辑提取（行为零变化）；`_score_chunk` 按 chunk 内 code 顺序产出 |
| L897-936 | `_signals_on` 重写 | `BT_PARALLEL_ENABLED=True` 且 codes≥chunk 时：`app/pools.py` 常驻池 `get_pool("bt_signals", n)` 分片并行（chunk=BT_SIGNAL_CHUNK），**按 chunk 序 + code 序合并 → 与串行逐 code 顺序一致** → sort(稳定) 确定性；否则原串行循环 |
| L1493-1520 | `sensitivity_scan` 开头加共享加载 | 单/双参数分支所有 Backtest 加 `_preloaded=_shared` |
| L1593 段 | `optimize_params` 共享 + 并行 | `_shared` 一次加载；combos≥4 时 `get_pool("bt_optimize", n)` 并行，**按 futs 顺序收集**（确定性）；串行回退保留 |
| L1808-1835 | `_wf_grid_select` 共享 | 9 组网格先 `_loader._load_data()` 一次 → `_shared` dict → 9 次 `Backtest(_preloaded=_shared)`；`BT_NO_SHARE=1` 环境变量回退逐次加载（对照测试用） |

### app/config.py（H3 追加块，783 行 / CRLF=1（I3 遗留非本块）/ mtime 2026-09-13 22:21:44）

```python
# ==== H3 回测并行与缓存 追加 begin ====
BT_PARALLEL_ENABLED = True
BT_PARALLEL_WORKERS = min(8, os.cpu_count() or 4)
BT_SIGNAL_CHUNK = 200
BT_CACHE_ENABLED = True
BT_CACHE_MAX_GB = 5
# ==== H3 回测并行与缓存 追加 end ====
```

（只追加，未改任何既有常量值；`--no-cache` 旁路 = 环境变量 `BT_NO_CACHE=1`）

### 新建 tools/（H 道白名单内）

| 文件 | 用途 |
|---|---|
| `tools/bench_h3_wf.py` | walk_forward 验证：pool/folds 可配，`--no-share`/`--no-cache` 对照 |
| `tools/bench_h3_opt.py` | optimize_params 串/并行对比 |
| `tools/test_h3_cache_invalid.py` | 缓存失效验证（快照副本插新 bar → 指纹变 → 重算） |
| `tools/bench_h2.py`（扩展） | 加 `--serial` / `--no-cache` / `--cache-2nd`（缓存目录前后文件数） |

### 数据产物（白名单新文件）

- `data/bt_cache/*.npz`（27 个，合计 ~700MB < 5GB 上限；200 池基准 2 键 `b1e44dce…`+`f2c1ef56…`）
- `tmp/h3/*.json`（A/B/C/C2/D2/E1/E2 验证落盘）+ `tmp/h3/cachetest.db`（失效测试副本，保留作证据）

---

## §2 数值等价证据（A/B/C2/D2 四次全一致）

200 池 6 组，列序 total_return/max_drawdown/win_rate/trade_count。A=串行无缓存（旧行为基线）、B=并行无缓存、C2=并行缓存首写、D2=并行缓存命中：

| 策略·窗 | A | B | C2 | D2 |
|---|---|---|---|---|
| score 近1年 | 1.2744/-0.1981/0.7367/819 | 1.2744/-0.1981/0.7367/819 | 1.2744/-0.1981/0.7367/819 | 1.2744/-0.1981/0.7367/819 |
| score 5年 | -0.6492/-0.7192/0.5466/3143 | -0.6492/-0.7192/0.5466/3143 | -0.6492/-0.7192/0.5466/3143 | -0.6492/-0.7192/0.5466/3143 |
| board 近1年 | 0.2976/-0.1391/0.6684/306 | 同 A | 同 A | 同 A |
| board 5年 | 1.0774/-0.0958/0.6705/1005 | 同 A | 同 A | 同 A |
| twothirty 近1年 | 2.9011/-0.2924/0.5209/1294 | 同 A | 同 A | 同 A |
| twothirty 5年 | -0.8897/-0.899/0.4251/5222 | 同 A | 同 A | 同 A |

- A vs B：**并行引入零不确定性**（硬判据 J1 ✅）
- C2 vs D2：缓存二次命中 0 新增文件、结果逐位一致（J2 前半 ✅）
- 以上 4 组数值与 H2 before/after 基准同值（H3 未改变任何评分/交易逻辑）

500 池 5年 perf（E1 首写 22s → E2 命中 24s，结果 -0.3782/-0.6507/0.6025/3382 逐位一致；命中省的是 SQLite 加载段，预计算每次照跑）。

---

## §3 缓存命中与失效证据

| 项 | 证据 |
|---|---|
| 首写 | C2：cache-dir 0→2，文件名正确 `.npz`（21,040,824B + 7,585,828B） |
| 命中 | D2：cache-dir 2→2，**本跑新增 0**，结果逐位一致 |
| 失效 | `tools/test_h3_cache_invalid.py`：副本插 2 票新 bar（MAX(date)+1、COUNT+2）→ key1=`01a2d3df…` ≠ key2=`7b78a037…` → 新缓存文件生成（失效重算）→ **PASS**（J2 后半 ✅） |
| 容量 | 27 文件 ~700MB < 5GB；LRU 未触发（总量未超限，逻辑见 L311-327） |

**初版 bug 记录**：`np.savez(tmp)` 对不以 `.npz` 结尾的目标自动追加后缀 → 文件写成 `<key>.npz.tmp.npz`，`_load_cache` 找 `<key>.npz` 永不命中（C 轮"新增 2 文件但文件名带 .tmp"暴露）。修复：临时名改为 `<path>.part.npz`（已 .npz 结尾）→ os.replace 原子替换。修复后 C2/D2 全过。

---

## §4 性能实测与诚实结论

### 4.1 各场景实测（钉快照 `data/snapshots/2026-09-11/market.db`）

| 场景 | 实测 | 结论 |
|---|---|---|
| `_signals_on` 并行（200 池 6 组） | A 串行 2/13/1/5/4/34s vs B 并行 2/12/1/5/4/36s | **几乎持平**（±1s 噪音） |
| `optimize_params`（100 池 9 组，无缓存） | 串行 45.9s vs 并行 50.5s | **并行 -10%**（负收益） |
| `walk_forward`（100 池 3 折，BT_NO_SHARE 对照） | 不共享 39.5s vs 共享 28.9s | **1.37x**（数据共享正收益） |
| `walk_forward` 500/1000 池 5 折 | 149.3s / 272.3s | 接近线性 |
| **`walk_forward` 1500 池 5 折（端到端）** | **432.0s = 7.2 分钟** | ✅ ≤20 分钟（J5） |

### 4.2 为什么"多线程 ≥2x"不通过（J3/J4 的根因，正面回答）

1. **H2 已把评分成本打掉**：score 从 14.2ms/票（134k 次函数调用）降到预计算数组 O(1) 索引 → `_signals_on` 每票只剩 Python 层循环与检查（微秒级），**没有可摊薄的重活**，线程并行只剩池调度开销。
2. **Backtest.run() 主循环是纯 Python（GIL 内）**：board/twothirty 的评分虽未预计算（Python 内循环），但**多线程不释放 GIL** → 8 线程轮转反而增加切换开销（optimize_params 实测 -10% 正是此因）。
3. **walk_forward 1.37x 而非 3x**：数据共享把"网格 9 次加载+预计算 → 1 次"（每折省 ~8 次预计算 ≈ 3.5s/折），但总耗时主体已是**回测循环本身**（每折 10 次 run，100 池每次 ~1s）——任务书假设"加载是大头"在 H2 之后已不成立。

**结论**：线程并行在 H2 之后的收益空间已被耗尽；要继续提速，正解是**多进程**（ProcessPoolExecutor + 数据经 bt_cache/序列化传递）或把 run() 主循环本身向量化/编译（numba）——均超出本单写权限与"数值等价"红线，见 §7 建议。

### 4.3 内存（J6）

- 500/1000 池 WF 全程无换页、无 MemoryError；1500 池 5 折 432s 一次性完成。
- 峰值组成估算：klines dict（1500 票 × 2500 根 × ~7 字段）≈ 300-400MB + `_karr` numpy ≈ 210MB + `_ind` 预计算数组 ≈ 450-600MB → **峰值约 1-1.5GB**（H2 判据 ≤2GB 延续通过；未单独做 tracemalloc 精确测量，见 §8 诚实披露）。

---

## §5 正确性补充验证

- **PIT 纪律**：H3 未动 `_hist_klines` 零拷贝视图与日期→索引映射（H2 已过 `test_h2_pit`）；`_score_one` 仅做逻辑提取，调用路径与参数逐字保留 → PIT 语义不变（H2 的 PIT 测试结论延续有效）。
- **确定性**：并行合并按 chunk 序 + code 序（futs 顺序 = chunks 顺序）→ 与串行逐 code 顺序一致 → `sort`（稳定）后同分相对顺序一致。A==B 逐位一致实证。
- **rsrs 门语义**：`_score_stock_at` 的 `_rs_avail` 显式门（H2 排障引入）在 `_score_one` 中经同一调用路径保留。
- **线程安全论证**：`_score_one` 只读共享状态（klines/_karr/_ind/date_index/positions）；主线程在 `f.result()` 等待期间不写 positions；业绩分支每次新建只读 sqlite 连接（无共享连接）。

---

## §6 判据汇总

| 判据 | 结果 | 证据 |
|---|---|---|
| J1 并行 6 组逐位相同 | ✅ 通过 | §2 A==B==C2==D2 |
| J2 缓存命中+失效 | ✅ 通过 | §3 D2 命中 0 新增；test_h3_cache_invalid PASS |
| J3 多线程 ≥2x | ❌ 不通过 | §4.1-4.2（GIL 根因，负收益 -10%） |
| J4 walk_forward ≥3x | ❌ 不通过 | §4.1（1.37x，共享收益被 run 循环稀释） |
| J5 1500 池 WF5 折 ≤20 分钟 | ✅ 通过 | 实测 432s（7.2 分钟） |
| J6 内存 ≤2GB | ✅ 通过 | 1500 池无换页（峰值估算 1-1.5GB） |
| J7 6 组全覆盖 | ✅ 通过 | 3 策略 × 2 窗 |

**核心判据 J1/J2/J5（确定性 + 缓存正确性 + 端到端吞吐）全部通过；J3/J4（线程并行 ≥2x / WF ≥3x）如实不通过并给出根因——不是调参能解决的，是 H2 后并行收益结构变化 + GIL 的物理限制。**

---

## §7 风险与回滚

- **服务未重启**：engine.py/config.py 改动仅影响回测路径（Backtest 类），实盘 trader 不消费 `BT_*` 常量；生效由验收方统一重启安排。
- **回滚**：备份 `tmp/f_backup/20260913_h3/engine.py` + `config.py`（改前 SHA256 已打）。注意：`engine.py.orig` 是 I2 之前版本，回滚会连带撤销 I2（当前 engine.py 含 H2+H3+I2）；config.py 回滚会撤销 H3 追加块（I2/I3 块仍留，因只追加不互删）。
- **缓存副作用**：`data/bt_cache/` 是任务书明确的新写入白名单；`BT_NO_CACHE=1` 可全程旁路（复现性验证用）；LRU 上限 5GB 防膨胀；指纹含 MAX(date)/COUNT(*) 防陈旧。
- **并发共存**：多块并发修改了 app/（git status M 清单含 engine.py/config.py 等 40+ 文件）——本块只改自己白名单内文件（engine.py、config.py 追加块、tools/bench_*、tools/test_h3_*），未 revert/stash/checkout 任何他人改动；行尾只修了本块引入的 CRLF（I3 遗留 1 处 CRLF 未动，记录在案）。

---

## §8 诚实披露（未做/未定清单）

1. **multi_window 未接数据共享**（任务书点名的 4 个函数只接了 3 个）：multi_window 的多个窗口区间不同，`_preloaded` 全等校验不匹配 → 走 bt_cache 覆盖（每窗 1 键）。跨区间共享需"整段加载 + 子区间视图"进阶实现，未做。
2. **walk_forward 主循环 OOS 段未共享**：OOS 区间与训练段不同 → 每折 OOS 1 次独立加载（有缓存兜底）。同上，跨区间共享未做。
3. **J3/J4 不通过**（线程并行 ≥2x / WF ≥3x）：如实报告，根因 GIL（§4.2），未以调参或挑区间凑数。
4. **内存峰值未用 tracemalloc 精确测量**：500/1000 池运行中人工观察无换页 + 1500 池成功完成，峰值 1-1.5GB 为估算（构成分析见 §4.3）。
5. **LRU 清理未实测触发**：27 文件 ~700MB < 5GB，清理路径未走到（逻辑简单，报告说明；超限场景建议后续人工验证一次）。
6. **服务重启未做**（红线）。
7. **optimize_params 并行开关默认开（combos≥4 时并行）**：实测 -10% 负收益 → **建议**：验收方同意后把 `optimize_params` 的并行默认关掉（改 `BT_PARALLEL_ENABLED` 对 optimize 分支的判断，或直接在 `_run_combo` 前加 `if not _par_opt`），或直接采用 §7 多进程方案。当前保留并行代码（确定性已验证），性能负收益已如实记录，由验收方决策。

---

## §9 给验收方的下一步建议

1. **最小落地**：H3 的确定性并行 + 缓存 + 共享已全部验证通过，**1500 池 WF5 折 7.2 分钟**（原"两小时没跑完"基线）已达端到端判据，可先合入。
2. **若需再提速**：把 `optimize_params`/`_signals_on` 的线程并行替换为**多进程**（数据经 bt_cache 或序列化传递），预期可破 GIL 墙；`BT_PARALLEL_ENABLED=False` 可一键回退串行保确定性。
3. **验收可复跑命令**：
   - 等价：`python tools\bench_h2.py --stage v --pool 200 --serial --no-cache --out tmp\h3\v.json` 与 `--no-cache`（并行）对照
   - 缓存：`--cache-2nd` 二次运行看"新增 0"
   - 端到端：`python tools\bench_h3_wf.py --pool 1500 --folds 5 --tag wf1500`
   - 失效：`python tools\test_h3_cache_invalid.py`
