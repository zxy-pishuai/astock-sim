# -*- coding: utf-8 -*-
"""F1（2026-09-13）实时性隔离 验收测试。

判据（预注册）：
1. 单元级 single-flight：monkeypatch _http=sleep(3) 假实现 + tdx 禁用 →
   并发 8 线程请求同一批 codes → 假 _http 只被调用 1 次（腾讯批；新浪无需补缺），
   8 线程都拿到结果、最大等待 < QUOTE_SINGLEFLIGHT_WAIT_S + 1s。
2. 隔离级：假 _http 让展示通道 sleep(30)，同时交易通道请求 →
   交易通道在 TRADING_FETCH_DEADLINE_S + 1s 内返回（展示慢请求不阻塞交易）。
3. 回归级：fetch_quotes 返回值结构（键集）与 _parse_tencent 直接解析同一批
   完全一致（tdx 禁用 → 纯 HTTP 路径），取 50 只实测，字段级 diff 为空。

运行：python tools/test_f1_singleflight.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import datafeed as df

# ---------- 假实现 ----------
_FAKE_CALLS = []          # (ts, url[:60])
_FAKE_HTTP_SLEEP = 3.0    # 单元级：每次假请求 sleep 3s
_ORIG_HTTP = df._http     # 真实 _http（case3 回归用）
_ORIG_PARSE_TENCENT = df._parse_tencent


def _fake_http(url, decode="utf-8", ref=None, timeout=None):
    _FAKE_CALLS.append((time.time(), url[:70]))
    t = _FAKE_HTTP_SLEEP
    if "display" in threading.current_thread().name:
        t = 30.0          # 隔离级：展示通道挂 30s
    time.sleep(t)
    return "v_sh600000=1~浦发银行~600000~9.35~9.26~9.35~9.22~9.26~653273~60463~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0__t_hang__"  # 假文本（_parse_tencent 也被 monkeypatch，文本内容无关）


def _fake_parse_tencent(text):
    """假解析：同时覆盖 case1(600000)/case2(600001)，避免新浪回退多一次 _http。"""
    q = {"price": 9.35, "pct_chg": -0.96, "name": "浦发银行",
         "high": 9.35, "low": 9.22, "volume": 65327300.0,
         "amount": 604630000.0, "turnover": 0.19, "vol_ratio": 0.87,
         "float_mktcap": 2.9e11}
    return {"600000": dict(q), "600001": dict(q)}


def _run_case1():
    """判据 1：single-flight 在途去重。"""
    global _FAKE_HTTP_SLEEP
    _FAKE_HTTP_SLEEP = 3.0
    _FAKE_CALLS.clear()
    codes = ["600000"] * 10   # 同一批（去重后 1 只）
    df._http = _fake_http
    df._parse_tencent = _fake_parse_tencent
    import app.tdx as tdx_mod
    tdx_mod.available = lambda: False
    results = []
    t0 = time.time()

    def _call():
        r = df.fetch_quotes(codes, force=True)
        results.append((time.time() - t0, r))

    ths = [threading.Thread(target=_call) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    el = time.time() - t0
    calls = [u for u in _FAKE_CALLS]
    print("[判据1] 8 线程并发同批 | 假 _http 调用次数:", len(calls),
          "| 总耗时 %.2fs" % el)
    assert len(calls) == 1, "single-flight 应只调 1 次（其余 7 线程等待同一结果），实际 %d" % len(calls)
    assert len(results) == 8, "8 线程都应返回"
    for dt, r in results:
        assert r.get("600000", {}).get("price") == 9.35, "结果应含假数据"
    wait_s = df.C.QUOTE_SINGLEFLIGHT_WAIT_S
    max_wait = max(dt for dt, _ in results)
    print("  最大等待 %.2fs < wait_s+1 = %d" % (max_wait, wait_s + 1))
    assert max_wait < wait_s + 1, "等待超硬超时"
    print("  判据1 通过 ✓")


def _run_case3():
    """判据 3：回归级——fetch_quotes 键集 == _parse_tencent 直解键集（50 只，tdx 禁用）。"""
    print("[判据3] 回归：fetch_quotes 返回值结构（50 只，tdx 禁用 → 纯腾讯路径）")
    df._http = _ORIG_HTTP          # 恢复真实 _http（case1/2 已 monkeypatch）
    df._parse_tencent = _ORIG_PARSE_TENCENT
    import app.tdx as tdx_mod
    tdx_mod.available = lambda: False
    codes = ["600000", "000001", "600519", "000858", "601318", "600036", "000002",
             "601899", "600900", "000333", "601166", "600030", "000651", "601988",
             "600028", "000725", "601288", "600887", "000063", "600276", "601857",
             "000100", "600104", "601668", "000568", "600050", "601328", "000538",
             "600031", "601088", "000776", "600585", "601601", "000895", "600009",
             "601012", "000625", "600048", "601633", "000069", "600016", "601818",
             "000001", "600519", "601166", "000002", "600036", "601318", "000858",
             "600900"]
    # 直解同一批（_parse_tencent 原实现）
    symbols = ",".join(df._prefix(c) for c in codes)
    text = df._http("https://qt.gtimg.cn/q=" + symbols, decode="gbk",
                    ref="https://gu.qq.com/", timeout=10)
    direct = df._parse_tencent(text)
    r = df.fetch_quotes(codes, force=True)
    print("  直解票数:", len(direct), "| fetch_quotes 票数:", len(r))
    diff = 0
    for c in codes:
        dq, fq = direct.get(c), r.get(c)
        if not dq or not fq:
            continue
        dk, fk = set(dq.keys()), set(fq.keys())
        if dk != fk:
            diff += 1
            print("  字段 diff:", c, "直解", sorted(dk), "fetch", sorted(fk))
    print("  字段级 diff 票数:", diff)
    assert diff == 0, "字段级 diff 应为空"
    # 关键键恒在
    for c, q in r.items():
        for k in ("price", "pct_chg", "name", "high", "low", "volume"):
            assert k in q, "缺关键键 %s @ %s" % (k, c)
    print("  判据3 通过 ✓（%d 只票关键键齐全、键集与直解一致）" % len(r))


def _run_case2():
    """判据 2：隔离级——展示通道挂 30s 不阻塞交易通道。"""
    global _FAKE_HTTP_SLEEP
    _FAKE_HTTP_SLEEP = 0.1
    df._http = _fake_http
    df._parse_tencent = _fake_parse_tencent
    import app.tdx as tdx_mod
    tdx_mod.available = lambda: False
    # 展示通道（display 池线程 → 假 _http sleep 30s）
    t_show = threading.Thread(
        target=lambda: df.fetch_quotes_display(["600001"] * 10, enrich=False))
    t_show.start()
    time.sleep(0.5)   # 确保展示线程已进入 sleep(30)
    t0 = time.time()
    r = df.fetch_quotes_trading(["600001"] * 10, enrich=False)
    el = time.time() - t0
    dl = df.C.TRADING_FETCH_DEADLINE_S
    print("[判据2] 展示通道 sleep(30) 期间 | 交易通道耗时 %.2fs < deadline+1 = %d"
          % (el, dl + 1))
    assert el < dl + 1, "交易通道被展示阻塞（%.2fs）" % el
    assert r.get("600001", {}).get("price") == 9.35
    print("  判据2 通过 ✓")
    # 不等展示线程（30s 挂起），直接终止进程（daemon 池线程由进程退出回收）
    df._get_display_pool().shutdown(wait=False)
    os._exit(0)


if __name__ == "__main__":
    _run_case1()
    _run_case3()
    _run_case2()
