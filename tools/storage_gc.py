# -*- coding: utf-8 -*-
"""B3（2026-09-13）存储治理工具 —— 默认只盘点不删除。

子命令（全部先输出 JSON 留痕，--apply 才动文件）：
  --inventory          只读盘点 data/：大小/mtime/引用标记(可删|需确认|必须保留)
                       → data/storage_inventory.json + 控制台摘要
  --slim [--tag YYYY-MM-DD]
                       为 6~30 天档全库快照生成 market.slim.db（只留 kline day，
                       ATTACH + CREATE TABLE AS SELECT + 重建索引），不动源库。
                       不带 --tag = 对当前所有"超全库保留期且无 slim"的快照补生成。
  --kpi                每周存储 KPI：data/ 总大小 / 快照数 / 最大单文件 / 7 日增幅
                       → audit(storage_weekly_kpi) + 控制台（历史基线 data/storage_kpi_history.json）
  --dry-run（默认）     列出待删清单（_archive_pending_del 二期删、.bak_* 陈旧备份、
                       >30 天快照），打印清理命令；**不执行任何删除**。
  --apply --yes        用户书面确认后执行待删清单（--yes 缺失则拒绝）。

红线：默认只盘点不删除；删除动作必须有用户书面确认（--apply --yes）；
禁碰 data/audit、data/app.lock、tmp/watchdog.log；不重启服务。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
DATA = os.path.join(BASE, "data")
SNAP = os.path.join(DATA, "snapshots")
STAGE = os.path.join(SNAP, "_archive_pending_del")
INV_JSON = os.path.join(DATA, "storage_inventory.json")
KPI_JSON = os.path.join(DATA, "storage_kpi_history.json")
TODAY = time.strftime("%Y-%m-%d")

# 引用判定（静态规则 + 全仓 grep 结果固化，见 B3 报告 §引用盘点）
PROTECT = {  # 必须保留：热库 / 审计链 / 台账 / 回测基准
    "market.db", "min5.db", "audit/", "archive_index.json",
    "bt_", "qfq_", "quality_", "update.lock", "stale_first_calib.json",
    "daily_skip_codes.json", "cyq_", "storage_inventory.json",
    "storage_kpi_history.json", "close_update_cron.log", "app.lock",
}
CONFIRM_PREFIX = ("market.db.bak", "snapshots/", "backups/", "keypack/")


def _bytes(n):
    if n >= 2**30:
        return "%.2f GB" % (n / 2**30)
    if n >= 2**20:
        return "%.1f MB" % (n / 2**20)
    return "%.0f KB" % (n / 2**10)


def _age_days_tag(tag):
    try:
        from datetime import date
        y, mo, d = (int(x) for x in tag.split("-"))
        return (date.today() - date(y, mo, d)).days
    except Exception:
        return 999


def classify(rel):
    """rel: data/ 下相对路径。返回 (label, reason)。"""
    base = os.path.basename(rel)
    for p in PROTECT:
        if rel.startswith(p) or base.startswith(p.rstrip("/")):
            return "must_keep", "代码/回测/审计引用"
    if rel.startswith("snapshots/_archive_pending_del"):
        return "to_confirm", "归档暂存区（09-08 已过 7 天观察期，二期删待确认）"
    if base.startswith("market.db.bak"):
        return "to_confirm", "F 块/手工备份（验收已过，保留期限待确认）"
    if rel.startswith("snapshots/"):
        tag = rel.split("/")[1] if rel.count("/") > 1 else ""
        age = _age_days_tag(tag)
        if age <= 5:
            return "must_keep", "最近 5 天全库快照（回测 PIT 基准）"
        if age <= 30:
            return "to_confirm", "6~30 天快照（应转 slim，保留/删除待确认）"
        return "to_confirm", ">30 天快照（删除档，待确认）"
    if rel.startswith("keypack/"):
        return "to_confirm", "密钥包（每日轮转，保留策略待确认）"
    if rel.startswith("backups/"):
        return "to_confirm", "备份目录（待确认）"
    return "must_keep", "其他数据文件"


def inventory():
    out = []
    total = 0
    for dirpath, _dirs, files in os.walk(DATA):
        for fn in files:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, DATA).replace("\\", "/")
            try:
                st = os.stat(p)
                size, mtime = st.st_size, st.st_mtime
            except Exception:
                continue
            total += size
            label, reason = classify(rel)
            out.append({"path": rel, "size": size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
                        "label": label, "reason": reason})
    out.sort(key=lambda x: -x["size"])
    inv = {"generated_at": TODAY, "total_bytes": total,
           "total_gb": round(total / 2**30, 2), "items": out}
    with open(INV_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(inv, f, ensure_ascii=False, indent=1)
    # 摘要
    by = {}
    for it in out:
        by.setdefault(it["label"], [0, 0])
        by[it["label"]][0] += 1
        by[it["label"]][1] += it["size"]
    print("== data/ 盘点（%s）总 %s ==" % (TODAY, _bytes(total)))
    for k, (n, s) in sorted(by.items(), key=lambda kv: -kv[1][1]):
        print("  %-12s %4d 文件  %s" % (k, n, _bytes(s)))
    print("清单已写:", INV_JSON)
    return inv


def _slim_one(tag, full_path):
    slim_path = os.path.join(os.path.dirname(full_path), "market.slim.db")
    # 裁行窗口：config.SNAPSHOT_SLIM_YEARS（None=全史无损；N=近 N 年，有损）
    years = None
    try:
        from app import config as _cfg
        years = getattr(_cfg, "SNAPSHOT_SLIM_YEARS", None)
    except Exception:
        years = None
    if os.path.isfile(slim_path):
        # 已有 slim 必须校验内容：kline day 行数 > 0 且 ≥ 源库 90%（防失败轮次留下的空壳半成品）
        try:
            c = sqlite3.connect("file:%s?mode=ro&immutable=1" % slim_path.replace("\\", "/"),
                                uri=True, timeout=15)
            n = c.execute("SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]
            c.close()
            src_c = sqlite3.connect("file:%s?mode=ro&immutable=1" % full_path.replace("\\", "/"),
                                    uri=True, timeout=15)
            src_n = src_c.execute("SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]
            src_c.close()
            if n > 0 and n >= src_n * 0.9:
                return slim_path, False
            print("  [rebuild] %s 已有 slim 行数=%d（源=%d）无效，重建" % (tag, n, src_n))
            os.remove(slim_path)
        except Exception as e:
            print("  [rebuild] %s 已有 slim 校验异常（%s），重建" % (tag, str(e)[:60]))
            try:
                os.remove(slim_path)
            except Exception:
                pass
    src = sqlite3.connect("file:%s?mode=ro&immutable=1" % full_path.replace("\\", "/"),
                          uri=True, timeout=30)
    try:
        dst = sqlite3.connect(slim_path)
        try:
            dst.execute("""CREATE TABLE kline(
                code TEXT, period TEXT, date TEXT,
                open REAL, high REAL, low REAL, close REAL,
                volume REAL, amount REAL,
                PRIMARY KEY(code, period, date))""")
            dst.execute("ATTACH DATABASE ? AS src", (full_path,))
            if years:
                _cut = "%d-01-01" % (int(time.strftime("%Y")) - int(years) + 1)
                dst.execute("""INSERT INTO kline(code,period,date,open,high,low,close,volume,amount)
                               SELECT code,period,date,open,high,low,close,volume,amount
                               FROM src.kline WHERE period='day' AND date >= ?""", (_cut,))
            else:
                dst.execute("""INSERT INTO kline(code,period,date,open,high,low,close,volume,amount)
                               SELECT code,period,date,open,high,low,close,volume,amount
                               FROM src.kline WHERE period='day'""")
            dst.commit()                       # 先提交再 DETACH（ATTACH 库不能被未提交事务锁住）
            dst.execute("DETACH DATABASE src")
            dst.execute("CREATE INDEX idx_kline_pcd ON kline(period,code,date)")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return slim_path, True


def slim(tag=None):
    """为超全库保留期且无 slim 的快照补生成精简版。返回 [(tag, slim_path, created)]"""
    full_days = int(getattr(__import__("app.config", fromlist=["C"]), "SNAPSHOT_KEEP_FULL_DAYS", 5))
    made = []
    snaps = sorted(d for d in os.listdir(SNAP)
                   if os.path.isdir(os.path.join(SNAP, d)) and not d.startswith("_"))
    for d in snaps:
        if tag and d != tag:
            continue
        full = os.path.join(SNAP, d, "market.db")
        if not os.path.isfile(full):
            continue
        age = _age_days_tag(d)
        if age <= full_days:
            continue
        sp, created = _slim_one(d, full)
        made.append((d, sp, created))
        print("  [%s] %s %s（age=%dd）" % (
            "生成" if created else "已有", d, os.path.basename(sp), age))
        # manifest 标注 slim
        mf = os.path.join(SNAP, d, "manifest.json")
        try:
            with open(mf, encoding="utf-8") as f:
                m = json.load(f)
            m["slim"] = True
            m["slim_generated_at"] = TODAY
            m["slim_years"] = years
            with open(mf, "w", encoding="utf-8", newline="\n") as f:
                json.dump(m, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return made


def pending_delete():
    """待删清单（只读）：_archive_pending_del 二期删 + 陈旧 .bak_* + >30 天无 slim 快照。
    返回 [(rel_path, size, 理由, 建议动作)]。"""
    items = []
    # 1) _archive_pending_del 全部（src 源 + .xz 归档；09-08 到期已过观察期）
    if os.path.isdir(STAGE):
        for dirpath, _d, files in os.walk(STAGE):
            for fn in files:
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, DATA).replace("\\", "/")
                items.append((rel, os.path.getsize(p),
                              "归档暂存区（09-01 建，09-08 观察期到）",
                              "二期删（用户确认后）"))
    # 2) 陈旧 .bak_*（F1/F3 验收已过、a1 已过；保留 market.db.bak* 中最新一份？→ 全部列，用户定）
    for fn in os.listdir(DATA):
        if fn.startswith("market.db.bak"):
            p = os.path.join(DATA, fn)
            if os.path.isfile(p):
                items.append((fn, os.path.getsize(p), "验收已过备份", "删（用户确认）"))
    # 3) >30 天快照（无 slim 保护时也列，由用户定；有 slim 的全库由 _cleanup 管）
    if os.path.isdir(SNAP):
        for d in sorted(os.listdir(SNAP)):
            if d.startswith("_") or not os.path.isdir(os.path.join(SNAP, d)):
                continue
            full = os.path.join(SNAP, d, "market.db")
            if not os.path.isfile(full):
                continue
            age = _age_days_tag(d)
            if age > 30:
                items.append((os.path.join("snapshots", d, "market.db"),
                              os.path.getsize(full), ">30 天快照（删除档）", "删（用户确认）"))
    items.sort(key=lambda x: -x[1])
    return items


def apply_delete(confirmed=False):
    if not confirmed:
        print("拒绝：--apply 需要 --yes（用户书面确认后执行）。本次仅列清单。")
        return 1
    items = pending_delete()
    total = sum(x[1] for x in items)
    print("待删 %d 项，共 %s" % (len(items), _bytes(total)))
    for rel, size, reason, act in items:
        p = os.path.join(DATA, rel)
        try:
            if os.path.isdir(p) and not rel.startswith("snapshots/"):
                import shutil
                shutil.rmtree(p)
            elif os.path.isfile(p):
                os.remove(p)
            print("  [DEL] %s（%s）" % (rel, _bytes(size)))
        except Exception as e:
            print("  [ERR] %s: %s" % (rel, e))
    # 清理空的 _archive_pending_del 目录树
    if os.path.isdir(STAGE):
        for root, _d, _f in list(os.walk(STAGE))[::-1]:
            try:
                os.rmdir(root)
            except Exception:
                pass
    return 0


def kpi():
    """每周存储 KPI：总大小/快照数/最大单文件/7 日增幅 → audit + history json。"""
    inv = inventory()
    total = inv["total_bytes"]
    snaps = [i for i in inv["items"]
             if i["path"].startswith("snapshots/") and i["path"].endswith("market.db")
             and "/_archive_pending_del/" not in i["path"]]
    biggest = max(inv["items"], key=lambda x: x["size"]) if inv["items"] else None
    prev = None
    try:
        with open(KPI_JSON, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        prev = None
    growth7 = None
    if prev and prev.get("total_bytes"):
        growth7 = total - prev["total_bytes"]
    rec = {"date": TODAY, "total_bytes": total,
           "total_gb": round(total / 2**30, 2),
           "snapshot_count": len(snaps),
           "snapshot_gb": round(sum(s["size"] for s in snaps) / 2**30, 2),
           "biggest_file": {"path": biggest["path"], "size": biggest["size"]} if biggest else None,
           "growth_7d_bytes": growth7,
           "growth_7d_gb": round(growth7 / 2**30, 2) if growth7 is not None else None}
    with open(KPI_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    try:
        from app import audit
        audit.record(kind="storage", event="storage_weekly_kpi", level="INFO",
                     total_gb=rec["total_gb"], snapshot_count=rec["snapshot_count"],
                     snapshot_gb=rec["snapshot_gb"],
                     biggest_file=(biggest or {}).get("path", ""),
                     biggest_gb=round((biggest or {}).get("size", 0) / 2**30, 2),
                     growth_7d_gb=rec["growth_7d_gb"])
    except Exception as e:
        print("audit 写入失败（不影响 KPI 计算）:", e)
    print("== storage KPI %s ==" % TODAY)
    print("  data/ 总大小 : %s" % _bytes(total))
    print("  快照数        : %d（共 %s）" % (len(snaps), _bytes(sum(s["size"] for s in snaps))))
    print("  最大单文件    : %s（%s）" % (biggest["path"], _bytes(biggest["size"])))
    print("  7 日增幅      : %s" % (_bytes(growth7) if growth7 is not None else "首次无基线"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="B3 存储治理（默认只盘点不删除）")
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("--slim", action="store_true")
    ap.add_argument("--tag")
    ap.add_argument("--kpi", action="store_true")
    ap.add_argument("--dry-run", dest="dry", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()

    if a.apply:
        return apply_delete(confirmed=a.yes)
    if a.kpi:
        return kpi()
    if a.slim:
        return 0 if slim(a.tag) is not None else 1
    if a.inventory or a.dry or not any([a.inventory, a.slim, a.kpi, a.apply]):
        inv = inventory()
        print("\n== 待删清单（默认只登记，需用户书面确认后 --apply --yes 执行）==")
        items = pending_delete()
        total = sum(x[1] for x in items)
        for rel, size, reason, act in items:
            print("  %-62s %8s  %s" % (rel, _bytes(size), reason))
        print("  共 %d 项 / %s" % (len(items), _bytes(total)))
        print("\n清理命令（确认后执行）:")
        print("  python tools/storage_gc.py --apply --yes")
        return 0


if __name__ == "__main__":
    sys.exit(main())
