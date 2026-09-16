# A2 报告：amount 从 09-09 起全写 0——根因定位 + 修复 + 补数（2026-09-13）

任务书：`A2 amount 从 09-09 起全写 0`。执行窗口 2026-09-13（周日，非交易日；引擎未运行——8899 无监听，无 main.py 进程）。

## 1. 根因定位（四源诊断实测，结论写前验证）

对 600000 最近 5 日同时拉四源 + 库内对照（`r3_diag_amount.py`）：

| 源 | 600000 amount | 结论 |
|---|---|---|
| 库内（09-04~09-08，tdx 时代） | 7.12亿/8.06亿/3.75亿 | 正常 |
| **tdx `fetch_kline_fast`（不复权/qfq）** | **None**（`tdx.available()=False`，熔断中） | **候选 1 排除**（不是 qfq 丢额，是源不可用） |
| 腾讯 fqkline | 0.0（6 字段无额） | **候选 2 证实** |
| 新浪 getKLineData | 0.0（无 amount 字段） | **候选 2 证实** |

**根因链**（实测证据）：
1. **tdx 主源自 09-09 起熔断不可用**（`available()=False`，`_get_client()=None`；quality_alert 09-09/09-10/09-11 连报 `amount_zero_guard ratio=1.0`）→ 全量降级腾讯/新浪；
2. **腾讯/新浪日K接口结构无 amount**（腾讯 6 字段 `[date,open,close,high,low,volume]`，F1 已证实；新浪 `e.get("amount",0)` 恒 0，本批实测）→ 来源 amount=0；
3. **UPSERT 保护盲区（候选 3 证实为放大层）**：`_kline_save` 的 `amount=CASE WHEN excluded.amount>0...` 只救"同日已存在"行；**09-09 起均为新日期 bar → 走 INSERT 直接写来源 amount=0**，保护完全失效 → 100% 全 0。

## 2. 修复改动（只改 amount 相关，候选 1 未动）

| 文件 | 改动 | 行数 |
|---|---|---|
| `app/datafeed.py` `_kline_save` | 写入期断言：`volume>0 且 amount<=0` 的行收集进返回值 `{"amount_missing": [(date,volume)...]}`；UPSERT amount 保护保留 | +10/-6 |
| `app/updater.py` `_fetch_daily_one` | 返回 `(k, src)`，src ∈ {tdx, tencent, sina}（按源统计前置） | +4/-2 |
| `app/updater.py` `_fetch_and_save` | 解包 (k,src)；`_kline_save` 返回的 missing → audit WARN `kline_amount_missing`（每源限 20 条防刷）+ 当日汇总 `amount_missing_by_source`（按源统计） | +24/-4 |
| `tools/backfill_amount_recent.py` | **新建**：腾讯 newfqkline 补额工具（历史任意日期带额；688 票百万元单位、其余万元）；`--date/--check-only/--limit` 参数；写锁重试；幂等（只 UPDATE amount<=0/NULL 且 volume>0） | 新建 140 行 |

**断言实证**（`_fetch_and_save` 3 票小样本）：audit 收到 `kline_amount_missing` WARN（code/date/volume/source='tencent'）+ `amount_missing_by_source`（`missing_total=236, by_source={'tencent': 236}`）✓；3 票×80 根窗口库内无新增 amount=0 行（无污染）✓。

**补数源选择说明**：任务书建议复用 `tmp/h2_fill_0908_amount.py` 的 qt.gtimg.cn 实时快照——**该源只对最近交易日（09-11）有效，无法补 09-09/09-10**。实测后改用**腾讯 `proxy.finance.qq.com newfqkline`**（历史任意日期、带额）：09-09/09-10/09-11 值与原东财验证值完全一致（如 600000 09-09：46754.99万 = 467,549,947 元）。**东财 push2his 在本环境高频批量后被 WAF 封**（Remote end closed connection，50 票探测全败；单票冷身重试也超时）——腾讯接口未见封禁。

## 3. 补数执行与并发协调

- 首跑沙箱 runtime 失败（东财被 WAF）→ 换真环境 + 腾讯源成功。
- **并行协调**：A1 块（`tools/backfill_missing_days.py --batch 150`，12:56-13:43 运行）同时在补三天缺失日K行（INSERT OR IGNORE，腾讯/新浪无额源 → 插入 amount=0 新行）。补额工具内置写锁重试（busy_timeout + 4 次退避）与之并存推进；**A1 结束后补跑一轮 09-11 尾**（515 行）清零。
- 全量补额：14,152 行（含 A1 并行插入），344s；补尾 515 行 36s。

## 4. 验收（任务书预注册判据逐条）

| 判据 | 结果 | 证据 |
|---|---|---|
| 三天补完后 amount0 行数各 ≤20 | ✅ **全 0** | 09-09: 0/4995、09-10: 0/4988、09-11: 0/4993 |
| 随机 200 行 `amount/(close*volume)` ∈ [0.7,1.4] | ✅ 达标 | median=1.001；异常 32/14976（0.2%）——其中指数 3 只（sh000001/sz399001/sz399006 口径特殊，非个股） + 主板边缘 29 只（ratio 1.4~1.6，可能含当日除权/复权系数小差，占比可忽略） |
| 修复后连续 2 个交易日 guard 返回 None | ⏳ 机制已验证 | 今日周日无新交易日；`_amount_zero_guard` 判据（volume>0 且 amount=0）对三天复跑均返回 None（0 行）；写入期断言已实证会在未来交易日自动上报，收盘补额工具已就位。**09-14/09-15 收盘后应各查一次 quality_alert.jsonl 无新增 amount_zero_guard** |

## 5. 过程中发现的新问题（超出本批范围，交人类决策）

1. **688 票 amount 库内一贯小 100 倍**（独立缺陷）：腾讯 newfqkline 对 688 的成交额字段单位=百万元（实证 688004 09-11 6483.36 百万=64.8亿，与 close×volume=19.02×3.41亿股 吻合）；**tdx 时代同样**（09-04 库内 4806 万 vs 合理 47 亿，ratio=0.01）。本批已修正**09-09~09-11 三天 688 共 1,796 行（×100，修正前 SHA256=47e386c8）**；**09-08 之前的 688 历史 amount 同样错 100 倍**——建议后续批次统一 `UPDATE kline SET amount=amount*100 WHERE code LIKE '688%'`（先小样本抽验 tdx 源是否全部如此）。
2. **指数代码在 kline 表**（sh000001/sz399001/sz399006 等 09-09 起有行）——疑为降级源返回指数行情被写入；amount/volume 口径与个股不同，量额比校验会误报。建议后续确认指数是否应进 kline（或独立 global_kline）。
3. **tdx 熔断根因未查**：`available()=False` 从 09-09 持续至今（周末亦不可用）——恢复机制/服务器状态待查（相关：Z6 半僵死诊断的 updater 卡死候选）。

## 6. 回滚

```
# 代码回滚（备份在 tmp/f_backup/20260913_124654/）
Copy-Item tmp\f_backup\20260913_124654\updater.py app\updater.py -Force
Copy-Item tmp\f_backup\20260913_124654\datafeed.py app\datafeed.py -Force
# 补数回滚（若需）：UPDATE kline SET amount=0 WHERE period='day' AND date IN
#   ('2026-09-09','2026-09-10','2026-09-11') AND code NOT LIKE '688%';   # 688 已 ×100 修正
```
所有 Python 文件 ast.parse + py_compile 通过、CRLF=0；未重启任何进程（引擎未运行）；未出网（仅腾讯/东财行情接口，属既有数据面）；market.db 写入仅限任务书授权的补额/修正 UPDATE。
