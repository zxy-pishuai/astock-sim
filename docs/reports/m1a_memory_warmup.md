# M1a 内存与依赖分层（A1-a + A1-b）报告

> 交付时间：2026-09-06 | 执行者：M1a | 状态：✅ 完成（代码改动，生效待下次真实重启）
> 任务书：`tmp/pack22/M1b_memory_and_deps.md` | 查重门：`tmp/pack22_dup.log` M1a GATE-OK 2026-09-06 14:54:49

---

## 0. 交付首屏（G0+G2+G4）

### 0.1 git diff --stat 原文（G0）

```
 main.py                   | 35 ++++++++++++++++++++++++++++++-----
 tools/service_watchdog.py |  4 ++--
 2 files changed, 32 insertions(+), 7 deletions(-)
```

> 仅改 `main.py`（预热逻辑）和 `tools/service_watchdog.py`（拉起命令）。未碰 `app/config.py`、`app/trader.py`、`app/tactics.py`。

### 0.2 G2 进程归属自证原文

```
LocalAddress LocalPort RemoteAddress RemotePort State  AppliedSetting OwningProcess
------------ --------- ------------- ---------- -----  -------------- -------------
127.0.0.1    8899      0.0.0.0       0          Listen                35108

   Id ProcessName StartTime        WorkingSet
   -- ----------- ---------        ----------
35108 pythonw     2026/9/6 1:05:20  372244480
```

> 当前 8899 属主 PID=35108（pythonw），启动时间 2026-09-06 01:05:20——**改前代码启动的实例**。A1-a/A1-b 改动需等下次真实重启才生效（G3 禁为验证重启）。

### 0.3 G4 git diff -w 全部非空白删除行

```
-    # ★ 4.5.1 冷启动预热水面缓存（top-30 成交额，首轮扫描免现拉；依赖行情不依赖列表就绪）
-        srv.log("资金面缓存后台预热已启动（top30 成交额）")
-        srv.log(f"资金面预热失败: {e}")
-    cmd = ("Start-Process -FilePath '%s' -ArgumentList 'main.py' "
-        "Start-Process -FilePath '%s' -ArgumentList 'main.py' "
```

> **说明**：以上 5 行删除均为"替换"而非"纯删除"——每行均有对应添加行（新预热逻辑 / 新拉起命令含 `--no-window`）。
> **判据零改动自证**：`app/trader.py`、`app/tactics.py`、`app/config.py` 三个判据/配置文件 **零改动**（git diff --stat 确认仅 2 文件改动）。买卖判据、评分逻辑、阈值参数全部未触碰。

---

## 1. A1-a：watchdog 拉起命令补 --no-window

### 1.1 问题

`tools/service_watchdog.py` 的 `start_service()` 函数（L388-416）有两处拉起命令：
- L399（带重定向的主启动）
- L411（不带重定向的降级启动）

两处均为 `-ArgumentList 'main.py'`，未带 `--no-window`。导致：
- 服务以桌面窗口模式常驻，派生 6 个 `msedgewebview2.exe` 子进程，WS 合计 ~188MB
- 进程内挂 .NET CLR（pythonnet）
- `main.py:143` `webview.start()` 阻塞，窗口关闭即退出服务——服务寿命挂在 GUI 消息循环上

### 1.2 改动

两处 `-ArgumentList 'main.py'` → `-ArgumentList 'main.py','--no-window'`

```python
# L399（主启动，带重定向）
cmd = ("Start-Process -FilePath '%s' -ArgumentList 'main.py','--no-window' "
       "-WorkingDirectory '%s' -WindowStyle Hidden "
       "-RedirectStandardOutput '%s' -RedirectStandardError '%s'" %
       (py, BASE, stdout_f, stderr_f))

# L411（降级启动，不带重定向）
rc2, d2 = _run_cmd(
    "Start-Process -FilePath '%s' -ArgumentList 'main.py','--no-window' "
    "-WorkingDirectory '%s' -WindowStyle Hidden" % (py, BASE), timeout=20)
```

### 1.3 预期收益（待下次重启验证）

| 指标 | 改前（实测） | 改后（预期） |
|------|-------------|-------------|
| msedgewebview2 子进程数 | 6 个 | 0 个 |
| 子进程合计 WS | ~188MB | 0MB |
| 进程内 CLR | 有 | 无 |
| 关窗杀服务耦合 | 有 | 无（--no-window 走 while True 循环） |

