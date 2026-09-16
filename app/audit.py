# -*- coding: utf-8 -*-
"""交易审计日志（v3.5）—— 借鉴 vnpy 事件流持久化 / xuanji 可审计可回滚 / llm-quant 哈希链防篡改
统一事件流：信号 → 风控校验 → 委托 → 成交回报 → 持仓变动，全链路落本地文件。
每条记录：时间戳 + JSON 结构化字段 + 上一条哈希（哈希链，防篡改）。
回测引擎与实盘交易中心共用同一记录器，Web"审计"页按日期回放。
"""
import hashlib
import json
import os
import threading
import time

from . import config as C

_AUDIT_DIR = os.path.join(C.DATA_DIR, "audit")
_AUDIT_FILE = os.path.join(_AUDIT_DIR, "audit.jsonl")
_lock = threading.Lock()
_prev_hash = ""
_prev_date = ""
_buf = []          # 内存缓冲（Web 查询用）
_BUF_MAX = 2000
_ROTATE_BYTES = 50 * 1024 * 1024   # R2-P1.6：audit.jsonl 超 50MB 轮转归档

try:
    import msvcrt as _msvcrt      # Windows 跨进程字节锁
except ImportError:
    _msvcrt = None                # 非 Windows 部署降级为仅线程锁


def _ensure_dir():
    os.makedirs(_AUDIT_DIR, exist_ok=True)


def _today():
    return time.strftime("%Y-%m-%d")


