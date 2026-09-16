# -*- coding: utf-8 -*-
"""★ Phase34: 竞价弱转强策略研究（独立模拟器，只出研究结论，不做实盘/引擎集成）。

信号定义（严格 PIT，只用开盘价及以前信息）：
  弱势（昨日，三选一分开测）：
    W1 炸板   —— 昨日 high==涨停价(engine.limit_prices 口径) 且收盘低于涨停价 ≥2%
    W2 大阴线 —— 昨日跌幅 ≤ -3%
    W3 强势回落—— 昨日振幅 ≥6% 且收阴
  转强（今日开盘）：高开 +1%~+4%（>4% 追高剔除，<1% 转强失败）
  买入价 = 今日开盘 × (1+0.001 滑点)。禁用当日成交量等收盘后信息。

模拟器假设（与 app.engine 对齐，逐条）：
  初始资金 10 万；100 股整数手；单边滑点 0.001；费用 = engine.buy_fee/sell_fee（只读引用）；
  T+1（买入当日不可卖）；池 = bt_pool.json top500（engine 内同款 DATA_EXCLUDE 过滤在此显式做）；
  四窗口与 Phase21 一致；仓位假设 MAX_POS=3、单仓=cash×30%（对齐 engine score 缺省参数，
  见 docs/reports/auction_w2s.md §2 说明）；同日多信号按代码序入剩余槽位（无 PIT 排序依据，如实声明）。
退出三套分开测：
  E1 次日收盘卖；E2 固定 +4%/-2% 盘中触网（自 T+1 起每日监控当日 high/low，触网价含滑点，
     同日先止损后止盈的保守顺序，跳空穿越按开盘价成交；未触网持有至窗口末强平）；
  E3 持有 3 日收盘强平。

用法：python tools/auction_w2s_research.py            # 全量 36 组 + min5 增强 + 判定
"""
import sys
import os
import json
import time
import sqlite3
import itertools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import config as C                    # noqa: E402  (只读)
from app import engine as eng                  # noqa: E402  (只读 limit_prices/buy_fee/sell_fee)

OUT_JSON = os.path.join(C.DATA_DIR, "bt_auction_w2s.json")

# ---------------- 写死的判定常量（研究口径，勿随意改） ----------------
WINDOWS = [["2019-01-01", "2020-12-31"], ["2021-01-01", "2022-12-31"],
           ["2023-01-01", "2024-12-31"], ["2025-08-18", "2026-08-18"]]
WIN_NAMES = ["2019-20", "2021-22", "2023-24", "2025-08~2026-08(牛市)"]
WEAK_KEYS = ["W1", "W2", "W3"]
WEAK_NAMES = {"W1": "炸板", "W2": "大阴线", "W3": "强势回落"}
EXIT_KEYS = ["E1", "E2", "E3"]
EXIT_NAMES = {"E1": "次日收盘卖", "E2": "+4%/-2%触网", "E3": "持3日强平"}
GAP_MIN, GAP_MAX = 1.0, 4.0          # 今日高开区间(%)
W2_PCT_MAX = -3.0                    # 大阴线阈值(%)
W3_AMP_MIN = 6.0                     # 强势回落振幅阈值(%)
W1_CLOSE_BELOW_LU = 2.0              # 炸板：收盘低于涨停价幅度(%)
SLIP = 0.001                         # 单边滑点（与 engine.SLIPPAGE 一致）
CAPITAL = 100000.0
MAX_POS = 3                          # 对齐 engine score 缺省 max_positions
POS_PCT = 0.30                       # 对齐 engine score 缺省 position_pct
LIST_MIN_BARS = 60                   # 上市<60 日剔除（以本地 K 线根数为代理口径）
VR_FIRST_BAR = 1.5                   # min5 增强：首根5分钟量/昨日全天量 ≥1.5%
MIN5_DB = os.path.join(C.DATA_DIR, "min5.db")
# 判定（事先写死）
JUDGE_MIN_POS_WINDOWS = 3            # 最优变体 ≥3/4 窗口总收益为正
JUDGE_MIN_WINRATE = 0.48             # 整体胜率 ≥48%
JUDGE_MIN_TRADES = 80                # 每个窗口交易笔数 ≥80
BOARD_BULL_BASE = 0.2730             # Phase21 board 牛市基线
JUDGE_BULL_FLOOR = BOARD_BULL_BASE - 0.05   # 牛市不弱于 board 基线 -5pp


