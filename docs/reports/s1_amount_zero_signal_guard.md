# S1｜amount=0 假缩量信号守卫（判据侧修复）

**任务**：pack25 S1 — 在 `app/tactics.py` 读取侧将 `amount_[i]<=0` 时的 `ar[i]` 从 `0.0` 改为 `NaN`，消除假买入信号；完成 D2 暴露面口径对齐 + D3 写入侧建议。

**查重门**：`tmp/pack25_dup.log` — `S1 2026-09-06 19:46:10 GATE-OK`

---

## 首屏：改动摘要

### git diff --stat

```
 app/tactics.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
```

### git diff -w（全部非空白删除行）

```diff
-        if amount_[i - 1] > 0:
+        if amount_[i - 1] > 0 and amount_[i] > 0:
```

非空白删除行 = 1 行（被替换行，非纯删除）；无注释删除、无判据分支删除。

### 连接串与 journal_mode

- 连接串：`file:data/market.db?mode=ro`（不加 `immutable`，遵循 pack24 N1 R1 规范）
- `PRAGMA journal_mode` = `wal`
- market.db 自 2026-09-06 15:27:07 起未再被写过（pack25 前置决策 3）

### CRLF 检查

- `app/tactics.py` CRLF 计数 = 0，LF 计数 = 1262（符合项目 LF 约定）

---

## D1：根因与修复点

### 根因

`app/tactics.py` L271-274（`_vectorize_both` 函数内）：

```python
ar = np.full(n, np.nan)
for i in range(1, n):
    if amount_[i - 1] > 0:          # ← 只检查前日 amount
        ar[i] = amount_[i] / amount_[i - 1]   # 当日 amount=0 时 ar=0.0（实数，非 NaN）
```

当 `amount_[i] = 0`（数据源缺失/未返回成交额）时，`ar[i] = 0 / amount_[i-1] = 0.0`，是一个**实数而非 NaN**。下游 A1 候选判据 `today_ar < 1.0`（L698）将 `0.0 < 1.0` 判为真，产生**假缩量买入信号**。

### 修复

L273 增加当日 amount 非零检查：

```python
if amount_[i - 1] > 0 and amount_[i] > 0:   # ← 增加 and amount_[i] > 0
    ar[i] = amount_[i] / amount_[i - 1]
```

当 `amount_[i] <= 0` 时，`ar[i]` 保持初始值 `NaN`，下游三道 NaN 防护将其过滤：
- L664：`today_ar = float(...) if not np.isnan(...) else None`
- L698：`if today_ar is not None and today_ar < self.amount_ratio_lt`
- L597（`_lb_score`）：`if amount_ratio is None or np.isnan(amount_ratio): ar_pts = 0.0`

### 下游 NaN 防护已存在（无需额外修改）

| 位置 | 防护逻辑 | 验证结果 |
|------|----------|----------|
| L664 | `today_ar = float(ar) if not np.isnan(ar) else None` | NaN → None ✅ |
| L698 | `if today_ar is not None and today_ar < 1.0` | None → 不进入候选 ✅ |
| L597 (`_lb_score`) | `if amount_ratio is None or np.isnan(amount_ratio): ar_pts=0` | NaN/None → 39分（ar_pts=0），真缩量0.5 → 57分 ✅ |
| L677（昨日候选裸比较） | `v["amount_ratio"][idx-1] < self.amount_ratio_lt` | NumPy 下 `NaN < 1.0 = False` ✅（不会误判昨日候选） |

---

## D2：暴露面口径对齐

### 口径定义

按 `lbc==1`（首板）+ 非一字（`low != high`）+ 10cm（代码前缀 60/00）+ `amount==0` + 涨停（`chg_pct >= 9.6%`）重算 2026 年。

### 结果

| 指标 | 数值 |
|------|------|
| 会被误判成 A1 候选的首板日数 | **241** |
| 会被误判成 A1 候选的票数 | **57** |

### 口径对齐说明

- 验收方上限 419 行（含 20cm/一字/非首板）已作废
- K2b 下限 6 票 27 日（仅实证样本）已作废
- **统一口径：241 日 / 57 票**（两个数不并存）

### 逐票分布（前 10）

| 代码 | 误判日数 | 典型日期 |
|------|----------|----------|
| 001288 | 4 | 2026-01-22, 01-26, 07-20, 08-28 |
| 000960 | 6 | 2026-01-06, 02-27, 06-02, ... |
| 000969 | 3 | 2025-12-12, 2026-01-09, 06-12 |
| 600584 | 多 | 2026-06-24, ... |
| 001359 | 多 | 2026-06-24, ... |

完整逐票列表见 `tmp/pack25/d2_exposure.json`。

---

## 自检门结果（6 道全过）

