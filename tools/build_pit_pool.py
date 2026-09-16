# -*- coding: utf-8 -*-
"""★ Phase64：PIT 股票池与幸存者偏差量化

现行 bt_pool 是"2026-08 成交额 top500"的静态池——用它回测 2019~2024 存在双重
前视：幸存者偏差（退市/ST 前的票不在池内）+ 当日成交额选股（把未来赢家放进过去）。
本工具构建**分窗口时点池（PIT）**做对照：
  - 资格线：东财 clist f26 上市日期 ≤ 窗口起始日 − 120 自然日（外部源实测可用）
  - 排序线：窗口开始前最后 60 个可得交易日的平均成交额 top500（只用窗口前信息）
    ★ 例外：2019-20 窗口本地库无前置历史 → 用窗口内首 60 日成交额排序，
    含轻微前视、会高估该窗 PIT 表现——报告与 JSON 均已标注
  - 数据局限预声明：未引入退市清单（无免费稳定源），PIT 池仍只含现存上市股票，
    幸存者偏差方向为"低估"，量化结果是偏差下界

用法:
  python tools/build_pit_pool.py --build      # 拉上市日期 + 构建分窗 PIT 池
  python tools/build_pit_pool.py --run        # PIT 池 vs 现行 top500 四窗对照
  python tools/build_pit_pool.py --report     # 打印对照摘要
输出: data/pit_pools.json / data/bt_pit_compare.json
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config as C            # noqa: E402

WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-18"],
]
WINDOW_TAGS = ["2019-20", "2021-22", "2023-24", "近1年"]
POOL_SIZE = 500
SEASONING_DAYS = 120          # 上市未满 N 个自然日不入池
TRAILING_DAYS = 60            # 前置成交额排序窗口（交易日）
LISTING_CACHE = os.path.join(C.DATA_DIR, "listing_dates.json")
POOLS_JSON = os.path.join(C.DATA_DIR, "pit_pools.json")
CMP_JSON = os.path.join(C.DATA_DIR, "bt_pit_compare.json")
_EM_HOSTS = ["https://push2.eastmoney.com", "https://45.push2.eastmoney.com",
             "https://21.push2.eastmoney.com"]


_PAGE_CACHE = os.path.join(C.DATA_DIR, "listing_pages_cache.json")


def _em_clist_page(page):
    path = ("/api/qt/clist/get?pn=%d&pz=200&po=1&np=1&fltt=2&invt=2&fid=f20"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
            "&fields=f12,f13,f14,f20,f26" % page)
    # ★ 断点缓存：每页成功即落盘（限流中断后重跑不重复抓取）
    cache = {}
    if os.path.exists(_PAGE_CACHE):
        try:
            with open(_PAGE_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    if str(page) in cache:
        d = cache[str(page)]
        return int(d.get("total") or 0), d.get("diff") or []
    last = None
    for a in range(8):
        host = _EM_HOSTS[a % len(_EM_HOSTS)]
        try:
            req = urllib.request.Request(host + path, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                js = json.loads(r.read().decode("utf-8", "replace"))
            d = js.get("data") or {}
            diff = d.get("diff")
            if isinstance(diff, list) and diff:
                cache[str(page)] = {"total": d.get("total"), "diff": diff}
                with open(_PAGE_CACHE, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
                return int(d.get("total") or 0), diff
            last = RuntimeError("empty diff")
        except Exception as e:
            last = e
        time.sleep(2 + a * 2)
    raise RuntimeError("eastmoney clist p%d 失败: %s" % (page, last))


def fetch_listing_dates():
    """{code: {"name","ipo"(YYYY-MM-DD)}} —— 东财 clist f26 上市日期，全市场"""
    out = {}
    page, total = 1, None
    while True:
        total, diff = _em_clist_page(page)
        for it in diff:
            ipo = str(it.get("f26") or "")
            if len(ipo) == 8 and ipo.isdigit():
                ipo = "%s-%s-%s" % (ipo[:4], ipo[4:6], ipo[6:8])
            else:
                continue
            out[str(it.get("f12"))] = {"name": it.get("f14", ""),
                                       "ipo": ipo,
                                       "mktcap": it.get("f20")}
        print("  page %d: 累计 %d/%s" % (page, len(out), total), flush=True)
        if total and len(out) >= total:
            break
        if not diff:
            break
        page += 1
        time.sleep(1.5)
    return out


def cmd_build():
    t0 = time.time()
    listing = {}
    degraded = False
    try:
        print("拉取上市日期（东财 clist，分页）...", flush=True)
        listing = fetch_listing_dates()
        print("  %d 只（含 IPO 日期）→ %s" % (len(listing), LISTING_CACHE), flush=True)
    except Exception as e:
        # ★ 降级路径（任务书允许）：东财不可得时用库内首日作伪上市日。
        #   对 2019 后上市的新股精确（恰是近期偏差主要来源）；更早上市无法区分。
        degraded = True
        print("  东财失败(%s)，降级：库内首日=伪上市日" % str(e)[:60], flush=True)
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=30)
    cur = conn.cursor()
    if degraded:
        cur.execute("SELECT code, MIN(date) FROM kline WHERE period='day' "
                    "AND length(code)=6 GROUP BY code")
        listing = {r[0]: {"name": r[0], "ipo": r[1]} for r in cur}
    cur.execute("SELECT DISTINCT date FROM kline WHERE period='day' "
                "AND length(code)=6 ORDER BY date")
    with open(LISTING_CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "count": len(listing),
                   "degraded": degraded, "listing": listing},
                  f, ensure_ascii=False)
    print("  %d 只（%s）→ %s" % (
        len(listing), "降级=库内首日伪上市" if degraded else "含东财 IPO 日期",
        LISTING_CACHE), flush=True)

    cur.execute("SELECT DISTINCT date FROM kline WHERE period='day' "
                "AND length(code)=6 ORDER BY date")
    all_days = [r[0] for r in cur]
    day_idx = {d: i for i, d in enumerate(all_days)}
    # ★ D2：成交额读取下界从硬编码 2019-01-01 改为最早窗口(2019-20)的
    #   尾随排序窗起点（窗口起 −120 自然日）。此前 w0 的"窗前最后 60 个交易日"
    #   永远落在查询范围之外，have_pre 恒假、被迫用窗内前视排序——P64 报告
    #   §局限 3 与本任务书指向的唯一缺窗根因。对其他窗口无影响（它们的
    #   尾随窗都在 2019 之后）。
    import datetime as _dt
    _w0_lo = (_dt.datetime.strptime(WINDOWS[0][0], "%Y-%m-%d")
              - _dt.timedelta(days=SEASONING_DAYS + 40)).strftime("%Y-%m-%d")
    cur.execute("SELECT code, date, amount FROM kline WHERE period='day' "
                "AND length(code)=6 AND date>=? AND amount IS NOT NULL "
                "ORDER BY code, date", (_w0_lo,))
    amt = {}
    for code, d, a in cur:
        if a and a > 0:
            amt.setdefault(code, []).append((day_idx[d], a))

    pools = {}
    for wi, (w0, _w1) in enumerate(WINDOWS):
        cutoff = (_dt.datetime.strptime(w0, "%Y-%m-%d")
                  - _dt.timedelta(days=SEASONING_DAYS)).strftime("%Y-%m-%d")
        # 资格线：上市早于 窗口起−120自然日；且在库内有该窗前的成交额历史
        eligible = []
        end_i = day_idx.get(w0)
        if end_i is None:
            cand = [i for i, d in enumerate(all_days) if d > w0]
            end_i = cand[0] - 1 if cand else len(all_days) - 1
        start_i = max(0, end_i - TRAILING_DAYS)   # 窗口前最后 60 个可得交易日
        have_pre = end_i - start_i >= TRAILING_DAYS // 2   # 是否真有前置历史
        for code, meta in listing.items():
            if meta["ipo"] > cutoff:
                continue
            arr = amt.get(code)
            if not arr:
                continue
            vals = [a for i, a in arr if start_i <= i < end_i]
            if len(vals) >= TRAILING_DAYS // 2:
                eligible.append((code, sum(vals) / len(vals)))
        eligible.sort(key=lambda x: -x[1])
        pool = [c for c, _ in eligible[:POOL_SIZE]]
        tag = WINDOW_TAGS[wi]
        pools[tag] = {
            "codes": pool,
            "eligible_count": len(eligible),
            "ranking_basis": ("pre_window_60d_amount" if have_pre
                              else "in_window_first_60d_amount(含轻微前视,见报告§局限)"),
            "have_pre_window_history": have_pre,
        }
        print("  [%s] 资格=%d → PIT 池 %d 只（排序依据=%s）" % (
            tag, len(eligible), len(pool), pools[tag]["ranking_basis"]), flush=True)
    conn.close()

    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        cur_top = json.load(f)["codes"][:500]
    for tag, p in pools.items():
        ov = len(set(p["codes"]) & set(cur_top))
        p["overlap_with_current_top500"] = ov
        print("  [%s] 与现行 top500 重叠 %d/500" % (tag, ov), flush=True)
    doc = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "seasoning_days": SEASONING_DAYS, "trailing_days": TRAILING_DAYS,
           "pool_size": POOL_SIZE,
           "degraded_pseudo_ipo": degraded,
           "limitation": "未引入退市清单（无免费稳定源）：PIT 池仍仅含现存上市股票，"
                         "幸存者偏差被低估；本结果为偏差下界"
                         + ("。本次为降级模式：伪上市日=库内首日。★D2 后该口径已修复——"
                            "2019 前历史日K回补后，库内首日≈真实上市日（mootdx 全史），"
                            "seasoning 线对 2019 前后上市的票均可用；个别长期停牌/"
                            "复权拉取失败的票首日可能偏晚（见 backfill_pre2019_failed.json）"
                            if degraded else ""),
           "pools": pools}
    with open(POOLS_JSON, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("已写出 %s（%.0fs）" % (POOLS_JSON, time.time() - t0), flush=True)


# ---------------- --run：PIT 池 vs 现行 top500 四窗对照 ----------------
def _run_one(task):
    variant, strategy, widx = task
    import io
    from app import config as C
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    if variant == "pit":
        with open(POOLS_JSON, encoding="utf-8") as f:
            codes = json.load(f)["pools"][WINDOW_TAGS[widx]]["codes"]
    else:
        with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
            codes = json.load(f)["codes"][:500]
    names = {c: c for c in codes}
    t0 = time.time()
    orig_quotes = eng.df.fetch_quotes
    orig_mom = getattr(C, "BOARD_MOMENTUM_MIN", None)
    eng.df.fetch_quotes = lambda cs: {}
    C.BOARD_MOMENTUM_MIN = 7.0     # 冻结口径（与 P48+49 一致）
    try:
        params = ({"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30}
                  if strategy == "score" else
                  {"buy_threshold": 40, "max_positions": 2, "position_pct": 0.25})
        params.update({"zt_eco_gate": False, "dd_gate": False, "slippage": 0.001})
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, strategy, params)
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_quotes
        if orig_mom is not None:
            C.BOARD_MOMENTUM_MIN = orig_mom
    row = {"variant": variant, "strategy": strategy, "widx": widx,
           "pool_size": len(codes),
           "total_return": r.get("total_return"),
           "max_drawdown": r.get("max_drawdown"),
           "annual_return": r.get("annual_return"),
           "win_rate": r.get("win_rate"),
           "trade_count": len(bt.trades),
           "elapsed": round(time.time() - t0, 1)}
    print("  [%s/%s %s] ret=%.4f dd=%.4f trades=%d (%.0fs)" % (
        variant, strategy, WINDOW_TAGS[widx], row["total_return"] or 0,
        row["max_drawdown"] or 0, row["trade_count"], row["elapsed"]), flush=True)
    return row


def cmd_run():
    with open(POOLS_JSON, encoding="utf-8") as f:
        pools_doc = json.load(f)["pools"]
    tasks = [(v, s, w) for v in ("top500", "pit")
             for s in ("score", "board") for w in range(len(WINDOWS))
             if v != "pit" or pools_doc[WINDOW_TAGS[w]]["codes"]]
    runs = []
    if os.path.exists(CMP_JSON):
        with open(CMP_JSON, encoding="utf-8") as f:
            runs = json.load(f).get("runs", [])
    done = {(r["variant"], r["strategy"], r["widx"]) for r in runs}
    todo = [t for t in tasks if t not in done]
    print("待跑 %d/%d" % (len(todo), len(tasks)), flush=True)

    def dump():
        order = {t: i for i, t in enumerate(tasks)}
        runs.sort(key=lambda r: order.get(
            (r["variant"], r["strategy"], r["widx"]), 99))
        with open(CMP_JSON, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "phase": "64", "runs": runs}, f, ensure_ascii=False, indent=1)

    if todo:
        ex = ProcessPoolExecutor(max_workers=min(8, len(todo)),
                                 initializer=_init_worker)
        futs = {ex.submit(_run_task, t): t for t in todo}
        try:
            for fut in as_completed(futs):
                try:
                    runs.append(fut.result())
                except Exception as e:
                    print("任务异常 %s: %s" % (futs[fut], e), flush=True)
                dump()
                print("进度 %d/%d" % (len(runs), len(tasks)), flush=True)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        done = {(r["variant"], r["strategy"], r["widx"]) for r in runs}
        for t in tasks:
            if t not in done:
                print("补跑 %s" % (t,), flush=True)
                _init_worker()
                runs.append(_run_task(t))
                dump()
    print("完成：%s（%d 组）" % (CMP_JSON, len(runs)), flush=True)


codes = names = None


def _init_worker():
    pass   # 池数据由 _run_one 自行加载（PIT 池按窗口不同）


def _run_task(task):
    # 单参透传（_run_one 自行解包 variant/strategy/widx）
    return _run_one(task)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.build:
        cmd_build()
    if a.run:
        cmd_run()
    if a.report or (not a.build and not a.run):
        with open(CMP_JSON, encoding="utf-8") as f:
            runs = json.load(f)["runs"]
        grid = {(r["variant"], r["strategy"], r["widx"]): r for r in runs}
        for wi, tag in enumerate(WINDOW_TAGS):
            print("[%s]" % tag)
            for s in ("score", "board"):
                t = grid.get(("top500", s, wi))
                p = grid.get(("pit", s, wi))
                if t and p:
                    print("  %-5s top500=%.2f%% pit=%.2f%% Δ=%+.2fpp" % (
                        s, (t["total_return"] or 0) * 100,
                        (p["total_return"] or 0) * 100,
                        ((p["total_return"] or 0) - (t["total_return"] or 0)) * 100))
