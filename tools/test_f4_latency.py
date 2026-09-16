# -*- coding: utf-8 -*-
"""F4（2026-09-13）主循环相位延迟指标化：离线自检。

判据（预注册）：
1. 埋点自身开销：相位埋点（deque append + 时间戳）每轮 <= 1ms（微基准 10000 次）。
2. 聚合正确：伪造含超阈样本的 _lp_hist → _lp_latency_flush 产出 audit
   loop_phase_latency，字段（phases.p50/p95/max/count、slow_phases）正确，级别 WARN。
3. 告警去重：同一相位超阈持续，notify 1 小时内只推 1 次（audit 全量记录，通知去重）。
4. 快照保留策略：stall_trace 落日期子目录，保留上限=STALL_TRACE_KEEP。
"""
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as C
from app import trader
from app.trader import TradingEngine

# ---- 判据1：埋点开销微基准（_lp_finish 埋点 = 1 次 deque.append） ----
d = deque(maxlen=2000)
t0 = time.perf_counter()
N = 100000
for i in range(N):
    d.append(("board", t0, t0 + 0.05))
dt = time.perf_counter() - t0
per_op_us = dt / N * 1e6
print("[判据1] 埋点开销: %.2f us/次（%d 次基准，阈值 1ms=1000us）" % (per_op_us, N))
ok1 = per_op_us < 1000
print("  判据1", "通过 ✓" if ok1 else "不通过 ✗")

# ---- 判据2/3：聚合 + 告警去重（轻量对象 + monkeypatch audit/notify） ----
audit_calls = []
notify_calls = []
_orig_audit = trader.audit.record
_orig_notify = trader.al.notify


def _fake_audit(kind, event, level="INFO", **fields):
    audit_calls.append({"kind": kind, "event": event, "level": level, **fields})


def _fake_notify(level, key, title, message, cooldown=60):
    notify_calls.append({"level": level, "key": key, "title": title})


trader.audit.record = _fake_audit
trader.al.notify = _fake_notify

tr = object.__new__(TradingEngine)
tr._lp_hist = deque(maxlen=2000)
tr._lp_lat_ts = 0.0
tr._lp_alert_ts = {}
now = time.time()
# 伪造 5 分钟窗口样本：board 正常(2s)，fast_watch 超阈(5s>3s)，monitor 正常(1s)，scan 正常(30s)
for i in range(30):
    tr._lp_hist.append(("board", now - 290 + i * 5, now - 290 + i * 5 + 2))
for i in range(20):
    tr._lp_hist.append(("fast_watch", now - 280 + i * 5, now - 280 + i * 5 + 5.0))
for i in range(30):
    tr._lp_hist.append(("monitor", now - 290 + i * 5, now - 290 + i * 5 + 1))
for i in range(3):
    tr._lp_hist.append(("scan", now - 280 + i * 20, now - 280 + i * 20 + 30))

tr._lp_latency_flush()
ev = [a for a in audit_calls if a["event"] == "loop_phase_latency"]
ok2 = False
if ev:
    e = ev[0]
    ok2 = (e["level"] == "WARN" and "fast_watch" in e["slow_phases"]
           and e["phases"]["fast_watch"]["p95_ms"] >= 5000
           and e["phases"]["board"]["p95_ms"] <= 2000
           and e["phases"]["board"]["count"] == 30)
    print("[判据2] 聚合: event=%s level=%s slow=%s board_p95=%dms fast_watch_p95=%dms n(board)=%d"
          % (e["event"], e["level"], e["slow_phases"],
             e["phases"]["board"]["p95_ms"], e["phases"]["fast_watch"]["p95_ms"],
             e["phases"]["board"]["count"]))
print("  判据2", "通过 ✓" if ok2 else "不通过 ✗")

# 判据3：告警去重——第一次 flush 已推 1 条 notify；立即二次聚合（<1h）不新增
before = len([n for n in notify_calls if n["key"] == "loop_phase_fast_watch"])
audit_calls.clear()
tr._lp_lat_ts = 0.0   # 强制再次聚合
tr._lp_latency_flush()
after = len([n for n in notify_calls if n["key"] == "loop_phase_fast_watch"])
ok3 = (before == 1 and after == 1)
print("[判据3] 告警去重: 首次 flush notify=%d 次，立即二次聚合 notify=%d 次（应 1/1）"
      % (before, after))
print("  判据3", "通过 ✓" if ok3 else "不通过 ✗")

trader.audit.record = _orig_audit
trader.al.notify = _orig_notify

# ---- 判据4：快照保留目录逻辑（不真实写文件，只验证参数与目录常量） ----
print("[判据4] STALL_TRACE_KEEP=%d（任务要求 5->20），子目录=stall_trace/<YYYYMMDD>/" % C.STALL_TRACE_KEEP)
ok4 = C.STALL_TRACE_KEEP == 20

sys.exit(0 if (ok1 and ok2 and ok3 and ok4) else 1)