def load_pool():
    pool = json.load(open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8"))
    codes = pool["codes"][:500]
    ex = set(getattr(C, "DATA_EXCLUDE_CODES", None) or [])
    names = {}
    try:
        for row in json.load(open(os.path.join(C.DATA_DIR, "stock_list.json"), encoding="utf-8")):
            names[row[0]] = row[1]
    except Exception:
        pass
    kept = []
    for c in codes:
        if c in ex:
            continue
        nm = names.get(c, "")
        if "ST" in (nm or "").upper():
            continue
        kept.append(c)
    return kept, names


def load_daily(codes, start="2018-01-01", end="2026-08-21"):
    conn = sqlite3.connect("file:%s?mode=ro" % C.DB_FILE, uri=True, timeout=20)
    out = {}
    try:
        for i in range(0, len(codes), 100):
            chunk = codes[i:i + 100]
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT code,date,open,high,low,close,volume FROM kline "
                "WHERE period='day' AND length(code)=6 AND code IN (%s) "
                "AND date>=? AND date<=? ORDER BY code,date" % ph,
                tuple(chunk) + (start, end)).fetchall()
            for c, d, o, h, l, cl, v in rows:
                if cl is None or cl <= 0 or o is None or o <= 0:
                    continue
                out.setdefault(c, {"dates": [], "o": [], "h": [], "l": [], "c": [], "v": []})
                rec = out[c]
                rec["dates"].append(d)
                rec["o"].append(o)
                rec["h"].append(h)
                rec["l"].append(l)
                rec["c"].append(cl)
                rec["v"].append(v or 0.0)
    finally:
        conn.close()
    for c, rec in out.items():
        rec["idx"] = {d: i for i, d in enumerate(rec["dates"])}
    return out


def build_calendar(daily, w0, w1):
    s = set()
    for c, rec in daily.items():
        for d in rec["dates"]:
            if w0 <= d <= w1:
                s.add(d)
    return sorted(s)


def find_signals(daily, codes, names, w0, w1, weak_key):
    """返回 {day_date: [code,...]}：昨日弱势 + 今日高开转强。全部用 T 开盘及以前信息。"""
    sigs = {}
    for c in codes:
        rec = daily.get(c)
        if not rec:
            continue
        o, h, l, cl, dates = rec["o"], rec["h"], rec["l"], rec["c"], rec["dates"]
        n = len(cl)
        for i in range(LIST_MIN_BARS, n):          # i=今日索引；上市不足60根的已天然排除
            d = dates[i]
            if d < w0 or d > w1:
                continue
            if i < 2:
                continue
            yc, ycc = cl[i - 1], cl[i - 2]         # 昨收、前收
            if ycc <= 0:
                continue
            # --- 弱势定义（昨日）---
            if weak_key == "W1":
                lu, _ld = eng.limit_prices(c, ycc, names.get(c, ""))
                if not (abs(h[i - 1] - lu) < 0.005):          # 昨日摸板
                    continue
                if (lu - cl[i - 1]) / lu * 100.0 < W1_CLOSE_BELOW_LU:  # 收盘离板≥2%
                    continue
            elif weak_key == "W2":
                if (cl[i - 1] / ycc - 1.0) * 100.0 > W2_PCT_MAX:
                    continue
            elif weak_key == "W3":
                amp = (h[i - 1] - l[i - 1]) / ycc * 100.0
                if not (amp >= W3_AMP_MIN and cl[i - 1] < o[i - 1]):
                    continue
            # --- 转强（今日开盘）---
            g = (o[i] / yc - 1.0) * 100.0
            if not (GAP_MIN <= g <= GAP_MAX):
                continue
            sigs.setdefault(d, []).append(c)
    return sigs


