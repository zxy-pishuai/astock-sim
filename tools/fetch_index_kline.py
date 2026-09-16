# -*- coding: utf-8 -*-
"""★ 阶段2 指数日K入库：把三大指数日K写入 market.db kline 表（code='sh000001' 等）
供指数择时闸门（app/index_timing.py）离线 PIT 回测使用（避免回测时打网络）。
幂等 upsert；用法：python -m tools.fetch_index_kline
"""
import json
import sqlite3
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "C:/Users/26838/A股模拟盘"
DB = BASE + "/data/market.db"
SYMBOLS = ["sh000001", "sz399001", "sz399006"]   # 上证/深成/创业板指
DAYS = 600

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

try:
    from app.index_timing import _sane_rows   # ★ F1：通用 K 线行断言（个股/指数共用）
except Exception:
    def _sane_rows(rows):
        return rows


def _http(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_index(code, days=DAYS):
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={code},day,,,{days},qfq")
    data = json.loads(_http(url))
    sd = (data.get("data") or {}).get(code) or {}
    raw = sd.get("qfqday") or sd.get("day") or []
    out = []
    for e in raw:
        try:
            if len(e) < 6:
                continue
            # ★ F1（2026-09-08）：腾讯 fqkline 数组序为
            #   [date, open, close, high, low, volume]（6 元素，无 amount）。
            #   旧代码按 (open,high,low,close) 位置解析 → high=收盘、low=最高、
            #   close=最低、amount 恒 0。现按真实顺序映射；接口无成交额 → 显式写 0。
            out.append((code, "day", e[0], float(e[1]), float(e[3]),
                        float(e[4]), float(e[2]), float(e[5]) * 100, 0.0))
        except (ValueError, IndexError, TypeError):
            continue
    return _sane_rows(out)


def main():
    conn = sqlite3.connect(DB, timeout=30)
    total = 0
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    for sym in SYMBOLS:
        rows = fetch_index(sym)
        if not rows:
            print(f"  {sym}: 拉取失败/为空")
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO kline(code,period,date,open,high,low,close,volume,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        rng = conn.execute(
            "SELECT MIN(date), MAX(date), COUNT(*) FROM kline WHERE code=? AND period='day'",
            (sym,)).fetchone()
        # ★ F1：陈旧度提示（目标：三指数都不落后 >5 自然日）
        last = rng[1] or ""
        stale_days = -1
        if last:
            try:
                stale_days = (_dt.strptime(today, "%Y-%m-%d")
                              - _dt.strptime(last, "%Y-%m-%d")).days
            except Exception:
                pass
        stale_txt = f"（落后 {stale_days} 自然日）" if stale_days > 5 else ""
        print(f"  {sym}: 写入 {len(rows)} 根（{rng[0]} ~ {rng[1]}，库内 {rng[2]}）{stale_txt}")
        total += len(rows)
    conn.close()
    print(f"完成，共写入 {total} 根指数日K")


if __name__ == "__main__":
    main()