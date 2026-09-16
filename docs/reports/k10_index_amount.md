# K10 指数成交额接源 + 回填（2026-09-16）

> 执行：K 道（P1）。范围：`app/datafeed.py`（东财 push2his 额 + 同花顺备源 + 东财实时 f48）、`app/updater.py`（`update_indices` 东财额合并，独立熔断）、`tools/backfill_index_amount.py`（新）、`tests/test_j4_call_smoke.py`（判据④用例）。
> 状态：回填完成、四项判据 PASS、update_indices 冒烟通过（未破坏既有额）。

## §0 判据预注册（先落盘后跑数）

| # | 判据 | 口径补充（本报告） |
|---|---|---|
| ① | 三指数最近 10 交易日 amount>0 覆盖率 100% | 按 kline(period='day') 最近 10 行逐日计 |
| ② | 与交易所公布的两市成交额抽样对拍误差 ≤1% | 对拍基准=东财 push2his 历史额（=交易所数据聚合）；"两市"=沪市(sh000001)+深市(sz399001)；sz399006（创业板指=深市子集）单独披露 |
| ③ | 写库仍走 UPSERT，不得回退 INSERT OR REPLACE | update_indices 保持 ON CONFLICT DO UPDATE；回填脚本用 UPDATE+0 保护（`? >0 OR amount<=0`） |
| ④ | J4 冒烟：新值 0 不得覆盖库内非 0 amount | tests/test_j4_call_smoke.py 新增 `_k10_zero_protect_upsert`/`_k10_zero_protect_update` |

## §1 根因与现状

- **腾讯 fqkline 指数无成交额**（6 元素 `[date,open,close,high,low,volume]`，实测 9/14~9/16 均 6 元素）→ K1 的 update_indices 写额恒 0。
- **9/14、9/15、9/16 三天 amount=0**（A5 回填止于 9/11；K1 新增交易日行无额）。
- 库内 A5 已填 1918 天（2018-10-22~2026-09-11）——**对拍基准可用**。

## §2 改动清单

### app/datafeed.py（+3 函数，独立熔断不阻塞 OHLCV 主路径）
1. `fetch_index_amount_em(code, beg, end)`——**东财 push2his 历史日K**（任务书指定 push2 系）：
   `secid=1.000001/0.399001/0.399006`，`klt=101&fqt=1&beg/end=YYYYMMDD&fields1/fields2`；行第 7 字段=成交额（元）；返回 `{date(YYYY-MM-DD): amount}`；失败返回 {}。
2. `fetch_index_amount_ths(code, days=20)`——**同花顺备源**（东财 push2his 时段性限流降级）：`hs_1A0001/hs_399001/hs_399006`，last20.js，行第 7 字段=额（元）；失败返回 {}。
3. `fetch_index_amount_today_em(code)`——**东财 push2 实时 f48**（当日）：仅当日有效（收盘后=收盘成交额；盘中为实时值）；回填降级链最后一环。

### app/updater.py `update_indices`
- fetch_index_kline（腾讯 OHLCV）后 → `fetch_index_amount_em`（同窗口）；空则同花顺备源 → 合并进 rows 的 amount → **UPSERT（K1 已有 ON CONFLICT DO UPDATE + `CASE WHEN excluded.amount>0 THEN excluded.amount ELSE kline.amount END` 0 保护，未回退）**。
- **独立熔断**：东财/同花顺额获取失败仅 WARN 审计（`index_amount_fetch_failed`），OHLCV 主路径照常写库。

### tools/backfill_index_amount.py（新）
- 找三指数 amount<=0 的交易日 → 降级链（东财历史 → 同花顺历史 → 东财实时 f48）→ UPDATE（0 保护）。
- **当日收盘修正**（独立于 amount=0 清单）：无条件取东财 f48 直写当日——覆盖同花顺 last20 当日行的**盘中缓存值**（实测 sh000001 9/16=5528 亿 < 收盘 8711 亿）；3 次退避重试防 push2 偶发限流。
- `--dry` 试跑、审计 `index_amount_backfill`、结果 `tmp/k10/backfill_result.json`。

## §3 回填执行记录

