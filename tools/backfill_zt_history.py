# -*- coding: utf-8 -*-
"""★ K4（2026-09-16）：情绪周期历史表补数（P1-④）。

根因（本脚本头注，证据见 docs/reports/k4_zt_history_backfill.md §1）：
  - qg_zt_full / qg_sentiment_history 的唯一写入者是 tools/build_sentiment_history.py
    （手动工具），2026-08-21 之后从未被调用（未接入 updater 日更链路）→ 两表停更。
  - 取数源链路正常（limit_pool 已由 limitup.py 更新至 20260915）→ 是"写入路径从未被调用"。

本脚本：用 kline day 全历史反推每日涨停池（口径与 build_sentiment_history.py 完全一致），
回填 qg_zt_full 至库内最近交易日；据此重建 qg_sentiment_history 单行快照。
limit_pool（20260815~20260915 约 3 周真值）作正确性门对表。
严格 PIT：每行只用当日及以前 kline。

用法:
  python tools/backfill_zt_history.py [--dry] [--start 2019-01-01]
"""
import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = os.path.join(BASE, "data", "market.db")
TABLE = "qg_sentiment_history"
FULL_TABLE = "qg_zt_full"
BACKUP_DIR = os.path.join(BASE, "tmp", "k4")

# 涨停检测阈值（与 build_sentiment_history.py 一致：0.5/0.5 留容差）
LIMIT_MAIN = 9.2    # 主板 60/00
LIMIT_GEM = 19.2    # 创业/科创 30/68


def _ro_conn():
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=15)


def load_kline_all(start="2019-01-01"):
    """一次读全市场 day 日K（code, date, close）。"""
    conn = _ro_conn()
    try:
        rows = conn.execute(
            "SELECT code, date, close FROM kline WHERE period='day' AND date>=? "
            "AND close IS NOT NULL ORDER BY code, date", (start,)).fetchall()
    finally:
        conn.close()
    by_code = {}
    for code, d, c in rows:
        by_code.setdefault(code, []).append((d, c))
    return by_code


def detect_zt_dates(code, seq):
    """基于 (date, close) 检测涨停日 → {date: pct}（PIT：只用当日及以前）。"""
    lim = LIMIT_GEM if code.startswith(("30", "68")) else LIMIT_MAIN
    prev = None
    out = {}
    for d, c in seq:
        if prev is not None and prev > 0:
            pct = (c - prev) / prev * 100
            if pct >= lim:
                out[d] = round(pct, 2)
        prev = c
    return out


def _phase_of(zt_count, max_days, yzt):
    """阶段规则（与 build_sentiment_history.py 内联规则一致）。"""
    if zt_count < 25:
        return "冰点"
    if zt_count >= 60 and max_days >= 5:
        return "高潮"
    if yzt is not None and yzt < -0.02 and max_days <= 3:
        return "退潮"
    return "发酵"


def build_series_eco(eco_pool, eco_series, start="2019-01-01"):
    """★ K11（2026-09-16）：以 eco 权威口径重算 qg_zt_full 全字段。

    输入：
      eco_pool:   app.zt_ecosystem.daily_zt_pool() → {date: set(code)}
                  （前缀 60/00/30/68 + 排除 DATA_EXCLUDE_CODES/ST + 上市满 60 bar
                    + engine.limit_prices 精确板价 close 封板）
      eco_series: app.zt_ecosystem.build(force=True)["series"]
                  （max_streak / yzt_ret_mean 等，与 eco_pool 同源）
    输出：series 列表 [{date, zt_count, prev_zt_premium, sentiment_score,
                       max_days, phase}]——zt_count 直接取 eco_pool（同源自证），
          max_days ← eco max_streak，prev_zt_premium ← eco yzt_ret_mean
          （全量等权 vs 旧口径"前 100 只采样"，口径统一），
          sentiment_score/phase 沿用 _phase_of 规则（新输入重算）。
    严格 PIT：eco 两端均为单遍顺序扫描，每行只用当日及以前数据。"""
    dates = sorted(d for d in eco_pool.keys() if d >= start)
    eco_by_date = {s["date"]: s for s in eco_series}
    series = []
    for d in dates:
        zt = eco_pool.get(d, set())
        es = eco_by_date.get(d, {})
        prev_premium = es.get("yzt_ret_mean")
        max_days = int(es.get("max_streak", 0) or 0)
        score = round(min(100, len(zt) * 1.2), 1)
        series.append({
            "date": d,
            "zt_count": len(zt),
            "max_days": max_days,
            "prev_zt_premium": prev_premium,
            "sentiment_score": score,
            "phase": _phase_of(len(zt), max_days, prev_premium),
        })
    return series


