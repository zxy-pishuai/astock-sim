# -*- coding: utf-8 -*-
"""P65 影子日更调度 — 盘中 record + 收盘 settle，daemon 线程，只记账不碰账户，异常隔离。"""
import threading, time, traceback

_started = False
_lock = threading.Lock()
_last_record_day = ""
_last_settle_day = ""

def _is_trading_day_today():
    try:
        from . import trading_calendar as tcal
        import time as _t
        return tcal.is_trading_day(_t.strftime("%Y-%m-%d"))
    except Exception:
        import datetime
        return datetime.datetime.now().weekday() < 5

def start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    th = threading.Thread(target=_loop, daemon=True, name="shadow-scheduler")
    th.start()

def _loop():
    global _last_record_day, _last_settle_day
    while True:
        try:
            import time as _t, datetime
            now = datetime.datetime.now()
            today = now.strftime("%Y-%m-%d")
            hm = now.hour * 100 + now.minute
            is_td = _is_trading_day_today()
            if is_td:
                # 盘中 10:00-14:50 每小时 record 一次（去重到日），收盘后 15:05-15:30 settle
                if 1000 <= hm <= 1450 and today != _last_record_day:
                    try:
                        from tools import shadow_board as sb
                        sb.do_record(today)
                        _last_record_day = today
                    except Exception as e:
                        try:
                            from . import audit
                            audit.record(kind="daily", event="shadow_record_failed", level="WARN", error=str(e)[:200])
                        except Exception:
                            pass
                if 1505 <= hm <= 1530 and today != _last_settle_day:
                    try:
                        from tools import shadow_board as sb
                        sb.do_settle(today)
                        _last_settle_day = today
                    except Exception as e:
                        try:
                            from . import audit
                            audit.record(kind="daily", event="shadow_settle_failed", level="WARN", error=str(e)[:200])
                        except Exception:
                            pass
        except Exception:
            pass
        time.sleep(60)

def status():
    return {"started": _started, "last_record": _last_record_day, "last_settle": _last_settle_day}
