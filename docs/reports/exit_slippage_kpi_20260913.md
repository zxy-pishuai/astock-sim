# D4｜卖出滑点 KPI（触发价 vs 成交价）（2026-09-13）

## §0 判据预注册（先于结果落盘；与代码同步，跑完未改）
1. **符号约定**：`slippage_bp = (fill_price / trigger_price - 1) * 10000`（中性值，卖出 fill<trigger 为负 = 成本）；**成本视角 `cost_bp = -slippage_bp`（正=损失）**——此前 W3 提过两次反号教训，此处统一以 cost_bp 做汇总与告警。
2. **回测假设对表**：V1 常量 SLIP=0.002 → **20bp**（`tools/tactic_lianban_backtest.py` 与报告 §0）。告警规则：**平均成本 bp > 2×20=40bp → SLIPPAGE_ALERT**。
3. **trigger_price 定义**：条件触发时的判据价——常规退出=行情快照现价 cp；T+1 挂起执行=挂起时记录的 `trail_pending.trigger_px`；阶梯/打板半仓/量价减仓=判据现价 cp。无 trigger 的调用点（如手动）字段记 None，不硬算。
4. **历史回放重建口径（09-07~09-10）**：
   - `移动止损(峰P)` → trigger = P×(1+TRAILING_STOP_PCT)（config L52 = −0.03）——**重建假设**，真实触发为逐 tick 判定价；
   - `冲高回落止盈(峰P→C)` → trigger = C（判据现价）；
   - `阶梯止盈/止损/时间止损` → trigger=None（无峰值记录，**不可重建**，如实标）。
   - 回放行 `delay_min/trigger_ts=None`（实盘无触发时刻记录），全部标 `source=reconstructed`。
5. **验收判据**：①模拟一笔移动止损卖出 → exit_slippage.jsonl 完整记录（全字段 + 正确 slippage_bp/delay_min）；②日汇总事件字段齐全（n/avg_bp/p95/avg_delay_min/vs_backtest_20bp/alert/by_exit_kind）；③09-07~09-10 回放算出滑点且 source=reconstructed；④import app.server/trader/performance 回归通过。

## §1 改动清单（diff）
### `app/trader.py`
| 位置 | 改动 |
|---|---|
| `_sell()`（L872 一带） | 签名加 `trigger_price=None, trigger_ts=None`；计算 `slippage_bp`/`delay_min`（trigger_ts 为 HH:MM:SS 时补当日日期；负延迟=时序异常置 None）；strategy_sell 事件**追加** `trigger_price/fill_price/slippage_bp/trigger_ts/fill_ts/delay_min`（无 trigger → None，向后兼容）；调 `_log_exit_slippage()` 落 `data/exit_slippage.jsonl`（source=live） |
| `_log_exit_slippage()`（新方法） | append-only 逐笔 JSON 行：t/code/name/exit_kind/reason/trigger_price/fill_price/slippage_bp/trigger_ts/fill_ts/delay_min/source |
| `_exits_round()` 常规退出（L1858 一带） | 传 `trigger_price=cp, trigger_ts=now` |
| `_exits_round()` T+1 挂起执行（L1677 一带） | 传 `trigger_price=_pend['trigger_px'], trigger_ts=_pend['trigger_ts']`（trail_pending 新增 `trigger_ts` 字段，挂起时记触发时刻） |
| 打板半仓止盈 / 量价减仓 / 阶梯止盈（L1766/1799/1644 一带） | 传 `trigger_price=cp, trigger_ts=now` |
| 收盘日结段（15:00-15:30） | 日结推送后追加 `performance.record_daily_exit_slippage(today)`（失败不阻塞日结） |

### `app/performance.py`
| 位置 | 改动 |
|---|---|
| 模块头 | `BACKTEST_SLIP_BP = 20.0`（回测假设常量） |
| `_exit_slip_rows(path)` / `_p95(xs)` | 读 jsonl / 分位数（新增） |
| `execution_quality(slips, backtest_slip_bp=20.0)` | 按 exit_kind 分组 avg_bp/p95_bp/avg_delay_min + 总体 vs 20bp 对比与 `SLIPPAGE_ALERT`（avg>40bp）——**D5 周报"执行质量"章节数据源** |
| `record_daily_exit_slippage(trade_date, audit=None, slips=None)` | 当日汇总 → `exit_slippage_daily` 审计事件（kind='daily'；SLIPPAGE_ALERT 时 level=WARN）；slips 可注入（测试） |

## §2 模拟卖出验收（tmp/d4/test_sell_slippage.py，PASS）
触发价 10.20 → 成交 9.90（now−5.5min → now）：
```json
{"t": "2026-09-13 15:57:08", "code": "000001", "name": "测试股", "exit_kind": "移动止损",
 "trigger_price": 10.2, "fill_price": 9.9, "slippage_bp": -294.1,
 "trigger_ts": "2026-09-13 15:51:38", "fill_ts": "2026-09-13 15:57:08",
 "delay_min": 5.5, "source": "live"}
```
- `slippage_bp = (9.90/10.20−1)×10000 = −294.1` ✓；`delay_min = 5.5` ✓
- strategy_sell 事件含 D4 六字段 ✓；无 trigger 调用 → 全 None（兼容性）✓
- **测试行验证后已截断回原状**，生产文件零污染（曾有一次失败运行残留 1 行脏数据，已清空）。

