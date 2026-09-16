# -*- coding: utf-8 -*-
"""★ 4.5 业绩事件驱动模块 —— 借鉴 qlib ann_date 防前视 / ai-hedge-fund 基本面信号
补齐因子库"全价量"的盲区：业绩预增/预减是最强题材催化之一，
纯技术面看不到"基本面拐点"。

数据源：东财 datacenter RPT_PUBLIC_OP_NEWPREDICT（业绩预告，免费零鉴权，纯 urllib）。
字段：SECURITY_CODE / ADD_AMP_LOWER·UPPER(预增幅度%) / NOTICE_DATE(公告日) /
      REPORT_DATE(报告期) / PREDICT_CONTENT(摘要) / CHANGE_REASON_EXPLAIN(原因)

能力：
  earnings_signal(code)   个股业绩信号（近90天预告：预增/预减/扭亏/首亏）
  earnings_stocks()       全市场近期业绩预增榜（Web 展示）
  PIT 合规：按 NOTICE_DATE 对齐信号日（公告日后才生效，杜绝未来函数）

★ I1（2026-09-13）改造：
  1. fetch_earnings 增加 as_of：窗口 [as_of-days, as_of]（as_of=None=今天，行为不变），
     东财 filter_sql 按 NOTICE_DATE 区间过滤分页——历史 PIT 可见性从"恒 0"修复；
  2. 增加 offline 显式参数：offline=True 时只读本地库、绝不发 HTTP
     （回测上下文由调用点显式传，不靠全局猜测）；
  3. _save 在 offline 模式跳过（回测期零写库）；
  4. CREATE TABLE/INDEX/WAL 移到模块初始化 _init_db() 执行一次，连接不再重复建表；
  5. BACKTEST_OFFLINE 常量（config 追加块）作默认开关提示，但取数仍以显式参数为准。
"""
import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import config as C
from . import db as _db            # ★ J3：统一连接工厂

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_REF = "https://data.eastmoney.com/"
_lock = threading.Lock()
_mem = {}          # key -> (ts, value)

# ---- 模块初始化：建表/索引/WAL 一次性执行（I1：从 _conn 移出）----
_init_lock = threading.Lock()
_init_done = False


