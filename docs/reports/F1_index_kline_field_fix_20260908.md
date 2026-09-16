# F1 指数日K 字段错位修复（2026-09-08）

## 0. 摘要
腾讯 fqkline 接口数组序为 `[date, open, close, high, low, volume]`（6 元素、无 amount），
代码按 `[date, open, high, low, close, volume, amount]` 位置解析 → 三大指数日K 全量错位：
`high`=真实收盘、`low`=真实最高、`close`=真实最低、`amount` 恒 0。
已修复解析、轮换库内 1,815 行错位数据、补齐深成/创业板指停更、加通用 K 线断言、
R2 判定前后对比（**0 天差异**）。服务未重启（改动在下次启动生效；当前服务 INDEX_TIMING_ENABLED=False，不触发指数写入路径）。

## 1. 证据（修复前，2026-09-08 15:5x 实测）
| code | 行数 | high<low | amount>0 | max_date |
|---|---|---|---|---|
| sh000001 | 615 | 586 | 0 | 2026-09-08 |
| sz399001 | 600 | 568 | 0 | 2026-08-18 |
| sz399006 | 600 | 584 | 0 | 2026-08-18 |

抽样 2026-09-08 sh000001（修复前）：`open=3935.55 high=3946.53 low=3951.32 close=3935.55`
→ high(3946.53) < low(3951.32)，实锤错位；反推 src=(open 3935.55, close 3946.53, high 3951.32, low 3935.55) 自洽。

## 2. 根因
两处写入路径同一错误映射（`float(e[2])→high, float(e[3])→low, float(e[4])→close`）：
- `app/index_timing.py::_fetch_online()`（2026-09-08 早上"指数陈旧补拉"修复后开始实际写库 → 暴露既有解析 bug）
- `tools/fetch_index_kline.py::fetch_index()`（历史 600 天入库）

腾讯接口元素序：`e=[date, open, close, high, low, volume]`；无 amount 字段 → 旧代码
`float(e[6]) if len(e)>6 else 0` 恒 0。

## 3. 代码修复（3 文件）
1. **`app/index_timing.py`**
   - `_fetch_online` 映射改为 `open=e[1], high=e[3], low=e[4], close=e[2], volume=e[5]*100, amount=0.0`
     （amount 显式写 0 + 注释"腾讯 fqkline 无 amount，指数成交额需另接源"）。
   - 新增通用断言 `_sane_rows(rows)`（个股/指数共用）：拒绝
     `high<low` / `high<max(open,close)` / `low>min(open,close)` / `volume<0` /
     `0<volume<100` / `0<amount<1000`；失败打印 WARN 不写库。`_fetch_online` 写库前调用。
2. **`tools/fetch_index_kline.py`**
   - 同样映射修复 + `from app.index_timing import _sane_rows` 过滤。
   - main() 增加陈旧度提示（max_date 落后自然日 >5 打印警示）——保证三指数不落后。
3. **`tools/repair_index_kline_fields.py`**（新建，一次性修复脚本）
   - 三值轮换 `new_high=old_low; new_low=old_close; new_close=old_high`（open/volume/date 不动）。
   - 选行条件=`high<low OR high<max(open,close) OR low>min(open,close)`（覆盖 src 当日
     high==close 的平顶错位行）；修复后正确行不满足条件 → **幂等**（重复执行 0 行）。
   - 内存算好新值 → 单事务 `executemany` 按 rowid 批量 UPDATE → 立即校验。

## 4. 数据修复（先备份）
- 备份：`data/market.db.bak_F1_20260908`（1,573,601,280 B）。原库被运行中服务持有无法哈希，
  以备份文件 SHA256 完整性代替：`BA5428029D91BBB341AB6BC4ADE739362B5304F43A516AD3FD5BFD683C3A78C7`。
- 修复执行：
  - 第一轮（high<low）：**1,738 行**轮换
  - 第二轮（越界行，src 当日 high==close 的平顶行）：**77 行**轮换
  - 合计 1,815 行；再跑幂等验证 = 0 行。
- 随后以修正后的 `fetch_index_kline` 重拉 600 天（3 指数 × 600 = 1,800 根）：
  覆盖为权威正确值，且 **sz399001/sz399006 从 2026-08-18 补齐到 2026-09-08**。

## 5. 校验（全部通过）
1. 三指数 `high<low` 计数 = 0
2. 三指数 `high<max(open,close) OR low>min(open,close)` 计数 = 0
3. **新浪实时交叉核对 2026-09-08（逐位吻合）**：
   | 指数 | 库内 open/high/low/close | 新浪 open/high/low/最新 |
   |---|---|---|
   | sh000001 | 3935.55 / 3951.32 / 3925.72 / 3940.55 | 3935.55 / 3951.32 / 3925.72 / 3940.55 |
   | sz399001 | 13782.86 / 13843.92 / 13655.84 / 13703.21 | 13782.86 / 13843.92 / 13655.84 / 13703.21 |
   | sz399006 | 3394.35 / 3418.48 / 3346.71 / 3359.72 | 3394.35 / 3418.48 / 3346.71 / 3359.72 |
4. `py -3.13 -m py_compile` 三文件通过。

## 6. R2 指数择时判定：修复前后对比
以备份库（修复前错位数据）vs 当前库（修复后真实数据），重算
`engine.py:377` 口径 `close > MA20(close)`（sh000001，595 个判定日）：
- 修复前：站上 344/595 = 57.8%
- 修复后：站上 344/595 = 57.8%
- **判定变化天数 = 0**（最低价序列与收盘价序列相对 MA20 的位置关系几乎一致）

结论：**未发现"错位导致过去回测仓位被长期压制"**——R2 判定在修复前后一致；
但指数 K 线 OHLC 本身已修正为真实行情，波动率/回撤等依赖 high/low 的指标恢复正确。

## 7. 遗留与建议
1. **指数 amount 恒 0**（腾讯 fqkline 无成交额）：engine R2 只用 close，无影响；若面板需指数成交额，
   另接腾讯 rt `qt.gtimg.cn/q=sh000001`（f[37] 万元）或东财。
2. `_sane_rows` 断言仅覆盖指数两条写入路径；个股写入路径（`app/datafeed.py` 等）未在本单范围
   （F1 只改列出的 3 文件），如需全链路断言另开单。
3. `tools/fetch_index_kline.py` 未挂入收盘更新链路（本单未动 updater）；靠手动/计划执行保证追平，
   当前三指数均已到 2026-09-08。

## 8. 涉及文件
- 改：`app/index_timing.py`、`tools/fetch_index_kline.py`
- 新：`tools/repair_index_kline_fields.py`
- 备份：`data/market.db.bak_F1_20260908`
- 报告：本文件
