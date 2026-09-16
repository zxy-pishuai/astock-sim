#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E块 F1 qfq 跨段污染批修复工具（草案） —— 只读对账产物之一，不 apply

用途
----
读取 data/qfq_batch_state.json 的 queueA_detail（短窗接缝未修复队列），逐票：
  - --check（默认）：只读体检。kline 现状接缝扫描 + （若 mootdx 可用）重建接缝预测，
    判定该票 rebuild 是否可清（clean）/ 被守卫拦截（blocked）/ 无 xdxr。
  - --apply：代码在案，但需【人工获批后】执行。批前哈希备份 → wait_quiet →
    逐票 repair_one()（import 复用，唯一写库入口）→ 批后复检 → 批级报告。

红线遵守
--------
1. 不复制第二套写库逻辑：写库唯一入口 = repair_qfq.repair_one()（import 复用）。
2. 默认跳过指数噪声（sz399006）与队列外代码。
3. 不写 progress.json / config.py / backlog.md —— 更新留给验收方。
4. --apply 前打印醒目授权提示并二次确认（stdin）后才执行。

环境
----
mootdx 仅在 py -3.13（C:\\Users\\26838\\AppData\\Local\\Programs\\Python\\Python313\\python.exe）
可用；本工具 --check 的“重建预测”需要 mootdx + 网络，缺省则退化为仅现状扫描。
执行批次请使用：py -3.13 tools/qfq_batch_repair.py ...
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "market.db")
DEFAULT_STATE = os.path.join(BASE, "data", "qfq_batch_state.json")
DEFAULT_BACKUP_DIR = os.path.join(BASE, "data", "backups")
DEFAULT_OUT_DIR = os.path.join(BASE, "tmp", "qfq_f1")
SEAM_THRESHOLD = 0.21  # 与 repair_qfq 守卫一致（主板口径，对创科更严=保守）
QFQ_SCAN_FROM = "2019-01-01"

# 复用模板函数（工具目录入 path；禁复制粘贴其写库逻辑）
sys.path.insert(0, os.path.join(BASE, "tools"))
try:
    from repair_002594 import wait_quiet, db_mtime, fetch_db_rows  # noqa: F401
    from repair_qfq import fetch_raw, make_qfq, repair_one  # noqa: F401
    _HAVE_REPAIR = True
except Exception as _e:  # pragma: no cover
    _HAVE_REPAIR = False
    _IMPORT_ERR = _e

try:
    from mootdx.utils.adjust import get_xdxr
    _HAVE_MOOTDX = True
except Exception:
    _HAVE_MOOTDX = False


# --------------------------------------------------------------------------
# 只读查询
# --------------------------------------------------------------------------
def _ro_connect():
    """market.db 只读连接（URI mode=ro）。"""
    return __import__("sqlite3").connect("file:%s?mode=ro" % DB, uri=True, timeout=30)


def load_queue(state_path=None, codes=None, skip_index=True):
    """读 state.json 的 queueA_detail；--codes 覆盖。返回 [dict]。"""
    state_path = state_path or DEFAULT_STATE
    st = json.load(open(state_path, encoding="utf-8"))
    detail = st.get("queueA_detail") or st.get("final_queue", {}).get("detail") or []
    if skip_index:
        detail = [d for d in detail if not str(d.get("code", "")).startswith("sz")]
    if codes:
        cs = set(codes)
        detail = [d for d in detail if str(d.get("code", "")) in cs]
    return detail


def scan_current_seams(code, threshold=SEAM_THRESHOLD):
    """只读：扫描 kline 该票 2019 起连续行 > threshold 的跳变（接缝现状）。"""
    con = _ro_connect()
    try:
        rows = con.execute(
            "SELECT date, open, close FROM kline WHERE code=? AND period='day' "
            "AND date>=? ORDER BY date",
            (code, QFQ_SCAN_FROM)).fetchall()
    finally:
        con.close()
    seams = []
    prev = None
    for date, open_, close in rows:
        if prev and prev[2] and prev[2] > 0:
            j = (open_ - prev[2]) / prev[2]
            if abs(j) > threshold:
                seams.append({"prev_date": prev[0], "date": date,
                              "open": open_, "prev_close": prev[2],
                              "jump_pct": round(j * 100, 2)})
        prev = (date, open_, close)
    return seams


