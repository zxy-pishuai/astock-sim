# -*- coding: utf-8 -*-
"""★ 4.5 数据自动增量更新模块 —— 解决"数据永远停留在导入日"的问题
之前：min5.db 数据靠手动 import_min5.py 一次性导入，之后永不更新；
      日K有新鲜度判断但只在单股请求时惰性更新。

本模块：纯标准库，启动时 + 每日收盘后 自动增量补库：
  1. min5 增量：腾讯 mkline 接口拉 320 根 5 分钟K（覆盖约 40 个交易日）→ 补进 min5.db
  2. 日K 增量：腾讯 fqkline 拉日K → 复用 _kline_save 幂等 upsert 补进 market.db
  3. 频率控制：启动一次 + 每交易日 15:10 后一次；后台线程，不阻塞服务
"""
import json
import math
import os
import sqlite3
import threading
import time
import urllib.request

from . import config as C
from . import db as _db            # ★ J3：统一连接工厂（★ 验收修复 2026-09-15：补回缺失导入）
from . import datafeed as df

_LAST_UPDATE_KEY = "data_update"   # 记录上次更新日期，避免重复


def _http_json(url, timeout=15, ref="https://gu.qq.com/"):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": ref})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _min5_conn():
    try:
        conn = _db.open_rw(C.MIN5_DB_FILE)   # ★ J3：统一连接工厂（WAL 初始化已由 db.py 一次完成）
        conn.execute("""CREATE TABLE IF NOT EXISTS kline_min5(
            code TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY(code, date))""")
        # ★ J3 验收修复（2026-09-14）：不再重建冗余索引 idx_kmin5_c——已被 PK (code, date) 前缀完全覆盖（见 docs/reports/sqlite_pragma_j3_20260913.md；195.3MB 为 market.db 的 idx_kline_cp 释放量，与本索引无关）。原语句会使 DROP 不可持久。
        # conn.execute("CREATE INDEX IF NOT EXISTS idx_kmin5_c ON kline_min5(code)")
        return conn
    except Exception:
        return None


def _min5_last_date(conn, code):
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM kline_min5 WHERE code=?", (code,)).fetchone()
        return row[0] or ""
    except Exception:
        return ""


def _strip_prefix(code):
    """★ P0-2：min5 库存储 key 混合带 sh/sz 前缀（sh510300）与裸代码（600519），
    拉取前统一转裸代码（TDX/腾讯接口都只认裸代码）"""
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            return code[2:]
    return code


def _fetch_tx_min5(code, count=320):
    """5分钟K（多日）。★ 通达信优先（快10倍），失败回退腾讯 mkline。
    返回 [{date,open,high,low,close,volume,amount}]"""
    bare = _strip_prefix(code)
    try:
        from . import tdx as _tdx
        if _tdx.available():
            k = _tdx.fetch_kline_fast(bare, "min5", min(count, 800))
            if k:
                return k
    except Exception:
        pass
    sym = df._prefix(bare)
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m5,,{count}"
    try:
        data = _http_json(url)
    except Exception:
        return []
    sd = (data.get("data") or {}).get(sym) or {}
    rows = sd.get("m5") or []
    out = []
    for e in rows:
        try:
            if len(e) < 6:
                continue
            ts = str(e[0])   # YYYYMMDDHHMM
            dt = (f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:00"
                  if len(ts) >= 12 else "")
            if not dt:
                continue
            out.append({
                "date": dt, "open": float(e[1]), "close": float(e[2]),
                "high": float(e[3]), "low": float(e[4]),
                "volume": float(e[5]) * 100,   # 手 → 股
                "amount": 0.0,
            })
        except (ValueError, IndexError, TypeError):
            continue
    return out


def _update_min5_one(conn, code, count=320):
    """单只股票 min5 增量补库。返回新增根数。"""
    rows = _fetch_tx_min5(code, count)
    if not rows:
        return 0
    last = _min5_last_date(conn, code)
    new_rows = [r for r in rows if r["date"] > last] if last else rows
    if not new_rows:
        return 0
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO kline_min5(code,date,open,high,low,close,volume) "
            "VALUES(?,?,?,?,?,?,?)",
            [(code, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"])
             for r in new_rows])
        conn.commit()
    except Exception:
        return 0
    return len(new_rows)


def _min5_dynamic_codes(target=None):
    """★ P62：min5 动态覆盖清单 —— bt_pool 前500 ∪ 当前持仓 ∪ 近期候选
    （自选 + 近90日交易 + 既有 min5 库内），不足 target 只时以全市场成交额
    top 补足。返回 (codes 列表, 来源计数 dict)。"""
    if target is None:
        target = getattr(C, "MIN5_COVERAGE_TARGET", 800)
    codes, src = set(), {}
    try:
        with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
            pool = [c for c in (json.load(f).get("codes") or [])[:500] if c]
        codes.update(pool)
        src["bt_pool_top500"] = len(pool)
    except Exception:
        pass
    try:
        from . import state as st
        acct = st.load_account()
        pos = set((acct.get("positions") or {}).keys())
        codes |= pos
        src["positions"] = len(pos)
        wl = {w.get("code") for w in (st.load_watchlist().get("watchlist") or [])
              if w.get("code")}
        codes |= wl
        src["watchlist"] = len(wl)
        import datetime as _dt
        cut = (_dt.datetime.now() - _dt.timedelta(days=90)).strftime("%Y-%m-%d")
        recent = {t.get("code") for t in (acct.get("trades") or [])
                  if str(t.get("time", ""))[:10] >= cut and t.get("code")}
        codes |= recent
        src["recent_trades_90d"] = len(recent)
    except Exception:
        pass
    try:
        conn = _min5_conn()
        if conn:
            m5codes = {r[0] for r in conn.execute(
                "SELECT DISTINCT code FROM kline_min5")}
            codes |= m5codes
            src["existing_min5"] = len(m5codes)
            conn.close()
    except Exception:
        pass
    need = max(0, target - len(codes))
    if need:
        try:
            stocks = df.fetch_all_stocks()
            ranked = sorted(((c, q.get("amount") or 0) for c, q in stocks.items()
                             if c not in codes), key=lambda kv: -kv[1])
            topup = [c for c, _ in ranked[:need]]
            codes |= set(topup)
            src["amount_topup"] = len(topup)
        except Exception:
            pass
    return sorted(codes), src


