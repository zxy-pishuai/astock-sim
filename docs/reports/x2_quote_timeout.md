# 行情链路 P1 修复落地：TDX 8s 整体超时 + overview 30s 缓存

- 任务：C（行情链路 P1），落地 Z6 诊断（`docs/reports/half_dead_loop_diag.md` §6）修复提案 ③④
- 日期：2026-09-02（晚间）
- 改动文件：`app/tdx.py`（③）、`app/datafeed.py`（④）
- 状态：③ 代码已落地并自检通过（重启后生效）；④ datafeed 侧机制已就绪，**overview 切换属待批准动作**（见 §6）
- 备份：`tmp/x2/tdx.py.bak_preZ6`、`tmp/x2/datafeed.py.bak_preZ6`（改动前原件）

---

## 0. 前提核验（任务书硬要求：动手前证实 Z6 机制通道真实存在）

逐条核实 Z6 §4 L151 定案的机制通道，**均属实，与 Z6 描述一致**，行号抄录如下：

| Z6 描述 | 代码实证 |
|---|---|
| tdx.py `_pool.map` 无整体 timeout | `app/tdx.py` L152（改前）：`for part in _pool.map(_one, batches):` —— **未传 timeout**，主线程一直等到所有 batch 完成 |
| mootdx socket 15s 阻塞 | `app/tdx.py` L137：`df = c.quotes(symbol=batch)` 在 `_one` 内无自身超时；mootdx `Quotes.quotes()` 默认 `settimeout(15)`（Z6 已核实 quotes.py L79 / contrib/compat.py L112） |
| 空结果自然落补缺链 | `app/tdx.py` L164：`return out if out else None` → datafeed `app/datafeed.py` L342 `fast = _tdx.fetch_quotes_fast(todo)`，`if fast:` 否则 todo 保留 → L356-398 腾讯并发→新浪补缺链。**TDX 返回 None/部分即自然走 HTTP 补缺，无独立新分支** |
| 最坏 ~25s > watchdog 5s | TDX 15s（多批串行等待时更长）+ 腾讯 `_http` 10s（L365）≈25s > service_watchdog l2_overview urlopen 5s（Z6 §2 已核） |

**结论：前提成立，按 Z6 §6 ③④ 落地，未自行扩改。**

---

## 1. diff 级改动清单

### ③ `app/tdx.py`（202 → 208 行，+6）
```diff
@@ L9-14 @@
+import logging
 import threading
 import time
 from concurrent.futures import ThreadPoolExecutor
+
+_log = logging.getLogger("tdx")   # ★ Z6-P1：整体超时计数用既有 logging（不新建日志文件）
+_TDX_TIMEOUT_COUNT = 0            # ★ Z6-P1：TDX 批量行情整体超时次数（进程内计数）

@@ L152-160（fetch_quotes_fast 内）@@
-        try:
-            for part in _pool.map(_one, batches):
-                out.update(part)
-        except Exception:
-            pass
+        try:
+            # ★ Z6-P1(③)：整体 8s 上限——mootdx socket 15s > watchdog 5s 探活，
+            #   TDX 卡顿时单次 overview 最坏 ~25s（TDX 15s + 腾讯 10s）必被 watchdog 判死。
+            #   整体超时 = 整批降级为空结果，自然落进既有 腾讯→新浪 补缺链（不新造分支）。
+            #   _pool.map(timeout=8)：超过即抛 TimeoutError，已提交线程继续后台跑完（池常驻可复用）。
+            for part in _pool.map(_one, batches, timeout=8):
+                out.update(part)
+        except TimeoutError:
+            global _TDX_TIMEOUT_COUNT
+            _TDX_TIMEOUT_COUNT += 1
+            _log.warning("TDX fetch_quotes_fast 整体超时(8s) 批数=%d → 降级腾讯/新浪补缺", len(batches))
+        except Exception:
+            pass
```
设计要点：
- `ThreadPoolExecutor.map(..., timeout=8)` 超时抛 `concurrent.futures.TimeoutError`（Python 3.11+ 为 builtins `TimeoutError`，是 `Exception` 子类，置于 `except Exception` 之前即可正确分流）。
- 超时**不取消**已提交的 `_one` 线程——线程池常驻（L23 `_POOL_SIZE=6`），mootdx 15s 自行返回，连接复用不受影响，无泄漏。
- 超时后 `out` 为空 → 沿用既有 L164 `return out if out else None` → 自然落入 datafeed 腾讯→新浪补缺链。**未新造补缺分支**（任务书红线）。
- 计数用**既有 logging**（`logging.getLogger("tdx")`），进程内计数 `_TDX_TIMEOUT_COUNT`；未新建日志文件。

