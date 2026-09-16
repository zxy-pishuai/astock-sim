# -*- coding: utf-8 -*-
"""I3 回测复现性护栏：结果指纹 + 禁网守卫 + 环境锁定。

用法：
  py -3.13 tools/bt_fingerprint.py run --codes 000001,600519 --start 2026-01-01 \
      --end 2026-08-31 [--strategy score] [--threshold 60] [--capital 100000] \
      [--tag 说明] [--db <数据源库>]
  py -3.13 tools/bt_fingerprint.py --verify <run_id> [--db <数据源库>]
  py -3.13 tools/bt_fingerprint.py --guard-test
  py -3.13 tools/bt_fingerprint.py --list

设计要点：
- 每次 run 落一条结构化记录到 data/bt_runs.jsonl（ts/参数全量/codes 指纹/数据指纹/
  依赖版本指纹/结果/结果哈希/禁网守卫计数）。
- --verify 重跑比对结果哈希，不一致时按 参数→数据→依赖 顺序归因。
- 禁网守卫（仅回测上下文）：monkeypatch urllib.request.urlopen/urlretrieve，
  任何外发请求抛异常并计数；回测 100% 离线，杜绝"断网降级导致不可复现"。
- date.today() 依赖已由 I1 的 as_of 注入消除（回测可达路径审计见报告）。
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app import config as C  # noqa: E402
from app import engine as eng  # noqa: E402

RUNS_FILE = os.path.join(_ROOT, getattr(C, "BT_FINGERPRINT_FILE", "data/bt_runs.jsonl"))
# 回测实际读 C.DB_FILE（datafeed.fetch_kline 锚定生产库，只读使用）；
# BT_DEFAULT_DB 仅在显式配置时覆盖（如快照回测）。
DEFAULT_DB = os.path.join(_ROOT, getattr(C, "BT_DEFAULT_DB", "") or C.DB_FILE)
OFFLINE_ATTEMPTS = {"n": 0, "stacks": []}
_ORIG_URLOPEN = urllib.request.urlopen
_ORIG_URLRETRIEVE = getattr(urllib.request, "urlretrieve", None)


def install_offline_guard():
    """安装禁网守卫（仅回测上下文）：任何 urllib 外发请求 → 抛异常并计数。"""
    def _guard_open(*a, **kw):
        OFFLINE_ATTEMPTS["n"] += 1
        import traceback
        OFFLINE_ATTEMPTS["stacks"].append("".join(traceback.format_stack()[-8:]))
        raise RuntimeError(
            "I3 禁网守卫: 回测进程禁止外发请求 urlopen(%s) —— 回测必须 100%% 离线可复现"
            % (a[0] if a else kw))
    urllib.request.urlopen = _guard_open
    if _ORIG_URLRETRIEVE is not None:
        def _guard_retrieve(*a, **kw):
            OFFLINE_ATTEMPTS["n"] += 1
            import traceback
            OFFLINE_ATTEMPTS["stacks"].append("".join(traceback.format_stack()[-8:]))
            raise RuntimeError("I3 禁网守卫: 回测进程禁止 urlretrieve(%s)" % (a[0] if a else ""))
        urllib.request.urlretrieve = _guard_retrieve
    # requests（若安装且回测路径用到）一并拦截
    try:
        import requests
        _orig_req = requests.sessions.Session.request

        def _guard_req(self, method, url, *a, **kw):
            OFFLINE_ATTEMPTS["n"] += 1
            import traceback
            OFFLINE_ATTEMPTS["stacks"].append("".join(traceback.format_stack()[-8:]))
            raise RuntimeError("I3 禁网守卫: 回测进程禁止 requests.%s(%s)" % (method, url))
        requests.sessions.Session.request = _guard_req
    except Exception:
        pass
    return OFFLINE_ATTEMPTS


def restore_online():
    urllib.request.urlopen = _ORIG_URLOPEN
    if _ORIG_URLRETRIEVE is not None:
        urllib.request.urlretrieve = _ORIG_URLRETRIEVE


def sha256s(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deps_fingerprint():
    """关键依赖版本指纹（pip freeze 关键包）。"""
    pkgs = ["numpy", "pandas", "akshare", "mootdx", "tdxpy", "requests"]
    out = {"python": platform.python_version()}
    for p in pkgs:
        try:
            out[p] = importlib.metadata.version(p)
        except Exception:
            out[p] = None
    return out


def data_fingerprint(db):
    """数据指纹：MAX(date) + day 行数 + 覆盖率 + 内容聚合（SUM close/volume/amount）。
    内容聚合使指纹对库内任何一行数据修改敏感（如改 close），供 --verify 归因。"""
    fp = {"db": db, "hash": None, "fp_ver": 2}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        fp["max_date"] = c.execute(
            "SELECT MAX(date) FROM kline WHERE period='day'").fetchone()[0]
        fp["day_rows"] = c.execute(
            "SELECT COUNT(*) FROM kline WHERE period='day'").fetchone()[0]
        fp["distinct_codes"] = c.execute(
            "SELECT COUNT(DISTINCT code) FROM kline WHERE period='day'").fetchone()[0]
        if fp["max_date"]:
            fp["cov_codes"] = c.execute(
                "SELECT COUNT(DISTINCT code) FROM kline WHERE period='day' AND date=?",
                (fp["max_date"],)).fetchone()[0]
        else:
            fp["cov_codes"] = 0
        fp["ratio"] = round(fp["cov_codes"] / fp["distinct_codes"], 4) if fp["distinct_codes"] else 0
        # 内容聚合（SQLite SUM 表序固定 → 确定性；double 精度对 ~9e7 量级安全）
        row = c.execute(
            "SELECT COALESCE(SUM(close),0), COALESCE(SUM(volume),0), "
            "COALESCE(SUM(amount),0) FROM kline WHERE period='day'").fetchone()
        fp["sum_close"], fp["sum_volume"], fp["sum_amount"] = (round(row[0], 4),
                                                               round(row[1], 2),
                                                               round(row[2], 2))
        c.close()
        fp["hash"] = sha256s(json.dumps(
            {"fp_ver": fp["fp_ver"],
             **{k: fp[k] for k in ("max_date", "day_rows", "distinct_codes", "cov_codes",
                                   "ratio", "sum_close", "sum_volume", "sum_amount")}},
            sort_keys=True))
    except Exception as e:
        fp["error"] = str(e)
    return fp


def result_hash(result):
    sub = {k: result.get(k) for k in
           ("total_return", "trade_count", "win_rate", "max_drawdown", "sharpe")}
    return sha256s(json.dumps(sub, sort_keys=True))


def record_run(run_id, tag, params, params_hash, codes_hash, data_fp, deps_fp,
               result, res_hash, offline_n, ts):
    rec = {
        "run_id": run_id, "ts": ts, "tag": tag,
        "params": params, "params_hash": params_hash,
        "codes": params.get("codes", []), "codes_hash": codes_hash,
        "data_fp": data_fp, "deps_fp": deps_fp,
        "result": result, "result_hash": res_hash,
        "offline_guard": {"urlopen_attempts": offline_n},
    }
    os.makedirs(os.path.dirname(RUNS_FILE), exist_ok=True)
    with open(RUNS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_runs():
    if not os.path.exists(RUNS_FILE):
        return []
    out = []
    with open(RUNS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _norm_code(c):
    """K6（2026-09-16）：A 股代码规范化——PowerShell 会把裸 `000001` 解析为数字 1
    再字符串化成 '1'（前缀零丢失，实测 argv=['--codes','1,600519']），导致 codes_hash
    与回测对象错成无效代码（'1' 查不到 K 线 → 该票被静默跳过，回测失真且"一致地错"）。
    防御：纯数字且 <6 位 → zfill(6) 补零；带前缀（sh000001）或 ≥6 位原样保留。
    codes_hash 始终基于规范化后的字符串列表计算。"""
    c = c.strip()
    if c.isdigit() and len(c) < 6:
        return c.zfill(6)
    return c


def build_params(args):
    codes = [_norm_code(c) for c in args.codes.split(",") if c.strip()]
    params = {
        "codes": codes,
        "start": args.start, "end": args.end,
        "strategy": args.strategy,
        "capital": args.capital,
        "buy_threshold": args.threshold,
        "seed": getattr(args, "seed", 42),
        "db": args.db,
    }
    if args.tag:
        params["tag"] = args.tag
    return params


def params_hash_of(params):
    """参数指纹：排除 tag（运行备注，非回测参数）。"""
    p = {k: v for k, v in params.items() if k != "tag"}
    return sha256s(json.dumps(p, sort_keys=True))


def run_backtest(args, tag=None):
    params = build_params(args)
    params_hash = params_hash_of(params)
    codes_hash = sha256s(",".join(params["codes"]))
    OFFLINE_ATTEMPTS["n"] = 0
    OFFLINE_ATTEMPTS["stacks"] = []
    offline_n = install_offline_guard()
    _orig_db = C.DB_FILE
    _orig_min5 = C.MIN5_DB_FILE
    try:
        # --db 显式传入时切换回测实际数据源（engine/datafeed 读它）；默认=生产库只读
        if args.db:
            C.DB_FILE = args.db
            C.MIN5_DB_FILE = ""
        bt = eng.Backtest(params["codes"], {}, params["start"], params["end"],
                          params["capital"], params["strategy"],
                          {"buy_threshold": params["buy_threshold"], "seed": params["seed"]})
        result = bt.run()
    finally:
        C.DB_FILE = _orig_db
        C.MIN5_DB_FILE = _orig_min5
        restore_online()
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError("回测失败: %s" % result["error"])
    res_hash = result_hash(result)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    run_id = "%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), hashlib.md5(
        (ts + json.dumps(params)).encode()).hexdigest()[:4])
    data_fp = data_fingerprint(params["db"])
    deps_fp = deps_fingerprint()
    rec = record_run(run_id, tag or params.get("tag"), params, params_hash, codes_hash,
                     data_fp, deps_fp, result, res_hash, offline_n["n"], ts)
    return rec


def verify_run(run_id, args):
    runs = {r["run_id"]: r for r in load_runs()}
    if run_id not in runs:
        print("✗ run_id 不存在: %s（可用 --list 查看）" % run_id)
        return 1
    old = runs[run_id]
    print("=== 重跑（禁网守卫生效）===")
    new_params = dict(old["params"])
    if args.db:
        new_params["db"] = args.db
    ns = argparse.Namespace(
        codes=",".join(new_params["codes"]),
        start=new_params["start"], end=new_params["end"],
        strategy=new_params["strategy"], threshold=new_params["buy_threshold"],
        capital=new_params["capital"], seed=new_params.get("seed", 42),
        db=new_params.get("db", ""), tag="verify:%s" % run_id)
    rec = run_backtest(ns, tag="verify:%s" % run_id)
    print("run_id        : %s" % run_id)
    print("旧 result_hash: %s" % old["result_hash"])
    print("新 result_hash: %s" % rec["result_hash"])
    ok = old["result_hash"] == rec["result_hash"]
    print("结果哈希      : %s" % ("✓ 复现一致" if ok else "✗ 不一致 → 归因："))
    if ok:
        return 0
    if old["params_hash"] != rec["params_hash"]:
        print("  → 参数不同（params_hash 不匹配）")
    elif old["data_fp"].get("fp_ver") != rec["data_fp"].get("fp_ver"):
        print("  → 指纹定义版本不同（护栏升级，非数据漂移；旧记录 fp_ver=%s 新=%s）" % (
            old["data_fp"].get("fp_ver"), rec["data_fp"].get("fp_ver")))
    elif old["data_fp"].get("hash") != rec["data_fp"].get("hash"):
        print("  → 数据指纹不同（%s vs %s）" % (old["data_fp"].get("hash"),
                                           rec["data_fp"].get("hash")))
    elif old["deps_fp"] != rec["deps_fp"]:
        print("  → 依赖版本不同")
    else:
        print("  → 参数/数据/依赖均相同但结果不同：代码路径漂移（查禁网守卫计数/库锁）")
    return 1


def guard_test():
    n = install_offline_guard()
    try:
        try:
            urllib.request.urlopen("http://127.0.0.1:1/x")
            print("✗ 守卫未生效（urlopen 未抛异常）")
            return 1
        except RuntimeError as e:
            print("✓ urlopen 被拦: %s" % str(e)[:60])
        try:
            if _ORIG_URLRETRIEVE is not None:
                urllib.request.urlretrieve("http://127.0.0.1:1/x", "tmp/i3/x")
                print("✗ urlretrieve 未拦")
                return 1
            print("✓ urlretrieve 未安装，跳过")
        except RuntimeError:
            print("✓ urlretrieve 被拦")
        # 本地 sqlite 只读不受守卫影响
        db = DEFAULT_DB
        c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        n_max = c.execute("SELECT MAX(date) FROM kline WHERE period='day'").fetchone()[0]
        c.close()
        print("✓ 本地只读 sqlite 不受影响（MAX(date)=%s）" % n_max)
        print("守卫计数=%d" % n["n"])
        return 0
    finally:
        restore_online()


def main():
    ap = argparse.ArgumentParser(description="I3 回测复现性护栏")
    sub = ap.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="跑一次带指纹的回测")
    p_run.add_argument("--codes", required=True)
    p_run.add_argument("--start", required=True)
    p_run.add_argument("--end", required=True)
    p_run.add_argument("--strategy", default="score")
    p_run.add_argument("--threshold", type=int, default=60)
    p_run.add_argument("--capital", type=float, default=100000.0)
    p_run.add_argument("--seed", type=int, default=42)
    p_run.add_argument("--db", default=DEFAULT_DB)
    p_run.add_argument("--tag", default="")
    sub.add_parser("guard-test", help="禁网守卫生效自检")
    sub.add_parser("list", help="列出最近运行")
    p_v = sub.add_parser("verify", help="复现校验")
    p_v.add_argument("run_id")
    p_v.add_argument("--db", default="")
    args = ap.parse_args()

    if args.cmd == "guard-test":
        sys.exit(guard_test())
    if args.cmd == "list":
        runs = load_runs()
        print("共 %d 条运行记录:" % len(runs))
        for r in runs[-10:]:
            print("  %s  %s  %s  codes=%d  result_hash=%s" % (
                r["run_id"], r["ts"], r.get("tag", ""),
                len(r.get("codes", [])), r["result_hash"][:12]))
        sys.exit(0)
    if args.cmd == "verify":
        sys.exit(verify_run(args.run_id, args))
    if args.cmd == "run":
        rec = run_backtest(args)
        print("run_id   : %s" % rec["run_id"])
        print("result   : total_return=%.4f trade_count=%d win_rate=%.4f "
              "max_drawdown=%.4f sharpe=%.3f" % (
                  rec["result"].get("total_return", 0),
                  rec["result"].get("trade_count", 0),
                  rec["result"].get("win_rate", 0),
                  rec["result"].get("max_drawdown", 0),
                  rec["result"].get("sharpe", 0)))
        print("res_hash : %s" % rec["result_hash"])
        print("data_fp  : max=%s rows=%d cov=%d/%d ratio=%.4f hash=%s" % (
            rec["data_fp"].get("max_date"), rec["data_fp"].get("day_rows"),
            rec["data_fp"].get("cov_codes"), rec["data_fp"].get("distinct_codes"),
            rec["data_fp"].get("ratio"), rec["data_fp"].get("hash")))
        print("deps_fp  : %s" % json.dumps(rec["deps_fp"], ensure_ascii=False))
        print("guard    : urlopen_attempts=%d（0=全程离线）" % rec["offline_guard"]["urlopen_attempts"])
        sys.exit(0)
    ap.print_help()


if __name__ == "__main__":
    main()
