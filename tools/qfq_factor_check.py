#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X3 | qfq factor date-level recheck (research / read-only, NO apply)

Purpose:
    Recompute qfq close from akshare real hfq_factor + raw (unadjusted) close via
        qfq(t) = raw_close(t) * hfq_factor(t) / hfq_factor(latest)
    then compare day-by-day against in-DB kline(day) close, and emit the list of
    dates where |deviation| > 0.5% (focus zones: 2024-12-30 +/-10d, 2019 segment).

Net discipline (sole outbound block):
    concurrency=1, request interval >= 1s, per-request timeout 30s, total <= 120.
    Raw akshare responses archived to tmp/x3/raw/{code}_factor.csv / _raw.csv.

Red lines:
    - read-only on market.db (file:data/market.db?mode=ro&immutable=1), never writes DB
    - never touches app/; no git write commands
    - write surface: data/qfq_factor_sample.json + tmp/x3/

Usage:
    python tools/qfq_factor_check.py            # full sample (5 polluted + 3 control + 1 observe)
    python tools/qfq_factor_check.py --codes 600188        # specific codes
    python tools/qfq_factor_check.py --test-one 600188     # probe 1 code, keep raw dumps
    python tools/qfq_factor_check.py --out data/qfq_factor_sample.json
"""
import argparse
import bisect
import json
import os
import socket
import sqlite3
import sys
import time
import traceback

import akshare as ak

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_URI = "file:data/market.db?mode=ro&immutable=1"
DEV_THRESHOLD_PCT = 0.5
FETCH_INTERVAL_S = 1.0
TIMEOUT_S = 30
RAW_DIR = os.path.join(ROOT, "tmp", "x3", "raw")

POLLUTED = [
    {"code": "001400", "name": "江顺科技", "role": "polluted", "note": "batchA已修复(2025-08-14集群/2026-05-28)"},
    {"code": "001359", "name": "平安电工", "role": "polluted", "note": "batchA已修复(2025-08-14集群)"},
    {"code": "300628", "name": "亿联网络", "role": "polluted", "note": "batchA被拦,2019-07残留(2019段)"},
    {"code": "002831", "name": "裕同科技", "role": "polluted", "note": "batchA已修复(2025-09/2026-06)"},
    {"code": "600188", "name": "兖矿能源", "role": "polluted", "note": "batchA被拦,2019段残留"},
]
CONTROL = [
    {"code": "600519", "name": "贵州茅台", "role": "control", "note": "2019+ max jump 9.5%, 未入册"},
    {"code": "600036", "name": "招商银行", "role": "control", "note": "2019+ max jump 10.0%, 未入册"},
    {"code": "601318", "name": "中国平安", "role": "control", "note": "2019+ max jump 10.0%, 未入册"},
]
OBSERVE = [
    {"code": "000333", "name": "美的集团", "role": "observe", "note": "2024-12-30前后 -15.1% 未入册低幅度污染观察"},
]

FOCUS_ZONES = [
    ("2024-12-30±10", lambda d: "2024-12-20" <= d <= "2025-01-10"),
    ("2019段", lambda d: d <= "2019-12-31"),
]


def norm_date(d):
    """akshare date may be datetime/str; normalize to YYYY-MM-DD"""
    s = str(d)[:10]
    if len(s) == 10 and s[4] == "-":
        return s
    return s.replace("/", "-")


def to_ak_symbol(code):
    """DB 6-digit code -> akshare symbol with exchange prefix (6->sh, 0/3->sz)"""
    code = code.strip()
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    raise ValueError("unsupported code format: %r" % code)


def fetch_hfq_factor(code):
    """akshare: stock_zh_a_daily(adjust='hfq-factor') -> date+hfq_factor change table (full history)"""
    sym = to_ak_symbol(code)
    df = ak.stock_zh_a_daily(symbol=sym, adjust="hfq-factor")
    df = df.copy()
    df["date"] = df["date"].map(norm_date)
    return df[["date", "hfq_factor"]].sort_values("date").reset_index(drop=True)


def fetch_raw_daily(code):
    """akshare: stock_zh_a_daily(adjust='') -> unadjusted daily OHLCV (full history)"""
    sym = to_ak_symbol(code)
    df = ak.stock_zh_a_daily(symbol=sym, adjust="")
    df = df.copy()
    df["date"] = df["date"].map(norm_date)
    return df[["date", "close"]].sort_values("date").reset_index(drop=True)


def db_kline(code):
    conn = sqlite3.connect(DB_URI, uri=True, timeout=60)
    try:
        cur = conn.cursor()
        cur.execute("SELECT date, close FROM kline WHERE code=? AND period='day' ORDER BY date", (code,))
        rows = cur.fetchall()
        return rows
    finally:
        conn.close()


def recompute_qfq(raw_df, factor_df):
    """qfq(t) = raw_close(t) * hfq_factor(t) / hfq_factor(latest)"""
    if factor_df.empty or raw_df.empty:
        return {}
    latest_factor = float(factor_df["hfq_factor"].iloc[-1])
    dates = list(factor_df["date"])
    factors = list(factor_df["hfq_factor"])
    out = {}
    f_dates = [d for d in dates]
    for _, row in raw_df.iterrows():
        d = row["date"]
        # latest factor change date <= d
        idx = bisect.bisect_right(f_dates, d) - 1
        if idx < 0:
            continue
        f = float(factors[idx])
        out[d] = float(row["close"]) * f / latest_factor
    return out


def classify_zone(date):
    for name, fn in FOCUS_ZONES:
        if fn(date):
            return name
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", default=None, help="override code list (6-digit)")
    ap.add_argument("--test-one", default=None, help="probe a single code then exit")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "qfq_factor_sample.json"))
    ap.add_argument("--no-observe", action="store_true", help="skip 000333 observe stock")
    args = ap.parse_args()

    socket.setdefaulttimeout(TIMEOUT_S)  # applied to all akshare internal requests
    os.makedirs(RAW_DIR, exist_ok=True)

    if args.test_one:
        sample = [{"code": args.test_one, "name": "?", "role": "probe", "note": "test-one"}]
    elif args.codes:
        sample = [{"code": c, "name": "?", "role": "explicit", "note": ""} for c in args.codes]
    else:
        sample = POLLUTED + CONTROL + ([] if args.no_observe else OBSERVE)

    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "method": "qfq(t) = raw_close(t) * hfq_factor(t) / hfq_factor(latest)",
        "source": "akshare stock_zh_a_daily(adjust='hfq-factor') + adjust=''",
        "db_uri": "file:data/market.db?mode=ro&immutable=1",
        "threshold_pct": DEV_THRESHOLD_PCT,
        "request_stats": {"total": 0, "failures": 0, "fetch_interval_s": FETCH_INTERVAL_S},
        "stocks": {},
    }

    for meta in sample:
        code = meta["code"]
        rec = dict(meta)
        print("[X3] %s %s (%s) ..." % (code, meta.get("name", ""), meta.get("role", "")), flush=True)
        rec["fetch_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["errors"] = []
        try:
            # --- 1. hfq factor (change-date table) ---
            try:
                fdf = fetch_hfq_factor(code)
                fdf.to_csv(os.path.join(RAW_DIR, "%s_factor.csv" % code), index=False, encoding="utf-8")
                result["request_stats"]["total"] += 1
                time.sleep(FETCH_INTERVAL_S)
            except Exception as e:
                result["request_stats"]["failures"] += 1
                rec["errors"].append("hfq_factor: %s: %s" % (type(e).__name__, str(e)[:200]))
                print("  ! hfq_factor failed: %s %s" % (type(e).__name__, str(e)[:120]), flush=True)
                fdf = None

            # --- 2. raw unadjusted daily ---
            try:
                rdf = fetch_raw_daily(code)
                rdf.to_csv(os.path.join(RAW_DIR, "%s_raw.csv" % code), index=False, encoding="utf-8")
                result["request_stats"]["total"] += 1
                time.sleep(FETCH_INTERVAL_S)
            except Exception as e:
                result["request_stats"]["failures"] += 1
                rec["errors"].append("raw_daily: %s: %s" % (type(e).__name__, str(e)[:200]))
                print("  ! raw_daily failed: %s %s" % (type(e).__name__, str(e)[:120]), flush=True)
                rdf = None

            if fdf is None or rdf is None or fdf.empty or rdf.empty:
                rec["status"] = "incomplete"
                result["stocks"][code] = rec
                continue

            # --- 3. recompute qfq close ---
            qfq_map = recompute_qfq(rdf, fdf)
            rec["ak_raw_rows"] = int(len(rdf))
            rec["ak_factor_rows"] = int(len(fdf))
            rec["ak_factor_latest"] = float(fdf["hfq_factor"].iloc[-1])
            rec["ak_last_factor_date"] = str(fdf["date"].iloc[-1])

            # --- 4. compare against DB ---
            db_rows = db_kline(code)
            rec["db_rows"] = len(db_rows)
            rec["db_min_date"] = db_rows[0][0] if db_rows else None
            rec["db_max_date"] = db_rows[-1][0] if db_rows else None

            dev_days = []
            max_abs = (0.0, None)
            both = 0
            raw_close = {d: float(r) for d, r in zip(rdf["date"], rdf["close"])}
            db_is_raw_cnt = 0
            for d, db_close in db_rows:
                rc = qfq_map.get(d)
                rw = raw_close.get(d)
                if rc is None:
                    continue
                both += 1
                if rc == 0:
                    continue
                dev = (float(db_close) - rc) / rc * 100.0
                if abs(dev) > abs(max_abs[0]):
                    max_abs = (dev, d)
                dev_raw = None
                if rw:
                    dev_raw = (float(db_close) - rw) / rw * 100.0
                    if abs(dev_raw) < 0.5:
                        db_is_raw_cnt += 1
                if abs(dev) > DEV_THRESHOLD_PCT:
                    dev_days.append({
                        "date": d,
                        "db_close": round(float(db_close), 4),
                        "recalc_close": round(rc, 4),
                        "dev_pct": round(dev, 4),
                        "raw_close": round(rw, 4) if rw else None,
                        "dev_vs_raw_pct": round(dev_raw, 4) if dev_raw is not None else None,
                        "zone": classify_zone(d),
                    })

            rec["compared_days"] = both
            rec["db_matches_raw_days"] = db_is_raw_cnt
            rec["db_unadjusted_ratio"] = round(db_is_raw_cnt / max(both, 1), 4)
            rec["dev_days_gt_0.5pct"] = len(dev_days)
            rec["max_abs_dev_pct"] = round(max_abs[0], 4)
            rec["max_abs_dev_date"] = max_abs[1]
            # focus-zone summaries
            for zone, fn in FOCUS_ZONES:
                zdays = [x for x in dev_days if x["zone"] == zone]
                if zdays:
                    zmax = max(zdays, key=lambda x: abs(x["dev_pct"]))
                    rec["zone_%s" % zone] = {
                        "count": len(zdays),
                        "max_abs_dev_pct": zmax["dev_pct"],
                        "sample": zdays[:10],
                    }
                else:
                    rec["zone_%s" % zone] = {"count": 0, "max_abs_dev_pct": None, "sample": []}
            rec["dev_dates"] = dev_days
            rec["status"] = "ok"
            result["stocks"][code] = rec
            print("  dev>0.5%%: %d/%d days, max_abs=%.2f%% @ %s"
                  % (len(dev_days), both, max_abs[0], max_abs[1]), flush=True)
        except Exception as e:
            result["request_stats"]["failures"] += 1
            rec["status"] = "error"
            rec["errors"].append("main: %s: %s" % (type(e).__name__, str(e)[:300]))
            rec["traceback"] = traceback.format_exc()[-500:]
            result["stocks"][code] = rec
            print("  !! main error: %s %s" % (type(e).__name__, str(e)[:150]), flush=True)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("[X3] written %s" % args.out)
    print("[X3] request_stats=%s" % json.dumps(result["request_stats"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
