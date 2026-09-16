# -*- coding: utf-8 -*-
"""启动核验 —— 服务起来后跑一遍，确认生效参数与配套子系统全部在位。

只读工具：import app.config（磁盘当前值）、HTTP 读本机服务、读 data/ 下 JSON/JSONL、
调用 schtasks/netstat/powershell 只查不写。唯一输出是控制台报告。

检查项：
  ① 生效参数四项 + 冷却暂停开关（对照预期值）
  ② 服务存活（/api/overview）
  ③ 影子线程启动证据（/api/log 含"影子日更调度已启动"）
  ④ 前瞻台账（forward_eval.jsonl 最新记账日 + 计划任务 20:30 在册）
  ⑤ 秒级监控（fast_watch 与交易引擎同生命周期：/api/trading/status）
  ⑥ 进程新鲜度（app/*.py 或 main.py 晚于运行实例启动时间 → 提醒重启）

用法：
  python tools/startup_check.py
退出码：0 全过；2 有 FAIL/WARN（供计划任务或人工一眼识别）。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C   # noqa: E402

DATA_DIR = C.DATA_DIR
LOCK_FILE = os.path.join(DATA_DIR, "app.lock")
PORT_FALLBACK = 8899

EXPECT = [
    ("STOP_LOSS_PCT", -0.07, C.STOP_LOSS_PCT, "=="),
    ("INDEX_TIMING_ENABLED", False, bool(getattr(C, "INDEX_TIMING_ENABLED", None)), "=="),
    ("AUCTION_MIN_PCT", 4.0, float(getattr(C, "AUCTION_MIN_PCT", 0)), "=="),
    ("BOARD_MOMENTUM_MIN", 8.0, float(getattr(C, "BOARD_MOMENTUM_MIN", 0)), "=="),
    ("RISK_COOLDOWN_SUSPEND_UNTIL", ">today=生效",
     str(getattr(C, "RISK_COOLDOWN_SUSPEND_UNTIL", "")), ">="),
]


def _http_get_json(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def _svc_port():
    """优先从 app.lock 的 PID 反查监听端口（find_free_port 可能改道）。"""
    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=20).stdout.decode("utf-8", "replace")
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 5 and parts[3] == str(pid) and parts[1].endswith(":%d" % PORT_FALLBACK):
                return PORT_FALLBACK
            if len(parts) >= 5 and parts[3] == str(pid) and "LISTENING" in ln.upper():
                m = re.search(r"127\.0\.0\.1:(\d+)", parts[1] if parts[1].startswith("127.") else "")
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return PORT_FALLBACK


def _proc_info(pid):
    """(alive, creation_time_str)；用 PowerShell 只读查询。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$p=Get-Process -Id %d -ErrorAction Stop; $p.StartTime.ToString('yyyy-MM-dd HH:mm:ss')" % pid],
            capture_output=True, text=True, timeout=30)
        s = (out.stdout or "").strip()
        return (bool(s), s if s else "?")
    except Exception:
        return (False, "?")


def _newest_core_mtime():
    newest = ""
    files = [os.path.join(ROOT, "main.py")]
    appdir = os.path.join(ROOT, "app")
    try:
        files += [os.path.join(appdir, f) for f in os.listdir(appdir) if f.endswith(".py")]
    except Exception:
        pass
    for p in files:
        try:
            mt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))
            if mt > newest:
                newest = mt
        except Exception:
            continue
    return newest


