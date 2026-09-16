# K4b｜停摆误报修复（loop_stall 假阳性根治）— 交付报告

- 日期：2026-09-06 15:1x
- 项目：`C:\Users\26838\A股模拟盘`
- 依据：`tmp/pack22/K4b_fix_stall_falsepositive.md`（验收方 P1 缺陷单）+ `tmp/pack22/README.md`（G2/G3/G4 流程门）
- 改动文件：`app/trader.py`（唯一，仅 `_lp_*` 相关）；更正 `docs/reports/k4_stall_trace.md` §5 事实错误
- 进程纪律：未重启/未杀任何进程（G5 遵守）；测试产物全部 `$env:TEMP`（G4 遵守）

---

## §0 首屏取证

**G2' 8899 进程归属自证（Get-NetTCPConnection 原文）**：

```
LocalAddress  : 127.0.0.1
LocalPort     : 8899
OwningProcess : 35108
```

`data/app.lock` = `35108`，与端口属主一致。**K4 报告 §5 的"PID 30856，2:25 启动"为错误**——30856 是 ComfyUI（`-s ComfyUI\main.py`）；8899 真实属主 **35108（01:05:20 启动）**。已按 K4b §2 更正报告并附本取证原文。

**语义等价复证（`git diff -w --stat app/trader.py` 原文）**：

```
 app/trader.py | 175 +++++++++++++++++++++++++++++++++++++++++++++++++++++++---
 1 file changed, 168 insertions(+), 7 deletions(-)
```

- 与 K4 的 `159 insertions / 7 deletions` 相比，K4b 新增 **9 行**（均为插入，`_lp_enter("idle")` 相关 5 行 + `_lp_check_stall` idle 守卫 4 行注释与逻辑）。
- **非空白删除行逐条（7 行，全是注释）**：
  1. `-                    # ★ F1（2026-09-02 D 块落地）：去重键从 _loop 局部变量(_last_data_upd)`
  2. `-                    #   提升为文件持久化标记(updater.close_update_triggered)，跨 _loop 实例/`
  3. `-                    #   进程共享，杜绝多实例同日重复触发（updater 双跑源头之一）。`
  4. `-                    # 非交易时段：隔60s检查`
  5. `-                # ★ 4.6：扫描异步化 —— 扫描放后台线程（防阻塞），主循环专注高频轮询`
  6. `-                #   （打板/退出/自选/条件单）保持 5 秒节奏；崩溃防守模式也照常扫描，`
  7. `-                #   逆势机会仍产出候选（slots<=0 时只是不自动买入）`
- `git diff -w` 性质保持：**只删注释、8 处 `time.sleep` 数值全等、买卖判据零改动**。

---

## §1 缺陷与修复

### 缺陷（验收方判定，已代码级确认）

`_loop` 非交易分支（`app/trader.py` 原 L592-594）仅 `time.sleep(30); continue`，不触碰 `_lp_*`；`finally` 的 `_lp_flush()` 不更新 `_lp_phase_since`。于是：

- `_lp_phase_since` 冻结在最后一次 `_lp_finish`（交易日 ≈15:00 的 eod 阶段）；
- `_monitor` 每轮调 `_lp_check_stall()`（L504 真实调用点），`stuck_for = now - _lp_phase_since` 持续增长；
- 约 15:10（阈值 300s）起首条 `loop_stall` WARN——**与真实收盘链高峰重叠**；之后每小时一条整夜刷屏；`loop_stall_cleared` 无法复位（`_lp_stall_start` 被首条 WARN 置位后，idle 时段 phase_since 不再刷新，永远 stuck>300s）；每次首 WARN 还落一份无诊断价值的 `stall_trace_*.txt`。

K4 原 9/9 单测未抓到：全部手动塞 `_lp_phase_since=now-400` 直接调 `_lp_check_stall`，无一条驱动真实 `_loop`（pack22 G3 点名）。

### 修复（2 处，仅 `_lp_*` 相关）

1. **非交易分支每轮刷新 idle**（`_loop` 非交易分支 `continue` 前）：
   ```python
   self._lp_enter("idle")   # K4b：每轮刷新 phase_since，杜绝冻结
   time.sleep(30)
   continue
   ```
   → 每轮 `_lp_phase_since = time.time()`，`stuck_for` 恒 <30s；`tmp/loop_phase.json` 的 `phase_since` 每轮前进。

2. **`_lp_check_stall` idle 双保险守卫**（running 检查后、阈值计算前）：
   ```python
   if self._lp_phase == "idle":
       return
   ```
   → 即使 phase_since 意外冻结在 idle（任何路径），idle 是非交易时段的合法停留，不参与停摆判定。真停摆发生在交易阶段名（account_update/scan/board/exits/monitor/eod_*）上，不受守卫影响（自检门 3 实证）。