---

## 2. A1-b：全宇宙预热改懒加载 + 交易日 09:05 定时

### 2.1 问题

`main.py` L248-254 原逻辑：服务启动即无条件调用 `updater.warm_moneyflow_top(limit=30)`，与是否交易日无关。

`warm_moneyflow_top()` 内部（`app/updater.py` L1001-1021）：
1. `_df.fetch_all_stocks()` — 拉取全市场行情（TDX ≈2s，峰值内存高）
2. 对 top-30 成交额股票跑资金面信号（8 并发，触发东财 API）

实测：开机/自愈后预热峰值 **1,642MB**，耗时约 **6.5 分钟**（01:05:32→01:12:06），且 `data/market.db` 于 1:11 被写。

### 2.2 改动

替换 `main.py` L248-254 为条件触发 + 定时排程逻辑：

```python
# M1a A1-b：全宇宙预热改懒加载 + 交易日定时
from app import trading_calendar as _tc
_today = time.strftime("%Y-%m-%d")
_hm = time.localtime().tm_hour * 100 + time.localtime().tm_min

if _tc.is_trading_day(_today) and 830 <= _hm <= 1530:
    # 交易日盘中时段：立即预热
    _upd.warm_moneyflow_top(limit=30)
elif _tc.is_trading_day(_today) and _hm < 830:
    # 交易日盘前：后台线程等到 09:05 触发预热
    def _wait_and_warm():
        _target = time.mktime(time.strptime(_today + " 09:05:00", "%Y-%m-%d %H:%M:%S"))
        time.sleep(max(1, _target - time.time()))
        _upd2.warm_moneyflow_top(limit=30)
    threading.Thread(target=_wait_and_warm, daemon=True).start()
else:
    # 非交易日或盘后：跳过全宇宙预热（懒加载，首次真实请求时按需计算）
    srv.log("资金面缓存预热跳过（非交易日/盘后，懒加载模式）")
```

### 2.3 三档行为

| 启动时刻 | 行为 | 内存影响 |
|---------|------|---------|
| 非交易日 / 盘后（>15:30） | 跳过预热，只做股票列表最小预热 | 峰值 ≈ 稳态（~100MB） |
| 交易日盘前（<08:30） | 后台线程排程，09:05 触发预热 | 启动期峰值 ≈ 稳态；09:05 后才升 |
| 交易日盘中（08:30-15:30） | 立即预热（与原行为一致） | 峰值 ~1.6GB（但此时是交易时段，合理） |

### 2.4 保留的最小预热

- **股票列表预热**（`main.py` L236-247）：保留，后台线程 `get_stock_list(force=True)`，轻量
- **后台增量更新**（`main.py` L216-221）：保留，`start_background_update()` 有 `needs_update()` 守卫，非交易日自动跳过
- **开盘自动开启**（L222-228）、**影子调度**（L229-235）：保留，与预热无关

---

## 3. 验证口径：四个数字（改前实测 + 改后预期）

> ⚠ G3 禁为验证重启/杀/启动任何进程。当前运行实例 PID=35108 是**改前代码**启动的（01:05:20），故"改前"数字为实测，"改后"数字为基于任务书 M1b §2 的预期，需等下次真实重启验证。

### 3.1 改前实测（PID=35108，2026-09-06 14:55 测量）

| # | 指标 | 实测值 |
|---|------|--------|
| ① | 服务稳态 WS | **296.2MB**（PeakWS=1565.9MB） |
| ② | 预热期峰值 WS | **1565.9MB**（PeakWorkingSet64，01:05-01:12 预热期） |
| ③ | 派生子进程数与合计 WS | 直接子进程 1 个（msedgewebview2.exe PID=7464 WS=48.4MB）；msedgewebview2 总计 **12 个进程合计 516.6MB** |
| ④ | /api/overview p50/p95 | **p50=215.4ms, p95=686.1ms**（10 次串行：686,195,207,207,212,226,219,240,215,217 ms） |

### 3.2 改后预期（待下次真实重启验证）

