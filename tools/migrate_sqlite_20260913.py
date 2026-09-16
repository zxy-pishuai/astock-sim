# -*- coding: utf-8 -*-
"""★ J3（2026-09-13）：删除冗余索引迁移脚本。

  DROP idx_kline_cp（market.db，被 PK(code,period,date) 前缀覆盖，dbstat 195.3MB）
  DROP idx_kmin5_c（min5.db，被 PK(code,date) 覆盖）

安全要求（任务书红线）：
  ① 执行前导出两库索引清单 + dbstat 体积 → docs/reports/j3_index_before.md
  ② DROP 前后各跑一遍 5 类查询基准（改后每类不慢于改前）
  ③ 可回滚：备份文件 + 内嵌 CREATE INDEX 复原语句
  ④ 09:00-15:30 交易时段直接拒绝运行

用法：python tools/migrate_sqlite_20260913.py
"""
import datetime
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

M = os.path.join(ROOT, "data", "market.db")
MN = os.path.join(ROOT, "data", "min5.db")
BACKUP_DIR = os.path.join(ROOT, "tmp", "j3", "backup")
BENCH_FILE = os.path.join(ROOT, "tmp", "j3", "bench_before.json")
BENCH_AFTER = os.path.join(ROOT, "tmp", "j3", "bench_after.json")
INDEX_BEFORE = os.path.join(ROOT, "docs", "reports", "j3_index_before.md")

RESTORE_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_kline_cp ON kline(code, period);",
    "CREATE INDEX IF NOT EXISTS idx_kmin5_c ON kline_min5(code);",
]

BENCH_QUERIES = [
    ("单只全历史 600519",
     "SELECT * FROM kline WHERE code=? AND period='day' ORDER BY date",
     ("600519",), M),
    ("单只全历史 000001",
     "SELECT * FROM kline WHERE code=? AND period='day' ORDER BY date",
     ("000001",), M),
    ("全市场单日 2026-09-11",
     "SELECT code, close, volume, amount FROM kline WHERE period='day' AND date=?",
     ("2026-09-11",), M),
    ("区间扫描 30日",
     "SELECT * FROM kline WHERE period='day' AND date BETWEEN ? AND ?",
     ("2026-08-11", "2026-09-11"), M),
    ("min5 按日期范围",
     "SELECT * FROM kline_min5 WHERE code=? AND date>=?",
     ("600519", "2026-09-01 09:30:00"), MN),
    ("覆盖率统计 30日",
     "SELECT date, COUNT(*) FROM kline WHERE period='day' AND date>=? GROUP BY date",
     ("2026-08-11",), M),
]


def uri(p):
    return Path(p).as_uri()


def ro(p, timeout=30):
    return sqlite3.connect("%s?mode=ro" % uri(p), uri=True, timeout=timeout)


def _in_trading_window():
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 <= hm <= 15 * 60 + 30


def index_snapshot(path):
    con = ro(path)
    try:
        rows = con.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' "
            "ORDER BY tbl_name, name").fetchall()
        out = []
        for name, tbl, sql in rows:
            try:
                n = con.execute(
                    "SELECT COUNT(*) FROM pragma_index_info(?)", (name,)).fetchone()[0]
            except Exception:
                n = -1
            out.append({"name": name, "table": tbl, "cols": n,
                        "sql": sql[:120] if sql else "(auto)"})
        return out
    finally:
        con.close()


def dbstat(path):
    con = ro(path)
    try:
        try:
            rows = con.execute(
                "SELECT name, SUM(pgsize) FROM dbstat WHERE aggregate=TRUE "
                "GROUP BY name ORDER BY 2 DESC").fetchall()
            return [(r[0], r[1]) for r in rows]
        except Exception as e:
            return [("dbstat 不可用", str(e))]
    finally:
        con.close()


def file_size(path):
    return os.path.getsize(path)


