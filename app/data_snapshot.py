# -*- coding: utf-8 -*-
"""★ Phase46: 数据快照 —— 冻结每日收盘后的 market.db，治"前复权重锚定"根因。

问题：market.db 的 kline 存的是随行情每日重算的前复权价，updater 每晚整体重写
历史收盘（实测一天内修订票数 [66,91]→[138,156] 增长），跨天回测的绝对收益不可比，
一切"基线精确复现"类验收失效。

方案：每日增量更新成功后，用 sqlite3 在线 backup API 把整库冻结为不可变快照
  data/snapshots/YYYYMMDD/market.db（在线备份对 WAL 并发写安全；服务不中断）。
回测工具用 latest_snapshot_path() 拿路径、以 C.DB_FILE=快照 方式运行，
即获得"当日数据状态"的可重放基准。保留策略：默认最近 KEEP_N 份。

用法：
  python tools/data_snapshot.py [--keep 10] [--force]
app 内调用：from app import data_snapshot; data_snapshot.auto_snapshot()
"""
import os
import json
import time
import sqlite3
from datetime import date

from . import config as C          # 只读引用：DATA_DIR 等
from . import db as _db            # ★ J3：统一连接工厂

SNAP_DIR = os.path.join(C.DATA_DIR, "snapshots")
KEEP_N_DEFAULT = 10


def today_tag():
    return time.strftime("%Y-%m-%d")


def snapshot_path(tag=None):
    return os.path.join(SNAP_DIR, tag or today_tag(), "market.db")


def list_snapshots():
    """已有序列（旧→新）：[(tag, path, size_mb), ...]"""
    out = []
    if not os.path.isdir(SNAP_DIR):
        return out
    for d in sorted(os.listdir(SNAP_DIR)):
        p = os.path.join(SNAP_DIR, d, "market.db")
        if os.path.isfile(p):
            out.append((d, p, round(os.path.getsize(p) / 1048576.0, 1)))
    return out


def snapshot_info(path):
    """读快照 manifest 明细，返回 {tag, path, age_days, created_at, rows_kline_day,
    size_mb, coverage, daily_done}。manifest 缺失/损坏（F4 部署前旧格式）时明细字段
    为 None，调用方自行降级显示。age_days 按自然日（目录名 tag 与今天之差）。"""
    tag = os.path.basename(os.path.dirname(path))
    info = {"tag": tag, "path": path,
            "created_at": None, "rows_kline_day": None, "size_mb": None,
            "daily_done": None, "coverage": None, "age_days": None}
    mf = os.path.join(os.path.dirname(path), "manifest.json")
    try:
        with open(mf, encoding="utf-8") as f:
            m = json.load(f)
        info["created_at"] = m.get("created_at")
        info["rows_kline_day"] = m.get("rows_kline_day")
        info["size_mb"] = m.get("size_mb")
        info["daily_done"] = m.get("daily_done")
        info["coverage"] = m.get("coverage")
    except Exception:
        pass
    try:
        y, mo, d = (int(x) for x in tag.split("-"))
        info["age_days"] = (date.today() - date(y, mo, d)).days
    except Exception:
        pass
    return info


def latest_snapshot_path(max_age_days=3):
    """返回最新**可用**快照：跳过 pit_lost 重建占位（★ B1，防止回测误当 PIT 基准）；
    超过 max_age_days 天（自然日）视为不可用，**返回 None**（★ B2，2026-09-13）——
    回测工具拿到 None 必须显式拒绝运行并提示"先补快照"，杜绝"5 天前旧快照静默
    进入回测"（09-13 实证：最新 09-08 已 5 天）。max_age_days=None 表示不校验
    新鲜度（显式选择）。明细见 snapshot_info()。"""
    snaps = list_snapshots()
    for tag, path, _mb in reversed(snaps):
        if _is_pit_lost(path):
            continue
        if max_age_days is None:
            return path
        info = snapshot_info(path)
        age = info.get("age_days")
        if age is None or age > max_age_days:
            return None
        return path
    return None