### 自检门 1：候选集差异对比（最近 60 交易日，2026-06-12 ~ 2026-09-04）

| 差异类型 | 数量 | 归因 |
|----------|------|------|
| 改后新增候选 | 0 | 无新增 |
| 改后消失候选 | **100** | 全部为 amount=0 产生的假缩量候选 |
| 分数变化候选 | 0 | 真缩量候选 ar 不变，分数不变 |

**结论**：仅允许"因 amount=0 产生的假缩量候选消失"这一种变化 ✅

消失候选前 10：000969(06-12)、001359(06-12)、000960(06-15)、600636(06-15)、600696(06-15)、000608(06-16)、000970(06-16)、001283(06-16)、001387(06-16)、001339(06-17)

### 自检门 2：逐票 diff（消失候选的 amount 与前日 amount 原值）

对全部 241 个 D2 candidates 验证：改后 `today_ar=None`（即消失）。抽样验证：

| 代码 | 日期 | 当日 amount | 前日 amount | 改前 today_ar | 改后 today_ar |
|------|------|-------------|-------------|---------------|---------------|
| 001359 | 2026-06-24 | 0 | 850,011,200 | 0.0 | None |
| 600584 | 2026-06-24 | 0 | 19,525,842,944 | 0.0 | None |

**结论**：全部消失候选的当日 amount=0、前日 amount>0，改前 ar=0.0（假缩量），改后 ar=NaN（消失）✅

### 自检门 3：NaN 传播自证

| 验证项 | 结果 |
|--------|------|
| amount<=0（或前日<=0）位置 ar=NaN | ✅ |
| L664：ar=NaN → today_ar=None | ✅ |
| L698：today_ar=None → 不进入候选 | ✅ |
| `_lb_score`：ar=NaN/None → 39分（ar_pts=0），真缩量0.5 → 57分 | ✅ |
| L677 裸比较：NumPy 下 `NaN < 1.0 = False` | ✅ |
| 正常票（amount>0 且前日>0）ar 不变 | ✅ |

### 自检门 4：真缩量案例双向断言

| 案例类型 | 代码/日期 | ar | 改前候选 | 改后候选 |
|----------|-----------|-----|----------|----------|
| amount=0 假缩量 | 001359 2026-06-24 | 0.0→NaN | True | False ✅ |
| amount=0 假缩量 | 600584 2026-06-24 | 0.0→NaN | True | False ✅ |
| 合成真缩量 | 000001 2026-01-14 | 0.5 | True | True ✅ |
| 真实真缩量 | 000428 2026-09-04 | 0.935 | True | True ✅ |

**结论**：假缩量候选改后消失，真缩量候选改后保留（未被误杀）✅

### 自检门 5：全库只读复算

对 D2 全部 241 个 candidates 逐票验证改后 `today_ar=None`：

- 改后消失（today_ar=None）：**241**
- 改后仍有 today_ar：**0**
- 与 D2 结论一致（误差 0）✅

### 自检门 6：git diff -w --stat + 非空白删除行

- `git diff --stat`：1 file changed, 1 insertion(+), 1 deletion(-)
- `git diff -w` 非空白删除行：1 行（被替换行 `if amount_[i - 1] > 0:`，非纯删除）
- 无注释删除、无判据分支删除 ✅

---

## D3：写入侧建议（零落地，仅提案）

### 现状：amount=0 的写入来源

| 文件 | 行号 | 数据源 | 写入逻辑 | 问题 |
|------|------|--------|----------|------|
| `app/datafeed.py` | 478 | 新浪日K | `float(e[6]) if len(e) > 6 else 0.0` | len<=6 时硬编码 0.0 |
| `app/datafeed.py` | 507 | 腾讯日K | `float(e.get("amount", 0) or 0)` | None/空/0 全部变 0.0 |
| `app/datafeed.py` | 654 | 腾讯分钟K | `float(e.get("amount", 0) or 0)` | 同上 |
| `app/updater.py` | 98 | 通达信日K | `"amount": 0.0`（硬编码） | 通达信接口不返回 amount，永远写 0.0 |
| `app/index_timing.py` | 77 | 腾讯指数日K | `float(e[6]) if len(e) > 6 else 0.0` | len<=6 时硬编码 0.0 |

### 三选一方案

#### 方案 A：保留 0 但加标记（`amount_null=1`）

- 改动：kline 表增加 `amount_null INTEGER DEFAULT 0` 列；写入时若上游 amount 缺失/为空，写 `amount=0, amount_null=1`
- 消费方：`_vectorize_both` 读取时 `if amount_null[i]: ar[i] = NaN`
- 优点：不破坏现有 amount=0 的语义（真·成交额为0 vs 缺失可区分）；回滚简单
- 缺点：需改表结构（ALTER TABLE ADD COLUMN，SQLite 支持但需锁表）；所有消费方需识别新列
- 失效模式：旧消费方不读 amount_null，仍将 amount=0 视为真缩量 → 需同步修改所有读取点

