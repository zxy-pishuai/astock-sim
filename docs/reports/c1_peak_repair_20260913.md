# C1 清洗存量持仓污染 peak（F2 遗留）修复报告

- 日期：2026-09-13（周日）
- 改动文件：`app/trader.py`（加载持仓一致性校验 + 启动护栏）、`tools/repair_position_peak.py`（新增一次性诊断）
- 产物：`data/repair_position_peak.json`
- 状态：✅ 完成（含与并发块的冲突合并说明）

## §0 背景

F2（2026-09-08）上线 `TRAILING_ENTRY_GATE=True` 后新建仓正确（002815 崇达 09-09 两笔止盈
+481.7/+639.12）；但 09-08 11:22 建仓的 002011 盾安环境 `peak=12.54`（**建仓前早盘高点**）
留在 account.json，09-09 13:00:18 仍按脏峰移动止损，亏 **-867.37**。本任务清洗存量污染 +
量化 F2 之前的损失 + 防复发。

## §1 改动 diff 摘要

### app/trader.py（1962 行，LF，py_compile rc=0，mtime 15:41:59）

1. **模块级**（import 区后）新增：
   - `_F2_GATE_DATE = "2026-09-09"`（F2 门控上线日；此前建仓=无 entry_ts）
   - `_PEAK_REPAIR_TOL = 0.005`（污染判定容差）
   - `_peak_buy_ts(acct, code, entry_date)`：从 trades 解析建仓时刻 "HH:MM:SS"
   - `_peak_obs_high_after(code, entry_date, buy_ts)`：min5.db（URI 只读）取
     `date >= 建仓时刻` 的 `MAX(high)`（**实时尺度，与 peak 同尺度**）；
     无 min5 覆盖返回 `(None, "no_min5")`——日K为 qfq 方言尺度，跨尺度直接比较会
     误判，宁可 WARN 不修也不误修。
2. **`_repair_positions_peak(self, acct=None, persist=True)`**（新方法）：
   - 判定：`entry_date < _F2_GATE_DATE` 且无 `entry_ts`（F2 前建仓特征）且 `peak > entry_price`
   - 清洗：`peak > obs_high×(1+0.5%)` → 重算 `peak = max(entry_price, obs_high)`，
     `audit.record("order", "position_peak_repaired", old_peak/new_peak/obs_source/test=not persist)`
   - `acct=None` 读生产；`persist=False` 不写回（验收/演练注入副本）
   - 写回走 `st.save_account`（原子写）
3. **`start()`**：线程启动前调用 `self._repair_positions_peak()`；且
   `TRAILING_ENTRY_GATE=False` 时打印 WARN + 事件（回退旧行为必须显式）。

### tools/repair_position_peak.py（新增，一次性诊断）

只读 account.json/min5.db（URI mode=ro），输出 `data/repair_position_peak.json`：
- 当前持仓一致性扫描（现在空仓 → 防复发验证）
- 历史**带"峰"卖单**（正则 `峰([\d.]+)`）逐笔：独立重算建仓后观测最高
  （**截止到卖出时刻**，避免用未来数据）、污染判定、`extra_loss_est` 估算

## §2 并发冲突说明（重要）

实施中并发块在两次 Edit 之间覆盖 trader.py，**模块级辅助（常量/函数）一度丢失**
（方法/护栏保留），导致方法引用 NameError 风险。已重新补齐模块级段并内容级验证
（40s 轮询 + 最终完整性检查全过）。**与 B1/B2 同文件的并发教训一致**：合写后必须
立即内容验证。

## §3 历史污染量化（repair_position_peak.json）

12 笔带峰卖单中 **8 笔污染**（含 F2 未覆盖的 3 笔冲高回落止盈）：