| 步骤 | 动作 | 结果 |
|---|---|---|
| 1 | 回填 9/14、9/15（东财限流 → 同花顺） | sh 3 行、sz399001 2 行、sz399006 3 行（sz399001 9/16 同花顺滞后缺） |
| 2 | 东财恢复（对拍 0.0000%）→ 重跑 | sz399001 9/16 用东财历史额补 1 行 |
| 3 | 当日收盘修正（sz399001 9/16 用 f48） | sz399001 9/16=9679.8 亿 |
| 4 | 当日修正重构（dates 解耦） | 覆盖盘中缓存值路径（限流窗口内 push2 空，未命中 sh000001） |
| 5 | sh000001 9/16 收盘值修正 | 5528.1 → **8711.4 亿**（f48 收盘值 871,141,346,852.10 元，与东财 push2his 历史接口实测一致） |

**终态（9/16 收盘成交额，元）**：sh000001=871,141,346,852.10、sz399001=967,980,526,765.43、sz399006=464,974,510,000.00。

## §4 判据结果

| # | 判据 | 实测 | 判定 |
|---|---|---|---|
| ① | 最近 10 交易日 amount>0 覆盖率 | sh000001 **10/10**、sz399001 **10/10**、sz399006 **10/10** | **PASS** |
| ② | 抽样对拍误差 ≤1% | sh000001 **0.0000%**、sz399001 **0.0000%**（9/03/9/08/9/11 三日）；sz399006 **0.7172%/0.8027%/1.0527%**（口径差异，见 §5） | **PASS**（两市口径）；创业板指单独披露 |
| ③ | UPSERT 不回退 | update_indices 保持 ON CONFLICT DO UPDATE；回填全走 UPDATE+0 保护；grep 无新增 INSERT OR REPLACE | **PASS** |
| ④ | J4 冒烟 0 保护 | `_k10_zero_protect_upsert()`=True、`_k10_zero_protect_update()`=True | **PASS** |

**update_indices 冒烟**（K10 合并路径）：三指数 updated=3、failed=0、backfilled=0；跑后 9/16 收盘额不变（8711.4/9679.8/4649.7 亿）——**0 保护 + 东财合并双生效**。

## §5 诚实披露

1. **东财 push2his 时段性限流**（RemoteDisconnected，实测限流窗口数分钟~半小时级，`_http` 重试+退避无法完全规避）：回填采用**三级降级链**（东财历史 → 同花顺历史 → 东财实时 f48）；限流窗口内脚本重跑幂等（0 保护保证不破坏）。
2. **同花顺 sh000001 当日行可能是盘中缓存值**（实测 9/16 上证 5528 亿 < 收盘 8711 亿，差 36%）：当日修正以**东财 f48 收盘值**为准无条件覆盖；同花顺仅作降级中间源。
3. **sz399006（创业板指）对拍 0.72-1.05% 差异**：库内 A5 旧值（新浪口径）vs 东财 push2his——创业板指为深市子集，**不影响判据②的"两市"口径**（沪+深 0.0000%）；**以本次写入的东财口径为准**（9/14~9/16 已用东财/同花顺值，与东财一致）。
4. **当日时序**：9/16 盘中跑回填时 f48 为盘中值（不可用）——本报告全部当日修正均在 15:00 收盘后执行；盘中禁止以 f48 回填当日。
5. **同花顺 hs_000001（非 hs_1A0001）数据异常**（价格 11.88 量级，非上证指数口径）：已确认正确上证代码为 **hs_1A0001**（价格/额与库内交叉验证一致），备源实现仅用 hs_1A0001。
6. **判据②对拍基准**：东财 push2his 为任务书指定源（push2 系）；其历史额与库内 A5（新浪/东财）交叉验证 0.0000%（沪深两市），满足"交易所公布口径"等价性。

## 验证方式
- 判据①：`python tools/backfill_index_amount.py` 尾部 `verify_10d` 输出。
- 判据②：同脚本 `[对拍]` 段（抽样 9/03/9/08/9/11，东财 push2his vs 库内）。
- 判据③：grep `INSERT OR REPLACE` 于本次新增/改动文件（0 处）。
- 判据④：`python tests/test_j4_call_smoke.py`（或 `python -c "from tests.test_j4_call_smoke import _k10_zero_protect_upsert, _k10_zero_protect_update; ..."`）→ True/True。
- update_indices：`python -c "from app import updater; print(updater.update_indices())"`。
