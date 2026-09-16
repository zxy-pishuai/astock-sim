# -*- coding: utf-8 -*-
"""K6-3（2026-09-16）：J4 冒烟升级——"导入即调用"用例。

背景：J4（2026-09-13）只做了 import 冒烟，本次 P0 漏网点
（updater._daily_max_dates / updater.close_fallback_needed / global_market 读库函数）
属于"import 能过、一调用就炸"的类型，import 冒烟抓不住。

本文件：
1) 对 app 各模块挑"公开只读、无副作用"的函数（白名单维护），用最小 fixture 真调一次；
   任何 NameError / AttributeError / ImportError 直接判失败。
2) 负向自证：注入"删除 updater._db 依赖"后调用必须变红——证明用例真实依赖
   目标符号，而非空跑。
3) 全套离线、≤60s（本地 sqlite 只读 + 内存读）。
"""
import importlib
import time

BASE = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
if BASE not in __import__("sys").path:
    __import__("sys").path.insert(0, BASE)

import app  # noqa: F401
from app import config as C  # noqa: E402
import app.db as _db  # noqa: E402

# ---- 白名单：module -> [(函数路径, args, kwargs, 说明)] ----
# 只收录"只读、无副作用、不触网、快速"的函数；新增条目须注明出处与只读依据。
CALL_WHITELIST = [
    # ★ P0 漏网点 1：每票 kline max(date)（读 kline 表，GROUP BY，只读）
    ("app.updater", "_daily_max_dates", (), {}, "P0 漏网点：日K max(date) 读库"),
    # ★ P0 漏网点 2：收盘兜底判据（_daily_coverage 读库 + snapshot_path 路径计算，只读）
    ("app.updater", "close_fallback_needed", (), {}, "P0 漏网点：收盘兜底覆盖率判据"),
    # ★ P0 漏网点 3：global_market 读库函数（读 global_kline 表，只读；无网络路径）
    ("app.global_market", "load_history", ("usNVDA",), {"days": 3}, "P0 漏网点：global_kline 本地读库"),
    ("app.global_market", "_fill_pct_from_kline", ({"price": 1.0},), {}, "pct 兜底（load_history 只读回退）"),
    # audit 内存查询（只读 _buf）
    ("app.audit", "query", (), {"limit": 1}, "audit 内存查询（只读）"),
]

# 本地 helper（不走模块属性解析）：J3 open_ro 连接工厂连通性
LOCAL_CALLS = [
    ("_open_ro_ok", (), {}, "J3 open_ro 连接工厂连通性"),
    ("_k10_zero_protect_upsert", (), {}, "K10 判据④：UPSERT 新值0不覆盖库内非0 amount"),
    ("_k10_zero_protect_update", (), {}, "K10 判据④：UPDATE 新值0不覆盖库内非0 amount"),
]


def _open_ro_ok():
    """J3 连接工厂连通性（本地只读，不触网）。"""
    conn = _db.open_ro(C.DB_FILE, 5000)
    try:
        cur = conn.execute("SELECT 1")
        return cur.fetchone()[0] == 1
    finally:
        conn.close()


def _k10_zero_protect_upsert():
    """★ K10 判据④（UPSERT 语义，update_indices 同款 SQL）：
    新值 amount=0 时不得覆盖库内非 0 amount；新值>0 时正常覆盖。"""
    import sqlite3 as _sq
    conn = _sq.connect(":memory:")
    try:
        conn.execute("CREATE TABLE kline(code TEXT, period TEXT, date TEXT, "
                     "amount REAL, PRIMARY KEY(code,period,date))")
        conn.execute("INSERT INTO kline VALUES('sh000001','day','2026-09-16',871141346852.1)")
        # 新值=0 → 保留库内旧值（amount=CASE WHEN excluded.amount>0 ...）
        conn.execute(
            "INSERT INTO kline(code,period,date,amount) VALUES('sh000001','day','2026-09-16',0.0) "
            "ON CONFLICT(code,period,date) DO UPDATE SET "
            "amount=CASE WHEN excluded.amount>0 THEN excluded.amount ELSE kline.amount END")
        v1 = conn.execute("SELECT amount FROM kline").fetchone()[0]
        # 新值>0 → 正常覆盖
        conn.execute(
            "INSERT INTO kline(code,period,date,amount) VALUES('sh000001','day','2026-09-16',999.0) "
            "ON CONFLICT(code,period,date) DO UPDATE SET "
            "amount=CASE WHEN excluded.amount>0 THEN excluded.amount ELSE kline.amount END")
        v2 = conn.execute("SELECT amount FROM kline").fetchone()[0]
        return v1 == 871141346852.1 and v2 == 999.0
    finally:
        conn.close()