def bench_all(n=4):
    results = []
    for label, sql, params, db in BENCH_QUERIES:
        times = []
        con = ro(db)
        try:
            for _ in range(n):
                t0 = time.time()
                con.execute(sql, params).fetchall()
                times.append((time.time() - t0) * 1000)
        finally:
            con.close()
        times.sort()
        results.append({"label": label, "med_ms": round(times[n // 2], 1),
                        "min_ms": round(times[0], 1), "max_ms": round(times[-1], 1)})
    return results


def export_before():
    os.makedirs(os.path.dirname(INDEX_BEFORE), exist_ok=True)
    lines = [
        "# J3 索引迁移前置快照（2026-09-13，迁移脚本自动生成）",
        "",
        "> 由 tools/migrate_sqlite_20260913.py 在执行 DROP 前导出。",
        "> 回滚语句：",
        "> ```sql",
    ]
    lines += ["> " + s for s in RESTORE_STATEMENTS]
    lines += ["> ```", ""]
    for name, path in [("market.db", M), ("min5.db", MN)]:
        lines.append("## %s（%.2f GB）" % (name, file_size(path) / 1073741824))
        lines.append("")
        lines.append("| 索引 | 表 | 列数 | SQL |")
        lines.append("|---|---|---|---|")
        for ix in index_snapshot(path):
            lines.append("| %s | %s | %s | %s |" % (
                ix["name"], ix["table"], ix["cols"],
                (ix["sql"] or "").replace("|", "\\|")))
        lines.append("")
        lines.append("### dbstat 体积（top 15）")
        lines.append("")
        lines.append("| 对象 | 字节 | MB |")
        lines.append("|---|---|---|")
        for name2, b in dbstat(path)[:15]:
            if isinstance(b, str):
                lines.append("| %s | %s | - |" % (name2, b))
            else:
                lines.append("| %s | %d | %.1f |" % (name2, b, b / 1048576))
        lines.append("")
    with open(INDEX_BEFORE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("索引前置快照已导出: %s" % INDEX_BEFORE)


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    out = []
    for src, tag in [(M, "market"), (MN, "min5")]:
        dst = os.path.join(BACKUP_DIR, "%s.db.pre_j3" % tag)
        if os.path.exists(dst):
            print("备份已存在（跳过）: %s" % dst)
            continue
        t0 = time.time()
        print("备份 %s -> %s ..." % (src, dst))
        shutil.copy2(src, dst)
        out.append((src, dst, time.time() - t0))
    for src, dst, dt in out:
        print("  OK %.1fs %s (%.2f GB)" % (
            dt, dst, file_size(dst) / 1073741824))


def drop_indexes():
    con = sqlite3.connect(M, timeout=30)
    try:
        t0 = time.time()
        con.execute("DROP INDEX IF EXISTS idx_kline_cp")
        con.commit()
        print("DROP idx_kline_cp OK (%.1fs)" % (time.time() - t0))
    finally:
        con.close()
    con = sqlite3.connect(MN, timeout=30)
    try:
        t0 = time.time()
        con.execute("DROP INDEX IF EXISTS idx_kmin5_c")
        con.commit()
        print("DROP idx_kmin5_c OK (%.1fs)" % (time.time() - t0))
    finally:
        con.close()


def main():
    if _in_trading_window():
        print("拒绝执行：当前处于交易时段（09:00-15:30），为避免影响生产写入，"
              "请于收盘后运行。")
        sys.exit(3)

    print("=== J3 索引迁移 %s ===" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("改前文件大小: market %.2f GB / min5 %.2f GB" % (
        file_size(M) / 1073741824, file_size(MN) / 1073741824))

    # ① 前置快照
    export_before()

    # 备份
    backup()

    # ② 改前基准
    print("\n=== 改前基准 ===")
    before = bench_all()
    for r in before:
        print("  %-24s med=%.1fms" % (r["label"], r["med_ms"]))
    with open(BENCH_FILE, "w", encoding="utf-8") as f:
        json.dump(before, f, ensure_ascii=False, indent=1)

    # ③ DROP
    print("\n=== DROP 冗余索引 ===")
    drop_indexes()

    # ④ 改后基准
    print("\n=== 改后基准 ===")
    after = bench_all()
    for r in after:
        print("  %-24s med=%.1fms" % (r["label"], r["med_ms"]))
    with open(BENCH_AFTER, "w", encoding="utf-8") as f:
        json.dump(after, f, ensure_ascii=False, indent=1)

    print("\n=== 对比 ===")
    bmap = {r["label"]: r["med_ms"] for r in before}
    for r in after:
        b = bmap.get(r["label"])
        if b:
            delta = (r["med_ms"] - b) / b * 100
            flag = "⚠️ 退化" if r["med_ms"] > b * 1.1 else "OK"
            print("  %-24s %6.1f -> %6.1f ms  (%+.0f%%) %s" % (
                r["label"], b, r["med_ms"], delta, flag))
    print("\n改后文件大小: market %.2f GB / min5 %.2f GB" % (
        file_size(M) / 1073741824, file_size(MN) / 1073741824))
    print("\n回滚：")
    for s in RESTORE_STATEMENTS:
        print("  %s" % s)
    print("  或直接恢复备份: tmp/j3/backup/*.pre_j3")
    print("\n注意：DROP 后索引页标记 free（dbstat 显示索引消失），物理文件缩小需"
          " VACUUM（交由用户决策，见报告 §page_size/VACUUM 评估）。")


if __name__ == "__main__":
    main()