| 卖出 | 代码 | 名称 | 类型 | 峰 | 建仓后观测最高 | 污染 | extra_loss_est |
|---|---|---|---|---|---|---|---|
| 08-21 | 600354 | 敦煌种业 | 移动止损 | 6.98 | 6.94 | ✅ | +2191.14 |
| 08-21 | 000153 | 丰原药业 | 移动止损 | 6.73 | 6.47 | ✅ | +577.22 |
| 08-25 | 002716 | 湖南白银 | 移动止损 | 11.70 | 11.70 | ❌（勘误F2） | 0 |
| 08-31 | 600095 | 湘财股份 | 冲高回落 | 10.14 | 9.89 | ✅ | −167.62 |
| 09-02 | 605577 | 龙版传媒 | 冲高回落 | 12.26 | 11.68 | ✅ | −469.44 |
| 09-03 | 600121 | 郑州煤电 | 冲高回落 | 5.94 | 5.62 | ✅ | +214.76 |
| 09-07 | 002059 | 云南旅游 | 移动止损 | 6.35 | 6.02 | ✅ | +307.38 |
| 09-07 | 002787 | 华源控股 | 移动止损 | 25.38 | 23.91 | ✅ | −67.44 |
| 09-09 | 002011 | 盾安环境 | 移动止损 | 12.54 | 12.16 | ✅ | +665.92 |
| （其余 3 笔 000998/002300/002815 判干净） | | | | | | | |

- `extra_loss_est` 口径：干净触发价 − 实际成交价 × qty。**正 = 脏峰使止损基准虚高、
  若市场在干净触发价有流动性则本可少亏的估算**；负 = 该口径下反而更差（冲高回落类
  方向可能反转，仅列示）。**移动止损类（触发=峰×0.97）较可靠；全部为估算，
  真实成交受市场深度/跌停影响，供验收方复核不替代对账**。
- 汇总：`extra_loss_est_total = +3251.92`（正数口径，8 笔污染中 5 笔移动止损贡献
  +3077.32；3 笔冲高回落 −422.30 方向不定）。

### ★ F2 报告勘误：002716 湖南白银并非污染

F2 报告 1.2 判定"002716 峰 11.70 高于当日任何行情高点（日K/min5 均 11.46）→ 异常值
穿透"。**C1 独立取证证伪**：min5 与日K 均含 **08-25 09:35 high=11.70**（建仓 08-24
10:06:49 **之后**）→ 峰 11.70 可由建仓后行情解释 → **非污染**（F2 当时数据源/查询
区间不完整）。F2 的 5 笔污染中实际成立 4 笔（600354/000153/002059/002787），
002716 除外；002011 由 F2 报告 1.3 + C1 独立复算双确认。

## §4 验收证据

### 验收①：副本构造污染 peak → 重算 + audit（tmp/c1/accept1.py）

副本（002011 真实结构，peak 构造为 20.0 模拟异常值穿透，无 entry_ts）：
- 修复前 `peak=20.0` → 修复后 `peak=13.96`（= max(11.912, min5 建仓后观测最高 13.96)）✓
- 引擎事件：`WARN ⛑ C1 盾安环境(002011) peak 污染修复 20.000→13.960（min5）` ✓
- audit：`{"kind":"order","event":"position_peak_repaired","code":"002011",
  "old_peak":20.0,"new_peak":13.96,"obs_source":"min5","test":true,...}` ✓
- **生产 account.json 未动**（mtime 09-10 15:00:39 前后一致）✓

### 验收②：TRAILING_ENTRY_GATE=True 下新建仓 peak=entry_price

代码审查证据：`_buy` 建仓 `"peak": price`（建仓时恒等于买入价）；F2 的
`eng.update_peak_after_entry` 只接受"行情时间 ≥ entry_ts"的观测刷新 → 新建仓
peak 恒 = max(entry_price, 建仓后 high)。实盘实证：002815 崇达 09-09 两笔止盈正确
（F2 已生效）。

## §5 生效条件与说明

- `app/trader.py` 改动**下次服务重启生效**（运行中进程内存为旧版）；当前 positions={}，
  重启后 `_repair_positions_peak()` 零持仓零成本。
- `tools/repair_position_peak.py` 即时可用（只读 + 输出 JSON）。
- 修复逻辑只处理 **F2 前（entry_date < 09-09 且无 entry_ts）且 peak 超观测**的持仓；
  F2 后持仓带 entry_ts 天然豁免；无 min5 覆盖的票保守 WARN 不误修。

## §6 遗留

- 8 笔污染的历史损失为**估算**（市场深度不可回放）；如需精确对账需逐日逐 bar 模拟
  撮合，超出本块范围。
- 冲高回落类的 extra 方向性不稳定（600095/605577 为负），建议后续只把**移动止损类**
  的 +3077.32 作为 F2 之前污染的保守损失量化。