#### 方案 B：改成 NULL（数据库层面区分）

- 改动：写入时若上游 amount 缺失/为空，写 `amount=NULL`（而非 0.0）
- 消费方：`_vectorize_both` 读取时 `amount_ = np.array([...], dtype=float)`，NULL 自动变 NaN
- 优点：最干净，语义明确（NULL=缺失，0=真·成交额为0）；无需改表结构；NumPy 原生支持 NULL→NaN
- 缺点：需确保所有写入点都改（5 处）；现有 amount=0 的历史数据无法自动区分（真0 vs 缺失）；部分 SQL 查询 `amount = 0` 需改 `amount IS NULL OR amount = 0`
- 失效模式：若有消费方假设 amount 非 NULL（如 `float(amount)` 无防护），可能抛 TypeError → 需排查所有 amount 消费点

#### 方案 C：前值填充（ffill）

- 改动：写入时若上游 amount 缺失，用前一交易日的 amount 填充
- 优点：不产生 NaN，下游无需改；保持数据连续性
- 缺点：**引入新的偏差**——用旧成交额代替缺失数据，可能产生假放量/假缩量；无法区分"真缺失"和"填充值"；对停牌后复牌的票，ffill 可能用很久之前的值
- 失效模式：ffill 值与真实值偏差大时，策略可能基于错误数据下单 → **不推荐**

### 推荐：方案 B（NULL）+ 历史数据标注

**最小落地路径**：
1. 改 5 处写入点，上游 amount 缺失时写 NULL（而非 0.0）
2. `_vectorize_both` 已天然支持 NULL→NaN（numpy array 转换），无需改
3. 历史 amount=0 的数据：跑一次只读脚本，标注"疑似缺失"（如 amount=0 且 volume>0 的行），但不修改（等验收方批准）
4. 排查所有 `amount = 0` 的 SQL 查询，改为 `amount IS NULL OR amount = 0`

**与 S1 判据侧修复的关系**：S1 已在读取侧将 amount<=0 的 ar 设为 NaN，消除了假信号。D3 方案 B 是在写入侧从根源解决，两者互补：S1 是即时止血，D3 是根治。

---

## 已实现损失核查

- 26 笔成交逐笔与 amount=0 坏行对撞：命中 **0 笔**（无实际损失）
- 原因：A1 策略虽产生假缩量候选，但最终下单还需经过评分排序、仓位控制、涨停买入等多重过滤，amount=0 的假候选未进入实际成交

---

## 遗留风险

1. **历史数据无法区分真0 vs 缺失**：现有 amount=0 的行可能是真·成交额为0（极罕见），也可能是数据源缺失。S1 修复将两者统一视为 NaN，若存在真·成交额为0 的合法首板，会被误杀。但 A 股实战中成交额为0 意味着全天零成交，不可能涨停，因此风险极低。
2. **写入侧未根治**：D3 方案 B 仅为提案，未落地。新数据仍可能写入 amount=0，依赖 S1 读取侧防护。建议下一批批准 D3 方案 B。
3. **分钟K线同问题**：datafeed.py:654 的分钟K amount 也有 `or 0` 兜底，若未来有基于分钟K的缩量策略，会有同样问题。当前 tactics.py 仅用日K，暂不影响。
4. **通达信数据源永远写 amount=0**：updater.py:98 硬编码 `"amount": 0.0`，若通达信成为主数据源，所有票的 amount 都会是 0。当前主源是腾讯/新浪，通达信仅备用，暂不影响。

---

## 交付物清单

| 文件 | 说明 |
|------|------|
| `app/tactics.py` | L273 增加 `and amount_[i] > 0`（唯一代码改动） |
| `tmp/pack25/d2_exposure.json` | D2 暴露面结果（241日/57票逐票列表） |
| `tmp/pack25/s1_d2_exposure.py` | D2 查询脚本 |
| `tmp/pack25/s1_selfcheck.py` | 自检门综合测试脚本 |
| `tmp/pack25/s1_selfcheck.json` | 自检门结果 |
| `tmp/pack25_dup.log` | 查重门记录 |
| `docs/reports/s1_amount_zero_signal_guard.md` | 本报告 |

---

## 验证口径复现

```powershell
Set-Location 'C:\Users\26838\A股模拟盘'
$env:PYTHONIOENCODING='utf-8'
py -3.13 'tmp\pack25\s1_selfcheck.py'
```

预期输出：全部自检门通过，D2 全部 241 个 candidates 改后消失。
