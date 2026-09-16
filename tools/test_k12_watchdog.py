# -*- coding: utf-8 -*-
"""K12（2026-09-16）离线自检：僵尸半死救援 / lock 错配自愈 / audit 事件 / 真死判据不变。
全部 mock，不真杀进程、不真重启、不写生产 audit。"""
import json
import os
import sys

sys.path.insert(0, r"C:\Users\26838\A股模拟盘")
import tools.service_watchdog as wd

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print("%s %s  %s" % ("PASS" if cond else "FAIL", name, detail))


# ---------- mock 基座 ----------
KILLED = []


def _reset():
    KILLED.clear()
    wd.check_tcp = lambda *a, **k: False          # 端口 down
    wd.kill_pid = lambda pid, max_retry=2: (KILLED.append(pid), True)[1]
    wd._pid_alive_wd = lambda pid: pid not in KILLED
    wd._proc_age = lambda pid: 999.0              # 进程龄 >180s
    wd.read_lock_pid = lambda: 0
    wd.clear_lock = lambda: None
    wd._audit_record = lambda *a, **k: None       # 默认吞掉（用例3单独抓）


def _mock_cim(pids_str):
    """mock confirm_no_duplicate 内的 CIM subprocess.run：只回 CIM 查询。"""
    orig = wd.subprocess.run

    def fake(args, **kw):
        if "Get-CimInstance" in " ".join(args):
            return type("R", (), {"stdout": pids_str, "returncode": 0})()
        return orig(args, **kw)

    wd.subprocess.run = fake


# ---------- 判据①：僵尸半死救援（端口 down + main.py 进程龄>180s → kill+清锁+放行） ----------
print("\n=== 判据① 僵尸半死救援 ===")
_reset()
_mock_cim("21996,37660")
ok, why = wd.confirm_no_duplicate()
check("① 半死判定放行", ok is True, why)
check("① 两个僵尸都被 kill", sorted(KILLED) == [21996, 37660], "killed=%s" % KILLED)
check("① 返回含 half_dead_killed_pids", "half_dead_killed_pids=[21996, 37660]" in why, why)

# ①-b：进程龄 ≤180s（刚启动未绑端口）→ 维持拒绝（防误杀）
_reset()
_mock_cim("21996")
wd._proc_age = lambda pid: 60.0
ok, why = wd.confirm_no_duplicate()
check("①-b 新生实例维持拒绝（≤180s）", ok is False and "等待绑端口" in why, why)
check("①-b 新生实例未被杀", KILLED == [], "killed=%s" % KILLED)

# ---------- 判据②：lock PID 错配自愈 ----------
print("\n=== 判据② lock PID 错配 ===")
# ②-a 错配：lock=18972 存活但不在 killed 列表（今日实况模拟：lock 指已死/他进程）
_reset()
_mock_cim("34096")
wd.read_lock_pid = lambda: 18972
wd._pid_alive_wd = lambda pid: pid == 18972        # lock PID 存活但非 main.py
wd.clear_lock = lambda: setattr(wd, "_cl_called", True)
wd._cl_called = False
ok, why = wd.confirm_no_duplicate()
check("②-a 错配仍放行（清锁+重启不阻塞）", ok is True, why)
check("②-a 错配被记录", "lock_mismatch=True" in why, why)
check("②-a clear_lock 被调用", wd._cl_called is True)
check("②-a 真实 main.py 34096 被杀", KILLED == [34096], "killed=%s" % KILLED)

# ②-b 陈旧锁（lock PID 已死）：非错配，正常清锁
_reset()
_mock_cim("34096")
wd.read_lock_pid = lambda: 18972
wd._pid_alive_wd = lambda pid: pid == 34096        # lock PID 已死
ok, why = wd.confirm_no_duplicate()
check("②-b 陈旧锁（lock PID 已死）→ lock_mismatch=False", "lock_mismatch=False" in why, why)

# ---------- 判据③：恢复/拒绝均有 audit 事件 ----------
print("\n=== 判据③ audit 事件 ===")
_captured = []


def _fake_audit(kind, event, level="INFO", **fields):
    _captured.append({"kind": kind, "event": event, "level": level, **fields})


def _run_recover_restart():
    """端口 down → confirm ok → start 成功 → 等待新锁。返回 action。"""
    _reset()
    wd.check_tcp = lambda *a, **k: False
    wd._mock = None
    # confirm_no_duplicate 真实逻辑：CIM 无 main.py → lock 无 → ok
    _mock_cim("")                                   # 无 main.py 进程
    wd.read_lock_pid = lambda: 0
    wd.start_service = lambda: True
    wd.time.sleep = lambda *a, **k: None            # 跳过 30s 等待循环
    wd._audit_record = _fake_audit
    _captured.clear()
    return wd.recover()


