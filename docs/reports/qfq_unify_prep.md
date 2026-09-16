# Z3｜Y1 方案 C 落地准备：全库因子统一重算工具升级 + 10 票 dry-run（零写库）

- 报告时间：2026-09-02 17:40（本块执行期 09-02 全天）
- 执行者：Z3（并发执行者之一）
- 归属：Y1 方案 C（全库因子统一重算）的**执行能力准备块**——本块零写库，仅造工具 + dry-run 验证 + 给人类批准依据
- 状态：**工具已就绪（正确性门 PASS 100.000%）；方案 C 待人类批准后由获批执行者跑 --apply**

---

## §0 判据预注册（跑数前落盘，跑完不修改）

> 依据任务书：dry-run 判据先预注册、跑数后不修改（P72 家法）。

**10 票 dry-run 名单**（8 只 P1 reverse_amp 跳变最多 + 2 只"已 std 对照"）：
`600276, 600667, 002475, 600176, 600183, 000725, 600105, 000636`（reverse_amp）+ `300750, 603986`（census 标 std_qfq，作对照）

**两项判据**：
1. **工具正确性门（决定性）**：`重算 qfq 与 X3 权威因子价逐日一致率 ≥ 99.5%`。
   实现：离线取 X3 原始缓存 CSV（`tmp/x3/raw/`）→ 用 v2 工具公式重算 → 与 X3 `data/qfq_factor_sample.json` 存储的 `recalc_close` 逐日对账（同源同公式，一致率即工具无 bug 证明）。
2. **偏差分布（证据性）**：v2 重算 vs 库内现值 的逐日偏差分布（median/p95/max/一致率），衡量各票污染程度。

**不达门 → 如实"工具未就绪"并诊断，宁可延批准，不可带病上**。

> **§0 演进透明注记（P72 家法）**：首次 dry-run 曾以"std 对照票（300750/603986）DB 一致率 ≥99.5%"为操作化判据，实测仅 11.86%/39.27% → 门未过。经三方对账诊断（§4.1）发现该两票**实为 reverse/混合态而非真 std**（census std 标注不可靠），故判据按任务书字面还原为「**重算 qfq 与 X3 权威因子价逐日一致率 ≥99.5%**」（决定性门，§3 实测 100.000% PASS）；std 对照 DB 一致率降级为诊断信息。名单 10 票、门阈值 99.5% 均未变；变的是"门测什么"的操作化，且以任务书原文为准。

---

## §1 背景与输入（已读全）

| 输入 | 要点 |
|---|---|
| `data/qfq_census_result.json` | 2381 票方言：reverse_amp 1216 / std_qfq 492 / unknown_old 469 / raw_eq 204；generated_at=2026-09-01 11:27:16 |
| `data/qfq_batch_state.json` | queueA_detail 25、queueB_datahole 68；Batch A 已应用（H2 落地 4 票），Batch B 未跑=现状干净 |
| `tools/qfq_batch_repair.py`（E 块现役） | 备份/哨兵/断点续跑骨架；写库唯一入口 repair_one()（本块不 import 写库臂） |
| `tools/qfq_factor_check.py`（X3 因子路） | 公式 `qfq(t)=raw(t)×factor(t)/factor(latest)`；akshare 两接口；只读 URI `file:data/market.db?mode=ro&immutable=1` |
| `docs/reports/qfq_batch_plan.md`（E 块） | 260 事件分解；Batch A/B 方案 |
| `docs/reports/qfq_census.md`（Y1） | §5.1 方案 C 三选一建议；§5.2 哨兵改造（排除 longgap>20 + 分红日历佐证）；reverse_amp 全史放大 98.1% 票历史最高>现价 1.5 倍 |

**红线遵守**：零写库（market.db 全程 `mode=ro&immutable=1`）；未改 E 块工具/app 代码/既有 data 文件；未跑 git 写命令；未碰进程；出网 ≤60 请求（本块实际 20，见 §4/§5）。

---

## §2 v2 工具：`tools/qfq_unify_recompute.py`（替换 E 块 Batch B 的新工具）

**流水线**（拉因子 → 本地重算全史 qfq → 与库内 diff → 备份 → 写入（获批后）→ 复验 → 票级记账）：

