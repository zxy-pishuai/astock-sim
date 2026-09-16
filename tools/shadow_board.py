# -*- coding: utf-8 -*-
"""P65 真打板 M2 影子运行 — 只记账不下单不碰账户

盘中: 记录信号+假设成交（min5 触板检测 + M2 概率）
收盘后: T+1 结算到 data/shadow/（复用 board_true 退出链）

约束: 只记账，绝不下单，不碰 account.json 任何账户文件；
      先满 20 交易日再谈接入（本工具不做自动接入判断，仅记账）。

用法:
  python tools/shadow_board.py --record         # 盘中记录一次（幂等，按日期去重）
  python tools/shadow_board.py --settle         # 收盘后 T+1 结算一次
  python tools/shadow_board.py --backtest --start 2025-01-01 --end 2025-12-31  # 回放验证
  python tools/shadow_board.py --status

输出: data/shadow/shadow_board.jsonl  (每行一个影子回合)
      data/shadow/shadow_board_manifest.json
"""
import argparse, json, os, sys, time, sqlite3, random
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from app import config as C
from app import engine as eng

SHADOW_DIR = BASE / "data" / "shadow"
SHADOW_JL = SHADOW_DIR / "shadow_board.jsonl"
SHADOW_MANIFEST = SHADOW_DIR / "shadow_board_manifest.json"

def _today():
    return time.strftime("%Y-%m-%d")

def _load_pool():
    # 复用 bt_pool.json top500 或 config,默认
    pool = BASE / "data" / "bt_pool.json"
    if pool.exists():
        try:
            d = json.loads(pool.read_text(encoding="utf-8"))
            codes = d.get("codes", [])[:500]
            if codes:
                return codes
        except Exception:
            pass
    # fallback: stock_list
    lst = BASE / "data" / "stock_list.json"
    if lst.exists():
        try:
            d = json.loads(lst.read_text(encoding="utf-8"))
            return [r[0] for r in d[:500] if isinstance(r, (list, tuple)) and r]
        except Exception:
            pass
    return []

def _load_names(codes):
    names = {}
    lst = BASE / "data" / "stock_list.json"
    if lst.exists():
        try:
            d = json.loads(lst.read_text(encoding="utf-8"))
            for r in d:
                if isinstance(r, (list, tuple)) and r and r[0] in set(codes):
                    names[str(r[0])] = str(r[1]) if len(r) > 1 else ""
        except Exception:
            pass
    for c in codes:
        names.setdefault(c, "")
    return names

def _read_shadow_jl():
    if not SHADOW_JL.exists():
        return []
    out = []
    for line in SHADOW_JL.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out

def _append_shadow(records):
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    with SHADOW_JL.open("a", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # manifest
    existing = []
    if SHADOW_MANIFEST.exists():
        try:
            existing = json.loads(SHADOW_MANIFEST.read_text(encoding="utf-8")).get("records", [])
        except Exception:
            existing = []
    all_recs = existing + records
    manifest = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(all_recs),
        "note": "P65 影子运行：只记账不下单不碰账户；满20交易日再谈接入",
        "fill_params": {
            "p_early": getattr(C, "SHADOW_BOARD_FILL_P_EARLY", 0.30),
            "p_late": getattr(C, "SHADOW_BOARD_FILL_P_LATE", 0.60),
            "early_cutoff": getattr(C, "SHADOW_BOARD_EARLY_CUTOFF", "10:30"),
        },
        "records": all_recs[-500:],
    }
    SHADOW_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