def simulate(daily, calendar, sig_by_day, exit_key, w1_end):
    """组合级模拟：MAX_POS 槽位、pos_pct 现金比、T+1。返回指标与交易明细数。"""
    cash = CAPITAL
    pos = {}      # code -> dict(buy_px, qty, cost_all, entry_i, days)
    trades = {"n": 0, "win": 0}
    eq = []
    idx_of = {d: i for i, d in enumerate(calendar)}
    last_px = {}  # code -> 最近已知价（估值兜底）

    def do_sell(c, fill_px, di):
        nonlocal cash
        p = pos.pop(c)
        amount = fill_px * p["qty"]
        fee = eng.sell_fee(amount)
        cash += amount - fee
        pnl = amount - fee - p["cost_all"]
        trades["n"] += 1
        if pnl > 0:
            trades["win"] += 1

    for d in calendar:
        # ---- 1) 退出（先卖后买）----
        for c in list(pos.keys()):
            rec = daily[c]
            i = rec["idx"].get(d)
            p = pos[c]
            if i is None:
                continue                      # 停牌：无法交易
            held_days = i - p["entry_i"]      # T+1 后才可卖
            if held_days < 1:
                continue
            if exit_key == "E1":
                if held_days >= 1:
                    do_sell(c, rec["c"][i] * (1 - SLIP), i)
            elif exit_key == "E3":
                if held_days >= 3:
                    do_sell(c, rec["c"][i] * (1 - SLIP), i)
            else:                              # E2 触网（保守：先止损后止盈；跳空按开盘）
                stop_trig = p["buy_px"] * (1 - 0.02)
                tp_trig = p["buy_px"] * (1 + 0.04)
                o_, h_, l_ = rec["o"][i], rec["h"][i], rec["l"][i]
                if o_ <= stop_trig:
                    do_sell(c, o_ * (1 - SLIP), i)
                elif l_ <= stop_trig:
                    do_sell(c, stop_trig * (1 - SLIP), i)
                elif o_ >= tp_trig:
                    do_sell(c, o_ * (1 - SLIP), i)
                elif h_ >= tp_trig:
                    do_sell(c, tp_trig * (1 - SLIP), i)
        # ---- 2) 入场（今日开盘）----
        for c in sig_by_day.get(d) or []:
            if len(pos) >= MAX_POS:
                break
            if c in pos:
                continue
            rec = daily[c]
            i = rec["idx"].get(d)
            if i is None:
                continue
            px = rec["o"][i] * (1 + SLIP)
            qty = int((cash * POS_PCT) / px / 100) * 100
            if qty < 100:
                continue
            cost = px * qty
            fee = eng.buy_fee(cost)
            if cost + fee > cash:
                continue
            cash -= cost + fee
            pos[c] = {"buy_px": rec["o"][i], "qty": qty, "cost_all": cost + fee,
                      "entry_i": i}
        # ---- 3) 日终估值 ----
        mv = 0.0
        for c, p in pos.items():
            rec = daily[c]
            i = rec["idx"].get(d)
            px = rec["c"][i] if i is not None else last_px.get(c, p["buy_px"])
            last_px[c] = px
            mv += px * p["qty"]
        eq.append(cash + mv)
    # ---- 窗口末强平（按最后可得收盘，计滑点与费用）----
    for c in list(pos.keys()):
        rec = daily[c]
        i = max(0, len(rec["c"]) - 1)
        # 用窗口内该股最后一根
        di = None
        for dd in reversed(calendar):
            j = rec["idx"].get(dd)
            if j is not None:
                di = j
                break
        if di is None:
            continue
        p = pos[c]
        amount = rec["c"][di] * (1 - SLIP) * p["qty"]
        fee = eng.sell_fee(amount)
        cash += amount - fee
        pnl = amount - fee - p["cost_all"]
        trades["n"] += 1
        if pnl > 0:
            trades["win"] += 1
        pos.pop(c, None)
    total_ret = (cash / CAPITAL) - 1.0
    peak, mdd = -1e18, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return {
        "total_return": round(total_ret, 4),
        "trade_count": trades["n"],
        "win_rate": round(trades["win"] / trades["n"], 4) if trades["n"] else 0.0,
        "max_drawdown": round(mdd, 4),
    }


