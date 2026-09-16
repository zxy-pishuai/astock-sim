import json, os, sys, time
BASE=r"C:\Users\26838\A股模拟盘"
if BASE not in sys.path:
    sys.path.insert(0,BASE)
from app import config as C, engine as eng
POOL_SIZE=500
WINDOWS=[["2019-01-01","2020-12-31"],["2021-01-01","2022-12-31"],["2023-01-01","2024-12-31"],["2025-08-18","2026-08-18"]]
TAGS=["2019-20","2021-22","2023-24","近1年"]
STRAT_PARAMS={"score":{"buy_threshold":25,"max_positions":3,"position_pct":0.30},"board":{"buy_threshold":40,"max_positions":2,"position_pct":0.25}}
SLIPPAGE=0.001
pool=json.load(open(os.path.join(C.DATA_DIR,"bt_pool.json"),encoding="utf-8"))
codes=list(pool["codes"])[:POOL_SIZE]
names={c:c for c in codes}
old_sell = eng.Backtest._sell_price
def _check_exits_ab(self, date, f_enabled, b_enabled, e_enabled):
    for code in list(self.positions.keys()):
        pos = self.positions[code]
        bar = self._bar(code, date)
        if not bar:
            continue
        cp = bar["close"]
        entry = pos["entry_price"]
        pnl_pct = (cp - entry) / entry
        pos["days"] += 1
        if bar["high"] > pos["peak"]:
            pos["peak"] = bar["high"]
        if pos["days"] < 1:
            continue
        if f_enabled:
            _h48 = self._hist_klines(code, date)
            if len(_h48) >= 2 and _h48[-2]["close"] > 0:
                _lu48, _ld48 = eng.limit_prices(code, _h48[-2]["close"], self.names.get(code, ""))
                if (_ld48 > 0 and bar["low"] <= _ld48 * 1.001 and cp <= _ld48 * 1.001):
                    continue
        reason = None
        if b_enabled and self.strategy == "board" and pos.get("board_trade"):
            if pos["days"] >= 2:
                pos.pop("board_trade", None)
            elif pos["days"] == 1:
                _h48 = self._hist_klines(code, date)
                if len(_h48) >= 2 and _h48[-2]["close"] > 0:
                    _yc48 = _h48[-2]["close"]
                    _trig = _yc48 * (1 + C.BOARD_NEXT_DAY_LOW)
                    if bar["low"] <= _trig:
                        base = max(bar["open"], _trig)
                        fill = self._sell_price(code, base, pos["qty"], bar) if e_enabled else base * (1 - self.slippage)
                        self._sell(date, code, fill, pos["qty"], f"打板低开止损({(fill/entry-1):+.1%})")
                        continue
                    _hi_trig = entry * 1.05
                    if bar["high"] >= _hi_trig:
                        _half = pos["qty"] // 200 * 100
                        if _half >= 100:
                            base = max(bar["open"], _hi_trig)
                            fill = self._sell_price(code, base, _half, bar) if e_enabled else base * (1 - self.slippage)
                            self._sell(date, code, fill, _half, f"打板半仓止盈({((_hi_trig-entry)/entry):+.1%})")
                            continue
        if self.next_close_sell and pos["days"] == 1:
            fill = self._sell_price(code, cp, pos["qty"], bar) if e_enabled else cp * (1 - self.slippage)
            self._sell(date, code, fill, pos["qty"], "两点半战法:次日收盘卖")
            continue
        if C.LADDER_TP_ENABLED and pos["qty"] > 0:
            for i, (thr, frac) in enumerate(C.LADDER_TP_STEPS):
                if i in (pos.get("ladder_sold") or []):
                    continue
                if pnl_pct < thr:
                    continue
                qty_total = pos["qty"]
                qty_sell = int(qty_total * frac / 100) * 100
                if qty_sell < 100:
                    qty_sell = qty_total
                qty_sell = min(qty_sell, qty_total)
                pos["ladder_sold"] = list(pos.get("ladder_sold") or []) + [i]
                fill = self._sell_price(code, cp, qty_sell, bar) if e_enabled else cp * (1 - self.slippage)
                self._sell(date, code, fill, qty_sell, f"阶梯止盈(+{int(round(thr*100))}%)(+{pnl_pct:+.1%})")
                if pos["qty"] <= 0:
                    break
        if C.VOLP_SELL_ENABLED and pos["qty"] > 0 and not pos.get("volp_half_done"):
            try:
                from app import volprice_sell as vps
                _hist = self._hist_klines(code, date)
                if len(_hist) >= 6:
                    _vk, _vratio, _vr = vps.volp_sell_signal(_hist)
                    if _vk == "volp_surge_stall":
                        fill = self._sell_price(code, cp, pos["qty"], bar) if e_enabled else cp*(1-self.slippage)
                        self._sell(date, code, fill, pos["qty"], _vr)
                        continue
                    elif _vk == "volp_shrink_newhigh":
                        _hqty = int(pos["qty"] * C.VOLP_SELL_HALF_PCT / 100) * 100
                        if _hqty >= 100:
                            pos["volp_half_done"] = True
                            fill = self._sell_price(code, cp, min(_hqty, pos["qty"]), bar) if e_enabled else cp*(1-self.slippage)
                            self._sell(date, code, fill, min(_hqty, pos["qty"]), _vr)
                            if pos["qty"] <= 0:
                                continue
            except Exception:
                pass
        if pos["qty"] > 0:
            if pnl_pct <= C.STOP_LOSS_PCT:
                reason = "止损"
            elif pnl_pct >= C.TAKE_PROFIT_PCT:
                reason = "止盈"
            elif (pos["days"] >= 2 and pos["peak"] >= entry * (1 + C.INTRADAY_HIGH_TRIGGER) and (pos["peak"] - cp) / pos["peak"] >= C.INTRADAY_PULLBACK and pnl_pct >= 0):
                reason = f"冲高回落止盈(峰{pos['peak']:.2f}→{cp:.2f})"
            elif pos["peak"] >= entry * (1 + C.TRAILING_ACTIVATE_PCT) and cp <= pos["peak"] * (1 + C.TRAILING_STOP_PCT):
                reason = f"移动止损(峰{pos['peak']:.2f})"
            elif pos["days"] >= C.TIME_STOP_DAYS and pnl_pct < 0:
                reason = f"时间止损({pos['days']}天亏{pnl_pct:+.1%})"
            elif pos["days"] >= C.MAX_HOLD_DAYS:
                reason = f"超时退出({pos['days']}天)"
            if not reason and pnl_pct < 0.05:
                hist = self._hist_klines(code, date)
                if eng._distribution_signal(hist):
                    reason = "主力出货"
            if reason:
                fill = self._sell_price(code, cp, pos["qty"], bar) if e_enabled else cp*(1-self.slippage)
                self._sell(date, code, fill, pos["qty"], reason)