def build_series(by_code, start="2019-01-01"):
    """与 build_sentiment_history.main 前半段一致：逐日 zt_count/max_days/prev_zt_premium/
    sentiment_score/phase。返回 (series, zt_by_day, day_max_days)。"""
    try:
        from app import config as C
        _ex = set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])
        if _ex:
            by_code = {c: v for c, v in by_code.items() if c not in _ex}
    except Exception:
        pass

    zt_by_day = {}
    for code, seq in by_code.items():
        for d, pct in detect_zt_dates(code, seq).items():
            zt_by_day.setdefault(d, {})[code] = pct
    dates = sorted(zt_by_day.keys())

    # prev_zt_premium：昨日涨停股今日收益均值（全市场等权近似，PIT）
    prev_zt_codes = set()
    series = []
    for d in dates:
        zt = zt_by_day.get(d, {})
        prev_premium = None
        if prev_zt_codes:
            rets = []
            for c in list(prev_zt_codes)[:100]:
                seq = by_code.get(c)
                if seq is None:
                    continue
                prev_c = None
                cur_c = None
                for dd, cc in seq:
                    if dd < d:
                        prev_c = cc
                    elif dd == d:
                        cur_c = cc
                        break
                if prev_c and cur_c and prev_c > 0:
                    rets.append((cur_c - prev_c) / prev_c)
            if rets:
                prev_premium = round(sum(rets) / len(rets), 4)
        score = round(min(100, len(zt) * 1.2), 1)
        series.append({"date": d, "zt_count": len(zt),
                       "prev_zt_premium": prev_premium, "sentiment_score": score})
        prev_zt_codes = set(zt.keys())

    # 连板高度递推（PIT）
    streak = {}
    day_max_days = {}
    for d in dates:
        cur_zt = zt_by_day.get(d, {})
        mx = 0
        new_streak = {}
        for c in cur_zt:
            s = streak.get(c, 0) + 1
            new_streak[c] = s
            mx = max(mx, s)
        day_max_days[d] = mx
        streak = new_streak

    for s in series:
        d = s["date"]
        s["max_days"] = day_max_days.get(d, 0)
        s["phase"] = _phase_of(s["zt_count"], s["max_days"], s["prev_zt_premium"])
    return series, zt_by_day, day_max_days


def load_limit_pool():
    """limit_pool kind='zt' → {date(YYYY-MM-DD): set(code)}。"""
    conn = _ro_conn()
    try:
        rows = conn.execute(
            "SELECT date, code FROM limit_pool WHERE kind='zt'").fetchall()
    finally:
        conn.close()
    out = {}
    for d, code in rows:
        ds = "%s-%s-%s" % (d[:4], d[4:6], d[6:]) if len(d) == 8 else d
        out.setdefault(ds, set()).add(code)
    return out


