# 修复报告：涨停家数误判冰点（10:0→实时 70:0）+ 上证指数反馈慢

- 日期：2026-09-08 11:25
- 触发：用户报告"市场涨跌停 64:0，系统显示 10:0，误判冰点禁止开仓，错过机会；上证指数反馈慢"
- 修复范围：`app/limitup.py`（核心）、`app/index_timing.py`、`app/datafeed.py`
- 备份：`tmp/bugfix_20260908/*.20260908_112045.bak`

---

## 一、根因

### 1.1 涨停家数 10:0（核心 bug）

情绪快照链路：`sentiment.compute_sentiment` → `limitup.fetch_pool("zt")` → DB 缓存 `limit_pool`。

`fetch_pool` 原逻辑：**DB 缓存一旦存在（当日键）就直接返回，永不重拉**。实测：

| 时间点 | 数据源 | 涨停家数 |
|---|---|---|
| 09:25 竞价 | 东财接口（早盘快照）→ 落 `limit_pool` DB | **10**（红棉股份等竞价板） |
| 盘中持续 | 系统读 DB 缓存（`_load` 命中即返回） | **10（全天卡死）** |
| 11:20 实测 | 东财实时接口 | **67~70** |
| 用户观察 | 市场实际 | **64**（盘口口径略异） |

→ `_phase_of(zt=10, ...)` 命中 `zt < 25` → **冰点** → `phase_position` → `open=False, max_pos=0` → **全局禁止开仓**，盘中新增的 60 只涨停被无视。跌停池同理。

### 1.2 上证指数反馈慢 / 数据陈旧

- `app/index_timing.py` `_load_idx_klines`：仅 `len(rows) < 120` 才线上补拉。实测 DB 里 sh000001 有 600 行（≥120）→ **永不触发补拉 → 指数日K停在 2026-08-18（3 周陈旧）**。
- `app/datafeed.py` `fetch_indices`：**无缓存**，overview 每次刷新都打腾讯接口。

---

## 二、修复（3 处，最小侵入）

### 2.1 `app/limitup.py` — `fetch_pool`：当日交易日先拉网络，缓存兜底

```python
def _is_trading_day(date):
    """date(YYYYMMDD) 是否为工作日（周末非交易日；节假日接口返回空时自然回退缓存）"""
    from datetime import datetime as _dt
    return _dt.strptime(date, "%Y%m%d").weekday() < 5

def fetch_pool(kind, date=None, force=False):
    date = date or time.strftime("%Y%m%d")
    key = (kind, date)
    now = time.time()
    live_today = _is_trading_day(date)
    ttl = 120 if live_today else 300          # 盘中 2 分钟刷新一次，历史 5 分钟
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    cached = _load(kind, date)
    if live_today:
        rows = _fetch_pool(kind, date)        # ★ 当日盘中：先拉网络拿实时池
        if rows:
            _save(kind, date, rows)
            _mem[key] = (now, rows)
            return rows
        if cached:                            # 网络失败 → 回退 DB 缓存
            _mem[key] = (now, cached)
            return cached
        return []
    if cached and not force:
        _mem[key] = (now, cached)
        return cached
    rows = _fetch_pool(kind, date)
    ...
```

要点：仅**当日且工作日**走实时路径；历史日期/周末仍走 DB 缓存（`_save` 的周末脏写清洗门不变）；网络失败回退缓存（服务刚启动/断网不空返回）；盘中 mem TTL 120s 平衡刷新频率。

### 2.2 `app/index_timing.py` — `_load_idx_klines`：指数日K陈旧自动补拉

新增 `_idx_stale(rows)`（最新日期距今 >5 自然日且今天是工作日 → 陈旧），`_load_idx_klines` 中 `len(rows) < 120 or _idx_stale(rows)` 时走 `_fetch_online()`（幂等 INSERT OR REPLACE 落库，原有函数未改）。

### 2.3 `app/datafeed.py` — `fetch_indices`：加 5s TTL 缓存

overview 高频刷新不再每次打腾讯接口；与 `fetch_quotes_overview` 的 30s 缓存同思路（指数不会秒变，5s 对展示足够）。

---

## 三、验证

### 3.1 独立进程（改后，未重启前）

```
fetch_pool(zt) 返回 69 只, 耗时 0.18s   ← 实时（修复前 10 只）
情绪: phase=发酵 zt=69 dt=0 score=55.8 open=True max_pos=2   ← 不再冰点
第二次调用(缓存) 69 只, 耗时 0.000s     ← mem 120s 缓存生效
指数日K: 601 行, 最新 2026-09-08        ← 从 8-18 补到今日
fetch_indices 第一次 0.269s / 第二次 0.000s  ← 5s 缓存生效
```

### 3.2 运行实例（服务已重启，新 PID 37436 监听 8899）

```
GET /api/overview  → regime=强势 max_pos=3 breadth=0.7983（上证 3945.76 +0.33%）
GET /api/limitup   → zt=70 只 / dt=0 / summary.total=70 max_days=16 first_board=49
GET /api/sentiment → phase=发酵 score=55.1 zt_count=70 dt_count=0 open=True max_pos=2
                     action=轻仓试探，做首板/低位板
```

### 3.3 编译与零回归

```
py -3.13 -m py_compile app/limitup.py app/index_timing.py app/datafeed.py → 通过
```

---

## 四、备份与回滚

- 备份：`tmp/bugfix_20260908/limitup.py.20260908_112045.bak`（改前 SHA256 `2FED9824…DF53`）、`index_timing.py.20260908_112045.bak`（`0C26E0ED…3EA3`）、`datafeed.py.20260908_112045.bak`（`509040EA…B1F2`）。
- 回滚：停服务 → 恢复 .bak 覆盖 → 重启服务。三处独立可回滚。

---

## 五、遗留说明

1. **INDEX_TIMING_ENABLED=False**（config 默认关闭，需回测验证后人工开启）——本次指数补拉为未来开启择时排除陈旧数据隐患；当前不影响交易路径。
2. **涨跌停家数仍依赖东财 push2ex 免费接口**：若接口被 IP 风控（已有熔断器 3 连败冷却 600s），当日会回退到 DB 缓存（早盘快照），极端情况仍可能短暂显示旧值——属接口可用性降级而非代码缺陷，熔断恢复后自动回到实时。
3. 空间板 max_days=16 来自东财 `zttj.days`，若个别票 days 异常会拉高空间板——本次未触碰该口径，后续可单独核查。
4. 服务重启瞬间中断约 8 秒（11:23 完成），期间无交易动作（修复前系统处于"冰点禁开仓"状态，未新增仓位）。
