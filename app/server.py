# -*- coding: utf-8 -*-
"""HTTP 服务：REST API + 静态文件托管（标准库实现，零第三方依赖）
性能：行情 TTL 内存缓存（datafeed）、K线磁盘+内存双层缓存、
     回测异步任务（job 轮询）、全市场列表分页
"""
import json
import gzip
import hashlib
import os
import sqlite3
import threading
import time
import urllib.parse
import uuid
from collections import deque
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import alert as al
from . import db as _db            # ★ J3：统一连接工厂
from . import audit
from . import config as C
from . import datafeed as df
from . import engine as eng
from . import engine_minute as engm
from . import factor as fac
from . import portfolio as pf
from . import review as rv
from . import risk as rk
from . import scoring as sc
from . import sector as sector
from . import state as st
from . import trader as trader

# ★ 4.3：加载保存的佣金档位（覆盖 config 默认）
try:
    _tier = st.load_commission_tier()
    if _tier in C.COMMISSION_TIERS:
        C.COMMISSION_TIER = _tier
        C.COMMISSION_RATE = C.COMMISSION_TIERS[_tier]
except Exception:
    pass

# ============ 日志缓冲 ============
LOG_BUF = deque(maxlen=500)
LOG_LOCK = threading.Lock()

# ============ 数据健康只读面板（D3 2026-09-13）============
# 纯只读：接口内零写入、零重计算。60s TTL 内存缓存；sqlite 锁定/断库时
# 回退上次缓存并置 stale=True（前端灰显"上次缓存"），绝不报错。
_HEALTH_CACHE = {"ts": 0.0, "payload": None}
_HEALTH_LOCK = threading.Lock()
_HEALTH_TTL = 60.0
_HEALTH_INDEX_CODES = ("sh000001", "sz399001", "sz399006")
# 全票池 distinct code 数（5328）全索引扫描 ~1.2s → 后台线程计算 + 3600s 缓存，
# 主路径只做毫秒级趋势查询；首帧 total=None（前端显示"—"，下轮刷新填充）。
_TOTAL_CODES_CACHE = {"ts": 0.0, "n": None}
_TOTAL_CODES_LOCK = threading.Lock()


def _total_codes_cached():
    now = time.time()
    with _TOTAL_CODES_LOCK:
        if _TOTAL_CODES_CACHE["n"] is not None \
                and now - _TOTAL_CODES_CACHE["ts"] < 3600:
            return _TOTAL_CODES_CACHE["n"]

    def _calc():
        try:
            conn = _db.open_ro(C.DB_FILE, 2000)   # ★ J3：统一连接工厂（读复用）
            n = conn.execute(
                "SELECT COUNT(DISTINCT code) FROM kline WHERE period='day'"
            ).fetchone()[0] or 0
            with _TOTAL_CODES_LOCK:
                _TOTAL_CODES_CACHE["ts"] = time.time()
                _TOTAL_CODES_CACHE["n"] = n
        except Exception:
            pass

    threading.Thread(target=_calc, daemon=True).start()
    return None
# 存储统计独立缓存（300s）：data/ 下 cyq_cache 等数千小文件全扫 ~1.5s，
# 若放主路径会击穿 300ms 预算 → 后台线程首轮计算，接口首帧返回"统计中"。
_STORAGE_CACHE = {"ts": 0.0, "payload": None}
_STORAGE_LOCK = threading.Lock()
_STORAGE_TTL = 300.0


def _storage_stats_cached():
    """返回存储统计 dict；无缓存时启动后台线程计算并立即返回 None。"""
    now = time.time()
    with _STORAGE_LOCK:
        if _STORAGE_CACHE["payload"] and now - _STORAGE_CACHE["ts"] < _STORAGE_TTL:
            return _STORAGE_CACHE["payload"]

    def _calc():
        try:
            snap_dir = os.path.join(C.DATA_DIR, "snapshots")
            g7 = 0
            since7_dt = date.today() - timedelta(days=7)
            if os.path.isdir(snap_dir):
                for name in os.listdir(snap_dir):
                    p = os.path.join(snap_dir, name)
                    if os.path.isdir(p) and len(name) == 10:
                        try:
                            if date.fromtimestamp(os.path.getmtime(p)) >= since7_dt:
                                g7 += _dir_size_bytes(p)
                        except OSError:
                            continue
            p = {"data_total_bytes": _dir_size_bytes(C.DATA_DIR),
                 "snapshots_bytes": _dir_size_bytes(snap_dir),
                 "growth7_bytes": g7}
            with _STORAGE_LOCK:
                _STORAGE_CACHE["ts"] = time.time()
                _STORAGE_CACHE["payload"] = p
        except Exception:
            pass

    threading.Thread(target=_calc, daemon=True).start()
    return None


def _dir_size_bytes(path):
    """只读递归统计目录大小（lstat 不跟随符号链接，异常目录跳过）。"""
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        total += _dir_size_bytes(e.path)
                    else:
                        total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _snapshot_days(data_dir):
    """快照枚目录名（YYYY-MM-DD），排序返回；异常返回空。"""
    snap_dir = os.path.join(data_dir, "snapshots")
    days = []
    try:
        for name in os.listdir(snap_dir):
            p = os.path.join(snap_dir, name)
            if os.path.isdir(p) and len(name) == 10 and name[4] == "-" \
                    and name[:4].isdigit() and name[5:7].isdigit() and name[8:10].isdigit():
                days.append(name)
    except OSError:
        pass
    return sorted(days)


