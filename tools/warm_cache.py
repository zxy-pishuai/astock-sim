# -*- coding: utf-8 -*-
"""★ J2（2026-09-13）：启动预热工具——tactics / sentiment/history / stocklist。
在服务启动后（或计划任务里）调用，使首个用户请求不吃冷启动：
  - tactics：subprocess 独立跑 scan_all 写 tmp/tactics_cache/{date}.json（Web 进程只读文件）
  - sentiment/history：请求本地 8899 /api/sentiment/history，触发 Web 进程内 1800s 缓存填充
    （60s 重活在 warm 时刻执行，用户到来时已命中）
  - stocklist：请求本地 8899 /api/stocklist，触发 _LIST_CACHE 填充（30s TTL）
用法：python tools/warm_cache.py [port]   （默认 8899；tactics 恒为独立进程）
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)   # 保证 `python tools/warm_cache.py` 可直接 import app.*
TACTICS_CACHE = os.path.join(ROOT, "tmp", "tactics_cache")


def _warm_tactics():
    """独立进程跑 scan_all → 落当日缓存文件。零 Web 进程负担。
    日期用最近交易日（_latest_date）：周末/节假日无当日数据，跳过预热（无害）。"""
    import sqlite3
    from app import config as C
    d = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=10)
        try:
            row = con.execute("SELECT MAX(date) FROM kline WHERE period='day'").fetchone()
            d = (row[0] or "")[:10]
        finally:
            con.close()
    except Exception:
        pass
    if not d:
        print("[warm] tactics 无最近交易日（库不可读），跳过")
        return False
    cache = os.path.join(TACTICS_CACHE, "%s.json" % d)
    if os.path.exists(cache):
        print("[warm] tactics 最近交易日 %s 缓存已存在: %.0fKB" % (d, os.path.getsize(cache) / 1024))
        return True
    os.makedirs(TACTICS_CACHE, exist_ok=True)
    _tmp = cache + ".tmp.json"
    # ★ K5（2026-09-16）：路径转正斜杠——%r/字面量里 Windows 反斜杠（\U \t 等）
    #   触发 Unicode 转义 SyntaxError（实测 rc=1），正斜杠在 Python 字面量中无转义。
    _c = cache.replace("\\", "/")
    _t = _tmp.replace("\\", "/")
    _code = ("from app.tactics import scan_all; import json, os;"
             "f = scan_all(date='%s');"
             "open('%s', 'w', encoding='utf-8').write(json.dumps(f, ensure_ascii=False));"
             "os.replace('%s', '%s')" % (d, _t, _t, _c))
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-c", _code], cwd=ROOT,
                           timeout=300, capture_output=True)
        ok = r.returncode == 0 and os.path.exists(cache)
        print("[warm] tactics 独立进程预热 %s (%.1fs, rc=%d, %s)"
              % ("OK" if ok else "FAIL", time.time() - t0, r.returncode,
                 "%.0fKB" % (os.path.getsize(cache) / 1024) if ok else "no-cache"))
        return ok
    except Exception as e:
        print("[warm] tactics 预热异常: %s" % e)
        return False


def _warm_http(port, path, label):
    """请求本地 Web 端口触发内存缓存填充（stocklist / sentiment/history）。"""
    url = "http://127.0.0.1:%d%s" % (port, path)
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            data = r.read()
        dt = time.time() - t0
        print("[warm] %s OK (%.1fs, %d bytes)" % (label, dt, len(data)))
        return True
    except Exception as e:
        print("[warm] %s FAIL: %s" % (label, e))
        return False


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print("[warm] start %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    _warm_tactics()
    _warm_http(port, "/api/stocklist?page=1&size=50", "stocklist")
    _warm_http(port, "/api/sentiment/history", "sentiment/history")
    print("[warm] done %s" % time.strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    main()
