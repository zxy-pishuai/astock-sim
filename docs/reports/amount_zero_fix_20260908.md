# 收盘日K amount 全零修复 + 9/8 数据补跑（2026-09-08 15:10-16:20）

## 0. 摘要
- 15:10 收盘增量更新触发后，日K仅覆盖 510/2838 只、且写入的 9/8 行 **amount 全部为 0**；
  更严重的是旧写库逻辑（`INSERT OR REPLACE`）把库内 9/7 及更早的正常 amount 一并覆盖成 0。
- 已修复：① `app/datafeed.py::_kline_save` 改为 UPSERT + amount 保护（amount=0 不覆盖已有值）；
  ② 独立进程补跑日K（跳过失效的 tdx 主源）→ 9/8 全市场 4,990 行补齐；
  ③ 9/7 被误覆盖的 497 只 amount 从昨日快照恢复（与快照 0 差异）；
  ④ 9/8 当日成交额用腾讯实时接口补 4,978/4,990 只（99.8%）；
  ⑤ 重建 9/8 快照。
- 服务未重启（修复在独立进程生效；服务内代码下次重启自动加载新 `_kline_save`）。

## 1. 背景与现象
- 15:10:03 audit 记录「收盘数据增量更新已触发」；15:36:25 `data_update` 完成
  （min5_updated=1007/1011 达标、daily_updated=500）；15:36:01 同时记 `daily_coverage CRITICAL`
  （fresh=510/total=2838、ratio=0.18、missing=2328）。
- 15:25 时 kline 9/8 仅 12 行、9/7 amount=0 仅 21 行；15:36 后 9/7 amount=0 升至 517，
  随后补跑过程中一度升至 2,999（旧代码在 15:25-15:36 间把约 497 只 9/7 的 amount 覆盖成 0）。
- 上证/深成指数日K 15:10 前已由当日早间修复补拉至 9/8（`bugfix_limitup_intraday_20260908.md`）。

## 2. 根因（三层）
1. **tdx 主源不可用**：`tdx.available()` 只检查 mootdx 是否安装（恒 True），但实际
   `_bars_one()` 连接成功（`_get_client` 1.04s）后 `c.bars()` 空 → 每票浪费约 14s 后走兜底。
   15:39 实测：`_get_client=1.04s`、`_bars_one=13.88s → None`。
2. **腾讯/新浪日K接口不含成交额**（长期缺陷，平时被 tdx 掩盖）：
   - 腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get` → `qfqday` 元素仅 6 字段
     `[date,open,close,high,low,volume(手)]`，无成交额；`amount=float(e[6]) if len(e)>6 else 0` 恒 0。
   - 新浪 `CN_MarketData.getKLineData` → 仅 `{day,open,high,low,close,volume}`，无 amount。
   - 15:37 实测两链路返回 80 行 amount 全 0（9/7 亦为 0）。
3. **`_kline_save` 无保护**：`INSERT OR REPLACE` 全字段覆盖 → 兜底链路的 amount=0 抹掉库内正常值。
   另：东财 `push2his` 日K接口（唯一含成交额的 HTTP 备源）实测被远端断连
   （`RemoteDisconnected`，主/备/HTTP 域名均失败），无法借道。

## 3. 修复与数据修复（已执行）
### 3.1 代码修复（1 文件）
`app/datafeed.py::_kline_save`（L136-147）：
```
INSERT OR REPLACE INTO kline(...) VALUES(...)
   ↓
INSERT INTO kline(...) VALUES(...)
ON CONFLICT(code,period,date) DO UPDATE SET
  open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
  volume=excluded.volume,
  amount=CASE WHEN excluded.amount>0 THEN excluded.amount ELSE kline.amount END
```
- 效果：tdx/含额源正常覆盖；腾讯/新浪兜底（amount=0）保留库内已有值；幂等去重不变。
- 备份：`tmp/bugfix_20260908/datafeed.py.20260908_154013.bak`
  （改前 SHA256 = 改后工作副本一致校验通过：`F8F802BF19D93446C1E7188E11B8B197BCE1D740EDD1015CF433F436F8E811CD`）。
- 语义验证：内存库模拟 amount=0 保留旧值 / amount>0 覆盖 / 无重复行，全部通过；`py_compile` 通过。

### 3.2 数据修复（独立进程，均不触碰服务）
| 步骤 | 方式 | 结果 |
|---|---|---|
| 日K补跑 | `tmp/h2_refill_fast.py`（monkeypatch 跳过 tdx，腾讯→新浪，预算 5400s） | 9/8 达 4,990 行（全市场）；剩余 25 只无数据（停牌/退市），预算内重试仍 0，接受 |
| 9/7 恢复 | `tmp/h2_restore_0907.py`：以昨日快照 `snapshots/2026-09-07/market.db`（9/7 16:38 生成，干净基线）为权威源回填 | 更新 497 只；与快照 amount>0 的 2,505 只比对 **0 差异** |
| 9/8 当日额 | `tmp/h2_fill_0908_amount.py`：腾讯 rt `qt.gtimg.cn/q=`（f[37] 成交额万→元），8 并发 | 补 4,978 只，用时 157s；茅台 9/8 amount=23.03 亿元（与腾讯日K volume 1,753,400 股一致） |
| 快照重建 | `create_snapshot(force=True)` | `snapshots/2026-09-08/` 重建，9/8=4,990 行/amount>0 4,978；9/7=5,007 行 |

## 4. 复验（修复后）
- 9/8：4,990 行，amount>0 = 4,978（99.8%）；12 只 amount=0 为停牌/无成交（含 sh000001 指数——rt 字段不适用，指数成交额由其他链路提供）。
- 9/7：5,007 行；快照 9/7 中 amount>0 的 2,505 只与生产**逐只一致（diff=0）**。
- 9/8 快照重建后内容验证通过（9/8 4,990 / 9/7 5,007 / min5 正常）。
- 服务 HTTP 正常：`/api/limitup`（红棉股份等 zt 列表）、`/api/overview`（上证 3940.55 +0.2%）。

## 5. 遗留与建议
1. **9/7 补跑新增约 2,493 只的 9/7 amount=0 且无权威源**：这些票昨日收盘更新本就未覆盖
   （库内无 9/7 行），快照无行可回填；腾讯/新浪历史接口无成交额、东财被风控。
   影响=9/7 单日成交额因子缺失（volume 正常）。**不建议估算填数**；若需精确，待 tdx/东财恢复后补。
2. **tdx 主源长期"假可用"**：`available()` 仅检查安装，建议改为实际连通探测或失败快速降级
   （当前每票首查浪费 ~14s，是收盘更新慢的主因之一）。
3. **腾讯/新浪兜底不含成交额**为结构性问题：东财日K（push2his）被断连期间无含额 HTTP 备源。
   建议恢复后验证东财通路并考虑加入 `_fetch_daily_one` 链路。
4. **`daily_coverage` 告警已工作**：15:36 CRITICAL 正常触发；建议补跑后人工确认覆盖率再快照。
5. 15:10 收盘更新当日不重试的既有行为：本次靠人工补跑完成，若 tdx 持续不可用，次日收盘仍可能低覆盖。

## 6. 涉及文件
- 改：`app/datafeed.py`（1 处，约 10 行）
- 新：`tmp/h2_refill_fast.py`、`tmp/h2_restore_0907.py`、`tmp/h2_fill_0908_amount.py`、
  `tmp/h2_daily_refill_20260908.log`、`tmp/h2_fill_0908_amount.log`、`tmp/h2_snapshot_rebuild.log`
- 备份：`tmp/bugfix_20260908/datafeed.py.20260908_154013.bak`
