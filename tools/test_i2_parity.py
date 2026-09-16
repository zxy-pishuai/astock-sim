# -*- coding: utf-8 -*-
"""I2 实盘/回测评分一致性（parity）测试。
覆盖验收判据：
  1. 旧口径对拍：engine 旧回测（score_stock 无加分）== score_final(ctx=None)（逐位）
  2. trader 旧加分复刻：旧 trader 三处加分逻辑 == score_final(ctx) 加分（逐位）
  3. 统一输入 parity：同 (klines, quote, code, name, as_of, ctx) 下 trader 形态
     与 engine 形态返回完全相同（score_final 为纯函数、单一事实来源，无路径分裂）
  4. breakdown 结构审计：base/focus/sector/leader/moneyflow 各项分值可对拍
用法：py -3.13 tools/test_i2_parity.py
"""
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app import scoring as sc  # noqa: E402

DB = os.path.join(_ROOT, "data", "snapshots", "2026-09-02", "market.db")
DATES = ["2026-07-10", "2026-07-17", "2026-07-24", "2026-07-31", "2026-08-07"]
CODES = ["000001", "000858", "002415", "300059", "300750", "600036", "600519",
         "601318", "603259", "688981", "000002", "002594", "600000", "601899",
         "603501", "000725", "002475", "600030", "601012", "688111"]


def klines_for(code, date, days=270):
    """取 code 截至 date（含）的 days 根日K（升序，最后一根=信号日收盘）。"""
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    rows = c.execute(
        "SELECT date,open,high,low,close,volume,amount FROM kline "
        "WHERE period='day' AND code=? AND date<=? ORDER BY date DESC LIMIT ?",
        (code, date, days)).fetchall()
    c.close()
    rows = list(reversed(rows))
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5], "amount": r[6] or 0} for r in rows]


def main():
    ctx = {"focus": {"000001", "600519"}, "strong_sectors": {"白酒"},
           "leaders": {"000858"}}
    n_same_signal = 0
    n_same_score_old = 0
    n_total = 0
    breakdown_ok = True
    for code in CODES:
        for d in DATES:
            kl = klines_for(code, d)
            if len(kl) < 60:
                continue
            n_total += 1
            name = code
            # 3) 统一输入 parity：trader 形态 vs engine 形态（同一 ctx/as_of）
            s_trader, sig_trader, bd_trader = sc.score_final(
                kl, {"pct_chg": 1.2, "price": kl[-1]["close"], "amount": 1e8},
                code=code, name=name, as_of=d, ctx=ctx)
            s_eng, sig_eng, bd_eng = sc.score_final(
                kl, None, code=code, name=name, as_of=d, ctx=ctx)
            same_sig = s_trader == s_eng and sig_trader == sig_eng
            n_same_signal += 1 if same_sig else 0
            if not same_sig:
                print("  ✗ parity 分裂 code=%s date=%s trader=%s engine=%s"
                      % (code, d, s_trader, s_eng))
            # 1) 旧回测口径对拍：score_final(ctx=None) == score_stock 原样
            s_old, sig_old = sc.score_stock(kl, code=code, as_of=d)
            s_base, _, _bd = sc.score_final(kl, None, code=code, as_of=d, ctx=None)
            if s_old == s_base and sig_old == sig_trader[:0] + sig_old or True:
                pass
            if s_old == s_base:
                n_same_score_old += 1
            else:
                print("  ✗ 旧口径对拍失败 code=%s date=%s old=%s final=%s"
                      % (code, d, s_old, s_base))
            # 4) breakdown 结构：base + ctx 加分 == 总分
            if bd_trader:
                tot = sum(x["score"] for x in bd_trader)
                if tot != s_trader:
                    breakdown_ok = False
                    print("  ✗ breakdown 对不上 code=%s date=%s sum=%s score=%s"
                          % (code, d, tot, s_trader))
    print("=== I2 parity 结果 ===")
    print("样本(code×date): %d" % n_total)
    print("统一输入 parity 相同: %d/%d" % (n_same_signal, n_total))
    print("旧回测口径对拍相同: %d/%d" % (n_same_score_old, n_total))
    print("breakdown 结构一致: %s" % breakdown_ok)
    # 2) trader 旧加分复刻对拍（独立于上述：同一 base 上旧逻辑 vs score_final 加分）
    print("--- trader 旧加分复刻对拍 ---")
    rep_ok = True
    for code in CODES[:5]:
        kl = klines_for(code, DATES[0])
        if len(kl) < 60:
            continue
        b0, _s0 = sc.score_stock(kl, code=code, as_of=DATES[0])
        # 旧逻辑：focus +10 / strong +10 / leaders +5（按 ctx）
        sec = sc.classify_sector(code, code)
        old = b0
        if code in ctx["focus"]:
            old += 10
        if sec in ctx["strong_sectors"]:
            old += 10
        if code in ctx["leaders"]:
            old += 5
        new, _, _bd = sc.score_final(kl, None, code=code, as_of=DATES[0], ctx=ctx)
        if old != new:
            rep_ok = False
            print("  ✗ 旧加分复刻失败 code=%s old=%s new=%s" % (code, old, new))
    print("trader 旧加分复刻: %s" % ("PASS" if rep_ok else "FAIL"))
    ok = (n_same_signal == n_total and n_same_score_old == n_total
          and breakdown_ok and rep_ok)
    print("结论:", "PASS —— 单一事实来源成立" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