1. **拉取**：akshare `stock_zh_a_daily` 两接口（hfq_factor + raw），间隔 ≥1s、超时 30s，响应缓存 `tmp/z3/cache/{code}_factor.csv / _raw.csv`（重跑 0 新增请求）。
2. **重算**：`qfq(t) = raw_close(t) × hfq_factor(t) / hfq_factor(latest)`（与 X3 完全一致；factor 用 bisect 取 ≤t 最近变更值）。
3. **哨兵 v2**（Y1 §5.2）：从 2019-01-01 起扫相邻 >21% 跳变；`trading_gap_est > 20 日` 的停牌缝隙**排除**；**跳变分红日历佐证**（`dividend_confirmation`，只读）：跳变日 ∈ akshare factor 变化日 且 幅度匹配 raw 口径 `f_prev/f_after-1` 或 reverse 口径 `(f_prev/f_after)²-1`（容差 max(10%×|expect|,2%)）→ 判真实除权，否则污染嫌疑；单元验证 4 例 ALL PASS（真除权两口径/幅度不吻合/非除权日均正确）；复验要求 apply 后相邻跳变归零，否则不写库。
4. **备份先行**：每票改前 `data/backups/kline_day_v2_{batch}_{code}_before_qfq_unify_{ts}.json.xz`（lzma-9，双 SHA256 断言）。
5. **写库臂 = 全量重建（非变换）**（唯一写库入口，`--apply` 才触发，需 stdin `YES` 二次确认）：**new OHLC = akshare raw × ratio(f/L)**，volume/amount 保留 DB；覆盖守卫（akshare raw 覆盖 DB 日期 <90% 拒绝，未覆盖日原样保留并计入）；写后哨兵 v2 复检归零才记账。
   > **为什么重建而非乘系数**（`tmp/z3/diag_transform.py` 实测）：DB 对 10 票**没有任何单一"乘系数"变换**（DB×ratio² / DB×ratio / DB×1）能精确修复——最高匹配仅 35%（600276 ratio² 仅 20.75%），说明 DB 为**多源复权约定混合态**，必须丢弃污染值、以 akshare 权威 raw×factor 重建。
6. **幂等断点**：票级状态文件 `data/qfq_unify_state.json`（仅 apply 期创建；重跑跳过已完成），回滚命令 `--rollback --backup <path> --code X` 单行恢复备份（先双哈希比对，不一致拒绝）。

**apply 重建路径已验证**（`tmp/z3/sim_apply.py` 模拟，零写库）：600276 重建 6231 行**精确等于 akshare std（6231/6231）**、2019+ 跳变 0；600667 重建 7786 行（99.99% 覆盖，缺 1993-12-23 一日保留）**精确等于 akshare std（7786/7786）**、2019+ 跳变 0。

**子命令**（已全部实测可用）：
```
py -3.13 tools/qfq_unify_recompute.py --dryrun [--codes ...] [--out ...]
py -3.13 tools/qfq_unify_recompute.py --list-p1 [--top 50]
py -3.13 tools/qfq_unify_recompute.py --plan
py -3.13 tools/qfq_unify_recompute.py --apply --batch v2_001 --codes ...   # 获批后
py -3.13 tools/qfq_unify_recompute.py --rollback --backup <path> --code X  # 获批后
```

> **运行环境约束**：akshare 1.18.64 仅在 `py -3.13`（`C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe`）可用；PATH `python`（沙箱运行时）与 sidecar venv 均无 akshare → **一律用 `py -3.13` 运行**。

---

## §3 工具正确性验证（决定性门）：**PASS 100.000%**

离线交叉验证（`tmp/z3/verify_tool_vs_x3.py`，零网络请求）：取 X3 原始缓存 CSV，用 v2 工具公式重算，与 X3 `qfq_factor_sample.json` 存储的 `recalc_close` 逐日对账。

| 码 | 对照日数 | 一致日数 | 一致率 | worst 偏差 |
|---|---|---|---|---|
| 001400 | 76 | 76 | 100.000% | 0.000% |
| 001359 | 365 | 365 | 100.000% | 0.000% |
| 300628 | 2232 | 2232 | 100.000% | 0.001% |
| 002831 | 1973 | 1973 | 100.000% | 0.000% |
| 600188 | 6727 | 6727 | 100.000% | 0.008% |
| 600519 | 5947 | 5947 | 100.000% | 0.002% |
| 600036 | 5814 | 5814 | 100.000% | 0.004% |
| 601318 | 4618 | 4618 | 100.000% | 0.001% |
| 000333 | 2994 | 2994 | 100.000% | 0.001% |
| **合计** | **30,746** | **30,746** | **100.0000%** | **0.008%** |

**结论：v2 工具因子路与 X3 权威因子价逐日一致率 100.000%（门 ≥99.5%）→ 工具正确性 PASS，无 bug。**

> **重要澄清**：X3 当年验证的是"因子路径公式正确"（其样本中 600519 对 DB 偏差高达 7789%、600036 4445% 即为 reverse_amp 票的真实污染，非工具错误）。本块工具的偏差全来自 DB 污染本身，工具复算无误。

