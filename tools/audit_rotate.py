# -*- coding: utf-8 -*-
"""R3-E2：audit.jsonl 按日轮转（schtasks 每日 00:05 调用）。
- 调 app.audit.rotate_daily()：持 _cross_proc_lock 原子归档 + 超期清理（默认 90 天）。
- 读路径零改动：app/audit.py 已支持 audit_*.jsonl 多文件读取（R2-P1.6）。
- 幂等：今日已有同名归档则跳过；重复运行安全。
- 手动试跑：python tools\\audit_rotate.py
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import audit

if __name__ == "__main__":
    keep = 90
    if len(sys.argv) > 1:
        try:
            keep = int(sys.argv[1])
        except ValueError:
            pass
    res = audit.rotate_daily(keep_days=keep)
    print(json.dumps(res, ensure_ascii=False))
    # G1（2026-09-13）：轮转后立即向新 audit.jsonl 写一条链首 marker 心跳，
    # 杜绝 watchdog 读到空文件触发 no_t_in_tail（09-13 00:20 误杀根因）。
    # record 持跨进程锁 + 写前重读磁盘尾部 prev，链格式完整（新链首 prev=""）。
    if res.get("rotated"):
        try:
            audit.record("daily", "audit_rotate_marker", "INFO")
            print("marker written: audit_rotate_marker")
        except Exception as e:
            print("marker write failed: %s" % e)
    # 试跑后自检：新 audit.jsonl 存在
    ok = os.path.exists(audit._AUDIT_FILE)
    print("new audit.jsonl exists: %s" % ok)
    sys.exit(0 if ok else 2)