def predict_rebuild(code):
    """只读：若 mootdx 可用，重建该票并返回 2019+ 接缝数（模拟 repair_one 守卫）。
    返回 None 表示无法预测（无 mootdx/网络/无 xdxr/原始数据异常）。"""
    if not (_HAVE_REPAIR and _HAVE_MOOTDX):
        return None
    try:
        raw = fetch_raw(code)
        if raw is None or len(raw) < 10:
            return {"ok": False, "reason": "raw_short(%d)" % (len(raw) if raw is not None else 0)}
        xdxr = get_xdxr(code)
        if xdxr is None or len(xdxr) == 0:
            return {"ok": False, "reason": "no_xdxr"}
        adj = make_qfq(raw, xdxr)
        if adj is None or len(adj) < 10:
            return {"ok": False, "reason": "adj_short"}
        gaps = 0
        prev = None
        n = 0
        for r in adj:
            if r["date"] < QFQ_SCAN_FROM:
                prev = r
                continue
            n += 1
            if n > 5 and prev and prev.get("close") and prev["close"] > 0:
                if abs((r["open"] - prev["close"]) / prev["close"]) > SEAM_THRESHOLD:
                    gaps += 1
            prev = r
        return {"ok": gaps == 0, "rebuild_gaps_2019": gaps, "rows": len(adj)}
    except Exception as e:
        return {"ok": False, "reason": "error:%s" % type(e).__name__}


def batch_backup(codes, batch_id, backup_dir=DEFAULT_BACKUP_DIR):
    """批前哈希备份：读所有目标票 day 行 → 单 JSON（codes 字典 + sha256）。非写库。"""
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, "kline_day_%s_before_qfq_batch_%s.json" % (batch_id, ts))
    con = _ro_connect()
    try:
        payload = {"batch_id": batch_id, "created_at": ts, "reason": "qfq batch repair backup",
                   "codes": {}, "sha256": {}}
        for code in codes:
            rows = con.execute(
                "SELECT date, open, high, low, close, volume, amount FROM kline "
                "WHERE code=? AND period='day' ORDER BY date", (code,)).fetchall()
            recs = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
                     "close": r[4], "volume": r[5], "amount": r[6]} for r in rows]
            blob = json.dumps(recs, ensure_ascii=False, sort_keys=True).encode("utf-8")
            payload["codes"][code] = recs
            payload["sha256"][code] = hashlib.sha256(blob).hexdigest()
    finally:
        con.close()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return path


def post_check(codes, threshold=SEAM_THRESHOLD):
    """批后哨兵复检（只读）：每票 2019+ 相邻跳变 > threshold 的事件数。"""
    out = {}
    for code in codes:
        seams = scan_current_seams(code, threshold=threshold)
        out[code] = {"high_seam_count": len(seams),
                     "max_abs_jump_pct": max((abs(s["jump_pct"]) for s in seams), default=0.0)}
    return out


def write_report(data, batch_id, out_dir=DEFAULT_OUT_DIR):
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, "batch_%s_report_%s.json" % (batch_id, ts))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return path


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run_check(detail, dry_n=None, out_dir=DEFAULT_OUT_DIR):
    batch_id = "check_%s" % datetime.now().strftime("%Y%m%d_%H%M%S")
    items = detail[:dry_n] if dry_n else detail
    results = []
    for d in items:
        code = d["code"]
        cur = scan_current_seams(code)
        pred = predict_rebuild(code)
        verdict = "unknown"
        if pred is None:
            verdict = "mootdx_na" if not _HAVE_MOOTDX else "pred_na"
        elif pred.get("ok"):
            verdict = "clean"
        else:
            _reason = pred.get("reason", "")
            if _reason == "no_xdxr":
                verdict = "blocked_no_xdxr"
            elif _reason in ("raw_short", "adj_short"):
                verdict = "blocked_raw_short"
            elif _reason.startswith("error:"):
                verdict = "pred_error"
            else:
                verdict = "blocked_gaps(%d)" % pred.get("rebuild_gaps_2019", 0)
        results.append({
            "code": code, "name": d.get("name"), "excluded": d.get("already_excluded"),
            "in_progress": d.get("in_progress"),
            "current_seams": cur, "current_max_abs_jump_pct":
                max((abs(s["jump_pct"]) for s in cur), default=0.0),
            "rebuild_prediction": pred, "verdict": verdict,
        })
        print("%s %-8s verdict=%-18s current_seams=%d max=%.2f%% | %s" % (
            code, d.get("name", ""), verdict, len(cur),
            results[-1]["current_max_abs_jump_pct"],
            pred or ""))
    report = {"mode": "check", "batch_id": batch_id, "generated_at":
              datetime.now().isoformat(timespec="seconds"), "results": results}
    p = write_report(report, batch_id, out_dir)
    print("\ncheck 报告: %s" % p)
    return p


