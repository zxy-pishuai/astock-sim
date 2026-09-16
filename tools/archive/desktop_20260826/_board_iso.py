import json, os, sys, time
BASE=r"C:\Users\26838\A股模拟盘"
sys.path.insert(0,BASE)
from app import config as C, engine as eng
WINDOWS=[["2019-01-01","2020-12-31"],["2021-01-01","2022-12-31"],["2023-01-01","2024-12-31"],["2025-08-18","2026-08-18"]]
TAGS=["2019-20","2021-22","2023-24","近1年"]
pool=json.load(open(os.path.join(C.DATA_DIR,"bt_pool.json"),encoding="utf-8"))
codes=list(pool["codes"])[:500]
names={c:c for c in codes}
old_sell=eng.Backtest._sell_price
def _check_ab(self, date, f, b, e):
    for code in list(self.positions.keys()):
        pos=self.positions[code]
        bar=self._bar(code,date)
        if not bar: continue
        cp=bar["close"]; entry=pos["entry_price"]; pnl_pct=(cp-entry)/entry
        pos["days"]+=1
        if bar["high"]>pos["peak"]: pos["peak"]=bar["high"]
        if pos["days"]<1: continue
        if f:
            _h=self._hist_klines(code,date)
            if len(_h)>=2 and _h[-2]["close"]>0:
                _,ld=eng.limit_prices(code,_h[-2]["close"],self.names.get(code,""))
                if ld>0 and bar["low"]<=ld*1.001 and cp<=ld*1.001:
                    continue
        if b and self.strategy=="board" and pos.get("board_trade"):
            if pos["days"]>=2:
                pos.pop("board_trade",None)
            elif pos["days"]==1:
                _h=self._hist_klines(code,date)
                if len(_h)>=2 and _h[-2]["close"]>0:
                    yc=_h[-2]["close"]; trig=yc*(1+C.BOARD_NEXT_DAY_LOW)
                    if bar["low"]<=trig:
                        base=max(bar["open"],trig)
                        fill=self._sell_price(code,base,pos["qty"],bar) if e else base*(1-self.slippage)
                        self._sell(date,code,fill,pos["qty"],f"打板低开止损({(fill/entry-1):+.1%})")
                        continue
                    hi=entry*1.05
                    if bar["high"]>=hi:
                        half=pos["qty"]//200*100
                        if half>=100:
                            base=max(bar["open"],hi)
                            fill=self._sell_price(code,base,half,bar) if e else base*(1-self.slippage)
                            self._sell(date,code,fill,half,f"打板半仓止盈({((hi-entry)/entry):+.1%})")
                            continue
        if self.next_close_sell and pos["days"]==1:
            fill=self._sell_price(code,cp,pos["qty"],bar) if e else cp*(1-self.slippage)
            self._sell(date,code,fill,pos["qty"],"两点半战法:次日收盘卖"); continue
        if C.LADDER_TP_ENABLED and pos["qty"]>0:
            for i,(thr,frac) in enumerate(C.LADDER_TP_STEPS):
                if i in (pos.get("ladder_sold") or []): continue
                if pnl_pct<thr: continue
                qty_total=pos["qty"]; qty_sell=int(qty_total*frac/100)*100
                if qty_sell<100: qty_sell=qty_total
                qty_sell=min(qty_sell,qty_total)
                pos["ladder_sold"]=list(pos.get("ladder_sold") or [])+[i]
                fill=self._sell_price(code,cp,qty_sell,bar) if e else cp*(1-self.slippage)
                self._sell(date,code,fill,qty_sell,f"阶梯止盈(+{int(round(thr*100))}%)(+{pnl_pct:+.1%})")
                if pos["qty"]<=0: break
        if C.VOLP_SELL_ENABLED and pos["qty"]>0 and not pos.get("volp_half_done"):
            try:
                from app import volprice_sell as vps
                _hist=self._hist_klines(code,date)
                if len(_hist)>=6:
                    _vk,_,_vr=vps.volp_sell_signal(_hist)
                    if _vk=="volp_surge_stall":
                        fill=self._sell_price(code,cp,pos["qty"],bar) if e else cp*(1-self.slippage)
                        self._sell(date,code,fill,pos["qty"],_vr); continue
                    elif _vk=="volp_shrink_newhigh":
                        _hqty=int(pos["qty"]*C.VOLP_SELL_HALF_PCT/100)*100
                        if _hqty>=100:
                            pos["volp_half_done"]=True
                            fill=self._sell_price(code,cp,min(_hqty,pos["qty"]),bar) if e else cp*(1-self.slippage)
                            self._sell(date,code,fill,min(_hqty,pos["qty"]),_vr)
                            if pos["qty"]<=0: continue
            except: pass
        if pos["qty"]>0:
            reason=None
            if pnl_pct<=C.STOP_LOSS_PCT: reason="止损"
            elif pnl_pct>=C.TAKE_PROFIT_PCT: reason="止盈"
            elif pos["days"]>=2 and pos["peak"]>=entry*(1+C.INTRADAY_HIGH_TRIGGER) and (pos["peak"]-cp)/pos["peak"]>=C.INTRADAY_PULLBACK and pnl_pct>=0: reason=f"冲高回落止盈(峰{pos['peak']:.2f}→{cp:.2f})"
            elif pos["peak"]>=entry*(1+C.TRAILING_ACTIVATE_PCT) and cp<=pos["peak"]*(1+C.TRAILING_STOP_PCT): reason=f"移动止损(峰{pos['peak']:.2f})"
            elif pos["days"]>=C.TIME_STOP_DAYS and pnl_pct<0: reason=f"时间止损({pos['days']}天亏{pnl_pct:+.1%})"
            elif pos["days"]>=C.MAX_HOLD_DAYS: reason=f"超时退出({pos['days']}天)"
            if not reason and pnl_pct<0.05:
                hist=self._hist_klines(code,date)
                if eng._distribution_signal(hist): reason="主力出货"
            if reason:
                fill=self._sell_price(code,cp,pos["qty"],bar) if e else cp*(1-self.slippage)
                self._sell(date,code,fill,pos["qty"],reason)