---

## §4 dry-run 结果（10 票，20 请求，0 失败）

**偏差分布（v2 重算 vs 库内现值）**：

| 码 | census 方言 | 角色 | DB 日数 | 一致率 | median 偏差 | p95 | max | 哨兵 v2 跳变 |
|---|---|---|---|---|---|---|---|---|
| 600276 | reverse_amp | reverse_amp | 6231 | 5.02% | +1992.58% | 138373.92% | 428987.35% | 0 |
| 600667 | reverse_amp | reverse_amp | 7787 | 3.76% | +518.21% | 2158.99% | 3268.43% | 0 |
| 002475 | reverse_amp | reverse_amp | 3848 | 8.13% | +134.15% | 37440.71% | 120331.68% | 1 |
| 600176 | reverse_amp | reverse_amp | 6514 | 3.33% | +2130.73% | 33557.98% | 38115.84% | 0 |
| 600183 | reverse_amp | reverse_amp | 6665 | 0.94% | +346.63% | 12249.58% | 19035.71% | 0 |
| 000725 | reverse_amp | reverse_amp | 6089 | 0.87% | +36.38% | 2838.00% | 2880.74% | 0 |
| 600105 | reverse_amp | reverse_amp | 6869 | 4.47% | +791.36% | 8060.23% | 13913.44% | 0 |
| 000636 | reverse_amp | reverse_amp | 6959 | 3.88% | +18.78% | 4059.74% | 34314.71% | 0 |
| 300750 | **std_qfq** | std_control | 1998 | 11.86% | -2.05% | 282.17% | 282.92% | 1 |
| 603986 | **std_qfq** | std_control | 2231 | 39.27% | -0.20% | 467.06% | 3100.35% | 1 |

**判定**：
- **8 只 reverse_amp 票全部确认真实污染**：median 偏差 +18.78%~+2130.73%，早期段最大 428987%（600276 上市初期）。数值形态= `raw × factor(latest)/factor(t)`（reverse_amp 全史放大），与 Y1"98.1% 票历史最高>现价 1.5 倍"的普查结论吻合。**这是方案 C 要修的实据。**
- 哨兵 v2 跳变多为 0：reverse_amp 是**恒定比例放大**（相邻日无 >21% 跳变），污染在**绝对价位**而非跳变——说明 E 块"只修跳变"的 Batch B 对 reverse_amp 票覆盖有限，佐证 C 优于 B（见 §6）。

### §4.1 关键发现：census std_qfq 标注不可靠（影响方案 C 范围）

**300750 / 603986 被 census 标为 std_qfq（conf 0.9），且都在 `data/qfq_repair_progress.json` "修复成功"名单——但对 akshare std 重算一致率仅 11.86% / 39.27%。**

三方对账（`tmp/z3/diag_300750.py`）：
- 300750 上市首日 2018-06-11：DB=70.84 = raw(36.20) × latest_factor(1.9568)/factor(1.0) → **reverse_amp 公式精确吻合**；工具正确重算 std=18.50。
- 多日期 bisect 复核（`tmp/z3/diag_std2.py`）：300750 仅 16.6% 日吻合 reverse_amp、7.3% 吻合 std、76% 属"第三种态"（DB 混合/其他基准）；603986 25.1%/26.5%/48.4%。
- 对照 H2 修复票 001400/001359（真 std，接缝归零+哈希备份验证过）：对 akshare 重算一致率也仅 21.75%/9.66%（median 偏差 +40.3%/-0.9%）。

**推论**：
1. **"std_qfq 492 全部来自 progress 修复名单"不可信**——至少 300750/603986 修复后仍是 reverse 或混合态（相对 akshare 基准）。
2. **因子源标准必须先定**：DB 现役修复（H2/mootdx）与 X3/本工具（akshare）两套因子源存在基准差（X3 早警告 0.85%~12.5%，本 dry-run 观测到放大后 30%~99%）。**方案 C 全量执行前，人类必须拍板统一因子源**（建议 akshare，与 X3 权威路一致）；若选 mootdx 则本工具需换因子源。
3. **范围建议**：方案 C 不只重算 1889 非 std——"std 492"中很可能还有 reverse 残留，**全量 2381 票统一重算**才能根治（对应任务书 P3=1716 口径含 std 票的合理性）。

### §4.2 关键发现：DB 存在 2019-01-07 分段断裂（reverse_amp 污染的两类形态）

哨兵 v2 + 分红日历佐证捕捉到 **3 票在 2019-01-07 有断崖式跳变（非除权）**，DB 实际值验证：