def check_limit_pool(zt_by_day, lp):
    """正确性门（集合级）：limit_pool 是"曾涨停滚动池"——表列 date 是池快照日
    （周末/盘前复制上一池），payload 无涨停当日字段，逐日 join 口径不成立
    （实测 8/15~8/17 三天为同一份 8/14 池）。
    因此只做集合级校验：重叠期 limit_pool 收录的 (code) 中，多大比例能在
    kline 反推的同窗涨停集合中找到（池语义=该票近期涨停过）。
    返回 (命中率, 明细)。
    """
    overlap_dates = sorted(set(zt_by_day) & set(lp))
    if not overlap_dates:
        return None, {}
    lo, hi = overlap_dates[0], overlap_dates[-1]
    # 窗口放宽：重叠期 + 前 4 个涨停日（覆盖池快照滞后：8/15~8/17 池=8/14 涨停池）
    all_dates = sorted(zt_by_day.keys())
    try:
        i0 = all_dates.index(lo)
        w0 = all_dates[max(0, i0 - 4)]
    except ValueError:
        w0 = lo
    k_zt = set()
    for d in all_dates:
        if w0 <= d <= hi:
            k_zt |= set(zt_by_day.get(d, {}))
    # limit_pool 收录集合（窗内）
    p_codes = set()
    for d in overlap_dates:
        p_codes |= set(lp.get(d, {}))
    hit = len(p_codes & k_zt)
    miss = sorted(p_codes - k_zt)
    # miss 分类：DATA_EXCLUDE 排除清单（预期 miss） vs 真差异
    try:
        from app import config as C
        _ex = set(str(x) for x in (getattr(C, "DATA_EXCLUDE_CODES", None) or []))
    except Exception:
        _ex = set()
    miss_ex = [c for c in miss if c in _ex]
    miss_real = [c for c in miss if c not in _ex]
    ratio = round(hit / len(p_codes), 4) if p_codes else None
    return ratio, {"overlap_days": len(overlap_dates),
                   "pool_codes": len(p_codes), "kline_codes": len(k_zt),
                   "hit": hit, "miss": len(miss),
                   "miss_excluded": len(miss_ex),
                   "miss_real": len(miss_real),
                   "miss_samples": miss_real[:15]}


def verify_vs_old(series, old_rows):
    """历史复现对账：回填结果 vs 旧 qg_zt_full（8/21 前）逐日逐字段一致率。
    旧表由 build_sentiment_history.py 同口径生成 → 同口径复现应完全一致。"""
    old = {r[0]: r[1:] for r in old_rows}
    agree = total = 0
    diffs = []
    for s in series:
        if s["date"] not in old:
            continue
        o = old[s["date"]]
        # 旧表列: (zt_count, max_days, prev_zt_premium, sentiment_score, phase)
        exp = (s["zt_count"], s.get("max_days", 0),
               s.get("prev_zt_premium"), s["sentiment_score"], s["phase"])
        # prev_zt_premium None vs 0.0 视为一致
        oo = (o[0], o[1], o[2] if o[2] is not None else None,
              o[3], o[4])
        same = (oo[0] == exp[0] and oo[1] == exp[1]
                and (oo[2] == exp[2] or (oo[2] is None and exp[2] is None)
                     or (oo[2] is not None and exp[2] is not None
                         and abs(oo[2] - exp[2]) < 1e-6))
                and abs(oo[3] - exp[3]) < 1e-6 and oo[4] == exp[4])
        total += 1
        if same:
            agree += 1
        elif len(diffs) < 10:
            diffs.append((s["date"], oo, exp))
    ratio = round(agree / total, 4) if total else None
    return ratio, {"old_days": total, "agree_days": agree,
                   "diff_samples": diffs}


