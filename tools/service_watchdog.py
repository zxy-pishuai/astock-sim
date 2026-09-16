#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
H1｜看门狗硬化 —— 三级健康判据 + 半死状态恢复（纯标准库，不 import app/*）

用途
----
作为计划任务每 5 分钟运行一次（StockService_Watchdog），对 8899 端口的服务做三级健康检查：
  L1 TCP 8899 可连通
  L2 GET /api/overview 返回 HTTP 200 且 time 字段新鲜（≤90 秒）
  L3 审计心跳新鲜：data/audit/audit.jsonl 尾部 t 字段
     交易时段（交易日 09:30-11:30 / 13:00-15:00）≤10 分钟；非交易时段放宽到 7 天
     （非交易时段引擎不写心跳属正常，避免夜间/周末误判）
任一失效记一次；连续 3 次 → 执行恢复。

恢复逻辑（先探 8899 防双实例）
----
1. 端口关闭（全死）→ 三重确认（无监听/无 main.py 进程/lock 持有者已死）后 Start-Process 重启。
2. 端口开启但 overview/audit 不新鲜（半死僵尸，如 08-28 主线程退出后端口仍监听）
   → 定位占用 8899 且命令行含 main.py 的本服务进程 → taskkill → 清 data/app.lock → 重启。
   （半死状态下 main.py 的单实例锁会因"端口健康"而拒绝新实例，必须先清僵尸再启。）
3. G2（2026-09-13）：kill 失败 → **中止恢复、不启动新实例**（防孤儿+新实例并存的双实例窗口，
   00:20 事故：孤儿 43336 + 新 52044）；orphan_pid/orphan_first_seen 入状态文件，后续轮次
   升级 taskkill /T /F 杀进程树，仍失败则只告警（needs_human）不重启。
4. 启动前三重确认：①8899 无监听 ②无 CommandLine 含 main.py 的存活进程 ③app.lock 持有者
   PID 已不存在；任一不满足 → 拒绝启动（dup_abort）。
5. 端口开启且实际健康 → 视为瞬时抖动，跳过恢复（防双实例）。

写库/重启边界
----
- 不 import app/*；不写 market.db；不动审计链（只读 audit.jsonl 尾部）。
- 恢复仅在连续 3 次失败后触发，且触发前二次复核，避免误杀健康实例。
- 每次动作追加 tmp/watchdog.log（时刻 + 旧PID消失 + 新PID）。

用法
----
  python tools/service_watchdog.py            # 单次完整检查（默认，供计划任务）
  python tools/service_watchdog.py --verify   # 只读体检，不改状态、不恢复（供人工验证）
"""
import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import threading

# 09-04 修复：pythonw 父进程下 netstat/powershell/taskkill 等控制台子程序会自建黑框窗
# （每5分钟一闪）。全部 spawn 加 CREATE_NO_WINDOW。授权：用户 09-04 直接修复指令。
# 备份: tmp/pack18/service_watchdog.py.bak_20260904
CREATE_NO_WINDOW = 0x08000000
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8899
OVERVIEW_URL = "http://127.0.0.1:%d/api/overview" % PORT
OVERVIEW_MAX_AGE = 90          # 秒：/api/overview time 字段新鲜度
AUDIT_FILE = os.path.join(BASE, "data", "audit", "audit.jsonl")
AUDIT_DIR = os.path.join(BASE, "data", "audit")
AUDIT_MAX_AGE_IN = 10 * 60     # 秒：交易时段审计心跳 ≤10 分钟
# G1（2026-09-13）：非交易时段 6 小时（原 7 天过宽——7 天意味着"服务半死但 audit
# 未超龄"的判定窗口长达一周；夜间引擎静默不写心跳属正常，收紧后由 health_check
# 的 l2 新鲜豁免兜底：l2 overview 新鲜 → audit 超龄判 engine_active_audit_silent）。
# 依据：audit 心跳只出现在引擎活跃写链时，非交易时段最长静默=周末（周五 15:00 →
# 周一 09:30），6h 上限必然触发 audit_stale，但 l2 豁免保证不误杀；而 l2 也停更时
# l2+l3 双信号可更快捕获真半死。
AUDIT_MAX_AGE_OUT = 6 * 3600   # 秒：非交易时段审计心跳 ≤6 小时（配合 l2 新鲜豁免）
STATE_FILE = os.path.join(BASE, "tmp", "h1_watchdog_state.json")
LOG_FILE = os.path.join(BASE, "tmp", "watchdog.log")
LOCK_FILE = os.path.join(BASE, "data", "app.lock")
RECOVER_THRESHOLD = 3          # 连续失败次数触发恢复
OVERVIEW_HTTP_TIMEOUT = 90     # ★ 验收修复（2026-09-16）：l2 探测读超时。原为 5s，
                               #   而上游行情源降级时 /api/overview 实测 11-52s（TDX
                               #   fetch_quotes_fast 8s 超时降级腾讯/新浪）→ l2 恒失败，
                               #   3 次后杀进程重启，重启后源仍降级 → 盘中重启循环
                               #   （09-16 12:15-13:03 实证：500→进程死→拒绝启动×2→杀僵尸
                               #   →仍超时）。提到 90s 后“慢但活着”不再计失败（实测 overview 最慢 55.7s）；
                               #   真死仍由 l1 TCP（5s connect）与 HTTP 500 捕获。
TS_FMT = "%Y-%m-%d %H:%M:%S"
STARTUP_GRACE = 180            # 秒：实例出生豁免期（重启后 l2_overview 超时不计连续失败）
LOG_DIR = os.path.join(BASE, "data", "logs")
# R2-P0.3：watchdog 心跳文件（引擎侧 trader._monitor 每 ~10 分钟检查其 mtime，
# 超 10 分钟 → audit CRITICAL）。watchdog 自身不写 audit（遵守只读 audit 尾部红线），
# 只写心跳 + 记 log；调度延迟由本文件自检记 log，audit 落点归引擎侧。
HEARTBEAT_FILE = os.path.join(BASE, "tmp", "watchdog_heartbeat.ts")
HEARTBEAT_MAX_GAP = 10 * 60       # 秒：距上次心跳超 10 分钟视为调度断更
# E2 P0：子进程日志改为按次唯一文件名（watchdog_child_<kind>_<ts>_<seq>.log），
# 不再使用固定名 CHILD_STDOUT/CHILD_STDERR——固定名被僵尸进程握持句柄是 09-03 恢复启动
# 失败 8 小时的根因（见 docs/reports/audit_20260903.md §2 P0-2）。


def now_str():
    return datetime.datetime.now().strftime(TS_FMT)


def log(msg):
    line = "%s  %s\n" % (now_str(), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="")


# --------------------------------------------------------------------------
# 三级健康判据
# --------------------------------------------------------------------------
def check_tcp(port=PORT):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return True
    except OSError:
        return False


def check_overview(port=PORT, max_age=OVERVIEW_MAX_AGE):
    """GET /api/overview；返回 (ok, detail)。time 字段须在 max_age 秒内。"""
    try:
        req = urllib.request.Request(OVERVIEW_URL, method="GET")
        with urllib.request.urlopen(req, timeout=OVERVIEW_HTTP_TIMEOUT) as r:
            if r.status != 200:
                return False, "http_%d" % r.status
            data = json.loads(r.read().decode("utf-8", "replace"))
        t = data.get("time")
        if not t:
            return False, "no_time_field"
        dt = datetime.datetime.strptime(t, TS_FMT)
        age = (datetime.datetime.now() - dt).total_seconds()
        if age > max_age:
            return False, "time_stale_%ds" % int(age)
        return True, "time_age_%ds" % int(age)
    except Exception as e:
        return False, "%s:%s" % (type(e).__name__, e)


def in_trading_window(now=None):
    """交易时段近似判据：工作日 09:30-11:30 / 13:00-15:00。
    （纯标准库无法用 app 交易日历；节假日白天可能出现审计空窗，
     由恢复前的二次复核兜底，不会误杀健康实例。）"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def _tail_t_from_file(path):
    """读文件尾部 8KB，返回最后一个含 t 字段的值或 None（解析失败/无 t 均 None）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    last_t = None
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("t"):
            last_t = obj["t"]
    return last_t


def latest_archived_audit():
    """G1：返回 data/audit/ 下按文件名倒序最新、且尾部含 t 的轮转归档路径，或 None。
    轮转脚本（tools/audit_rotate.py）把 audit.jsonl 归档为 audit_<日期>.jsonl 并新建
    空文件；归档按日期命名，倒序取最新即"上一段链"。"""
    try:
        if not os.path.isdir(AUDIT_DIR):
            return None
        files = [f for f in os.listdir(AUDIT_DIR)
                 if f.startswith("audit_") and f.endswith(".jsonl")]
        files.sort(reverse=True)
        for fn in files:
            p = os.path.join(AUDIT_DIR, fn)
            if _tail_t_from_file(p):
                return p
    except Exception:
        pass
    return None


def check_audit(max_age_in=AUDIT_MAX_AGE_IN, max_age_out=AUDIT_MAX_AGE_OUT):
    """读审计尾部 t 字段；返回 (ok, detail)。交易时段严格、非交易宽松。

    G1（2026-09-13）修复 audit 轮转导致的 no_t_in_tail 误杀：
    - 轮转感知：audit.jsonl 为空/无 t 时，回退到 data/audit/ 下最新的轮转归档
      audit_*.jsonl 取 t（成功 → detail 标 rotated_fallback_<归档名>_age_<秒>）；
    - 仍无 → no_t_in_tail（由 health_check 的 l2 新鲜豁免与 main 的轮转宽限窗兜底，
      不再直接构成 3 连恢复的死亡证据）。"""
    if not os.path.exists(AUDIT_FILE):
        # 活动文件缺失：直接回退归档（仍无 → audit_missing）
        p = latest_archived_audit()
        last_t = _tail_t_from_file(p) if p else None
        src = os.path.basename(p) if p else "current"
        if not last_t:
            return False, "audit_missing"
    else:
        last_t = _tail_t_from_file(AUDIT_FILE)
        src = "current"
        if not last_t:
            # G1 轮转感知：当前文件空（00:05 轮转后）→ 回退最新归档
            p = latest_archived_audit()
            if p:
                last_t = _tail_t_from_file(p)
                src = os.path.basename(p)
    if not last_t:
        return False, "no_t_in_tail"
    dt = datetime.datetime.strptime(last_t, TS_FMT)
    age = (datetime.datetime.now() - dt).total_seconds()
    limit = max_age_in if in_trading_window() else max_age_out
    if age > limit:
        return False, "audit_stale_%ds(limit_%ds)" % (int(age), int(limit))
    if src != "current":
        return True, "rotated_fallback_%s_age_%ds" % (src, int(age))
    return True, "audit_age_%ds" % int(age)


def health_check():
    """返回 (all_ok, levels:{l1:..., l2:..., l3:...})

    G1（2026-09-13）：引擎活跃豁免——l2 overview 新鲜（time_age ≤90s）证明引擎进程
    活着且在更新运行状态；此时 l3 audit 超龄/无 t 属夜间静默或 00:05 轮转空窗，
    不构成半死证据（09-13 00:10-00:20 误杀根因：l2 全程 time_age_0s 仍被 l3 连杀）。
    l2 不新鲜时 l3 照常严格 → 真半死仍由 l2+l3 双信号捕获。"""
    l1 = check_tcp()
    l2_ok, l2_d = check_overview()
    l3_ok, l3_d = check_audit()
    # ★ G3（2026-09-13）：豁免收紧为"仅非交易时段"。交易时段引擎必写 audit 心跳，
    #   audit_age 超阈（>6h）即使 l2 新鲜也必须独立触发失败（audit 写入链故障不能
    #   被 l2 掩盖）；非交易时段引擎静默正常，l2 新鲜时豁免（G1 engine_active_audit_silent）。
    if not l3_ok and l2_ok and not in_trading_window():
        l3_ok, l3_d = True, "engine_active_audit_silent"
    return (l1 and l2_ok and l3_ok), {
        "l1_tcp": "ok" if l1 else "down",
        "l2_overview": l2_d,
        "l3_audit": l3_d,
    }


# --------------------------------------------------------------------------
# 状态持久化
# --------------------------------------------------------------------------
def load_state():
    """读取状态；G2 扩展：除四基础键外，保留 orphan_pid/orphan_first_seen/orphan_pids/
    needs_human（kill 失败中止恢复后的孤儿接管状态，必须跨轮次持久化）。
    ★ K7-3：保留 wall_ts/mono_ts 双记（audit._g3_heartbeat_age 用于区分
    机器休眠空档与看护真死——Windows 单调钟在系统睡眠期间不前进）。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    base = {"consecutive_failures": int(st.get("consecutive_failures", 0)),
            "last_status": st.get("last_status", ""),
            "last_check": st.get("last_check", ""),
            "restart_ts": st.get("restart_ts", "")}
    for k in ("orphan_pid", "orphan_first_seen", "orphan_pids", "needs_human",
              "wall_ts", "mono_ts"):
        if k in st:
            base[k] = st[k]
    return base


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        st2 = dict(st)
        # ★ K7-3：墙钟 + 单调钟双记（audit 心跳新鲜度/休眠区分的数据源）
        st2["wall_ts"] = time.time()
        st2["mono_ts"] = time.monotonic()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st2, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("状态保存失败: %s" % e)


def heartbeat_age_seconds():
    """R2-P0.3：距上次心跳的秒数；文件缺失/异常返回 None。"""
    try:
        return time.time() - os.path.getmtime(HEARTBEAT_FILE)
    except Exception:
        return None


def touch_heartbeat():
    """R2-P0.3：运行即写心跳（mtime + 内容时间戳），供引擎侧监测调度断更。"""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(time.strftime(TS_FMT))
    except Exception as e:
        log("心跳写入失败: %s" % e)


def restart_age_seconds(st):
    """返回距最近一次重启的秒数；无 restart_ts 或解析失败返回 None。"""
    ts = st.get("restart_ts", "")
    if not ts:
        return None
    try:
        dt = datetime.datetime.strptime(ts, TS_FMT)
    except Exception:
        return None
    return (datetime.datetime.now() - dt).total_seconds()


def in_startup_grace(st):
    """实例出生 180 秒内（重启豁免期）→ True。"""
    age = restart_age_seconds(st)
    return age is not None and 0 <= age < STARTUP_GRACE


def startup_grace_applies(st, levels):
    """P0②：豁免仅保护"l2_overview 超时"这一条——重启后上游行情源/引擎预热期
    /api/overview 的 time 字段短暂不新鲜属预期，不计连续失败。
    只豁免 time_stale（超时）；http 错误、no_time 等仍计；
    l1/l3 失败不受豁免（端口没起来/审计断更仍要计，避免豁免掩盖真死）。
    """
    if not in_startup_grace(st):
        return False
    if levels.get("l1_tcp") != "ok":
        return False
    if not str(levels.get("l2_overview", "")).startswith("time_stale"):
        return False
    if not str(levels.get("l3_audit", "")).startswith("audit_age"):
        return False
    return True


# ★ F5+E3：晚间批处理窗口（risk_daily 20:00、forward_eval 20:30；ml_scores_ledger 16:30）。
#   窗口内磁盘/CPU 重 IO，l2_overview 短暂超时属预期。
#   E3 变更（2026-09-13，依据 watchdog.log 7 日实测）：
#   ① 原豁免只认 "time_stale"（HTTP 200 但 time 旧），而 09-12 晚间 20:20-21:40 密集超时
#      全是 "TimeoutError:timed out"（urlopen 超时）→ 豁免失效，3 连恢复误重启 3 次。
#      现把 l2 "短暂不可用"形态统一为 _l2_degraded（time_stale / TimeoutError / URLError）。
#   ② 改为"阈值放宽"而非"完全豁免"：窗口内或批处理运行标记存在时，恢复阈值 3→6
#      （约 30 分钟），防 3 连误杀，同时不掩盖真死（持续 >30 分钟仍会恢复）。
#   ③ 批处理运行标记（tmp/batch_running/*.flag，由批处理 .cmd 创建/删除）：09-11 19:23
#      ml_scores_ledger、09-12 20:30 forward_eval 两次 ^C 均与 watchdog 恢复动作重叠，
#      标记存在期间放宽 l2 阈值；标记 mtime 超 BATCH_FLAG_MAX_AGE 视为残留（批处理崩溃
#      未删）→ 豁免失效，防永久掩盖。
CRON_WINDOWS = ((20, 0, 21, 30),)
BATCH_FLAG_DIR = os.path.join(BASE, "tmp", "batch_running")
BATCH_FLAG_MAX_AGE = 3 * 3600      # 秒：批处理标记新鲜度（超时视为残留，豁免失效）
RECOVER_THRESHOLD_WIDE = 6         # 批处理窗口/标记内的放宽阈值（约 30 分钟）
# G1：audit 轮转宽限窗——TianjiAuditRotate 每日 00:05 归档 audit.jsonl 并新建空文件，
# 引擎下一跳心跳写入前的空窗（约 5-10 分钟）内 no_t_in_tail 不计连续失败（只记 INFO）。
# 配合轮转感知回退（check_audit → rotated_fallback）与 l2 新鲜豁免（engine_active_audit_silent）
# 三道兜底，根除 09-13 00:20 误杀。可配：改此元组即可，无需改 main 逻辑。
ROTATE_GRACE_WINDOW = ((0, 0, 0, 30),)


def in_cron_window(now=None):
    """当前是否在已知 cron 批处理窗口内。"""
    now = now or datetime.datetime.now()
    t = now.hour * 100 + now.minute
    for h0, m0, h1, m1 in CRON_WINDOWS:
        if (h0 * 100 + m0) <= t <= (h1 * 100 + m1):
            return True
    return False


def in_rotate_grace(now=None):
    """G1：当前是否在 audit 轮转宽限窗（默认 00:00-00:30）内。"""
    now = now or datetime.datetime.now()
    t = now.hour * 100 + now.minute
    for h0, m0, h1, m1 in ROTATE_GRACE_WINDOW:
        if (h0 * 100 + m0) <= t <= (h1 * 100 + m1):
            return True
    return False


def _l2_degraded(detail):
    """l2 失败是否属"短暂不可用"形态（批处理重 IO 下常见）：
    time_stale（HTTP 200 但 time 旧）/ TimeoutError / timed out / 连接拒绝(10061/10060)。
    l1_tcp=down 时 detail 也常为 URLError 10061，但调用方要求 l1=ok 才放宽，不冲突。"""
    d = str(detail)
    return (d.startswith("time_stale") or "TimeoutError" in d or "timed out" in d
            or "10061" in d or "10060" in d)


def batch_flag_active():
    """是否有新鲜的批处理运行标记（mtime ≤ BATCH_FLAG_MAX_AGE）。
    批处理 .cmd 启动时写 tmp/batch_running/<name>.flag、结束时删除；
    崩溃残留的旧标记（>3h）不生效。"""
    try:
        if not os.path.isdir(BATCH_FLAG_DIR):
            return False
        now = time.time()
        for fn in os.listdir(BATCH_FLAG_DIR):
            if fn.endswith(".flag"):
                p = os.path.join(BATCH_FLAG_DIR, fn)
                if now - os.path.getmtime(p) <= BATCH_FLAG_MAX_AGE:
                    return True
    except Exception:
        pass
    return False


def effective_recover_threshold(st, levels):
    """E3：批处理窗口内 或 批处理标记新鲜期间，且 l1=ok、l2 为"短暂不可用"形态
    → 恢复阈值放宽到 RECOVER_THRESHOLD_WIDE（6 次，约 30 分钟）；否则维持 3。
    l1 down / l3 断更不受放宽影响（不改变判据本身），防止掩盖真死。"""
    if levels.get("l1_tcp") == "ok" and _l2_degraded(levels.get("l2_overview", "")):
        # ★ 验收修复（2026-09-16）：原仅在批处理窗口/标记期间放宽。
        #   但 l1=ok + l2 仅“慢”（超时/time_stale）意味着进程活着且在服务，
        #   杀掉重启不会让上游行情源变快，反而造成盘中重启循环：
        #   09-16 12:15-13:10 实证 HTTP 500 → 进程死 → 拒绝启动×2 → 杀僵尸
        #   → 仍超时 → 再杀（overview 实测 11-56s，TDX 8s 超时降级所致）。
        #   故只要 l1=ok 且 l2 属“慢”形态就放宽到 6 次（约 30 分钟）；
        #   真死仍由 l1_tcp=down 与 HTTP 5xx（不属 _l2_degraded）走 3 次快速恢复。
        return RECOVER_THRESHOLD_WIDE
    return RECOVER_THRESHOLD


# --------------------------------------------------------------------------
# 恢复
# --------------------------------------------------------------------------
def pid_on_port(port=PORT):
    """netstat -ano 解析 LISTENING 该端口的 PID 列表。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, errors="replace", timeout=15,
                             creationflags=CREATE_NO_WINDOW).stdout or ""
    except Exception:
        return []
    pids = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        if ":%d" % port in line:
            parts = line.split()
            if parts and parts[-1].strip().isdigit():
                pids.add(int(parts[-1].strip()))
    return sorted(pids)


def cmdline_has(pid, needle="main.py"):
    """检查进程命令行是否含 needle（避免误杀非本服务进程）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % pid],
            capture_output=True, text=True, errors="replace", timeout=15,
            creationflags=CREATE_NO_WINDOW).stdout or ""
        return needle in out
    except Exception:
        return False


def _pid_alive_wd(pid):
    """Windows: OpenProcess 探测进程存活。
    ★ G2（2026-09-13）修复句柄残留假阳性：taskkill 成功后进程对象可能因其他进程
      未关闭的句柄而短暂"可打开"，纯 OpenProcess 误报存活（00:20 事故：43336 实际
      已被 taskkill 终止，但 OpenProcess 返回 True → 误 CRITICAL + 强删 app.lock +
      启动新实例的连锁）。改用 GetExitCodeProcess：已终止进程返回真实退出码
      （≠STILL_ACTIVE=259）→ 判死；存活进程返回 259 → 判活。"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True   # 查询失败保守视为存活
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def kill_pid(pid, max_retry=2):
    """★ F5：taskkill /F 后轮询确认进程真的消失；最多重试 max_retry 次，间隔 2s。
    返回 True=已消失；False=仍存活（调用方记 CRITICAL 并留孤儿 PID 到状态文件）。
    旧实现 kill 后不校验不重试 → 今日遗留孤儿 38504。"""
    for i in range(max_retry + 1):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        # 轮询确认消失（最多等 2s×2 轮）
        for _ in range(2):
            if not _pid_alive_wd(pid):
                return True
            time.sleep(1)
        if i < max_retry:
            time.sleep(2)
    return not _pid_alive_wd(pid)


def kill_pid_tree(pid, max_retry=2):
    """G2：更强终止——taskkill /T /F（连带子进程树），用于孤儿升级处理。
    返回 True=已消失；False=仍存活（调用方只告警不重启，需人工介入）。"""
    for i in range(max_retry + 1):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        for _ in range(2):
            if not _pid_alive_wd(pid):
                return True
            time.sleep(1)
        if i < max_retry:
            time.sleep(2)
    return not _pid_alive_wd(pid)


def clear_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def resolve_python():
    """返回能运行 main.py 的解释器（须带 mootdx）。
    实测：本机 PATH 首位是 Doubao 沙箱 python（无 mootdx），而运行中的服务
    用 C:\\Users\\26838\\AppData\\Local\\Programs\\Python\\Python313\\python.exe（带 mootdx）。
    若当前解释器即项目 Python313 则直接用；否则回退到已核实的 Python313 路径；
    都不可用才退回 sys.executable。
    """
    me = sys.executable or ""
    if me and os.path.exists(me) and "Python313" in me:
        return me
    alt = r"C:\Users\26838\AppData\Local\Programs\Python\Python313\python.exe"
    if os.path.exists(alt):
        return alt
    return me or "python"


def _child_log_name(kind):
    """按次唯一子进程日志名：watchdog_child_<kind>_<YYYYmmdd_HHMMSS>_<seq>.log。
    seq 用于同一秒内多次启动的区分（第 1 代 seq=1）。E2 P0：杜绝固定文件名
    被僵尸进程握持句柄后重定向失败阻塞恢复启动。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(LOG_DIR, "watchdog_child_%s_%s" % (kind, ts))
    seq = 1
    p = "%s_%d.log" % (base, seq)
    while os.path.exists(p):
        seq += 1
        p = "%s_%d.log" % (base, seq)
    return p


# K7-4（2026-09-16）：日志保留天数上限（close_update_cron 归档 + 子进程日志天数维度）
_MAX_LOG_AGE_S = 30 * 24 * 3600


def _prune_child_logs(keep=10):
    """只保留最近 keep 代子进程日志（stdout+stderr 同代各一文件，共 ≤2*keep 份），
    更老删除。★ G4：keep=5→10（≤20 份文件）；单份 >5MB 优先删除（防失控膨胀）。
    ★ K7-4：增加天数维度——mtime >30 天直接删除（与代数/大小清理并存）。
    按文件名中 <YYYYmmdd_HHMMSS>_<seq> 排序判断代数。"""
    try:
        if not os.path.isdir(LOG_DIR):
            return
        gens = {}
        for fn in os.listdir(LOG_DIR):
            m = re.match(r"watchdog_child_(?:stdout|stderr)_(\d{8}_\d{6})_(\d+)\.log$", fn)
            if m:
                gens.setdefault((m.group(1), int(m.group(2))), []).append(fn)
        # K7-4 天数维度：>30 天文件直接删除（优先于代数/大小逻辑）
        now = time.time()
        for gen, fns in list(gens.items()):
            for fn in list(fns):
                try:
                    if now - os.path.getmtime(os.path.join(LOG_DIR, fn)) > _MAX_LOG_AGE_S:
                        os.remove(os.path.join(LOG_DIR, fn))
                        fns.remove(fn)
                except Exception:
                    pass
            if not fns:
                gens.pop(gen, None)
        # 单份上限 5MB：超限文件直接删除（优先于代数清理）
        for gen, fns in list(gens.items()):
            for fn in list(fns):
                try:
                    if os.path.getsize(os.path.join(LOG_DIR, fn)) > 5 * 1024 * 1024:
                        os.remove(os.path.join(LOG_DIR, fn))
                        fns.remove(fn)
                except Exception:
                    pass
            if not fns:
                gens.pop(gen, None)
        for gen in sorted(gens)[:-keep] if len(gens) > keep else []:
            for fn in gens[gen]:
                try:
                    os.remove(os.path.join(LOG_DIR, fn))
                except Exception:
                    pass
    except Exception:
        pass


def _rotate_close_cron_log():
    """K7-4（2026-09-16）：data/close_update_cron.log 大小轮转 + 归档 30 天清理。
    close_update.cmd 用 >> 追加且自身无轮转（实测 2.78MB 持续增长）。
    >5MB → 改名归档 close_update_cron_<YYYYMMDD_HHMMSS>.log（cmd 下次 >> 自动建新文件；
    该 cmd 为短进程非持续占用，Windows rename 安全）；归档 mtime >30 天删除。
    由 watchdog 每 5 分钟顺带执行（tools/* 写权限内，开销 = 一次 stat + listdir）。"""
    try:
        base_dir = os.path.join(BASE, "data")
        p = os.path.join(base_dir, "close_update_cron.log")
        if os.path.isfile(p) and os.path.getsize(p) > 5 * 1024 * 1024:
            arch = os.path.join(base_dir, "close_update_cron_%s.log" %
                                time.strftime("%Y%m%d_%H%M%S"))
            os.replace(p, arch)
            log("K7-4 日志轮转：close_update_cron.log 超 5MB，归档为 %s" %
                os.path.basename(arch))
        now = time.time()
        for fn in os.listdir(base_dir):
            if fn.startswith("close_update_cron_") and fn.endswith(".log"):
                ap = os.path.join(base_dir, fn)
                if now - os.path.getmtime(ap) > _MAX_LOG_AGE_S:
                    os.remove(ap)
    except Exception:
        pass


def _run_cmd(cmd, timeout=20):
    """H1b①②：启动子进程的唯二安全通道——绝不捕获管道（DEVNULL），wait(timeout) 有界，
    超时 kill 后 wait(5) 收尸再返回 (-99, 'timeout')。
    铁律：恢复动作永远不许被重定向/输出收集阻塞。
    Windows 下 close_fds=True 安全且为默认（无 pipe 继承，DEVNULL 走 NUL 设备，
    孙进程不会持有任何管道写端）。返回 (rc, detail)。"""
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", cmd],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW, close_fds=True)
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)          # 收尸：有界，绝不无超时等待
        except Exception:
            pass
        return -99, "timeout>%ds" % timeout
    return rc, ("ok" if rc == 0 else "rc=%s" % rc)


def start_service():
    """Start-Process 启动 main.py（hidden）。用 resolve_python() 确保带 mootdx 的解释器。
    E2 P0 修复（唯一文件名 + 失败降级）：
      - 重定向目标改为按次唯一文件名（watchdog_child_<kind>_<ts>_<seq>.log），只保留
        最近 5 代——固定文件名被僵尸握持句柄不再阻塞恢复启动（09-03 事故根因）；
      - 带重定向 Start-Process 失败/异常 → 立即重试不带重定向并记日志，
        **恢复动作永远不许被重定向阻塞**（铁律）。
    """
    py = resolve_python()
    stdout_f, stderr_f = _child_log_name("stdout"), _child_log_name("stderr")
    _prune_child_logs()
    # ★ G4（2026-09-13）：Start-Process 前先设 PYTHONIOENCODING/PYTHONUTF8 供子进程继承，
    #   与 main.py 的 sys.stderr.reconfigure 双保险——重定向日志必须 UTF-8（原按 locale GBK
    #   编码，含中文 traceback 全 mojibake，如 C:\Users\26838\A?????）。
    cmd = ("$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; "
           "Start-Process -FilePath '%s' -ArgumentList 'main.py','--no-window' "
           "-WorkingDirectory '%s' -WindowStyle Hidden "
           "-RedirectStandardOutput '%s' -RedirectStandardError '%s'" %
           (py, BASE, stdout_f, stderr_f))
    # H1b①②：绝不用 capture_output（管道被孙进程 main.py 继承写端 → 超时 kill 后
    #   communicate() 无超时等待 = 永久挂起，今日 12:15/22:00 两次事故根因）。
    rc, d = _run_cmd(cmd, timeout=20)
    if rc == 0:
        return True
    log("重定向启动 %s（rc=%s），重定向不可用，降级启动（不带重定向）" % (d, rc))
    # 降级：不带重定向重试（铁律：恢复动作永不因重定向失败而阻塞）；同样带 UTF-8 env
    rc2, d2 = _run_cmd(
        "$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; "
        "Start-Process -FilePath '%s' -ArgumentList 'main.py','--no-window' "
        "-WorkingDirectory '%s' -WindowStyle Hidden" % (py, BASE), timeout=20)
    if rc2 == 0:
        return True
    log("降级启动 %s（rc=%s），启动命令执行失败" % (d2, rc2))
    return False


def read_lock_pid():
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _audit_record(kind, event, level="INFO", **fields):
    """★ K12（2026-09-16）：恢复/拒绝动作落 audit 哈希链。
    watchdog 自身保持"纯标准库、不 import app/*"纪律 → subprocess 调 app.audit.record
    （恢复/拒绝动作低频，每 5 分钟至多一次，进程开销可忽略；哈希链兼容由官方实现
    保证：跨进程锁 + 轮转 + _g3 状态机）。失败仅 log——audit 是增强观测，
    绝不阻塞恢复主流程。"""
    try:
        rec = {"kind": kind, "event": event, "level": level, **fields}
        code = ("import json,sys;from app import audit;"
                "audit.record(**json.loads(sys.stdin.read()))")
        subprocess.run([resolve_python(), "-c", code],
                       input=json.dumps(rec, ensure_ascii=False).encode("utf-8"),
                       capture_output=True, timeout=30,
                       creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        log("audit 落盘失败（不阻塞恢复）: %s" % e)


def _proc_age(pid):
    """进程龄（秒）；-1=取不到（保守起见视为未知，不触发救援）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Id %d -ErrorAction SilentlyContinue) | "
             "ForEach-Object { [int]([datetime]::Now - $_.StartTime).TotalSeconds }"
             % pid],
            capture_output=True, text=True, errors="replace", timeout=15,
            creationflags=CREATE_NO_WINDOW).stdout or ""
    except Exception:
        return -1.0
    try:
        return float(out.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return -1.0


def confirm_no_duplicate():
    """G2 三重确认：启动新实例前必须全部满足——
    ①8899 端口无监听（check_tcp）；
    ②无 CommandLine 含 main.py 的存活进程（CIM/WMI，防 kill 失败未死透的旧实例；
       也排除 watchdog 自身——其命令行是 service_watchdog.py 不含 main.py）；
    ③data/app.lock 持有者 PID 已不存在（防锁被劫持/陈旧）。
    任一不满足 → (False, 原因)；全部满足 → (True, "ok")。
    ★B-G2（2026-09-16，验收方修复）：新增"半死实例救援"分支——
    端口已关但 main.py 进程仍在 = HTTP 服务线程已死、主循环苟活的半死实例。
    原判据一律拒绝 → 当日 12:20~12:41 生产停摆 21 分钟，watchdog 每 5 分钟喊
    "需人工介入"却无法自救（实测 21996 半死 28 分钟）。
    新判据：main.py 进程龄 >180s 且 8899 无监听 → 判定半死，
    kill_pid 后 clear_lock 并放行重启（返回 half_dead_killed_pids）；
    进程龄 ≤180s（刚启动还没绑端口的正常窗口）→ 维持拒绝，防竞态误杀。"""
    if check_tcp(PORT):
        return False, "port_8899_listening"
    try:
        # ★ 验收修复（2026-09-14）：原查询 CommandLine -like '*main.py*' 扫描全部进程，
        #   而承载该查询的 powershell 子进程自身命令行就含 main.py 字面量 → 自匹配，
        #   pids 恒非空 → 三重确认永不通过 → 自动恢复永久失效。
        #   实证：09-14 00:10/00:25/00:40 三次拒绝启动，报告 PID 各不相同
        #   （17200 / 21608 / 18336，即每次新建的 powershell 自身），服务实际早已不存在。
        #   修法：先用 Name 限定 python/pythonw（服务为 pythonw.exe main.py --no-window），
        #   powershell 不可能匹配自身；全用单引号避免 Windows 嵌套双引号被破坏。
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process | Where-Object { "
             "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') "
             "-and $_.CommandLine -like '*main.py*' } | "
             "Select-Object -ExpandProperty ProcessId) -join ','"],
            capture_output=True, text=True, errors="replace", timeout=20,
            creationflags=CREATE_NO_WINDOW).stdout or ""
        pids = [int(x) for x in out.replace(",", " ").split() if x.strip().isdigit()]
    except Exception:
        pids = []
    pids = [p for p in pids if p != os.getpid()]   # 双保险：排除 watchdog 自身
    if pids:
        # ★B-G2：半死实例救援（端口已关 + 进程龄超 180s → kill 放行）。
        stale = [p for p in pids if _proc_age(p) > 180]
        if stale:
            killed = [p for p in stale if kill_pid(p)]
            if killed:
                _lock_pid = read_lock_pid()
                # ★ K12（2026-09-16）：lock PID 与实际存活进程一致性校验。
                #   今日曾现 lock=18972 而实际 main.py=34096 的错配——kill 后若
                #   lock 内 PID 仍存活且不在被杀列表 → 错配残留（该 PID 非本服务
                #   进程，清锁放行但记录），杜绝"锁指死 PID 而实际进程存活"隐患。
                _lock_mismatch = (_lock_pid and _lock_pid not in killed
                                  and _pid_alive_wd(_lock_pid))
                if _lock_mismatch:
                    log("K12 lock PID 错配：lock=%d 存活但不在被杀列表 %s（残留非服务进程）"
                        % (_lock_pid, killed))
                clear_lock()
                if not [p for p in pids if p not in killed and _pid_alive_wd(p)]:
                    return True, ("half_dead_killed_pids=%s lock_pid=%s lock_mismatch=%s "
                                  "(port closed, HTTP thread dead, aged>=%ss)"
                                  % (stale, _lock_pid, _lock_mismatch, 180))
                return False, "zombies_left_after_kill=%s" % (
                    [p for p in pids if p not in killed and _pid_alive_wd(p)])
            return False, "half_dead_kill_failed=%s（升级人工）" % stale
        return False, "main_py_running_pids=%s(进程龄<180s，等待绑端口)" % pids
    lock_pid = read_lock_pid()
    if lock_pid and _pid_alive_wd(lock_pid):
        return False, "lock_holder_alive_pid=%d" % lock_pid
    return True, "ok"


def recover():
    """三级检查连续失败后的恢复。先探 8899 防双实例。返回 action。
    H1b③：每个出口必落日志（含 旧PID/新PID/耗时/rc）；H1b②：所有启动走 _run_cmd
    有界超时，恢复动作永不阻塞。"""
    _t0 = time.time()
    log(">>> 恢复触发（连续失败≥%d）" % RECOVER_THRESHOLD)
    # ★ K12：downtime 估算素材——连续失败 N 次 × 5 分钟 + 本次恢复耗时
    _fail_count = load_state().get("consecutive_failures", 0)
    action = "noop"
    old_pid = read_lock_pid() or (pid_on_port(PORT)[0] if pid_on_port(PORT) else 0)

    if check_tcp(PORT):
        # 端口被占：可能健康实例或半死僵尸 → 二次复核
        ov_ok, ov_d = check_overview()
        au_ok, au_d = check_audit()
        if ov_ok and au_ok:
            log("服务实际健康（overview=%s audit=%s），跳过恢复（防双实例，耗时=%.1fs）"
                % (ov_d, au_d, time.time() - _t0))
            return "skip_healthy"
        # 半死僵尸：终止占用 8899 且为 main.py 的本服务进程
        target = None
        for pid in pid_on_port(PORT):
            if cmdline_has(pid, "main.py"):
                target = pid
                break
        if target:
            log("半死僵尸 PID=%d（overview=%s audit=%s），终止后重启" % (target, ov_d, au_d))
            if not kill_pid(target):
                # ★ G2（2026-09-13）：kill 失败 → **中止本次恢复，绝不启动新实例**。
                #   原实现只记 CRITICAL 就继续 clear_lock+start_service，制造"孤儿 + 新实例"
                #   并存的双实例窗口（00:20 事故：孤儿 43336 + 新 52044），双实例会导致
                #   重复下单 / account.json 写冲突 / SQLite 写锁争用 / audit 事件交错。
                #   现在：不删 app.lock、不启动；orphan_pid/orphan_first_seen 入状态文件，
                #   由后续轮次 main() 的孤儿接管逻辑升级处理（/T /F 进程树 → 仍失败则只告警）。
                #   watchdog 只读 audit 红线不变：CRITICAL 落 watchdog.log + 状态文件。
                _st = load_state()
                _st["orphan_pid"] = target
                _st["orphan_first_seen"] = now_str()
                _orphans = _st.get("orphan_pids") or []
                if target not in _orphans:
                    _orphans.append(target)
                _st["orphan_pids"] = _orphans[-20:]
                _st.pop("needs_human", None)   # 重置：下一轮将尝试升级 /T /F 终止
                save_state(_st)
                log("CRITICAL G2 kill_pid 失败：PID %d 重试后仍存活（孤儿进程）。"
                    "中止恢复、不启动新实例（防双实例窗口）。orphan_pid=%d 已记入状态文件，"
                    "后续轮次将升级 /T /F 终止" % (target, target))
                _audit_record("daily", "watchdog_refused", "CRITICAL",
                              action="abort_orphan_alive",
                              reason="kill_pid_failed_orphan_%d" % target,
                              pids_killed=[], pids=[target],
                              downtime_s=int(_fail_count * 300 + (time.time() - _t0)))
                return "abort_orphan_alive"
            time.sleep(2)
            old_pid = target
        else:
            log("8899 被非 main.py 进程占用（PID=%s），不终止、跳过恢复（耗时=%.1fs）"
                % (pid_on_port(PORT), time.time() - _t0))
            return "skip_foreign"
        clear_lock()
    else:
        log("端口 8899 关闭（全死），直接重启")

    # G2 三重确认：启动新实例前必须 ①无 8899 监听 ②无 main.py 存活进程 ③lock 持有者已死。
    # 任一不满足 → 拒绝启动（防孤儿未死透/端口竞态下的双实例）。
    ok_dup, why = confirm_no_duplicate()
    _dup_why = why
    if not ok_dup:
        log("G2 三重确认未过，拒绝启动新实例：%s（需人工介入或下一轮重试）" % why)
        _st = load_state()
        _st["needs_human"] = "dup_confirm_%s" % why
        save_state(_st)
        _audit_record("daily", "watchdog_refused", "WARN",
                      action="dup_abort", reason="dup_confirm_%s" % why,
                      pids_killed=[], downtime_s=int(_fail_count * 300 + (time.time() - _t0)))
        return "dup_abort"

    if not start_service():
        log("恢复启动执行失败，请人工介入（耗时=%.1fs）" % (time.time() - _t0))
        _audit_record("daily", "watchdog_refused", "CRITICAL",
                      action="start_failed",
                      reason="start_service_rc_failed",
                      pids_killed=[], downtime_s=int(_fail_count * 300 + (time.time() - _t0)))
        return "start_failed"
    action = "restarted"
    # 等待新 PID 落盘（main.py 启动写 data/app.lock），最长 30s
    new_pid = 0
    for _ in range(30):
        time.sleep(1)
        new_pid = read_lock_pid()
        if new_pid and new_pid != old_pid:
            break
    # 回退：main.py 清僵尸锁后不重写新 PID（既有怪癖），改从端口反查新实例 PID
    if not new_pid or new_pid == old_pid:
        for pid in pid_on_port(PORT):
            if pid != old_pid and cmdline_has(pid, "main.py"):
                new_pid = pid
                break
    log("恢复完成：旧PID=%s 新PID=%s（动作=%s，耗时=%.1fs）"
        % (old_pid, new_pid, action, time.time() - _t0))
    # ★ K12（2026-09-16）：恢复动作落 audit（watchdog_recovered 补 action/reason/
    #   pids_killed/downtime_s；pids_killed 从 confirm_no_duplicate 的半死救援返回值解析）
    try:
        _killed = []
        _km = re.search(r"half_dead_killed_pids=(\[[^\]]*\])", _dup_why or "")
        if _km:
            _killed = [int(x) for x in _km.group(1).strip("[]").split(",") if x.strip()]
        _downtime_est = int(_fail_count * 300 + (time.time() - _t0))
        _audit_record("daily", "watchdog_recovered", "INFO",
                      action=action,
                      reason=(_dup_why if (_dup_why and _dup_why != "ok")
                              else "port_down_full_restart"),
                      pids_killed=_killed, downtime_s=_downtime_est,
                      lock_pid=old_pid or 0)
    except Exception as _e:
        log("watchdog_recovered audit 构造失败: %s" % _e)
    return action


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="H1 看门狗：三级健康判据 + 半死恢复")
    ap.add_argument("--verify", action="store_true",
                    help="只读体检：不改状态、不恢复（供人工验证）")
    args = ap.parse_args()

    # H1b⑤ 进程级自退兜底：总时限 90s，未知阻塞 → log TIMEOUT-BAIL 后 os._exit(3)
    #   （与 H2 的 ExecutionTimeLimit=PT2M 互为双保险；daemon 线程不阻塞主流程。
    #   必须在本函数任何可能阻塞的调用（含 health_check）之前启动——自检3曾证明
    #   原位置在 health_check 阻塞时永远执行不到，兜底失效）。
    def _bail():
        log("TIMEOUT-BAIL watchdog 总时限超时（>90s），os._exit(3)")
        try:
            os._exit(3)
        except Exception:
            pass
    _bail_timer = threading.Timer(90.0, _bail)
    _bail_timer.daemon = True
    _bail_timer.start()

    ok, levels = health_check()
    detail = " | ".join("%s=%s" % (k, v) for k, v in levels.items())

    if args.verify:
        # H1b⑥ 外部性：--verify 只读模式仍不写状态、不恢复、不写心跳
        print("verify: %s | %s" % ("OK" if ok else "UNHEALTHY", detail))
        return 0

    st = load_state()
    # K7-4：每轮顺带做日志轮转（close_update_cron 大小轮转+归档清理；--verify 只读模式已提前返回）
    _rotate_close_cron_log()
    # H1b④ 陈旧计数复位：上一轮若卡死/异常未收尾，加载到 >=THRESHOLD 先归零再 log
    #   ——日志中永不得再出现 "异常 N/3"（N>3）。
    if st.get("consecutive_failures", 0) >= RECOVER_THRESHOLD:
        log("陈旧计数复位：consecutive_failures=%d（>=%d）→ 0（上一轮可能未正常收尾）"
            % (st.get("consecutive_failures", 0), RECOVER_THRESHOLD))
        st["consecutive_failures"] = 0

    # ★ G2 孤儿进程接管：kill 失败中止恢复后，由后续轮次升级处理——
    #   孤儿已消失 → 清除状态走正常流程；仍存活 → 尝试 taskkill /T /F 杀进程树；
    #   升级终止仍失败 → 只告警不重启（needs_human 入状态文件），避免双实例窗口。
    _op = st.get("orphan_pid") or 0
    if _op:
        if not _pid_alive_wd(_op):
            log("G2 孤儿 PID %d 已消失，清除 orphan 状态，恢复正常流程" % _op)
            st.pop("orphan_pid", None)
            st.pop("orphan_first_seen", None)
            st.pop("needs_human", None)
            save_state(st)
        elif st.get("needs_human"):
            # 已标记需人工介入（升级 kill 已失败）：静默保持，不刷屏、不重启
            return 0
        else:
            log("G2 孤儿升级：PID %d 仍存活（首见 %s），尝试 taskkill /T /F 杀进程树"
                % (_op, st.get("orphan_first_seen", "")))
            if kill_pid_tree(_op):
                log("G2 孤儿 PID %d 已被 /T /F 终止，清除 orphan 状态，恢复正常流程" % _op)
                st.pop("orphan_pid", None)
                st.pop("orphan_first_seen", None)
                save_state(st)
            else:
                st["needs_human"] = "orphan_kill_tree_failed_%d" % _op
                save_state(st)
                log("CRITICAL G2 孤儿 PID %d 升级 /T /F 终止仍失败：只告警不重启，"
                    "需人工介入（needs_human=%s，详见状态文件）" % (_op, st["needs_human"]))
                return 0

    # ★ F5 退出码语义：健康/服务异常（已记日志）/豁免/恢复执行 → 0；
    #   仅"自身故障/恢复失败需人工介入"才返回非 0（避免污染计划任务历史）。
    rc = 0
    try:
        # R2-P0.3：运行即写心跳；自检调度断更（距上次心跳>10min=计划任务漏跑/延迟）。
        #   audit 告警由引擎侧 trader._monitor 落盘（watchdog 只读 audit，不写链）。
        _prev_hb = heartbeat_age_seconds()
        touch_heartbeat()
        if _prev_hb is not None and _prev_hb > HEARTBEAT_MAX_GAP:
            log("R2-P0.3 调度断更告警：距上次心跳 %.0fs(>%ds)，计划任务可能漏跑/延迟" %
                (_prev_hb, HEARTBEAT_MAX_GAP))
        if ok:
            st["consecutive_failures"] = 0
            st["last_status"] = "ok"
            st["last_check"] = now_str()
            save_state(st)
            log("健康 OK：%s（连续失败已清零）" % detail)
            return 0

        # P0② 启动豁免：重启后 180s 内，l2_overview 超时不计入连续失败（防上游源故障误判连环杀）
        if startup_grace_applies(st, levels):
            st["last_status"] = "grace"
            st["last_check"] = now_str()
            save_state(st)
            log("启动豁免(l2)：实例出生 %.0fs，l2 超时不累计（连续失败保持 %d）" %
                (restart_age_seconds(st), st.get("consecutive_failures", 0)))
            return 0

        # G1 轮转宽限：00:00-00:30 内 audit 轮转空窗，l3 no_t_in_tail 不计连续失败
        # （仅日志 INFO；轮转感知回退与 l2 新鲜豁免已在上游 health_check 兜底，
        #   此分支处理"回退也失败"的极端空窗，避免三连误杀健康实例）
        if in_rotate_grace() and str(levels.get("l3_audit", "")).startswith("no_t_in_tail"):
            st["last_status"] = "rotate_grace"
            st["last_check"] = now_str()
            save_state(st)
            log("轮转宽限(l3)：00:00-00:30 audit 轮转空窗，l3 no_t_in_tail 不计连续失败（连续失败保持 %d）" %
                st.get("consecutive_failures", 0))
            return 0

        # E3：批处理窗口/标记内的 l2 短暂不可用 → 阈值放宽（3→6），非完全豁免
        thr = effective_recover_threshold(st, levels)
        st["consecutive_failures"] = st.get("consecutive_failures", 0) + 1
        st["last_status"] = "fail"
        st["last_check"] = now_str()
        save_state(st)
        log("异常 %d/%d：%s%s" % (st["consecutive_failures"], thr, detail,
            "（批处理放宽阈值）" if thr > RECOVER_THRESHOLD else ""))
        if st["consecutive_failures"] >= thr:
            action = recover()
            if action == "restarted":
                st["restart_ts"] = now_str()
                log("已记录 restart_ts=%s，进入 %d 秒启动豁免" % (st["restart_ts"], STARTUP_GRACE))
            elif action in ("start_failed", "abort_orphan_alive", "dup_abort"):
                # ★ F5+G2：恢复失败/孤儿中止/三重确认拒绝 → 需人工介入 → 非 0
                #   （watchdog 自身无法完成职责的唯一非0出口）
                rc = 1
            # 恢复尝试后清计数，交由下一轮重新累计（避免每 5 分钟都触发）
            st["consecutive_failures"] = 0
            st["last_status"] = "recovered"
            st["last_check"] = now_str()
            save_state(st)
    finally:
        # H1b④ 状态必复位：即使 recover() 抛异常也保存状态（恢复后计数已清）
        try:
            save_state(st)
        except Exception:
            pass
        # H1b⑥ touch_heartbeat 语义保持"运行即写"，收尾再写一次，失败不影响退出
        try:
            touch_heartbeat()
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
