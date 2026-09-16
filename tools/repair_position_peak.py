# -*- coding: utf-8 -*-
"""C1 一次性诊断：存量持仓污染 peak 扫描 + 历史带"峰"卖单污染量化。

只读：data/account.json、data/min5.db（URI mode=ro）。输出 data/repair_position_peak.json。
用法：python tools/repair_position_peak.py

口径（与 app/trader.py C1 修复一致）：
- 建仓后观测最高 = min5 实时尺度（动态清单 1002 只，历史持仓票实测全覆盖），
  截止到卖出时刻（历史卖单）或最新（当前持仓）。
- 污染判定：reason 中解析的峰 > 建仓后观测最高 × 1.005（0.5% 容差）。
- extra_loss_est 口径：干净触发价 − 实际成交价 × qty。
  · 正 = 脏峰使止损基准虚高，若市场在干净触发价有流动性，本可少亏的估算金额；
  · 负 = 干净触发价反而低于实际成交（冲高回落类方向可能反转，仅列示不算损失）；
  · 移动止损类（触发价=峰×0.97）口径较可靠；冲高回落类（峰×0.98）仅参考。
  ★ 真实成交受市场深度/跌停影响，全部为估算供验收方复核，不替代逐笔对账。
"""
import json
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import config as C  # noqa: E402  只读引用路径常量/阈值

_F2_GATE_DATE = "2026-09-09"
_TOL = 0.005


def _min5_conn():
    return sqlite3.connect("file:%s?mode=ro" % C.MIN5_DB_FILE, uri=True, timeout=10)


def obs_high_range(code, start_dt, end_dt=""):
    """min5 中 [start_dt, end_dt] 区间的最高 high（实时尺度）。end_dt 空=不限截止。
    start_dt/end_dt 格式 "YYYY-MM-DD HH:MM:SS"。返回 (high, src) 或 (None, "no_min5")。"""
    try:
        conn = _min5_conn()
        try:
            if end_dt:
                row = conn.execute(
                    "SELECT MAX(high) FROM kline_min5 WHERE code=? AND date>=? AND date<=?",
                    (code, start_dt, end_dt)).fetchone()
            else:
                row = conn.execute(
                    "SELECT MAX(high) FROM kline_min5 WHERE code=? AND date>=?",
                    (code, start_dt)).fetchone()
            if row and row[0]:
                return float(row[0]), "min5"
        finally:
            conn.close()
    except Exception:
        pass
    return None, "no_min5"


def find_buy(acct, code, sell_time):
    """卖出前最近一笔买入。返回 (entry_date, entry_price, buy_ts) 或 (None, 0, "")。"""
    for b in reversed(acct.get("trades", []) or []):
        if b.get("code") == code and b.get("side") == "buy" and b["time"] <= sell_time:
            t_ = str(b["time"])
            return t_[:10], float(b["price"]), t_[11:19]
    return None, 0.0, ""


def main():
    with open(C.ACCOUNT_FILE, encoding="utf-8") as f:
        acct = json.load(f)

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "f2_gate_date": _F2_GATE_DATE,
        "positions_now": {},
        "positions_scan": [],
        "peak_sells": [],
        "summary": {},
    }

    # ---- 1) 当前持仓一致性扫描（现在空仓 → 防复发验证）----
    for code, pos in acct.get("positions", {}).items():
        entry_date = str(pos.get("entry_date", "") or "")
        entry = float(pos.get("entry_price") or 0)
        peak = float(pos.get("peak") or 0)
        verdict, detail = "ok", ""
        if entry_date and entry_date < _F2_GATE_DATE and not pos.get("entry_ts") and peak > entry:
            buy_ts = ""
            for b in reversed(acct.get("trades", []) or []):
                if b.get("code") == code and b.get("side") == "buy" \
                        and str(b.get("time", "")).startswith(entry_date):
                    buy_ts = str(b["time"])[11:19]
                    break
            h, src = obs_high_range(code, entry_date + " " + (buy_ts or "00:00:00"))
            if h and peak > h * (1 + _TOL):
                verdict, detail = "POLLUTED", "peak=%.3f 建仓后观测最高=%.3f(%s)" % (peak, h, src)
            elif h:
                verdict, detail = "clean", "peak=%.3f ≤ 观测最高=%.3f(%s)" % (peak, h, src)
            else:
                verdict, detail = "no_data", "无分钟行情(%s)" % src
        report["positions_scan"].append({
            "code": code, "name": pos.get("name", ""), "entry_date": entry_date,
            "entry_price": entry, "peak": peak, "verdict": verdict, "detail": detail,
        })

    # ---- 2) 历史带"峰"卖单污染量化 ----
    stop_f = 1 + C.TRAILING_STOP_PCT      # 0.97
    pull_f = 1 - C.INTRADAY_PULLBACK      # 0.98
    peak_sells = []
    for t_ in acct.get("trades", []):
        if t_.get("side") != "sell":
            continue
        reason = str(t_.get("reason", "") or "")
        m = re.search(r"峰([\d.]+)", reason)
        if not m:
            continue
        peak_used = float(m.group(1))
        code = t_["code"]
        sell_time = str(t_["time"])
        entry_date, entry_price, buy_ts = find_buy(acct, code, sell_time)
        if not entry_date:
            peak_sells.append({
                "time": sell_time, "code": code, "name": t_.get("name", ""),
                "reason": reason, "peak_used": peak_used, "pnl": t_.get("pnl"),
                "entry_date": None, "polluted": False, "detail": "未找到建仓记录",
                "extra_loss_est": 0.0,
            })
            continue
        h, src = obs_high_range(code, entry_date + " " + buy_ts, sell_time)
        polluted = bool(h and peak_used > h * (1 + _TOL))
        extra = 0.0
        if polluted and h:
            clean_base = max(entry_price, h)
            if "移动止损" in reason:
                clean_px = clean_base * stop_f
            else:
                clean_px = clean_base * pull_f
            extra = round((clean_px - float(t_["price"])) * int(t_["qty"]), 2)
        peak_sells.append({
            "time": sell_time, "code": code, "name": t_.get("name", ""),
            "reason": reason, "peak_used": peak_used, "pnl": t_.get("pnl"),
            "entry_date": entry_date, "entry_price": entry_price,
            "obs_high": round(h, 3) if h else None, "obs_source": src,
            "polluted": polluted,
            "extra_loss_est": extra,
            "detail": "峰=%.2f 建仓后观测最高=%.2f(%s)" % (peak_used, h, src) if h
                      else "无分钟行情(%s)" % src,
        })

    n_poll = sum(1 for p in peak_sells if p.get("polluted"))
    extra_total = round(sum(p.get("extra_loss_est") or 0 for p in peak_sells if p.get("polluted")), 2)
    report["peak_sells"] = peak_sells
    report["summary"] = {
        "positions_now_count": len(acct.get("positions", {})),
        "peak_sells_total": len(peak_sells),
        "polluted": n_poll,
        "extra_loss_est_total": extra_total,
        "note": "extra_loss_est 口径=干净触发价−实际成交价×qty；正=脏峰下本可少亏的估算，"
                "负=该口径下反而更差（冲高回落类方向可能反转）；移动止损类口径较可靠；"
                "真实成交受市场深度/跌停影响，仅估算供复核",
    }

    out = os.path.join(C.DATA_DIR, "repair_position_peak.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("已写出 %s" % out)
    print("positions_now=%d | 带峰卖单=%d | 污染=%d | extra_loss_est_total=%.2f"
          % (len(acct.get("positions", {})), len(peak_sells), n_poll, extra_total))


if __name__ == "__main__":
    main()