def _init_db():
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        try:
            conn = _db.open_rw(C.DB_FILE)   # ★ J3：统一连接工厂（WAL 初始化已由 db.py 一次完成）
            conn.execute("""CREATE TABLE IF NOT EXISTS earnings(
                code TEXT, notice_date TEXT, report_date TEXT, type TEXT,
                amp_lower TEXT, amp_upper TEXT, payload TEXT,
                PRIMARY KEY(code, notice_date, report_date, type, amp_lower, amp_upper))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_earn_d ON earnings(notice_date)")
            conn.close()
            _init_done = True
        except Exception:
            pass


_init_db()


def _http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": _REF})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _dc(report_name, page_size=50, sort="NOTICE_DATE", filter_sql="", page_number=1):
    params = {"reportName": report_name, "columns": "ALL", "pageNumber": page_number,
              "pageSize": page_size, "sortColumns": sort, "sortTypes": -1}
    if filter_sql:
        params["filter"] = filter_sql
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           + urllib.parse.urlencode(params))
    d = _http_json(url)
    return (d.get("result") or {}).get("data") or []


def _conn(readonly=False):
    """只读连接走统一工厂读连接（mode=ro + PRAGMA，线程内复用）；
    读写连接仅 _save 使用（每次新建，保持写后 close 语义）。"""
    if readonly:
        return _db.open_ro(C.DB_FILE, 15000)
    return _db.open_rw(C.DB_FILE)


def _save(rows, offline=False):
    """入库（I1：offline=True 时跳过——回测期零写库）。
    复合主键 (code,notice_date,report_date,type) 全量唯一存储——
    东财同一公告可能含多条不同指标（净利润/扣非/营收），不折叠丢信息；
    INSERT OR REPLACE 幂等，重跑不重复。"""
    if offline:
        return
    try:
        with _lock:
            conn = _conn()
            try:
                for r in rows:
                    conn.execute(
                        "INSERT OR REPLACE INTO earnings(code,notice_date,report_date,type,"
                        "amp_lower,amp_upper,payload) VALUES(?,?,?,?,?,?,?)",
                        (r["code"], r["notice_date"], r["report_date"], r.get("type", ""),
                         str(r.get("amp_lower")), str(r.get("amp_upper")),
                         json.dumps(r, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _load(days=60, as_of=None):
    """本地库读取 [as_of-days, as_of] 窗口（I1：as_of 化窗口，只读 URI）。"""
    try:
        end = as_of or date.today().isoformat()
        since = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
        with _lock:
            conn = _conn(readonly=True)
            cur = conn.execute(
                "SELECT payload FROM earnings WHERE notice_date>=? AND notice_date<=? "
                "ORDER BY notice_date DESC LIMIT 50000",
                (since, end))
            return [json.loads(r[0]) for r in cur.fetchall()]
    except Exception:
        return []


def _dedup(rows):
    """完全重复行去重（复合键=全部业务字段）——东财分页偶发返回重复行。
    保留所有 (code,notice_date,report_date,type) 唯一组合，不折叠不同指标。"""
    seen = set()
    out = []
    for r in rows:
        k = (r["code"], r["notice_date"], str(r.get("report_date", "")),
             r.get("type", ""), str(r.get("amp_lower")), str(r.get("amp_upper")))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _normalize(r):
    """东财行 → 内部 dict（I1 抽函数：联网/回填共用同一映射）。"""
    code = r.get("SECURITY_CODE", "")
    if not code:
        return None
    low = r.get("ADD_AMP_LOWER")
    up = r.get("ADD_AMP_UPPER")
    fcode = r.get("PREDICT_FINANCE_CODE", "")
    etype = "其他"
    if fcode == "006":  # 营业收入（幅度型）
        etype = "预增" if (low or 0) > 0 else ("预减" if (up or 0) < 0 else "不确定")
    elif fcode in ("013", "014"):  # 净利润 预增/预减 等
        if (low or 0) > 0 and (up or 0) > 0:
            etype = "预增"
        elif (low or 0) < 0 and (up or 0) < 0:
            etype = "预减"
    return {
        "code": code,
        "name": r.get("SECURITY_NAME_ABBR", code),
        "notice_date": str(r.get("NOTICE_DATE", ""))[:10],
        "report_date": str(r.get("REPORT_DATE", ""))[:10],
        "type": etype,
        "amp_lower": low,
        "amp_upper": up,
        "content": r.get("PREDICT_CONTENT", ""),
        "reason": r.get("CHANGE_REASON_EXPLAIN", ""),
    }


def _fetch_range(since, end, page_size=500, max_pages=40):
    """按 NOTICE_DATE 区间分页拉取（I1 核心：服务端 filter 分页）。"""
    flt = "(NOTICE_DATE>='%s')(NOTICE_DATE<='%s')" % (since, end)
    out = []
    for page in range(1, max_pages + 1):
        rows = _dc("RPT_PUBLIC_OP_NEWPREDICT", page_size=page_size,
                   filter_sql=flt, page_number=page)
        if not rows:
            break
        for r in rows:
            item = _normalize(r)
            if item:
                out.append(item)
        if len(rows) < page_size:
            break
    return out


def fetch_earnings(days=60, force=False, as_of=None, offline=False):
    """全市场近期业绩预告（公告日起算，避免未来函数）。
    days: 回看天数（默认60天，覆盖一个财报季主要披露期）。
    as_of: 信号日（PIT 窗口 [as_of-days, as_of]；None=今天，保持旧行为）。
    offline: True=只读本地库绝不 HTTP（回测上下文显式传入）。
    返回 [{code,name,notice_date,report_date,type,amp_lower,amp_upper,content,reason}]
    """
    end = (as_of or date.today().isoformat())
    since = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    key = ("earn", days, end, offline)
    now = time.time()
    if not force:
        hit = _mem.get(key)
        if hit and now - hit[0] < 600:
            return hit[1]
    if offline:
        cached = _load(days, as_of=end)
        _mem[key] = (now, cached)
        return cached
    try:
        out = _fetch_range(since, end)
        out = [x for x in out if since <= x["notice_date"] <= end]
        _save(out)
        out = _dedup(out)
        _mem[key] = (now, out)
        return out
    except Exception:
        cached = _load(days, as_of=end)
        if cached:
            _mem[key] = (now, cached)
            return cached
        return []


def earnings_signal(code, days=60, force=False, as_of=None, offline=False):
    """个股业绩信号：近 days 天（截至 as_of 日）是否有预增/预减公告。
    ★ I1：as_of 传信号日（回测用），杜绝未来函数 —— 只认 as_of 之前已公告的业绩。
    offline=True 时只读本地库（回测禁网）。
    返回 (bonus, signals)：
      pre-increase: 预增 → 题材催化加分（公告日后生效，PIT 合规）
    """
    try:
        rows = [r for r in fetch_earnings(days, force, as_of=as_of, offline=offline)
                if r["code"] == code]
        if not rows:
            return 0, []
        # PIT 双保险：只保留公告日 ≤ as_of（as_of=None 表示今天）
        if as_of:
            rows = [r for r in rows if r["notice_date"] <= as_of]
            if not rows:
                return 0, []
        latest = max(rows, key=lambda x: (x["notice_date"], x.get("report_date") or ""))
        t = latest.get("type", "")
        amp = latest.get("amp_lower") or latest.get("amp_upper") or 0
        if t == "预增":
            bonus = 8 if amp >= 50 else (6 if amp >= 20 else 4)
            return bonus, [f"业绩预增{amp:+.0f}%(+{bonus},{latest['notice_date']})"]
        if t == "预减":
            return -6, [f"业绩预减{amp:+.0f}%(-6,{latest['notice_date']})"]
        return 0, []
    except Exception:
        return 0, []


def earnings_board(days=14, force=False):
    """近期业绩预增榜（Web 展示）：预增幅度排序 TOP 20"""
    rows = fetch_earnings(days, force)
    ups = [r for r in rows if r["type"] == "预增"]
    ups.sort(key=lambda x: -(x.get("amp_lower") or 0))
    return ups[:20]