def load_min5_firstbar(pool_codes):
    """{(code, 'YYYY-MM-DD'): 首根5分钟量(换算为股)}。
    实测口径：kline_min5.date 形如 'YYYY-MM-DD 09:35:00'（含秒）；
    volume 单位为手（与日线股量差 ×100，已用 600519 交叉验证），此处统一换算成股。"""
    want = set()
    for c in pool_codes:
        want.add(("sh" if c.startswith("6") else "sz") + c)
    out = {}
    conn = sqlite3.connect("file:%s?mode=ro" % MIN5_DB, uri=True, timeout=20)
    try:
        codes_l = sorted(want)
        for i in range(0, len(codes_l), 80):
            chunk = codes_l[i:i + 80]
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT code, substr(date,1,10) AS day, volume FROM kline_min5 "
                "WHERE code IN (%s) AND substr(date,12,5)='09:35'" % ph,
                tuple(chunk)).fetchall()
            for c, day, v in rows:
                out[(c, day)] = (v or 0.0) / 100.0
    finally:
        conn.close()
    return out


def run():
    t0 = time.time()
    codes, names = load_pool()
    print("[pool] %d codes (excl DATA_EXCLUDE/ST)" % len(codes), flush=True)
    daily = load_daily(codes)
    print("[data] loaded %d codes daily, %.0fs" % (len(daily), time.time() - t0), flush=True)

    results = {}
    cal_cache = {}
    for wk, ek in itertools.product(WEAK_KEYS, EXIT_KEYS):
        cfg_key = "%s_%s" % (wk, ek)
        wins = []
        for wi, (w0, w1) in enumerate(WINDOWS):
            cal = cal_cache.setdefault(wi, build_calendar(daily, w0, w1))
            sigs = find_signals(daily, codes, names, w0, w1, wk)
            m = simulate(daily, cal, sigs, ek, w1)
            m["window"] = WIN_NAMES[wi]
            m["signal_days"] = len(sigs)
            wins.append(m)
            print("[sim] %s %s %s ret=%+.4f trades=%d win=%.3f" % (
                cfg_key, WIN_NAMES[wi], "", m["total_return"], m["trade_count"],
                m["win_rate"]), flush=True)
        results[cfg_key] = {"weak": WEAK_NAMES[wk], "exit": EXIT_NAMES[ek], "windows": wins}

    # ---- min5 增强（仅牛市窗口有覆盖）：首根5分钟量/昨日全天量 ≥1.5% 过滤对照 ----
    min5_cmp = {"note": "仅 2025-08-18~2026-08-21 覆盖期(第4窗口)；首5分钟量/昨日全天量≥1.5%",
                "variants": {}}
    try:
        fb = load_min5_firstbar(codes)
        daily_vol = {}
        for c, rec in daily.items():
            for i, d in enumerate(rec["dates"]):
                daily_vol[(c, d)] = rec["v"][i - 1] if i > 0 else 0.0
        w0, w1 = WINDOWS[3]
        cal = build_calendar(daily, w0, w1)
        for wk in WEAK_KEYS:
            sigs_raw = find_signals(daily, codes, names, w0, w1, wk)
            sigs_f = {}
            dropped = kept = 0
            for d, lst in sigs_raw.items():
                keep = []
                for c in lst:
                    key = ("sh" if c.startswith("6") else "sz") + c
                    fv = fb.get((key, d))
                    pv = daily_vol.get((c, d))
                    if fv is None or not pv:
                        dropped += 1
                        continue
                    if fv / pv >= VR_FIRST_BAR / 100.0:   # 量比≥1.5%（fv 已换算为股）
                        kept += 1
                        keep.append(c)
                    else:
                        dropped += 1
                if keep:
                    sigs_f[d] = keep
            row = {"signals_kept": kept, "signals_dropped": dropped}
            for ek in EXIT_KEYS:
                m = simulate(daily, cal, sigs_f, ek, w1)
                row[ek] = m
            min5_cmp["variants"][wk] = row
            print("[min5] %s kept=%d dropped=%d" % (wk, kept, dropped), flush=True)
    except Exception as e:
        min5_cmp["error"] = str(e)
        print("[min5] FAILED:", e, flush=True)

    # ---- 判定（事先写死）----
    judgement = []
    best = None
    for cfg_key, r in results.items():
        wins = r["windows"]
        pos_n = sum(1 for w in wins if w["total_return"] > 0)
        n_tr = sum(w["trade_count"] for w in wins)
        wr = (sum(w["win_rate"] * w["trade_count"] for w in wins) / n_tr) if n_tr else 0.0
        enough = all(w["trade_count"] >= JUDGE_MIN_TRADES for w in wins)
        bull_ok = wins[3]["total_return"] >= JUDGE_BULL_FLOOR
        passed = (pos_n >= JUDGE_MIN_POS_WINDOWS and wr >= JUDGE_MIN_WINRATE
                  and enough and bull_ok)
        judgement.append({
            "variant": cfg_key, "positive_windows": pos_n,
            "overall_win_rate": round(wr, 4), "all_windows_ge%d_trades" % JUDGE_MIN_TRADES: enough,
            "bull_return": wins[3]["total_return"], "bull_floor": round(JUDGE_BULL_FLOOR, 4),
            "pass": passed})
        if passed and (best is None or wr > best[1]):
            best = (cfg_key, wr)
    out = {
        "meta": {
            "pool": "bt_pool.json top500, 再滤 DATA_EXCLUDE_CODES/ST",
            "assumptions": {
                "capital": CAPITAL, "lot": 100, "slippage_one_way": SLIP,
                "fees": "app.engine.buy_fee/sell_fee（只读引用）", "t_plus_1": True,
                "max_positions": MAX_POS, "position_pct": POS_PCT,
                "entry": "今日开盘×(1+滑点)；同日多信号按代码序入剩余槽位",
                "listing_filter": "本地K线根数<60 剔除（上市时长的代理口径）",
                "e2_note": "自T+1起每日盘中触网判定；同日先止损后止盈；跳空穿越按开盘成交；窗口末未平仓强平",
            },
            "judge_rules": {
                "positive_windows_ge": JUDGE_MIN_POS_WINDOWS,
                "win_rate_ge": JUDGE_MIN_WINRATE,
                "trades_per_window_ge": JUDGE_MIN_TRADES,
                "bull_floor_vs_board_minus5pp": round(JUDGE_BULL_FLOOR, 4)},
        },
        "results": results, "min5_enhancement": min5_cmp, "judgement": judgement,
        "best_variant": best[0] if best else None,
        "elapsed_s": round(time.time() - t0, 1),
    }
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_JSON)
    print("\n=== 判定 ===")
    for j in judgement:
        print(j["variant"], "pos_win=%d win=%.3f pass=%s" % (
            j["positive_windows"], j["overall_win_rate"], j["pass"]))
    print("best:", out["best_variant"])
    print("saved", OUT_JSON)


if __name__ == "__main__":
    run()
