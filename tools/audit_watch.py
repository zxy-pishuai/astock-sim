# -*- coding: utf-8 -*-
"""★ D2（2026-09-13）审计巡检：静默失败防护（INFO 级"延后/跳过"事件的自动升级）

痛点：快照三天没生成全靠三条 INFO 级 data_snapshot_deferred 记录，无任何升级机制
→ 静默失败。本工具每日 21:30 跑一次（计划任务 TianjiAuditWatch）：

  - 白名单（必须每日出现，按【交易日】判定）：data_update /
    daily_coverage 或 daily_coverage_pending（任一）/ data_snapshot*（前缀）/
    heartbeat；从窗口尾部往前数"连续缺失的交易日数"，≥1 即报缺失。
  - 黑名单（不应连续出现）：data_snapshot_deferred / stale_first_budget_exceeded /
    daily_coverage_pending / amount_zero_guard / watchdog_stale；
    窗口内同事件连续 ≥2 个交易日 → 升级 WARN→CRITICAL 并写明连续天数。

数据源：主 audit.jsonl + audit_*.jsonl 归档 + quality_alert.jsonl（含归档）
（amount_zero_guard 历史只落在 quality_alert.jsonl，两边都要聚合）。
结果写 audit（audit_watch 事件）+ quality_alert.jsonl（各 finding 一条 + 转发）。

用法：
  python tools/audit_watch.py                    # 默认：检查最近交易日（回溯 7 个交易日）
  python tools/audit_watch.py --from 2026-09-09 --to 2026-09-11   # 回放指定窗口
  python tools/audit_watch.py --dry-run          # 只输出不写（验收/演练用）
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

from app import audit as _audit
from app import config as C
from app import trading_calendar as tc

# 白名单：前缀匹配（data_snapshot* 指成功快照事件）；daily_coverage 与
# daily_coverage_pending 任一满足（"当天有覆盖检查记录"）。
WHITELIST = [
    ("data_update", "data_update"),
    ("daily_coverage", "daily_coverage|daily_coverage_pending"),
    ("data_snapshot", "data_snapshot"),
    ("heartbeat", "heartbeat"),
]
WHITELIST_LABEL = {
    "data_update": "data_update（收盘增量更新）",
    "daily_coverage": "daily_coverage（覆盖检查）",
    "data_snapshot": "data_snapshot（快照冻结）",
    "heartbeat": "heartbeat（进程心跳）",
}
# 黑名单：不应连续出现
BLACKLIST = [
    "data_snapshot_deferred",
    "stale_first_budget_exceeded",
    "daily_coverage_pending",
    "amount_zero_guard",
    "watchdog_stale",
]
BLACKLIST_LABEL = {
    "data_snapshot_deferred": "快照延后(data_snapshot_deferred)",
    "stale_first_budget_exceeded": "首批预算超时(stale_first_budget_exceeded)",
    "daily_coverage_pending": "覆盖未完成(daily_coverage_pending)",
    "amount_zero_guard": "成交额缺失(amount_zero_guard)",
    "watchdog_stale": "看门狗陈旧(watchdog_stale)",
}


def load_events():
    """聚合 (date -> {event})：audit 主文件 + audit_*.jsonl 归档 + quality_alert.jsonl(含归档)。"""
    days = {}
    # audit 侧
    try:
        files = [f for f in os.listdir(os.path.join(C.DATA_DIR, "audit"))
                 if f == "audit.jsonl"
                 or (f.startswith("audit_") and f.endswith(".jsonl"))]
        for fname in files:
            with open(os.path.join(C.DATA_DIR, "audit", fname),
                      encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        ev = d.get("event")
                        dt = str(d.get("t", ""))[:10]
                        if ev and dt:
                            days.setdefault(dt, set()).add(ev)
                    except Exception:
                        pass
    except Exception:
        pass
    # quality_alert 侧（amount_zero_guard 等历史只落在此）
    try:
        files = [f for f in os.listdir(C.DATA_DIR)
                 if f == "quality_alert.jsonl"
                 or (f.startswith("quality_alert_") and f.endswith(".jsonl"))]
        for fname in files:
            with open(os.path.join(C.DATA_DIR, fname), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        ev = d.get("event")
                        dt = str(d.get("date") or d.get("t") or "")[:10]
                        if ev and dt:
                            days.setdefault(dt, set()).add(ev)
                    except Exception:
                        pass
    except Exception:
        pass
    return days


def whitelist_missing_streak(events, window, key, matcher):
    """从窗口尾部往前数：该白名单事件【连续缺失】的交易日数（缺失=当天无匹配事件）。"""
    streak = 0
    for day in reversed(window):
        evs = events.get(day, set())
        hit = any(__m(ev, matcher) for ev in evs)
        if not hit:
            streak += 1
        else:
            break
    return streak


def __m(ev, matcher):
    if matcher.endswith("*"):
        return ev.startswith(matcher[:-1])
    return ev == matcher or ev in matcher.split("|")


def blacklist_longest_streak(events, window, event):
    """窗口内该事件在交易日序列上的最长连续出现天数。"""
    best = cur = 0
    for day in window:
        if event in events.get(day, set()):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def run_window(window, dry_run=True):
    events = load_events()
    findings = []

    # 1) 白名单连续缺失（从窗口尾部往前数）
    for key, matcher in WHITELIST:
        streak = whitelist_missing_streak(events, window, key, matcher)
        if streak > 0:
            findings.append({
                "key": "wl_missing_%s" % key,
                "level": "CRITICAL",
                "summary": "%s 连续 %d 个交易日缺失（CRITICAL）" % (
                    WHITELIST_LABEL[key], streak),
                "streak_days": streak,
            })

    # 2) 黑名单连续出现（≥2 交易日升级）
    for ev in BLACKLIST:
        streak = blacklist_longest_streak(events, window, ev)
        if streak >= 2:
            findings.append({
                "key": "bl_streak_%s" % ev,
                "level": "CRITICAL",
                "summary": "%s 连续 %d 个交易日（升级 WARN→CRITICAL）" % (
                    BLACKLIST_LABEL[ev], streak),
                "streak_days": streak,
            })

    # 排序：白名单缺失在前，其余按连续天数降序
    findings.sort(key=lambda f: (not f["key"].startswith("wl_"),
                                 - (f.get("streak_days") or 0)))
    return findings, events


def main():
    ap = argparse.ArgumentParser(description="审计巡检（D2 静默失败防护）")
    ap.add_argument("--from", dest="frm", default=None)
    ap.add_argument("--to", dest="to", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="只输出不写 audit/quality_alert（验收/演练）")
    a = ap.parse_args()

    today = time.strftime("%Y-%m-%d")
    if a.frm:
        d0, d1 = a.frm, a.to or a.frm
        window = tc.trading_days(d0, d1)
        if not window:
            print("窗口内无交易日: %s ~ %s" % (d0, d1))
            return 0
    else:
        last_td = tc.prev_trading_day(today)
        # 回溯 6 个更早交易日 → 共 7 个交易日窗口
        window = [last_td]
        cur = last_td
        for _ in range(6):
            cur = tc.prev_trading_day(cur)
            window.insert(0, cur)

    findings, events = run_window(window, dry_run=a.dry_run)

    print("========== 审计巡检（D2） ==========")
    print("窗口: %s ~ %s（%d 个交易日）%s" % (
        window[0], window[-1], len(window), " [DRY-RUN]" if a.dry_run else ""))
    if not findings:
        print("✓ 未发现异常（白名单齐、黑名单无连续）")
    for f in findings:
        print("  [%s] %s" % (f["level"], f["summary"]))

    if a.dry_run or not findings:
        return 0

    # 结果写 audit（audit_watch 汇总事件）+ quality_alert.jsonl（各 finding 一条 + 转发）
    try:
        _audit.record("alert", "audit_watch", level="CRITICAL",
                      window="%s~%s" % (window[0], window[-1]),
                      n_findings=len(findings),
                      findings=[f["summary"] for f in findings])
    except Exception as e:
        print("audit 写入失败:", e)
    try:
        qa = os.path.join(C.DATA_DIR, "quality_alert.jsonl")
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        for f in findings:
            entry = {
                "date": ts,
                "event": "audit_watch_%s" % f["key"],
                "level": f["level"],
                "detail": f["summary"],
                "window": "%s~%s" % (window[0], window[-1]),
                "streak_days": f.get("streak_days"),
            }
            with open(qa, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            try:
                _audit.quality_alert_forward(entry,
                                             source="tools/audit_watch.py")
            except Exception:
                pass
        _audit.quality_alert_rotate()
    except Exception as e:
        print("quality_alert 写入失败:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