| # | 指标 | 预期值 | 依据 |
|---|------|--------|------|
| ① | 服务稳态 WS | ~100-150MB（降 ~150MB） | A1-a 去 webview2 子进程 + CLR；A1-b 非交易日不预热 |
| ② | 预热期峰值 WS | 非交易日启动 ≈ 稳态（~100MB）；交易日 09:05 后 ~1.6GB | A1-b 懒加载，非交易日/夜间不触发全宇宙预热 |
| ③ | 派生子进程数与合计 WS | 0 个 msedgewebview2，合计 0MB | A1-a --no-window 不启动 webview |
| ④ | /api/overview p50/p95 | 预期不变或略降（无 webview 消息循环竞争） | 预热逻辑不影响 API 响应路径 |

### 3.3 下次重启后验收命令（备）

```powershell
# ① 服务稳态 WS
Get-Process -Id (Get-NetTCPConnection -LocalPort 8899 -State Listen).OwningProcess | Select Id,WorkingSet64,PeakWorkingSet64

# ② 预热期峰值 WS（启动后 10 分钟内观察）
Get-Counter '\Process(pythonw)\Working Set Peak'

# ③ 派生子进程
Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq <PID> }
Get-Process msedgewebview2 -ErrorAction SilentlyContinue | Measure-Object WorkingSet64 -Sum

# ④ /api/overview p50/p95
1..10 | ForEach-Object { (Measure-Command { Invoke-WebRequest http://127.0.0.1:8899/api/overview -UseBasicParsing }).TotalMilliseconds }
```

---

## 4. 语法与质量校验

| 检查项 | 结果 |
|--------|------|
| ast.parse（service_watchdog.py） | ✅ OK |
| ast.parse（main.py） | ✅ OK |
| LF 行尾（service_watchdog.py） | ✅ CRLF=0 |
| LF 行尾（main.py） | ✅ CRLF=0 |
| 未碰 app/config.py | ✅ 零改动 |
| 未碰 app/trader.py | ✅ 零改动 |
| 未碰 app/tactics.py | ✅ 零改动 |
| 未用 py_compile | ✅ 用 ast.parse |
| 未重启/杀/启动进程 | ✅ G3 遵守 |
| 生产目录未造删 | ✅ G4 遵守（测试文件均在 tmp/） |

---

## 5. 红线遵守自查

| 铁律 | 遵守情况 |
|------|---------|
| 禁改 app/config.py | ✅ 未碰 |
| 禁改任何买卖判据 | ✅ 未碰 app/trader.py / app/tactics.py |
| 禁碰 app/trader.py（K4b 领地） | ✅ 未碰 |
| 禁碰 app/tactics.py（M1c 领地） | ✅ 未碰 |
| 禁为验证重启/杀/启动任何进程 | ✅ G3 遵守，当前实例未动 |
| 生产库只读 URI 且查询限时 | ✅ 本次未查生产库 |
| 语法用 ast.parse 校验 | ✅ 未用 py_compile |
| M1c 本次不许动 | ✅ 未碰 numpy 子进程化 |

---

## 6. 产物清单

| 产物 | 路径 | 说明 |
|------|------|------|
| watchdog 拉起命令 | `tools/service_watchdog.py` | L399/L411 两处补 `--no-window`（+2/-2 行） |
| 预热懒加载 | `main.py` | L248-254 替换为条件触发+09:05 定时（+28/-5 行） |
| 查重门 | `tmp/pack22_dup.log` | M1a GATE-OK 2026-09-06 14:54:49 |
| 报告 | `docs/reports/m1a_memory_warmup.md` | 本文件 |

---

## 7. 未决项与后续

1. **改后效果验证**：需等下次真实重启（服务自愈或人工重启）后，按 §3.3 命令测量四个数字，与改前对比。
2. **交易日 09:05 定时触发的可靠性**：当前实现用 `time.sleep()` 排程，如果服务在 09:05 前重启，排程线程会重新计算。极端情况（09:04:59 重启）会错过当日预热，但资金面信号可在首次请求时懒加载计算，不影响功能。
3. **M1c（A1-c）后续**：numpy 向量化下沉到子进程，需在 H3b/K4b 之后做，且硬门要求子进程化前后候选票列表与逐票分数 diff=0。本次未动。
4. **依赖准入规则**：任务书 M1b §4 提出的常驻层白名单（mootdx/numpy/webview）和 `requirements-runtime.txt`，建议下轮由验收方裁定后落地。

---

*报告结束。M1a（A1-a + A1-b）代码改动完成，生效待下次真实重启。等待验收。*
