# -*- coding: utf-8 -*-
"""★ Phase31：数据质量哨兵 —— 自动发现脏数据，而不是等它坑回测

只读检查（SQLite 全程 mode=ro），不修改任何数据库/config；输出
data/quality_report.json + 终端摘要。异常候选只列清单，是否进
config.DATA_EXCLUDE_CODES 由人工决定（操作流程见 docs/operations.md
「数据与备份/年度维护」与 docs/reports/clean_data_baseline.md 的既有流程）。

检查项：
  ① kline 缺口：每只票在自身 [首日,末日] 内缺失的交易日
     （经验日历=全市场日期并集；新股上市前自然排除；volume=0 行视为停牌标记）
     并对照 app/trading_calendar.py 校验近 60 个交易日的日历一致性
     （节假日表仅覆盖 2025-2026，更早年份用经验日历避免海量误报）
  ② 复权异常：相邻两日 |次日开盘/当日收盘 - 1| > 板块涨跌停限制 + 0.5% 缓冲
     （复刻 Phase20 规则：>2×限制+1% 记 high，其余记 medium；
       剔除上市前 5 个交易日；2019 起）
  ③ 股票名单新鲜度：stock_list.json ↔ kline 库互相核对
  ④ min5.db 覆盖度：最新日期/总行数/近 5 个交易日是否有更新
  ⑤ ml_pred 新鲜度：最后预测日期距今是否 >25 个交易日
  ⑥ global_kline 积累检查（美元指数 DINIW 兜底依赖它）
  ⑦ 历史长度分桶 + 票池同构 KPI（实盘扫描池 vs 回测池交集占比，F6）
  ⑧ 停牌占位行非物理值检测（0<vol<100 或 0<amt<1000，F6）
  ⑨ 指数完整性（high>=low、amount>0 占比、最新距今天数，F6）
  ⑩ 当日缺票清单 + 停牌/预算截断归因（F6）

用法: python tools/data_sentinel.py [--full-gaps]   # --full-gaps 输出全部缺口明细
输出: data/quality_report.json
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C                    # 只读引用：路径与排除清单
from app import trading_calendar as tc         # 只读引用：官方交易日历

OUT_JSON = os.path.join(C.DATA_DIR, "quality_report.json")
STOCK_LIST_FILE = os.path.join(C.DATA_DIR, "stock_list.json")
REPAIR_FAILED_FILE = os.path.join(C.DATA_DIR, "qfq_repair_failed.json")

GAP_DETAIL_TOP = 80            # 缺口明细最多详列的票数（按缺口天数降序）
QFQ_SCAN_FROM = "2019-01-01"   # Phase20 口径起点
LISTING_GRACE_DAYS = 5         # 上市前 5 个交易日不判复权异常（Phase20 规则）
LIMIT_BUFFER = 0.005           # 涨跌停限制 + 0.5% 缓冲
ML_PRED_STALE_TDAYS = 25       # ml_pred 最后预测距今超过 N 个交易日 → 提醒滚动训练
MIN5_STALE_TDAYS = 5           # min5 最近 N 个交易日无更新 → 提醒


def ro_conn(path):
    # F6: 服务运行中 delete 模式已 checkpoint（WAL=0），mode=ro 会因 -shm 被独占
    # 报 disk I/O error → 改 immutable=1 读一致快照（巡检只读可接受）。
    return sqlite3.connect("file:%s?mode=ro&immutable=1" % path, uri=True, timeout=15)


def load_names_and_excludes():
    """stock_list.json: [[code,name,price],...] → {code:name}；外加排除清单"""
    names = {}
    if os.path.exists(STOCK_LIST_FILE):
        try:
            with open(STOCK_LIST_FILE, encoding="utf-8") as f:
                for row in json.load(f):
                    if isinstance(row, (list, tuple)) and row:
                        names[str(row[0])] = str(row[1]) if len(row) > 1 else ""
        except Exception:
            pass
    return names


def limit_pct_of(code, name=""):
    """板块涨跌停限制（复刻 engine.limit_pct_of 规则，只读复刻不 import 引擎）"""
    if code.startswith(("4", "8", "92")):
        return 0.30
    n = (name or "").upper()
    if "ST" in n:
        return 0.05
    if code.startswith(("30", "68")):
        return 0.20
    return 0.10


# ============ ① kline 缺口 + 日历一致性 ============
def check_kline_gaps(cur, cal_days_recent):
    """单遍流式扫描 period='day' 全表：
    - 经验日历 = 全市场日期并集
    - 每票在其 [首日,末日] 内相对经验日历的缺口
    - volume=0 行计数（停牌标记）
    """
    emp_cal = set()
    cur.execute("SELECT DISTINCT date FROM kline WHERE period='day'")
    for r in cur:
        emp_cal.add(r[0])
    emp_sorted = sorted(emp_cal)
    emp_idx = {d: i for i, d in enumerate(emp_sorted)}

    codes_scanned = 0
    vol0_rows = 0
    bad_rows = 0
    gaps = {}          # code -> {"missing": n, "max_run": k, "samples": [...]}
    jumps = []         # ② 复权异常候选（顺带在同一遍完成）
    first_dates = {}   # code -> 扫描范围内首个交易日（上市豁免用）
    prev_by_code = None

    cur.execute(
        "SELECT code, date, open, close, volume FROM kline "
        "WHERE period='day' AND date>=? ORDER BY code, date", (QFQ_SCAN_FROM,))
    cur_code = None
    seen_dates = []

    def flush(code):
        nonlocal codes_scanned
        if not code or not seen_dates:
            return
        codes_scanned += 1
        first_dates[code] = seen_dates[0]
        i0, i1 = emp_idx.get(seen_dates[0]), emp_idx.get(seen_dates[-1])
        if i0 is None or i1 is None:
            return
        present = set(seen_dates)
        missing = [d for d in emp_sorted[i0:i1 + 1] if d not in present]
        if missing:
            # 最长连续缺口
            max_run = run = 1
            for a, b in zip(missing, missing[1:]):
                run = run + 1 if emp_idx[b] - emp_idx[a] == 1 else 1
                max_run = max(max_run, run)
            gaps[code] = {
                "missing": len(missing),
                "max_run": max_run,
                "first": seen_dates[0], "last": seen_dates[-1],
                "samples": missing[:8],
            }

    for code, date, o, c, v in cur:
        if code != cur_code:
            flush(cur_code)
            cur_code, seen_dates = code, []
        if c is None or o is None or c <= 0 or o <= 0:
            bad_rows += 1
            continue
        if not v:
            vol0_rows += 1     # volume=0：停牌标记（有行但零成交）
        seen_dates.append(date)
        # ② 复权异常：收盘→次日开盘跳变（同票相邻行）
        if prev_by_code is not None and prev_by_code[0] == code:
            pdate, pclose = prev_by_code[1], prev_by_code[3]
            if date > pdate and pclose and pclose > 0:
                jump = o / pclose - 1.0
                if abs(jump) > LIMIT_BUFFER:
                    # 占位，阈值判断放后面统一做（需要名字与上市豁免）
                    jumps.append((code, pdate, date, pclose, o, jump))
        prev_by_code = (code, date, o, c)
    flush(cur_code)

    # 近期窗口的日历一致性：官方日历近 60 交易日 vs 经验日历
    cal_mismatch = []
    today = time.strftime("%Y-%m-%d")
    for d in cal_days_recent:
        if d not in emp_cal:
            note = "（今日数据尚未更新属正常）" if d == today else ""
            cal_mismatch.append({"date": d,
                                 "issue": "官方日历为交易日但全库无任何K线" + note})
    extra = sorted(d for d in emp_sorted[-60:] if d in tc.HOLIDAYS or d in tc._user_holidays)
    for d in extra:
        cal_mismatch.append({"date": d, "issue": "库内有K线但官方日历标记为节假日（可能节假表误配或数据脏）"})

    return {
        "codes_scanned": codes_scanned,
        "empirical_trading_days": len(emp_sorted),
        "first": emp_sorted[0] if emp_sorted else None,
        "last": emp_sorted[-1] if emp_sorted else None,
        "volume0_suspension_rows": vol0_rows,
        "bad_rows_null_or_nonpositive_price": bad_rows,
        "codes_with_gaps": len(gaps),
        "gaps_top": [
            {"code": k, **v} for k, v in
            sorted(gaps.items(), key=lambda kv: (-kv[1]["missing"], -kv[1]["max_run"]))[:GAP_DETAIL_TOP]
        ],
        "gaps_all_counts": {k: v["missing"] for k, v in sorted(gaps.items())},
        "calendar_mismatch": cal_mismatch[:40],
    }, gaps, jumps, first_dates, emp_idx


def check_qfq_jumps(jumps, names, excludes, first_dates, emp_idx):
    """对①中收集的跳变做阈值与豁免过滤（Phase20 口径：剔上市前5个交易日）"""
    known_failed = set()
    if os.path.exists(REPAIR_FAILED_FILE):
        try:
            with open(REPAIR_FAILED_FILE, encoding="utf-8") as f:
                d = json.load(f)
            rows = d.get("codes") or d.get("stocks") or d
            if isinstance(rows, dict):
                known_failed = set(map(str, rows.keys()))
            elif isinstance(rows, list):
                known_failed = set(str(r[0]) if isinstance(r, (list, tuple)) else str(r)
                                   for r in rows)
        except Exception:
            pass

    out = []
    for code, d0, d1, pc, o, jump in jumps:
        lim = limit_pct_of(code, names.get(code, ""))
        thr_high = 2 * lim + 0.01          # Phase20: >21%（主板）/31%（北交所）
        thr_low = lim + LIMIT_BUFFER       # 本任务口径：限制+0.5%
        aj = abs(jump)
        if aj <= thr_low:
            continue
        # 上市豁免：跳变日距该票在扫描范围内的首个交易日不足 5 个经验交易日 → 不判
        fd = first_dates.get(code)
        if fd is not None:
            i0, i1 = emp_idx.get(fd), emp_idx.get(d1)
            if i0 is not None and i1 is not None and i1 - i0 < LISTING_GRACE_DAYS:
                continue
        sev = "high" if aj > thr_high else "medium"
        out.append({
            "code": code, "name": names.get(code, ""),
            "prev_date": d0, "date": d1,
            "prev_close": round(pc, 3), "open": round(o, 3),
            "jump_pct": round(jump * 100, 2),
            "limit_pct": lim, "severity": sev,
            "already_excluded": code in excludes,
            "in_repair_failed": code in known_failed,
        })
    out.sort(key=lambda x: -abs(x["jump_pct"]))
    high = sum(1 for x in out if x["severity"] == "high")
    return {
        "rule": "|次日开盘/当日收盘-1| > 板块限幅+%0.1f%% 即候选；>2x限幅+1%% 为 high（Phase20 口径）；范围 %s 起" % (
            LIMIT_BUFFER * 100, QFQ_SCAN_FROM),
        "candidates_total": len(out),
        "high_severity": high,
        "new_not_in_excludes": sum(1 for x in out if not x["already_excluded"]),
        "candidates": out[:1500],  # P71: 提高上限以覆盖全部 1112 候选（原 300 截断导致 812 未入库）
    }


# ============ ③ 名单新鲜度 ============
def check_stock_list(cur):
    res = {"file_exists": os.path.exists(STOCK_LIST_FILE)}
    if not res["file_exists"]:
        return res
    with open(STOCK_LIST_FILE, encoding="utf-8") as f:
        lst = json.load(f)
    listed = [str(r[0]) for r in lst if isinstance(r, (list, tuple)) and r]
    cur.execute("SELECT DISTINCT code FROM kline WHERE period='day'")
    ink = set(str(r[0]) for r in cur)
    lset = set(listed)
    res.update({
        "list_count": len(listed),
        "kline_code_count": len(ink),
        "in_list_no_kline": sorted(lset - ink)[:200],      # 名单有但无K线（新上市未拉数据）
        "in_list_no_kline_count": len(lset - ink),
        "has_kline_not_in_list": sorted(ink - lset)[:200],  # 有K线但名单消失（退市/更名）
        "has_kline_not_in_list_count": len(ink - lset),
    })
    return res


# ============ ④ min5 覆盖度 ============
def check_min5():
    res = {"db_exists": os.path.exists(C.MIN5_DB_FILE)}
    if not res["db_exists"]:
        return res
    conn = ro_conn(C.MIN5_DB_FILE)
    try:
        res["total_rows"] = conn.execute("SELECT COUNT(*) FROM kline_min5").fetchone()[0]
        res["distinct_codes"] = conn.execute("SELECT COUNT(DISTINCT code) FROM kline_min5").fetchone()[0]
        res["latest_date"] = conn.execute("SELECT MAX(date) FROM kline_min5").fetchone()[0]
        res["latest_rows_on_latest_date"] = conn.execute(
            "SELECT COUNT(*) FROM kline_min5 WHERE date=?", (res["latest_date"],)).fetchone()[0]
    finally:
        conn.close()
    return res


# ============ ⑤ ml_pred 新鲜度 ============
def check_ml_pred(today):
    conn = ro_conn(C.DB_FILE)
    try:
        row = conn.execute("SELECT MAX(date), COUNT(*), COUNT(DISTINCT date) FROM ml_pred").fetchone()
    finally:
        conn.close()
    last = row[0]
    stale_tdays = len(tc.trading_days(last, today)) - 1 if last else None
    return {
        "last_pred_date": last,
        "total_rows": row[1],
        "distinct_dates": row[2],
        "trading_days_since_last": stale_tdays,
        "stale_threshold_tdays": ML_PRED_STALE_TDAYS,
        "stale": bool(stale_tdays is not None and stale_tdays > ML_PRED_STALE_TDAYS),
        "hint": "超过 %d 个交易日未出新预测 → 滚动训练该跑了" % ML_PRED_STALE_TDAYS,
    }


# ============ ⑤b ml_scores 覆盖率（★ Phase76 补项） ============
ML_SCORES_COVER_WARN = 0.30     # scores 数 / bt_pool 数 低于 30% 记 WARN
def check_ml_scores_coverage(today):
    """ml_scores.json 覆盖率与新鲜度：predict.py 实测只产出 ~177/700 只
    （最新 bar 特征 NaN 剔除），覆盖率骤降即预警。"""
    path = os.path.join(C.DATA_DIR, "ml_scores.json")
    res = {"file_exists": os.path.isfile(path)}
    if not res["file_exists"]:
        return res
    try:
        d = json.load(open(path, encoding="utf-8"))
        n_scores = len(d.get("scores") or {})
        sig_date = d.get("date") or ""
        pool_n = 0
        with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
            pool_n = len(json.load(f).get("codes") or [])
        cover = (n_scores / pool_n) if pool_n else None
        stale_tdays = len(tc.trading_days(sig_date, today)) - 1 if sig_date else None
        warn = []
        if cover is not None and cover < ML_SCORES_COVER_WARN:
            warn.append("覆盖率%.0f%%<%d%%" % (cover * 100, ML_SCORES_COVER_WARN * 100))
        if stale_tdays is not None and stale_tdays > MIN5_STALE_TDAYS:
            warn.append("信号日距今%d个交易日" % stale_tdays)
        res.update({
            "scores": n_scores, "pool_size": pool_n,
            "coverage": round(cover, 4) if cover is not None else None,
            "signal_date": sig_date,
            "trading_days_since_signal": stale_tdays,
            "warn": "；".join(warn) if warn else None,
        })
    except Exception as e:
        res["warn"] = "读取失败: %s" % e
    return res


# ============ ⑥ global_kline 积累 ============
def check_global_kline(today):
    conn = ro_conn(C.DB_FILE)
    try:
        rows = conn.execute(
            "SELECT market, sym, COUNT(*), MIN(date), MAX(date) "
            "FROM global_kline GROUP BY market, sym").fetchall()
    finally:
        conn.close()
    items = []
    for market, sym, n, d0, d1 in rows:
        stale_tdays = len(tc.trading_days(d1, today)) - 1 if d1 and d1 < today else 0
        items.append({
            "market": market, "sym": sym, "rows": n,
            "first": d0, "latest": d1,
            "trading_days_since_latest": stale_tdays,
            "thin": n < 30,   # 积累不足：少于约一个半月的日频数据
        })
    usdx = next((x for x in items if x["sym"] == "DINIW"), None)
    return {
        "series": items,
        "usdx_index": usdx,
        "usdx_ok": bool(usdx and not usdx["thin"] and usdx["trading_days_since_latest"] <= MIN5_STALE_TDAYS),
        "note": "美元指数兜底依赖 global_kline(sym=DINIW)；thin=累计<30行",
    }


# ============ 主流程 ============
# ============ ⑦ 历史长度分桶 + 票池同构 KPI（★ F6 2026-09-09） ============
def check_history_buckets(cur):
    """全 kline 票历史长度分桶 + 同构 KPI：
    - 实盘扫描池 = 全 kline 票（5328 只，含 2876 只 ≤100 根短史票）
    - 回测池 = 历史 ≥ MIN_HISTORY_BARS（因子最长窗口 MA250/年线/250日新高）的票
    - intersection_pct 过低 → 回测收益无法代表实盘（策略调参结论失效风险）
    """
    min_bars = int(getattr(C, "MIN_HISTORY_BARS", 250) or 250)
    buckets = {"<=100": 0, "101-300": 0, "301-800": 0, "801-2000": 0, ">2000": 0}
    rows = []
    cur.execute("SELECT code, COUNT(1) FROM kline WHERE period='day' GROUP BY code")
    total = 0
    for code, n in cur:
        total += 1
        if n <= 100:
            buckets["<=100"] += 1
        elif n <= 300:
            buckets["101-300"] += 1
        elif n <= 800:
            buckets["301-800"] += 1
        elif n <= 2000:
            buckets["801-2000"] += 1
        else:
            buckets[">2000"] += 1
        rows.append((str(code), int(n)))
    pool_ok = sum(1 for _, n in rows if n >= min_bars)
    long_2000 = buckets[">2000"]
    kpi = round(pool_ok / total, 4) if total else 0.0
    return {
        "total_codes": total,
        "min_bars_required": min_bars,
        "buckets": buckets,
        "pool_long_enough": pool_ok,
        "pool_short": total - pool_ok,
        "pool_short_pct": round((total - pool_ok) / total, 4) if total else 0.0,
        "long_history_over2000": long_2000,          # 任务书口径的"长历史回测池"
        "homogeneity_kpi": {
            "live_pool_size": total,
            "backtest_pool_size": pool_ok,
            "intersection_pct": kpi,
            "verdict": "OK" if kpi >= 0.8 else "WARN",
        },
        "short_history_codes_sample": [c for c, _ in sorted(rows, key=lambda x: x[1])[:20]],
        "verdict": "WARN" if (total - pool_ok) / max(1, total) > 0.3 else "OK",
    }


# ============ ⑧ 停牌占位行非物理值检测（★ F6 2026-09-09） ============
def check_suspension_placeholders(cur):
    """0<volume<100（股）或 0<amount<1000（元）＝非物理量（A 股最小成交 1 手=100 股，
    最小成交额>100 元）。F6 实锤：11 行 volume=5.88e-37/amount=5.88e-39（denormal 双写），
    会绕过"volume=0 即停牌"判定、污染量比/换手/均量线。"""
    n = cur.execute(
        "SELECT COUNT(1) FROM kline WHERE period='day' "
        "AND ((volume>0 AND volume<100) OR (amount>0 AND amount<1000))").fetchone()[0]
    rows = []
    cur.execute(
        "SELECT code, date, open, high, low, close, volume, amount FROM kline "
        "WHERE period='day' AND ((volume>0 AND volume<100) OR (amount>0 AND amount<1000)) "
        "ORDER BY date DESC LIMIT 30")
    for r in cur:
        rows.append({"code": r[0], "date": r[1], "open": r[2], "high": r[3],
                     "low": r[4], "close": r[5], "volume": r[6], "amount": r[7]})
    all_eq = sum(1 for r in rows if r["open"] == r["high"] == r["low"] == r["close"])
    return {
        "non_physical_rows": n,
        "samples": rows,
        "all_ohlc_equal_ratio": round(all_eq / len(rows), 2) if rows else 0.0,
        "verdict": "WARN" if n else "OK",
    }


# ============ ⑨ 指数完整性（★ F6 2026-09-09，给 F1 长期看护） ============
INDEX_CODES = ["sh000001", "sz399001", "sz399006", "000905"]


def check_index_integrity(cur):
    out = {}
    for code in INDEX_CODES:
        r = cur.execute(
            "SELECT COUNT(1), "
            "SUM(CASE WHEN high<low THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN amount>0 THEN 1 ELSE 0 END), MAX(date) FROM kline "
            "WHERE code=? AND period='day'", (code,)).fetchone()
        n, bad, amt_ok, latest = (r[0] or 0), (r[1] or 0), (r[2] or 0), r[3]
        days = -1
        if latest:
            try:
                days = len(tc.trading_days(latest, time.strftime("%Y-%m-%d"))) - 1
            except Exception:
                days = -1
        out[code] = {"rows": n, "high_lt_low": bad,
                     "amount_positive_pct": round(amt_ok / n, 4) if n else 0.0,
                     "latest": latest, "days_since_latest": days}
    ok = all(v["rows"] > 0 and v["high_lt_low"] == 0 and 0 <= v["days_since_latest"] <= 2
             for v in out.values())
    return {"indices": out, "verdict": "OK" if ok else "WARN"}


# ============ ⑩ 当日缺票清单 + 归因（★ F6 2026-09-09） ============
def check_missing_today(cur, today):
    hm = int(time.strftime("%H%M"))
    dates = [r[0] for r in cur.execute(
        "SELECT DISTINCT date FROM kline WHERE period='day' ORDER BY date DESC LIMIT 3")]
    if len(dates) < 2:
        return {"verdict": "SKIP", "reason": "不足两个交易日"}
    # 盘中（<15:10 收盘更新前）：今日行不完整（仅部分票有盘中行）→
    # 跳过进行中的今日，检查"上一完整交易日 vs 再前一日"。
    if hm < 1510 and len(dates) >= 3 and dates[0] == today:
        d_today, d_prev = dates[1], dates[2]
    else:
        d_today, d_prev = dates[0], dates[1]
    have_today = {r[0] for r in cur.execute(
        "SELECT code FROM kline WHERE period='day' AND date=?", (d_today,))}
    have_prev = {r[0] for r in cur.execute(
        "SELECT code FROM kline WHERE period='day' AND date=?", (d_prev,))}
    missing = sorted(str(x) for x in (have_prev - have_today))
    last_map = {}
    for code in missing:
        r = cur.execute("SELECT MAX(date) FROM kline WHERE code=? AND period='day'",
                        (code,)).fetchone()
        last_map[code] = r[0] if r else None
    stale_residual = [c for c, l in last_map.items() if l == d_prev]
    older = [c for c, l in last_map.items() if l and l < d_prev]
    return {
        "asof": d_today, "prev": d_prev,
        "prev_codes": len(have_prev), "today_codes": len(have_today),
        "missing_count": len(missing),
        "missing_codes": missing,
        "last_eq_prev_count": len(stale_residual),      # 昨前日有 → 今日未补（预算截断/停牌1日）
        "last_eq_prev_codes": stale_residual,
        "last_older_count": len(older),                 # 更早 → 停牌/长停嫌疑
        "last_older_codes": older,
        "verdict": "WARN" if missing else "OK",
    }


def _emit_quality_alerts(report):
    """★ D1（2026-09-13）：哨兵关键项 → quality_alert.jsonl + audit 转发（双轨打通）。
    判定：复权跳变 high_severity>0 / ml_pred 陈旧 / min5 覆盖陈旧 → 各写一条 WARN 事件；
    转发经 audit.quality_alert_forward（连续 ≥2 交易日自动升级 WARN→CRITICAL）。
    只读检查，失败静默。"""
    try:
        alerts = []
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        qfq = report.get("qfq_jump_candidates") or {}
        hs = qfq.get("high_severity") or 0
        if hs > 0:
            alerts.append({"date": ts, "event": "sentinel_qfq_high_severity",
                           "level": "WARN", "high_severity": hs,
                           "candidates_total": qfq.get("candidates_total"),
                           "note": "复权跳变高风险候选（未入排除清单），需人工复核"})
        ml = report.get("ml_pred_freshness") or {}
        if ml.get("stale"):
            alerts.append({"date": ts, "event": "sentinel_ml_pred_stale",
                           "level": "WARN",
                           "last_pred_date": ml.get("last_pred_date"),
                           "trading_days_since_last":
                               ml.get("trading_days_since_last"),
                           "note": "ML 预测陈旧（ml_pred 表长期未滚动训练）"})
        m5 = report.get("min5_coverage") or {}
        latest5 = str(m5.get("latest_date") or "")[:10]
        if latest5:
            last_td = tc.prev_trading_day(time.strftime("%Y-%m-%d"))
            if latest5 < last_td:
                alerts.append({"date": ts, "event": "sentinel_min5_stale",
                               "level": "WARN", "latest_min5_date": latest5,
                               "note": "min5 最新覆盖 %s 早于最近交易日 %s"
                                       % (latest5, last_td)})
        if not alerts:
            return
        qa = os.path.join(C.DATA_DIR, "quality_alert.jsonl")
        with open(qa, "a", encoding="utf-8", newline="\n") as f:
            for a in alerts:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
        try:
            from app import audit as _audit
            for a in alerts:
                _audit.quality_alert_forward(a, source="tools/data_sentinel.py")
            _audit.quality_alert_rotate()
        except Exception:
            pass
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-gaps", action="store_true",
                    help="JSON 中保留全部缺口票明细（默认只详列最差 %d 只）" % GAP_DETAIL_TOP)
    ap.add_argument("--out", default=None,
                    help="输出 JSON 路径（默认 data/quality_report.json；F6 验收期可指向临时文件）")
    a = ap.parse_args()
    t0 = time.time()
    today = time.strftime("%Y-%m-%d")
    names = load_names_and_excludes()
    excludes = set(map(str, getattr(C, "DATA_EXCLUDE_CODES", []) or []))

    conn = ro_conn(C.DB_FILE)
    cur = conn.cursor()

    # 官方日历：近 60 个交易日（用于一致性校验）
    cal_days_recent = tc.trading_days("2026-05-01", today)[-60:]

    print("① 扫描 kline(day) 缺口与复权跳变（流式单遍）...", flush=True)
    gap_res, gaps, jumps, first_dates, emp_idx = check_kline_gaps(cur, cal_days_recent)
    print("  扫描 %d 票 / %d 经验交易日；有缺口票 %d；volume=0 停牌行 %d"
          % (gap_res["codes_scanned"], gap_res["empirical_trading_days"],
             gap_res["codes_with_gaps"], gap_res["volume0_suspension_rows"]), flush=True)

    print("② 复权异常候选（%s 起，Phase20 口径）..." % QFQ_SCAN_FROM, flush=True)
    qfq_res = check_qfq_jumps(jumps, names, excludes, first_dates, emp_idx)
    print("  候选 %d（high=%d，未入排除清单 %d）"
          % (qfq_res["candidates_total"], qfq_res["high_severity"],
             qfq_res["new_not_in_excludes"]), flush=True)

    print("③ 股票名单新鲜度...", flush=True)
    list_res = check_stock_list(cur)
    print("  名单 %d ↔ 库内 %d；名单有但无K线 %d；有K线但不在名单 %d"
          % (list_res.get("list_count", 0), list_res.get("kline_code_count", 0),
             list_res.get("in_list_no_kline_count", 0),
             list_res.get("has_kline_not_in_list_count", 0)), flush=True)

    print("④ min5 覆盖度...", flush=True)
    min5_res = check_min5()
    print("  行数 %s / 最新 %s" % (min5_res.get("total_rows"), min5_res.get("latest_date")), flush=True)

    print("⑤ ml_pred 新鲜度...", flush=True)
    ml_res = check_ml_pred(today)
    print("  最后预测 %s（距今 %s 个交易日，阈值 %d）"
          % (ml_res["last_pred_date"], ml_res["trading_days_since_last"],
             ML_PRED_STALE_TDAYS), flush=True)

    # ★ Phase76 补项：ml_scores 覆盖率
    print("⑤b ml_scores 覆盖率...", flush=True)
    ms_res = check_ml_scores_coverage(today)
    if ms_res.get("file_exists"):
        print("  scores=%s / pool=%s（覆盖率 %s），信号日 %s%s"
              % (ms_res.get("scores"), ms_res.get("pool_size"),
                 ("%d%%" % (ms_res["coverage"] * 100)) if ms_res.get("coverage") is not None else "—",
                 ms_res.get("signal_date") or "—",
                 ("；⚠ " + ms_res["warn"]) if ms_res.get("warn") else ""), flush=True)
    else:
        print("  ml_scores.json 不存在", flush=True)

    print("⑥ global_kline 积累...", flush=True)
    gk_res = check_global_kline(today)
    print("  序列 %d 条；美元指数(DINIW) %s" % (
        len(gk_res["series"]),
        ("rows=%(rows)d 最新=%(latest)s" % gk_res["usdx_index"]) if gk_res["usdx_index"] else "缺失",
        ), flush=True)

    print("⑦ 历史长度分桶 + 票池同构 KPI...", flush=True)
    hist_res = check_history_buckets(cur)
    print("  全池 %d 只：%s；达标(≥%d根) %d 只，同构交集 %.1f%%"
          % (hist_res["total_codes"], hist_res["buckets"], hist_res["min_bars_required"],
             hist_res["pool_long_enough"], hist_res["homogeneity_kpi"]["intersection_pct"] * 100),
          flush=True)

    print("⑧ 停牌占位行非物理值...", flush=True)
    susp_res = check_suspension_placeholders(cur)
    print("  非物理量行 %d 条（OHLC四值全等占比 %s）"
          % (susp_res["non_physical_rows"], susp_res["all_ohlc_equal_ratio"]), flush=True)

    print("⑨ 指数完整性...", flush=True)
    idx_res = check_index_integrity(cur)
    print("  指数 latest: %s" % {k: v["latest"] for k, v in idx_res["indices"].items()}, flush=True)

    print("⑩ 当日缺票（相对上一交易日）...", flush=True)
    miss_res = check_missing_today(cur, today)
    if miss_res.get("verdict") == "SKIP":
        print("  不足两个交易日，跳过", flush=True)
    else:
        print("  缺 %d 只（昨前日有=%d / 更早=%d）"
              % (miss_res["missing_count"], miss_res["last_eq_prev_count"],
                 miss_res["last_older_count"]), flush=True)

    conn.close()

    if not a.full_gaps:
        gap_res.pop("gaps_all_counts", None)

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "31",
        "tool": "tools/data_sentinel.py",
        "mode": "read-only（SQLite mode=ro；不改库不进config）",
        "today": today,
        "excluded_codes_currently_in_config": len(excludes),
        "kline_gaps": gap_res,
        "qfq_jump_candidates": qfq_res,
        "stock_list_sync": list_res,
        "min5_coverage": min5_res,
        "ml_pred_freshness": ml_res,
        "ml_scores_coverage": ms_res,
        "global_kline": gk_res,
        "history_buckets": hist_res,
        "suspension_placeholders": susp_res,
        "index_integrity": idx_res,
        "missing_today": miss_res,
        "elapsed_sec": round(time.time() - t0, 1),
        "how_to_exclude": (
            "异常候选是否进入 config.DATA_EXCLUDE_CODES 由人工决定。既有流程见 "
            "docs/operations.md「数据与备份/年度维护」与 docs/reports/clean_data_baseline.md"
            "（人工确认 → 在 app/config.py DATA_EXCLUDE_CODES 增补代码并注明原因与日期 → "
            "回测池/ML训练/情绪历史自动生效）。本工具不做任何自动写入。"),
    }
    _out_path = a.out or OUT_JSON
    with open(_out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    # ★ D1：关键项判定 → quality_alert.jsonl + audit 转发（双轨打通）
    try:
        _emit_quality_alerts(report)
    except Exception:
        pass

    # ---- 终端摘要 ----
    print("\n========== 数据质量哨兵摘要 ==========")
    print("① K线缺口   : 有缺口票 %d / %d（最差: %s）" % (
        gap_res["codes_with_gaps"], gap_res["codes_scanned"],
        "; ".join("%s缺%d天" % (g["code"], g["missing"])
                  for g in gap_res["gaps_top"][:5]) or "无"))
    n_mismatch = len(gap_res["calendar_mismatch"])
    print("   日历一致性: %s" % (
        "%d 处不一致（详见报告）" % n_mismatch if n_mismatch else "一致"))
    print("② 复权异常  : 候选 %d（high %d / 未入排除清单 %d）" % (
        qfq_res["candidates_total"], qfq_res["high_severity"],
        qfq_res["new_not_in_excludes"]))
    print("③ 名单同步  : 名单有但无K线 %d；有K线但名单消失 %d" % (
        list_res.get("in_list_no_kline_count", 0),
        list_res.get("has_kline_not_in_list_count", 0)))
    print("④ min5      : %s 行，最新 %s%s" % (
        min5_res.get("total_rows", "?"), min5_res.get("latest_date", "?"),
        "" if min5_res.get("db_exists") else "（库不存在）"))
    print("⑤ ml_pred   : 最后预测 %s（距今 %s 个交易日)%s" % (
        ml_res["last_pred_date"], ml_res["trading_days_since_last"],
        " ⚠ 该跑滚动了" if ml_res["stale"] else ""))
    print("⑥ global_kline: %d 条序列%s" % (
        len(gk_res["series"]),
        ("；美元指数 thin=%s 最新=%s" % (gk_res["usdx_index"]["thin"],
                                        gk_res["usdx_index"]["latest"])) if gk_res["usdx_index"] else "；美元指数缺失!"))
    print("已写出 %s（耗时 %.1fs）" % (OUT_JSON, time.time() - t0))


if __name__ == "__main__":
    main()