# ③-a 恢复成功（全死重启）
action = _run_recover_restart()
check("③-a recover 返回 restarted", action == "restarted", action)
recs = [c for c in _captured if c["event"] == "watchdog_recovered"]
check("③-a watchdog_recovered 事件存在", len(recs) == 1, json.dumps(recs, ensure_ascii=False)[:200])
if recs:
    r = recs[0]
    check("③-a 字段齐全 action/reason/pids_killed/downtime_s",
          all(k in r for k in ("action", "reason", "pids_killed", "downtime_s")),
          str(sorted(r.keys())))
    check("③-a action=restarted", r.get("action") == "restarted", str(r.get("action")))
    check("③-a pids_killed=[]（全死无僵尸）", r.get("pids_killed") == [], str(r.get("pids_killed")))
    check("③-a downtime_s 有值", isinstance(r.get("downtime_s"), int) and r["downtime_s"] >= 0,
          "downtime_s=%s" % r.get("downtime_s"))

# ③-b 拒绝启动（dup_abort）
_run_recover_restart()
_reset()
wd.check_tcp = lambda *a, **k: False
_mock_cim("21996")                                  # main.py 存活（龄>180s → 半死救援）
wd.read_lock_pid = lambda: 0
wd.start_service = lambda: True
wd.time.sleep = lambda *a, **k: None
wd._audit_record = _fake_audit
_captured.clear()
action = wd.recover()
# 半死救援成功 → 走恢复（重启）→ watchdog_recovered 且 pids_killed=[21996]
recs = [c for c in _captured if c["event"] == "watchdog_recovered"]
check("③-b 半死救援也落 watchdog_recovered", len(recs) == 1, json.dumps(recs, ensure_ascii=False)[:200])
if recs:
    check("③-b pids_killed=[21996]（从半死救援解析）",
          recs[0].get("pids_killed") == [21996], str(recs[0].get("pids_killed")))
    check("③-b reason 含 half_dead_killed_pids", "half_dead_killed_pids" in str(recs[0].get("reason")),
          str(recs[0].get("reason"))[:200])

# ③-c 拒绝启动（dup_abort：lock 持有者存活）
_reset()
wd.check_tcp = lambda *a, **k: False
_mock_cim("")                                       # 无 main.py
wd.read_lock_pid = lambda: 7777
wd._pid_alive_wd = lambda pid: True                 # lock 持有者存活 → 拒绝
wd.start_service = lambda: True
wd.time.sleep = lambda *a, **k: None
wd._audit_record = _fake_audit
_captured.clear()
action = wd.recover()
check("③-c dup_abort（lock 持有者存活）", action == "dup_abort", action)
refs = [c for c in _captured if c["event"] == "watchdog_refused"]
check("③-c watchdog_refused 事件存在", len(refs) == 1, json.dumps(refs, ensure_ascii=False)[:200])
if refs:
    check("③-c refused 含 reason=dup_confirm_*", str(refs[0].get("reason", "")).startswith("dup_confirm"),
          str(refs[0].get("reason"))[:120])

# ③-d 拒绝启动（abort_orphan_alive：kill 失败）
_reset()
wd.check_tcp = lambda *a, **k: True                 # 端口活着
wd.check_overview = lambda *a, **k: (False, "time_stale_999s")
wd.check_audit = lambda *a, **k: (False, "audit_stale_999s")
wd.pid_on_port = lambda *a, **k: [5555]
wd.cmdline_has = lambda pid, n: True                # 端口进程是 main.py
wd.kill_pid = lambda pid, max_retry=2: False        # 杀不掉 → 孤儿
wd._audit_record = _fake_audit
_captured.clear()
action = wd.recover()
check("③-d abort_orphan_alive（kill 失败中止）", action == "abort_orphan_alive", action)
refs = [c for c in _captured if c["event"] == "watchdog_refused"]
check("③-d watchdog_refused CRITICAL 存在", len(refs) == 1, json.dumps(refs, ensure_ascii=False)[:200])
if refs:
    check("③-d refused level=CRITICAL", refs[0].get("level") == "CRITICAL", str(refs[0].get("level")))

# ---------- 判据④：真死判据不变（l1 down 仍 3 次恢复；放宽仅限 l1=ok+l2慢） ----------
print("\n=== 判据④ 真死判据不变 ===")
check("④ RECOVER_THRESHOLD 仍为 3", wd.RECOVER_THRESHOLD == 3, str(wd.RECOVER_THRESHOLD))
check("④ l1 down 不触发阈值放宽", wd.effective_recover_threshold({}, {"l1_tcp": "down"}) == 3,
      str(wd.effective_recover_threshold({}, {"l1_tcp": "down"})))
check("④ l1=ok+l2慢 才放宽（既有语义未动）",
      wd.effective_recover_threshold({}, {"l1_tcp": "ok", "l2_overview": "time_stale_50s"}) == 6,
      str(wd.effective_recover_threshold({}, {"l1_tcp": "ok", "l2_overview": "time_stale_50s"})))

# ---------- 汇总 ----------
print("\n===== 汇总 =====")
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for n, d in FAIL:
    print("  FAIL %s | %s" % (n, d))
sys.exit(1 if FAIL else 0)
