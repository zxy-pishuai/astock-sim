# -*- coding: utf-8 -*-
"""★ 任务4 score act12_T4 PIT 复验（唯一活候选）— R1 加固版 2026-08-31

快照: data/snapshots/2026-08-27/market.db（任务书指定；含 D3 退市宇宙与 2019 前回补，
      PIT 池 2019-20 的退市股均有 K 线 → 无偏 PIT 口径；08-26 缺退市股 K 线，
      若回退到它则降级为"仅在市"口径，报告必须声明）
      回退链: 2026-08-27 → 2026-08-30 → 2026-08-26（后两者同样声明口径差异）
口径: DATA_EXCLUDE_CODES=196 同快照同日运行；PIT 池按窗注入（pit_pools.json 每窗 500，
      已过滤 196）；static 池 = bt_pool.json 前 500 经 196 过滤后 458 只；
      两池分别跑 base 与两变体。
变体（score 策略）: base_score {} / act8_T4 {0.08,-0.04,T4} / act12_T4 {0.12,-0.05,T4}。
判定（预注册，以 PIT 列为准，静态仅对照）:
  - ≥3/4 窗 Δ≥−1pp（loss_pp≤1，judge_kit）且牛市 Δ≤+2pp → 落地级；Δ=变体−基线，正=改善
  - 防伪（B3/T4 教训）: 任一改善窗（loss_pp<−1）的 gated 交易数相对同窗 ungated 缩减
    >50% → 记伪改善嫌疑，该窗不计入达标数。
加固（R1 整改，2026-08-31）:
  - 主入口断言 OUT_JSON basename == bt_act12_t4_pit.json 且不含 'c_lane'；每次写盘前再断言
  - 逐格增量落盘（每完成一格 read-modify-write），中断可断点续跑（已存格跳过）
  - ProcessPool workers（默认 4），BrokenProcessPool 时剩余格回退顺序执行
  - 运行日志由启动器重定向至 tmp/act12_run.log / tmp/act12_err.log（禁落项目根）
用法: python tools/act12_t4_pit.py [--workers 4] [--mode process|sequential] [--limit N]
输出: data/bt_act12_t4_pit.json
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 任务书指定 08-27；回退链随后
SNAPSHOT_CANDIDATES = ["2026-08-27", "2026-08-30", "2026-08-26"]
OUT_JSON = os.path.join(BASE, "data", "bt_act12_t4_pit.json")

WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-21"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "牛市"]
PIT_WIN_MAP = ["2019-20", "2021-22", "2023-24", "近1年"]
PARAMS_SCORE = {"buy_threshold": 25, "max_positions": 3, "position_pct": 0.30,
                "slippage": 0.001, "zt_eco_gate": False, "dd_gate": False}

A1 = {"TRAILING_ACTIVATE_PCT": 0.08, "TRAILING_STOP_PCT": -0.04}
A2 = {"TRAILING_ACTIVATE_PCT": 0.12, "TRAILING_STOP_PCT": -0.05}

VARIANTS = [
    ("act8_T4", dict(A1, TIME_STOP_DAYS=4)),
    ("act12_T4", dict(A2, TIME_STOP_DAYS=4)),
]

_G = {}


def _resolve_snapshot():
    for d in SNAPSHOT_CANDIDATES:
        p = os.path.join(BASE, "data", "snapshots", d, "market.db")
        if os.path.isfile(p):
            return p, d
    return None, None


def _filtered_pool(codes):
    from app import config as C
    excl = set(getattr(C, "DATA_EXCLUDE_CODES", []) or [])
    return [c for c in codes if c not in excl]


def _init_worker(snapshot):
    """worker 初始化：DB 指向冻结快照 + PIT/static 池载入（含 196 过滤）"""
    from app import config as C
    import json as _j
    _G["snap"] = snapshot
    C.DB_FILE = snapshot
    pit = _j.load(open(os.path.join(C.DATA_DIR, "pit_pools.json"), encoding="utf-8"))
    _G["pit_pools"] = pit["pools"]
    _G["pit_limitation"] = pit.get("limitation", "")
    _G["pit_generated_at"] = pit.get("generated_at", "")
    bp = _j.load(open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8"))["codes"][:500]
    _G["static_codes"] = _filtered_pool(bp)


def _run_one(task):
    pool_kind, label, patches, widx = task
    from app import config as C
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    if pool_kind == "pit":
        key = PIT_WIN_MAP[widx]
        codes = _filtered_pool(_G["pit_pools"][key]["codes"])
    else:
        codes = _G["static_codes"]
    names = {c: c for c in codes}
    t0 = time.time()
    orig = {k: getattr(C, k) for k in patches}
    orig_q = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}
    try:
        for k, v in patches.items():
            setattr(C, k, v)
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, "score", dict(PARAMS_SCORE))
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_q
        for k, v in orig.items():
            setattr(C, k, v)
    return {"pool": pool_kind, "variant": label, "widx": widx, "window": WIN_NAMES[widx],
            "pit_key": PIT_WIN_MAP[widx] if pool_kind == "pit" else "static",
            "n_codes": len(codes),
            "total_return": r.get("total_return"),
            "max_drawdown": r.get("max_drawdown"),
            "sharpe": r.get("sharpe"),
            "trade_count": r.get("trade_count"),
            "win_rate": r.get("win_rate"),
            "elapsed": round(time.time() - t0, 1)}


def _dump(out):
    """原子落盘 + 每次写盘前路径断言（防再犯 c_lane 覆盖事故）"""
    assert os.path.basename(OUT_JSON) == "bt_act12_t4_pit.json", OUT_JSON
    assert "c_lane" not in OUT_JSON, OUT_JSON
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)


def _load_existing():
    """幂等续跑：载入已落盘 results（{pool/variant/widx: cell}）"""
    if not os.path.isfile(OUT_JSON):
        return {}
    try:
        with open(OUT_JSON, encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return {}
    res = {}
    for k, v in (j.get("results") or {}).items():
        # key 形如 'pit/act12_T4/w0'
        parts = k.split("/")
        if len(parts) == 3:
            pool, lab, w = parts[0], parts[1], parts[2]
            if w.startswith("w") and w[1:].isdigit():
                res[(pool, lab, int(w[1:]))] = v
    return res


def _build_out(results, meta, t0):
    import tools.judge_kit as jk

    def build(pool_kind):
        out = []
        for lab, _p in VARIANTS:
            losses, details, wins_ok = [], [], 0
            for wi in range(4):
                b = results.get((pool_kind, "base", wi))
                v = results.get((pool_kind, lab, wi))
                if not b or not v or b.get("total_return") is None or v.get("total_return") is None:
                    details.append({"win": WIN_NAMES[wi], "error": "missing-cell"})
                    continue
                loss_pp = ((b["total_return"] or 0) - (v["total_return"] or 0)) * 100
                bt = b.get("trade_count") or 0
                vt = v.get("trade_count") or 0
                shrink = (bt - vt) / bt if bt else 0
                fake = shrink > 0.5 and loss_pp < -1  # 改善窗且交易缩减>50%
                losses.append(loss_pp)
                if not fake:
                    wins_ok += 1 if jk.window_pass([loss_pp], tol_loss_pp=1.0, min_windows=1)[0] else 0
                details.append({"win": WIN_NAMES[wi], "ret": v["total_return"],
                                "base_ret": b["total_return"],
                                "loss_pp": round(loss_pp, 2),
                                "trades": "%s->%s" % (bt, vt),
                                "shrink_pct": round(shrink * 100, 1),
                                "pseudo": fake})
            bull_loss = losses[3] if len(losses) == 4 else None
            passed = bool(losses) and len(losses) == 4 and jk.window_pass(
                losses, tol_loss_pp=1.0, min_windows=3)[1] and jk.bull_ok(bull_loss, max_loss_pp=2.0)
            pseudo_any = any(d.get("pseudo") for d in details)
            out.append({"variant": lab, "pool": pool_kind, "detail": details,
                        "losses_pp": [round(x, 2) for x in losses],
                        "windows_ok": wins_ok,
                        "bull_loss_pp": round(bull_loss, 2) if bull_loss is not None else None,
                        "pass": passed, "pseudo_suspect": pseudo_any})
        return out

    pit_j = build("pit")
    static_j = build("static")
    beautify = []
    for wi, wn in enumerate(WIN_NAMES):
        b_pit = (results.get(("pit", "base", wi)) or {}).get("total_return")
        b_sta = (results.get(("static", "base", wi)) or {}).get("total_return")
        for lab, _ in VARIANTS:
            v_pit = (results.get(("pit", lab, wi)) or {}).get("total_return")
            v_sta = (results.get(("static", lab, wi)) or {}).get("total_return")
            beautify.append({"window": wn, "variant": lab,
                             "pit_delta_pp": round((v_pit - b_pit) * 100, 2) if v_pit is not None and b_pit is not None else None,
                             "static_delta_pp": round((v_sta - b_sta) * 100, 2) if v_sta is not None and b_sta is not None else None,
                             "overstatement_pp": round(((v_sta - b_sta) - (v_pit - b_pit)) * 100, 2) if v_pit is not None and v_sta is not None and b_pit is not None and b_sta is not None else None})
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "act12_T4 PIT 复验（R1 加固）",
        "meta": meta,
        "pit_limitation": _G.get("pit_limitation", ""),
        "pit_generated_at": _G.get("pit_generated_at", ""),
        "results": {f"{pool}/{lab}/w{wi}": results.get((pool, lab, wi)) for (pool, lab, wi) in results},
        "judgement_pit": pit_j,
        "judgement_static": static_j,
        "beautify": beautify,
        "cells_done": len(results),
        "cells_total": 24,
        "elapsed_s": round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--mode", choices=["process", "sequential"], default="process")
    ap.add_argument("--limit", type=int, default=0, help="调试：只跑前 N 格")
    a = ap.parse_args()
    t0 = time.time()

    SNAPSHOT, snap_name = _resolve_snapshot()
    if not SNAPSHOT:
        print("无可用冻结快照:", SNAPSHOT_CANDIDATES, flush=True)
        return 1
    print("[db] 固定冻结快照: %s (%s)" % (SNAPSHOT, snap_name), flush=True)

    _init_worker(SNAPSHOT)

    # ---- 自证 PIT 口径 ----
    from app import config as C
    excl = set(getattr(C, "DATA_EXCLUDE_CODES", []) or [])
    print("[pit] pools", list(_G["pit_pools"].keys()), "generated", _G["pit_generated_at"], flush=True)
    print("[pit] limitation:", _G["pit_limitation"][:200], flush=True)
    for k in PIT_WIN_MAP:
        print("  [pit/%s] raw=%d filtered196=%d" % (
            k, len(_G["pit_pools"][k]["codes"]),
            len([c for c in _G["pit_pools"][k]["codes"] if c not in excl])), flush=True)
    print("[static] bt_pool top500 filtered 196 -> %d" % len(_G["static_codes"]), flush=True)
    print("[universe] DATA_EXCLUDE_CODES=%d (002594 已移出，196)" % len(excl), flush=True)

    # ---- 任务队列：base + 两变体 × 4窗 × 2池 = 24 格 ----
    tasks = []
    for pool_kind in ("pit", "static"):
        for widx in range(4):
            tasks.append((pool_kind, "base", {}, widx))
            for lab, patches in VARIANTS:
                tasks.append((pool_kind, lab, patches, widx))
    if a.limit > 0:
        tasks = tasks[:a.limit]

    # 幂等：跳过已落盘且有效的格（existing 键 = (pool, variant, widx)）
    existing = _load_existing()
    def _key(t):
        return (t[0], t[1], t[3])
    existing_keys = set(existing.keys())
    pending = [t for t in tasks if _key(t) not in existing_keys]
    print("[resume] existing=%d pending=%d (of %d tasks)" % (len(existing), len(pending), len(tasks)), flush=True)

    results = dict(existing)
    meta = {"snapshot": SNAPSHOT, "snapshot_name": snap_name,
            "excluded_codes": 196, "params_score": PARAMS_SCORE,
            "pit_map": PIT_WIN_MAP, "windows": WINDOWS, "win_names": WIN_NAMES,
            "method": "冻结快照+C.DB_FILE+196宇宙+PIT窗池注入+judge_kit（PIT为准，伪改善>50%剔除）",
            "variants": [{"label": l, "patches": p} for l, p in VARIANTS],
            "exec_mode": a.mode, "workers": a.workers,
            "prereg": "≥3/4窗 Δ≥-1pp 且牛窗 Δ≤+2pp 落地级；改善窗交易缩减>50%记伪改善嫌疑"}

    def _save():
        _dump(_build_out(results, meta, t0))

    def _record(r):
        key = (r["pool"], r["variant"], r["widx"])
        results[key] = r
        _save()

    def _record_err(t, e):
        pk, lab, _p, wi = t
        results[(pk, lab, wi)] = {"pool": pk, "variant": lab, "widx": wi,
                                  "window": WIN_NAMES[wi], "error": repr(e)}
        _save()

    done_n = 0
    if a.mode == "process" and pending:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from concurrent.futures.process import BrokenProcessPool
        try:
            ex = ProcessPoolExecutor(max_workers=a.workers, initializer=_init_worker,
                                     initargs=(SNAPSHOT,))
            futs = {ex.submit(_run_one, t): t for t in pending}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    r = fut.result()
                    _record(r)
                    print("完成 [%s/%s/%s] ret=%.4f trades=%s elapsed=%.1fs" % (
                        r["pool"], r["variant"], r["window"], r["total_return"] or 0,
                        r["trade_count"], r["elapsed"]), flush=True)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    _record_err(t, e)
                    print("cell crash [%s/%s/%s]: %r" % (t[0], t[1], WIN_NAMES[t[3]], e), flush=True)
                done_n += 1
                if done_n % 6 == 0:
                    print("进度 %d/%d 累计 %.0fs" % (done_n, len(pending), time.time() - t0), flush=True)
            ex.shutdown(wait=True)
        except BrokenProcessPool:
            import traceback
            traceback.print_exc()
            print("!! ProcessPool 断裂，剩余格回退顺序执行", flush=True)
            # 已完成并落盘的格跳过，剩余未消费的改顺序
            rest = [t for t in pending if _key(t) not in results]
            for t in rest:
                try:
                    r = _run_one(t)
                    _record(r)
                    print("完成(seq) [%s/%s/%s] ret=%.4f trades=%s elapsed=%.1fs" % (
                        r["pool"], r["variant"], r["window"], r["total_return"] or 0,
                        r["trade_count"], r["elapsed"]), flush=True)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    _record_err(t, e)
                    print("cell crash(seq) [%s/%s/%s]: %r" % (t[0], t[1], WIN_NAMES[t[3]], e), flush=True)
                done_n += 1
    else:
        for t in pending:
            try:
                r = _run_one(t)
                _record(r)
                print("完成 [%s/%s/%s] ret=%.4f trades=%s elapsed=%.1fs" % (
                    r["pool"], r["variant"], r["window"], r["total_return"] or 0,
                    r["trade_count"], r["elapsed"]), flush=True)
            except Exception as e:
                import traceback
                traceback.print_exc()
                _record_err(t, e)
                print("cell crash [%s/%s/%s]: %r" % (t[0], t[1], WIN_NAMES[t[3]], e), flush=True)
            done_n += 1
            if done_n % 6 == 0:
                print("进度 %d/%d 累计 %.0fs" % (done_n, len(pending), time.time() - t0), flush=True)

    # ---- 判定输出（以 PIT 为准，静态对照）----
    out = _build_out(results, meta, t0)
    _dump(out)
    print("\n===== PIT 判定（落地以此为准） =====", flush=True)
    for j in out["judgement_pit"]:
        dd = "; ".join("%s loss=%spp trades=%s%s" % (x["win"], x.get("loss_pp"), x.get("trades"), " [伪]" if x.get("pseudo") else "") for x in j["detail"])
        print("[pit/%s] 达标窗=%s 牛市损=%spp pass=%s pseudo=%s\n    %s" % (j["variant"], j["windows_ok"], j["bull_loss_pp"], j["pass"], j["pseudo_suspect"], dd), flush=True)
    print("\n===== 静态对照 =====", flush=True)
    for j in out["judgement_static"]:
        dd = "; ".join("%s loss=%spp" % (x["win"], x.get("loss_pp")) for x in j["detail"])
        print("[static/%s] 达标窗=%s 牛市损=%spp pass=%s" % (j["variant"], j["windows_ok"], j["bull_loss_pp"], j["pass"]), flush=True)
    print("\nsaved", OUT_JSON, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