def main():
    L = []
    bad = []

    def ok(msg):
        L.append("[PASS] " + msg)

    def warn(msg):
        L.append("[WARN] " + msg)
        bad.append(msg)

    def fail(msg):
        L.append("[FAIL] " + msg)
        bad.append(msg)

    today = time.strftime("%Y-%m-%d")
    L.append("# 启动核验 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    L.append("")

    # ---- 锁与进程 ----
    pid = None
    born = "?"
    try:
        pid = int(open(LOCK_FILE, encoding="utf-8").read().strip())
    except Exception:
        pass
    if pid:
        alive, born = _proc_info(pid)
        if alive:
            ok("服务进程存活 PID=%s 启动于 %s（lock=%s）" % (pid, born, LOCK_FILE))
        else:
            fail("app.lock 指向 PID %s 但进程不在（服务未运行？）" % pid)
    else:
        fail("无法读取 app.lock（服务从未以单实例模式启动？）")

    # ---- ① 生效参数 ----
    L.append("")
    L.append("## ① 生效参数（config 磁盘值）")
    suspend_until = ""
    for name, want, got, op in EXPECT:
        if op == "==":
            judge = (got == want)
            note = "期望 %r" % (want,)
        else:  # >=：暂停开关须覆盖今天
            suspend_until = str(got)
            judge = bool(got) and got >= today
            note = "须 ≥ 今天(%s)" % today
        if judge:
            ok("%s = %r（%s）" % (name, got, note))
        else:
            fail("%s = %r ≠ 预期（%s）" % (name, got, note))
    if suspend_until:
        rs_path = os.path.join(DATA_DIR, "risk_state.json")
        try:
            rs = json.load(open(rs_path, encoding="utf-8"))
            L.append("[INFO] risk_state.json: consec_losses=%s cooldown_until=%s breaker_on=%s"
                     % (rs.get("consec_losses"), rs.get("cooldown_until"), rs.get("breaker_on")))
        except Exception:
            warn("risk_state.json 读取失败")

    # ---- HTTP 面 ----
    port = _svc_port()
    base = "http://127.0.0.1:%d" % port

    # ---- ② 服务存活 ----
    ov = _http_get_json(base + "/api/overview", timeout=30)
    if ov.get("error"):
        fail("服务无响应(%s)：GET %s/api/overview" % (ov["error"][:60], base))
    else:
        ok("服务响应 /api/overview：market_count=%s time=%s（port=%d）"
           % (ov.get("market_count"), ov.get("time"), port))

    # ---- ③ 影子线程 ----
    lg = _http_get_json(base + "/api/log", timeout=15)
    lines = lg.get("lines") or []
    shadow_line = [x for x in lines if "影子日更调度已启动" in x]
    if shadow_line:
        t = str(shadow_line[-1])[:6]
        if born != "?" and t != "?" and str(born)[:10] >= today:
            ok("影子线程启动证据：%s（今天 %s 的实例内）" % (shadow_line[-1], today))
        else:
            warn("影子线程启动行在日志缓冲内但实例非今日启动（%s）：建议重启后复核" % born)
    else:
        fail("/api/log 无『影子日更调度已启动』——该实例接线未生效或为旧进程")

    # ---- ④ 前瞻台账 ----
    jl = os.path.join(DATA_DIR, "forward_eval.jsonl")
    last_date = ""
    try:
        with open(jl, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        last_date = json.loads(ln).get("date") or last_date
                    except Exception:
                        continue
        ok("前瞻台账最新记账日=%s（%s）" % (last_date, jl))
    except FileNotFoundError:
        warn("前瞻台账不存在：%s（任务到点后生成）" % jl)
    st_out = subprocess.run(["schtasks", "/query", "/tn", "forward_eval", "/fo", "LIST"],
                            capture_output=True, timeout=30)
    st_txt = (st_out.stdout or b"").decode("utf-8", "replace")
    if st_out.returncode == 0:
        nxt = re.search(r"Next Run Time:\s*(.+)", st_txt)
        ok("计划任务 forward_eval 在册 Next Run=%s" %
           ((nxt.group(1).strip() if nxt else "?")))
    else:
        warn("计划任务 forward_eval 不在册！重挂：见 docs/operations.md 前瞻记账节")

    # ---- ⑤ 秒级监控 ----
    ts = _http_get_json(base + "/api/trading/status", timeout=15)
    if ts.get("error"):
        warn("trading/status 读取失败：%s" % ts["error"][:60])
    elif ts.get("running"):
        ok("交易引擎运行中——秒级监控线程同生命周期存活（2s/次盯仓，只告警不下单）")
    else:
        L.append("[INFO] 交易引擎未启动：秒级监控随之未启（盘前属常态，交易日 09:15 自动开启后生效）")

    # ---- ⑥ 进程新鲜度 ----
    newest = _newest_core_mtime()
    if born != "?" and newest > born:
        warn("存在比运行实例更新的代码：core 最新 mtime=%s > 实例启动 %s —— 改码后未重启，参数/逻辑仍是旧值"
             % (newest, born))
    elif born != "?":
        ok("进程新鲜度：core 最新 mtime %s ≤ 实例启动 %s" % (newest, born))

    # ---- 输出 ----
    L.append("")
    n_bad = len(bad)
    L.append("**结论：%s**（PASS 见上；FAIL/WARN %d 项）" %
             ("✅ 全部通过" if n_bad == 0 else "⚠️ 有 %d 项需关注" % n_bad, n_bad))
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n".join(L))
    sys.exit(2 if n_bad else 0)


if __name__ == "__main__":
    main()