class _cross_proc_lock:
    """★ 任务1-②：审计目录内的跨进程互斥。
    机制：对 <audit>/.write.lock 的首字节加排他锁（阻塞等待≤60s），
    使"读尾部→算 hash→追加落盘"成为跨进程原子段；
    msvcrt 不可用时降级为空操作（此时仅同进程互斥，链严格性依赖单进程）。"""

    def __enter__(self):
        self.f = None
        try:
            self.f = open(os.path.join(_AUDIT_DIR, ".write.lock"), "a+")
        except Exception:
            self.f = None
            return self
        if _msvcrt is not None:
            for _ in range(600):
                try:
                    _msvcrt.locking(self.f.fileno(), _msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            try:
                if _msvcrt is not None:
                    self.f.seek(0)
                    _msvcrt.locking(self.f.fileno(), _msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            finally:
                try:
                    self.f.close()
                except Exception:
                    pass
        return False


def _read_tail():
    """读文件尾最后一条可解析记录。返回 (date_str, hash) 或 (None, None)。
    容忍末行半截 JSON（断电/并发截断）：向回找最近一个完整行。"""
    if not os.path.exists(_AUDIT_FILE):
        return None, None
    try:
        with open(_AUDIT_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "ignore").splitlines()
        for ln in reversed(tail):
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            d = (rec.get("t") or "")[:10]
            return d, rec.get("hash", "")
    except Exception:
        pass
    return None, None


def _maybe_rotate():
    """R2-P1.6：按大小轮转。超 _ROTATE_BYTES → 当前 audit.jsonl 归档为
    audit_<YYYYmmdd_HHMMSS>.jsonl（同目录），新建空 audit.jsonl 由调用方继续写。
    轮转点为文件级链首（新文件首条 prev=""），verify_chain 按文件切换重置。
    必须持 _cross_proc_lock 调用；os.replace 原子。返回归档文件名或 None。"""
    try:
        if os.path.exists(_AUDIT_FILE) and os.path.getsize(_AUDIT_FILE) > _ROTATE_BYTES:
            ts = time.strftime("%Y%m%d_%H%M%S")
            dst = os.path.join(_AUDIT_DIR, "audit_%s.jsonl" % ts)
            if not os.path.exists(dst):
                os.replace(_AUDIT_FILE, dst)
                return os.path.basename(dst)
    except Exception:
        pass
    return None


def _content_major_date(path):
    """B4（2026-09-13）：统计 audit 文件内各日期事件数，返回占多数的日期
    （YYYYMMDD）或 None。用于按"被归档内容的日期"命名（而非执行时刻日期）。"""
    from collections import Counter
    cnt = Counter()
    try:
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln).get("t", "")[:10]
                if d:
                    cnt[d] += 1
            except Exception:
                pass
    except OSError:
        return None
    if not cnt:
        return None
    return cnt.most_common(1)[0][0].replace("-", "")


def rotate_daily(keep_days=90):
    """R3-E2（2026-09-10）+ B4（2026-09-13）：按日轮转（外部 schtasks 00:05 调用入口）。
    - 持 _cross_proc_lock 原子执行（与 record 互斥，杜绝"rename 与写并发"竞态）；
    - 将 audit.jsonl 归档为 audit_<内容多数日期>.jsonl（B4 修正：原按执行时刻日期命名，
      00:05 归档会把"昨天"的数据命名为"今天"，查某天审计需找后一天文件，反直觉）；
      内容跨多天时取占多数日期；同日已有归档（手动+计划任务同日等罕见场景）→ 加 _2 后缀
      保数据不覆盖；新建空 audit.jsonl 由后续 record 继续写（文件级链首语义不变）；
    - 清理超过 keep_days 的归档（兼容命名：audit_YYYYMMDD.jsonl 与
      audit_YYYYmmdd_HHMMSS.jsonl——解析前缀时间戳）。
    返回 {"rotated": 归档名或 None, "removed": [归档名...]}。
    注意：本函数由独立脚本进程调用，模块级 _prev_hash 无需维护（record 每次
    _sync_prev_locked 以磁盘尾部为准）。"""
    out = {"rotated": None, "removed": []}
    with _cross_proc_lock():
        _ensure_dir()
        # 1) 按内容日期归档（非空才归档；空文件直接保留为当日新链首）
        if os.path.exists(_AUDIT_FILE) and os.path.getsize(_AUDIT_FILE) > 0:
            d = _content_major_date(_AUDIT_FILE) or time.strftime("%Y%m%d")
            dst = os.path.join(_AUDIT_DIR, "audit_%s.jsonl" % d)
            if os.path.exists(dst):  # 罕见：同日已有归档 → 后缀保数据
                dst = os.path.join(_AUDIT_DIR, "audit_%s_2.jsonl" % d)
            os.replace(_AUDIT_FILE, dst)
            out["rotated"] = os.path.basename(dst)
        # 1b) 确保活动文件存在（归档后新建空 audit.jsonl；幂等 touch）
        if not os.path.exists(_AUDIT_FILE):
            open(_AUDIT_FILE, "a", encoding="utf-8").close()
        # 2) 超期清理
        cutoff = time.time() - keep_days * 86400
        try:
            for fn in os.listdir(_AUDIT_DIR):
                if not (fn.startswith("audit_") and fn.endswith(".jsonl")):
                    continue
                ts = fn[len("audit_"):-len(".jsonl")]  # YYYYMMDD 或 YYYYmmdd_HHMMSS
                try:
                    t = time.mktime(time.strptime(ts, "%Y%m%d"))
                except ValueError:
                    try:
                        t = time.mktime(time.strptime(ts, "%Y%m%d_%H%M%S"))
                    except ValueError:
                        continue
                if t < cutoff:
                    try:
                        os.remove(os.path.join(_AUDIT_DIR, fn))
                        out["removed"].append(fn)
                    except OSError:
                        pass
        except OSError:
            pass
    return out


def _sync_prev_locked(today):
    """★ 任务1-②核心（须持锁调用）：以磁盘真实尾部计算本次写入的 prev，
    替代旧的"启动时一次性恢复+内存维护"（那是并发断链根因——每个进程各自
    记忆链头，彼此不可见）。与 verify_chain 约定一致：每天首条 prev=""。"""
    global _prev_hash, _prev_date
    d, h = _read_tail()
    if d == today and h:
        prev = h
    else:
        prev = ""                  # 文件为空或尾部是更早日期 → 新一天链首
    _prev_hash = prev
    _prev_date = today
    return prev


# ============================================================
# ★ G3（2026-09-13）告警语义修正：watchdog_stale 状态机 + trading_event 降噪
# 背景：
#  - watchdog_stale CRITICAL 的 streak_s 恒 0、每次自称"首次告警"——写入端
#    （app/trader.py H4）的实例状态未跨轮次保持（trader.py 属 F 道，本块不改），
#    故在 record() 层做集中状态机：streak 从首次断更时刻累计（持久化 tmp/，跨
#    进程/轮次/重启保留），按 10/30/60 分钟升级 WARN→ERROR→CRITICAL，每级只发
#    一次、之后每 60 分钟重复当前级；节流内抑制（不落盘）消除告警疲劳。
#  - trading_event 的 WARN/ERROR 中约 96% 是"信息性业务事件"（情绪闸门/风控拦截/
#    盘中提示/冷却）被误标级别 → 白名单校准降 INFO（根因=设计行为，非数字游戏）；
#    "cannot schedule new futures / 后台扫描异常"类根因未修（F2 处理中）→ 保持
#    ERROR 但按归一化 msg 每 600s 至多 1 条去重。
# 字段口径与 D1 一致：level / event / streak_s / detail（trading_event 的详情在 msg）。
# ============================================================
import re as _re

_G3_STATE_FILE = os.path.join(os.path.dirname(C.DATA_DIR), "tmp", "g3_alert_state.json")

# 信息性 trading_event 白名单（WARN/ERROR → INFO；根因=设计行为：拦截/提示属正常功能）
_G3_CALM_PATTERNS = (
    "情绪[冰点]禁开仓", "风控拦截", "冲高回落", "下跌", "分时量拦截",
    "连续亏损冷却", "仓位超上限", "污染修复", "peak 污染修复",
    "触板后回落", "高位缩量",
)
# 根因未修、保持级别但需去重的模式（F2 处理中；后台扫描异常历史已修但保留 ERROR 观察复发）
_G3_DEDUPE_PATTERNS = ("cannot schedule new futures", "循环异常", "高频监控异常", "后台扫描异常")


def _g3_load_state():
    try:
        with open(_G3_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _g3_save_state(st):
    try:
        os.makedirs(os.path.dirname(_G3_STATE_FILE), exist_ok=True)
        with open(_G3_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass


def _g3_heartbeat_age(now):
    """K7-3（2026-09-16）：watchdog 心跳新鲜度 = 距上次心跳的秒数（与 detail
    "断更 N 分钟"同源，保证 streak 与文案自洽）。
    优先读 tmp/watchdog_heartbeat.ts（trader 同源）；心跳缺失/异常时回退读
    tmp/h1_watchdog_state.json 的墙钟/单调钟双记（wall_ts/mono_ts，watchdog
    每次 save_state 写入）：
      - 墙钟年龄 = now_wall - wall_ts（断更表面时长）
      - 单调钟年龄 = now_mono - mono_ts（机器唤醒后累计的活跃时长；
        Windows 单调钟在系统睡眠期间不前进）
      - sleep_gap = 墙钟年龄 - 单调钟年龄（>0 说明存在休眠空档）
    返回 (age_or_None, sleep_gap_s)。"""
    _tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp")
    try:
        _hb = os.path.join(_tmp, "watchdog_heartbeat.ts")
        if os.path.isfile(_hb):
            return now - os.path.getmtime(_hb), 0.0
    except Exception:
        pass
    try:
        with open(os.path.join(_tmp, "h1_watchdog_state.json"), "r", encoding="utf-8") as f:
            st = json.load(f)
        wall_ts = float(st.get("wall_ts") or 0)
        mono_ts = float(st.get("mono_ts") or 0)
        if wall_ts > 0 and mono_ts > 0:
            wall_age = max(0.0, now - wall_ts)
            mono_age = max(0.0, time.monotonic() - mono_ts)
            return wall_age, max(0.0, wall_age - mono_age)
    except Exception:
        pass
    return None, 0.0


def _g3_wd_stale_stateful(now_ts, level, detail):
    """watchdog_stale 状态机：返回 (level, streak_s, detail, emit_bool)。
    - ★ K7-3：streak = 距上次心跳的秒数（heartbeat age，与 detail 文案自洽）；
      心跳缺失时退回"距首次断更时刻"口径（dur），保证始终可判定。
    - 级别阶梯 10min→WARN、30min→ERROR、60min→CRITICAL；每级首条 + 之后每
      3600s 重复当前级；
    - 机器休眠区分：墙钟/单调钟差（sleep_gap）≥300s 时视为"休眠空档非真死"，
      级别上限压到 WARN（不升级 ERROR/CRITICAL、不触发恢复），detail 标注；
    - 节流内 → emit=False（不落盘，不破坏哈希链——链以磁盘尾部为准）。"""
    st = _g3_load_state()
    last_ts = float(st.get("wd_last_level_ts", 0) or 0)
    last_lvl = st.get("wd_last_level", "")
    now = now_ts or time.time()
    hb_age, sleep_gap = _g3_heartbeat_age(now)
    if hb_age is None:
        # 心跳不可得 → 退回首次断更口径
        start = st.get("wd_stale_start")
        if not start:
            start = now
            st["wd_stale_start"] = start
        dur = now - start
        if not last_lvl:
            st["wd_last_level_ts"] = now
    else:
        dur = max(0.0, hb_age)
    target = "WARN" if dur <= 1800 else ("ERROR" if dur <= 3600 else "CRITICAL")
    if sleep_gap >= 300:
        # 休眠空档：非看护真死，级别上限 WARN，标注
        if target != "WARN":
            target = "WARN"
            dur_note = "（含休眠空档 %ds，非看护真死）" % int(sleep_gap)
        else:
            dur_note = ""
    else:
        dur_note = ""
    if not last_lvl:
        st["wd_last_level"] = target
        st["wd_last_level_ts"] = now
        emit = True
    elif target != last_lvl:
        st["wd_last_level"] = target
        st["wd_last_level_ts"] = now
        emit = True
    elif now - last_ts >= 3600:
        st["wd_last_level_ts"] = now
        emit = True
    else:
        emit = False
    _g3_save_state(st)
    d2 = str(detail)
    d2 = d2.replace("，首次告警", "").replace("，持续中（每小时一条）", "")
    if emit:
        d2 = d2 + "（G3 %s，streak %ds%s）" % (target, int(dur), dur_note)
    return target, int(dur), d2, emit


def _g3_wd_recovered():
    """watchdog_recovered：清断更状态（下次断更重新计时）。"""
    st = _g3_load_state()
    for k in ("wd_stale_start", "wd_last_level_ts", "wd_last_level"):
        st.pop(k, None)
    _g3_save_state(st)


def _g3_trading_calibrate(level, fields):
    """trading_event 级别校准 + 去重。返回 (level, fields, emit)。"""
    msg = str(fields.get("msg", ""))
    if not msg:
        return level, fields, True
    if level in ("WARN", "ERROR"):
        for pat in _G3_CALM_PATTERNS:
            if pat in msg:
                return "INFO", fields, True
    for pat in _G3_DEDUPE_PATTERNS:
        if pat in msg:
            st = _g3_load_state()
            key = "dedupe_" + _re.sub(r"[\W\d]+", "", msg)[:40]
            last = float(st.get(key, 0) or 0)
            now = time.time()
            if now - last < 600:
                return level, fields, False
            st[key] = now
            _g3_save_state(st)
            break
    return level, fields, True


def record(kind, event, level="INFO", **fields):
    """写入一条审计事件。
    kind: 事件类型（signal/risk/order/fill/position/alert/daily）
    event: 事件名（如 buy_rejected / scan_done / strategy_buy）
    level: INFO/WARN/ERROR/BUY/SELL
    fields: 附加结构化字段

    ★ 任务1-②（并发链修复）：写入全程= 进程内 threading.Lock ＋ 跨进程
    .write.lock 字节锁；prev 一律以"写前重读磁盘尾部"为准（单写者语义），
    并 flush+fsync 落盘。据此新事件的哈希链在多工具/多进程交错下保持连续
    （历史断裂不修补，见 tools/audit_chain_diag.py 基线）。
    """
    with _lock:
        # ★ G3：告警语义修正（状态机实现在上方 _g3_* 函数）——哈希链落盘前接管
        #   level/streak_s/msg；抑制时不落盘（链以磁盘尾部为准，不破坏连续性）。
        if event == "watchdog_stale" and level in ("WARN", "ERROR", "CRITICAL"):
            _lvl, _streak, _det, _emit = _g3_wd_stale_stateful(None, level, fields.get("detail", ""))
            if not _emit:
                return None
            level = _lvl
            fields["detail"] = _det
            fields["streak_s"] = _streak
        elif event == "watchdog_recovered":
            _g3_wd_recovered()
        elif event == "trading_event":
            level, fields, _emit = _g3_trading_calibrate(level, fields)
            if not _emit:
                return None
        with _cross_proc_lock():
            _ensure_dir()
            today = _today()
            # R2-P1.6：写前检查大小，超限则轮转（持锁段内原子，轮转点为新链首）
            _maybe_rotate()
            prev = _sync_prev_locked(today)
            rec = {
                "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "event": event,
                "level": level,
                **fields,
            }
            # 哈希链：prev_hash 参与本次哈希计算
            payload = json.dumps(rec, ensure_ascii=False, sort_keys=True)
            rec["hash"] = hashlib.sha256(
                (payload + "|" + prev).encode("utf-8")).hexdigest()[:16]
            rec["prev"] = prev
            line = json.dumps(rec, ensure_ascii=False)
            try:
                with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())       # 落盘同步：进程崩溃不丢已确认事件
            except Exception:
                pass
            _buf.append(rec)
            if len(_buf) > _BUF_MAX:
                _buf[:100] = []
            return rec


def query(day=None, kind=None, limit=500):
    """查询审计日志。day: 'YYYY-MM-DD'；kind: 事件类型过滤"""
    with _lock:
        rows = list(_buf)
    if day:
        rows = [r for r in rows if r.get("t", "").startswith(day)]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows[-limit:]


# ============================================================
# ★ D1（2026-09-13）告警双轨打通：quality_alert.jsonl → audit 转发 / 升级 / 轮转
# 痛点：哨兵/INFO 级事件只落 quality_alert.jsonl，三天无人知晓、不进 audit、
#       不上前端、无升级机制。此处提供统一转发入口（哈希链由 record() 维护）。
# ============================================================
_QA_FILE = os.path.join(C.DATA_DIR, "quality_alert.jsonl")
_QA_ROTATE_BYTES = 512 * 1024          # quality_alert 超 512KB 归档（对应 audit 50MB 按 1% 量级）
_QA_KEEP_DAYS = 90                     # 归档保留 90 天（与 audit rotate_daily keep_days 一致）
_ML_STALE_TDAYS = 25                   # 与 tools/data_sentinel.py L52 ML_PRED_STALE_TDAYS 一致


def _qa_read_all():
    """读 quality_alert.jsonl 全部条目（含归档 quality_alert_*.jsonl，按名序）。
    当前文件路径以 _QA_FILE 为准（支持验收 monkeypatch 注入临时文件）。"""
    rows = []
    try:
        cur = os.path.basename(_QA_FILE)
        files = [cur] + [f for f in os.listdir(C.DATA_DIR)
                         if f.startswith("quality_alert_")
                         and f.endswith(".jsonl") and f != cur]
        for fname in sorted(set(files)):
            p = _QA_FILE if fname == cur else os.path.join(C.DATA_DIR, fname)
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return rows


def quality_alert_streak(event, until_date=None):
    """同一 event 从 until_date（含）往回在【交易日序列】上连续出现的天数。
    例如 09-09/10/11 三个连续交易日出现 → 3；中间缺交易日则中断重新计数。
    事件日取条目的 date 字段前 10 位（触发日），同一自然日去重。"""
    from . import trading_calendar as _tc
    until_date = until_date or time.strftime("%Y-%m-%d")
    days = set()
    for r in _qa_read_all():
        if r.get("event") == event:
            d = str(r.get("date") or r.get("t") or "")[:10]
            if d and d <= until_date:
                days.add(d)
    if not days:
        return 0
    # 从 until_date 往回（按交易日）找到最近一个"有该事件"的日期再开始数连续
    cur = until_date
    while cur >= min(days) and cur not in days:
        cur = _tc.prev_trading_day(cur)
    if cur < min(days):
        return 0
    streak = 0
    while cur in days:
        streak += 1
        cur = _tc.prev_trading_day(cur)
    return streak


def quality_alert_forward(entry, source="quality_alert.jsonl"):
    """★ D1：把一条 quality_alert 条目转发为 audit 事件（哈希链完整）。
    - 同 event 在 quality_alert.jsonl（含归档）连续出现 ≥2 个交易日 → 级别升一档
      （WARN→CRITICAL），并另记 quality_alert_escalated（带连续天数）；
    - 升级至 CRITICAL 时尝试 alert.notify 推送（延迟 import 防循环依赖）；
    - 返回 (实际级别, 连续天数)；失败返回 None。
    ⚠ 注意：entry 内的 event/level 键会被过滤改名，避免覆盖 record() 的命名参数。"""
    try:
        orig_event = entry.get("event") or entry.get("orig_event") or "quality_alert"
        level = entry.get("level") or "WARN"
        streak = quality_alert_streak(orig_event, until_date=(entry.get("date") or "")[:10] or None)
        escalated = False
        if streak >= 2 and level == "WARN":
            level = "CRITICAL"
            escalated = True
        fields = {k: v for k, v in entry.items()
                  if k not in ("event", "level", "kind", "date")}
        fields["orig_event"] = orig_event
        fields["event_date"] = str(entry.get("date") or entry.get("t") or "")[:10]
        fields["source"] = source
        fields["streak_days"] = streak
        if escalated:
            fields["escalated"] = True
        rec = record("alert", "quality_alert", level=level, **fields)
        if escalated:
            record("alert", "quality_alert_escalated", level="CRITICAL",
                   orig_event=orig_event, streak_days=streak, source=source,
                   event_date=fields["event_date"])
            try:
                from . import alert as _alert
                _alert.notify("WARN", "qa-escalated-%s" % orig_event,
                              "🔴 quality_alert 自动升级",
                              "事件 %s 连续 %d 天未处置，已升为 CRITICAL（%s）"
                              % (orig_event, streak, fields["event_date"]),
                              cooldown=1800)
            except Exception:
                pass
        return level, streak, rec
    except Exception:
        return None


def quality_alert_rotate():
    """★ D1：quality_alert.jsonl 无界增长治理——超 512KB 归档（os.replace 原子），
    并删除 90 天前的 quality_alert_*.jsonl 归档（与 audit 轮转保留期一致）。"""
    try:
        if os.path.exists(_QA_FILE) and os.path.getsize(_QA_FILE) > _QA_ROTATE_BYTES:
            ts = time.strftime("%Y%m%d_%H%M%S")
            dst = os.path.join(C.DATA_DIR, "quality_alert_%s.jsonl" % ts)
            if not os.path.exists(dst):
                os.replace(_QA_FILE, dst)
        # 清理超期归档
        cutoff = time.time() - _QA_KEEP_DAYS * 86400
        for fname in os.listdir(C.DATA_DIR):
            if fname.startswith("quality_alert_") and fname.endswith(".jsonl"):
                p = os.path.join(C.DATA_DIR, fname)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass


def quality_alert_summary(limit=30):
    """★ D1：前端红条数据源——读 quality_alert.jsonl（含归档）聚合关键项。
    返回 {banner_text, items[], escalated[], generated_at}。
    关键项：amount 缺失 / 复权跳变 high_severity / ML 陈旧 / 快照缺失。"""
    rows = _qa_read_all()
    items = []
    banner = []
    today = time.strftime("%Y-%m-%d")

    # 1) amount 缺失（amount_zero_guard 最近一条 + 连续天数）
    az = [r for r in rows if r.get("event") == "amount_zero_guard"]
    if az:
        last = az[-1]
        stk = quality_alert_streak("amount_zero_guard")
        items.append({"key": "amount_zero_guard",
                      "level": last.get("level", "WARN"),
                      "date": str(last.get("date", ""))[:10],
                      "detail": "amount=0 行 %s，占比 %.1f%%" % (
                          last.get("amount_zero_rows"),
                          (last.get("ratio") or 0) * 100),
                      "streak_days": stk})
        if stk >= 2:
            banner.append("🔴 成交额缺失连续 %d 天（amount=0 占比 %.0f%%）" % (
                stk, (last.get("ratio") or 0) * 100))
        else:
            banner.append("⚠ 成交额缺失（amount=0 占比 %.0f%%）" %
                          ((last.get("ratio") or 0) * 100))

    # 2) 复权跳变 high_severity（最新摘要条目）
    hs = [r for r in rows if r.get("high_severity") is not None]
    if hs:
        last = hs[-1]
        sev = last.get("high_severity") or 0
        if sev > 0:
            items.append({"key": "qfq_high_severity", "level": "WARN",
                          "date": str(last.get("date", ""))[:10],
                          "detail": "复权跳变高风险 %d 只" % sev,
                          "streak_days": None})
            banner.append("⚠ 复权跳变高风险 %d 只" % sev)

    # 3) ML 预测陈旧（哨兵摘要 ml_pred_last）
    ml = [r for r in rows if r.get("ml_pred_last")]
    if ml:
        last_ml = str(ml[-1].get("ml_pred_last", ""))[:10]
        if last_ml:
            try:
                from datetime import datetime as _dt
                gap = (_dt.strptime(today, "%Y-%m-%d")
                       - _dt.strptime(last_ml, "%Y-%m-%d")).days
            except Exception:
                gap = None
            if gap is not None and gap > _ML_STALE_TDAYS:
                items.append({"key": "ml_pred_stale", "level": "WARN",
                              "date": str(ml[-1].get("date", ""))[:10],
                              "detail": "ML 预测停在 %s（%d 天）" % (last_ml, gap),
                              "streak_days": None})
                banner.append("⚠ ML 预测陈旧（%s，%d 天未更新）" % (last_ml, gap))

    # 4) 快照缺失（data/snapshots/ 最新**真实快照** vs 最近交易日；
    #    pit_lost 占位（B1 重建，manifest.pit_lost=true）不算真实快照）
    try:
        from . import trading_calendar as _tc
        _snaps_dir = os.path.join(C.DATA_DIR, "snapshots")
        def _snap_real(d):
            try:
                m = json.load(open(os.path.join(_snaps_dir, d, "manifest.json"),
                                   encoding="utf-8"))
                if m.get("pit_lost"):
                    return False
            except Exception:
                pass
            return True
        snaps = sorted(d for d in os.listdir(_snaps_dir)
                       if re_fullmatch_date(d) and _snap_real(d))
        if snaps:
            latest_snap = snaps[-1]
            last_td = _tc.prev_trading_day(today)
            if latest_snap < last_td:
                items.append({"key": "snapshot_missing", "level": "WARN",
                              "date": today, "detail": "最新快照 %s（应到 %s）"
                              % (latest_snap, last_td),
                              "streak_days": None})
                banner.append("⚠ 快照缺失（最新 %s，晚于最近交易日）" % latest_snap)
    except Exception:
        pass

    # 5) 最近升级事件（audit 里 quality_alert_escalated）
    esc = []
    try:
        files = sorted(f for f in os.listdir(_AUDIT_DIR)
                       if f == "audit.jsonl" or (f.startswith("audit_") and f.endswith(".jsonl")))
        for fname in files[-3:]:
            with open(os.path.join(_AUDIT_DIR, fname), encoding="utf-8") as f:
                for line in f:
                    if "quality_alert_escalated" not in line:
                        continue
                    try:
                        d = json.loads(line)
                        esc.append({"date": str(d.get("t", ""))[:10],
                                    "orig_event": d.get("orig_event"),
                                    "streak_days": d.get("streak_days")})
                    except Exception:
                        pass
        esc = esc[-5:]
        for e in esc:
            banner.append("🔴 %s 连续 %s 天升级 CRITICAL" % (
                e["orig_event"], e["streak_days"]))
    except Exception:
        pass

    return {"banner_text": " | ".join(banner) if banner else "",
            "items": items[-limit:], "escalated": esc,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def re_fullmatch_date(s):
    """目录名是否为 YYYY-MM-DD（快照目录判定辅助）。"""
    try:
        import datetime as _dt
        _dt.datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False


def verify_chain(day=None):
    """校验哈希链完整性（防篡改）。返回 (ok, checked, broken_at)。
    ★ P0-4 修复：写入端按"天"重置链首，校验端也必须按天重置 prev，
    否则每天第一条记录必然校验失败 → 防篡改功能自报假警。
    ★ R2-P1.6：支持轮转多文件——audit_*.jsonl（按名序）+ 当前 audit.jsonl
    连续遍历；文件切换时重置链首（与 record 轮转语义一致：轮转点为文件级链首）。
    """
    try:
        files = sorted(f for f in os.listdir(_AUDIT_DIR)
                       if f == "audit.jsonl" or (f.startswith("audit_") and f.endswith(".jsonl")))
    except Exception:
        files = []
    if not files:
        return True, 0, None
    ok = True
    broken_at = None
    checked = 0
    try:
        for fname in files:
            prev = ""
            cur_day = ""
            with open(os.path.join(_AUDIT_DIR, fname), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        ok = False
                        broken_at = "bad-line@" + fname
                        return ok, checked, broken_at
                    t = rec.get("t", "")
                    d = t[:10]
                    if d != cur_day:
                        cur_day = d
                        prev = ""
                    if day and not t.startswith(day):
                        continue
                    payload = {k: v for k, v in rec.items() if k not in ("hash", "prev")}
                    expect = hashlib.sha256(
                        (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "|" + prev)
                        .encode("utf-8")).hexdigest()[:16]
                    if rec.get("hash") != expect or rec.get("prev") != prev:
                        ok = False
                        broken_at = "%s@%s" % (t, fname)
                        return ok, checked, broken_at
                    prev = rec["hash"]
                    checked += 1
    except Exception:
        return False, checked, "read-error"
    return ok, checked, broken_at


def daily_summary(day=None):
    """某日审计汇总（供日结/审计页）：买卖/风控拦截/异动计数"""
    day = day or _today()
    rows = query(day, limit=5000)
    summary = {
        "day": day,
        "total": len(rows),
        "buys": sum(1 for r in rows if r.get("level") == "BUY"),
        "sells": sum(1 for r in rows if r.get("level") == "SELL"),
        "risk_blocks": sum(1 for r in rows if r.get("kind") == "risk"),
        "alerts": sum(1 for r in rows if r.get("kind") == "alert"),
        "signals": sum(1 for r in rows if r.get("kind") == "signal"),
        "warns": sum(1 for r in rows if r.get("level") == "WARN"),
    }
    return summary