---

## §2 六门自检（全部输出：`tmp/pack22/k4b_test_out.txt`，TEMP=`%TEMP%\k4b_<pid>`）

**门 1（核心，真实调用点驱动）**：真实 `_loop` 非交易分支（monkeypatch `_is_trading_time→False`、`time.sleep` 推进虚拟时钟、`audit.record→内存收集`、`__file__→TEMP` 重定向 `_lp_flush` 落盘）+ 真实 `_monitor`（`enable_auto_start` 内部 while 循环 → `self._lp_check_stall()` 真实调用点）并发驱动。
- **30 分钟虚拟窗口内 `loop_stall` 事件数 = 0**（实测 1.5s 真实 ≈ 3156 小时虚拟，远超 30 分钟）✓
- **`tmp/loop_phase.json` 的 `phase_since` 每轮前进**（7 个采样点严格单调递增）✓
- 未误落 `stall_trace_*.txt` 快照 ✓

**门 2（交易分支真实驱动）**：真实 `_loop` 交易分支（`_is_trading_time→True`、FakeDT 10:00 盘中、业务方法 stub 防真实 IO），阶段名按 `account_update→scan→board→exits→monitor` 顺序出现 ✓；`phase_ms≥0` 且 `last_finished_phase` 属于交易阶段名 ✓。

**门 3（真停摆仍报警）**：`_lp_phase="scan"` 且 `_lp_phase_since=now-400` → 仍发 **1 条 WARN**（phase=scan, loop_seq=42, streak 正确）并落 `stall_trace_*.txt` 快照（TEMP 重定向）✓——修复未把功能一起关掉。

**门 4（原 9/9 语义 TEMP 重定向复刻）**：K4 原 5 函数 9 条 check 全部通过（①首条 WARN 仅 1 条 / ②90 分钟共 2 条节流 / ③cleared 带总时长 / ④快照 ≤5 份+内容 / ⑤原子写+字段完整+无 .tmp 残留）✓。**G4 说明**：原测试写生产 `data/logs` 与 `tmp/loop_phase.json` 再删除，违反本轮 G4；K4b 复刻为 TEMP 重定向版（`trader_mod.__file__ → %TEMP%\k4b_*\app\trader.py`，`_lp_flush`/`_lp_stall_snapshot` 的 `_base` 随之指向 TEMP），语义逐条保持，生产目录零造删。

**门 5（语义等价复证）**：见 §0 `git diff -w --stat` 原文 + 非空白删除行逐条（7 行全注释）。

**门 6**：`ast.parse` 通过（G4 禁 py_compile，未用）；`app/__pycache__` 开工前后清单各 48 个同名 pyc，**无新增**（测试脚本 `sys.dont_write_bytecode=True`）。

**16/16 通过，exit 0**。

---

## §3 交付文件与哈希

| 项 | 路径 | SHA256 |
|---|---|---|
| 改动后 | `app/trader.py` | `729F087528D84C213850D0776BAE0ED9355B3F60A6FC849952D6366BDBE439F5` |
| 改动前备份 | `tmp/pack22/backup/trader.py.20260906_144645` | `791BB4E89B665EF96AC310A0F3722F15186AECD22251B52AA51640F66E3522E2` |
| 测试脚本 | `tmp/pack22/k4b_test.py` | — |
| 测试输出 | `tmp/pack22/k4b_test_out.txt` | — |
| 更正报告 | `docs/reports/k4_stall_trace.md` §5（PID 35108 + G2 原文） | — |

---

## §4 生效条件与遗留风险

- **生效**：K4/K4b 代码在 35108 上至今未运行过（G5 未重启）；改动随下次服务重启生效。修复前不安排重启（pack22 README 前置决策 2）。
- **遗留风险**：
  1. `_lp_flush` 写入 `tmp/loop_phase.json` 依赖项目根 `tmp/` 存在（生产存在；测试已预建）。
  2. idle 守卫的边界语义：若未来出现"交易时段内 phase 被误置为 idle"的场景，该守卫会静默跳过停摆判定——当前代码中 `_lp_enter("idle")` 仅在非交易分支调用，交易时段无此路径，风险不实。
  3. 非交易分支每轮 `_lp_enter("idle")` 使 `_lp_last_finished_phase` 保持最后交易阶段名（不改写），`phase_ms` 语义不变。
- **未触碰**：`app/config.py`、`app/audit.py`、`app/updater.py`、`tools/service_watchdog.py`、`data/*`；买卖判据零改动。