def _build_data_health():
    """构建数据健康载荷（只读）。任何单源失败→该组降级字段，不整体报错。"""
    today = date.today()
    today_s = today.isoformat()
    since7 = (today - timedelta(days=8)).isoformat()
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "stale": False, "source": "live", "today": today_s}

    # ---- 库内查询（kline/指数/ml_pred），失败→降级组 ----
    kc = {"error": None, "latest_date": None, "today_rows": 0,
          "total_codes": 0, "trend7": []}
    am = {"error": None, "latest_date": None, "amount0": 0,
          "total": 0, "ratio": None, "trend7": []}
    idx = {"error": None, "items": {}}
    mlp = {"error": None, "last_pred_date": None, "age_days": None}
    db_ok = False
    try:
        conn = _db.open_ro(C.DB_FILE, 2000)   # ★ J3：统一连接工厂（读复用）
        # 覆盖率 + amount 7 日趋势（走 idx_kline_pd：period+date）
        rows = conn.execute(
            "SELECT date, COUNT(*), "
            "SUM(CASE WHEN amount=0 AND volume>0 THEN 1 ELSE 0 END) "
            "FROM kline WHERE period='day' AND date >= ? "
            "GROUP BY date ORDER BY date", (since7,)).fetchall()
        total_codes = _total_codes_cached()
        latest_row = conn.execute(
            "SELECT MAX(date) FROM kline WHERE period='day'").fetchone()[0]
        kc["total_codes"] = total_codes  # None=后台计算中
        kc["latest_date"] = latest_row
        kc["today_rows"] = 0
        trend = []
        for d, n, az in rows:
            az = az or 0
            trend.append({"date": d, "rows": n,
                          "total": total_codes,
                          "ratio": round(n / total_codes, 4)
                          if total_codes else None,
                          "amount0": az,
                          "amount_ratio": round(az / n, 4) if n else 0.0})
            if d == latest_row:
                kc["today_rows"] = n
                am["latest_date"] = d
                am["amount0"] = az
                am["total"] = n
                am["ratio"] = round(az / n, 4) if n else 0.0
        kc["trend7"] = trend[-7:]
        am["trend7"] = trend[-7:]
        # 指数新鲜度（走 idx_kline_cp：period+code+date）
        for code in _HEALTH_INDEX_CODES:
            r = conn.execute(
                "SELECT MAX(date), COUNT(*), "
                "SUM(CASE WHEN amount>0 THEN 1 ELSE 0 END) "
                "FROM kline WHERE period='day' AND code=?",
                (code,)).fetchone()
            if r and r[0]:
                last, n, amt_gt0 = r
                idx["items"][code] = {
                    "last_date": last,
                    "age_days": (today - date.fromisoformat(last)).days,
                    "rows": n,
                    "amount_gt0": amt_gt0 or 0,
                    "amount_available": bool(amt_gt0 and n and amt_gt0 > 0)}
            else:
                idx["items"][code] = {"last_date": None, "age_days": None,
                                      "rows": 0, "amount_gt0": 0,
                                      "amount_available": False}
        # ML：ml_pred 表
        r = conn.execute("SELECT MAX(date) FROM ml_pred").fetchone()
        if r and r[0]:
            mlp["last_pred_date"] = r[0]
            mlp["age_days"] = (today - date.fromisoformat(r[0])).days
        conn.close()
        db_ok = True
    except Exception as e:
        kc["error"] = str(e)[:120]
        am["error"] = str(e)[:120]
        idx["error"] = str(e)[:120]
        mlp["error"] = str(e)[:120]
    out["db_ok"] = db_ok
    out["db_error"] = kc["error"]

    # ---- 覆盖率口径：今日(自然日) vs 最近交易日 ----
    kc["today_date"] = today_s
    out["kline_coverage"] = kc

    # ---- amount 完整性 ----
    out["amount"] = am

    # ---- 指数新鲜度 ----
    out["indices"] = idx

    # ---- 快照状态（只读目录）----
    snaps = {"latest": None, "age_days": None, "deferred": False,
             "deferred_reason": None, "last7": [], "count": 0}
    days = _snapshot_days(C.DATA_DIR)
    snaps["count"] = len(days)
    if days:
        latest = days[-1]
        snaps["latest"] = latest
        snaps["age_days"] = (today - date.fromisoformat(latest)).days
    last7 = []
    for i in range(7):
        d = (today - timedelta(days=6 - i)).isoformat()
        last7.append({"date": d, "exists": d in days})
    snaps["last7"] = last7
    # deferred 口径：最新快照落后于最近交易日（kline MAX(date)）
    if kc["latest_date"] and snaps["latest"]:
        if snaps["latest"] < kc["latest_date"]:
            snaps["deferred"] = True
            snaps["deferred_reason"] = ("快照 %s 落后于日K最新交易日 %s"
                                        % (snaps["latest"], kc["latest_date"]))
    elif not snaps["latest"]:
        snaps["deferred"] = True
        snaps["deferred_reason"] = "快照目录为空"
    out["snapshots"] = snaps

    # ---- ML 新鲜度（ml_scores.json + ml_pred）----
    mls = {"signal_date": None, "age_days": None, "model": None,
           "error": None, "n_scores": 0}
    try:
        with open(C.ML_SCORE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        mls["signal_date"] = d.get("date")
        mls["model"] = d.get("model")
        mls["n_scores"] = len(d.get("scores") or {})
        if d.get("date"):
            mls["age_days"] = (today - date.fromisoformat(str(d["date"]))).days
    except Exception as e:
        mls["error"] = str(e)[:120]
    out["ml"] = {"ml_scores": mls, "ml_pred": mlp}

    # ---- 未处置告警（quality_alert.jsonl 近 7 天）----
    al7 = {"warn": 0, "critical": 0, "info": 0, "recent": [], "error": None}
    try:
        qa = os.path.join(C.DATA_DIR, "quality_alert.jsonl")
        recs = []
        if os.path.exists(qa):
            with open(qa, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:
                        continue
                    dt = (r.get("date") or "")[:10]
                    if dt >= since7:
                        recs.append(r)
        for r in recs:
            lv = (r.get("level") or "").upper()
            if lv == "WARN":
                al7["warn"] += 1
            elif lv == "CRITICAL":
                al7["critical"] += 1
            else:
                al7["info"] += 1
        al7["recent"] = [{
            "date": (r.get("date") or "")[:19],
            "event": r.get("event") or "summary",
            "level": (r.get("level") or "INFO").upper(),
            "target_day": r.get("target_day"),
            "note": str(r.get("note") or r.get("high_severity") or "")[:100],
        } for r in recs[-6:]][::-1]
    except Exception as e:
        al7["error"] = str(e)[:120]
    out["alerts"] = al7

    # ---- 存储（后台线程统计，300s 缓存；首帧"统计中"不阻塞主路径）----
    stg = _storage_stats_cached()
    if stg is None:
        stg = {"data_total_bytes": None, "snapshots_bytes": None,
               "growth7_bytes": None, "growth7_note":
                   "统计中（后台线程首轮计算，300s 缓存）", "error": None}
    else:
        stg = dict(stg)
        stg["growth7_note"] = ("近7日新增快照枚体积合计（接口只读，"
                               "不做基线写盘）")
    out["storage"] = stg

    return out


def _data_health_cached():
    """60s TTL 缓存入口；构建失败回退上次缓存并标 stale。"""
    now = time.time()
    with _HEALTH_LOCK:
        if _HEALTH_CACHE["payload"] and now - _HEALTH_CACHE["ts"] < _HEALTH_TTL:
            return _HEALTH_CACHE["payload"]
    try:
        payload = _build_data_health()
        # 库级失败（db_ok=False）：有旧缓存→回退旧缓存并标 stale（灰显），
        # 无缓存→返回含各分组 error 的降级载荷（前端红态显示原因）。
        if not payload.get("db_ok"):
            with _HEALTH_LOCK:
                if _HEALTH_CACHE["payload"]:
                    p = dict(_HEALTH_CACHE["payload"])
                    p["stale"] = True
                    p["stale_reason"] = "market.db 只读连接失败: %s" % (
                        payload.get("db_error") or "unknown")
                    return p
        with _HEALTH_LOCK:
            _HEALTH_CACHE["ts"] = now
            _HEALTH_CACHE["payload"] = payload
        return payload
    except Exception as e:
        with _HEALTH_LOCK:
            if _HEALTH_CACHE["payload"]:
                p = dict(_HEALTH_CACHE["payload"])
                p["stale"] = True
                p["stale_reason"] = str(e)[:120]
                return p
        return {"error": str(e)[:120], "stale": True,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def _db_max(table, period=None, min5=False):
    """取数据表最新日期（增量更新状态用）"""
    try:
        db = C.MIN5_DB_FILE if min5 else C.DB_FILE
        conn = _db.open_ro(db, 10000)   # ★ J3：统一连接工厂（读复用）
        if min5:
            r = conn.execute("SELECT MAX(date) FROM kline_min5").fetchone()
        else:
            r = conn.execute(
                "SELECT MAX(date) FROM kline WHERE period=?", (period,)).fetchone()
        return (r[0] or "")[:10]
    except Exception:
        return ""

# ★ 4.1：情绪历史缓存（首次重建慢，缓存 30 分钟）
_SENTI_HIST_CACHE = None


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with LOG_LOCK:
        LOG_BUF.append(line)
    print(line, flush=True)


# ============ 回测任务管理 ============
JOBS = {}
JOBS_LOCK = threading.Lock()


def _run_backtest_job(job_id, payload):
    def update(phase=None, cur=None, total=None):
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j:
                j["progress"] = {"phase": phase, "cur": cur, "total": total}
    try:
        codes = payload.get("codes", [])
        names = payload.get("names", {})
        start = payload.get("start", "")
        end = payload.get("end", "")
        capital = float(payload.get("capital", C.INITIAL_CAPITAL))
        strategy = payload.get("strategy", "score")
        params = payload.get("params", {})
        mode = payload.get("mode", "normal")  # v3.5: normal | walk_forward | v3.7: portfolio | v3.9: optimize
        if mode == "portfolio":
            # ★ v3.7：多策略组合回测
            strategies = payload.get("strategies", ["score", "board", "twothirty"])
            allocation = payload.get("allocation", "score")
            from . import multistrategy as ms
            result = ms.combine(strategies, codes, names, start, end,
                                capital, params, allocation=allocation)
            with JOBS_LOCK:
                JOBS[job_id]["result"] = result
                JOBS[job_id]["status"] = "done"
            return
        if mode == "optimize" and strategy not in ("premium", "board_intraday", "auction_intraday"):
            # ★ v3.9：参数自动寻优
            grid = payload.get("optimize_grid") or {}
            result = eng.optimize_params(
                codes, names, start, end, strategy, capital, params,
                grid=grid, method=payload.get("optimize_method", "grid"),
                max_iters=int(payload.get("optimize_iters", 60)),
                metric=payload.get("optimize_metric", "sharpe"))
            with JOBS_LOCK:
                JOBS[job_id]["result"] = result
                JOBS[job_id]["status"] = "done"
            return
        if mode == "multi_window":
            # ★ Phase12：跨年代多窗口验证（同一套参数跑多个年代窗口）
            windows = payload.get("windows") or None
            update("多窗口验证", 0, 1)
            result = eng.multi_window(codes, names, windows, strategy, capital, params)
            with JOBS_LOCK:
                JOBS[job_id]["result"] = result
                JOBS[job_id]["status"] = "done"
            return
        if mode == "rigorous":
            # ★ Phase14：一键严谨回测 —— 串行编排 多窗口 → WF → 双参敏感性 → 综合判定
            #   全部在 daemon 线程内执行，不阻塞 HTTP；progress.phase 分阶段更新
            try:
                # 阶段1：跨年代多窗口（预设 4 窗口）
                update("严谨回测：多窗口验证", 0, 4)
                mw = eng.multi_window(codes, names, None, strategy, capital, params)
                update("严谨回测：多窗口验证完成", 4, 4)
                # 阶段2：walk_forward 3 折（旧式等长换算；窗口不足 200 天自动用近一年兜底）
                update("严谨回测：Walk-Forward 3折", 1, 3)
                try:
                    from datetime import datetime as _dt
                    _d0 = _dt.strptime(start, "%Y-%m-%d"); _d1 = _dt.strptime(end, "%Y-%m-%d")
                    _span = (_d1 - _d0).days
                except Exception:
                    _span = 0
                _wf_start, _wf_end = start, end
                if _span < 200:
                    _wf_start, _wf_end = "2025-08-18", "2026-08-18"
                wf = eng.walk_forward(codes, names, _wf_start, _wf_end, strategy,
                                      capital, params, folds=3)
                update("严谨回测：Walk-Forward 完成", 3, 3)
                # 阶段3：双参数敏感性（buy_threshold × position_pct）
                update("严谨回测：双参数敏感性", 0, 3)
                sens = eng.sensitivity_scan(
                    codes, names, start, end, strategy, capital, params,
                    param="buy_threshold",
                    base=float(params.get("buy_threshold", 25) if params else 25),
                    steps=(0.8, 1.0, 1.2),
                    param2="position_pct", values2=[0.2, 0.3, 0.4])
                update("严谨回测：敏感性完成", 3, 3)
                # 阶段4：综合判定
                verdict = _rigorous_verdict(mw, wf, sens)
                result = {
                    "mode": "rigorous",
                    "multi_window": mw,
                    "walk_forward": wf,
                    "sensitivity": sens,
                    "verdict": verdict,
                }
            except Exception as e:
                result = {"mode": "rigorous", "error": str(e)}
            with JOBS_LOCK:
                JOBS[job_id]["result"] = result
                JOBS[job_id]["status"] = "done"
            return
        if strategy in ("board_intraday", "auction_intraday"):
            bt = engm.MinuteBoardBacktest(codes, names, start, end, capital, strategy, params)
        elif mode == "walk_forward" and strategy not in ("premium",):
            # ★ v3.5 / Phase10：walk-forward 样本外验证（滚动切分 + 网格调参）
            folds = int(payload.get("wf_folds", 3))
            result = eng.walk_forward(codes, names, start, end, strategy,
                                      capital, params, folds=folds,
                                      train_days=int(payload.get("wf_train_days", 252)),
                                      test_days=int(payload.get("wf_test_days", 126)),
                                      step_days=int(payload.get("wf_step_days", 126)),
                                      anchored=bool(payload.get("wf_anchored", False)))
            with JOBS_LOCK:
                JOBS[job_id]["result"] = result
                JOBS[job_id]["status"] = "done"
            return
        else:
            bt = eng.Backtest(codes, names, start, end, capital, strategy, params)
        result = bt.run(progress_cb=lambda d, t, phase=None: update(phase, d, t))
        # ★ v3.9：保存回测报告（前端可勾选）
        if payload.get("save_report"):
            try:
                from . import report as rpt
                meta = {"strategy": strategy, "codes": codes, "names": names,
                        "start": start, "end": end, "capital": capital,
                        "params": params, "mode": mode}
                saved = rpt.save_report(result, meta)
                result["report_id"] = saved["id"]
            except Exception:
                pass
        # ★ Phase13：参数敏感度扫描（单/双参数，过拟合检验）
        sensitivity = payload.get("sensitivity")
        if sensitivity and strategy not in ("board_intraday", "auction_intraday"):
            _steps2 = sensitivity.get("steps2")
            result["sensitivity"] = eng.sensitivity_scan(
                codes, names, start, end, strategy, capital, params,
                param=sensitivity.get("param", "buy_threshold"),
                base=float(sensitivity.get("base", 25)),
                steps=tuple(float(x) for x in sensitivity.get("steps", (0.8, 1.0, 1.2))),
                param2=sensitivity.get("param2") if _steps2 else None,
                values2=tuple(_steps2) if _steps2 else None)
        with JOBS_LOCK:
            JOBS[job_id]["result"] = result
            JOBS[job_id]["status"] = "done"
    except Exception as e:
        log(f"回测任务失败: {e}")
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)


def start_backtest(payload):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "progress": {}, "result": None, "error": None}
    threading.Thread(target=_run_backtest_job, args=(job_id, payload), daemon=True).start()
    return job_id


def _rigorous_verdict(mw, wf, sens):
    """综合判定：可用 / 疑似过拟合 / 建议弃用。
    三维度：① 多窗口正收益占比≥50% ② WF stable ③ 双参敏感性平台区（相邻平均Δ<5pp）。
    """
    reasons = []
    ok = 0
    # ① 多窗口
    mws = (mw or {}).get("summary") or {}
    mw_ok = False
    if mws.get("count"):
        ratio = mws.get("positive_ratio") or 0
        mw_ok = ratio >= 0.5
        ok += 1 if mw_ok else 0
        reasons.append(f"多窗口正收益占比 {mws.get('positive_folds')}/{mws.get('count')}"
                       + ("（达标）" if mw_ok else "（未达标）"))
    else:
        reasons.append("多窗口无有效数据")
    # ② WF
    wfs = (wf or {}).get("summary") or {}
    wf_ok = bool(wfs.get("stable"))
    ok += 1 if wf_ok else 0
    reasons.append("WF 样本外" + ("稳健（达标）" if wf_ok else "不稳定（未达标）")
                   + f"（OOS均{wfs.get('avg_oos_return')}）")
    # ③ 敏感性平台区
    sens_ok = False
    if sens and sens.get("dual"):
        m = sens.get("matrix") or []
        deltas = []
        for row in m:
            for j in range(1, len(row)):
                if row[j] is not None and row[j - 1] is not None:
                    deltas.append(abs(row[j] - row[j - 1]))
        for i in range(1, len(m)):
            if m[i] and m[i - 1] and m[i][0] is not None and m[i - 1][0] is not None:
                deltas.append(abs(m[i][0] - m[i - 1][0]))
        avg_d = (sum(deltas) / len(deltas)) if deltas else 0
        sens_ok = avg_d < 0.05
        ok += 1 if sens_ok else 0
        reasons.append(f"双参敏感性平台区（相邻平均Δ{(avg_d*100):.1f}pp）"
                       + ("达标" if sens_ok else "未达标"))
    else:
        reasons.append("双参敏感性未产生数据")
    if ok == 3:
        verdict = "✅ 可用：多窗口、样本外、参数平台三维度全部通过"
    elif ok >= 2:
        verdict = f"⚠️ 疑似过拟合：三维度通过 {ok}/3，" + "；".join(reasons)
    else:
        verdict = f"❌ 建议弃用：三维度通过 {ok}/3，" + "；".join(reasons)
    return {"level": "ok" if ok == 3 else ("warn" if ok == 2 else "bad"),
            "ok_count": ok, "total": 3, "text": verdict, "reasons": reasons}


# ============ 全市场列表缓存（内存） ============
# ★ J2（2026-09-13）：_LIST_CACHE 加在途去重（refreshing 单飞）+ SWR（过期返旧值后台刷新），
#   消除"缓存过期 → 并发 N 请求各触发一次全市场 fetch_quotes"的惊群。
_LIST_CACHE = {"ts": 0, "data": [], "refreshing": False}
_LIST_CACHE_LOCK = threading.Lock()


def _refresh_list_sync():
    """全市场行情取数（首次同步 / 后台刷新共用）。改用 F1 展示通道 fetch_quotes_display。"""
    lst = df.get_stock_list()
    if not lst:
        return _LIST_CACHE["data"]
    try:
        quotes = df.fetch_quotes_display([c for c, _, _ in lst])
    except Exception:
        quotes = df.fetch_quotes([c for c, _, _ in lst])
    rows = []
    for c, n, p in lst:
        q = quotes.get(c) or {}
        rows.append({
            "code": c, "name": n,
            "price": q.get("price", p),
            "pct_chg": q.get("pct_chg", 0),
            "amount": q.get("amount", 0),
            "turnover": q.get("turnover", 0),
            "vol_ratio": q.get("vol_ratio", 0),
        })
    _LIST_CACHE["data"] = rows
    _LIST_CACHE["ts"] = time.time()
    _LIST_CACHE["refreshing"] = False
    return rows


def _refresh_list_bg():
    try:
        _refresh_list_sync()
    except Exception:
        _LIST_CACHE["refreshing"] = False


def _stocklist_paged(page, size, sort, order, kw):
    now = time.time()
    with _LIST_CACHE_LOCK:
        has = bool(_LIST_CACHE["data"])
        stale = has and now - _LIST_CACHE["ts"] > 30
        refreshing = _LIST_CACHE["refreshing"]
    if not has:
        _refresh_list_sync()                      # 首次：同步填（之后全命中）
    elif stale and not refreshing:
        with _LIST_CACHE_LOCK:
            if not _LIST_CACHE["refreshing"]:
                _LIST_CACHE["refreshing"] = True
                threading.Thread(target=_refresh_list_bg, daemon=True).start()
        # SWR：立即返回旧值（带 stale 标记由调用方处理）
    rows = _LIST_CACHE["data"]
    if kw:
        kw = kw.lower()
        rows = [r for r in rows if kw in r["code"].lower() or kw in r["name"].lower()]
    sort_key = {"price": "price", "pct_chg": "pct_chg", "amount": "amount",
                "turnover": "turnover"}.get(sort, "amount")
    rev = order != "asc"
    rows = sorted(rows, key=lambda r: r.get(sort_key) or 0, reverse=rev)
    total = len(rows)
    page = max(1, page)
    start = (page - 1) * size
    out = {"total": total, "page": page, "size": size,
           "rows": rows[start:start + size]}
    if stale:
        out["stale"] = True
    return out


# ============ 请求处理 ============
# ★ J1（2026-09-13）：HTTP 基础三件套——gzip / keep-alive / 静态资源缓存
#   - Handler.protocol_version = HTTP/1.1（keep-alive；已有 Content-Length，安全）
#   - _send 文本类 ≥1KB 且客户端接受 gzip → 自动压缩（>512KB 用 level=1 省 CPU）
#   - _serve_static 内存缓存 + ETag/Last-Modified + 304 协商 + vendor immutable
#   - 仅标准库（gzip/hashlib），零外部依赖
_TEXT_CT = ("application/json", "application/javascript", "text/css",
            "text/html", "image/svg+xml")
_GZ_MIN = 1024            # 压缩阈值：≥1KB 才压
_GZ_LEVEL_BIG = 1         # >512KB 的 _send 自动压缩档（echarts 类大响应省 CPU）
_GZ_LEVEL_SMALL = 9       # <512KB 档（API/静态小文件；backtest/pool 37.9% 接近 gzip 极限）
_GZ_STATIC_BIG = 6        # 静态预压大文件档（一次性+缓存，echarts 1MB → 338KB vs level1 478KB）
_GZ_BIG_THRESHOLD = 512 * 1024
_IMMUTABLE_PREFIX = "/vendor/"
_IMMUTABLE_EXACT = ("/favicon.ico",)
_STATIC_CACHE = {}        # rel -> {mtime,size,etag,ctype,cache_control,last_modified,raw,gz}
_EXT_MAP = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
            ".css": "text/css; charset=utf-8", ".png": "image/png",
            ".svg": "image/svg+xml", ".ico": "image/x-icon",
            ".json": "application/json", ".woff2": "font/woff2"}


