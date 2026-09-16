# -*- coding: utf-8 -*-
"""数据层：实时行情（腾讯为主/新浪备用）+ 历史K线（腾讯/新浪）+ 分钟K（新浪）
性能设计：
  1. 磁盘缓存（SQLite）：K线跨会话复用，只增量补齐缺失日期
  2. 内存缓存（TTL）：行情 3s / K线 90s，避免重复 HTTP
  3. 全市场列表缓存 6 小时 + 后台预取
"""
import json
import os
import sqlite3
import threading
import time
import urllib.request
import urllib.parse

from . import config as C
from . import pools

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Mozilla/5.0"
_lock = threading.Lock()
# F3 (2026-09-13): external HTTP quote request counter for audit.
_HTTP_CALLS = 0
_HTTP_CALLS_LOCK = threading.Lock()

def request_count():
    """F3 read-only: cumulative external HTTP request count."""
    with _HTTP_CALLS_LOCK:
        return _HTTP_CALLS

def _bump_request_count(n=1):
    global _HTTP_CALLS
    with _HTTP_CALLS_LOCK:
        _HTTP_CALLS += n
_mem_quotes = {}     # code -> (ts, dict)
_mem_kline = {}      # (code,period) -> (ts, list)
_mem_minute = {}     # code -> (ts, list)  ★ 4.4 分时K线内存缓存（3秒）
_mem_list = None
_mem_list_ts = 0

# ★ E4（2026-09-13）：降级取数留痕 —— 每次从降级源（腾讯/新浪）拉取日K都记一行，
#   收盘更新末尾据此统一补偿缺失 amount（source_degradation_daily 汇总）。
#   文件 append-only；写失败静默（不影响取数主流程）。
_SD_FILE = None
_sd_lock = threading.Lock()