def update_min5(codes=None, limit=None, verbose=False):
    """min5 增量更新。
    ★ P62：codes 与 limit 均缺省时走动态覆盖清单（bt_pool 前500 ∪ 持仓 ∪
    近期候选，补足至 MIN5_COVERAGE_TARGET≥800）；显式传 codes 用指定清单；
    显式传 limit 走旧"库内前 limit 只"路径（0=库内全部）。"""
    if codes is None and limit is not None:
        codes = []
        try:
            conn = _min5_conn()
            if conn:
                if limit and limit > 0:
                    rows = conn.execute(
                        "SELECT DISTINCT code FROM kline_min5 ORDER BY code LIMIT ?",
                        (limit,)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT DISTINCT code FROM kline_min5 ORDER BY code").fetchall()
                codes = [r[0] for r in rows]
                conn.close()
        except Exception:
            pass
    elif codes is None:
        codes, src = _min5_dynamic_codes()
        if verbose:
            print(f"  min5 动态清单 {len(codes)} 只：{src}")
    if not codes:
        return {"updated": 0, "rows": 0, "errors": 0, "empty": 0, "total": 0}
    conn = _min5_conn()
    if conn is None:
        return {"updated": 0, "rows": 0, "errors": 1, "empty": 0, "total": len(codes)}
    updated = added = errors = empty = 0
    t0 = time.time()
    # ★ 4.5：并发拉取（腾讯 mkline 支持并发），收集结果后串行写入（SQLite 单连接安全）
    #   ★ P62 实测：清单扩到 1002 只后，8 并发稳态 ~24s（≈现状 3×，超 1.5× 目标）；
    #   提到 16 并发反而更慢（远端按 IP 限流，31.7s），故维持默认并发。
    #   后续优化路径：TDX 批量接口 或 清单分片隔日轮换。
    from concurrent.futures import ThreadPoolExecutor, as_completed
    fetched = {}   # code -> rows
    with ThreadPoolExecutor(max_workers=C.PARALLEL_WORKERS) as ex:
        futs = {ex.submit(_fetch_tx_min5, code): code for code in codes}
        for f in as_completed(futs):
            code = futs[f]
            try:
                rows = f.result()
                if rows:
                    fetched[code] = rows
                else:
                    empty += 1
            except Exception:
                errors += 1
    # 串行增量写入
    for code, rows in fetched.items():
        try:
            last = _min5_last_date(conn, code)
            new_rows = [r for r in rows if r["date"] > last] if last else rows
            if new_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO kline_min5(code,date,open,high,low,close,volume) "
                    "VALUES(?,?,?,?,?,?,?)",
                    [(code, r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"])
                     for r in new_rows])
                conn.commit()
                updated += 1
                added += len(new_rows)
        except Exception:
            errors += 1
        if verbose and (updated + errors) % 100 == 0:
            print(f"  min5 更新 完成{updated}只 新增{added}根 用时{time.time()-t0:.0f}s")
    conn.close()
    # ★ P0-2：total=候选总数（供 run_update 判定"基本成功"）
    return {"updated": updated, "rows": added, "errors": errors, "empty": empty,
            "total": len(codes)}


def _fetch_daily_one(code, days):
    """★ 4.5：单票日K取数——通达信优先（快10倍，qfq 本地复权），腾讯次之，新浪兜底。
    E1（2026-09-03）：从 update_daily 内联抽出，供常规/stale-first/回补三处复用，链路不变。
    A2（2026-09-13）：返回 (k, src)，src ∈ {tdx, tencent, sina, None}——供 _fetch_and_save
    按源统计 amount 缺失（腾讯/新浪为无额源，tdx 熔断时会全量降级）。"""
    try:
        from . import tdx as _tdx
        if _tdx.available():
            k = _tdx.fetch_kline_fast(code, "day", max(days, 80), adjust="qfq")
            if k:
                return k, "tdx"
    except Exception:
        pass
    k = df._fetch_kline_tencent(code, "day", max(days, 80))
    if k:
        return k, "tencent"
    k = df._fetch_kline_sina(code, "day", max(days, 80))
    return (k, "sina") if k else (None, None)


def _in_intraday_window():
    """★ H3（2026-09-06）：是否盘中交易时段（stale-first 守卫用：不写当日残缺 bar）。
    复用 trading_calendar.is_trading_day（节假日/周末）+ 9:15-15:05 时段（与 tdx._is_market_hours 同口径）。
    非交易日或盘后返回 False——此时当日 bar 已收盘定型，可正常写入。"""
    from .trading_calendar import is_trading_day
    today = time.strftime('%Y-%m-%d')
    if not is_trading_day(today):
        return False
    now = time.localtime()
    hm = now.tm_hour * 100 + now.tm_min
    return 915 <= hm <= 1505


def _fetch_and_save(codes, days, workers=None, skip_today=False):
    """并发取数 + 幂等 upsert（复用 _kline_save 既有保存函数，不 DELETE 不改表）。
    ★ H3：skip_today=True 时过滤当日 bar（盘中残缺数据不入库），历史 bar 照写。
    ★ A1（2026-09-13）：返回值新增 failed 列表（三源均无数据/异常的 code），
    供 stale-first 熔断判断——区分"拉取失败可重试"与"数据源确认无此票"。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    w = workers or C.PARALLEL_WORKERS
    fetched, errors = {}, 0
    failed = []
    with ThreadPoolExecutor(max_workers=w) as ex:
        futs = {ex.submit(_fetch_daily_one, code, days): code for code in codes}
        for f in as_completed(futs):
            code = futs[f]
            try:
                k, src = f.result()
                if k:
                    fetched[code] = (k, src)
                else:
                    failed.append(code)
            except Exception:
                errors += 1
                failed.append(code)
    today_str = time.strftime('%Y-%m-%d') if skip_today else None
    missing_by_src = {}
    for code, (k, src) in fetched.items():
        try:
            if skip_today and k:
                k = [row for row in k if row.get('date') != today_str]
            res = df._kline_save(code, "day", k) or {}
            miss = res.get("amount_missing") or []
            if miss:
                missing_by_src.setdefault(src or "?", []).extend(
                    (code, d, v) for (d, v) in miss)
        except Exception:
            errors += 1
    # ★ A2（2026-09-13）：写入期断言——volume>0 且 amount<=0（降级无额源）→
    #   WARN 逐票（限 20 条防刷）+ 当日按源汇总事件，供定位与收盘批量补额。
    if missing_by_src:
        try:
            from . import audit
            for src, mlist in missing_by_src.items():
                for (cd, dt, vol) in mlist[:20]:
                    audit.record(kind="daily", event="kline_amount_missing", level="WARN",
                                 code=cd, date=dt, volume=vol, source=src)
            audit.record(kind="daily", event="amount_missing_by_source", level="WARN",
                         missing_total=sum(len(v) for v in missing_by_src.values()),
                         by_source={s: len(v) for s, v in missing_by_src.items()})
        except Exception:
            pass
    # ★ E4（2026-09-13）：missing_by_src 随返回值上抛，供 run_update 收盘末尾
    #   统一补偿（_compensate_amount）与 source_degradation_daily 汇总。
    return {"updated": len(fetched), "errors": errors, "failed": failed,
            "missing_by_src": {s: [[cd, dt, v] for (cd, dt, v) in ml]
                               for s, ml in missing_by_src.items()}}


def _audit_source_degradation(comp):
    """E4（2026-09-13）：source_degradation_daily 汇总事件（各源命中次数 + 补偿成功率）。
    供 run_update 收盘末尾调用；验收可直接调用同一实现。失败静默。"""
    try:
        from . import audit as _aud_e4
        _sd_stats = {}
        try:
            _sd_path = os.path.join(C.DATA_DIR, "source_degradation.jsonl")
            if os.path.exists(_sd_path):
                _today = time.strftime("%Y-%m-%d")
                for _ln in open(_sd_path, encoding="utf-8"):
                    try:
                        _j = json.loads(_ln)
                        if _j.get("date") == _today:
                            _sd_stats[_j.get("source", "?")] = \
                                _sd_stats.get(_j.get("source", "?"), 0) + 1
                    except Exception:
                        pass
        except Exception:
            pass
        _aud_e4.record(
            kind="daily", event="source_degradation_daily", level="INFO",
            degradation_by_source=_sd_stats,       # 当日降级记录（source_degradation.jsonl）
            missing_by_source=comp.get("by_source") or {},  # 实际写 0 的缺失
            compensate_attempted=comp.get("attempted", 0),
            compensate_ok=comp.get("ok", 0),
            compensate_failed=comp.get("failed", 0),
            remaining_zero=comp.get("remaining_zero", 0),
            amount_ratio=comp.get("ratio"),
            note="降级补偿汇总：各源命中次数与补偿成功率（缺失仅补当日，"
                 "历史日属 D4a 回灌范畴）")
    except Exception:
        pass


def _compensate_amount(missing_by_src, verbose=False):
    """E4（2026-09-13）：降级补偿——收盘更新末尾对当日缺失 amount 的 (code, date)
    从东财 push2his 补额（复用 _G_HOSTS 主机轮换，urllib 零第三方依赖）。
    ★ 只补 amount：读库内现值 OHLCV + df._kline_save 条件覆盖（amount>0 才覆盖），
      不触碰 OHLCV/volume；amount 为真实成交额，不受复权基准差异影响。
    ★ 范围：只补 target 日（当日）缺失——历史日缺失属 D4a 历史回灌范畴（另有方案）。
    返回 {attempted, ok, failed, remaining_zero, total_rows, ratio, by_source}。
    """
    if not missing_by_src:
        return {"attempted": 0, "ok": 0, "failed": 0, "remaining_zero": 0,
                "total_rows": 0, "ratio": None, "by_source": {}}
    target = _target_day()
    # 只保留 target 日的缺失（当日 bar 是降级源新写入的，需补；历史日另有 D4a）
    pairs = {}      # code -> set(date)
    by_source = {}
    for src, mlist in missing_by_src.items():
        by_source[src] = by_source.get(src, 0) + len(mlist)
        for cd, dt, _v in mlist:
            if dt == target:
                pairs.setdefault(cd, set()).add(dt)
    if not pairs:
        return {"attempted": 0, "ok": 0, "failed": 0, "remaining_zero": 0,
                "total_rows": 0, "ratio": None, "by_source": by_source}

    def _fetch_qt_amounts(codes):
        """腾讯 qt.gtimg.cn 批量拉最近交易日成交额（万元×10000→元）。
        返回 {code: amount}；仅收录 date 字段 == target 的票。"""
        out = {}

        def _sym(c):
            return ("sh" if c.startswith(("6", "5", "9")) else "sz") + c

        for i in range(0, len(codes), 60):
            batch = codes[i:i + 60]
            url = "https://qt.gtimg.cn/q=" + ",".join(_sym(c) for c in batch)
            try:
                text = df._http(url, decode="gbk", timeout=15)
                for line in text.strip().split(";"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    code = key.strip().split("_", 1)[-1][2:]   # v_sh600000 → 600000
                    parts = val.strip('"').split("~")
                    if len(parts) > 40 and str(parts[30])[:8] == target.replace("-", ""):
                        try:
                            amt = float(parts[37]) * 10000   # 万元 → 元
                            if amt > 0:
                                out[code] = amt
                        except (ValueError, IndexError):
                            pass
            except Exception:
                continue
        return out

    def _fetch_em_amount(code):
        """东财 push2his 拉 (date -> amount)（备链）。返回 dict 或 None。"""
        secid = ("1." if code.startswith(("6", "5", "9")) else "0.") + code
        path = ("/api/qt/stock/kline/get?secid=" + secid +
                "&klt=101&fqt=1&beg=" + target.replace("-", "") +
                "&end=" + target.replace("-", "") +
                "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
        for a in range(4):
            host = _G_HOSTS[a % len(_G_HOSTS)]
            try:
                js = json.loads(_g_http(host + path,
                                        referer="https://quote.eastmoney.com/"))
                d = js.get("data") or {}
                kl = d.get("klines") or []
                out = {}
                for k in kl:
                    parts = k.split(",")
                    if len(parts) >= 7:
                        try:
                            out[parts[0]] = float(parts[6])
                        except (ValueError, IndexError):
                            pass
                return out or None
            except Exception:
                continue
        return None

    amounts = {}   # code -> {date: amount}
    codes = sorted(pairs.keys())
    # 主链：腾讯 qt 批量（当日/最近交易日）；备链：东财逐票
    qt_amt = {}
    try:
        qt_amt = _fetch_qt_amounts(codes)
    except Exception:
        qt_amt = {}
    em_codes = [c for c in codes if c not in qt_amt]
    if em_codes:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_em_amount, c): c for c in em_codes}
            for f in as_completed(futs):
                c = futs[f]
                try:
                    r = f.result()
                    if r:
                        amounts[c] = r
                except Exception:
                    pass
    # ★ K2（2026-09-16）：东财不可达（RemoteDisconnected 实测）→ baostock 兜底。
    #   归因（2026-09-16 实测 48 票）：东财 push2his 全域名断连、tdx None、
    #   腾讯 fqkline 无额 → 当日缺失 amount 唯一可靠历史源 = baostock（TCP 协议）。
    #   只取 amount 字段（真实成交额，与复权无关；baostock adjustflag=3 不复权）。
    bs_codes = [c for c in em_codes if c not in amounts]
    if bs_codes:
        try:
            import baostock as _bs
            _lg = _bs.login()
            if _lg.error_code == "0":
                try:
                    for c in bs_codes:
                        try:
                            bs_code = ("sh." if c.startswith(("6", "5", "9")) else "sz.") + c
                            rs = _bs.query_history_k_data_plus(
                                bs_code, "date,amount",
                                start_date=target, end_date=target,
                                frequency="d", adjustflag="3")
                            if rs.error_code == "0":
                                while rs.next():
                                    row = rs.get_row_data()
                                    if len(row) >= 2 and row[0] == target:
                                        try:
                                            a = float(row[1])
                                            if a > 0:
                                                amounts[c] = {target: a}
                                        except (ValueError, IndexError):
                                            pass
                                        break
                        except Exception:
                            continue
                finally:
                    try:
                        _bs.logout()
                    except Exception:
                        pass
        except Exception:
            pass
    for c in qt_amt:
        amounts[c] = {target: qt_amt[c]}
    ok = failed = 0
    for code, dates in pairs.items():
        em = amounts.get(code) or {}
        try:
            # 读库内现值（OHLCV 保持降级源值，只补 amount）
            conn = _db.open_ro(C.DB_FILE, 20000)   # ★ J3：统一连接工厂（读复用）
            try:
                row = conn.execute(
                    "SELECT open,high,low,close,volume FROM kline "
                    "WHERE code=? AND period='day' AND date=?", (code, target)).fetchone()
            finally:
                conn.close()
            if not row or target not in em or not (em.get(target) or 0):
                failed += 1
                continue
            rec = {"date": target, "open": row[0], "high": row[1], "low": row[2],
                   "close": row[3], "volume": row[4], "amount": em[target]}
            df._kline_save(code, "day", [rec])
            ok += 1
        except Exception:
            failed += 1
    # 补偿后当日完整率（只读）
    remaining_zero = total_rows = 0
    try:
        conn = _db.open_ro(C.DB_FILE, 20000)   # ★ J3：统一连接工厂（读复用）
        try:
            total_rows = conn.execute(
                "SELECT count(*) FROM kline WHERE period='day' AND date=? "
                "AND volume>0", (target,)).fetchone()[0]
            remaining_zero = conn.execute(
                "SELECT count(*) FROM kline WHERE period='day' AND date=? "
                "AND volume>0 AND (amount IS NULL OR amount=0)",
                (target,)).fetchone()[0]
        finally:
            conn.close()
    except Exception:
        pass
    return {"attempted": len(pairs), "ok": ok, "failed": failed,
            "remaining_zero": remaining_zero, "total_rows": total_rows,
            "ratio": round((total_rows - remaining_zero) / total_rows, 4)
            if total_rows else None,
            "by_source": by_source}


def update_indices(verbose=False, days=600):
    """★ K1（2026-09-16）：指数日K收盘增量更新（P1-①）。
    - 覆盖 datafeed.INDEX_CODES 全部三大指数（sh000001/sz399001/sz399006，
      与实时行情 fetch_indices、健康检查 server._HEALTH_INDEX_CODES 同一清单）；
    - 幂等 upsert：INSERT OR REPLACE，单连接事务，与个股同一 kline 表；
    - 自动回填历史缺口：days 天窗口内的全部缺失交易日（如 09-14/09-15）；
    - 审计 index_daily_updated（n_codes / max_date / 缺口回填条数）；
    - 任一指数拉取失败 → audit ERROR 并计入 failed（禁止 except: pass 静默）。
    返回 {updated, failed, max_date, backfilled, per_code}。"""
    from . import audit
    conn = _db.open_rw(C.DB_FILE)   # ★ J3：统一连接工厂（写连接，WAL init 一次）
    updated = failed = backfilled = 0
    max_date = ""
    per_code = {}
    try:
        # 更新前 MAX(date) 基线（缺口回填条数 = 新写行里 > 旧 MAX 的部分）
        old_max = {}
        for code in df.INDEX_CODES:
            r = conn.execute(
                "SELECT MAX(date) FROM kline WHERE period='day' AND code=?",
                (code,)).fetchone()
            old_max[code] = r[0] or ""
        for code in df.INDEX_CODES:
            try:
                rows = df.fetch_index_kline(code, days=days)
            except Exception as e:
                failed += 1
                try:
                    audit.record(kind="daily", event="index_daily_update_failed",
                                 level="ERROR", code=code, error=str(e)[:200])
                except Exception:
                    pass
                if verbose:
                    print(f"  指数 {code} 拉取异常: {str(e)[:120]}")
                continue
            if not rows:
                failed += 1
                audit.record(kind="daily", event="index_daily_update_failed",
                             level="ERROR", code=code, error="拉取为空")
                if verbose:
                    print(f"  指数 {code} 拉取为空")
                continue
            # ★ K10（2026-09-16）：指数成交额——东财 push2his 合并。
            #   腾讯 fqkline 无额（amount=0）；东财额>0 时随 UPSERT 写入。
            #   独立熔断：东财失败仅 WARN 审计，OHLCV 主路径不受阻；
            #   UPSERT 的 0 保护（CASE WHEN excluded.amount>0）兜底保留库内旧额。
            amt_map = {}
            try:
                dmin = min(r[2] for r in rows).replace("-", "")
                dmax = max(r[2] for r in rows).replace("-", "")
                amt_map = df.fetch_index_amount_em(code, dmin, dmax)
                if not amt_map:
                    # ★ K10：东财 push2his 时段性限流 → 同花顺备源降级
                    amt_map = df.fetch_index_amount_ths(code, days=20)
            except Exception:
                amt_map = {}
            if not amt_map:
                try:
                    audit.record(kind="daily", event="index_amount_fetch_failed",
                                 level="WARN", code=code,
                                 note="东财额获取为空，OHLCV 正常更新（0 保护保留旧额）")
                except Exception:
                    pass
            rows = [r[:8] + (amt_map.get(r[2], 0.0),) for r in rows]
            # ★ 验收修复（2026-09-16）：原 INSERT OR REPLACE 会整行覆盙，
            #   而 fetch_index_kline 的 amount 恒为 0（腾讯 fqkline 无该字段），
            #   days=600 窗口内 A5 已回填的指数成交额被抹零（实测 600 行/指数，
            #   amount>0 覆盖率 100% → 68.8%）。改用 UPSERT：amount 仅在新值>0
            #   时覆盖，否则保留库内旧值；OHLCV 仍正常更新。
            conn.executemany(
                "INSERT INTO kline(code,period,date,open,high,low,close,volume,amount)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(code,period,date) DO UPDATE SET"
                "  open=excluded.open, high=excluded.high, low=excluded.low,"
                "  close=excluded.close, volume=excluded.volume,"
                "  amount=CASE WHEN excluded.amount>0 THEN excluded.amount"
                "              ELSE kline.amount END", rows)
            conn.commit()
            rng = conn.execute(
                "SELECT MIN(date), MAX(date), COUNT(*) FROM kline "
                "WHERE code=? AND period='day'", (code,)).fetchone()
            # 缺口回填条数 = 新写行中 date > 旧 MAX(date) 的行数
            bf = sum(1 for r in rows if r[2] > (old_max.get(code) or ""))
            backfilled += bf
            updated += 1
            if rng[1] and (not max_date or rng[1] > max_date):
                max_date = rng[1]
            per_code[code] = {"range": f"{rng[0]}~{rng[1]}", "rows": rng[2],
                              "backfilled": bf}
            if verbose:
                print(f"  指数 {code}: 写 {len(rows)} 根（库内 {rng[2]} 行，"
                      f"回填 {bf} 根，最新 {rng[1]}）")
    finally:
        conn.close()
    audit.record(kind="daily", event="index_daily_updated", level="INFO",
                 n_codes=updated, max_date=max_date, backfilled=backfilled,
                 failed=failed, per_code={c: v["rows"] for c, v in per_code.items()})
    if failed:
        audit.record(kind="daily", event="index_daily_update_failed",
                     level="ERROR", n_failed=failed,
                     note="部分指数未更新（详见逐票 ERROR）")
    return {"updated": updated, "failed": failed, "max_date": max_date,
            "backfilled": backfilled, "per_code": per_code}


def _daily_max_dates():
    """每票 kline(period='day') 的 max(date)。返回 {code: max_date|''}（只读）。"""
    conn = _db.open_ro(C.DB_FILE, 20000)   # ★ J3：统一连接工厂（读复用）
    try:
        rows = conn.execute(
            "SELECT code, MAX(date) FROM kline WHERE period='day' GROUP BY code").fetchall()
        return {r[0]: (r[1] or "") for r in rows}
    finally:
        conn.close()


def _daily_max_dates_for(codes):
    """指定票的 max(date)（回补分批追平判定用）。"""
    out = {}
    conn = _db.open_ro(C.DB_FILE, 20000)   # ★ J3：统一连接工厂（读复用）
    try:
        for c in codes:
            row = conn.execute(
                "SELECT MAX(date) FROM kline WHERE code=? AND period='day'", (c,)).fetchone()
            out[c] = row[0] or ""
    finally:
        conn.close()
    return out


# ★ K3（2026-09-16）：覆盖率分母剔除 —— 退市清单缓存（delisted_universe.json 5549 只
#   含在市票，仅 status==0 / out 非空 为已退市）。mtime 变更自动重读。
_DELISTED_CACHE = {"mtime": 0, "codes": frozenset()}


def _delisted_codes(p=None):
    """已退市 code 集合（只读缓存）。p 可注入临时清单（验收单测用）。"""
    path = p or os.path.join(C.DATA_DIR, "delisted_universe.json")
    try:
        mt = os.path.getmtime(path)
        if p is None and _DELISTED_CACHE["mtime"] == mt:
            return _DELISTED_CACHE["codes"]
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        codes = frozenset(s["code"] for s in d.get("stocks", [])
                          if s.get("status") == 0 or s.get("out"))
        if p is None:
            _DELISTED_CACHE["mtime"] = mt
            _DELISTED_CACHE["codes"] = codes
        return codes
    except Exception:
        return _DELISTED_CACHE["codes"]


def _daily_traded_codes(day):
    """目标日当日有成交的 code 集合（date=? AND volume>0；volume=0 停牌占位不算）。只读。"""
    conn = _db.open_ro(C.DB_FILE, 30000)
    try:
        rows = conn.execute(
            "SELECT DISTINCT code FROM kline WHERE period='day' AND date=? "
            "AND volume>0", (day,)).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _coverage_exclusions(md, min_dates, day, delisted_file=None):
    """★K3（2026-09-16）：覆盖率分母剔除明细（分类计数 + 样例，互斥：每票只进一类）。
      suspended  停牌：目标日无成交（当日无行或 volume=0 占位），且非退市、非新股；
      delisted   退市：data/delisted_universe.json 中 status==0（out 非空）；
      new_listed 新股：kline 首行 date > 目标日（上市日晚于目标日，当日无行情属正常）。
    md={code:max_date}；min_dates={code:min_date}（kline 首日，扩表票首日=回填起点，
    早于目标日不会误判新股）。返回 {"suspended":[...], "delisted":[...], "new_listed":[...]}。"""
    delisted_set = _delisted_codes(delisted_file)
    traded = _daily_traded_codes(day)
    ex = {"suspended": [], "delisted": [], "new_listed": []}
    for c in md:
        if c in delisted_set:
            ex["delisted"].append(c)
        elif min_dates.get(c, "") and min_dates[c] > day:
            ex["new_listed"].append(c)
        elif c not in traded:
            ex["suspended"].append(c)
    return ex


def _daily_coverage(day=None):
    """★K3（2026-09-16）：全库日K新鲜度——分母改为"当日应有行情的票"（剔除停牌/退市/
    新股），ratio 可解释；剔除明细（分类计数+样例）随返回结构暴露，非空时记 audit
    coverage_universe_excluded（INFO）。键 fresh/total/ratio/missing/missing_sample
    语义保留（daily_backfill 等调用方兼容）。
    fresh=max(date)>=目标日 且属分母的票数；missing=分母内 max<目标日（真缺口）。"""
    try:
        md = _daily_max_dates()
    except Exception:
        return None
    day = day or _target_day()
    try:
        conn = _db.open_ro(C.DB_FILE, 30000)
        try:
            rows = conn.execute(
                "SELECT code, MIN(date) FROM kline WHERE period='day' GROUP BY code").fetchall()
            min_dates = {r[0]: (r[1] or "") for r in rows}
        finally:
            conn.close()
    except Exception:
        min_dates = {}
    ex = _coverage_exclusions(md, min_dates, day)
    suspended, delisted, new_listed = (set(ex["suspended"]),
                                       set(ex["delisted"]), set(ex["new_listed"]))
    missing = [c for c in md
               if c not in suspended and c not in delisted and c not in new_listed
               and not (md.get(c, "") and md[c] >= day)]
    total = len(md) - len(suspended) - len(delisted) - len(new_listed)
    fresh = total - len(missing)
    excluded_total = len(suspended) + len(delisted) + len(new_listed)
    if excluded_total > 0:
        try:
            from . import audit as _aud
            _aud.record(kind="daily", event="coverage_universe_excluded", level="INFO",
                        target_day=day, total_all=len(md), excluded_total=excluded_total,
                        suspended=len(suspended), delisted=len(delisted),
                        new_listed=len(new_listed),
                        sample_codes={"suspended": list(suspended)[:5],
                                      "delisted": list(delisted)[:5],
                                      "new_listed": list(new_listed)[:5]},
                        note="覆盖率分母剔除明细（停牌/退市/新股），ratio 按"
                             "“当日应有行情的票”口径计算")
        except Exception:
            pass
    return {"fresh": fresh, "total": total,
            "ratio": round(fresh / total, 4) if total else 0.0,
            "missing": len(missing),
            "missing_sample": missing[:10],
            "total_all": len(md),
            "excluded": {"suspended": len(suspended), "delisted": len(delisted),
                         "new_listed": len(new_listed), "total": excluded_total},
            "excluded_sample": {"suspended": list(suspended)[:5],
                                "delisted": list(delisted)[:5],
                                "new_listed": list(new_listed)[:5]}}


# ★ F3（2026-09-08）：amount=0 缺失哨兵 —— 每日收盘更新完成后统计当日
#   amount=0 且 volume>0 的行数（扩表票缺额源特征），>0 写 quality_alert.jsonl
#   （行数/占比/样例代码）。历史教训：05-18 扩表 3098 只 amount=0 全链路无感知，
#   靠人工 audit 才查出，此哨兵让其每次收盘后自曝。只读查询，失败静默。
def _amount_zero_guard(day=None, db_file=None, qa_file=None):
    """D1（2026-09-13）：参数 db_file/qa_file 支持验收注入临时副本库/临时告警文件，
    默认走生产路径。写 quality_alert.jsonl 后同步转发 audit（quality_alert_forward，
    连续 ≥2 交易日自动升级 WARN→CRITICAL）并触发轮转。只读查询，失败静默。"""
    try:
        day = day or _target_day()
        conn = _db.open_ro(db_file or C.DB_FILE, 30000)   # ★ J3：统一连接工厂（读复用）
        try:
            zero = conn.execute(
                "SELECT count(*) FROM kline WHERE period='day' AND date=? "
                "AND (amount IS NULL OR amount=0) AND volume>0", (day,)).fetchone()[0]
            total = conn.execute(
                "SELECT count(*) FROM kline WHERE period='day' AND date=? "
                "AND volume>0", (day,)).fetchone()[0]
            samples = [r[0] for r in conn.execute(
                "SELECT code FROM kline WHERE period='day' AND date=? "
                "AND (amount IS NULL OR amount=0) AND volume>0 LIMIT 5", (day,))]
        finally:
            conn.close()
        if zero > 0:
            alert = {
                "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "event": "amount_zero_guard",
                "level": "WARN",
                "target_day": day,
                "amount_zero_rows": zero,
                "ratio": round(zero / total, 4) if total else 0.0,
                "sample_codes": samples,
                "note": "当日存在 amount=0 且 volume>0 的行（缺成交额源），检查扩表票回填",
            }
            _qa = qa_file or os.path.join(C.DATA_DIR, "quality_alert.jsonl")
            with open(_qa, "a", encoding="utf-8", newline="\n") as af:
                af.write(json.dumps(alert, ensure_ascii=False) + "\n")
            try:
                from . import audit as _audit
                _audit.quality_alert_forward(alert,
                                             source="updater._amount_zero_guard")
                _audit.quality_alert_rotate()
            except Exception:
                pass
            return alert
        return None
    except Exception:
        return None


def _stale_first_calib():
    """★A1（2026-09-13）预算自校准：最近 5 次实测每批耗时（中位数，秒）。
    无自校准数据返回 None（调用方回退 config.STALE_FIRST_BATCH_EST_SEC）。"""
    p = os.path.join(C.DATA_DIR, "stale_first_calib.json")
    try:
        with open(p, encoding="utf-8") as f:
            arr = json.load(f)
        vals = [float(x["per_batch_s"]) for x in arr
                if isinstance(x, dict) and x.get("per_batch_s")]
        if len(vals) >= 2:
            vals.sort()
            return vals[len(vals) // 2]
    except Exception:
        pass
    return None


def _stale_first_record_calib(per_batch_s, batch, n_batch):
    """★A1（2026-09-13）预算自校准落盘（滚动保留最近 5 条）。失败静默。"""
    p = os.path.join(C.DATA_DIR, "stale_first_calib.json")
    try:
        arr = []
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                arr = json.load(f)
        arr.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "per_batch_s": round(per_batch_s, 1), "batch": batch,
                    "n_batch": n_batch})
        arr = arr[-5:]
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(arr, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _stale_first_save_skip_codes(fail_log):
    """★A1（2026-09-13）当日熔断名单落盘（data/daily_skip_codes.json，累积）。
    fail_log: {code: reason}。失败静默。"""
    if not fail_log:
        return
    try:
        sp = os.path.join(C.DATA_DIR, "daily_skip_codes.json")
        prev = {}
        if os.path.isfile(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    prev = json.load(f)
            except Exception:
                prev = {}
        today = time.strftime("%Y-%m-%d")
        prev.update({c: {"reason": rsn, "date": today}
                     for c, rsn in fail_log.items()})
        with open(sp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(prev, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _update_daily_stale_first(all_codes, days, verbose, budget_sec, batch):
    """E1（2026-09-03）：全库按陈旧度优先。先查每票 max(date)，仅陈旧票
    （max<target 或库内无记录）进入队列，最陈旧先更新，单批 ≤batch 只，循环直到
    追平或预算 budget_sec 用尽。未追平的（失败/停牌/退市）放回队尾预算内重试。
    ★ H3（2026-09-06）：盘中交易时段内 skip_today=True，不写当日残缺 bar；
    返回值新增 intraday_skipped 字段（被跳过当日 bar 的票数，= 实际更新票数）。
    ★ F4（2026-09-08）：
      - 预算动态化：budget_sec=None 时按待处理票数估算（每批 batch 只估
        C.STALE_FIRST_BATCH_EST_SEC，夹在 [STALE_FIRST_BUDGET_MIN, MAX]）。
        旧固定 480s 按旧票池（2838 只）定，扩池（5328 只）后必然超时硬截断。
      - "无进展才中止"：某轮零更新且零追平（连续 2 轮）才提前停；否则跑满预算。
      - 超预算仍有剩余 → 必须记 WARN 事件（剩余票数/已用时长），不再静默截断。
      返回值新增 pending_remain / budget_sec / no_progress_stop。"""
    target = _target_day()
    t0 = time.time()
    md = _daily_max_dates()
    pending = [c for c in all_codes if md.get(c, "") < target]
    # 陈旧度排序：①已在库的陈旧票优先（KPI 关键），②未入库的列表新增票次之；
    # 同一优先级内 max(date) 旧者在前，再按码序稳定。
    pending.sort(key=lambda c: (c not in md, md.get(c, ""), c))
    # ★ F4：预算动态化（按待处理票数估算）
    # ★ A1（2026-09-13）：估算值优先用自校准中位数（data/stale_first_calib.json
    #   最近 5 次实测每批耗时），无数据才回退 config 兜底值 90s。
    if budget_sec is None:
        n_batch = max(1, int(math.ceil(len(pending) / max(1, batch))))
        calib = _stale_first_calib()
        est = calib if calib else C.STALE_FIRST_BATCH_EST_SEC
        budget_sec = int(max(C.STALE_FIRST_BUDGET_MIN,
                             min(C.STALE_FIRST_BUDGET_MAX,
                                 n_batch * est)))
    # ★ A1（2026-09-13）：最迟完成时刻硬停（默认 22:00）——预算放大后防跨天
    deadline_ts = None
    try:
        import datetime as _dt
        _now = time.localtime()
        _dl = _dt.datetime(_now.tm_year, _now.tm_mon, _now.tm_mday,
                           C.STALE_FIRST_DEADLINE_HHMM // 100,
                           C.STALE_FIRST_DEADLINE_HHMM % 100, 0).timestamp()
        if time.time() < _dl:
            deadline_ts = _dl
    except Exception:
        deadline_ts = None
    updated = errors = covered = 0
    n_done = 0
    intraday_skipped = 0
    no_progress = 0
    skip_today = _in_intraday_window()
    _agg = {}   # ★ E4：各批 amount 缺失聚合（source -> [[code,date,vol],...]）
    # ★ A1（2026-09-13）：失败票熔断计数 + 当日熔断名单
    skip_codes = {}     # code -> 连续失败次数
    fail_log = {}       # code -> 失败原因（落盘 daily_skip_codes.json）
    batch_count = 0
    if verbose and skip_today:
        print("  [stale-first 守卫] 盘中交易时段，skip_today=True（不写当日残缺 bar）")
    while pending and (time.time() - t0) < budget_sec:
        if deadline_ts and time.time() >= deadline_ts:
            if verbose:
                print(f"  stale-first 达 22:00 硬停，剩余待处理{len(pending)}")
            break
        # ★ A1：本批剔除已熔断票（连续失败 >= STALE_FIRST_FAIL_SKIP_AFTER）
        #   注意：consumed 按 pending 顺序推进，熔断票同样移出队列（防错位卡死）
        b_codes = []
        _consumed = 0
        for _c in pending:
            if _consumed >= batch:
                break
            _consumed += 1
            if skip_codes.get(_c, 0) < C.STALE_FIRST_FAIL_SKIP_AFTER:
                b_codes.append(_c)
        if not b_codes:
            if verbose:
                print("  stale-first 剩余票全部已熔断，提前中止")
            break
        pending = pending[_consumed:]
        r = _fetch_and_save(b_codes, days, skip_today=skip_today)
        batch_count += 1
        updated += r["updated"]
        errors += r["errors"]
        # ★ E4（2026-09-13）：聚合各批 amount 缺失，随返回值上抛供收盘补偿
        for _src, _ml in (r.get("missing_by_src") or {}).items():
            _agg.setdefault(_src, []).extend(_ml)
        if skip_today:
            intraday_skipped += r["updated"]
        fr = _daily_max_dates_for(b_codes)
        still = [c for c in b_codes if fr.get(c, "") < target]
        covered += len(b_codes) - len(still)
        # ★ A1：失败票区分处理——拉取失败（计数重试至熔断线）vs
        #   数据源确认无 target 日（停牌/未上市，当日直接跳过不重试）
        still_replay = []
        failed_set = set(r.get("failed") or ())
        for c in still:
            if c in failed_set:
                skip_codes[c] = skip_codes.get(c, 0) + 1
                fail_log[c] = "fetch_failed"
                if skip_codes[c] < C.STALE_FIRST_FAIL_SKIP_AFTER:
                    still_replay.append(c)
            else:
                skip_codes[c] = skip_codes.get(c, 0) + 100   # 一次性熔断
                fail_log[c] = "no_target_row(停牌/未上市/新股)"
        if still_replay:
            pending += still_replay
        n_done += len(b_codes)
        # ★ F4：无进展才中止——本轮零更新且零追平（连续 2 轮）说明链路卡死/全失败
        if r["updated"] == 0 and len(b_codes) - len(still) == 0:
            no_progress += 1
            if no_progress >= 2:
                if verbose:
                    print("  stale-first 连续 2 轮零进展，提前中止（防死循环）")
                break
        else:
            no_progress = 0
        if verbose:
            print(f"  stale-first 批: 更新{r['updated']} 追平{len(b_codes)-len(still)}"
                  f" 待追平{len(still)} 剩余待处理{len(pending)}"
                  f" 已用{int(time.time()-t0)}s/{budget_sec}s")
    # ★ A1：预算自校准落盘（实际每批耗时）
    if batch_count > 0:
        _stale_first_record_calib((time.time() - t0) / batch_count,
                                  batch, batch_count)
    # ★ A1：当日熔断名单落盘留痕
    if fail_log:
        _stale_first_save_skip_codes(fail_log)
    # ★ F4：超预算仍有剩余 → 必须 WARN 事件（含剩余票数、已用时长），不再静默截断
    if pending:
        try:
            from . import audit
            audit.record(kind="daily", event="stale_first_budget_exceeded",
                         level="WARN",
                         pending_remain=len(pending),
                         elapsed_s=round(time.time() - t0, 1),
                         budget_sec=budget_sec,
                         skip_codes=len(fail_log),
                         note="stale-first 预算耗尽仍有待追平票，收盘更新未完成，"
                              "KPI/快照延后，兜底任务将按覆盖率判据接力")
        except Exception:
            pass
    return {"updated": updated, "errors": errors, "covered": covered,
            "target": target, "elapsed_s": round(time.time() - t0, 1),
            "budget_exhausted": bool(pending),
            "pending_remain": len(pending), "budget_sec": budget_sec,
            "no_progress_stop": no_progress >= 2,
            "n_codes": n_done,
            "intraday_skipped": intraday_skipped,
            "skip_codes": len(fail_log),
            "missing_by_src": _agg}


def update_daily(codes=None, limit=300, days=60, verbose=False,
                 stale_first=False, budget_sec=None, batch=250):
    """日K增量更新：补拉最近 days 天日K（幂等 upsert）。
    返回 {updated: 只数, errors}；stale_first 时另含 covered/target/elapsed_s。
    ★ E1（2026-09-03）：stale_first=True → 全库按陈旧度优先（最旧先更新，单批≤batch，
    循环至追平或预算耗尽）。旧签名 update_daily(limit=300) 行为完全不变。
    ★ F4（2026-09-08）：budget_sec 默认 None → 动态预算（按待处理票数估算，
    见 _update_daily_stale_first；旧固定 480s 已按票池扩量废弃）。"""
    if codes is None:
        lst = df.get_stock_list()
        all_codes = [c for c, _, _ in lst]
    else:
        all_codes = list(codes)
    if stale_first:
        return _update_daily_stale_first(all_codes, days, verbose, budget_sec, batch)
    codes = all_codes[:limit] if codes is None else all_codes
    if not codes:
        return {"updated": 0, "errors": 0}
    return _fetch_and_save(codes, days)


# ==================== ★ Phase63：全球源加固（全球段，本段独占） ====================
# 目标：CL/VIX/GC/DINIW 等全球序列的历史回补 + 每日增量 + 主备切换降级。
# 数据源（均经 Phase35/39/44 实测验证）：
#   VIX   主：CBOE 官方历史 JSON（1990 起全史，稳定）
#   CL    主：东财 push2his 102.CL00Y（间歇性掐连接 → 主机轮换重试）
#         备：新浪美股日线 USO（WTI 期货 ETF 代理，含展期损耗，降级口径）
#   DINIW 主：新浪环球外汇 fx_sdiniw（2000 起全史）；备：东财 100.UDI
# 写入：global_kline(market,sym,date,close)，主键(sym,date) INSERT OR REPLACE，
#       只写 <= 今天；每日增量 = 只追加大于库内最大日期的行。
_G_HOSTS = ["https://push2his.eastmoney.com", "https://45.push2his.eastmoney.com",
            "https://21.push2his.eastmoney.com"]


def _g_http(url, referer=None, timeout=20):
    h = {"User-Agent": "Mozilla/5.0"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _g_cboe_vix():
    """CBOE 官方 VIX 历史。返回 [(date, close)] 升序"""
    import datetime as _dt
    js = json.loads(_g_http(
        "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json",
        referer="https://www.cboe.com/"))
    out = []
    for item in (js.get("data") or []):
        if isinstance(item, dict) and item.get("close") is not None:
            d = str(item.get("date", ""))[:10]
            try:
                out.append((_dt.datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d"),
                            float(item["close"])))
            except (ValueError, TypeError):
                continue
    return sorted(out)


def _g_em_close(secid):
    """东财日线收盘（主机轮换 + 退避重试）。返回 [(date, close)] 升序"""
    import datetime as _dt
    path = ("/api/qt/stock/kline/get?secid=" + secid +
            "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f53&klt=101&fqt=0"
            "&beg=19990101&end=20500101")
    last = None
    for a in range(6):
        host = _G_HOSTS[a % len(_G_HOSTS)]
        try:
            js = json.loads(_g_http(host + path, referer="https://quote.eastmoney.com/"))
            d = js.get("data") or {}
            kl = d.get("klines") or []
            if kl:
                out = sorted((s.split(",")[0], float(s.split(",")[1])) for s in kl)
                return out
            last = RuntimeError("empty klines")
        except Exception as e:
            last = e
        time.sleep(1.5 + a * 2)
    raise RuntimeError("eastmoney %s 失败: %s" % (secid, last))


def _g_sina_pipe(sym_url_tail):
    """新浪 jsonp 行串解析（var _=("行1|行2|...")，行=date,o,l,h,c,）。"""
    text = _g_http(sym_url_tail, referer="https://finance.sina.com.cn")
    start = text.find("(")
    if start < 0:
        raise RuntimeError("sina 非 jsonp")
    raw = json.loads(text[start + 1: text.rfind(")")])
    if not isinstance(raw, str):
        raise RuntimeError("sina 返回非字符串")
    out = []
    for ln in raw.split("|"):
        p = ln.strip().strip(",").split(",")
        if len(p) >= 5 and p[0][:2] == "20":
            try:
                o, lo, hi, c = map(float, p[1:5])
            except ValueError:
                continue
            if lo <= min(o, c) and hi >= max(o, c):
                out.append((p[0], c))
    return sorted(out)


def _g_sina_futures(symbol):
    """★A4 新浪全球期货日线全史（GlobalFuturesService，dict数组）。返回 [(date, close)] 升序"""
    txt = _g_http(
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
        "var%20_=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=" + symbol,
        referer="https://finance.sina.com.cn")
    s = txt.find("(")
    if s < 0:
        raise RuntimeError("sina 非 jsonp")
    arr = json.loads(txt[s + 1: txt.rfind(")")])
    out = sorted((str(x["date"])[:10], float(x["close"])) for x in arr
                 if x.get("date") and x.get("close"))
    return out


def _g_sina_us_daily(symbol):
    """★A4 新浪美日线 getDailyK（dict 数组 {d,o,h,l,c,v}）。
    Phase63 的 _g_sina_pipe 对该端点返回"非字符串"——本解析器为正确口径，
    GLD/USO 代理与美股指数 .DJI 均走此通道。返回 [(date, close)] 升序"""
    txt = _g_http(
        "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
        "var%20_=/US_MinKService.getDailyK?symbol=" + symbol,
        referer="https://finance.sina.com.cn")
    s = txt.find("(")
    if s < 0:
        raise RuntimeError("sina 非 jsonp")
    raw = json.loads(txt[s + 1: txt.rfind(")")])
    if not isinstance(raw, list):
        raise RuntimeError("sina 返回非数组")
    out = []
    for x in raw:
        d = str(x.get("d") or x.get("date") or "")[:10]
        c = x.get("c") or x.get("close")
        if d[:2] == "20" and c:
            try:
                out.append((d, float(c)))
            except (TypeError, ValueError):
                continue
    return sorted(out)


def _global_source_chains():
    """每序列的主备源链：依次尝试，首个产出 >=30 行的成功。返回 [(sym, market, 链)]"""
    def _diniw_primary():
        return _g_sina_pipe(
            "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/"
            "var%20_=/NewForexService.getDayKLine?symbol=fx_sdiniw")

    def _cl_primary():
        return _g_em_close("102.CL00Y")

    def _cl_backup():
        # 降级代理：USO（WTI 期货 ETF），日收益与 WTI 高相关但含展期损耗
        return _g_sina_us_daily("USO")

    def _gc_primary():
        # ★A4：东财 102.GC00Y 间歇掐连接导致 GC 长期薄（仅实时占位4行）——
        # 主源改新浪全球期货 GC 全史（2016 起 2588 根，实测稳定）
        return _g_sina_futures("GC")

    def _gc_backup():
        # 降级代理：GLD（黄金 ETF），与 COMEX 金价高相关
        return _g_sina_us_daily("GLD")

    def _usdcnh_primary():
        # ★A4 新浪环球外汇离岸人民币全史（2014-11 起 3077 根）
        return _g_sina_pipe(
            "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/"
            "var%20_=/NewForexService.getDayKLine?symbol=fx_susdcnh")

    def _djia_primary():
        # ★A4 新浪美股指数道指日线（2004 起 5699 根）
        return _g_sina_us_daily(".DJI")

    chains = [
        ("VIX", "us", [(_g_cboe_vix, "cboe官方")]),
        ("CL", "fx", [(_cl_primary, "eastmoney CL00Y"), (_cl_backup, "sina USO代理(降级)")]),
        ("GC", "fx", [(_gc_primary, "sina全球期货GC全史"),
                      (lambda: _g_em_close("102.GC00Y"), "eastmoney GC00Y"),
                      (_gc_backup, "sina GLD代理(降级)")]),
        ("DINIW", "fx", [(_diniw_primary, "sina fx_sdiniw"),
                         (lambda: _g_em_close("100.UDI"), "eastmoney UDI")]),
        ("USDCNH", "fx", [(_usdcnh_primary, "sina fx_susdcnh"),
                          (lambda: _g_em_close("133.USDCNH"), "eastmoney USDCNH")]),
        ("DJIA", "us", [(_djia_primary, "sina .DJI"),
                        (lambda: _g_em_close("100.DJIA"), "eastmoney DJIA")]),
    ]
    return chains


def update_global(verbose=False):
    """★ Phase63：全球序列历史回补 + 每日增量（主备切换降级）。
    返回 {sym: {"source": 用了哪个源, "rows_written": n, "total": 库内总数}}"""
    today = df._today_str()
    conn = _db.open_rw(C.DB_FILE)   # ★ J3：统一连接工厂（写连接，WAL init 一次）
    results = {}
    try:
        for sym, market, chain in _global_source_chains():
            row = conn.execute("SELECT MAX(date), COUNT(*) FROM global_kline "
                               "WHERE sym=?", (sym,)).fetchone()
            max_d, total0 = (row[0] or ""), (row[1] or 0)
            got = None
            used_src = ""
            for fn, label in chain:
                for attempt in range(2):
                    try:
                        rows = fn()
                        if rows and len(rows) >= 30:
                            got, used_src = rows, label
                            break
                        last = "rows<30"
                    except Exception as e:
                        last = str(e)[:60]
                    time.sleep(1 + attempt)
                if got:
                    break
            if not got:
                results[sym] = {"source": None, "error": str(last),
                                "total": total0}
                if verbose:
                    print("  [global] %s 全链失败: %s" % (sym, last), flush=True)
                continue
            rows = [(d, c) for d, c in sorted(set(got)) if d <= today]
            if total0 >= 30:   # 库内已有积累 → 只追加增量行
                rows = [r for r in rows if r[0] > max_d]
            if rows:
                conn.execute("BEGIN")
                conn.executemany(
                    "INSERT OR REPLACE INTO global_kline(market,sym,date,close) "
                    "VALUES(?,?,?,?)", [(market, sym, d, c) for d, c in rows])
                conn.commit()
            row2 = conn.execute("SELECT COUNT(*) FROM global_kline WHERE sym=?",
                                (sym,)).fetchone()[0]
            results[sym] = {"source": used_src, "rows_written": len(rows),
                            "total": row2}
            if verbose:
                print("  [global] %-6s src=%-22s +%d 行（库内 %d）" % (
                    sym, used_src, len(rows), row2), flush=True)
    finally:
        conn.close()
    return results


# ==================== 调度 ====================
_UPDATE_STATE = {"last_day": "", "running": False}
_STATE_FILE = None   # 延迟初始化


def _state_path():
    global _STATE_FILE
    if _STATE_FILE is None:
        import os
        _STATE_FILE = os.path.join(C.DATA_DIR, "update_state.json")
    return _STATE_FILE


def _load_state():
    import os
    try:
        if os.path.exists(_state_path()):
            with open(_state_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            _UPDATE_STATE["last_day"] = d.get("last_day", "")
    except Exception:
        pass


def _save_state():
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump({"last_day": _UPDATE_STATE["last_day"]}, f)
    except Exception:
        pass


# ★ F1（2026-09-02 D 块落地）：收盘更新"已触发"标记 —— 文件持久化，跨 _loop 实例/进程共享。
#   原 trader.py L309 `_last_data_upd` 为 _loop 局部变量，多实例各触发一次 → 双跑源头之一。
_CLOSE_TRIGGER_FILE = None


def _close_trigger_path():
    global _CLOSE_TRIGGER_FILE
    if _CLOSE_TRIGGER_FILE is None:
        _CLOSE_TRIGGER_FILE = os.path.join(C.DATA_DIR, "close_update_state.json")
    return _CLOSE_TRIGGER_FILE


def close_update_triggered(today):
    """当日收盘更新是否已触发（读持久化标记）。"""
    try:
        if os.path.exists(_close_trigger_path()):
            with open(_close_trigger_path(), "r", encoding="utf-8") as f:
                return json.load(f).get("triggered_day") == today
    except Exception:
        pass
    return False


def mark_close_update_triggered(today):
    """标记当日收盘更新已触发（原子写：临时文件 + rename）。"""
    _tmp = _close_trigger_path() + ".tmp"
    try:
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump({"triggered_day": today,
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
        os.replace(_tmp, _close_trigger_path())
    except Exception:
        try:
            if os.path.exists(_tmp):
                os.remove(_tmp)
        except Exception:
            pass


def _target_day():
    """★ 修复：更新应覆盖的目标交易日——交易日15:10后（已收盘）=今天，
    否则=最近一个已收盘的交易日。原实现固定 prev_trading_day，
    导致当日收盘后跑更新也只补到昨天，当天K线永远滞后。"""
    today = df._today_str()
    try:
        from . import trading_calendar as tcal
        hm = int(time.strftime("%H")) * 100 + int(time.strftime("%M"))
        if tcal.is_trading_day(today) and hm >= 1510:
            return today
        return tcal.prev_trading_day(today) or today
    except Exception:
        return today


def needs_update():
    """是否需要增量更新：上次更新日 ≠ 目标交易日（_target_day）。
    ★ F4（2026-09-08）：兜底任务判据不再只看此标记（改看实际覆盖率，
    见 close_fallback_needed）——仅此函数读 _UPDATE_STATE。"""
    if not _UPDATE_STATE["last_day"]:
        _load_state()
    return _UPDATE_STATE["last_day"] != _target_day()


# ★ A3（2026-09-13）：兜底接力窗口与目标 —— 兜底从"15:45 一次性"升级为
#   "15:45~22:00 覆盖率驱动接力"（计划任务 TianjiCloseCatchup 每 30 分钟触发）。
CLOSE_CATCHUP_START = 1545      # 接力窗口起点 15:45
CLOSE_CATCHUP_END = 2200        # 接力窗口终点 22:00（此后不再执行更新，记收尾 WARN）
CLOSE_CATCHUP_TARGET = 0.95     # 接力达标线：当日覆盖率 ≥0.95 且当日已有快照 → no-op


def close_fallback_needed(thr=None, require_snapshot=False):
    """★ F4（2026-09-08）：收盘兜底任务（TianjiCloseUpdate 15:45）重跑判据。
    ★ A3（2026-09-13）：thr 可覆盖达标阈值（接力目标 CLOSE_CATCHUP_TARGET=0.95）；
    require_snapshot=True 时"当日无快照"也判 needed（覆盖率达标但快照缺失仍需接力落盘）。
    返回 (needed, ratio, pending, threshold)。
    needed = 当日覆盖率 < 阈值 或 存在未追平票（库内 max<target）或 当日无快照。
    不再看 triggered_day / needs_update 标记——修复"主流程 15:10 触发后预算耗尽
    未跑完，15:45 兜底见标记已触发就跳过"的时序缺口（2026-09-08 实证）。
    全程只读：_daily_coverage / _daily_max_dates / snapshot_path 均为只读。"""
    try:
        cov = _daily_coverage()
        ratio = cov["ratio"] if cov else 0.0
        # ★ K3（2026-09-16）：pending 与覆盖率同口径——只计"当日应有行情但缺失"的票
        #   （停牌/退市/新股已从分母剔除），不再把永远追不平的停牌票计入未追平。
        pending = cov["missing"] if cov else 0
    except Exception:
        cov, ratio, pending = None, 0.0, 0
    thr = float(thr if thr is not None else getattr(C, "CLOSE_FALLBACK_COVERAGE", 0.9))
    need_ratio = (not cov) or ratio < thr or pending > 0
    need_snap = False
    if require_snapshot:
        try:
            from . import data_snapshot as _dsnap
            need_snap = not os.path.isfile(_dsnap.snapshot_path())
        except Exception:
            need_snap = True
    return (need_ratio or need_snap), ratio, pending, thr


# ★ F2（2026-09-02 D 块落地）：跨进程防重入 OS 文件锁。
#   msvcrt(Windows)/fcntl(Linux) 非阻塞排它锁，锁与文件句柄绑定、进程死亡时 OS 自动释放，
#   无 stale 锁残留。Windows 字节锁按进程域（同进程多句柄可重复加锁），故同进程互斥由
#   _UPDATE_STATE["running"] + threading.Lock 承担；本锁负责跨进程互斥。
_UPDATE_THREAD_LOCK = threading.Lock()


def _acquire_update_lock():
    """成功返回已持锁文件对象；失败（其他进程已持锁/IO 异常）返回 None。"""
    path = os.path.join(C.DATA_DIR, "update.lock")
    try:
        f = open(path, "a+", encoding="utf-8")
    except Exception:
        return None
    try:
        f.seek(0)
        if not f.read(1):
            f.write("\0")
            f.flush()
        f.seek(0)
        try:
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        return None


def _release_update_lock(f):
    try:
        f.seek(0)
        try:
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
    except Exception:
        pass


def run_update(verbose=False):
    """执行一次完整增量更新（min5 + 日K）。后台调用，防重入。
    ★ P0-2：min5 更新"基本成功"（updated >= max(1, total*0.5)）才标记 last_day；
    否则保留旧 last_day 让下次启动/收盘后重试，并记 WARN。
    ★ F2（2026-09-02 D 块）：新增跨进程 OS 文件锁 + 同进程线程锁原子化检查-设置，
    双进程/双引擎时只有一方能完整执行，另一方直接 skipped（根治双跑）。
    """
    with _UPDATE_THREAD_LOCK:
        if _UPDATE_STATE["running"]:
            return {"skipped": True}
        _lk = _acquire_update_lock()
        if _lk is None:
            return {"skipped": True}   # 其他进程正在更新
        _UPDATE_STATE["running"] = True
    try:
        r1 = update_min5(verbose=verbose)   # ★ P62：动态覆盖清单（≥800 只）
        # ★ E1（2026-09-03）：日K全库 stale-first（修 P0-1 前300只病）——全库按陈旧度
        #   优先，最旧先更新，循环至追平或 8 分钟预算用尽；旧签名仍保留兼容。
        r2 = update_daily(verbose=verbose, stale_first=True)
        # ★ A3（2026-09-13）：主更新结束后若仍有待追平票 → 同一进程内直接续跑
        #   一轮（不受 15:45 兜底时刻约束），把"延后"消解在主流程内。
        #   run_update 全程持 update.lock，续跑无并发写风险；预算按剩余票动态。
        if (r2 or {}).get("pending_remain", 0) > 0:
            if verbose:
                print(f"  主更新剩余 {r2.get('pending_remain')} 票未追平 → 同进程续跑一轮")
            try:
                _r2x = update_daily(verbose=verbose, stale_first=True)
                r2 = {**r2,
                      "updated": get(r2, "updated") + get(_r2x, "updated"),
                      "covered": get(r2, "covered") + get(_r2x, "covered"),
                      "pending_remain": get(_r2x, "pending_remain"),
                      "budget_exhausted": get(_r2x, "budget_exhausted"),
                      "relay_extra": True}
                if verbose:
                    print(f"  同进程续跑完成：本轮更新{get(_r2x,'updated')}只，"
                          f"剩余待追平 {get(_r2x, 'pending_remain')}")
            except Exception as _e:
                r2 = {**r2, "relay_extra_error": str(_e)[:80]}
                if verbose:
                    print(f"  同进程续跑异常：{str(_e)[:80]}（留待兜底任务接力）")
        # ★ K1（2026-09-16）：指数日K收盘增量更新（P1-①）——三大指数对齐个股
        #   MAX(date)，自动回填 09-12 后缺失交易日；失败记 ERROR 审计，不吞异常。
        try:
            _idx = update_indices(verbose=verbose)
            if verbose and (_idx.get("updated") or _idx.get("failed")):
                print(f"  指数更新: 成功{_idx.get('updated')}只 失败{_idx.get('failed')}只 "
                      f"回填{_idx.get('backfilled')}根 最新{_idx.get('max_date')}")
        except Exception as _e:
            try:
                from . import audit
                audit.record(kind="daily", event="index_daily_update_failed",
                             level="ERROR", error=str(_e)[:200])
            except Exception:
                pass
            if verbose:
                print(f"  指数更新失败: {str(_e)[:120]}")
        # ★ F3（2026-09-08）：收盘更新完成后跑 amount=0 缺失哨兵（只读，失败静默）。
        #   当日存在 amount=0 且 volume>0 的行 → 追加 quality_alert.jsonl。
        #   K2（2026-09-16）：返回值 _az 供下方 data_update 日结附缺额率（不重复调用）。
        try:
            _az = _amount_zero_guard()
        except Exception:
            _az = None
        except Exception:
            pass
        # ★ E4（2026-09-13）：降级补偿 + 汇总 —— 若日K段发生降级（tdx 熔断→腾讯/新浪），
        #   缺失的当日 amount 从东财补额（只补 amount，走 _kline_save 条件覆盖）；
        #   汇总事件 source_degradation_daily 含各源命中次数与补偿成功率。
        try:
            _comp = _compensate_amount((r2 or {}).get("missing_by_src") or {},
                                       verbose=verbose)
            if verbose and _comp.get("attempted"):
                print(f"  降级补偿: 尝试{_comp.get('attempted')}只 "
                      f"成功{_comp.get('ok')} 失败{_comp.get('failed')} "
                      f"当日完整率{_comp.get('ratio')}")
        except Exception as _e:
            _comp = {"error": str(_e)[:120], "by_source": {}}
        try:
            _audit_source_degradation(_comp)
        except Exception:
            pass
        # ★ E1 + R2-P0.2：日K段结束后记覆盖 KPI（fresh/total/ratio）。
        #    R2：阈值分级——ratio<0.8 → CRITICAL（严重缺口，板块红条级）；
        #    0.8≤ratio<0.9 → WARN（原 09-03 379 缺口 0.839 属此类，不再静默）；
        #    ≥0.9 → INFO。缺口明细随事件落 audit，供诊断。
        #    ★ F4（2026-09-08）：KPI 评估时机 = 收盘更新完成（stale-first 追平）
        #    事件驱动——未追平（pending_remain>0）时记 INFO daily_coverage_pending
        #    （延后重评），不评 CRITICAL/WARN；追平后才按 R2 阈值分级评估。
        #    防"每日必然 CRITICAL 误报"（今日 15:36 ratio=0.18 CRITICAL 实为
        #    更新未完成，最终 09-08 覆盖 99.5% 实际健康）。
        _daily_done = bool(r2) and (r2.get("pending_remain") or 0) == 0
        try:
            from . import audit as _aud
            _cov = _daily_coverage()
            if _cov:
                if not _daily_done:
                    _aud.record(kind="daily", event="daily_coverage_pending",
                                level="INFO",
                                fresh=_cov["fresh"], total=_cov["total"],
                                ratio=_cov["ratio"], missing=_cov.get("missing", 0),
                                pending_remain=r2.get("pending_remain"),
                                target=_target_day(),
                                daily_updated=get(r2, "updated"),
                                note="收盘更新未完成（stale-first 仍有待追平票），"
                                     "KPI 延后重评；快照按 B1 分级落盘（0.8≤ratio 仍落，"
                                     "<0.8 延后由 22:00 兜底无条件落）")
                else:
                    if _cov["ratio"] < 0.8:
                        _lvl = "CRITICAL"
                    elif _cov["ratio"] < 0.9:
                        _lvl = "WARN"
                    else:
                        _lvl = "INFO"
                    _aud.record(kind="daily", event="daily_coverage",
                                level=_lvl,
                                fresh=_cov["fresh"], total=_cov["total"],
                                ratio=_cov["ratio"], missing=_cov.get("missing", 0),
                                missing_sample=_cov.get("missing_sample", []),
                                target=_target_day(),
                                daily_updated=get(r2, "updated"))
        except Exception:
            pass
        # ★ Phase63：全球序列回补/增量（主备切换降级；失败只记审计不影响主流程）
        try:
            rg = update_global(verbose=verbose)
        except Exception as e:
            rg = {"error": str(e)[:80]}
            try:
                from . import audit
                audit.record(kind="daily", event="global_update_failed",
                             level="WARN", error=str(e)[:120])
            except Exception:
                pass
        # ★ P0-2：结果落日志 + 审计（含 updated/rows/errors/empty/total）
        msg = (f"数据增量更新: min5 更新{get(r1, 'updated')}/{get(r1, 'total')}只"
               f" 新增{get(r1, 'rows')}根 空{get(r1, 'empty')} err{get(r1, 'errors')}"
               f" | 日K 更新{get(r2, 'updated')}只 err{get(r2, 'errors')}")
        if verbose:
            print(msg)
        try:
            _az = _amount_zero_guard()
        except Exception:
            _az = None
        # ★ K2（2026-09-16）：日结缺额率 —— data_update 事件附带当日
        #   amount0 行数/占比（复用上方 _amount_zero_guard 返回值，不重复写 alert），
        #   让"当日缺额率"随日结可见（不再需要翻 quality_alert.jsonl）。
        _az_r = {}
        if _az:
            _az_r = {"amount_zero_rows": _az.get("amount_zero_rows", 0),
                     "amount_zero_ratio": _az.get("ratio", 0.0)}
        try:
            from . import audit
            audit.record(kind="daily", event="data_update", level="INFO",
                         min5_updated=get(r1, "updated"), min5_total=get(r1, "total"),
                         min5_rows=get(r1, "rows"), min5_errors=get(r1, "errors"),
                         min5_empty=get(r1, "empty"),
                         daily_updated=get(r2, "updated"), daily_errors=get(r2, "errors"),
                         **_az_r)
        except Exception:
            pass
        # ★ P0-2：达标判定 —— min5 更新数 >= 候选一半才视为成功
        total = get(r1, "total") or 0
        updated = get(r1, "updated") or 0
        empty = get(r1, "empty") or 0
        daily_updated = get(r2, "updated") or 0
        daily_errors = get(r2, "errors") or 0
        ok = updated >= max(1, int(total * 0.5))
        if ok:
            _UPDATE_STATE["last_day"] = _target_day()
            _save_state()
            if verbose:
                print(f"  min5 达标({updated}/{total})，更新日标记为 {_UPDATE_STATE['last_day']}")
        else:
            # ★ F3/F4（2026-09-02 D 块落地）：区分"真失败"与"重复空跑"。
            #   真失败 = 数据没拉到（empty≈total 全池空 + daily 全错，如 8/21/24/28）；
            #   重复空跑 = u=0 且 empty 极小（第一遍已被先到进程写入，本遍无新数据可写）
            #   且日K成功 → 记 INFO skip_duplicate，不判 failed（根治监控狼来了）。
            dup_empty = (updated == 0 and empty < max(10, int(total * 0.1))
                         and daily_errors == 0 and daily_updated >= 1)
            if dup_empty:
                if verbose:
                    print(f"  min5 重复空跑(u=0 empty={empty} daily已成功) —— 数据已最新，非失败")
                try:
                    from . import audit
                    audit.record(kind="daily", event="data_update_skip_duplicate", level="INFO",
                                 min5_updated=0, min5_total=total, min5_empty=empty,
                                 daily_updated=daily_updated, daily_errors=0,
                                 note="重复触发空跑：第一遍已写入当日数据，本遍无新数据可写")
                except Exception:
                    pass
                # 数据既已完整，补标 last_day 防后续触发再次空跑/再次误报
                _UPDATE_STATE["last_day"] = _target_day()
                _save_state()
            else:
                warn = (f"min5 更新不达标({updated}/{total})，保留旧更新日，下次启动/收盘后重试")
                print(warn)
                try:
                    from . import audit
                    audit.record(kind="daily", event="data_update_failed", level="WARN",
                                 min5_updated=updated, min5_total=total)
                except Exception:
                    pass
        # ★ Phase46：更新成功后冻结数据快照（治前复权重锚定）+ 数据哨兵巡检。
        #   任一失败只记审计，不影响主流程。
        #   ★ B1（2026-09-13）：快照策略从"追平才落盘"改为**分级落盘**——
        #   ratio>=0.95 正常落（daily_done=true）；0.8~0.95 带病落（WARN partial）；
        #   <0.8 延后（deferred，22:00 当日最终兜底无条件落，宁可带病不可缺失）。
        #   审计事件（data_snapshot / data_snapshot_partial / data_snapshot_deferred /
        #   data_snapshot_forced / data_snapshot_repaired）由 auto_snapshot 内部记，
        #   updater 不再按 _daily_done 二分支双记。
        if ok:
            try:
                from . import data_snapshot as _dsnap
                _p, _created, _grade = _dsnap.auto_snapshot(coverage=_cov)
                if verbose:
                    print(f"  快照分级落盘: grade={_grade} path={_p}")
            except Exception as _snap_err:
                try:
                    from . import audit
                    audit.record(kind="daily", event="data_snapshot_failed",
                                 level="WARN", error=str(_snap_err))
                except Exception:
                    pass
            # 数据哨兵（只读）：复用 tools/data_sentinel.py，异常摘要追加 quality_alert.jsonl
            try:
                import os as _os
                import subprocess as _sub
                import sys as _sys
                _tools = _os.path.join(
                    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
                _sentinel = _os.path.join(_tools, "data_sentinel.py")
                if _os.path.isfile(_sentinel):
                    _sub.run([_sys.executable, _sentinel], timeout=900,
                             stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)
                    _qr = _os.path.join(C.DATA_DIR, "quality_report.json")
                    if _os.path.isfile(_qr):
                        with open(_qr, encoding="utf-8") as _f:
                            _rep = json.load(_f)
                        _qfq = _rep.get("qfq_jump_candidates") or {}
                        _alert = {
                            "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "high_severity": _qfq.get("high_severity"),
                            "candidates_total": _qfq.get("candidates_total"),
                            "min5_latest": (_rep.get("min5_coverage") or {}).get("latest_date"),
                            "ml_pred_last": (_rep.get("ml_pred_freshness") or {}).get("last_pred_date"),
                        }
                        with open(_os.path.join(C.DATA_DIR, "quality_alert.jsonl"), "a",
                                  encoding="utf-8", newline="\n") as _af:
                            _af.write(json.dumps(_alert, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return {"min5": r1, "daily": r2, "ok": ok}
    finally:
        _UPDATE_STATE["running"] = False
        _release_update_lock(_lk)


def get(d, k, default=0):
    """安全取值（dict 可能为 None/缺键）"""
    try:
        return d.get(k, default) if d else default
    except Exception:
        return default


def start_background_update(verbose=False):
    """启动时后台增量更新（daemon 线程，不阻塞服务启动）"""
    if not needs_update():
        return
    def _worker():
        try:
            run_update(verbose=verbose)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()


def _register_close_interrupt_trace():
    """★ A4（2026-09-13）：强杀留痕。注册 SIGINT(Ctrl+C)/SIGBREAK(控制台关闭)
    处理：被终止时向 stdout（cmd 已重定向到 close_update_cron.log）写一行带
    时间戳的中断记录再退出——0xC000013A(STATUS_CONTROL_C_EXIT) 强杀场景下
    即使 cmd 的收尾 echo 来不及执行，日志仍有中断留痕。
    退出码语义：0=正常/无需更新（含非交易日跳过），1=更新失败，
    0xC000013A(3221225786)=被 Ctrl+C/控制台关闭终止。"""
    import datetime as _dt
    import signal as _sig
    import sys as _sys

    def _intr(_signum, _frame):
        try:
            print(f"[A4] {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                  f"close_update 被中断 signal={_signum} "
                  f"(0xC000013A=被Ctrl+C/控制台关闭终止)，中断留痕", flush=True)
        except Exception:
            pass
        _sys.exit(130)
    try:
        _sig.signal(_sig.SIGINT, _intr)
    except Exception:
        pass
    try:
        _sig.signal(_sig.SIGBREAK, _intr)
    except Exception:
        pass


def _catchup_no_progress_mark(day, pending, p=None):
    """★K3（2026-09-16）：接力空转状态落盘（跨进程共享，data/close_catchup_progress.json）。
    同一交易日连续 2 轮 pending_remain 完全不变 → 判定空转（调用方停止重跑并记 WARN）。
    跨日 / pending<=0 / pending 变化 → 计数重置。返回连续不变轮数（0=未连续）。
    p 可注入临时状态文件（验收单测用）。"""
    p = p or os.path.join(C.DATA_DIR, "close_catchup_progress.json")
    st = {}
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
    except Exception:
        st = {}
    if st.get("day") != day or pending <= 0:
        st = {"day": day, "last_pending": pending, "same_count": 0}
    else:
        if st.get("last_pending") == pending:
            st["same_count"] = int(st.get("same_count", 0)) + 1
        else:
            st["same_count"] = 0
        st["last_pending"] = pending
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass
    return st["same_count"]


def _close_cli():
    """★ F5/F4/A4/A1/A3 收盘兜底入口的完整逻辑（--close 分支）：
    A4 强杀留痕 + 非交易日跳过 → A3 22:00 收尾（close_catchup_deadline WARN/INFO）
    → A3 判据（覆盖率<0.95 或当日无快照）→ 达标 no-op / 未达标执行（A1 等锁轮询至
    22:00 硬停）→ 复检。抽成函数便于单测（tmp/a3_sim.py 模拟验收）。"""
    # ★ A4（2026-09-13）：强杀留痕 + 非交易日不跑收盘更新。
    #   顺序：先注册中断留痕（任何路径被强杀都有日志证据）→ 非交易日判定
    #   （最前面，不做任何网络/DB 操作，直接 exit 0）。
    _register_close_interrupt_trace()
    import datetime as _dt
    import sys as _sys
    from . import trading_calendar as _tcal
    _today = _dt.date.today().strftime("%Y-%m-%d")
    if not _tcal.is_trading_day(_today):
        print(f"[A4] {_today} 非交易日（周末/节假日），收盘更新跳过"
              f"（无网络/DB 操作，exit 0）", flush=True)
        _sys.exit(0)
    # ★ A3（2026-09-13）：兜底接力化——判据 = 当日覆盖率 < 0.95 或当日无快照
    #   （CLOSE_CATCHUP_TARGET / require_snapshot），达标 no-op（无网络操作）；
    #   15:45~22:00 窗口内由 TianjiCloseCatchup 每 30 分钟触发；22:00 后
    #   不再执行更新，未达标记 WARN 收尾事件（close_catchup_deadline）。
    _hm = int(time.strftime("%H%M"))
    if _hm >= CLOSE_CATCHUP_END:
        # ★ B1（2026-09-13）：22:00 当日最终兜底——无论覆盖率如何，当日仍无
        #   快照则无条件落盘（宁可带病不可缺失）+ CRITICAL data_snapshot_forced。
        #   CloseCatchup 触发器 Duration 已延长至 22:45，保证 22:00 后有触发轮。
        try:
            from . import data_snapshot as _dsnap
            _fp, _fcreated, _fgrade = _dsnap.ensure_daily_final()
            print(f"  22:00 最终兜底: grade={_fgrade} path={_fp}")
        except Exception as _ferr:
            print(f"  22:00 最终兜底异常: {_ferr}")
        _needed_d, _ratio_d, _pending_d, _thr_d = close_fallback_needed(
            thr=CLOSE_CATCHUP_TARGET, require_snapshot=True)
        try:
            from . import audit
            audit.record(kind="daily", event="close_catchup_deadline",
                         level=("WARN" if _needed_d else "INFO"),
                         ratio=_ratio_d, threshold=_thr_d, pending_remain=_pending_d,
                         note=("22:00 接力窗口关闭仍未达标，留待明日主更新"
                               if _needed_d else "22:00 接力窗口关闭，当日已达标"))
        except Exception:
            pass
        print(f"22:00 接力窗口已关闭（ratio={_ratio_d:.4f}，目标≥{_thr_d}，"
              f"未追平票={_pending_d}）" + ("，未达标 → 记 WARN 收尾（不执行更新）"
                                          if _needed_d else "，已达标"))
        _sys.exit(0)
    # ★ F4（2026-09-08）+A3（2026-09-13）：兜底判据 = 当日覆盖率<0.95 或
    #   当日无快照（close_fallback_needed；run_update 自带 update.lock 与
    #   幂等 upsert，重复执行无副作用）。
    _needed, _ratio, _pending, _thr = close_fallback_needed(
        thr=CLOSE_CATCHUP_TARGET, require_snapshot=True)
    if not _needed:
        print(f"覆盖率达标（ratio={_ratio:.4f}≥{_thr}，pending={_pending}），"
              f"兜底跳过（无网络操作）")
    else:
        print(f"覆盖率未达标（ratio={_ratio:.4f}，阈值={_thr}，未追平票={_pending}），"
              f"执行收盘兜底更新（幂等） ...")
        # ★ K3（2026-09-16）：接力空转保护——同一交易日连续 2 轮 pending_remain 完全不变
        #   → 剩余票多为停牌/退市/无源票，再跑也是每 30 分钟烧一轮全量更新，停止重跑。
        if _pending > 0 and _catchup_no_progress_mark(_today, _pending) >= 2:
            try:
                from . import audit
                audit.record(kind="daily", event="close_catchup_no_progress",
                             level="WARN", target_day=_today,
                             pending_remain=_pending, consecutive_rounds=2,
                             note="接力连续 2 轮未追平票数完全不变（多为停牌/退市/无源票），"
                                  "停止重跑，留待明日主更新")
            except Exception:
                pass
            print(f"接力空转保护：pending={_pending} 连续 2 轮完全不变，"
                  f"停止本轮重跑（记 WARN close_catchup_no_progress）")
            _sys.exit(0)
        # ★ A1（2026-09-13）：主流程（15:10 触发，长跑可达 40min+）持 update.lock
        #   时 run_update 直接 skipped（09-08~11 兜底"0 进展"根因）。
        #   改为轮询等锁重试：每 30s 一次，最迟等到 22:00 硬停。
        res = None
        try:
            import datetime as _dt
            _now = time.localtime()
            _deadline = _dt.datetime(_now.tm_year, _now.tm_mon, _now.tm_mday,
                                     22, 0, 0).timestamp()
        except Exception:
            _deadline = time.time() + 45 * 60
        while True:
            res = run_update(verbose=True)
            if not (isinstance(res, dict) and res.get("skipped")):
                break
            if time.time() >= _deadline:
                print("  已到 22:00，update.lock 仍被占用，放弃本轮兜底（记 WARN 由明日接力）")
                try:
                    from . import audit
                    audit.record(kind="daily", event="close_fallback_lock_wait_timeout",
                                 level="WARN",
                                 note="兜底任务等锁至 22:00 仍被主流程占用，放弃本轮")
                except Exception:
                    pass
                break
            print("  update.lock 被占用（主流程运行中），30s 后重试 ...")
            time.sleep(30)
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        try:
            _needed2, _ratio2, _pending2, _ = close_fallback_needed(
                thr=CLOSE_CATCHUP_TARGET, require_snapshot=True)
            print(f"兜底重跑后：ratio={_ratio2:.4f}，未追平票={_pending2}"
                  + ("，覆盖达标" if not _needed2
                     else "，仍未达标（22:00 窗口内下个 30 分钟再试 / 窗口外留待明日）"))
        except Exception:
            pass


if __name__ == "__main__":
    # ★ F5（2026-09-02 D 块落地）：新增 --close 收盘兜底入口（供独立计划任务 15:45 触发）。
    import argparse as _ap
    _p = _ap.ArgumentParser(description="updater CLI：全球序列回补/增量 或 收盘增量更新兜底")
    _p.add_argument("--global", dest="g", action="store_true",
                    help="全球源回补/增量（原入口，默认）")
    _p.add_argument("--close", action="store_true",
                    help="收盘增量更新兜底：★F4/A3 按实际覆盖率判据（当日覆盖率<0.95"
                         "或当日无快照则强制重跑，幂等）；不再只看 needs_update()")
    _p.add_argument("--verbose", action="store_true")
    _a = _p.parse_args()
    if _a.close:
        _close_cli()
    else:
        print(json.dumps(update_global(verbose=True), ensure_ascii=False, indent=1))


def warm_moneyflow_top(limit=30):
    """★ 4.5.1 冷启动预热水面缓存：对当日成交额 top-N 预跑资金面信号
    （龙虎榜/两融/北向/席位/主力），首轮扫描不再现拉（东财慢/被封时省 7~25s）。
    后台线程执行，不阻塞服务。
    """
    def _worker():
        try:
            from . import datafeed as _df
            from . import scoring as _sc
            stocks = _df.fetch_all_stocks()   # 顺带预热全市场行情缓存（TDX ≈2s）
            top = sorted(stocks.items(), key=lambda kv: -(kv[1].get("amount") or 0))[:limit]
            if not top:
                return
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(max_workers=8) as ex:
                list(ex.map(
                    lambda kv: (_sc.moneyflow_signals(kv[0], kv[1].get("name", "")), kv[0]),
                    top))
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()