def main(start="2019-01-01", dry=False, caliber="legacy"):
    global BACKUP_DIR
    if caliber == "eco":
        BACKUP_DIR = os.path.join(BASE, "tmp", "k11")
    t0 = time.time()
    print("读取全市场日K（%s 起）..." % start, flush=True)
    by_code = load_kline_all(start)
    codes = list(by_code.keys())
    print("  股票数 %d" % len(codes), flush=True)

    if caliber == "eco":
        # ★ K11：权威口径 = app.zt_ecosystem（实盘闸门读取方）。涨停池 + 生态序列
        #   全部复用 eco 实现，杜绝第二套涨停判定逻辑。
        print("★ eco 权威口径：daily_zt_pool() + build(force=True)...", flush=True)
        from app import zt_ecosystem as eco
        eco_pool = eco.daily_zt_pool()
        eco_data = eco.build(force=True)
        eco_series = eco_data["series"]
        series = build_series_eco(eco_pool, eco_series, start)
        zt_by_day = eco_pool
        day_max_days = {s["date"]: s["max_days"] for s in series}
        dates = sorted(zt_by_day.keys())
        dates = [d for d in dates if d >= start]
        print("  eco 涨停日 %d 天（%s ~ %s）" % (
            len(dates), dates[0] if dates else "?", dates[-1] if dates else "?"), flush=True)
    else:
        print("反推每日涨停池...", flush=True)
        series, zt_by_day, day_max_days = build_series(by_code, start)
        dates = sorted(zt_by_day.keys())
        print("  涨停日 %d 天（%s ~ %s）" % (
            len(dates), dates[0] if dates else "?", dates[-1] if dates else "?"), flush=True)

    # 正确性门：limit_pool 集合级对表 + 旧表历史复现对账
    print("limit_pool 对表（集合级，池快照语义）...", flush=True)
    lp = load_limit_pool()
    ratio, g = check_limit_pool(zt_by_day, lp)
    if ratio is None:
        print("  [门] 无重叠期，跳过对表", flush=True)
    else:
        print("  [门] 重叠 %d 天：池收录 %d 码，kline 反推 %d 码，命中 %d = %.4f"
              "（miss=%d：排除清单 %d + 真差异 %d）" % (
                  g.get("overlap_days", 0), g.get("pool_codes", 0),
                  g.get("kline_codes", 0), g.get("hit", 0), ratio,
                  g.get("miss", 0), g.get("miss_excluded", 0),
                  g.get("miss_real", 0)), flush=True)
        for x in g.get("miss_samples", [])[:6]:
            print("    真差异样例: %s" % x, flush=True)

    # 旧表数据代差披露（8/21 前旧表 vs 当前 kline 反推——kline 已回补变更，不强求一致）
    print("旧 qg_zt_full 数据代差披露...", flush=True)
    conn0 = sqlite3.connect(DB, timeout=60)
    try:
        old_rows = conn0.execute(
            "SELECT date, zt_count, max_days, prev_zt_premium, sentiment_score, phase "
            "FROM %s ORDER BY date" % FULL_TABLE).fetchall()
    finally:
        conn0.close()
    vratio, vg = verify_vs_old(series, old_rows)
    print("  [代差] 旧表 %d 天，逐日全字段一致 %d 天 = %.4f（kline 8/21 后回补/数据源变更所致）" % (
        vg.get("old_days", 0), vg.get("agree_days", 0), vratio or 0), flush=True)
    for x in vg.get("diff_samples", [])[:6]:
        print("    差异样例: %s old=%s new=%s" % (x[0], x[1], x[2]), flush=True)

    # 落库
    if not dry:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        conn = sqlite3.connect(DB, timeout=60)
        try:
            # 1) 备份现有 qg_zt_full
            bak = os.path.join(BACKUP_DIR, "qg_zt_full_before_k11.csv")
            rows = conn.execute(
                "SELECT date, zt_count, max_days, prev_zt_premium, sentiment_score, phase "
                "FROM %s ORDER BY date" % FULL_TABLE).fetchall()
            with open(bak, "w", encoding="utf-8") as f:
                f.write("date,zt_count,max_days,prev_zt_premium,sentiment_score,phase\n")
                for r in rows:
                    f.write("%s,%s,%s,%s,%s,%s\n" % r)
            print("  备份现有 %s %d 行 → %s" % (FULL_TABLE, len(rows), bak), flush=True)
            # 1b) phase 变更清单（新旧对照，供人工复核）
            old_phase = {r[0]: r[5] for r in rows}
            old_zt = {r[0]: r[1] for r in rows}
            ph_path = os.path.join(BACKUP_DIR, "phase_changes_k11.csv")
            with open(ph_path, "w", encoding="utf-8") as f:
                f.write("date,old_zt,new_zt,old_phase,new_phase,changed\n")
                n_chg = 0
                for s in series:
                    d = s["date"]
                    op = old_phase.get(d)
                    np_ = s["phase"]
                    oz = old_zt.get(d)
                    changed = "Y" if (op is not None and op != np_) else "N"
                    if changed == "Y":
                        n_chg += 1
                    f.write("%s,%s,%s,%s,%s,%s\n" % (d, oz, s["zt_count"], op, np_, changed))
            print("  phase 变更清单 → %s（变更 %d 天）" % (ph_path, n_chg), flush=True)
            # 2) DROP+重建（与 build_sentiment_history.py 同款标准操作）
            conn.execute("DROP TABLE IF EXISTS %s" % FULL_TABLE)
            conn.execute(
                "CREATE TABLE %s("
                "date TEXT PRIMARY KEY, zt_count INT, max_days INT, "
                "prev_zt_premium REAL, sentiment_score REAL, phase TEXT)" % FULL_TABLE)
            conn.executemany(
                "INSERT OR REPLACE INTO %s(date,zt_count,max_days,prev_zt_premium,"
                "sentiment_score,phase) VALUES(?,?,?,?,?,?)" % FULL_TABLE,
                [(s["date"], s["zt_count"], s.get("max_days", 0),
                  s.get("prev_zt_premium"), s["sentiment_score"], s["phase"])
                 for s in series])
            # 3) 重建 qg_sentiment_history 单行快照
            payload = {
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "start": start, "end": dates[-1] if dates else "",
                "dates": len(dates),
                "phase_stats": {},
                "methods": "backfill_zt_history.py (K11): 权威口径=app.zt_ecosystem "
                           "(limit_prices 精确板价 close 封板 + 前缀/ST/新股60bar 过滤); "
                           "max_days←eco max_streak, prev_zt_premium←eco yzt_ret_mean; "
                           "sentiment_score/phase 沿用原规则; 严格 PIT",
                "gate": {"caliber": caliber,
                         "limit_pool_ratio": ratio,
                         "limit_pool": g,
                         "old_table_delta_ratio": vratio,
                         "old_table_delta": vg},
            }
            conn.execute(
                "DROP TABLE IF EXISTS %s" % TABLE)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS %s("
                "period TEXT PRIMARY KEY, zt_count REAL, prev_zt_premium REAL, "
                "sentiment_score REAL, phase TEXT, date_count INT, payload TEXT)" % TABLE)
            conn.execute(
                "INSERT OR REPLACE INTO %s(period,zt_count,prev_zt_premium,sentiment_score,"
                "phase,date_count,payload) VALUES('2019_%s',?,?,?,?,?,?)" % (
                    TABLE, dates[-1] if dates else ""),
                (0.0, None, 0.0, "全周期", len(dates),
                 json.dumps(payload, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
        print("已落库 %s（逐日 %d 行）+ %s（快照）" % (
            FULL_TABLE, len(series), TABLE), flush=True)
    else:
        print("[dry] 未写库", flush=True)
    print("总耗时 %.1fs" % (time.time() - t0), flush=True)
    return ratio, len(dates), dates[-1] if dates else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--caliber", default="legacy", choices=["legacy", "eco"],
                    help="legacy=旧口径(close环比pct阈值)；eco=K11权威口径(精确板价+ST/新股过滤)")
    a = ap.parse_args()
    main(start=a.start, dry=a.dry, caliber=a.caliber)
