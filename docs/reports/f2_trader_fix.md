# F2（P0）移动止损 peak 污染 + T+1 告警空转 —— 补做报告（2026-09-09）

规格书：`docs/reports/audit_20260908.md`（P0-2 / P0-3 / P1-3 / P3-1）。
本轮性质：**F2 主体已在 9da181a（2026-09-08 23:50 auto）落地并有报告
`docs/reports/f2_trailing_fix_20260908.md`（147 行，7 子任务全 ✅）**；本批核查后
确认**唯一实质缺口 = 情绪门禁 fail-closed（P1-3）**，已补齐。本报告 = 补做交付件
（任务书文件名 `f2_trader_fix.md`），与昨晚报告互为补充。

## 1. 本批改动（仅 1 处，app/trader.py +14 行）

| 位置 | 改前（fail-open） | 改后（fail-closed） |
|---|---|---|
| L822-824（情绪缺失） | `WARN "情绪数据缺失，按默认可开仓"` → `senti_data={"open":True}`，**照常开仓** | `WARN "情绪数据缺失，fail-closed 禁开仓"` + edge-triggered（当日同态只推一次）→ `return self.last_scan`，**禁开仓** |
| L842-843（异常） | `except Exception: pass` 完全静默 | `self._event("ERROR", "情绪门禁异常，fail-closed 禁开仓: ...")` + `return self.last_scan` |

- 复用既有 `self._scan_block_key` 状态键（日期\|状态）做幂等：`<date>\|senti-missing` / `<date>\|senti-error`，当日同状态只推一次，不刷屏（P3-1 同链路）。
- 未改：peak 门控（`update_peak_after_entry` + `entry_ts`）、T+1 冻结告警去重（`trail_alert_date` + 🔒 次开文案）、次日解冻开盘第一笔执行、edge-triggered 噪音治理、滑点 KPI —— 均在 9da181a / `f2_trailing_fix_20260908.md`，本批复核在位。
- 只新增 config 常量？无（F2 情绪块不需要新开关；config 已有 `TRAILING_ALERT_DEDUP` 等）。

## 2. 自检

- ast.parse + py_compile：`app/trader.py`（2007 行）通过，CRLF=0。
- **002011 对拍单测** `tmp/f2_peak_crosscheck_20260909.py`：**ALL-PASS**（16/16）——
  - 建仓前观测（obs_time<entry_ts）拒绝刷新 peak → 早盘 12.54 不进入 peak；
  - 建仓后现价 11.70/11.60 → peak 恒=entry（11.9119），回撤 2.62% < 3% 不触发；
  - 边界 11.50 → 回撤 3.46% > 3% 正常触发（peak 干净时止损仍工作）；
  - 旧污染对照：peak=12.54 → 回撤 7.89% 必触发（修复前实锤复现）；
  - fail-closed 分支静态断言：原 `按默认可开仓` 文案已移除、`fail-closed 禁开仓` 在位、except 记 ERROR。
- 备份：`tmp/f_backup/20260909_210444/trader.py`（SHA256=C628E8474DA6...266E1，源/备份一致，manifest 9/9）。

## 3. 生效状态

- 未重启服务（红线禁碰进程）：8899 当前属主 PID 非本批确认目标，**F2 全部修复下次服务重启后生效**；期间旧行为（情绪 fail-open）仍可能发生 → 与昨晚报告一致，由验收方统一安排重启。

## 4. git diff --stat（本批代码改动，未提交，验收方统一登记）

```
 app/trader.py | 22 +-
```
（相对 HEAD 8cee31c；F2 主体 +176 行在 9da181a。）
