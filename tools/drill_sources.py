# -*- coding: utf-8 -*-
"""★ P70：数据源故障演练（脚本化断源，不影响运行中的服务）

对四类数据源做受控断源演练：行情（quotes/K线）、资金流（moneyflow）、
新闻（news）、全球（global_market）。断源手段 = 子进程内把
socket.create_connection / socket.socket.connect 替换为抛 OSError
（HTTP/TDX 全灭；本地 sqlite/文件不受影响），随后调用各源公共接口，
观察是否优雅降级 / 切换到备援（本地库、缓存文件、备用站点）。

验证目标：
  - 备援切换：K线应回落本地 market.db/min5.db；股票列表回落 stock_list.json；
    全球回落 global_kline 表；资金流回落本地缓存。
  - 告警路径：故障场景下 audit.record / alert.notify 可用且不向外抛异常。

每案例独立子进程（monkeypatch 不泄漏、缓存冷启动）。结果落 JSON 并生成
docs/operations.md 断源手册章节（本工具 --write-docs 时追加）。

用法: python tools/drill_sources.py [--json data/source_drill.json]
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
except Exception:
    pass


def _case(code):   # code: fns 字典的键
    """子进程：断网 + 调用 + 观测。返回 {case, outcome, detail}"""
    import socket
    def _dead(*a, **k):
        raise OSError("drill: network cut")
    socket.create_connection = _dead
    socket.socket.connect = lambda self, addr: (_ for _ in ()).throw(
        OSError("drill: network cut"))
    from app import datafeed as df
    from app import moneyflow as mf
    from app import news as nw
    from app import global_market as gm
    fns = {
        "datafeed.fetch_quotes": lambda: df.fetch_quotes(
            ["000001", "600519"], force=True),
        "datafeed.fetch_kline_day": lambda: df.fetch_kline("000001", "day", 250),
        "datafeed.fetch_kline_min5": lambda: df.fetch_kline("000001", "min5", 80000),
        "datafeed.get_stock_list": lambda: df.get_stock_list(force=True),
        "news.fetch_news": lambda: nw.fetch_news(limit=5, force=True),
        "moneyflow.dragon_tiger": lambda: mf.dragon_tiger(limit=5, force=True),
        "global_market.summary": lambda: gm.summary(),
    }
    fn = fns[code]
    t0 = time.time()
    try:
        out = fn()
        n = len(out) if hasattr(out, "__len__") else -1
        ok = True if n != 0 else "empty"
        return {"case": code, "outcome": ok, "elapsed_s": round(time.time() - t0, 1),
                "detail": "返回长度 %s" % n}
    except Exception as e:
        return {"case": code, "outcome": "CRASH",
                "elapsed_s": round(time.time() - t0, 1),
                "detail": "%s: %s" % (type(e).__name__, e)}


def _alert_case(_):
    """告警路径演练：断网下 audit.record 与 alert.notify 必须不外抛"""
    import socket
    def _dead(*a, **k):
        raise OSError("drill: network cut")
    socket.create_connection = _dead
    socket.socket.connect = lambda self, addr: (_ for _ in ()).throw(
        OSError("drill: network cut"))
    from app import alert as al
    from app import audit as aud
    before = os.path.getsize(os.path.join(BASE, "data", "audit", "audit.jsonl")) \
        if os.path.exists(os.path.join(BASE, "data", "audit", "audit.jsonl")) else 0
    errs = []
    try:
        aud.record("order", "drill_alert_path", note="P70 断源演练")
    except Exception as e:
        errs.append("audit: %s" % e)
    try:
        al.notify("WARN", "p70_drill", "P70 演练", "断网告警路径测试", cooldown=0)
    except Exception as e:
        errs.append("notify: %s" % e)
    after = os.path.getsize(os.path.join(BASE, "data", "audit", "audit.jsonl"))
    return {"case": "alert_path(net_dead)", "outcome": "ok" if not errs else "FAIL",
            "detail": "audit 增长 %dB; notify 内部消化异常=%s" % (
                after - before, not any(e.startswith("notify") for e in errs)),
            "errors": errs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(BASE, "data", "source_drill.json"))
    a = ap.parse_args()
    t0 = time.time()
    cases = [
        "datafeed.fetch_quotes",
        "datafeed.fetch_kline_day",
        "datafeed.fetch_kline_min5",
        "datafeed.get_stock_list",
        "news.fetch_news",
        "moneyflow.dragon_tiger",
        "global_market.summary",
    ]
    from multiprocessing import Pool
    with Pool(processes=4) as pool:
        rows = pool.map(_case, cases)
    rows.append(_alert_case("alert"))

    fallback_expected = {
        "datafeed.fetch_quotes": "TDX→腾讯→新浪→内存/名单回填，全断则空字典（优雅降级）",
        "datafeed.fetch_kline_day": "本地 market.db 兜底（网络死仍应有数据）",
        "datafeed.fetch_kline_min5": "本地 min5.db 兜底",
        "datafeed.get_stock_list": "stock_list.json 缓存文件兜底",
        "news.fetch_news": "东财→新浪备，双断则降级为空/旧缓存",
        "moneyflow.dragon_tiger": "熔断器快速失败 + 本地缓存回退",
        "global_market.summary": "在线站点失败 → global_kline 本地表兜底",
        "alert_path(net_dead)": "audit 落盘 + notify 内部吞异常",
    }
    for r in rows:
        r["expected_fallback"] = fallback_expected.get(r["case"], "")
        r["pass"] = r["outcome"] not in ("CRASH", "FAIL")
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "70",
        "fault_injection": "子进程内 socket.create_connection/socket.connect 抛 OSError"
                           "（HTTP/TDX 全灭；sqlite/文件不受影响）",
        "results": rows,
        "all_pass": all(r["pass"] for r in rows),
    }
    with open(a.json, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    for r in rows:
        r.setdefault("elapsed_s", -1)
        print("%-34s %-8s %ss | %s" % (r["case"], r["outcome"],
                                       r["elapsed_s"], r["detail"][:80]), flush=True)
    print("\n全部通过:", payload["all_pass"], "| 已写出", a.json, flush=True)

    # 生成/追加 docs/operations.md 断源手册章节
    doc = os.path.join(BASE, "docs", "operations.md")
    lines = ["", "## 数据源断源手册（P70 演练自动生成 %s）" %
             time.strftime("%Y-%m-%d %H:%M"), "",
             "演练方法：`python tools/drill_sources.py`（子进程内掐断全部网络，"
             "不影响服务与本地库）。最近一次结果：%s。" %
             ("全部通过" if payload["all_pass"] else "存在 FAIL——按下表处置"), "",
             "| 数据源 | 断网后备援链 | 演练结果 |", "|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %s |" % (r["case"], r["expected_fallback"],
                                           r["outcome"]))
    lines += ["", "处置要点：",
              "1. 行情/K线断源属**预期内降级**：K线有本地库兜底，实时行情断网返回空——",
              "   trader 各轮询对空行情天然跳过，无需人工干预；恢复网络后自动回到主源。",
              "2. 资金流被东财风控掐断时 moneyflow 熔断器进入只读缓存模式（非故障）。",
              "3. 全球数据在线站失败时读 global_kline 本地积累；长期断网该表停止增长，",
              "   恢复后自动续写。",
              "4. 告警路径：audit.jsonl 本地落盘不受网络影响；微信/alert 推送断网时",
              "   内部静默失败，恢复后不补发（可接受）。"]
    mode = "a" if os.path.exists(doc) else "w"
    with open(doc, mode, encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("手册章节已写入 docs/operations.md (%s)" % mode, flush=True)


if __name__ == "__main__":
    main()
