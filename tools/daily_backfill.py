# -*- coding: utf-8 -*-
"""E1（pack18）一次性日K全库回补工具。

背景（docs/reports/audit_20260903.md §2 P0-1）：update_daily 默认 limit=300 只更新
股票列表前 300 只，全库 2438 票仅 379 票有 2026-09-03 日K。本工具做一次性回补。

设计：
  - stale-first：先查每票 kline.max(date)，最陈旧先更新（无记录最旧优先）。
  - 批 200、批间停 1s、workers 并发取数（默认 8，同 updater 既有 PARALLEL_WORKERS）。
  - 断点续跑：进度写 tmp/e1/backfill_state.json；重跑自动从 pending 续起。
  - 宇宙 = kline 已有票 ∪ get_stock_list()（main 全A）；KPI=fresh/原 kline 票数。
  - 网络只走 updater/datafeed 既有行情通道（TDX→腾讯→新浪），复用
    app.updater._fetch_and_save（幂等 upsert，不 DELETE 不改表结构）。
  - 达成覆盖目标后重建今日快照（既有 data_snapshot.create_snapshot(force=True) 路径）。

用法：
  py -3.13 tools/daily_backfill.py [--budget 1500] [--batch 200] [--pause 1.0]
      [--workers 8] [--days 60] [--target 2300] [--no-snapshot]
退出码：0=达成目标；1=预算耗尽未达标。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

STATE = os.path.join(ROOT, "tmp", "e1", "backfill_state.json")
LOG = os.path.join(ROOT, "tmp", "e1", "backfill.log")


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="日K全库一次性回补（E1）")
    ap.add_argument("--budget", type=int, default=1500, help="总时间预算（秒），默认 1500")
    ap.add_argument("--batch", type=int, default=200, help="单批票数，默认 200")
    ap.add_argument("--pause", type=float, default=1.0, help="批间停顿（秒），默认 1")
    ap.add_argument("--workers", type=int, default=8, help="并发取数，默认 8")
    ap.add_argument("--days", type=int, default=60, help="补拉天数，默认 60")
    ap.add_argument("--target", type=int, default=2300, help="覆盖目标（原 kline 宇宙内 fresh 数）")
    ap.add_argument("--no-snapshot", action="store_true", help="不重建今日快照")
    ap.add_argument("--reset", action="store_true", help="忽略已有断点状态，重新开始")
    a = ap.parse_args()

    from app import updater as U
    from app import datafeed as df

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    t0 = time.time()

    st = {}
    if not a.reset and os.path.isfile(STATE):
        try:
            st = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            st = {}

    if st.get("pending"):
        pending = st["pending"]
        universe_kline = st.get("universe_kline")
        log("断点续跑：pending=%d（续上次宇宙 kline=%s）" % (len(pending), universe_kline))
    else:
        md = U._daily_max_dates()
        universe_kline = sorted(md.keys())                      # 原 kline 宇宙（KPI 分母）
        lst = [c for c, _, _ in df.get_stock_list()]
        universe = sorted(set(universe_kline) | set(lst))       # 全库宇宙（含列表新增）
        kset = set(universe_kline)
        target_day = U._target_day()
        # 陈旧度排序：①已在库的陈旧票优先（KPI 关键），②未入库列表新增次之；同档 max 旧在前
        pending = sorted(universe, key=lambda c: ((c not in kset), md.get(c, ""), c))
        st = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "days": a.days,
              "batch": a.batch, "workers": a.workers,
              "universe_n": len(universe), "universe_kline_n": len(universe_kline),
              "universe_kline": universe_kline, "target_day": target_day}
        log("新建回补：宇宙 %d 票（kline %d ∪ list 新增），目标日=%s" % (
            len(universe), len(universe_kline), target_day))

    updated = st.get("updated", 0)
    errors = st.get("errors", 0)
    n_done = st.get("n_done", 0)

    def kpi():
        """KPI=fresh(原 kline 宇宙内 max>=target) / 原 kline 数；另附系统级 ratio。"""
        md = U._daily_max_dates()
        tgt = U._target_day()
        kset = set(st.get("universe_kline") or md.keys())
        fr = sum(1 for c in kset if md.get(c, "") >= tgt)
        sys_cov = U._daily_coverage() or {}
        return {"fresh_kline": fr, "total_kline": len(kset),
                "ratio_kline": round(fr / len(kset), 4) if kset else 0.0,
                "sys_fresh": sys_cov.get("fresh"), "sys_total": sys_cov.get("total"),
                "sys_ratio": sys_cov.get("ratio"), "target_day": tgt}

    while pending and (time.time() - t0) < a.budget:
        b = pending[:a.batch]
        pending = pending[a.batch:]
        r = U._fetch_and_save(b, a.days, workers=a.workers)
        updated += r["updated"]
        errors += r["errors"]
        n_done += len(b)
        tgt = U._target_day()
        fr = U._daily_max_dates_for(b)
        still = [c for c in b if fr.get(c, "") < tgt]
        pending += still
        kv = kpi()
        st.update({"pending": pending, "updated": updated, "errors": errors,
                   "n_done": n_done, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "kpi": kv})
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        log("批 %d 票: 更新%d 追平%d 待追平%d 剩%d | KPI fresh=%s/%s ratio=%s"
            "（系统 %s/%s） | 已用 %ds" % (
                len(b), r["updated"], len(b) - len(still), len(still), len(pending),
                kv["fresh_kline"], kv["total_kline"], kv["ratio_kline"],
                kv["sys_fresh"], kv["sys_total"], int(time.time() - t0)))
        if kv["fresh_kline"] >= a.target:
            log("达成覆盖目标 KPI fresh>=%d，回补完成" % a.target)
            break
        time.sleep(a.pause)

    kv = kpi()
    ok = bool(kv["fresh_kline"] >= a.target)
    st.update({"pending": pending, "updated": updated, "errors": errors,
               "n_done": n_done, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "elapsed_s": round(time.time() - t0, 1), "ok": ok, "kpi": kv})
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    log("回补结束: 更新%d 错误%d KPI fresh=%s/%s ratio=%s 耗时%.0fs → %s" % (
        updated, errors, kv["fresh_kline"], kv["total_kline"], kv["ratio_kline"],
        time.time() - t0, "OK" if ok else "BUDGET-EXHAUSTED"))

    if ok and not a.no_snapshot:
        try:
            from app import data_snapshot as ds
            p, created = ds.create_snapshot(force=True)   # 既有代码路径，force 覆盖带病快照
            import sqlite3
            sc = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=20)
            snap_fresh = sc.execute(
                "SELECT COUNT(DISTINCT code) FROM kline WHERE period='day' AND date>=?",
                (kv["target_day"],)).fetchone()[0]
            sc.close()
            log("快照重建: %s (%s) 快照内 %s 日新鲜" % (p, "新建" if created else "存在", snap_fresh))
            st["snapshot"] = {"path": p, "created": created, "snap_fresh": snap_fresh}
        except Exception as e:
            log("快照重建失败: %s" % e)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