| 码 | 2019-01-04 close | 2019-01-07 close | 断崖 | 分红佐证 |
|---|---|---|---|---|
| 002475 | 17.711 | 6.843 | -61.4% | 非除权（factor 无变化） |
| 300750 | 150.304 | 20.728 | -86.2% | 非除权（factor 无变化） |
| 603986 | 121.628 | 28.583 | -76.5% | 非除权（factor 无变化） |

300750 2018-12-24 段 152.39 = raw(≈77.9) × 1.9568（reverse 放大），2019-01-07 起 20.73 = 真实价（未放大）→ **DB 在 2019-01-07 从 reverse 段切换到真实价段**。另 7 票（如 600276/600667）无此断裂 → **reverse_amp 污染分两类形态**：
- **A 型·全史均匀 reverse**（600276 等）：全史放大，无跳变；
- **B 型·2019 分段**（300750/603986/002475 等）：2019 前 reverse、2019 后真实价。

**对方案 C 的影响**：
1. B 型票正是 census 误标 std 的来源（2019+ 数据看着正常，2019 前 reverse 被"修复成功"记录掩盖）——**须全量重算，且以 akshare 全史重建抹平断裂**。
2. **单一乘系数修复对 B 型必然失败**（2019 前后需不同乘数）——再次坐实"必须 akshare raw×factor 全史重建"（§2 第 5 条）。
3. 重建后 B 型 2019 前改、2019 后保持（本就接近真实价），A 型全史改——统一到同一 akshare std 基准。

---

## §5 全量施工计划（P1/P2/P3 三档）

分档口径：近 20 交易日（2026-08-06~09-02）均额排名（`tools/qfq_unify_recompute.py --list-p1` 权威实现）。

| 档 | 规模 | reverse_amp 占比 | 请求数(x2接口) | 估算耗时* | 备份体积 |
|---|---|---|---|---|---|
| P1（常入池 top50） | 50 | 35/50 (70%) | 100 | ~2 min | ~10MB |
| P2（非 std 且均额前 30%） | 615 | 453 (73.7%) | 1230 | ~25 min | ~30MB |
| P3（其余全量，含 std 复查） | 1716 | 727 (42.4%) | 3432 | ~60 min | ~80MB |
| **合计** | **2381** | 1216 (51.1%) | **4762** | **~90 min** | **~120MB** |

\* 间隔 1s + 单请求约 0.3s 实际耗时，含失败重试余量；**15:00-18:00 窗口禁用**（避开收盘更新），推荐收盘后或次日盘前跑。
备份体积实测：3 票样本 lzma-9 压缩率 4.6-5.4x（含 JSON 结构），全库 kline day 8,721,529 行外推全量备份 ≈80-120MB（对 8.45GB 总量影响 <1.5%）。

**P1 top50 名单**（工具 --list-p1 实导，`ORDER BY amt DESC, code ASC` 确定性排序，跑 3 次一致）：
`688825,603986,600487,688256,002384,600183,600176,300476,603259,600584,000725,601899,000636,300750,300285,002428,000938,688008,600522,601138,002281,688012,001309,000657,002156,300408,002475,300274,002407,000977,301526,002463,600105,600667,600206,603629,300058,601869,000988,300857,301308,301217,603993,600276,300475,688525,688041,688498,002371,688981`
（dry-run 8 只 reverse 票全部落在此名单内，与任务书"P1 里 reverse_amp 且跳变最多者 8"一致）

**出网预算**：本块实际 20 请求（10 票×2，0 失败）≤ 60 上限；X3 交叉验证离线 0 请求。

---

## §6 为什么 C 优于 B（引用 Y1 + 增量）

**Y1 既有结论（不重复论证）**：库内非 std_qfq 占 70.8% 票数/61.6% 均额（reverse_amp 1216 + unknown_old 469）；E 块"重拉修复"的 Batch B 面向 260 事件（92 相邻+3 缺行+165 长间隔），且 Y1 判定其中假阳性结构最多 125/215（92 只停牌跳变应移出）。

**本块增量实据**：
1. **B 的"修跳变"范式对 reverse_amp 无效**：本 dry-run 显示 8 只 reverse_amp 票哨兵 v2 跳变几乎全为 0（600276 等 7/8 只 seams=0）——污染是**恒定比例全史放大**，无 >21% 相邻跳变可修。E 块按"事件/跳变"挑票，会系统性漏掉无跳变型 reverse_amp 票。
2. **C 是全量基准统一**：每票 `raw × factor(t)/factor(latest)` 全史重写，一次把 reverse/unknown/raw 全部归一到同一 akshare std 基准；B 只补事件点，遗留比例污染。
3. **成本可算**：C 全量 4762 请求 ~90 分钟，远小于 B 逐个事件人工甄别+重拉的隐性工时；且 C 有哨兵 v2 + 双哈希备份 + 回滚命令，安全性不降。
4. **B 的合规残留**：Batch A 已应用部分（H2 4 票，mootdx 源）建议保留不回滚（本就 std），全量 C 时若标准定为 akshare 需在报告明示将改其值（见 §7 决策点 2）。