def run_apply(detail, batch_id=None, backup_dir=DEFAULT_BACKUP_DIR, out_dir=DEFAULT_OUT_DIR):
    """--apply：需人工获批后执行。写库唯一入口=repair_one()。"""
    if not _HAVE_REPAIR:
        print("错误: 无法导入 repair_qfq/repair_002594（_IMPORT_ERR=%s）" % _IMPORT_ERR)
        sys.exit(2)
    print("\n!! 本模式将写入 data/market.db（逐票全量重写 day 期）。")
    print("!! 需人工获批后执行。输入大写 YES 确认继续：")
    if sys.stdin.readline().strip() != "YES":
        print("已取消（未写入）。")
        return
    batch_id = batch_id or ("b%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    codes = [d["code"] for d in detail]
    print("\n[1/4] 批前 wait_quiet...")
    try:
        wait_quiet(window_sec=90, max_wait=900)
    except Exception as e:
        print("wait_quiet 异常: %s（继续）" % e)
    print("[2/4] 批前哈希备份 %d 只 → backups/" % len(codes))
    bak = batch_backup(codes, batch_id, backup_dir)
    print("  备份: %s" % bak)
    print("[3/4] 逐票 repair_one() ...")
    per = []
    for d in detail:
        code = d["code"]
        t0 = time.time()
        try:
            ok, msg, replaced, gaps = repair_one(code)
        except Exception as e:
            ok, msg, replaced, gaps = False, "exception:%s" % type(e).__name__, 0, None
        per.append({"code": code, "name": d.get("name"), "ok": ok, "msg": msg,
                    "replaced": replaced, "gaps": gaps, "elapsed_s": round(time.time() - t0, 2)})
        print("  %s %-8s ok=%s msg=%s" % (code, d.get("name", ""), ok, msg))
        # 票间 mtime 复查：库被写则中止后续
        try:
            if db_mtime() != db_mtime():
                print("  !! 批中库 mtime 变化，中止后续票")
                break
        except Exception:
            pass
    print("[4/4] 批后哨兵复检（只读）...")
    pc = post_check([x["code"] for x in per])
    report = {"mode": "apply", "batch_id": batch_id, "generated_at":
              datetime.now().isoformat(timespec="seconds"), "backup": bak,
              "per_code": per, "post_check": pc}
    p = write_report(report, batch_id, out_dir)
    print("\napply 报告: %s" % p)
    return p


def main():
    ap = argparse.ArgumentParser(description="E块 qfq 批量修复工具（草案）")
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--codes", nargs="*", default=None, help="覆盖队列（空格分隔的代码）")
    ap.add_argument("--no-index", action="store_true", default=True, help="默认跳过指数(sz)代码")
    ap.add_argument("--apply", action="store_true", help="执行写库（需获批+二次确认）")
    ap.add_argument("--dry", type=int, default=None, help="--check 只查前 N 只")
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    detail = load_queue(args.state, args.codes, skip_index=True)
    if not detail:
        print("队列为空（state=%s, codes=%s）。" % (args.state, args.codes))
        sys.exit(1)
    print("队列 %d 只（--apply=%s）：%s" % (
        len(detail), args.apply, ",".join(d["code"] for d in detail)))

    if args.apply:
        run_apply(detail, args.batch_id, args.backup_dir, args.out_dir)
    else:
        run_check(detail, args.dry, args.out_dir)


if __name__ == "__main__":
    main()
