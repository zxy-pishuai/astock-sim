# -*- coding: utf-8 -*-
"""I1 业绩预告历史回填（2019-01-01 ~ 今，按季度分页拉取入库）。
用法：py -3.13 tools/backfill_earnings_history.py [--force]
特性：
  - 先备份 earnings 表 → data/backups/earnings_pre_backfill_<ts>.db
  - 季度分页 + 限速(≥0.5s/请求) + 指数退避(3 次) + 断点续跑
    （data/earnings_backfill_state.json 记已完成季度）
  - INSERT OR REPLACE 幂等，重跑不重复
  - 只写 earnings 表，绝不动 kline
"""
import json
import os
import sqlite3
import sys
import time
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app import config as C        # noqa: E402
from app import earnings as ea     # noqa: E402

STATE_FILE = os.path.join(C.DATA_DIR, "earnings_backfill_state.json")
RATE_SLEEP = 0.5      # 每请求间隔（秒）
MAX_RETRY = 3         # 失败重试次数（指数退避）
PAGE_SIZE = 500


def quarters(start="2019-01-01", end=None):
    """季度区间列表 [(since, end), ...]，闭区间。"""
    end = end or date.today().isoformat()
    ys, ms = map(int, start.split("-")[:2])
    ye, me = map(int, end.split("-")[:2])
    out = []
    y, m = ys, ((ms - 1) // 3) * 3 + 1
    while (y, m) <= (ye, ((me - 1) // 3) * 3 + 1):
        q_end_m = m + 2
        q_end_y = y
        if q_end_m > 12:
            q_end_m -= 12
            q_end_y += 1
        import calendar
        last_day = calendar.monthrange(q_end_y, q_end_m)[1]
        out.append(("%04d-%02d-01" % (y, m),
                    "%04d-%02d-%02d" % (q_end_y, q_end_m, last_day)))
        m += 3
        if m > 12:
            m = 1
            y += 1
    return out


def backup_earnings():
    """备份 earnings 表到独立 db（回填可回滚）。返回备份路径与行数。"""
    os.makedirs(os.path.join(C.DATA_DIR, "backups"), exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(C.DATA_DIR, "backups", "earnings_pre_backfill_%s.db" % ts)
    conn = ea._conn(readonly=True)
    rows = conn.execute("SELECT code,notice_date,report_date,payload FROM earnings").fetchall()
    conn.close()
    b = sqlite3.connect(bak)
    b.execute("""CREATE TABLE IF NOT EXISTS earnings(
        code TEXT, notice_date TEXT, report_date TEXT,
        payload TEXT, PRIMARY KEY(code, notice_date))""")
    b.executemany(
        "INSERT OR REPLACE INTO earnings(code,notice_date,report_date,payload) VALUES(?,?,?,?)",
        rows)
    b.commit()
    b.close()
    return bak, len(rows)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"completed": [], "rows": 0, "updated_at": None}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def fetch_quarter_retry(since, end):
    """季度区间分页拉取（限速+退避）；失败重试 MAX_RETRY 次后抛错。"""
    flt = "(NOTICE_DATE>='%s')(NOTICE_DATE<='%s')" % (since, end)
    out = []
    page = 1
    while True:
        for attempt in range(MAX_RETRY + 1):
            try:
                rows = ea._dc("RPT_PUBLIC_OP_NEWPREDICT", page_size=PAGE_SIZE,
                              filter_sql=flt, page_number=page)
                break
            except Exception as e:
                if attempt == MAX_RETRY:
                    raise
                time.sleep(RATE_SLEEP * (2 ** attempt))
        if not rows:
            break
        for r in rows:
            item = ea._normalize(r)
            if item:
                out.append(item)
        if len(rows) < PAGE_SIZE:
            break
        page += 1
        time.sleep(RATE_SLEEP)
    return out


def main():
    force = "--force" in sys.argv
    st = load_state()
    qs = quarters()
    todo = [q for q in qs if force or q[0] not in st["completed"]]
    # 预估
    est = len(todo) * 3 * (RATE_SLEEP + 0.2)  # 平均每季 3 页
    print("回填计划：2019-01-01 ~ 今，%d 个季度，已完成 %d，本次 %d 个，预估 %.0fs"
          % (len(qs), len(st["completed"]), len(todo), est))
    if not todo:
        print("无待回填季度（--force 可强制重跑全部）")
        return
    # 备份（幂等：仅首次或 --force 时）
    bak, n0 = backup_earnings()
    print("备份 earnings 表 → %s（%d 行）" % (bak, n0))
    total = st.get("rows", 0)
    t0 = time.time()
    for i, (since, end) in enumerate(todo, 1):
        try:
            rows = fetch_quarter_retry(since, end)
        except Exception as e:
            print("  ✗ %s~%s 拉取失败：%s（断点已保存，可重跑续传）" % (since, end, str(e)[:100]))
            save_state(st)
            sys.exit(2)
        if rows:
            ea._save(rows)
            total += len(rows)
        st["completed"].append(since)
        st["rows"] = total
        st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
        el = time.time() - t0
        print("  [%d/%d] %s~%s → %d 条（累计 %d，已用 %.0fs）"
              % (i, len(todo), since, end, len(rows), total, el))
    print("完成：累计入库 %d 行，总耗时 %.0fs" % (total, time.time() - t0))


if __name__ == "__main__":
    main()