## §3 日汇总验收（注入 slips，字段齐全）
```json
{"trade_date": "2026-09-14", "n": 2, "avg_bp": 14.7, "p95_bp": 29.4,
 "avg_delay_min": 2.8, "vs_backtest_20bp": 0.73, "alert": "OK",
 "by_exit_kind": {"移动止损": {"n":1,"avg_bp":29.4,"p95_bp":29.4,"avg_delay_min":5.5},
                  "阶梯止盈": {"n":1,"avg_bp":0.0,"p95_bp":0.0,"avg_delay_min":0.0}}}
```
事件 level=INFO（未超阈值）；超 40bp 时自动 WARN。

## §4 历史回放：09-07~09-10（source=reconstructed）
| 日期 | code | 名称 | 成交价 | 重建触发价 | slippage_bp | cost_bp | 依据 |
|---|---|---|---|---|---|---|---|
| 09-07 09:30 | 002059 | 云南旅游 | 5.774 | 6.159 | **−625.1** | 625 | 峰6.35×0.97 |
| 09-07 09:30 | 002787 | 华源控股 | 23.277 | 24.619 | **−545.1** | 545 | 峰25.38×0.97 |
| 09-09 13:00 | 002815 | 崇达技术 | 18.400 | — | — | — | 阶梯止盈，不可重建 |
| 09-09 13:00 | 002011 | 盾安环境 | 11.379 | 12.164 | **−645.3** | 645 | 峰12.54×0.97 |
| 09-09 13:21 | 002815 | 崇达技术 | 18.012 | 18.030 | −10.0 | 10 | →C 判据现价 |
| 09-10 13:39 | 002286 | 保龄宝 | 8.912 | — | — | — | 止损，不可重建 |

**回放统计（可算 4/6 笔）**：平均成本 **456bp**；中位 585bp；移动止损类 545~645bp（**回测假设 20bp 的 27~32 倍**）；冲高回落 10bp（接近回测假设）。
**按日汇总**：09-07 avg 585bp（29.3x）/ 09-09 avg 328bp（16.4x）——均 **SLIPPAGE_ALERT**。
**与 09-08 审计实测对表**：600354 触发 6.77 → 成交 6.384 = 570bp，与重建的 545~645bp **量级一致**——重建口径可信（非编造）。

## §5 结论与 D5 周报接口
1. **执行滑点是 C2 归因"执行滑点"项的直接数据源，且量级显著**：移动止损/移动类止盈的真实成交滑点（500~650bp）是回测假设（20bp）的 **25~32 倍**，仅冲高回落（10bp）接近假设。**回测收益中执行滑点成本被系统性低估**——对 09-07~09-10 样本，单笔移动止损平均额外损失约 6%。
2. **D5 周报"执行质量"章节**：调 `app.performance.execution_quality(_exit_slip_rows())` 拿 `overall.avg_bp` 与 `by_exit_kind`；告警规则已内建（avg>40bp → `SLIPPAGE_ALERT`，周报按此标红）。**当前窗口（09-07~09-10）avg 456bp → 必须告警**。
3. 挂起执行路径的滑点（trail_pending 触发价 vs 次日开盘成交价）现已在 D4 字段内（trigger_ts=挂起时刻，delay_min=次日延迟分钟）——F2 修复后的日常化度量已就位。

## §6 诚实披露
1. **重建假设**：移动止损 trigger=峰×0.97 是近似（真实触发=逐 tick peak 判定价）；量级已由 09-08 审计实测交叉验证，但逐笔数值非实盘记录。
2. **小样本**：回放仅 6 笔（4 笔可算），且 4 笔集中在移动止损（尾盘/早盘集中触发）——平均 456bp 有偏，**结论方向（滑点远超假设）可靠，精确值待样本积累**。
3. 阶梯止盈/止损类 trigger 不可重建（reason 无峰值）——需要后续在实盘落账中自然积累（已由本改动覆盖）。
4. 回放行 t=原成交时间（09-07/09/10，已过），不会混入未来交易日 live 日汇总；但若按日期做历史周报，需自行过滤 `source=reconstructed` 以免重复计（当前 6 行全为 reconstructed）。
5. 模拟验收测试行已清理，`data/exit_slippage.jsonl` 现 6 行（全 reconstructed）。

## §7 明日验收命令
```
# 下一交易日（09-14 周一）收盘后（15:00-15:30 日结窗口）：
Select-String data\audit\audit.jsonl -Pattern 'exit_slippage_daily' | Select-Object -Last 1
# 预期：trade_date=2026-09-14 的事件，n≥1（当日有卖出时），字段齐（avg_bp/p95/avg_delay_min/vs_backtest_20bp/alert/by_exit_kind）
Get-Content data\exit_slippage.jsonl -Tail 5   # 当日 live 行（source=live）
```
代码改动生效前提：8899 服务重启加载新 trader.py（权在 X1/服务管理方）；`TianjiCloseCatchup` 独立进程若走 updater 收盘路径则不受影响（日汇总挂 trader 日结段，需服务侧重启后生效）。