def _row_count(db, table="kline", period="day"):
    try:
        conn = _db.open_ro(db, 15000)   # ★ J3：统一连接工厂（读复用）
        n = conn.execute(
            "SELECT COUNT(*) FROM %s WHERE period='day'" % table).fetchone()[0]
        return n
    except Exception:
        return -1


def create_snapshot(force=False, keep_n=KEEP_N_DEFAULT, coverage=None,
                    daily_done=False):
    """冻结今日快照（sqlite 在线 backup，对并发写安全）。返回 (path, created)。
    ★ F4（2026-09-08）：manifest 增加 daily_done / coverage —— 快照落盘时的
    日K完成状态与覆盖度（供"带病快照"重建判定，见 auto_snapshot）。"""
    tag = today_tag()
    dst = snapshot_path(tag)
    if os.path.isfile(dst) and not force:
        return dst, False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    src = _db.open_ro(C.DB_FILE, 30000)   # ★ J3：统一连接工厂（读复用，backup 源）
    dst_conn = sqlite3.connect(dst)       # 全新目标文件：裸连接（backup API 目标，
                                          # 非查询连接；PRAGMA 由快照语义自管）
    try:
        src.backup(dst_conn)               # 在线备份：WAL 并发写安全
        dst_conn.execute("PRAGMA journal_mode=DELETE")   # 快照不再需要 WAL
    finally:
        dst_conn.close()
    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": C.DB_FILE,
        "rows_kline_day": _row_count(dst),
        "size_mb": round(os.path.getsize(dst) / 1048576.0, 1),
        # ★ F4：快照生成时的日K完成状态与覆盖度（带病判定依据）
        "daily_done": bool(daily_done),
        "coverage": coverage or {},
    }
    with open(os.path.join(os.path.dirname(dst), "manifest.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    _cleanup(keep_n)
    return dst, True


def _cleanup(keep_n):
    """★ B3（2026-09-13）：快照保留分级（策略开关在 config，默认保守不自动删）。
    1) 最近 SNAPSHOT_KEEP_FULL_DAYS 天 → 全库保留（回测 PIT 基准）；
    2) 更老且已生成精简版（market.slim.db）→ 删除全库版（精简版覆盖 kline day）；
    3) 更老但无精简版 → 保留并登记（由 tools/storage_gc.py --slim 生成后清理）；
    4) 超过 SNAPSHOT_SLIM_MAX_AGE_DAYS 且 SNAPSHOT_DELETE_OLDER_THAN 开启 → 删除。
    keep_n 为旧接口兜底：保留份数不小于 min(keep_n, full_days)（防旧调用方删多）。
    幂等：重复调用无副作用。"""
    snaps = list_snapshots()
    if not snaps:
        return
    full_days = int(getattr(C, "SNAPSHOT_KEEP_FULL_DAYS", 5) or 5)
    slim_max = int(getattr(C, "SNAPSHOT_SLIM_MAX_AGE_DAYS", 30) or 30)
    del_after = getattr(C, "SNAPSHOT_DELETE_OLDER_THAN", None)
    today = date.today()
    removed = 0
    for tag, path, _mb in sorted(snaps):
        try:
            y, mo, d = (int(x) for x in tag.split("-"))
            age = (today - date(y, mo, d)).days
        except Exception:
            age = 999
        if age <= full_days:
            continue
        slim = os.path.join(os.path.dirname(path), "market.slim.db")
        if del_after is not None and age > int(del_after):
            # 策略开启自动删除档（> 配置阈值）
            for f in (path, slim, os.path.join(os.path.dirname(path), "manifest.json")):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except Exception:
                    pass
            try:
                os.rmdir(os.path.dirname(path))
            except Exception:
                pass
            removed += 1
            continue
        if os.path.isfile(slim):
            # 已有精简版 → 全库可删（slim 保留 kline day）
            try:
                os.remove(path)
                removed += 1
            except Exception:
                pass
            continue
        # 无精简版：保留（等 storage_gc --slim 生成后再清；默认保守不自动删）
    if removed:
        try:
            from . import audit
            audit.record(kind="storage", event="snapshot_cleanup_tiered",
                         level="INFO", removed=removed,
                         full_days=full_days, slim_max=slim_max,
                         note="B3 分级保留：删除被精简版覆盖的全库快照（自动删除档未开启则不删）")
        except Exception:
            pass


def auto_snapshot(coverage=None, daily_done=None):
    """供 updater 收盘更新成功后调用：当日无快照才生成。返回 (path|None, created, grade)。
    ★ B1（2026-09-13）：快照策略从"追平才落盘"改为**分级落盘**——
      ratio>=0.95（或未知）→ ok：落盘，manifest.daily_done=true，audit data_snapshot INFO；
      0.8<=ratio<0.95 → partial：仍然落盘，manifest.daily_done=false+coverage，
        audit data_snapshot_partial WARN；
      ratio<0.8 → deferred：延后不落，audit data_snapshot_deferred INFO（含兜底说明），
        22:00 后由当日最终兜底无条件落盘；
      22:00 后当日仍无快照 → forced：无条件落盘（宁可带病不可缺失），
        audit data_snapshot_forced CRITICAL。
    grade ∈ {ok, partial, deferred, forced, exists}。审计事件在函数内记（updater 不再双记）。
    ★ F4（2026-09-08）语义保留：当日已存在带病快照（daily_done=false 或 ratio<0.9）
    且本次 ratio>=0.95 → 强制重建 + data_snapshot_repaired（留痕不停在"带病"）。"""
    tag = today_tag()
    ratio = (coverage or {}).get("ratio")
    existing = snapshot_path(tag)
    has_snap = os.path.isfile(existing)
    hour = int(time.strftime("%H"))
    grade, should = decide_grade(ratio, has_snap, hour)

    # ★ F4 兼容：当日已有快照但"带病"且本次已高覆盖 → 重建 + repaired 事件
    if has_snap:
        mf = os.path.join(os.path.dirname(existing), "manifest.json")
        prev_bad = False
        try:
            with open(mf, encoding="utf-8") as f:
                m = json.load(f)
            c = m.get("coverage") or {}
            prev_bad = (not m.get("daily_done")
                        or (c.get("ratio") is not None and c["ratio"] < 0.9))
        except Exception:
            prev_bad = True
        if grade == "ok" and prev_bad:
            p, created = create_snapshot(force=True, coverage=coverage,
                                         daily_done=True)
            try:
                from . import audit
                audit.record(kind="daily", event="data_snapshot_repaired",
                             level="INFO", path=p or "", rebuilt_from=existing,
                             coverage_ratio=ratio,
                             note="日K覆盖达标后重建带病快照，留痕不再停在'带病'")
            except Exception:
                pass
            return p, True, "ok"
        return existing, False, "exists"

    if not should:
        # deferred：延后，22:00 兜底已注册（由 _close_cli 22:00 分支 / 后续调用触发）
        try:
            from . import audit
            audit.record(kind="daily", event="data_snapshot_deferred",
                         level="INFO", ratio=ratio,
                         note="日K覆盖率<0.8，快照延后；当日22:00最终兜底将无条件落盘（宁可带病不可缺失）")
        except Exception:
            pass
        return None, False, "deferred"

    if grade == "forced":
        daily_done_flag = bool(ratio is not None and ratio >= 0.95)
    else:
        daily_done_flag = (grade == "ok")
    p, created = create_snapshot(coverage=coverage or {"ratio": ratio},
                                 daily_done=daily_done_flag)
    try:
        from . import audit
        fld = {"path": p or "", "created": created,
               "fresh": (coverage or {}).get("fresh"),
               "total": (coverage or {}).get("total"),
               "ratio": ratio}
        if grade == "forced":
            audit.record(kind="daily", event="data_snapshot_forced",
                         level="CRITICAL",
                         note="22:00当日最终兜底：无条件落盘（宁可带病不可缺失，快照为治前复权重锚定）",
                         **fld)
        elif grade == "partial":
            audit.record(kind="daily", event="data_snapshot_partial",
                         level="WARN",
                         note="日K覆盖率0.8~0.95，快照带病落盘（daily_done=false）",
                         **fld)
        else:
            audit.record(kind="daily", event="data_snapshot", level="INFO", **fld)
    except Exception:
        pass
    return p, created, grade


def ensure_daily_final(coverage=None):
    """★ B1：当日最终兜底——22:00 后当日无快照 → 无条件落盘 + CRITICAL data_snapshot_forced。
    供 updater._close_cli 的 22:00 分支调用（CloseCatchup 末轮触发）。幂等：已有快照则不动。"""
    return auto_snapshot(coverage=coverage)


def decide_grade(ratio, has_snap, hour):
    """★ B1：快照落盘分级决策（纯函数，可单测）。返回 (grade, should_snap)：
      exists   — 当日已有快照 → 不动
      forced   — 22:00 后无快照 → 无条件落盘（宁可带病）
      ok       — ratio>=0.95 或未知 → 落盘，daily_done=true
      partial  — 0.8<=ratio<0.95 → 落盘，daily_done=false
      deferred — ratio<0.8 → 延后，注册 22:00 兜底
    """
    if has_snap:
        return "exists", False
    if hour >= 22:
        return "forced", True
    if ratio is None or ratio >= 0.95:
        return "ok", True
    if ratio >= 0.8:
        return "partial", True
    return "deferred", False


def create_reconstructed_snapshot(tag):
    """★ B1：补生成历史缺失快照（PIT 不可复现的占位副本）。
    tag 当日真实库状态已被后续更新覆盖 → 副本 = 当前库，manifest 标
    reconstructed=false, pit_lost=true —— 仅供磁盘一致性，**禁止当 PIT 基准**
    （latest_snapshot_path() 会跳过此类快照）。返回 (path, created)。"""
    dst = snapshot_path(tag)
    if os.path.isfile(dst):
        return dst, False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    src = _db.open_ro(C.DB_FILE, 30000)   # ★ J3：统一连接工厂（读复用，backup 源）
    dst_conn = sqlite3.connect(dst)       # 全新目标文件：裸连接（backup API 目标）
    try:
        src.backup(dst_conn)
        dst_conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst_conn.close()
    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": C.DB_FILE,
        "rows_kline_day": _row_count(dst),
        "size_mb": round(os.path.getsize(dst) / 1048576.0, 1),
        "reconstructed": False,
        "pit_lost": True,
        "note": "当日真实库状态已被后续更新覆盖，PIT 不可复现；本副本仅磁盘一致性占位，"
                "禁止当 PIT 基准（latest_snapshot_path 已跳过）",
    }
    with open(os.path.join(os.path.dirname(dst), "manifest.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return dst, True


def _is_pit_lost(path):
    """manifest 标记 pit_lost（重建占位）→ True。"""
    try:
        mf = os.path.join(os.path.dirname(path), "manifest.json")
        with open(mf, encoding="utf-8") as f:
            return bool(json.load(f).get("pit_lost"))
    except Exception:
        return False


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Phase46 数据快照")
    ap.add_argument("--keep", type=int, default=KEEP_N_DEFAULT)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    p, created = create_snapshot(force=a.force, keep_n=a.keep)
    print("[snapshot] %s (%s)" % (p, "新建" if created else "已存在"))
    for tag, path, mb in list_snapshots():
        print("  %s  %5.1fMB" % (tag, mb))
