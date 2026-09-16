# C5 卖出结构化审计事件 + D5 周报对拍修复（2026-09-13）

## §0 任务书判据与验收
1. 新增 `strategy_sell` 结构化审计事件，字段与 `strategy_buy` 对称：
   `code/name/price/qty/fee/pnl/hold_days/reason/exit_kind`
2. 修周报对拍：事件名集合 `{manual_buy, strategy_buy}` / `{manual_sell, strategy_sell}`，
   分别列"结构化事件覆盖率"与"缺失明细"
3. 混合口径：新事件上线前 account.json trades 兜底，上线后 audit 交叉校验，
   差异 > 阈值告警
4. 验收：模拟卖出 → audit 出现带全字段 `strategy_sell`；周报重跑数字正确
5. 与 D2 不重叠：本单只改 `app/trader.py` 事件本体；D 道告警链路不碰

## §1 事实勘误（任务书 vs 实测）
- **任务书称"卖出没有结构化事件，只有 trading_event SELL 文本"** → 实测勘误：
  `data/audit/audit_20260910.jsonl` 归档里已有 **strategy_sell=61 条、strategy_buy=63 条、
  manual_buy=5 条**（旧版 strategy_sell 字段不全：仅 code/name/price/qty/fee/pnl/
  strategy）。任务书看到"0/13"的真正原因是 **D5 周报只匹配 manual_sell 事件名** +
  **只读活动 audit.jsonl 不读归档**（B4 每日归档后事件全在归档文件里）。
- 任务书核心诉求不变且已落地：**补全 strategy_sell 字段**（hold_days/reason/
  exit_kind）+ **周报对拍修复**（事件名集合 + 读归档 + 混合口径）。

## §2 改动清单（diff 级）

### 2.1 `app/trader.py`（卖出落账处 `_sell`，L815-822 区域）
- 新增模块级 `_EXIT_KIND_RULES` + `_exit_kind_of(reason)`（TradingEngine 类之前，
  不劈类；`_sell` 保持类内方法）。
- `_sell` 内 `audit.record("position", "strategy_sell", ...)` 补全字段：
  `hold_days=int(pos.get("days") or 0)`、`reason`（完整文本）、
  `exit_kind=_exit_kind_of(reason)`；保留 `strategy` 兼容字段。
- **exit_kind 归类规则**（报告声明口径）：
  - 主枚举 8 项：移动止损/冲高回落止盈/阶梯止盈/时间止损/止损/两点半战法/
    打板半仓止盈/手动
  - 归并：超时退出(MAX_HOLD_DAYS)→**时间止损**（时间维度退出）；
    打板低开止损→**止损**；普通止盈(TAKE_PROFIT_PCT 全清)→**冲高回落止盈**
    （价格高点止盈语义，reason 原文可区分）
  - 未命中→reason 原文截断（保证审计字段非空可追溯）

### 2.2 `tools/live_vs_backtest_weekly.py`（D5 周报脚本）
- 常量：`BUY_EVENTS=("manual_buy","strategy_buy")`、
  `SELL_EVENTS=("manual_sell","strategy_sell")`、上线日
  `BUY_EV_ON=2026-09-08` / `SELL_EV_ON=2026-09-13`
- `load_audit_counts()`：
  - 事件名集合 2×2（原来只收 manual_* 两名字）
  - **新增 `_audit_files()` 遍历 audit 目录全部 jsonl（活动+归档）**——
    这是对拍数字从 0/13 到 12/16 的关键修复
- 交叉核对：命中 buy/sell 任一事件名即覆盖；混合口径拆分
  `buys/sells_pre_on`（上线日前 account 兜底）与 `post_miss`（上线日后真缺失，
  **>0 即告警**）
- 输出：覆盖率（"买入 X/N 有结构化事件（manual_buy/strategy_buy）"）+
  缺失明细（仅上线后真缺失逐笔列）+ 混合口径说明；JSON 增 `audit_meta` 段

## §3 验收

### 3.1 验收①：模拟卖出 → 全字段 strategy_sell（audit 写 tmp，不碰生产）
命令：`py -3.13 tmp\c5\verify_sell.py`（patch `C.DATA_DIR` 到 tmp/c5_audit_test）
```json
{"t":"2026-09-13 15:37:12","kind":"position","event":"strategy_sell","level":"SELL",
 "code":"600000","name":"测试票","price":10.5,"qty":200,"fee":6.05,"pnl":93.95,
 "hold_days":3,"reason":"移动止损(峰10.5)","exit_kind":"移动止损",
 "strategy":"移动止损(峰10.5)","hash":"5cb17516d552a4df","prev":""}
```
**PASS**：事件名/级别正确；`code/name/price/qty/fee/pnl/hold_days/reason/exit_kind`
九字段无缺失。
映射抽查：移动止损→移动止损、冲高回落止盈→冲高回落止盈、阶梯止盈→阶梯止盈、
时间止损→时间止损、两点半→两点半、打板半仓止盈→打板半仓止盈、打板低开止损→止损、
超时退出→时间止损、止盈→冲高回落止盈、未知→原文截断/手动。全对。

### 3.2 验收②：周报重跑（dry-run + --write）
| 项 | 修复前 | 修复后 |
|---|---|---|
| 买入覆盖 | 0/16 | **12/16** 有 manual_buy/strategy_buy |
| 卖出覆盖 | 0/19 | **14/19** 有 manual_sell/strategy_sell |
| 上线前兜底（account.trades） | — | 9 笔（买 4 + 卖 5，均在上线日前） |
| 上线后真缺失 | — | **0**（不告警） |

自洽：12+4=16、14+5=19、4+5=9。09-08 两笔买入各命中 strategy_buy（归档核对
63 条中含）。`--write` 已落盘 `docs/reports/live_vs_backtest_weekly.md` +
`data/live_vs_backtest_weekly.json`（新增 `audit_meta` 段）。

### 3.3 自检
- `py -3.13 -m py_compile app/trader.py tools/live_vs_backtest_weekly.py` → OK
- `TradingEngine._sell/_buy/scan_once` 归属完整（模块级插入未劈类）
- 生产 audit 零改动（验收①写 tmp、周报只读）

## §4 备份
- `tmp/c5_trader.py.bak_20260913_153110`（SHA256
  `82C9CB51...0C4311`，与原文件双哈希一致后恢复重改）
- `tmp/c5_weekly.py.bak_20260913_153110`（SHA256 `59FDEA2A...B8C75A0`）
- 中途一次错误插入已恢复备份重做，最终 diff 干净

## §5 生效与遗留
- **生效**：strategy_sell 新字段随下次引擎进程（写 strategy_sell 的调用方）生效；
  周报脚本改动即时生效（下次周度计划任务直接使用）
- **遗留**：
  1. 历史 09-13 前的 strategy_sell（61 条）字段不全（无 hold_days/reason/exit_kind），
     不可追溯补事件 → 由混合口径 account.trades 兜底
  2. 09-08 前无任何买入结构化事件（strategy_buy 09-08 上线）→ 兜底
  3. 卖出 5 笔上线前缺事件由 account 兜底；若验收方要求历史回填可另开单
- **红线遵守**：未重启服务、未 kill 进程、未动 data/app.lock/watchdog.log/
  audit.jsonl（生产）、未改 config