def _k10_zero_protect_update():
    """★ K10 判据④（UPDATE 语义，backfill_index_amount 同款 WHERE 保护）：
    仅当 新值>0 或 库内<=0 时覆盖；新值=0 且库内>0 → 不覆盖。"""
    import sqlite3 as _sq
    conn = _sq.connect(":memory:")
    try:
        conn.execute("CREATE TABLE kline(code TEXT, period TEXT, date TEXT, "
                     "amount REAL, PRIMARY KEY(code,period,date))")
        conn.execute("INSERT INTO kline VALUES('sh000001','day','2026-09-16',871141346852.1)")
        # 新值=0：条件 (?>0 OR amount<=0) → 0>0 False、amount(8711e9)>0 → 不更新
        cur = conn.execute(
            "UPDATE kline SET amount=? WHERE code=? AND period='day' AND date=? "
            "AND (?>0 OR amount<=0)", (0.0, "sh000001", "2026-09-16", 0.0))
        v1 = conn.execute("SELECT amount FROM kline").fetchone()[0]
        # 新值>0：正常覆盖
        cur = conn.execute(
            "UPDATE kline SET amount=? WHERE code=? AND period='day' AND date=? "
            "AND (?>0 OR amount<=0)", (999.0, "sh000001", "2026-09-16", 999.0))
        v2 = conn.execute("SELECT amount FROM kline").fetchone()[0]
        return cur.rowcount == 1 and v1 == 871141346852.1 and v2 == 999.0
    finally:
        conn.close()


def _resolve(mod_path, attr_path):
    """动态解析模块与属性（支持嵌套属性路径，如 a.b.c）。"""
    mod = importlib.import_module(mod_path)
    obj = mod
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def test_call_smoke_whitelist():
    """白名单函数逐个真调一次；任何 NameError/AttributeError/ImportError 判失败。"""
    t0 = time.time()
    failures = []
    cases = [(m, a, ar, kw, n) for (m, a, ar, kw, n) in CALL_WHITELIST]
    cases += [("<local>", a, ar, kw, n) for (a, ar, kw, n) in LOCAL_CALLS]
    for mod_path, attr, args, kwargs, note in cases:
        try:
            fn = _resolve(mod_path, attr) if mod_path != "<local>" else globals()[attr]
            result = fn(*args, **kwargs)
        except (NameError, AttributeError, ImportError) as e:  # noqa: BLE001
            failures.append("%s.%s (%s): %r" % (mod_path, attr, note, e))
            continue
        except Exception as e:  # noqa: BLE001
            # 业务异常不算"符号缺失"类失败，但记录便于人工核对（如库空表返回 []）
            print("[call-smoke] %s.%s 业务异常（非符号缺失）: %r" % (mod_path, attr, e))
        # 形状冒烟：返回值必须存在（None 视为可疑，除非函数契约本就可能 None）
        if result is None and note != "audit 内存查询（只读）":
            failures.append("%s.%s (%s): 返回 None，疑似空跑" % (mod_path, attr, note))
    elapsed = time.time() - t0
    print("[call-smoke] 白名单 %d 项，耗时 %.1fs" % (len(cases), elapsed))
    assert not failures, "导入即调用失败:\n" + "\n".join(failures)
    assert elapsed <= 60, "全套超时预算 60s：%.1fs" % elapsed


def test_db_dependency_removal_turns_red():
    """负向自证：注入"删掉 updater 的 _db 依赖"后调用必须变红。
    ——若实现不再依赖 _db（比如改成了别的连接方式），本用例自动失败，
    提醒维护白名单/自证逻辑，而不是让冒烟测试空转。"""
    import app.updater as updater

    assert hasattr(updater, "_db"), "updater 必须持有 _db（from . import db as _db）"
    saved = updater._db
    del updater._db
    try:
        try:
            updater._daily_max_dates()
        except (NameError, AttributeError):
            pass  # 期望：依赖缺失 → 符号找不到 → 变红 ✓
        else:
            raise AssertionError(
                "删掉 updater._db 后 _daily_max_dates() 仍能调用——自证失效，"
                "说明该函数不再依赖 _db（请更新本用例或确认重构意图）")
    finally:
        updater._db = saved
    # 恢复后必须可用（防测试自身污染模块状态）
    d = updater._daily_max_dates()
    assert isinstance(d, dict), "恢复 _db 后 _daily_max_dates 应返回 dict"