def run_one(strategy,widx,f_enabled,b_enabled,e_enabled):
    w0,w1=WINDOWS[widx]
    orig = eng.df.fetch_quotes
    orig_mom = getattr(C,"BOARD_MOMENTUM_MIN",None)
    eng.df.fetch_quotes = lambda cs: {}
    C.BOARD_MOMENTUM_MIN = 7.0
    old_check = eng.Backtest._check_exits
    old_sell2 = eng.Backtest._sell_price
    def sell_patch(self, code, base_px, qty, bar=None):
        if not e_enabled:
            return base_px * (1 - self.slippage)
        else:
            return old_sell(self, code, base_px, qty, bar)
    eng.Backtest._sell_price = sell_patch
    def check_patch(self, date):
        return _check_exits_ab(self, date, f_enabled, b_enabled, e_enabled)
    eng.Backtest._check_exits = check_patch
    try:
        params=dict(STRAT_PARAMS[strategy])
        params.update({"zt_eco_gate":False,"dd_gate":False,"slippage":SLIPPAGE})
        bt=eng.Backtest(codes,names,w0,w1,100000.0,strategy,params)
        # also patch期末清仓
        orig_run = eng.Backtest.run
        # run but intercept期末清仓: we patch _sell_price already, so run's final sell will use patched
        r=bt.run()
        return {"total_return":r.get("total_return"),"max_drawdown":r.get("max_drawdown"),"trade_count":len(bt.trades)}
    finally:
        eng.Backtest._check_exits = old_check
        eng.Backtest._sell_price = old_sell2
        eng.df.fetch_quotes = orig
        if orig_mom is not None:
            C.BOARD_MOMENTUM_MIN = orig_mom

# Only run 4 key combos quickly
import time
start=time.time()
for strat in ["score","board"]:
    for wi in [1,3]:
        r0=run_one(strat,wi,False,False,False)
        rf=run_one(strat,wi,True,False,False)
        re=run_one(strat,wi,False,False,True)
        rb=run_one(strat,wi,False,True,False)
        ra=run_one(strat,wi,True,True,True)
        print(f"{strat} {TAGS[wi]} before {r0['total_return']:+.4f} F {rf['total_return']:+.4f} (Δ{rf['total_return']-r0['total_return']:+.4f}) E {re['total_return']:+.4f} (Δ{re['total_return']-r0['total_return']:+.4f}) B {rb['total_return']:+.4f} (Δ{rb['total_return']-r0['total_return']:+.4f}) after {ra['total_return']:+.4f}")
        print(f"  trades before {r0['trade_count']} F {rf['trade_count']} E {re['trade_count']} B {rb['trade_count']} after {ra['trade_count']}")
        print(f"  dd before {r0['max_drawdown']:+.4f} -> F {rf['max_drawdown']:+.4f} E {re['max_drawdown']:+.4f} B {rb['max_drawdown']:+.4f} after {ra['max_drawdown']:+.4f}")
print("elapsed", time.time()-start)
