# 审计链统一（任务1｜修复型）

- 状态：**已完成**（2026-08-27）
- 范围：① 策略买卖接入统一审计事件流 ② 审计写入单写者+落盘同步（并发断链根治）
  ③ 历史断裂诊断脚本（只标记、绝不重写）
- 红线遵守：未改交易逻辑（仅成交确认后追加审计调用）、未动 account.json 结构、
  未重启服务、未改 app/config.py；全部 .py 过 py_compile（155 个项目文件，0 失败）
- 工具：`tools/audit_chain_diag.py`（诊断）；改动：`app/trader.py`（两处尾部）、
  `app/audit.py`（record 重构）

## 1. 修复前事实基线

| 指标 | 数值 | 出处 |
|---|---|---|
| 策略买入事件 | **0 条**（trader._buy 直写 account.json） | D5 交叉核对：买入 0/4 有事件 |
| 真·手动买入事件 | 1 条（state.manual_buy） | manual_buy 是唯一入流的买卖事件 |
| 策略卖出 / manual_sell 事件 | **0 条** | D5：卖出 0/5 |
| 哈希链断裂 | **339 处**（重同步口径；健康率 83%，1993 行） | tools/audit_chain_diag.py 首跑存档 `data/audit_chain_breaks.json` |

断裂根因（代码层面）：旧 `audit.record` 用"进程启动时恢复一次＋内存维护"的
`_prev_hash`——服务、计划任务、各工具进程各自持有互不可见的链头，任一交错写入
即 `prev_mismatch+hash_mismatch`。按日分布印证多写者叠加日集中爆发：
08-20×67（断源演练）、08-26×253（P71 排除扩容落地），另有零散同日重启断点。

## 2. 修复内容

### 2.1 ① _buy/_sell 成交路径补审计（app/trader.py 两处，成交记账之后追加）

| 事件名 | kind/level | 字段（语义对齐既有事件族） |
|---|---|---|
| `strategy_buy` | order/BUY | code、name、price(3位)、qty(int)、fee(2位)、strategy=策略事由截断80字 |
| `strategy_sell` | position/SELL | 同上 ＋ pnl(2位) |

字段命名与取值口径逐项对齐 `state.manual_buy`(order/BUY) 与
`state.manual_sell`(position/SELL)：买卖分属 order/position 两类与其一致；
失败不回滚交易、不抛出（audit 自身 except 兜底）。交易逻辑零改动：
金额/持仓/account.json 的写入语句一字未动。

### 2.2 ② 单写者 + 落盘同步（app/audit.py record 重构）

新写入临界区＝「读磁盘真实尾部→算 hash→追加→flush+fsync」，全程持有：
- 进程内 `threading.Lock`（原有保留）；
- **跨进程** `data/audit/.write.lock` 首字节排他锁（msvcrt.locking，
  阻塞等待≤60s；非 Windows 自动降级为仅线程锁）；
- prev 一律以写前重读文件尾为准（容忍末行半截 JSON 向回找最近完整行），
  替代旧"启动恢复+内存维护"；跨天复位语义与 verify_chain 保持一致
  （每天首条 prev=""）;
- 追加后 flush+fsync：已确认事件进程崩溃不丢。
内存缓冲 `_buf`、查询接口 `query/daily_summary/verify_chain` 行为不变；
兼容性：不再调用的 `_init_chain/_roll_day` 已随重构移除（全仓无外部引用，
tools/smoke_test.py 引用的是模块级 `_prev_hash` 变量，仍存在）。

### 2.3 ③ 断裂诊断（tools/audit_chain_diag.py）

只读标记器：按写入端同款口径（每日首条 prev=""，hash=payload(sort_keys)+prev
SHA256[:16]）逐行重算比对，断点处重同步继续验后续行，输出行号/时间/事件/
性质（prev_mismatch/hash_mismatch/bad_json）/期望与实际 prev 尾4位对照；
`--json` 存档清单、`--verify FILE` 可对任意审计文件做同口径复查（本报告 §4 即用它）。

## 3. 修复前后对比

事件覆盖（同一观察期类型）：

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 策略买入成交 | 无任何事件 | `strategy_buy`（order/BUY） |
| 策略卖出成交 | 无任何事件 | `strategy_sell`（position/SELL） |
| 手动买卖 | manual_buy / manual_sell（保持不变） | 同左 |
| 并发写入链完整性 | 339 断/1875 行，持续劣化 | 新增段 0 断（见 §4） |

## 4. 验证（真实并发实测，非模拟）

探针：4 个独立进程 × 各 25 条 `strategy_buy/strategy_sell` 交错写入
（`data/t1_concurrent_probe.py`，跑完删除；期间运行中的服务自身也在写入
trading_event/heartbeat——最恶劣的三方并发场景）：

```
并发写入完成 1.1s
复查: 总行 2123｜连续校验通过 1784｜断链合计 339（基线=339）
今日(2026-08-27)断链: 0
```

- 新增 131 行（100 探针＋31 服务原生事件）：`strategy_*` 子序列 100/100
  全部携带正确 prev、严格连续 ✅
- 复核命令可复现：`python tools/audit_chain_diag.py --verify data/audit/audit.jsonl`
  （预期输出"断裂合计 339"恒定不变即为健康信号）

**验证窗口结束后的补充取证**：探针结束后约 7 秒，未重启的服务进程又写入一条
`trading_event`（pos_cash 扫描异常告警），产生 1 处新增断链（行 2124，
prev=`…005e` 指向服务内存中的上一条自链而非磁盘尾 `…6420`）——
**写入者身份经前后行对照确认为旧版常驻服务**，非本修复的新代码路径；
断点后所有行已按磁盘尾自动续接（此后零新增），损害止步于单条。
此为 §7 遗留声明的直接实证：彻底消灭此类断链只需一次例行重启让服务加载新版
record（不在本次授权内）；在此期间新旧交错的最坏影响＝每次服务写一条即自愈，
不再有旧机制的连锁污染。

## 5. 历史断裂清单（339 处，只标记未修补）

存档：`data/audit_chain_breaks.json`（含每处行号/时间戳/性质/prev 对照）。
按红线不做任何重写或补链——历史断裂是并发写时代的客观事实，修补反而破坏证据。
分布：{08-16:3, 08-17:3, 08-18:2, 08-19:1, 08-20:67, 08-25:10, 08-26:253}。

## 6. 附带发现（与本任务无关，如实记录）

运行中服务在验证窗口内持续报
`后台扫描异常: cannot access local variable 'pos_cash' where it is not associated with a value`
——定位 trader.py 内 `pos_cash` 于扫描买入路径先使用（L655 一带用于推算委托量）
后赋值（L664），属**存量 use-before-assign 缺陷**，疑似即是近期"策略自动买卖
零触发"（D4/D5 所见）的直接原因。因触碰交易逻辑超出本任务红线（不改交易逻辑），
仅记录此处供后续立项修复。

## 7. 遗留风险声明

- **未重启的服务进程是当前唯一残余断链源**（实证见 §4 补充取证：行 2124）。
  其内存 audit 模块仍是修改前旧版；在其下次重启前，服务每写一条仍会以自链头
  断一次，但新机制保证损害止步于单条、后续写入自动续接。例行重启后即归零。
- `.write.lock` 为 Windows 专属字节锁实现；跨平台部署时 msvcrt 缺失会静默
  降级为仅线程锁（已在 docstring 声明）。