def run_one(widx,f,b,e):
    w0,w1=WINDOWS[widx]
    orig=eng.df.fetch_quotes; orig_mom=getattr(C,"BOARD_MOMENTUM_MIN",None)
    eng.df.fetch_quotes=lambda cs:{}; C.BOARD_MOMENTUM_MIN=7.0
    old_check=eng.Backtest._check_exits; old_sell2=eng.Backtest._sell_price
    def sell_patch(self,code,base_px,qty,bar=None):
        return old_sell(self,code,base_px,qty,bar) if e else base_px*(1-self.slippage)
    eng.Backtest._sell_price=sell_patch
    def check_patch(self,date): return _check_ab(self,date,f,b,e)
    eng.Backtest._check_exits=check_patch
    try:
        params={"buy_threshold":40,"max_positions":2,"position_pct":0.25,"zt_eco_gate":False,"dd_gate":False,"slippage":0.001}
        bt=eng.Backtest(codes,names,w0,w1,100000.0,"board",params)
        r=bt.run()
        return {"ret":r.get("total_return"),"dd":r.get("max_drawdown"),"trades":len(bt.trades)}
    finally:
        eng.Backtest._check_exits=old_check; eng.Backtest._sell_price=old_sell2; eng.df.fetch_quotes=orig
        if orig_mom is not None: C.BOARD_MOMENTUM_MIN=orig_mom

for widx in range(4):
    r0=run_one(widx,False,False,False)
    rf=run_one(widx,True,False,False)
    re=run_one(widx,False,False,True)
    rb=run_one(widx,False,True,False)
    ra=run_one(widx,True,True,True)
    print(f"board {TAGS[widx]} before {r0['ret']:+.4f} F {rf['ret']:+.4f} Δ{rf['ret']-r0['ret']:+.4f} E {re['ret']:+.4f} Δ{re['ret']-r0['ret']:+.4f} B {rb['ret']:+.4f} Δ{rb['ret']-r0['ret']:+.4f} after {ra['ret']:+.4f} sum {rf['ret']-r0['ret']+re['ret']-r0['ret']+rb['ret']-r0['ret']:+.4f} nonadd {ra['ret']-r0['ret']-(rf['ret']-r0['ret']+re['ret']-r0['ret']+rb['ret']-r0['ret']):+.4f}")
print("board iso done")