def _load_static(rel):
    """读静态文件并缓存（mtime/size 变化即失效重建）。
    rel: "/index.html" 等 URL 路径。缓存含 raw + gz（文本类 ≥1KB 预压缩）。
    越界/不存在 → None。"""
    try:
        full = os.path.normpath(os.path.join(C.WEB_DIR, rel.lstrip("/")))
    except Exception:
        return None
    if not full.startswith(os.path.normpath(C.WEB_DIR)) or not os.path.isfile(full):
        return None
    try:
        so = os.stat(full)
    except OSError:
        return None
    ent = _STATIC_CACHE.get(rel)
    if ent and ent["mtime"] == so.st_mtime and ent["size"] == so.st_size:
        return ent
    try:
        with open(full, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    etag = '"%s"' % hashlib.md5(raw).hexdigest()[:16]
    ctype = _EXT_MAP.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
    cache_control = ("public, max-age=604800, immutable"
                     if rel.startswith(_IMMUTABLE_PREFIX) or rel in _IMMUTABLE_EXACT
                     else "no-cache")
    gz = None
    if ctype.startswith(_TEXT_CT) and len(raw) >= _GZ_MIN:
        gz = gzip.compress(raw, _GZ_STATIC_BIG if len(raw) > _GZ_BIG_THRESHOLD
                           else _GZ_LEVEL_SMALL)
    ent = {"mtime": so.st_mtime, "size": so.st_size, "etag": etag,
           "ctype": ctype, "cache_control": cache_control,
           "last_modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                          time.gmtime(so.st_mtime)),
           "raw": raw, "gz": gz}
    _STATIC_CACHE[rel] = ent
    return ent


def _warm_static_gz():
    """启动时预压缩 vendor 大文件（echarts.min.js 等）进内存缓存，
    避免首次请求现场压 1MB。失败不影响服务启动。"""
    try:
        for root, _dirs, files in os.walk(os.path.join(C.WEB_DIR, "vendor")):
            for fn in files:
                p = os.path.join(root, fn)
                rel = "/" + os.path.relpath(p, C.WEB_DIR).replace("\\", "/")
                _load_static(rel)
    except Exception:
        pass


# ============ ★ J2（2026-09-13）：慢接口治理 ============
# 通用路由级缓存：命中→直返；过期有旧值→返旧(stale=true)+单飞后台刷新（SWR）；
# ★ K5（2026-09-16）：冷路径单飞——无缓存值（ent is None）也走 _API_REFRESHING
#   单飞（同一 key 只允许一个线程计算），其余请求立即返回轻量占位
#   {"pending": true, "retry_after_ms": N}，不再各自同步算（37s 冷启动悬崖）。
_API_CACHE = {}          # key -> {"ts": float, "data": obj}
_DEPTH_CACHE = {}        # ★D-S4 十档盘口 2s 微缓存：code -> (ts, payload)——K线页/持仓页共用
_API_REFRESHING = {}     # key -> bool
_API_CACHE_LOCK = threading.Lock()
API_SLOW_MS = 1000       # 慢接口阈值（自动发现埋点）
_SLOW_WINDOW = {"start": 0.0, "hits": []}   # 10 分钟滚动窗口（api_slow 汇总）


def _cached_api(key_fn, ttl, swr=True):
    """路由级缓存装饰器。key_fn: str 或 callable(*a,**kw)->str。
    K5 语义：无值 → 单飞启动 + pending 占位（客户端按 retry_after_ms 重试）。"""
    def deco(fn):
        def wrapper(*a, **kw):
            key = key_fn(*a, **kw) if callable(key_fn) else key_fn
            now = time.time()
            ent = _API_CACHE.get(key)
            if ent and now - ent["ts"] < ttl:
                return ent["data"]
            # 单飞：同一 key 同时只允许一个刷新任务（含冷路径）
            with _API_CACHE_LOCK:
                if not _API_REFRESHING.get(key):
                    _API_REFRESHING[key] = True
                    threading.Thread(target=_api_refresh_worker,
                                     args=(key, fn, a, kw), daemon=True).start()
            if ent is not None and swr:
                out = ent["data"]
                if isinstance(out, dict):
                    out = dict(out)
                    out["stale"] = True
                    out["generated_at"] = out.get("generated_at") or ""
                return out
            # 冷路径：单飞已启动 → 轻量占位（客户端轮询重试）
            return {"pending": True, "retry_after_ms": 3000}
        return wrapper
    return deco


def _api_refresh_worker(key, fn, a, kw):
    """单飞后台刷新（daemon 线程，失败保旧值，finally 清标记）。"""
    try:
        data = fn(*a, **kw)
        _API_CACHE[key] = {"ts": time.time(), "data": data}
    except Exception:
        pass
    finally:
        _API_REFRESHING[key] = False


def _api_slow_summary():
    """每 10 分钟滚动窗口：api_slow 事件汇总 P95 TOP5 接口 → audit。惰性触发。"""
    import statistics as _st
    now = time.time()
    if not _SLOW_WINDOW["hits"]:
        return
    if now - _SLOW_WINDOW["start"] < 600:
        return
    hits = _SLOW_WINDOW["hits"]
    by_path = {}
    for h in hits:
        by_path.setdefault(h["path"], []).append(h["ms"])
    top = sorted(by_path.items(), key=lambda kv: -_st.median(kv[1]))[:5]
    out = [{"path": p, "n": len(v), "p95": round(
        sorted(v)[min(len(v) - 1, int(0.95 * len(v)))], 1), "max": round(max(v), 1)}
        for p, v in top]
    try:
        audit.record("api", "api_slow_summary", level="WARN",
                     window_min=10, top5=out, n=len(hits))
    except Exception:
        pass
    _SLOW_WINDOW["hits"] = []
    _SLOW_WINDOW["start"] = now


def _note_api_slow(path, ms):
    """api_slow 埋点：>API_SLOW_MS 记 audit；滚动窗口累积（10 分钟 P95 TOP5）。"""
    try:
        if ms > API_SLOW_MS:
            audit.record("api", "api_slow", level="WARN",
                         path=str(path)[:80], ms=round(ms, 1),
                         t=time.strftime("%Y-%m-%d %H:%M:%S"))
        if not _SLOW_WINDOW["hits"]:
            _SLOW_WINDOW["start"] = time.time()
        _SLOW_WINDOW["hits"].append({"path": path, "ms": ms})
        if time.time() - _SLOW_WINDOW["start"] >= 600:
            _api_slow_summary()
    except Exception:
        pass


# ---- ★ J2：慢接口缓存包装（模块级，_api_get_inner 分支引用）----

def _tactics_cached():
    from . import tactics as tct
    return tct.get_tactics_board()
_TACTICS_API = _cached_api(lambda *a: "tactics:" + time.strftime("%Y-%m-%d"), 300)(_tactics_cached)


def _senti_hist_cached():
    from . import sentiment_series as ss
    pool = ss.rebuild_history(days=250)
    series = ss.build_sentiment_series(pool)
    ic = ss.sentiment_ic(series)
    return {"days": len(series), "ic": ic, "recent": series[-30:]}
_SENTI_HIST_API = _cached_api("senti_hist", 1800)(_senti_hist_cached)


def _pool_cached(kind):
    """backtest/pool（原 server.py 分支逻辑原样搬入，ttl=600）。"""
    if kind == "local_full":
        conn = _db.open_ro(C.DB_FILE, 10000)   # ★ J3：统一连接工厂（读复用）
        rows = conn.execute(
            "SELECT code, COUNT(*) AS c FROM kline WHERE period='day' "
            "GROUP BY code HAVING c>=300").fetchall()
        codes = [r[0] for r in rows]
        names = {}
        try:
            for c, n, _p in df.get_stock_list():
                names[str(c)] = n
        except Exception:
            pass
        names = {c: names.get(c) or c for c in codes}
    else:
        size = 500
        d = _stocklist_paged(1, size, "amount", "desc", "")
        codes, names = [], {}
        for r in d.get("rows", []):
            codes.append(r["code"])
            names[r["code"]] = r.get("name") or r["code"]
    return {"codes": codes, "names": names, "count": len(codes), "kind": kind}
_POOL_API = _cached_api(lambda *a: "pool:" + str(a[0]), 600)(_pool_cached)


def _premarket_cached(scope, min_score):
    from . import premarket_news as pn
    return pn.premarket_news(scope=scope, min_score=min_score, force=False)
_PRE_MKT_API = _cached_api(lambda *a: "premkt:%s:%s" % (a[0], a[1]), 600)(_premarket_cached)


def _sentiment_cached(date):
    from . import sentiment as senti
    return senti.sentiment_snapshot(date=date, force=False)
_SENTI_API = _cached_api(lambda *a: "senti:%s" % (a[0] or ""), 60)(_sentiment_cached)


def _status_cached():
    return trader.engine.status()


def _status_placeholder():
    """status 首次冷态（~3s，sentiment 冷构建）→ 200ms 内返回轻量占位，后台填真值。
    前端守卫：sentiment 用 if(se) 渲染，null 安全；running/auto/positions/cash 即时可用。"""
    try:
        with trader.engine._lock:
            running = trader.engine.running
            auto = trader.engine.auto
    except Exception:
        running, auto = False, False
    try:
        acct = st.load_account()
    except Exception:
        acct = {"positions": {}, "cash": 0}
    return {"running": running, "auto": auto,
            "in_trading_time": trader._is_trading_time(),
            "in_auction_time": trader._is_auction_time(),
            "board_enabled": C.BOARD_TRADE_ENABLED,
            "positions": len(acct.get("positions", {})),
            "cash": round(acct.get("cash", 0), 2),
            "sentiment": None, "risk": None, "events": [], "watch_events": [],
            "warming": True, "stale": True}


def _status_entry():
    """★ J2：trading/status 3s SWR——命中直返；过期/无值→立即返旧值或轻量占位 + 单飞后台刷新。
    目标：前端 3s 轮询永不阻塞（冷态 ≤200ms）。"""
    now = time.time()
    ent = _API_CACHE.get("trading/status")
    if ent and now - ent["ts"] < 3:
        return ent["data"]
    with _API_CACHE_LOCK:
        if not _API_REFRESHING.get("trading/status"):
            _API_REFRESHING["trading/status"] = True
            threading.Thread(target=_api_refresh_worker,
                             args=("trading/status", _status_cached, (), {}), daemon=True).start()
    if ent is not None:
        out = dict(ent["data"])
        out["stale"] = True
        return out
    return _status_placeholder()


def _launch_review_proc(day):
    """review 独立进程生成：subprocess 调 generate_review → 写 data/reviews/review_{day}.json
    （Web 进程只读文件；import 启动成本 ~0.4s < 2s 阈值，无需常驻 sidecar）。"""
    import subprocess
    import sys as _sys
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _out = os.path.join(C.DATA_DIR, "reviews")
        os.makedirs(_out, exist_ok=True)
        _fp = os.path.join(_out, "review_%s.json" % day)
        _code = ("from app.review import generate_review; import json;"
                 "r = generate_review(day=%r, save=True);"
                 "open(%r, 'w', encoding='utf-8').write(json.dumps(r, ensure_ascii=False))"
                 % (day, _fp))
        subprocess.Popen([_sys.executable, "-c", _code], cwd=_root)
    except Exception:
        pass


def _review_cached(day):
    """review 进程化读文件：命中文件→返回；否则触发独立进程生成并返 pending。"""
    day = day or df._today_str()
    fp = os.path.join(C.DATA_DIR, "reviews", "review_%s.json" % day)
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                r = json.load(f)
            r["cached"] = True
            return r
        except Exception:
            pass
    _launch_review_proc(day)
    return {"ok": False, "pending": True, "day": day,
            "report": "复盘生成中（独立进程），请稍后刷新", "provider": "pending"}
_REVIEW_API = _cached_api(lambda *a: "review:%s" % (a[0] or ""), 1800)(_review_cached)


# ★ G4（2026-09-13）：客户端提前断开保护——浏览器等不及慢接口主动断开后，
#   _send 不再刷 traceback（历史痛点：socketserver.sendall → ConnectionAbortedError
#   WinError 10053 淹没真实错误；app/tactics.py:1156 注释同源）。断开降级为一行计数，
#   每 100 次汇总一条 audit（event="http_client_abort"）——是"接口太慢"的用户侧信号，
#   与 J2 慢接口治理（api_slow 埋点）互为验证。
_client_abort_count = 0
_client_abort_lock = threading.Lock()


def _bump_client_abort():
    global _client_abort_count
    with _client_abort_lock:
        _client_abort_count += 1
        n = _client_abort_count
    if n % 100 == 0:
        try:
            audit.record(kind="alert", event="http_client_abort", level="WARN",
                         msg="客户端提前断开累计 %d 次（慢接口信号，与 J2 治理互证）" % n)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "TianjiQuant/4.5"
    protocol_version = "HTTP/1.1"     # ★ J1：keep-alive（已发 Content-Length，安全）
    # ★ J2：keep-alive 空闲超时——J1 只设了 protocol_version，未设 timeout：
    #   无连接池的客户端（urllib/轮询波动）每次新建连接，服务器线程被 keep-alive
    #   永久挂起（socket 默认无超时）→ 并发窗口内线程堆积、随机接口排队 ~500ms。
    #   5s 空闲即关闭，线程回收；浏览器连接池内 5s 内复用不受影响。
    timeout = 5

    # ---- 基础 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8",
              cache_control=None, etag=None, last_modified=None,
              pre_gz=None, allow_gzip=True):
        """★ J1：响应发送——gzip 压缩 + 缓存头支持。
        pre_gz: 调用方已压缩字节（静态缓存命中，Content-Encoding: gzip）；
        allow_gzip=False 时禁用自动压缩；cache_control=None → no-cache。
        ★ G4（待接）：本方法末尾 self.wfile.write 的客户端断开保护由 G4 块负责，
           J1 已在此预留位置——G4 合入前，断开异常会走 do_GET 的 except 500 兜底。"""
        data = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        content_encoding = None
        if pre_gz is not None:
            data = pre_gz
            content_encoding = "gzip"
        elif (allow_gzip and len(data) >= _GZ_MIN
                and ctype.startswith(_TEXT_CT)
                and "gzip" in (self.headers.get("Accept-Encoding") or "")):
            data = gzip.compress(data, _GZ_LEVEL_BIG if len(data) > _GZ_BIG_THRESHOLD
                                 else _GZ_LEVEL_SMALL)
            content_encoding = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control or "no-cache")
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        if content_encoding:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        # ★ G4（2026-09-13）：客户端断开保护包裹 end_headers + wfile.write——
        #   浏览器等不及慢接口主动断开 → ConnectionAbortedError/ConnectionResetError/
        #   BrokenPipeError，降级为一行计数（不打印 traceback）；发送错误响应本身
        #   （500 分支调 _send）也不得再抛。
        try:
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            _bump_client_abort()

    def _json(self, obj):
        self._send(200, obj)

    def _read_body(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            ln = 0
        if ln <= 0:
            return {}
        raw = self.rfile.read(ln)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _serve_static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = "/" + path.lstrip("/")
        ent = _load_static(rel)
        if ent is None:
            self._send(404, {"error": "not found"})
            return
        # 304 协商（no-cache 语义=必须 revalidate，ETag/Last-Modified 命中 → 304 不重传）
        inm = (self.headers.get("If-None-Match") or "").strip('"')
        ims = self.headers.get("If-Modified-Since")
        if inm and ent["etag"].strip('"') == inm:
            self.send_response(304)
            self.send_header("Cache-Control", ent["cache_control"])
            self.send_header("ETag", ent["etag"])
            self.end_headers()
            return
        if ims and ent["last_modified"] and ims == ent["last_modified"]:
            self.send_response(304)
            self.send_header("Cache-Control", ent["cache_control"])
            self.send_header("ETag", ent["etag"])
            self.end_headers()
            return
        pre_gz = None
        if ent["gz"] is not None and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            pre_gz = ent["gz"]
        self._send(200, ent["raw"], ent["ctype"],
                   cache_control=ent["cache_control"], etag=ent["etag"],
                   last_modified=ent["last_modified"], pre_gz=pre_gz)

    # ---- 路由 ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._api_get(path[5:], qs)
            else:
                self._serve_static(path)
        except Exception as e:
            log(f"GET {path} 异常: {e}")
            try:
                self._send(500, {"error": str(e)})
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                _bump_client_abort()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                self._api_post(path[5:], self._read_body())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            log(f"POST {path} 异常: {e}")
            try:
                self._send(500, {"error": str(e)})
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                _bump_client_abort()

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # ---- API GET ----
    def _api_get(self, api, qs):
        """★ J2：统一慢接口埋点（api_slow，>API_SLOW_MS 记 audit + 10min P95 TOP5）。"""
        _t0 = time.time()
        try:
            self._api_get_inner(api, qs)
        finally:
            _el = (time.time() - _t0) * 1000.0
            if _el > API_SLOW_MS:
                _note_api_slow(api, _el)

    def _api_get_inner(self, api, qs):
        def g(name, d=""):
            return (qs.get(name) or [d])[0]

        if api == "overview":
            # ★ K8（2026-09-16）：overview 根治——请求线程零取数零重算，
            #   直接读后台单飞刷新的内存快照（TDX 降级/超时时返回旧值+stale，绝不 8s×N 批同步等）。
            self._json(_overview_payload())
        elif api == "experiments":
            # ★ 5.0 实验台账: 读 tools/experiment_scan.py 产出的索引缓存;
            #   ?refresh=1 触发重扫（只读索引器），60s 级响应、不阻塞。
            _idx_path = os.path.join(C.DATA_DIR, "experiments_index.json")
            if g("refresh", "0") == "1":
                try:
                    import subprocess
                    import sys as _sys
                    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    _scan = os.path.join(_root, "tools", "experiment_scan.py")
                    _r = subprocess.run([_sys.executable, _scan],
                                        capture_output=True, timeout=180)
                    if _r.returncode != 0:
                        log("experiments refresh rc=%s: %s" % (
                            _r.returncode, _r.stderr.decode("utf-8", "replace")[:300]))
                except Exception as _e:
                    log("experiments refresh 失败: %s" % _e)
            try:
                with open(_idx_path, encoding="utf-8") as _f:
                    self._json(json.load(_f))
            except Exception as _e:
                self._json({"error": str(_e), "generated_at": "",
                            "counts": {}, "experiments": [], "backlog_risks": []})
        elif api == "sectors":
            spots = sector.fetch_board_spot()
            top = sector.get_top_sectors()
            self._json({"top": top, "count": len(spots),
                        "map_ready": bool(sector.load_sector_map())})
        elif api == "sector/flow":
            # ★ 4.6 板块资金流向（双侧连线图数据）：?type=industry|concept&top=6
            try:
                from . import sector_flow as sf
                _type = g("type", "industry")
                _top = int(g("top", "6") or 6)
                _res = sf.sector_flow(_type, top=_top)
                try:
                    from . import premarket_news as _pn
                    _res = dict(_res)
                    _res["news_sectors"] = _pn.news_sector_hits()
                except Exception:
                    pass
                self._json(_res)
            except Exception as e:
                self._json({"error": str(e), "left": [], "right": [],
                            "links": [], "fetched_at": ""})
        elif api == "news/premarket":
            # ★ 4.7 盘前新闻（多源聚合 + AI 筛选）：?scope=all|global|ashare&min=2&force=1
            # ★ J2：ttl=600s SWR 缓存（force=1 时绕过缓存走实时）
            try:
                _scope = g("scope", "all")
                _min = int(g("min", "2") or 2)
                _force = g("force", "0") == "1"
                if _force:
                    from . import premarket_news as pn
                    self._json(pn.premarket_news(scope=_scope, min_score=_min, force=True))
                else:
                    self._json(_PRE_MKT_API(_scope, _min))
            except Exception as e:
                self._json({"error": str(e), "items": [], "stats": {}})
        elif api == "news/brief":
            # ★ 4.8 盘前简报（开盘自动弹窗）：?force=1 忽略交易日/缓存
            try:
                from . import premarket_news as pn
                _force = g("force", "0") == "1"
                self._json(pn.premarket_brief(force=_force))
            except Exception as e:
                self._json({"enabled": False, "error": str(e), "items": []})
        elif api == "news/sectors":
            # ★ 4.9 新闻板块视图（按行业聚合 + 利好利空）
            try:
                from . import premarket_news as pn
                self._json(pn.sector_view())
            except Exception as e:
                self._json({"error": str(e), "sectors": []})
        elif api == "trading/status":
            # ★ J2：3s SWR（前端 3s 轮询；冷态 2s 来自 sentiment_snapshot 首次——
            #   首请求 ≤200ms 返回占位，后台单飞填真值，永不阻塞）
            self._json(_status_entry())
        elif api == "quotes":
            codes = [c for c in g("codes", "").split(",") if c]
            self._json(df.fetch_quotes(codes))
        elif api == "kline":
            code = g("code")
            period = g("period", "day")
            days = int(g("days", "250") or 250)
            force = g("force", "0") == "1"
            k = df.fetch_kline(code, period, days, force=force)
            name = code
            q = df.fetch_quotes([code])
            if code in q:
                name = q[code].get("name", code)
            self._json({"code": code, "name": name, "period": period, "klines": k})
        elif api == "score":
            code = g("code")
            try:
                k = df.fetch_kline(code, "day", 250)
                q = df.fetch_quotes([code]).get(code, {})
                # ★ P0-1 修复：实盘评分传 code（业绩信号现在真正生效）
                score, signals = sc.score_stock(k, q, code=code)
                # ★ v3.8：资金面加分（龙虎榜/两融/北向）
                mf_bonus, mf_sig = sc.moneyflow_signals(code, q.get("name", ""))
                if mf_bonus:
                    score += mf_bonus
                    signals.extend(mf_sig)
                # 附加打板评分（盘中实时）
                board_score = 0
                if q.get("pct_chg"):
                    from datetime import datetime as _dt
                    hour = _dt.now().hour + _dt.now().minute / 60
                    board_score, _ = sc.score_board(k, q, hour=hour)
                # ★ 4.4 量价综合诊断（量价是决定股价的唯二因素）
                vp_verdict, vp_tags = sc.vp_analysis(k)
                # ★ 4.5 多源信号投票（多智能体决策，6 分析师多空博弈）
                consensus = None
                try:
                    from . import consensus as cs
                    consensus = cs.consensus(code, klines=k, quote=q,
                                             name=q.get("name", ""))
                except Exception:
                    pass
                self._json({"code": code, "score": score, "signals": signals,
                            "board_score": board_score,
                            "vp": vp_verdict, "vp_tags": vp_tags,
                            "consensus": consensus})
            except Exception as e:
                self._json({"code": code, "score": 0, "signals": [f"评分失败: {e}"]})
        elif api == "minute":
            code = g("code")
            k = df.fetch_minute(code)  # 当日1分钟分时（★ 4.4 3秒内存缓存）
            q = df.fetch_quotes([code])
            quote = q.get(code, {})
            # ★ 4.4 分时量：时段量比 + 量能健康度（零额外网络，min5.db 基准）
            mv = {}
            try:
                from . import minute_vol as mvmod
                ratio, verdict = mvmod.minute_vol_check(code, quote)
                mv = {"ratio": round(ratio, 2) if ratio else None, "verdict": verdict}
            except Exception:
                pass
            self._json({"code": code, "quote": quote, "bars": k, "minute_vol": mv,
                                    "date": (k[-1]["date"][:10] if k else "")})
        elif api == "stocklist":
            page = int(g("page", "1"))
            size = min(int(g("size", "50")), 200)
            sort = g("sort", "amount")
            order = g("order", "desc")
            kw = g("kw", "")
            self._json(_stocklist_paged(page, size, sort, order, kw))
        elif api == "backtest/pool":
            # ★ Phase10：回测股票池扩展（★ J2：ttl=600s SWR 缓存）
            kind = g("kind", "top500")
            try:
                self._json(_POOL_API(kind))
            except Exception as e:
                self._json({"error": str(e), "codes": [], "names": {}, "count": 0,
                            "kind": kind})
        elif api == "tactics":
            # 🎯 战法选股（纯展示）：首板回调 + 连板梯队（零下单路径）
            # ★ J2：web 层 ttl=300s SWR（内部 get_tactics_board 已含当日文件缓存+进程化重扫）
            try:
                self._json(_TACTICS_API())
            except Exception as e:
                log("tactics error: %s" % e)
                self._json({"error": str(e), "tactics": [], "coverage": {},
                            "generated_at": ""})
        elif api == "state":
            acct = st.load_account()
            codes = list(acct["positions"].keys())
            quotes = df.fetch_quotes(codes) if codes else {}
            mv = 0.0
            rows = []
            for code, p in acct["positions"].items():
                q = quotes.get(code, {})
                cp = q.get("price", 0) or p["entry_price"]
                pnl = (cp - p["entry_price"]) * p["qty"]
                mv += cp * p["qty"]
                rows.append({**p, "code": code, "price": cp, "pnl": round(pnl, 2),
                             "pnl_pct": round((cp - p["entry_price"]) / p["entry_price"], 4),
                             "pct_chg": q.get("pct_chg", 0), "name": p.get("name", code)})
            total = acct["cash"] + mv
            # ★ 4.5：持仓归因（Brinson 简化版，区分选股/择时/规模贡献）
            attr = None
            try:
                from . import performance as perf
                attr = perf.trade_attribution(acct["trades"])
            except Exception:
                pass
            self._json({"cash": round(acct["cash"], 2), "mv": round(mv, 2),
                        "total": round(total, 2),
                        "pnl": round(total - C.INITIAL_CAPITAL, 2),
                        "pnl_pct": round((total - C.INITIAL_CAPITAL) / C.INITIAL_CAPITAL, 4),
                        "initial": C.INITIAL_CAPITAL,
                        "positions": rows, "trades": acct["trades"][-200:],
                        "attribution": attr})
        elif api == "watchlist":
            self._json(st.load_watchlist())
        elif api == "notify":
            n = st.load_notify()
            self._json({"sendkey": n.get("sendkey", ""), "enabled": bool(n.get("enabled", False)),
                        "wecom_url": n.get("wecom_url", ""), "dingtalk_url": n.get("dingtalk_url", ""),
                        "daily_report": n.get("daily_report", True)})
        elif api == "llm/config":
            # v3.5：LLM 复盘配置读取
            self._json(st.load_llm_config())
        elif api == "commission":
            # ★ 4.3：佣金档位配置读取
            tiers = [{"name": n, "rate": r} for n, r in C.COMMISSION_TIERS.items()]
            self._json({"current": C.COMMISSION_TIER, "tiers": tiers,
                        "rate": C.COMMISSION_RATE,
                        "stamp_tax": C.STAMP_TAX_RATE})
        elif api == "backtest/status":
            jid = g("id")
            with JOBS_LOCK:
                j = JOBS.get(jid)
            if not j:
                self._json({"status": "missing"})
            else:
                self._json(j)
        elif api == "log":
            with LOG_LOCK:
                self._json({"lines": list(LOG_BUF)[-100:]})
        elif api == "risk":
            # v3.4：组合风控状态与审计事件
            self._json(rk.engine.snapshot())
        elif api == "audit":
            # v3.5：交易审计日志查询（?day=YYYY-MM-DD&kind=xxx）
            day = g("day", "") or None
            kind = g("kind", "") or None
            rows = audit.query(day=day, kind=kind, limit=int(g("limit", "500")))
            ok, checked, broken = audit.verify_chain(day)
            summ = audit.daily_summary(day) if day else None
            self._json({"rows": rows[-200:], "total": len(rows),
                        "chain_ok": ok, "chain_checked": checked,
                        "chain_broken_at": broken, "summary": summ})
        elif api == "audit/daily":
            # v3.5：某日审计汇总（日结卡片）
            day = g("day", "") or None
            self._json(audit.daily_summary(day))
        elif api == "review":
            # v3.5：生成复盘报告（LLM 或本地结构化）
            # ★ J2：独立进程生成（写 data/reviews/review_{day}.json），Web 只读文件 + 1800s SWR
            day = g("day", "") or None
            try:
                self._json(_REVIEW_API(day))
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif api == "review/pack":
            # v3.5：复盘数据包（不含 LLM）
            day = g("day", "") or None
            try:
                self._json(rv.build_review_pack(day=day))
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "ai/meeting":
            # ★ 4.5：多智能体投研会议（?code=600519&name=贵州茅台）
            code = g("code", "")
            name = g("name", "")
            try:
                from . import ai
                self._json(ai.research_meeting(code, name))
            except Exception as e:
                self._json({"ok": False, "report": f"会议失败: {e}"})
        elif api == "ai/morning":
            # ★ 4.5：盘前晨报
            try:
                from . import ai
                self._json(ai.morning_brief())
            except Exception as e:
                self._json({"ok": False, "report": f"晨报失败: {e}"})
        elif api == "ai/qa":
            # ★ 4.5：个股 AI 问诊（?code=600519&q=为什么跌&name=贵州茅台）
            code = g("code", "")
            qq = g("q", "分析一下这只股票")
            name = g("name", "")
            try:
                from . import ai
                self._json(ai.stock_qa(code, qq, name))
            except Exception as e:
                self._json({"ok": False, "report": f"问诊失败: {e}"})
        elif api == "ai/health":
            # ★ 4.5：持仓组合 AI 体检
            try:
                from . import ai
                self._json(ai.portfolio_health())
            except Exception as e:
                self._json({"ok": False, "report": f"体检失败: {e}"})
        elif api == "factors":
            # v3.6：因子库列表
            self._json({"factors": [{"name": n, "desc": fac.FACTOR_DESC.get(n, "")}
                                    for n in fac.FACTORS]})
        elif api == "factor/ic":
            # v3.6：因子 IC 分析（?factor=动量10日&codes=600519,000001&horizon=5）
            name = g("factor", "")
            codes = [c for c in g("codes", "").split(",") if c]
            horizon = int(g("horizon", "5"))
            if not name or name not in fac.FACTORS:
                self._json({"error": "未知因子", "available": list(fac.FACTORS.keys())})
                return
            if not codes:
                self._json({"error": "请提供股票池 codes"})
                return
            klines_by_code = {}
            for c in codes:
                try:
                    kl = df.fetch_kline(c, "day", 300)
                    if len(kl) >= 60:
                        klines_by_code[c] = kl
                except Exception:
                    continue
            r = fac.factor_ic(name, klines_by_code, horizon=horizon)
            layers = fac.factor_layers(name, klines_by_code, horizon=horizon)
            mono = fac.factor_monotonicity(layers.get("layers", []))
            r["layers"] = layers.get("layers", [])
            r["monotonicity"] = round(mono, 3)
            self._json(r)
        elif api == "factor/scan":
            # v3.6：全因子 IC 扫描（?codes=...&horizon=5）
            codes = [c for c in g("codes", "").split(",") if c]
            horizon = int(g("horizon", "5"))
            if not codes:
                self._json({"error": "请提供股票池 codes"})
                return
            klines_by_code = {}
            for c in codes:
                try:
                    kl = df.fetch_kline(c, "day", 300)
                    if len(kl) >= 60:
                        klines_by_code[c] = kl
                except Exception:
                    continue
            results = []
            for name in fac.FACTORS:
                r = fac.factor_ic(name, klines_by_code, horizon=horizon)
                if r.get("ic_mean") is not None:
                    results.append(r)
            results.sort(key=lambda x: abs(x.get("ic_mean") or 0), reverse=True)
            self._json({"results": results, "stocks": len(klines_by_code)})
        elif api == "factor/mine":
            # ★ 4.3：LLM 因子挖掘循环（?codes=...&n=5&horizon=5&provider=local|auto）
            codes = [c for c in g("codes", "").split(",") if c]
            n = min(int(g("n", "5")), 10)
            horizon = int(g("horizon", "5"))
            provider = g("provider", "auto")
            if not codes:
                self._json({"error": "请提供股票池 codes"})
                return
            klines_by_code = {}
            for c in codes:
                try:
                    kl = df.fetch_kline(c, "day", 300)
                    if len(kl) >= 60:
                        klines_by_code[c] = kl
                except Exception:
                    continue
            try:
                from . import factor_miner as fm
                r = fm.run_mining_round(klines_by_code, n=n, horizon=horizon,
                                        provider=provider)
                r["stocks"] = len(klines_by_code)
                self._json(r)
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "factor/pool":
            # ★ 4.3：因子池（挖掘历史）
            try:
                from . import factor_miner as fm
                self._json({"factors": fm.load_factor_pool(limit=int(g("limit", "50")))})
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "portfolio/weights":
            # v3.6：组合优化权重（?codes=...&method=risk_parity）
            codes = [c for c in g("codes", "").split(",") if c]
            method = g("method", C.PORTFOLIO_METHOD)
            if not codes:
                self._json({"error": "请提供股票池 codes"})
                return
            closes_by_code = {}
            for c in codes:
                try:
                    kl = df.fetch_kline(c, "day", C.PORTFOLIO_LOOKBACK + 30)
                    if len(kl) >= 30:
                        closes_by_code[c] = [k["close"] for k in kl]
                except Exception:
                    continue
            w = pf.compute_weights(closes_by_code, method=method,
                                   target_vol=C.PORTFOLIO_TARGET_VOL,
                                   days=C.PORTFOLIO_LOOKBACK)
            # 附上名称
            quotes = df.fetch_quotes(codes)
            out = [{"code": c, "name": (quotes.get(c) or {}).get("name", c),
                    "weight": round(wgt, 4)} for c, wgt in sorted(w.items(), key=lambda x: -x[1])]
            self._json({"method": method, "weights": out,
                        "sum": round(sum(w.values()), 4),
                        "stocks": len(closes_by_code)})
        elif api == "moneyflow":
            # ★ v3.8 + 4.0：资金面数据（?code=600036）
            code = g("code", "")
            if not code:
                self._json({"error": "请提供 code"})
                return
            try:
                from . import fflow as ff
                from . import moneyflow as mf
                ff_rows = ff.daily_fflow(code, days=5)
                fflow_trend = None
                if ff_rows:
                    trend, fscore, fdesc = ff.main_inflow_trend(code, days=5)
                    fflow_trend = {"trend": trend, "score": fscore, "desc": fdesc,
                                   "main_latest": ff_rows[0]["main"] if ff_rows else None,
                                   "rows": ff_rows[:3]}
                out = {
                    "code": code,
                    "dragon_tiger": mf.dragon_tiger_of(code, days=30)[:5],
                    "margin": mf.margin_of(code, limit=5),
                    "margin_change": mf.margin_change_ratio(code),
                    "northbound": mf.northbound_of(code, limit=3),
                    "northbound_change": mf.northbound_change(code),
                    "fflow": fflow_trend,   # 4.0 主力资金流
                    "seat": mf.seat_analysis(code),   # ★ 4.5 龙虎榜席位（机构/游资）
                }
                self._json(out)
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "reports":
            # ★ v3.9：历史回测报告列表
            from . import report as rpt
            self._json({"reports": rpt.list_reports(limit=int(g("limit", "50")))})
        elif api == "report":
            # ★ v3.9：单份报告详情（?id=xxx）
            rid = g("id", "")
            from . import report as rpt
            doc = rpt.load_report(rid)
            if doc:
                self._json(doc)
            else:
                self._json({"error": "报告不存在"})
        elif api == "reports/compare":
            # ★ v3.9：多份报告对比（?ids=a,b,c）
            ids = [x for x in g("ids", "").split(",") if x]
            from . import report as rpt
            self._json({"rows": rpt.compare(ids)})
        elif api == "watchgroups":
            # ★ v3.9：自选分组
            self._json({"groups": st.watch_groups()})
        elif api == "alerts":
            # ★ 4.5：价格提醒（条件单）列表
            self._json({"alerts": st.load_price_alerts()})
        elif api == "update":
            # ★ 4.5：数据增量更新状态与手动触发（?run=1 强制更新）
            try:
                from . import updater as up
                if g("run", "0") == "1":
                    threading.Thread(target=up.run_update, kwargs={"verbose": False},
                                     daemon=True).start()
                    self._json({"ok": True, "msg": "后台更新已触发"})
                else:
                    self._json({"ok": True,
                                "needs": up.needs_update(),
                                "running": up._UPDATE_STATE["running"],
                                "last_day": up._UPDATE_STATE["last_day"],
                                "data": {
                                    "min5_latest": _db_max("kline_min5", "min5"),
                                    "day_latest": _db_max("kline", "day"),
                                }})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif api == "sentiment":
            # ★ 4.0：市场情绪周期快照（?date=YYYYMMDD；★ J2：ttl=60s，force=1 绕过）
            date = g("date", "") or None
            try:
                if g("force", "0") == "1":
                    from . import sentiment as senti
                    self._json(senti.sentiment_snapshot(date=date, force=True))
                else:
                    self._json(_SENTI_API(date))
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "sentiment/gate":
            # ★ Phase16：情绪仓位闸门状态 + 各阶段历史统计
            try:
                from . import sentiment_gate as sg
                import json as _json
                phase, mult, lowc = sg.phase_label(use_live=True)
                stats = {}
                try:
                    conn = _db.open_ro(C.DB_FILE, 5000)   # ★ J3：统一连接工厂（读复用）
                    try:
                        row = conn.execute(
                            "SELECT payload FROM qg_sentiment_history "
                            "WHERE period LIKE '2019_%' ORDER BY period DESC LIMIT 1").fetchone()
                        if row:
                            stats = (_json.loads(row[0]) or {}).get("phase_stats") or {}
                    finally:
                        conn.close()
                except Exception:
                    pass
                self._json({
                    "enabled": bool(getattr(C, "SENTIMENT_POS_GATE", False)),
                    "phase": phase, "mult": mult, "low_confidence": lowc,
                    "mult_table": getattr(C, "SENTIMENT_POS_MULT", {}),
                    "stats": stats,
                })
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "global/quotes":
            # ★ Phase18：全球外盘行情快照
            try:
                from . import global_market as gm
                self._json({"quotes": gm.all_quotes(),
                            "updated": __import__("time").strftime("%H:%M:%S")})
            except Exception as e:
                self._json({"error": str(e), "quotes": {}})
        elif api == "global/history":
            # ★ Phase18：指数日K收盘序列（?sym=DJIA&days=60）
            try:
                from . import global_market as gm
                _sym = g("sym", "DJIA") or "DJIA"
                _days = int(g("days", "60") or 60)
                self._json({"sym": _sym, "days": _days,
                            "rows": gm.history(_sym, _days)})
            except Exception as e:
                self._json({"error": str(e), "rows": []})
        elif api == "global/summary":
            # ★ Phase18：全球情绪摘要
            try:
                from . import global_market as gm
                s = gm.summary()
                try:
                    s["a_share_hints"] = gm.a_share_hint()
                except Exception:
                    s["a_share_hints"] = []
                self._json(s)
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "global/movers":
            # ★ 外盘个股异动（|pct|≥2% 降序 top8；不足3只降至1.5%并标注）
            try:
                from . import global_market as gm
                self._json(gm.movers())
            except Exception as e:
                self._json({"error": str(e), "items": []})
        elif api == "global/a_share_hint":
            # ★ Phase19：A股映射提示（纯展示）
            try:
                from . import global_market as gm
                self._json({"hints": gm.a_share_hint()})
            except Exception as e:
                self._json({"error": str(e), "hints": []})
        elif api == "news":
            # ★ 4.5：新闻情绪（场外情绪维度）
            try:
                from . import news as nmod
                self._json({"sentiment": nmod.market_sentiment(),
                            "burst": nmod.news_burst(),
                            "news": nmod.fetch_news(15)})
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "limitup":
            # ★ 4.0 + 4.1：涨停池（?date=YYYYMMDD）
            date = g("date", "") or None
            try:
                from . import limitup as lu
                self._json({
                    "zt": lu.limit_up_pool(date),
                    "dt": lu.limit_down_pool(date),
                    "summary": lu.pool_summary(lu.fetch_pool("zt", date)),
                    "reasons": lu.limit_up_reasons(date)[:60],   # 4.1 涨停原因
                    "themes": lu.theme_burst(date),              # 4.1 题材爆发度
                    "fund_focus": lu.fund_focus(date),           # ★ 4.5 资金聚焦（封板资金）
                })
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "earnings":
            # ★ 4.5：业绩预告榜（?days=14）
            try:
                from . import earnings as ea
                days = int(g("days", "14"))
                self._json({"board": ea.earnings_board(days=days),
                            "count": len(ea.fetch_earnings(days=days))})
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "depth":
            # ★D-S2（2026-09-16，验收方）：十档盘口（面板实盘用）。
            # tdx 优先（含 bid1~5/ask1~5 全量）；腾讯兜底（仅一档，其余 None）。
            code = (g("code") or "").strip()   # ★D-S2：走统一取参器 g()
            if not code.isdigit() or len(code) != 6:
                self._json({"error": "code 需为 6 位纯数字"}); return
            # ★D-S4：2s 微缓存——K线页/持仓页/哨兵同票并发时只打一次源
            _dep_now = time.time()
            _dep_hit = _DEPTH_CACHE.get(code)
            if _dep_hit and _dep_now - _dep_hit[0] < 2.0:
                self._json(_dep_hit[1]); return
            _dep_q, _dep_src = None, ""
            try:
                from . import tdx as _tdxm
                if _tdxm.available():
                    qq = _tdxm.fetch_quotes_fast([code])
                    _dep_q = (qq or {}).get(code)
                    if _dep_q:
                        _dep_src = "tdx"
            except Exception:
                _dep_q = None
            if not _dep_q:
                try:
                    _dep_q = df.fetch_quotes([code], force=True).get(code)
                    _dep_src = "qt" if _dep_q else ""
                except Exception:
                    _dep_q = None
            if not _dep_q:
                self._json({"error": "no data"}); return
            _dep_payload = {
                "code": code, "name": _dep_q.get("name", ""),
                "price": _dep_q.get("price"), "pct": _dep_q.get("pct_chg"),
                "yest_close": _dep_q.get("yest_close"),
                "bids": [{"p": round(_dep_q.get("bid%d_price" % i, 0) or 0, 3),
                          "v": int(_dep_q.get("bid%d_vol" % i, 0) or 0)} for i in range(1, 6)],
                "asks": [{"p": round(_dep_q.get("ask%d_price" % i, 0) or 0, 3),
                          "v": int(_dep_q.get("ask%d_vol" % i, 0) or 0)} for i in range(1, 6)],
                "outer": _dep_q.get("outer_vol"), "inner": _dep_q.get("inner_vol"),
                "src": _dep_src, "ts": int(_dep_now)}
            # ★D-S6：竞价撤单率（eltdx 真口径，best-effort，失败不拦 depth）
            try:
                from . import l2_auction
                _auc = l2_auction.auction_cancel(code)
                if _auc:
                    _dep_payload["auction"] = _auc
            except Exception:
                pass
            _DEPTH_CACHE[code] = (_dep_now, _dep_payload)
            self._json(_dep_payload)
        elif api == "sentiment/history":
            # ★ 4.1：情绪历史序列与 IC（首次重建 ~60s，之后 30 分钟缓存）
            # ★ J2：改走统一 _SENTI_HIST_API（1800s SWR；冷启动由 tools/warm_cache.py 预热）
            try:
                self._json(_SENTI_HIST_API())
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "sentiment/eco":
            # ★ K4：涨停生态温度展示（只读）：zt_ecosystem 最近 N 个完整交易日温度 + qg_zt_full 状态
            try:
                from . import zt_ecosystem as _ze
                as_of = g("as_of", "") or None
                days = min(120, max(5, int(g("days", "20"))))
                _eco_rows = _ze.recent_temperatures(days=days, as_of=as_of)
                _conn2 = _db.open_ro(C.DB_FILE, 5000)
                try:
                    _qrow = _conn2.execute(
                        "SELECT COUNT(*), MAX(date) FROM qg_zt_full").fetchone()
                finally:
                    _conn2.close()
                self._json({
                    "recent_days": _eco_rows,
                    "qg_zt_full": {"rows": _qrow[0], "max_date": _qrow[1]},
                    "as_of": as_of,
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "calendar":
            # ★ 4.2：交易日历（?date=YYYY-MM-DD 判断，?start=&end= 区间）
            date = g("date", "")
            start = g("start", "")
            end = g("end", "")
            try:
                from . import trading_calendar as tcal
                out = {"today": df._today_str(),
                       "is_trading": tcal.is_trading_day(date) if date else None,
                       "next": tcal.next_trading_day(date) if date else None,
                       "prev": tcal.prev_trading_day(date) if date else None}
                if start and end:
                    out["trading_days"] = tcal.trading_days(start, end)
                self._json(out)
            except Exception as e:
                self._json({"error": str(e)})
        elif api == "data_health":
            # ★ D3（2026-09-13）：数据健康只读面板。60s TTL 缓存；
            #   断库/锁定→回退上次缓存 stale=True；接口内零写入。
            self._json(_data_health_cached())
        else:
            self._send(404, {"error": f"unknown api {api}"})

    # ---- API POST ----
    def _api_post(self, api, body):
        if api == "trade":
            side = body.get("side")
            code = body.get("code", "")
            qty = int(body.get("qty", 0) or 0)
            price = body.get("price")
            try:
                price = float(price) if price else None
            except (ValueError, TypeError):
                price = None
            if side == "buy":
                ok, msg = st.manual_buy(code, qty, price, body.get("name", ""))
            else:
                ok, msg = st.manual_sell(code, qty, price)
            log(f"手动交易 {side} {code} {qty}股: {msg}")
            self._json({"ok": ok, "msg": msg})
        elif api == "reset":
            st.reset_account()
            log("账户已重置")
            self._json({"ok": True})
        elif api == "watchlist":
            act = body.get("action")
            code = body.get("code", "")
            if act == "add":
                ok, msg = st.add_watch(code, body.get("name", ""), body.get("group", ""))
                self._json({"ok": ok, "msg": msg})
            elif act == "remove":
                st.remove_watch(code)
                self._json({"ok": True})
            elif act == "group":
                # ★ v3.9：移动自选股到分组
                ok = st.set_watch_group(code, body.get("group", "默认"))
                self._json({"ok": ok})
            else:
                self._json({"ok": False, "msg": "unknown action"})
        elif api == "alerts":
            # ★ 4.5：价格提醒（条件单）增删
            act = body.get("action")
            if act == "add":
                a = st.add_price_alert(body.get("code", ""), body.get("name", ""),
                                       body.get("direction", "up"), body.get("price", 0))
                log(f"条件单新增: {a['name']}({a['code']}) {a['direction']} {a['price']}")
                self._json({"ok": True, "alert": a})
            elif act == "remove":
                st.remove_price_alert(body.get("id", ""))
                self._json({"ok": True})
            elif act == "rearm":
                # 重新启用已触发的提醒
                alerts = st.load_price_alerts()
                for a in alerts:
                    if a.get("id") == body.get("id", ""):
                        a["enabled"] = True
                        a["triggered"] = False
                        a.pop("trigger_time", None)
                st.save_price_alerts(alerts)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "msg": "unknown action"})
        elif api == "notify":
            st.save_notify({"sendkey": body.get("sendkey", ""),
                            "enabled": bool(body.get("enabled", False)),
                            "wecom_url": body.get("wecom_url", ""),
                            "dingtalk_url": body.get("dingtalk_url", ""),
                            "daily_report": bool(body.get("daily_report", True))})
            log("通知配置已保存")
            self._json({"ok": True})
        elif api == "llm":
            # v3.5：LLM 复盘配置保存
            st.save_llm_config({
                "ollama_url": body.get("ollama_url", "http://127.0.0.1:11434"),
                "ollama_model": body.get("ollama_model", ""),
                "api_key": body.get("api_key", ""),
                "api_base": body.get("api_base", "https://api.deepseek.com/v1"),
                "model": body.get("model", ""),
            })
            log("LLM 配置已保存")
            self._json({"ok": True})
        elif api == "commission":
            # ★ 4.3：佣金档位保存
            tier = body.get("tier", "")
            ok = st.save_commission_tier(tier)
            log(f"佣金档位已保存: {tier} -> {C.COMMISSION_RATE}")
            self._json({"ok": ok, "rate": C.COMMISSION_RATE})
        elif api == "audit/daily":
            # v3.5：手动触发收盘日结推送
            day = body.get("day", "") or None
            try:
                summ = al.daily_report(day)
                self._json({"ok": True, "summary": summ})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif api == "review":
            # v3.5：生成复盘报告（★ J2：POST 也走独立进程，请求即时返回 pending）
            day = body.get("day", "") or None
            day = day or df._today_str()
            try:
                _launch_review_proc(day)
                self._json({"ok": True, "pending": True, "day": day,
                            "report": "复盘生成中（独立进程），请稍后刷新", "provider": "pending"})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif api == "trading/start":
            ok, msg = trader.engine.start(auto=bool(body.get("auto", False)))
            self._json({"ok": ok, "msg": msg})
        elif api == "trading/stop":
            ok, msg = trader.engine.stop()
            self._json({"ok": ok, "msg": msg})
        elif api == "trading/scan":
            def _run():
                try:
                    trader.engine.scan_once(verbose=True)
                except Exception as e:
                    log(f"手动扫描异常: {e}")
            threading.Thread(target=_run, daemon=True).start()
            self._json({"ok": True, "msg": "扫描已启动（后台）"})
        elif api == "trading/twothirty":
            # v3.4：两点半战法手动扫描（可随时触发验证）
            def _run2():
                try:
                    trader.engine._twothirty_round(verbose=True)
                except Exception as e:
                    log(f"两点半扫描异常: {e}")
            threading.Thread(target=_run2, daemon=True).start()
            self._json({"ok": True, "msg": "两点半战法扫描已启动（后台）"})
        elif api == "backtest":
            jid = start_backtest(body)
            log(f"回测任务启动 {jid} ({body.get('strategy','score')}, {len(body.get('codes',[]))}只)")
            self._json({"job_id": jid})
        else:
            self._send(404, {"error": f"unknown api {api}"})


class Server(ThreadingHTTPServer):
    """★ K7-2（2026-09-16）：覆写 handle_error——客户端提前断开类异常静默
    （计数进 http_client_abort 已有事件，G4 块），其余异常照旧打印。
    背景：慢接口超时时浏览器主动断开 → socketserver 在 sendall 抛
    ConnectionAbortedError / ConnectionResetError / BrokenPipeError，
    若再打印 traceback 会淹没真实错误（G4 已在 _send/do_GET 内层兜底，
    本处是服务器线程级最后一道防线）。"""

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            _bump_client_abort()
            return
        super().handle_error(request, client_address)


def serve(host="127.0.0.1", port=8899):
    _warm_static_gz()   # ★ J1：启动时预压缩 vendor 大文件（echarts.min.js 1MB 级）
    _start_overview_refresher()   # ★ K8：overview 后台单飞刷新（daemon，永不阻塞请求线程）
    srv = Server((host, port), Handler)
    srv.daemon_threads = True
    _start_warmup_thread(port)   # ★ K5：启动预热（tactics 独立 subprocess + 本地 HTTP）
    return srv


# ---- ★ K8（2026-09-16）：overview 快照 + 分段打点 ----
# 背景：overview 分支原实现每请求同步取 600 只行情（TDX→腾讯→新浪补缺链），
# TDX 降级/超时时请求线程同步等 8s×N 批 → 首屏超时/板块黄条/看护 l2 误判（P0-1）。
# 方案：后台单飞刷新（daemon 线程，仅一个在途）+ 内存快照；请求线程零取数零重算。
#   行情侧 fetch_quotes_overview 30s 缓存自然限频；快照 age>10s 响应显式标 stale（判据⑤）。
_OV_SNAP = {"ts": 0.0, "data": None, "phases": {}, "refreshing": False,
            "_quality_cache": None, "_last_quality_ts": 0.0}
_OV_LOCK = threading.Lock()
_OV_LAST_AUDIT = 0.0
_OV_REFRESH_INTERVAL = 5.0    # 快照续新轮询间隔（秒）
_OV_STALE_AFTER = 10.0        # 快照 age>10s → 响应标 stale
_OV_QUALITY_TTL = 60.0        # quality 段重算节流（读 audit jsonl 较贵）
_OV_AUDIT_INTERVAL = 60.0     # overview_phase_latency 审计节流（防刷屏）


def _ov_compute_once():
    """一轮完整刷新：各段打点（indices/quotes/breadth/regime/quality）。
    任何段失败不阻断整体（取数失败退空，与改造前行为一致）。"""
    phases = {}
    _t = time.time()
    try:
        idx = df.fetch_indices()
    except Exception:
        idx = []
    phases["indices_ms"] = round((time.time() - _t) * 1000.0, 1)
    _t = time.time()
    try:
        lst = df.get_stock_list()
        codes = [c for c, _, _ in lst[:600]]
        quotes = df.fetch_quotes_overview(codes)   # 30s SWR 缓存，未命中才取数
    except Exception:
        lst, quotes = [], {}
    phases["quotes_ms"] = round((time.time() - _t) * 1000.0, 1)
    _t = time.time()
    try:
        breadth = sc.calc_breadth(quotes)
    except Exception:
        breadth = 0.0
    phases["breadth_ms"] = round((time.time() - _t) * 1000.0, 1)
    _t = time.time()
    try:
        regime, mp, thr, pp = sc.get_regime(breadth)
    except Exception:
        regime, mp, thr, pp = ("unknown", 0, 0, 0)
    phases["regime_ms"] = round((time.time() - _t) * 1000.0, 1)
    # quality：60s 节流重算（D1 红条数据源，延迟 ≤60s 可接受）
    _t = time.time()
    with _OV_LOCK:
        _qdata = _OV_SNAP.get("_quality_cache")
        _last_q = _OV_SNAP.get("_last_quality_ts", 0.0)
    if _qdata is None or time.time() - _last_q >= _OV_QUALITY_TTL:
        try:
            _qdata = audit.quality_alert_summary()
        except Exception:
            _qdata = {"banner_text": "", "items": [], "escalated": [],
                      "generated_at": ""}
        with _OV_LOCK:
            _OV_SNAP["_quality_cache"] = _qdata
            _OV_SNAP["_last_quality_ts"] = time.time()
    phases["quality_ms"] = round((time.time() - _t) * 1000.0, 1)
    payload = {"indices": idx, "breadth": round(breadth, 4),
               "regime": regime, "max_pos": mp, "threshold": thr,
               "position_pct": pp, "market_count": len(lst),
               "quality": _qdata,
               "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    return payload, phases


def _ov_refresh_worker():
    payload, phases = _ov_compute_once()
    with _OV_LOCK:
        _OV_SNAP["ts"] = time.time()
        _OV_SNAP["data"] = payload
        _OV_SNAP["phases"] = phases
    global _OV_LAST_AUDIT
    if time.time() - _OV_LAST_AUDIT >= _OV_AUDIT_INTERVAL:
        _OV_LAST_AUDIT = time.time()
        try:
            audit.record("api", "overview_phase_latency", level="INFO", phases=phases)
        except Exception:
            pass


def _ov_start_refresh():
    """单飞：同一时刻只允许一个刷新任务。"""
    with _OV_LOCK:
        if _OV_SNAP["refreshing"]:
            return
        _OV_SNAP["refreshing"] = True

    def _run():
        try:
            _ov_refresh_worker()
        except Exception as e:
            log(f"overview 刷新失败: {e}")
        finally:
            with _OV_LOCK:
                _OV_SNAP["refreshing"] = False
    threading.Thread(target=_run, daemon=True).start()


def _overview_payload():
    """请求线程入口：零取数零重算。冷态 → pending（客户端按 retry_after_ms 重试）；
    快照 age>10s → stale=true（判据⑤）；超过续新间隔 → 后台续新（SWR）。"""
    with _OV_LOCK:
        snap = _OV_SNAP["data"]
        ts = _OV_SNAP["ts"]
    if snap is None:
        _ov_start_refresh()
        return {"pending": True, "retry_after_ms": 3000}
    age = time.time() - ts
    out = dict(snap)
    if age > _OV_STALE_AFTER:
        out["stale"] = True
    if age > _OV_REFRESH_INTERVAL:
        _ov_start_refresh()
    return out


def _start_overview_refresher():
    """serve() 启动 daemon 轮询：每 5s 检查快照是否需要续新。"""
    def _loop():
        while True:
            try:
                with _OV_LOCK:
                    snap = _OV_SNAP["data"]
                    ts = _OV_SNAP["ts"]
                if snap is None or time.time() - ts > _OV_REFRESH_INTERVAL:
                    _ov_start_refresh()
            except Exception:
                pass
            time.sleep(_OV_REFRESH_INTERVAL)
    threading.Thread(target=_loop, daemon=True).start()


def _start_warmup_thread(port):
    """★ K5（2026-09-16）：服务启动后 daemon 预热线程。
    - tactics：独立 subprocess（tools.warm_cache._warm_tactics），零 Web 进程 GIL 占用；
    - stocklist / sentiment/history：本地 HTTP 触发内存缓存填充（等端口就绪）。
    看护（watchdog）重启 = 进程重启 → serve() 执行 → 自动预热，首个用户不吃冷启动。"""
    def _w():
        try:
            import socket
            import tools.warm_cache as _wc
            try:
                _wc._warm_tactics()     # 独立进程，不占 Web 进程
            except Exception:
                pass
            # 本地 HTTP 预热：等服务端口就绪（main.py 在 serve() 之后才 serve_forever）
            for _i in range(60):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.5)
            _wc._warm_http(port, "/api/tactics", "tactics")
            _wc._warm_http(port, "/api/stocklist?page=1&size=50", "stocklist")
            _wc._warm_http(port, "/api/sentiment/history", "sentiment/history")
        except Exception:
            pass
    threading.Thread(target=_w, daemon=True).start()


def find_free_port(prefer=8899):
    import socket
    for p in range(prefer, prefer + 30):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            continue
    return prefer


# 后台预热板块映射（不阻塞启动）
sector.warm_up()


def _warm_sentiment_history():
    """★ 4.1：后台预热情绪历史（首次 66s，预热后 API 秒回）"""
    try:
        from . import sentiment_series as ss
        pool = ss.rebuild_history(days=250)
        series = ss.build_sentiment_series(pool)
        ic = ss.sentiment_ic(series)
        global _SENTI_HIST_CACHE
        _SENTI_HIST_CACHE = (time.time(), {
            "days": len(series), "ic": ic, "recent": series[-30:]})
        log(f"情绪历史预热完成: {len(series)}天 IC={ic.get('ic_mean')}")
    except Exception as e:
        log(f"情绪历史预热失败: {e}")


threading.Thread(target=_warm_sentiment_history, daemon=True).start()