def _record_degradation(code, source, missing_fields, reason=""):
    """记一条降级取数记录：(code, 当日, 源名, 缺失字段)。线程安全，失败静默。"""
    try:
        global _SD_FILE
        if _SD_FILE is None:
            _SD_FILE = os.path.join(C.DATA_DIR, "source_degradation.jsonl")
        row = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "code": code,
            "date": time.strftime("%Y-%m-%d"),   # 拉取日（降级发生日）
            "source": source,                     # tencent / sina
            "missing_fields": missing_fields,     # ["amount"]（腾讯/新浪为无额源）
        }
        if reason:
            row["reason"] = reason
        with _sd_lock:
            with open(_SD_FILE, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _http(url, decode="utf-8", ref=None, timeout=None):
    headers = {"User-Agent": _UA}
    if ref:
        headers["Referer"] = ref
    # ★B-3 保险带：显式绕代理直连（国内行情源）。app/__init__ 已声明 NO_PROXY，
    # 这里再兜一层（双保险，防壳环境 NO_PROXY 被覆盖）。
    _op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in range(C.MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            _bump_request_count(1)
            with _op.open(req, timeout=timeout or C.REQUEST_TIMEOUT) as r:
                raw = r.read()
            try:
                return raw.decode(decode)
            except (UnicodeDecodeError, LookupError):
                return raw.decode("gbk", errors="replace")
        except Exception as e:
            last = e
            time.sleep(0.25 * (attempt + 1))
    raise last


# ================= SQLite 缓存 =================
_db_lock = threading.Lock()

def _conn():
    conn = sqlite3.connect(C.DB_FILE, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    # ★ 4.2：SQLite 性能优化（WAL下读不阻塞写，NORMAL减少fsync）
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn


def _init_db():
    with _db_lock:
        conn = _conn()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS kline(
                code TEXT, period TEXT, date TEXT,
                open REAL, high REAL, low REAL, close REAL,
                volume REAL, amount REAL,
                PRIMARY KEY(code, period, date))""")
            # ★ J3 验收修复（2026-09-14）：不再重建冗余索引 idx_kline_cp——已被 PK (code, period, date) 前缀完全覆盖（见 docs/reports/sqlite_pragma_j3_20260913.md，释放 195.3MB）。原语句会使 DROP 不可持久。
            # conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_cp ON kline(code, period)")
            conn.commit()
        finally:
            conn.close()


_meta_cache = {}        # ★ 4.5.1：(code,period) -> (ts, count, max_date)，盘中日K不变无需反复 COUNT/MAX
_META_TTL = 300


def _kline_meta(code, period):
    """返回 (count, max_date)（★ 内存缓存 300s，避免扫描 130 只每次串行打 SQLite）"""
    key = (code, period)
    now = time.time()
    with _lock:
        hit = _meta_cache.get(key)
        if hit and now - hit[0] < _META_TTL:
            return hit[1], hit[2]
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*), MAX(date) FROM kline WHERE code=? AND period=?",
                (code, period))
            row = cur.fetchone()
            count, max_date = (row[0] or 0, row[1] or "")
        finally:
            conn.close()
    with _lock:
        _meta_cache[key] = (now, count, max_date)
    return count, max_date


_kline_obj_cache = {}   # (code,period) -> (ts, list)  ★ 4.1 长效解析缓存（避免每次重读 SQLite）
_KLINE_OBJ_TTL = 1200   # 20 分钟（盘中数据由 _fetch_daily 的 90s 缓存负责增量，这里只管解析结果复用）


def _kline_load(code, period):
    # ★ 4.1：长效对象缓存 —— 磁盘数据在 session 内基本不变，避免重复全量读
    k2 = (code, period)
    now = time.time()
    with _lock:
        hit = _kline_obj_cache.get(k2)
        if hit and now - hit[0] < _KLINE_OBJ_TTL:
            return hit[1]
    with _db_lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT date,open,high,low,close,volume,amount FROM kline "
                "WHERE code=? AND period=? ORDER BY date", (code, period))
            # ★ 4.5：跳过 NULL 坏行（历史脏数据会毒化指标计算，如 sma 遇 None 崩溃）
            rows = []
            for r in cur.fetchall():
                if r[0] is None or r[4] is None:
                    continue
                rows.append({"date": r[0], "open": r[1], "high": r[2], "low": r[3],
                             "close": r[4], "volume": r[5], "amount": r[6]})
        finally:
            conn.close()
    with _lock:
        _kline_obj_cache[k2] = (now, rows)
    return rows


def _kline_save(code, period, klines):
    """UPSERT 写 K 线。★ F-AMT（2026-09-08）amount 保护 + ★ A2（2026-09-13）写入期断言：
    来源 amount<=0 且 volume>0 的行（降级到无额源所致）不覆盖库内旧值，并在返回值中上报
    {"amount_missing": [(date, volume), ...]}，由调用方（updater）记 WARN 与按源汇总——
    保证"新日期 bar 写 0"可见、可定位、可被收盘批量补额兜底。"""
    missing = []
    with _db_lock:
        conn = _conn()
        try:
            rows = []
            for k in klines:
                amt = k.get("amount", 0) or 0
                vol = k.get("volume", 0) or 0
                if vol > 0 and amt <= 0:
                    missing.append((k["date"], vol))
                rows.append((code, period, k["date"], k["open"], k["high"], k["low"],
                             k["close"], vol, amt))
            conn.executemany(
                "INSERT INTO kline(code,period,date,open,high,low,close,volume,amount) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code,period,date) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
                "volume=excluded.volume, "
                "amount=CASE WHEN excluded.amount>0 THEN excluded.amount ELSE kline.amount END",
                rows)
            conn.commit()
        finally:
            conn.close()
    # ★ 4.1：写入后使对象缓存失效（下次读取拿最新）
    with _lock:
        _kline_obj_cache.pop((code, period), None)
        _meta_cache.pop((code, period), None)   # ★ 4.5.1：meta 缓存同步失效
    return {"amount_missing": missing}


# ================= 实时行情（腾讯主 / 新浪备） =================
def _prefix(code):
    return "sh" + code if code.startswith(("6", "5", "9")) else "sz" + code


def _parse_tencent(text):
    out = {}
    for line in text.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        try:
            body = line.split('"')[1]
        except IndexError:
            continue
        p = body.split("~")
        if len(p) < 50:
            continue
        code = p[2]
        def f(i, d=0.0):
            try:
                return float(p[i]) if p[i] else d
            except (ValueError, IndexError):
                return d
        price = f(3)
        if price <= 0:
            continue
        out[code] = {
            "code": code, "name": p[1],
            "price": price,
            "yest_close": f(4),
            "open": f(5),
            "volume": f(6) * 100,          # 手 → 股
            "high": f(33),
            "low": f(34),
            "pct_chg": f(32),
            "amount": f(37) * 10000,       # 万元 → 元
            "turnover": f(38),             # 换手率 %
            "vol_ratio": f(49),            # 量比
            "limit_up": f(47),
            "limit_down": f(48),
            # ★ v3.3 打板增强字段（封单/内外盘/市值）
            "bid1_price": f(9),            # 买一价
            "bid1_vol": f(10) * 100,       # 买一量(手→股)
            "ask1_price": f(19),           # 卖一价
            "ask1_vol": f(20) * 100,       # 卖一量
            "outer_vol": f(7) * 100,       # 外盘(主动买)
            "inner_vol": f(8) * 100,       # 内盘(主动卖)
            "float_mktcap": f(44) * 1e8,   # 流通市值(亿→元)
            "total_mktcap": f(45) * 1e8,   # 总市值
            "time": p[30] if len(p) > 30 else "",
        }
    return out


def _parse_sina(text):
    out = {}
    for line in text.strip().split("\n"):
        if "=" not in line or '"' not in line:
            continue
        try:
            raw = line.split("hq_str_")[1].split("=")[0]
            code = raw[2:]
            parts = line.split('"')[1].split(",")
            if len(parts) < 10:
                continue
            def f(i):
                try:
                    return float(parts[i]) if parts[i] else 0.0
                except (ValueError, IndexError):
                    return 0.0
            price = f(3)
            if price <= 0:
                continue
            yc = f(2)
            out[code] = {
                "code": code, "name": parts[0], "price": price,
                "yest_close": yc, "open": f(1),
                "volume": f(8), "high": f(4), "low": f(5),
                "pct_chg": round((price - yc) / yc * 100, 2) if yc else 0.0,
                "amount": f(9),
                "turnover": 0.0, "vol_ratio": 0.0,
                "limit_up": 0.0, "limit_down": 0.0,
                "time": parts[31] if len(parts) > 31 else "",
            }
        except Exception:
            continue
    return out


def _backfill_names(result):
    """★ P0-3：TDX 批量行情无 name → 用股票列表缓存（6小时，零网络）回填。
    ST 识别（涨跌停 ±5%）与展示都依赖 name，硬性补齐。失败兜底保持原值。"""
    try:
        need = [c for c, q in result.items() if not (q.get("name") or "")]
        if not need:
            return
        lst = get_stock_list()
        if not lst:
            return
        nm = {c: n for c, n, _ in lst}
        for c in need:
            n = nm.get(c)
            if n:
                result[c]["name"] = n
    except Exception:
        pass


def _enrich_quotes(result, timeout=10):
    """★ P0-3：TDX 行情缺 turnover/vol_ratio/float_mktcap → 对缺字段的 code
    走一次腾讯 qt.gtimg 批量请求补齐（腾讯完整字段含换手/量比/流通市值，沿用 _parse_tencent）。
    仅调用方明确需要时（enrich=True）执行；失败兜底保持现值，不抛错不影响主流程。
    ★ F1（2026-09-13）：timeout 参数化——交易通道传 TRADING_HTTP_TIMEOUT（5s），
    展示/通用路径保持 10s。"""
    try:
        need = [c for c, q in result.items()
                if (q.get("price") or 0) > 0
                and (not q.get("turnover") or not q.get("vol_ratio")
                     or not q.get("float_mktcap"))]
        if not need:
            return
        BATCH = 400
        from concurrent.futures import ThreadPoolExecutor as _TPE3, as_completed as _AC3

        def _fetch_batch(batch):
            symbols = ",".join(_prefix(c) for c in batch)
            try:
                text = _http(f"https://qt.gtimg.cn/q={symbols}", decode="gbk",
                             ref="https://gu.qq.com/", timeout=timeout)
                return _parse_tencent(text)
            except Exception:
                return {}

        batches = [need[i:i + BATCH] for i in range(0, len(need), BATCH)]
        filled = {}
        with pools.get_pool("enrich", 4) as ex:
            futs = {ex.submit(_fetch_batch, b): i for i, b in enumerate(batches)}
            for f in _AC3(futs):
                try:
                    filled.update(f.result())
                except Exception:
                    pass
        for c in need:
            fq = filled.get(c)
            if not fq:
                continue
            q = result[c]
            # 只补缺失字段（保留 TDX 已有值）；result 与 _mem_quotes 为同引用，缓存同步更新
            for k in ("name", "turnover", "vol_ratio", "float_mktcap", "total_mktcap",
                      "outer_vol", "inner_vol", "bid1_price", "bid1_vol",
                      "ask1_price", "ask1_vol"):
                if not q.get(k) and fq.get(k) is not None:
                    q[k] = fq[k]
    except Exception:
        pass


def _finalize_quotes(result, enrich=False, enrich_timeout=10):
    """★ P0-3 统一收尾：name 回填（硬性）+ 可选腾讯字段补齐。
    所有返回路径（缓存命中 / TDX 全命中 / HTTP 路径）都必须走这里。
    ★ F1（2026-09-13）：enrich_timeout 参数化（交易通道短超时）。"""
    _backfill_names(result)
    if enrich:
        _enrich_quotes(result, timeout=enrich_timeout)


# ==================== ★ F1（2026-09-13）实时性隔离 ====================
# P0-J：09-11 主循环停摆 2142s 根因 = fetch_quotes 无在途去重 + 交易/展示共用取数路径。
# 三个独立在途表（展示/通用、交易、显式展示）+ 共享 _mem_quotes 缓存（避免重复取数）。
_INFLIGHT = {}                  # 通用/展示在途：code -> {"ev","ref","ts"}
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_TRADING = {}          # 交易通道在途（独立，交易不等展示的慢请求）
_INFLIGHT_LOCK_TRADING = threading.Lock()
_INFLIGHT_DISPLAY = {}          # 显式展示通道在途
_INFLIGHT_LOCK_DISPLAY = threading.Lock()

# 交易通道状态（停摆自愈）：连续失败计数 / 降级标记 / 最后失败时刻
_TRADING_STATE = {"fail_streak": 0, "degraded": False, "last_fail": 0.0}

def _get_display_pool():
    """展示通道常驻池（2 worker，长超时）。F2：统一由 app.pools 注册表管理。"""
    return pools.get_pool("display", 2)


def _inflight_wait(wait_list, wait_s):
    """等待在途结果（总预算 wait_s）。wait_list = [(code, ent)]——调用方直接持有
    Event 引用，发起者完成后 pop 不影响读取 ref（同一对象仍可访问）。
    返回 {code: quote}。不持 _lock。"""
    if not wait_list:
        return {}
    out = {}
    deadline = time.time() + wait_s
    for c, ent in wait_list:
        remain = deadline - time.time()
        if remain <= 0:
            break
        ent["ev"].wait(max(0.0, remain))
        ref = ent["ref"][0]
        if ref:
            out[c] = ref
    return out


def _inflight_checkout(codes, inflight, inflight_lock):
    """★ F1：原子 checkout——一次持锁完成"已在途判定 + 自己接管标记"，
    杜绝 wait/mark 之间的 TOCTOU 竞态（多线程同时看不到在途 → 重复拉取）。
    返回 (wait_list, mine_list)，元素均为 (code, ent)。"""
    wait_list, mine = [], []
    with inflight_lock:
        for c in codes:
            ent = inflight.get(c)
            if ent:
                wait_list.append((c, ent))
            else:
                ent = {"ev": threading.Event(),
                       "ref": [None], "ts": time.time()}
                inflight[c] = ent
                mine.append((c, ent))
    return wait_list, mine


def _inflight_done(codes, result, inflight, inflight_lock):
    """拉取完成：写 ref + set 事件 + 移除（等待者据此拿结果）。"""
    with inflight_lock:
        for c in codes:
            ent = inflight.get(c)
            if ent:
                ent["ref"][0] = (result or {}).get(c)
                ent["ev"].set()
        for c in codes:
            inflight.pop(c, None)


def _fetch_http_quotes(codes, http_timeout=10, pool=None, deadline=None):
    """腾讯 qt.gtimg 并发批量拉取（BATCH=400 分批，pool 并发，deadline 到点即止）。"""
    if not codes:
        return {}
    BATCH = 400
    from concurrent.futures import ThreadPoolExecutor as _TPE5, as_completed as _AC5

    def _fetch_batch(batch):
        symbols = ",".join(_prefix(c) for c in batch)
        try:
            text = _http(f"https://qt.gtimg.cn/q={symbols}", decode="gbk",
                         ref="https://gu.qq.com/", timeout=http_timeout)
            return _parse_tencent(text)
        except Exception:
            return {}

    batches = [codes[i:i + BATCH] for i in range(0, len(codes), BATCH)]
    if pool is not None:
        futs = {pool.submit(_fetch_batch, b): i for i, b in enumerate(batches)}
    else:
        ex = pools.get_pool("http", 4)
        futs = {ex.submit(_fetch_batch, b): i for i, b in enumerate(batches)}
    from concurrent.futures import as_completed as _AC6
    fetched = {}
    try:
        for f in _AC6(futs, timeout=None):
            if deadline and time.time() >= deadline:
                break          # ★ F1：整体 deadline 到点，丢弃未完成批次
            try:
                fetched.update(f.result())
            except Exception:
                pass
    except Exception:
        pass
    return fetched


def _fetch_sina_quotes(codes, http_timeout=10, pool=None, deadline=None):
    """新浪备用批量拉取（★ F1：由串行改并发，受整体 deadline 约束）。"""
    if not codes:
        return {}
    BATCH = 400
    from concurrent.futures import ThreadPoolExecutor as _TPE7, as_completed as _AC7

    def _fetch_batch(batch):
        sym2 = ",".join(_prefix(c) for c in batch)
        try:
            text = _http(f"https://hq.sinajs.cn/list={sym2}", decode="gbk",
                         ref="https://finance.sina.com.cn/", timeout=http_timeout)
            return _parse_sina(text)
        except Exception:
            return {}

    batches = [codes[i:i + BATCH] for i in range(0, len(codes), BATCH)]
    if pool is not None:
        futs = {pool.submit(_fetch_batch, b): i for i, b in enumerate(batches)}
    else:
        ex = pools.get_pool("sina", 4)
        futs = {ex.submit(_fetch_batch, b): i for i, b in enumerate(batches)}
    fetched = {}
    try:
        for f in _AC7(futs, timeout=None):
            if deadline and time.time() >= deadline:
                break
            try:
                fetched.update(f.result())
            except Exception:
                pass
    except Exception:
        pass
    return fetched


def _update_trading_state(mine, result):
    """★ F1 停摆自愈：交易通道拉取后更新连续失败计数。
    过半请求缺失 → streak+1；达到 TRADING_FETCH_FAIL_STREAK → 降级 + audit 事件。
    成功（缺失<50%）→ streak 清零并自动恢复 degraded。"""
    if not mine:
        return
    missing = [c for c in mine if c not in result]
    s = _TRADING_STATE
    if missing and len(missing) >= len(mine) * 0.5:
        s["fail_streak"] += 1
        s["last_fail"] = time.time()
    else:
        s["fail_streak"] = 0
        if s["degraded"]:
            s["degraded"] = False   # 自动恢复
    if (s["fail_streak"] >= getattr(C, "TRADING_FETCH_FAIL_STREAK", 3)
            and not s["degraded"]):
        s["degraded"] = True
        try:
            from . import audit
            audit.record(kind="trading", event="quote_channel_degraded", level="WARN",
                         fail_streak=s["fail_streak"],
                         note="交易行情通道连续失败，自动降级为只用 TDX/缓存（停摆自愈）")
        except Exception:
            pass


def trading_channel_state():
    """★ F1：交易通道状态（只读）。供 trader 以只读属性暴露给 trading/status（server 由 J 道接入）。"""
    s = dict(_TRADING_STATE)
    s["deadline_s"] = getattr(C, "TRADING_FETCH_DEADLINE_S", 8)
    s["wait_s"] = getattr(C, "QUOTE_SINGLEFLIGHT_WAIT_S", 8)
    return s


def _fetch_quotes_core(codes, force=False, enrich=False,
                       http_timeout=10, deadline_s=None, pool=None,
                       wait_s=None, inflight=None, inflight_lock=None,
                       http_disabled=False, enrich_timeout=10):
    """★ F1：fetch_quotes 共享核心。
    - single-flight：未命中缓存的 codes 先在途去重（在途等待硬超时 wait_s，不持 _lock）
    - TDX 优先 → HTTP 并发（pool 控制并发，deadline_s 整体截止，到点返回已获取部分）
    - 新浪回退并发 + deadline 约束
    - http_disabled=True：降级模式（只用 TDX/缓存，不发 HTTP）
    """
    if not codes:
        return {}
    codes = list(dict.fromkeys(codes))   # F1：去重——重复 code 会让 checkout 把"自己的重复项"当在途，自等超时
    now = time.time()
    deadline = time.time() + deadline_s if deadline_s else None
    result = {}
    todo = []
    with _lock:
        for c in codes:
            hit = _mem_quotes.get(c)
            if hit and not force and now - hit[0] < C.QUOTE_CACHE_SECONDS:
                result[c] = hit[1]
            else:
                todo.append(c)
    if not todo:
        _finalize_quotes(result, enrich, enrich_timeout)
        return result
    # single-flight：原子 checkout（在途判定+接管标记一次完成，杜绝竞态）
    wait_s = wait_s if wait_s is not None else getattr(C, "QUOTE_SINGLEFLIGHT_WAIT_S", 8)
    if inflight is None:
        inflight, inflight_lock = _INFLIGHT, _INFLIGHT_LOCK
    wait_list, mine = _inflight_checkout(todo, inflight, inflight_lock)
    if wait_list:
        got = _inflight_wait(wait_list, wait_s)
        if got:
            for c, q in got.items():
                result[c] = q
    if not mine:
        _finalize_quotes(result, enrich, enrich_timeout)
        return result
    mine_codes = [c for c, _ in mine]
    try:
        # TDX 优先（快 10 倍，实测 100 只 55ms）
        try:
            from . import tdx as _tdx
            if _tdx.available():
                fast = _tdx.fetch_quotes_fast(mine_codes)
                if fast:
                    for c, q in fast.items():
                        if q.get("price", 0) > 0:
                            result[c] = q
                            with _lock:
                                _mem_quotes[c] = (now, q)
        except Exception:
            pass
        todo2 = [c for c in mine_codes if c not in result]
        if todo2 and not http_disabled and (not deadline or time.time() < deadline):
            fetched = _fetch_http_quotes(todo2, http_timeout=http_timeout,
                                         pool=pool, deadline=deadline)
            # ★ F1：新浪回退并发（原串行）
            missing = [c for c in todo2 if c not in fetched]
            if missing and (not deadline or time.time() < deadline):
                fetched2 = _fetch_sina_quotes(missing, http_timeout=http_timeout,
                                              pool=pool, deadline=deadline)
                fetched.update(fetched2)
            with _lock:
                for c in todo2:
                    if c in fetched:
                        _mem_quotes[c] = (now, fetched[c])
                        result[c] = fetched[c]
    finally:
        _inflight_done(mine_codes, result, inflight, inflight_lock)
    _finalize_quotes(result, enrich, enrich_timeout)
    return result


def fetch_quotes(codes, force=False, enrich=False):
    """批量实时行情（内存 TTL 缓存 + ★ F1 single-flight 在途去重）。
    enrich: ★ P0-3 交易判定路径（扫描/打板）传 True —— 补齐 TDX 缺失的
    turnover/vol_ratio/float_mktcap（腾讯完整字段）；行情展示等高频低要求路径保持 False 不增加开销。
    通用/展示语义：10s 超时、默认池、无整体 deadline（行为与 F1 前一致，仅增加在途去重）。"""
    return _fetch_quotes_core(codes, force=force, enrich=enrich,
                              http_timeout=10, deadline_s=None, pool=None,
                              wait_s=getattr(C, "QUOTE_SINGLEFLIGHT_WAIT_S", 8),
                              inflight=_INFLIGHT, inflight_lock=_INFLIGHT_LOCK,
                              enrich_timeout=10)


def fetch_quotes_display(codes, enrich=False):
    """★ F1：显式展示通道（独立在途表 + 独立 2-worker 池 + 10s 超时）。
    绝不与交易通道共用池/在途表——展示慢请求阻塞不了交易。"""
    return _fetch_quotes_core(codes, force=False, enrich=enrich,
                              http_timeout=10, deadline_s=None,
                              pool=_get_display_pool(),
                              wait_s=getattr(C, "QUOTE_SINGLEFLIGHT_WAIT_S", 8),
                              inflight=_INFLIGHT_DISPLAY,
                              inflight_lock=_INFLIGHT_LOCK_DISPLAY,
                              enrich_timeout=10)


def _get_trading_pool():
    """交易通道常驻池（TRADING_POOL_WORKERS=6）。F2：统一由 app.pools 注册表管理。"""
    return pools.get_pool("trading", getattr(C, "TRADING_POOL_WORKERS", 6))


def fetch_quotes_trading(codes, enrich=True, force=False):
    """★ F1：交易通道（trader 实时判定专用）。
    - 独立常驻池（TRADING_POOL_WORKERS=6）+ 独立在途表（不等展示）
    - 独立超时（TRADING_HTTP_TIMEOUT=5s）+ 独立整体 deadline（TRADING_FETCH_DEADLINE_S=8s，
      到点立即返回已获取部分）
    - TDX 优先；HTTP 并发（绝不串行）；新浪回退并发
    - 停摆自愈：连续 TRADING_FETCH_FAIL_STREAK 次失败 → 降级（只用 TDX/缓存），
      成功自动恢复；状态经 trading_channel_state() 只读暴露。
    """
    dl = getattr(C, "TRADING_FETCH_DEADLINE_S", 8)
    if _TRADING_STATE.get("degraded"):
        # 降级：只用 TDX/缓存，HTTP 禁用，超短 deadline
        return _fetch_quotes_core(codes, force=force, enrich=enrich,
                                  http_timeout=getattr(C, "TRADING_HTTP_TIMEOUT", 5),
                                  deadline_s=2.0, pool=_get_trading_pool(),
                                  wait_s=2.0,
                                  inflight=_INFLIGHT_TRADING,
                                  inflight_lock=_INFLIGHT_LOCK_TRADING,
                                  http_disabled=True,
                                  enrich_timeout=getattr(C, "TRADING_HTTP_TIMEOUT", 5))
    return _fetch_quotes_core(codes, force=force, enrich=enrich,
                              http_timeout=getattr(C, "TRADING_HTTP_TIMEOUT", 5),
                              deadline_s=dl, pool=_get_trading_pool(),
                              wait_s=dl,
                              inflight=_INFLIGHT_TRADING,
                              inflight_lock=_INFLIGHT_LOCK_TRADING,
                              enrich_timeout=getattr(C, "TRADING_HTTP_TIMEOUT", 5))



# ★ Z6-P1(④)：overview/首页展示专用 30s 行情缓存。
#   选型：C.QUOTE_CACHE_SECONDS(3s) 被盘中实时路径共用（trader.py L823 打板判定 / engine.py
#   / ai/alert/risk 等），直接放宽会延迟盘中决策 → 不动常量，在 overview 取数处单独套 30s 缓存。
#   用法：server.py overview 分支 `df.fetch_quotes(codes)` → `df.fetch_quotes_overview(codes)`。
_OVERVIEW_CACHE_TTL = 30
_overview_cache = {}       # tuple(codes) -> (ts, {code: quote})，仅 overview 一个 key，无增长风险
_overview_lock = threading.Lock()


def fetch_quotes_overview(codes, enrich=False):
    """overview/首页展示专用行情：30s 内存缓存（Z6-P1：探活+首页展示 30s 足够）。
    ★ K8（2026-09-16）：升级 SWR 语义——有缓存（无论新旧）一律先返回；
    仅当缓存 age>TTL 时后台单飞续新（daemon 线程），本次调用不阻塞等待。
    无缓存（冷态）才同步取数（TDX→腾讯→新浪补缺链不变；调用方为后台线程时阻塞无妨）。
    force=True 绕过 3s 实时缓存，避免 overview 与盘中实时路径互相干扰。"""
    if not codes:
        return {}
    now = time.time()
    key = tuple(codes)
    with _overview_lock:
        hit = _overview_cache.get(key)
        if hit is not None:
            if now - hit[0] < _OVERVIEW_CACHE_TTL:
                return hit[1]
            # 过期：返回旧值 + 后台续新（SWR，绝不阻塞请求线程）
            _start_overview_bg_refresh(codes, enrich, key)
            return hit[1]
    quotes = fetch_quotes(codes, force=True, enrich=enrich)
    with _overview_lock:
        _overview_cache[key] = (now, quotes)
    return quotes


_overview_bg_refreshing = set()
_overview_bg_lock = threading.Lock()


def _start_overview_bg_refresh(codes, enrich, key):
    """单飞后台续新：同一 key 只允许一个在途刷新，写缓存时更新时间戳。"""
    with _overview_bg_lock:
        if key in _overview_bg_refreshing:
            return
        _overview_bg_refreshing.add(key)

    def _run():
        try:
            q = fetch_quotes(codes, force=True, enrich=enrich)
            with _overview_lock:
                _overview_cache[key] = (time.time(), q)
        except Exception:
            pass
        finally:
            with _overview_bg_lock:
                _overview_bg_refreshing.discard(key)
    threading.Thread(target=_run, daemon=True).start()


INDEX_CODES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}


def fetch_index_kline(code, days=600):
    """★ K1（2026-09-16）：指数日K（腾讯 fqkline）——收盘增量更新用。
    ★ F1（2026-09-08）修复后的映射：腾讯 fqkline 数组序为
      [date, open, close, high, low, volume]（6 元素，**无 amount**）。
      返回 [(code,'day',date,open,high,low,close,volume,amount=0.0)]，
      已过 _sane_rows 断言（H>=max(O,C)、L<=min(O,C)、volume>=0）。
    失败/为空返回 []（调用方负责审计，本函数不静默写库）。"""
    try:
        from .index_timing import _sane_rows   # ★ F1：通用 K 线行断言（个股/指数共用）
    except Exception:
        def _sane_rows(rows):
            return rows
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={code},day,,,{days},qfq")
    try:
        text = _http(url, timeout=15)
    except Exception:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    sd = (data.get("data") or {}).get(code) or {}
    raw = sd.get("qfqday") or sd.get("day") or []
    out = []
    for e in raw:
        try:
            if len(e) < 6:
                continue
            out.append((code, "day", e[0], float(e[1]), float(e[3]),
                        float(e[4]), float(e[2]), float(e[5]) * 100, 0.0))
        except (ValueError, IndexError, TypeError):
            continue
    return _sane_rows(out)


# ================= 指数成交额（东财 push2his，★ K10 2026-09-16） =================
# 腾讯 fqkline 指数无 amount（6 元素）。东财历史日K第 7 字段=成交额（元），
# 与腾讯 OHLCV 合并回填。独立熔断：本函数失败返回 {}（调用方记审计），
# 绝不抛异常阻塞 OHLCV 主路径（fetch_index_kline 照常写库）。
_EM_INDEX_SECID = {"sh000001": "1.000001", "sz399001": "0.399001",
                   "sz399006": "0.399006"}
# 东财限流偶发 RemoteDisconnected：_http 已有 MAX_RETRIES 重试，这里再兜一层
_EM_AMOUNT_CACHE = {}


def fetch_index_amount_em(code, beg, end, cache=True):
    """东财指数历史成交额（元）→ {date: amount}。
    - code: sh000001/sz399001/sz399006（INDEX_CODES 同源）；
    - beg/end: YYYYMMDD（含两端）；klt=101 日K、fqt=1 前复权（指数无除权影响）；
    - 每行字段序：[date, open, close, high, low, volume(手), amount(元), 振幅, 涨跌幅, 涨跌额, 换手]；
    - 失败返回 {}（独立熔断，主路径无感）；cache=True 时窗口内结果缓存（同窗口重复拉取去重）。
    """
    key = "%s:%s:%s" % (code, beg, end)
    if cache and key in _EM_AMOUNT_CACHE:
        return _EM_AMOUNT_CACHE[key]
    secid = _EM_INDEX_SECID.get(code)
    if not secid:
        return {}
    out = {}
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=%s&klt=101&fqt=1&beg=%s&end=%s"
           "&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61" % (secid, beg, end))
    try:
        text = _http(url, timeout=20, ref="https://quote.eastmoney.com/")
        data = json.loads(text)
        klines = (data.get("data") or {}).get("klines") or []
        for row in klines:
            p = row.split(",")
            if len(p) < 7:
                continue
            try:
                amt = float(p[6])
            except (ValueError, IndexError):
                continue
            if amt > 0:
                # key 统一为 YYYY-MM-DD（库内 kline.date 同格式；东财原生 YYYYMMDD）
                d = p[0]
                if len(d) == 8:
                    d = "%s-%s-%s" % (d[:4], d[4:6], d[6:])
                out[d] = amt
    except Exception:
        return {}   # 独立熔断：失败不抛，调用方审计
    if cache:
        _EM_AMOUNT_CACHE[key] = out
    return out


# 同花顺备源（东财 push2his 时段性限流时降级；数据与东财/库内交叉验证一致）
_THS_INDEX_SYM = {"sh000001": "hs_1A0001", "sz399001": "hs_399001",
                  "sz399006": "hs_399006"}


def fetch_index_amount_ths(code, days=20):
    """同花顺指数日K成交额（元）→ {date: amount}（备源，东财限流降级）。
    - URL: https://d.10jqka.com.cn/v6/line/{hs}/01/last{days}.js
    - 行: date,open,high,low,close,volume(股),amount(元),...；
    - 仅取 amount>0 行；失败返回 {}（独立熔断）。"""
    sym = _THS_INDEX_SYM.get(code)
    if not sym:
        return {}
    out = {}
    url = "https://d.10jqka.com.cn/v6/line/%s/01/last%d.js" % (sym, days)
    try:
        text = _http(url, timeout=20, ref="https://www.10jqka.com.cn/")
        import re
        m = re.search(r"\((\{.*\})\)", text, re.S)
        if not m:
            return {}
        data = json.loads(m.group(1))
        # 同花顺 data 行间用 ';' 分隔（实测 last10/last20），每行 11 字段
        rows = [r for r in ((data.get("data") or "") or "").split(";") if r]
        for row in rows:
            p = row.split(",")
            if len(p) < 7:
                continue
            try:
                amt = float(p[6])
            except (ValueError, IndexError):
                continue
            if amt > 0:
                d = p[0]
                if len(d) == 8:
                    d = "%s-%s-%s" % (d[:4], d[4:6], d[6:])
                out[d] = amt
    except Exception:
        return {}
    return out


def fetch_index_amount_today_em(code):
    """东财 push2 实时 f48（当日成交额，元）→ {今日date: amount}。
    - 仅用于当日（15:00 收盘后 f48=收盘成交额；盘中为实时值）；
    - 作为回填降级链的最后一环（东财历史/同花顺历史均缺当日时）；
    - 失败返回 {}（独立熔断）。"""
    secid = _EM_INDEX_SECID.get(code)
    if not secid:
        return {}
    url = ("https://push2.eastmoney.com/api/qt/stock/get?"
           "secid=%s&fields=f43,f48" % secid)
    try:
        text = _http(url, timeout=10, ref="https://quote.eastmoney.com/")
        data = json.loads(text)
        dt = data.get("data") or {}
        amt = float(dt.get("f48") or 0)
        if amt <= 0:
            return {}
        today = time.strftime("%Y-%m-%d")
        return {today: amt}
    except Exception:
        return {}


def fetch_all_stocks(enrich=False):
    """全A股实时行情（复用 fetch_quotes）。enrich: ★ P0-3 交易判定路径传 True"""
    lst = get_stock_list()
    if not lst:
        return {}
    return fetch_quotes([c for c, _, _ in lst], enrich=enrich)


# ★ BUG 修复（2026-09-08）：fetch_indices 加 5s TTL 缓存。
#   原实现每次调用都打腾讯 qt.gtimg 接口 → overview 高频刷新时指数反馈慢。
#   5s 对行情展示足够（指数不会秒变），与 fetch_quotes_overview 的 30s 缓存同思路。
_IDX_QUOTES_TTL = 5
_idx_quotes_cache = {"ts": 0.0, "data": None}


def fetch_indices():
    """三大指数实时行情（腾讯代码不带前缀，需映射）。5s TTL 缓存。"""
    now = time.time()
    if _idx_quotes_cache["data"] and now - _idx_quotes_cache["ts"] < _IDX_QUOTES_TTL:
        return _idx_quotes_cache["data"]
    codes = list(INDEX_CODES.keys())
    quotes = {}
    try:
        text = _http("https://qt.gtimg.cn/q=" + ",".join(codes), decode="gbk",
                     ref="https://gu.qq.com/", timeout=10)
        quotes = _parse_tencent(text)
    except Exception:
        pass
    out = []
    for sym, name in INDEX_CODES.items():
        q = quotes.get(sym[2:])
        if q:
            out.append({"symbol": sym, "name": name, **q})
    _idx_quotes_cache["ts"] = now
    _idx_quotes_cache["data"] = out
    return out


# ================= 历史 K 线 =================
def _fetch_kline_tencent(code, period="day", days=250, adjust="qfq"):
    """腾讯日K（adjust: qfq前复权/hfq后复权）或分钟K。返回 klines list。
    ★ 4.2：支持后复权（hfq）——长期回测优先用后复权，历史价格不受后续除权漂移。
    ★ E4（2026-09-13）：日K被调用即降级（tdx 失败后才会走此路径）→ 留痕
      source_degradation.jsonl（腾讯为无额源，amount 恒缺，收盘统一补额）。
    """
    if period == "day":
        _record_degradation(code, "tencent", ["amount"])
    sym = _prefix(code)
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sym},{period},,,{days},{adjust}")
    text = _http(url, timeout=15)
    data = json.loads(text)
    sd = data.get("data", {}).get(sym, {})
    key = adjust + period if period == "day" else period
    raw = sd.get(key) or sd.get(period) or []
    out = []
    for e in raw:
        try:
            if len(e) < 6:
                continue
            out.append({
                "date": e[0], "open": float(e[1]), "close": float(e[2]),
                "high": float(e[3]), "low": float(e[4]),
                "volume": float(e[5]) * 100,  # 手→股
                "amount": float(e[6]) if len(e) > 6 else 0.0,
            })
        except (ValueError, TypeError, IndexError):
            continue
    return out


def _fetch_kline_sina(code, period="day", days=250):
    """新浪日K（备用；不复权）
    ★ E4（2026-09-13）：日K被调用即降级 → 留痕 source_degradation.jsonl
      （新浪为无额源，amount 恒缺，收盘统一补额）。
    """
    if period == "day":
        _record_degradation(code, "sina", ["amount"])
    sym = _prefix(code)
    scale = {"day": 240, "week": 1200, "min5": 5, "min15": 15,
             "min30": 30, "min60": 60}.get(period, 240)
    if period == "day":
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen={days}")
    else:
        url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
               f"CN_MarketDataService.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen={days}")
    text = _http(url, timeout=15)
    data = json.loads(text)
    out = []
    if not isinstance(data, list):
        return out
    for e in data:
        try:
            out.append({
                "date": e.get("day", ""), "open": float(e["open"]),
                "close": float(e["close"]), "high": float(e["high"]),
                "low": float(e["low"]), "volume": float(e.get("volume", 0)),
                "amount": float(e.get("amount", 0) or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_kline(code, period="day", days=250, force=False, adjust=None):
    """带双层缓存的K线获取。period: day/week/min5/min15/min30/min60
    v3.6：min5 优先读本地 kline_min5 表（一年分钟数据，import_min5.py 导入）；
    无本地数据时回退新浪接口。
    adjust: "hfq"后复权（回测用，历史稳定）/ "qfq"前复权（默认，对齐现价）/
            None（按 C.BACKTEST_ADJUST，现为 qfq 前复权）
    """
    if period == "day":
        return _fetch_daily(code, days, force, adjust=adjust)
    # ★ v3.6：本地一年 5 分钟数据优先
    if period == "min5":
        local = _kline_min5_load(code)
        if local:
            with _lock:
                _mem_kline[(code, period)] = (time.time(), local)
            if days and len(local) > days:
                return local[-days:]
            return local
    # 分钟K：内存缓存 + 磁盘缓存
    key = (code, period)
    now = time.time()
    with _lock:
        hit = _mem_kline.get(key)
        if hit and not force and now - hit[0] < C.KLINE_CACHE_SECONDS:
            return hit[1]
    count, max_date = _kline_meta(code, period)
    need_full = count < min(days, 60) or not max_date
    if need_full:
        try:
            k = _fetch_kline_sina(code, period, days)
            if k:
                _kline_save(code, period, k)
        except Exception:
            pass
    k = _kline_load(code, period)
    with _lock:
        _mem_kline[key] = (now, k)
    return k


def _min5_conn():
    """打开分钟分库 min5.db（v3.7）。首次使用自动从 market.db 迁移 kline_min5 表。
    返回 sqlite3.Connection 或 None（失败时）。
    """
    try:
        conn = sqlite3.connect(C.MIN5_DB_FILE, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS kline_min5(
            code TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY(code, date))""")
        # ★ J3 验收修复（2026-09-14）：不再重建冗余索引 idx_kmin5_c——已被 PK (code, date) 前缀完全覆盖（见 docs/reports/sqlite_pragma_j3_20260913.md；195.3MB 为 market.db 的 idx_kline_cp 释放量，与本索引无关）。原语句会使 DROP 不可持久。
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_kmin5_c ON kline_min5(code)")
        # 首次迁移：market.db 中已有 kline_min5 表 → 复制到 min5.db
        cur = conn.execute("SELECT COUNT(*) FROM kline_min5")
        if cur.fetchone()[0] == 0:
            try:
                src = sqlite3.connect(C.DB_FILE, timeout=15)
                try:
                    cnt = src.execute("SELECT COUNT(*) FROM kline_min5").fetchone()[0]
                    if cnt > 0:
                        conn.execute("ATTACH DATABASE ? AS src", (C.DB_FILE,))
                        conn.execute(
                            "INSERT OR REPLACE INTO kline_min5 SELECT * FROM src.kline_min5")
                        conn.execute("DETACH DATABASE src")
                        conn.commit()
                finally:
                    src.close()
            except Exception:
                pass
        return conn
    except Exception:
        return None


def _kline_min5_load(code, start=None, end=None):
    """从本地一年分钟表读取 5 分钟K（v3.6 + v3.7 分库瘦身）。
    返回 [{date,open,high,low,close,volume}]
    注意：导入时按文件名存储（带 sh/sz 前缀），此处统一转换。
    v3.7：数据位于独立 min5.db（market.db 只留日线/账户）。
    """
    sym = code if code.startswith(("sh", "sz", "bj")) else _prefix(code)
    conn = _min5_conn()
    if conn is None:
        return []
    try:
        with _db_lock:
            if start and end:
                cur = conn.execute(
                    "SELECT date,open,high,low,close,volume FROM kline_min5 "
                    "WHERE code=? AND date>=? AND date<=? ORDER BY date", (sym, start, end))
            else:
                cur = conn.execute(
                    "SELECT date,open,high,low,close,volume FROM kline_min5 "
                    "WHERE code=? ORDER BY date", (sym,))
            return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
                     "close": r[4], "volume": r[5], "amount": 0.0} for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _is_trading_time():
    now = time.localtime()
    if now.tm_wday >= 5:
        return False
    hm = now.tm_hour * 100 + now.tm_min
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def _today_str():
    return time.strftime("%Y-%m-%d")


def fetch_minute(code):
    """当日1分钟分时K线（新浪 scale=1），过滤出最近交易日。
    ★ 4.4：加 3 秒内存缓存 —— 分时图每 5 秒刷新不再重复打新浪（之前每次请求都打网络）。
    """
    now = time.time()
    with _lock:
        hit = _mem_minute.get(code)
        if hit and now - hit[0] < C.MINUTE_CACHE_SECONDS:
            return hit[1]
    sym = _prefix(code)
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
           f"CN_MarketDataService.getKLineData?symbol={sym}&scale=1&ma=no&datalen=240")
    try:
        text = _http(url, timeout=10)
        data = json.loads(text)
    except Exception:
        return []
    out = []
    if not isinstance(data, list):
        return out
    for e in data:
        try:
            out.append({
                "date": e.get("day", ""), "open": float(e["open"]),
                "close": float(e["close"]), "high": float(e["high"]),
                "low": float(e["low"]), "volume": float(e.get("volume", 0)),
                "amount": float(e.get("amount", 0) or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    if out:
        last_day = out[-1]["date"][:10]
        out = [x for x in out if x["date"][:10] == last_day]
    with _lock:
        _mem_minute[code] = (now, out)
    return out


def _fetch_daily(code, days=250, force=False, adjust=None):
    # ★ 4.2：复权方式 —— adjust 显式指定(hfq/qfq)优先，否则 None=前复权(对外展示对齐现价)
    adj = adjust or "qfq"
    period = "day_hfq" if adj == "hfq" else "day"
    key = (code, period)
    now = time.time()
    with _lock:
        hit = _mem_kline.get(key)
        if hit and not force and now - hit[0] < C.KLINE_CACHE_SECONDS:
            k = hit[1]
            if days and len(k) > days:
                return k[-days:]
            if days and len(k) >= days:
                return k
            # 缓存条数不足（如 250→270），不命中缓存，继续走磁盘/网络补齐
    count, max_date = _kline_meta(code, period)
    # 磁盘缓存是否够用：条数足够 且 最后日期接近最近交易日
    # ★ 4.2：用交易日历判断"最近交易日"（周末/节假日不误判数据过期）
    try:
        from . import trading_calendar as tcal
        expect = tcal.prev_trading_day(_today_str())
    except Exception:
        expect = _today_str()
    fresh = count >= min(days, 80) and max_date >= expect
    if not fresh and not force:
        try:
            # ★ 4.5：通达信优先（快 10 倍，支持 qfq 本地复权），腾讯次之，新浪兜底
            k = None
            try:
                from . import tdx as _tdx
                if _tdx.available():
                    k = _tdx.fetch_kline_fast(code, "day", max(days, 80), adjust=adj)
            except Exception:
                k = None
            if not k:
                k = _fetch_kline_tencent(code, "day", max(days, 80), adjust=adj)
            if not k:
                k = _fetch_kline_sina(code, "day", max(days, 80))
            if k:
                _kline_save(code, period, k)
        except Exception:
            pass
    k = _kline_load(code, period)
    if days and len(k) > days:
        k = k[-days:]
    with _lock:
        _mem_kline[key] = (now, k)
    return k


# ================= 全市场列表 =================
def get_stock_list(force=False):
    """全A股代码列表 [(code,name,price)]，缓存6小时"""
    global _mem_list, _mem_list_ts
    now = time.time()
    if _mem_list and not force and now - _mem_list_ts < C.LIST_CACHE_HOURS * 3600:
        return _mem_list
    if os.path.exists(C.LIST_CACHE) and not force:
        age = now - os.path.getmtime(C.LIST_CACHE)
        if age < C.LIST_CACHE_HOURS * 3600:
            try:
                with open(C.LIST_CACHE, "r", encoding="utf-8") as f:
                    _mem_list = json.load(f)
                _mem_list_ts = now
                return _mem_list
            except Exception:
                pass
    # 新浪分页拉取（~75页，★ 优化：并发拉取，35秒→3~5秒），失败用旧缓存
    all_items = []
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed as _ac

        def _fetch_page(page):
            url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node=hs_a")
            try:
                text = _http(url, timeout=12, ref="https://finance.sina.com.cn/")
                return json.loads(text)
            except Exception:
                return []

        pages = range(1, 78)
        with pools.get_pool("all_stocks", 16) as ex:
            futs = {ex.submit(_fetch_page, p): p for p in pages}
            results = {}
            for f in _ac(futs):
                results[futs[f]] = f.result()
        for page in range(1, 78):
            data = results.get(page) or []
            if not data:
                continue
            for item in data:
                code = item.get("symbol", "")[2:]
                name = item.get("name", "")
                if not code or not name or code.startswith(("4", "8", "92")):
                    continue
                if "ST" in name or "退" in name:
                    continue
                try:
                    price = float(item.get("trade", 0) or 0)
                except (ValueError, TypeError):
                    price = 0.0
                all_items.append([code, name, price])
    except Exception:
        pass
    if len(all_items) > 1000:
        try:
            with open(C.LIST_CACHE, "w", encoding="utf-8") as f:
                json.dump(all_items, f, ensure_ascii=False)
        except Exception:
            pass
        _mem_list, _mem_list_ts = all_items, now
        return all_items
    # 兜底旧缓存
    if os.path.exists(C.LIST_CACHE):
        try:
            with open(C.LIST_CACHE, "r", encoding="utf-8") as f:
                _mem_list = json.load(f)
            _mem_list_ts = now
            return _mem_list
        except Exception:
            pass
    return []


def prefetch_list_async():
    """后台预取股票列表（不阻塞启动）"""
    def _run():
        try:
            get_stock_list()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


_init_db()
prefetch_list_async()