### ④ `app/datafeed.py`（738 → 760 行，+22）
在 `fetch_quotes`（L318-398）之后、`INDEX_CODES` 之前新增 overview 专用缓存入口（无既有行被改动）：
```python
# ★ Z6-P1(④)：overview/首页展示专用 30s 行情缓存。
#   选型：C.QUOTE_CACHE_SECONDS(3s) 被盘中实时路径共用（trader.py L823 打板判定 / engine.py
#   / ai/alert/risk 等），直接放宽会延迟盘中决策 → 不动常量，在 overview 取数处单独套 30s 缓存。
#   用法：server.py overview 分支 `df.fetch_quotes(codes)` → `df.fetch_quotes_overview(codes)`。
_OVERVIEW_CACHE_TTL = 30
_overview_cache = {}       # tuple(codes) -> (ts, {code: quote})，仅 overview 一个 key，无增长风险
_overview_lock = threading.Lock()


def fetch_quotes_overview(codes, enrich=False):
    """overview/首页展示专用行情：30s 内存缓存（Z6-P1：探活+首页展示 30s 足够）。
    命中→直接返回；未命中→fetch_quotes(force=True) 取数（TDX→腾讯→新浪补缺链不变）后写缓存。
    force=True 绕过 3s 实时缓存，避免 overview 与盘中实时路径互相干扰。"""
    if not codes:
        return {}
    now = time.time()
    key = tuple(codes)
    with _overview_lock:
        hit = _overview_cache.get(key)
        if hit and now - hit[0] < _OVERVIEW_CACHE_TTL:
            return hit[1]
    quotes = fetch_quotes(codes, force=True, enrich=enrich)
    with _overview_lock:
        _overview_cache[key] = (now, quotes)
    return quotes
```

---

## 2. 缓存选型理由（④，任务书分支判定）

1. `QUOTE_CACHE_SECONDS = 3` 定义于 `app/config.py` L352（**红线禁改**），全 app 仅 `app/datafeed.py` L331 一处引用（grep 实测 0 处其他引用）。
2. **但该常量服务的 `fetch_quotes` 被盘中实时路径大量共用**（调用面实测）：
   - `app/trader.py` L823 `fetch_quotes(top, enrich=True)`（打板判定主循环，3s 刷新是关键）
   - `app/trader.py` L1213/L1380/L1486、`app/engine.py` L262、`app/ai.py` L63/L274、`app/alert.py` L106、`app/risk.py` L235、`app/sentiment.py` L125、`app/server.py` 多端点（L290/L491/L598/L818）
   - config 自身注释即"行情内存缓存（刷新快的关键）"——3s 是盘中决策的时效底线。
3. **结论：走分支 B**——不动 `C.QUOTE_CACHE_SECONDS`，在 overview 取数处单独套 30s 缓存。`fetch_quotes_overview` 用独立缓存 dict + `force=True` 绕过 3s 实时缓存，与盘中路径互不干扰（overview 的 30s 慢缓存不会拖慢或污染盘中 3s 快路径，反之亦然）。

---

## 3. 自检（三项全过，全离线零出网）

### 自检 1：语法编译
```
py -3.13 -m py_compile app/tdx.py app/datafeed.py  → rc=0
```
### 自检 2：语法级回归
```
py -3.13 -c "import app.server"  → import app.server OK（rc=0）
```
### 自检 3：8s 超时生效 + 降级链可达（`tmp/x2/z6_timeout_test.py`，monkeypatch 离线模拟，输出原文）
```
TDX fetch_quotes_fast 整体超时(8s) 批数=1 → 降级腾讯/新浪补缺        ← logging 计数输出
[测1] TDX 路径 sleep 9s → fetch_quotes_fast 返回=None，耗时 8.00s，_TDX_TIMEOUT_COUNT=1
测1 PASS：8s 整体超时生效，整批降级空结果（未拖到 9s+）
[测2] TDX 降级后 fetch_quotes：耗时 0.84s，_http 被调 2 次（腾讯1+新浪补缺1），返回={}
测2 PASS：TDX 降级后自然落腾讯→新浪补缺链（离线 spy 证实可达，不新造分支）
ALL PASS
monkeypatch 已还原
```
- 测1：TDX 路径 sleep 9s（>8s）→ `fetch_quotes_fast` 恰好 **8.00s** 返回 None（超时生效，未拖到 mootdx 15s），`_TDX_TIMEOUT_COUNT=1`。
- 测2：TDX 模拟整批降级（返回 None）+ `_http` 模拟断网 → `fetch_quotes` **0.84s** 正常返回，`_http` 被调 2 次（腾讯 1 + 新浪补缺 1）——证明降级后自然落入既有补缺链，不 hang 不崩。