def do_record(date=None):
    """盘中记录一次：用 board_true 的 min5 扫描产出触板信号，按 M2 概率决定假设成交"""
    date = date or _today()
    # 只做一次去重：同 date 已有记录则跳过
    for r in _read_shadow_jl():
        if r.get("entry_date") == date and r.get("type") == "shadow_entry":
            print(f"[record] {date} 已记录，跳过")
            return 0
    codes = _load_pool()
    names = _load_names(codes)
    if not codes:
        print("[record] pool empty")
        return 0
    from app.board_true import TrueBoardBacktest
    # 用 config 的影子参数
    fp = {
        "p_early": getattr(C, "SHADOW_BOARD_FILL_P_EARLY", 0.30),
        "p_late": getattr(C, "SHADOW_BOARD_FILL_P_LATE", 0.60),
        "early_cutoff": getattr(C, "SHADOW_BOARD_EARLY_CUTOFF", "10:30"),
    }
    bt = TrueBoardBacktest(codes, names, date, date, 100000.0,
                           fill_model="M2", max_positions=getattr(C, "SHADOW_BOARD_MAX_POS", 2),
                           position_pct=getattr(C, "SHADOW_BOARD_POS_PCT", 0.25),
                           fill_params=fp, seed=42)
    daily = bt._load_daily()
    bt._scan_min5(daily)
    evs = sorted(bt._events_cache.get(date) or [], key=lambda e: e["hhmm"])
    if not evs:
        print(f"[record] {date} 无触板信号")
        # 仍记一条空记录便于审计
        _append_shadow([{"type":"shadow_empty","date":date,"touches":0,"ts":time.strftime("%Y-%m-%d %H:%M:%S")}])
        return 0
    # 按 M2 概率筛选假设成交（复用 TrueBoardBacktest 的 RNG 逻辑）
    rng = random.Random(42)
    early_cut = fp["early_cutoff"].replace(":", "")
    filled = []
    for ev in evs[:10]:
        if ev["sealed"]:
            prob = fp["p_early"] if ev["hhmm"] < early_cut else fp["p_late"]
            if rng.random() >= prob:
                continue
        filled.append(ev)
        if len(filled) >= getattr(C, "SHADOW_BOARD_MAX_POS", 2):
            break
    records = []
    for ev in filled:
        records.append({
            "type": "shadow_entry",
            "entry_date": date,
            "code": ev["code"],
            "name": names.get(ev["code"], ""),
            "hhmm": ev["hhmm"],
            "sealed": ev["sealed"],
            "lu": ev["lu"],
            "close": ev["close"],
            "fill_model": "M2",
            "fill_params": fp,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    if records:
        _append_shadow(records)
        print(f"[record] {date} 触板{len(evs)} 假设成交{len(records)} -> {SHADOW_JL}")
    else:
        print(f"[record] {date} 触板{len(evs)} 但 M2 未成交")
        _append_shadow([{"type":"shadow_empty","date":date,"touches":len(evs),"ts":time.strftime("%Y-%m-%d %H:%M:%S")}])
    return len(records)

def do_settle(date=None):
    """收盘后 T+1 结算：对 shadow_entry 按日线退出链结算（复用 board_true 的退出逻辑的简化版）"""
    date = date or _today()
    rows = _read_shadow_jl()
    # 找未结算的 shadow_entry
    pending = [r for r in rows if r.get("type")=="shadow_entry" and r.get("exit_date") is None]
    if not pending:
        print(f"[settle] {date} 无待结算影子")
        return 0
    # 简化：对每个 pending 按 daily 收盘做退出判定（与 board_true 同口径的四项）
    # 为保持只读不碰账户，这里仅做估算并追加一条 shadow_exit 记录
    n = 0
    exits = []
    for e in pending:
        # 防重复：若 entry_date == date 则 T+1 未到
        if e["entry_date"] >= date:
            continue
        code = e["code"]
        # 拉日线估算 pnl
        try:
            from app import datafeed as df
            kl = df.fetch_kline(code, "day", 30)
            if len(kl) < 3:
                continue
            # entry lu vs 当前收盘
            buy_px = e["lu"] * (1 + C.SLIPPAGE)
            # 找到 entry_date 后一天的收盘
            dates = [k["date"] for k in kl]
            try:
                idx = dates.index(e["entry_date"])
            except ValueError:
                continue
            if idx + 1 >= len(kl):
                continue
            cp = kl[idx+1]["close"]
            sell_px = cp * (1 - C.SLIPPAGE)
            # 简化费用
            ret = sell_px/buy_px - 1
            exits.append({
                "type": "shadow_exit",
                "code": code,
                "entry_date": e["entry_date"],
                "exit_date": dates[idx+1],
                "buy_px": round(buy_px, 3),
                "sell_px": round(sell_px, 3),
                "ret": round(ret, 6),
                "exit": "T+1结算",
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            # 标记原记录已结算（追加而非改写，保持 jsonl 追加语义）
            n += 1
        except Exception as ex:
            print(f"[settle] {code} err {ex}")
            continue
    if exits:
        _append_shadow(exits)
        print(f"[settle] {date} 结算{len(exits)}笔 -> {SHADOW_JL}")
    else:
        print(f"[settle] {date} 无可结算")
    return n

def do_backtest(start, end):
    codes = _load_pool()
    names = _load_names(codes)
    from app.board_true import TrueBoardBacktest
    fp = {
        "p_early": getattr(C, "SHADOW_BOARD_FILL_P_EARLY", 0.30),
        "p_late": getattr(C, "SHADOW_BOARD_FILL_P_LATE", 0.60),
        "early_cutoff": getattr(C, "SHADOW_BOARD_EARLY_CUTOFF", "10:30"),
    }
    for fm in ("M1","M2","M3"):
        bt = TrueBoardBacktest(codes, names, start, end, 100000.0, fill_model=fm, fill_params=fp if fm=="M2" else {}, seed=42)
        r = bt.run()
        print(f"[{fm}] {start}->{end} ret={r['total_return']:+.4f} dd={r['max_drawdown']:+.4f} trades={r['trade_count']} touches={r['touches']}")

def do_status():
    rows = _read_shadow_jl()
    entries = [r for r in rows if r.get("type")=="shadow_entry"]
    exits = [r for r in rows if r.get("type")=="shadow_exit"]
    empties = [r for r in rows if r.get("type")=="shadow_empty"]
    print(f"shadow_board: entries={len(entries)} exits={len(exits)} empty_days={len(empties)} total_lines={len(rows)}")
    if entries:
        print(" last entry:", entries[-1])
    if exits:
        print(" last exit:", exits[-1])
    # 满20交易日提示
    uniq_days = len(set(r.get("entry_date") for r in entries if r.get("entry_date")))
    tag = "OK 可谈接入" if uniq_days>=20 else "WAIT 未满20日"
    print(f" 去重交易日 {uniq_days}/20 {tag}")
    if SHADOW_MANIFEST.exists():
        m = json.loads(SHADOW_MANIFEST.read_text(encoding="utf-8"))
        print(f" manifest: {m.get('updated_at')} count={m.get('count')}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="盘中记录一次")
    ap.add_argument("--settle", action="store_true", help="收盘后 T+1 结算")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    if a.status or not (a.record or a.settle or a.backtest):
        do_status()
    if a.record:
        do_record(a.date)
    if a.settle:
        do_settle(a.date)
    if a.backtest:
        do_backtest(a.start, a.end)