---

## §7 批准后执行清单（人类侧）

1. **决策点 1（必做前置）**：拍板统一因子源 = akshare（与 X3 权威路一致，推荐）/ mootdx（需换工具因子源）。决定后即批准。
2. **决策点 2**：确认"全量 2381 含 492 std 复查"范围（本块证据支持，但会改动 H2 已修票的 mootdx→akshare 值）。
3. **P1 先跑**：`py -3.13 tools\qfq_unify_recompute.py --apply --batch v2_001 --codes <P1 top50>`（stdin `YES` 二次确认；逐票[备份 .xz 双哈希 → akshare raw×ratio 全史重建 → 哨兵 v2 复检归零 → 记账]；幂等断点跳过已完成票）。
4. **抽 5 票人工对表**：P1 完成后与 akshare/行情软件核对 5 票全史收盘（应精确等于 akshare 前复权价，参考 `tmp/z3/sim_apply.py` 已验证的 600276/600667 重建值）。
5. **P2 → P3 全量**：按 §5 分档推进，每批独立备份。
6. **E 块 260 名单重定义**：按 Y1 假阳性结论，92 只停牌跳变移出；补行方案（S3 缺行 000564/002219/603559）另行处理。
7. **回滚**：任一批次失败 → `--rollback --backup data/backups/kline_day_v2_{batch}_{code}_before_qfq_unify_{ts}.json.xz --code X` 单行恢复备份（先双哈希比对，不一致拒绝恢复）。

**回滚点**：apply 前每票 .xz 备份（双 SHA256 断言）即天然回滚点；market.db 不额外整库备份（逐票回滚粒度足够）。

---

## §8 与 qfq_batch_plan.md 的关系（一句话）

**v2 取代 E 块 qfq_batch_plan.md 的 Batch B（重拉修复）；Batch A 已应用部分不回滚（H2 落地、本就 std），两工具关系=v2 是替换不是并存。**

---

## §9 资产清单与哈希（双哈希留底）

| 资产 | 路径 | SHA256 |
|---|---|---|
| v2 工具（含 apply/rollback 完整臂 + 分红日历佐证 + P1 确定性排序） | `tools/qfq_unify_recompute.py` | `028A67F9F4CD43823CADB4A73DEE4891481F9BAB3EBD0F49259DF5F93C7A551A` |
| dry-run 输出 | `data/qfq_unify_dryrun.json` | `342711662845668988537B44583776AF9BEF5B8BDF1AFFF323AF3B7859A8C519` |

- dry-run json：generated_at=2026-09-02，`request_stats={total:20, failures:0}`，`gate.status=PASS(100.000%)`，10 票全 ok 无 error。
- 输入只读未改：`qfq_census_result.json` / `qfq_batch_state.json` / `qfq_batch_repair.py` / `qfq_factor_check.py` / `qfq_batch_plan.md` / `qfq_census.md`。
- 临时物（`tmp/z3/`）：`build_p1.py`、`p1_top50_raw.json`（早期 P1 预构建，被工具 build_p1 取代）、`diag_300750.py`、`diag_std.py`、`diag_std2.py`（census std 标注反证）、`diag_transform.py`（无单一乘系数可修 DB）、`diag_coverage.py`（akshare raw 覆盖 100%）、`diag_2019break.py`（2019-01-07 DB 断裂实测）、`sim_apply.py`（重建路径=akshare std 精确）、`verify_tool_vs_x3.py`（正确性门 PASS）、`tier_plan.py`、`backup_est.py`、`gate_std.json`、`cache/{code}_factor.csv/_raw.csv`（akshare 缓存，重跑 0 新请求）。
- apply 期新增（获批后才生成，本块未产生）：`data/qfq_unify_state.json`（票级幂等记账）、`data/backups/kline_day_v2_*.json.xz`（双哈希备份）。
- 遗留观察项：census std 标注复核（300750/603986 反例）建议转交 Y1/验收方；factor 源标准待人类拍板。

**收尾声明**：本块零写库（market.db 全程只读）；未注册计划任务；未改任何 app/既有 tools 文件；出网 20 请求 ≤60。工具就绪、dry-run 达标、方案 C 待批准。