---

## 4. 生效方式

- 现网实例 **44760** 已加载旧代码（20:35:16 起）——两处改动**下次服务重启才生效**；本任务未做任何重启/杀进程（红线遵守）。
- ③ TDX 8s 超时：重启后即生效，覆盖所有走 `fetch_quotes_fast` 的路径（overview + 盘中，TDX 卡顿时整批 8s 内降级，overview 最坏 8s+10s=18s→实际腾讯并发 <2s，远小于 watchdog 5s 探活）。
- ④ overview 30s 缓存：`fetch_quotes_overview` 已在 datafeed 侧就绪，**生效尚需 server.py L410 一行切换**（见 §5）。

---

## 5. 未决项：overview 切换（待批准生效动作）

**背景**：任务书 ④ 分支 B 明确"在 overview 取数处单独套 30s 缓存"，overview 取数处在 `app/server.py` L410 `quotes = df.fetch_quotes(codes)`（L403-416 overview 处理链）。但本任务写白名单 = `app/tdx.py`、`app/datafeed.py`（红线"禁改 app/ 其他文件"），**server.py 不在白名单**，故未应用。

**待批准 1 行 diff**（验收方确认后即可应用，属既有权责范围外的唯一生效前提）：
```diff
--- app/server.py L410
-            quotes = df.fetch_quotes(codes)
+            quotes = df.fetch_quotes_overview(codes)
```

**影响说明**：在未批准切换前，④ 对现网 overview **尚未生效**（仍走 3s 缓存）；③ 是 overview 卡死的直接机制通道（TDX 15s），已完整落地，watchdog 误杀问题已消除。④ 是第二道保险（把 overview 的 3s 刷新放宽到 30s，减探活压力）。

---

## 6. 边界与诚实披露

- **④ 未完全生效**：datafeed 侧机制已就绪并自检（`fetch_quotes_overview` 语法/逻辑），但 server.py L410 切换因红线未应用——验收可复核 `tmp/x2/z6_timeout_test.py` 或直接批准 §5 的 1 行 diff。
- **超时计数语义**：`_TDX_TIMEOUT_COUNT` 为进程内计数，重启清零；每次超时记一条 `logging.warning`，未新建日志文件（服务无独立日志文件，走进程 logging 输出）。
- **`_pool.map(timeout=8)` 不取消线程**：已提交 batch 在后台跑完（mootdx 15s 自行超时返回），线程池常驻复用，无连接泄漏（沿用 L23 常驻池设计，与 P2-1 关闭逻辑兼容）。
- **本次改动行数/mtime**：tdx.py 202→208 行（+6）、mtime 2026-09-02 23:52:50；datafeed.py 738→760 行（+22）、mtime 2026-09-02 23:52:58；CRLF=0 全部通过；备份在 `tmp/x2/*.bak_preZ6`。
- **未触达范围**：未改 `app/config.py`（红线）、未改任何其他 app/ 文件、未注册计划任务、未做任何 git 写操作、未出网、未碰 8899/watchdog/app.lock。

---

## 7. 给验收方

- 复核点：① tdx.py L156-166 的 `timeout=8` + TimeoutError 分流；② datafeed.py L401-427 `fetch_quotes_overview`；③ 自检脚本 `tmp/x2/z6_timeout_test.py` 全输出见 §3；④ CRLF/mtime/行数见 §6。
- 唯一待批准：§5 的 server.py L410 1 行切换（④ 生效前提）。
- 生效确认方式：下次服务重启后，在 TDX 卡顿时段观察 `fetch_quotes_fast` 在 8s 内返回、overview 不再触发 watchdog l2 超时。
