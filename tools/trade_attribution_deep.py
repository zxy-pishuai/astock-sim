# -*- coding: utf-8 -*-
"""★ Phase30：信号级交易归因 —— 回答"谁在赚钱、谁在亏钱"

独立进程运行（与 Phase23/26 同模式：直接 import app.engine，不走服务 HTTP）。
跑四窗口 ×（score+board）基线回测，直接读 Backtest 对象的 .trades 属性全量明细
（result["trades"] 只留最近 500 笔，不可用），按三个维度归因：
  1) 买入信号类别（reason 文本关键词映射，映射表见 docs/reports/attribution.md 附录）
  2) 卖出规则类别（含 ★"卖飞成本"：卖出后该股 5 日后续涨跌幅均值）
  3) 持仓天数分桶（1 天 / 2 天 / 3-5 天 / 5 天以上）

口径对齐 Phase21/22/25/26 基线配方：
  - 池 = data/bt_pool.json 前 500 只，names 用代码兜底
  - fetch_quotes 置空（实时流通市值快照不可复现 → 成交额×20 近似）
  - board 参数镜像实盘打板配置；score 用默认参数；zt_eco_gate/dd_gate 显式关闭
  - seed=42（engine 实例级 RNG，成交概率模拟可复现）

模拟盘侧：解析 data/audit/audit.jsonl 的 BUY/SELL 事件（trading_event 只有
msg 文本，用正则解析；manual_* 有结构化字段），并以 data/account.json 的
结构化 trades 交叉补全。样本如实呈现，不外推。

红线：只读 app/ 与 config；只新增 tools/、data/attribution/、docs/reports/ 文件。

用法:
  python tools/trade_attribution_deep.py                 # 跑回测 + 分析
  python tools/trade_attribution_deep.py --analyze-only  # 复用已落盘 trades 只重分析
输出:
  data/attribution/backtest_trades.json   全量交易明细 + reason 样本枚举
  data/attribution/by_signal.json         买入信号归因 + 信号×窗口交叉 + 持仓天数分桶 + 模拟盘买入归因
  data/attribution/by_exit.json           卖出规则归因（含卖飞成本） + 模拟盘卖出归因
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WINDOWS = [
    ["2019-01-01", "2020-12-31"],
    ["2021-01-01", "2022-12-31"],
    ["2023-01-01", "2024-12-31"],
    ["2025-08-18", "2026-08-18"],   # 近一年（牛市窗口）
]
WINDOW_TAGS = ["2019-20", "2021-22", "2023-24", "近1年"]
POOL_SIZE = 500
BOARD_PARAMS = {"buy_threshold": 40, "max_positions": 2,
                "position_pct": 0.25, "slippage": 0.001}
BASE_PARAMS = {"zt_eco_gate": False, "dd_gate": False}   # 基线：闸门全关

ATTR_DIR = os.path.join(BASE, "data", "attribution")
TRADES_JSON = os.path.join(ATTR_DIR, "backtest_trades.json")
SIGNAL_JSON = os.path.join(ATTR_DIR, "by_signal.json")
EXIT_JSON = os.path.join(ATTR_DIR, "by_exit.json")
AUDIT_JSONL = os.path.join(BASE, "data", "audit", "audit.jsonl")
ACCOUNT_JSON = os.path.join(BASE, "data", "account.json")

# ============ 买入信号映射表（人工可读；附录同步维护） ============
# reason 格式: "[策略]评分N分: 信号A(+w)、信号B(+w)、信号C(+w)"
# 归类规则：取第一个非⚠信号为主动类别（全是⚠时取第一个）；规范化 = 去⚠、去括号参数，
# 再按 SUBSTR_RULES 合并同族变体。未命中 KNOWN_SIGNALS 的归入原样类别并记入 unmapped。
SUBSTR_RULES = [
    ("换手", "换手结构"),        # 打板：换手可接受/换手适中 等变体合并
    ("业绩", "业绩事件"),        # 预增/预减/扭亏/超预期 等 earnings_signal 文本
    ("ml_pred", "ML预测"),       # Phase25 ml_pred 加权因子
    ("ML", "ML预测"),
]
KNOWN_SIGNALS = {
    # score 评分策略（app/scoring.py score_stock）
    "量价齐升", "放量突破20日新高", "均线多头", "RSRS看涨", "MACD金叉",
    "RSI超卖反弹", "缩量回踩", "回踩20日线企稳", "MA5/10金叉", "海龟20日突破",
    "RSRS加速", "高位放量滞涨", "RSI超买",
    "量价相关", "量能递增", "站稳VWAP", "放量启动", "量价背离", "量价负相关",
    "数据不足",
    # board 打板策略（app/scoring.py score_board / seal_quality_bonus 全枚举）
    "涨停动量启动", "涨停动量较强", "涨停动量很强", "涨停动量极强",
    "巨量封板", "放量扫板", "温和放量",
    "换手适中", "换手可接受", "换手结构",
    "首板优先", "二板接力", "三板博弈",
    "买方强势", "早盘封板", "午前封板", "尾盘慎入",
    "振幅过大", "外盘主动买入", "买方略占优", "买盘强", "买盘占优",
    "封单强", "封单可", "缩量封板惜售", "封板分歧加大",
    "首封今日早", "尾盘板", "封单资金比强", "封单资金比中",
    "零炸板", "多次炸板",
}

PENALTY_MARK = "⚠"


def norm_signal(tok):
    """单个信号文本 → 规范化类别名"""
    t = tok.strip()
    t = t.replace(PENALTY_MARK, "")
    t = t.split("(")[0].strip()
    # 数值写在括号前的变体：量价相关0.92 / 放量启动2.4x → 去掉尾随数值
    t = re.sub(r"[0-9]+(\.[0-9]+)?\s*x?$", "", t).strip()
    for sub, cat in SUBSTR_RULES:
        if sub.lower() in t.lower():
            return cat
    return t


def parse_buy_reason(reason):
    """买入 reason → (strategy_tag, [signal tokens])；解析失败返回 ("?", [])"""
    m = re.match(r"\[([^\]]+)\][^:]*:\s*(.+)", reason)
    if not m:
        return "?", []
    tag = m.group(1)
    toks = [t for t in m.group(2).split("、") if t.strip()]
    return tag, toks


def primary_category(reason):
    """买入 reason → 主动信号类别（第一个非惩罚信号；全惩罚取第一个）"""
    _, toks = parse_buy_reason(reason)
    if not toks:
        return "未解析"
    cats = [norm_signal(t) for t in toks]
    for c in cats:
        if c != "数据不足":
            return c
    return cats[0]


# ============ 卖出规则分类（engine._check_exits / 末日清仓的 reason 文本） ============
SELL_RULES = [
    ("阶梯止盈", lambda r: r.startswith("阶梯止盈")),
    ("量价卖出", lambda r: ("缩量新高" in r or "放量滞涨" in r)),
    ("两点半次日收盘卖", lambda r: "两点半" in r),
    ("冲高回落止盈", lambda r: r.startswith("冲高回落止盈")),
    ("移动止损", lambda r: r.startswith("移动止损")),
    ("时间止损", lambda r: r.startswith("时间止损")),
    ("超时退出", lambda r: r.startswith("超时退出")),
    ("止损", lambda r: r == "止损"),
    ("全清止盈", lambda r: r == "止盈"),
    ("主力出货", lambda r: r == "主力出货"),
    ("期末清仓", lambda r: r == "期末清仓"),
]


def classify_sell(reason):
    for cat, fn in SELL_RULES:
        if fn(reason):
            return cat
    return "其他:" + reason[:40]


# ============ 回测执行 ============
def build_pool():
    from app import config as C
    with open(os.path.join(C.DATA_DIR, "bt_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    codes = list(pool["codes"])[:POOL_SIZE]
    return codes, {c: c for c in codes}


def run_one(codes, names, strategy, widx):
    """跑单窗单策略；返回该窗全量 trades（附 didx/fwd5）与汇总指标"""
    from app import config as C
    from app import engine as eng
    w0, w1 = WINDOWS[widx]
    t0 = time.time()
    orig_quotes = eng.df.fetch_quotes
    eng.df.fetch_quotes = lambda cs: {}   # 基线配方：市值走成交额×20 近似
    try:
        params = dict(BOARD_PARAMS) if strategy == "board" else {}
        params.update(BASE_PARAMS)
        bt = eng.Backtest(codes, names, w0, w1, 100000.0, strategy, params)
        r = bt.run()
    finally:
        eng.df.fetch_quotes = orig_quotes
    if "error" in r:
        print("  [%s %s] ERROR: %s" % (strategy, WINDOW_TAGS[widx], r["error"]),
              flush=True)
        return {"strategy": strategy, "widx": widx, "window": [w0, w1],
                "error": r["error"], "trades": []}

    day_pos = {d: i for i, d in enumerate(bt.trading_days)}
    # 每只票的收盘序列 + 日期索引（算卖飞成本用，直接复用引擎已加载数据，不二次读库）
    closes_by_code = {}
    for code, kl in bt.klines.items():
        closes_by_code[code] = ([k["close"] for k in kl],
                                bt.date_index.get(code) or {})
    trades = []
    for t in bt.trades:
        rec = {k: t[k] for k in ("date", "code", "name", "side", "price",
                                 "qty", "amount", "fee", "pnl", "reason")}
        rec["didx"] = day_pos.get(t["date"])
        rec["widx"] = widx
        rec["strategy"] = strategy
        rec["fwd5"] = None
        if t["side"] == "sell":
            cl, di = closes_by_code.get(t["code"], ([], {}))
            i = di.get(t["date"])
            if i is not None and 0 <= i and i + 5 < len(cl) and cl[i]:
                rec["fwd5"] = round(cl[i + 5] / cl[i] - 1.0, 6)
        trades.append(rec)
    row = {
        "strategy": strategy, "widx": widx, "window": [w0, w1],
        "total_return": r.get("total_return"),
        "max_drawdown": r.get("max_drawdown"),
        "win_rate": r.get("win_rate"),
        "trade_count": len(trades),
        "buy_count": sum(1 for t in trades if t["side"] == "buy"),
        "elapsed": round(time.time() - t0, 1),
        "trades": trades,
    }
    print("  [%s %s] ret=%.4f dd=%.4f trades=%d (%.0fs)" % (
        strategy, WINDOW_TAGS[widx], row["total_return"] or 0,
        row["max_drawdown"] or 0, len(trades), row["elapsed"]), flush=True)
    return row


# ============ 回合构建（FIFO 配对买↔卖） ============
def build_rounds(trades):
    """把全量交易按代码 FIFO 配对成回合。
    返回 rounds 列表：每笔买入 lot 的已实现归因（部分卖按数量比例分摊 pnl）。
    """
    lots = defaultdict(list)   # code -> [lot, ...] FIFO
    rounds = []
    for t in sorted(trades, key=lambda x: (x.get("didx") if x.get("didx") is not None else 0,
                                           0 if x["side"] == "buy" else 1)):
        code = t["code"]
        if t["side"] == "buy":
            lots[code].append({
                "code": code, "name": t["name"], "entry_date": t["date"],
                "entry_didx": t.get("didx"), "qty_left": t["qty"],
                "amount": t["amount"], "cat": primary_category(t["reason"]),
                "reason": t["reason"], "widx": t.get("widx"),
                "strategy": t.get("strategy"), "realized": 0.0,
                "exit_date": None, "exit_didx": None, "exit_cat": None,
            })
            continue
        # sell：按 FIFO 消耗
        qty_left = t["qty"]
        sell_qty = max(t["qty"], 1)
        pnl = t.get("pnl") or 0.0
        while qty_left > 0 and lots[code]:
            lot = lots[code][0]
            q = min(qty_left, lot["qty_left"])
            alloc = pnl * (q / float(sell_qty))
            lot["realized"] += alloc
            lot["exit_date"] = t["date"]
            lot["exit_didx"] = t.get("didx")
            lot["exit_cat"] = classify_sell(t["reason"])
            lot["qty_left"] -= q
            qty_left -= q
            if lot["qty_left"] <= 0:
                lots[code].pop(0)
                hold = None
                if lot["entry_didx"] is not None and lot["exit_didx"] is not None:
                    hold = lot["exit_didx"] - lot["entry_didx"]
                rounds.append({
                    "code": lot["code"], "name": lot["name"],
                    "strategy": lot["strategy"], "widx": lot["widx"],
                    "cat": lot["cat"], "entry_date": lot["entry_date"],
                    "exit_date": lot["exit_date"], "hold_days": hold,
                    "amount": lot["amount"], "pnl": round(lot["realized"], 2),
                    "ret": round(lot["realized"] / lot["amount"], 6) if lot["amount"] else None,
                    "exit_cat": lot["exit_cat"], "reason": lot["reason"],
                })
    return rounds


# ============ 统计聚合 ============
def agg_stats(items, pnl_key="pnl", ret_key="ret"):
    n = len(items)
    pnls = [it[pnl_key] for it in items]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), -sum(losses)
    rets = [it[ret_key] for it in items if it.get(ret_key) is not None]
    return {
        "count": n,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "avg_pnl": round(sum(pnls) / n, 2) if n else None,
        "avg_ret": round(sum(rets) / len(rets), 6) if rets else None,
        "profit_factor": round(gw / gl, 3) if gl > 0 else (99.0 if gw > 0 else None),
        "total_pnl": round(sum(pnls), 2),
    }


def bucket_of(hold):
    if hold is None:
        return None
    if hold <= 1:
        return "1天"
    if hold == 2:
        return "2天"
    if hold <= 5:
        return "3-5天"
    return "5天+"


# ============ 分析主流程 ============
def analyze(runs):
    all_trades = [t for r in runs for t in r.get("trades", [])]
    rounds = build_rounds(all_trades)

    # --- 1. 买入信号归因 ---
    by_signal = {}
    for cat in sorted(set(rd["cat"] for rd in rounds)):
        grp = [rd for rd in rounds if rd["cat"] == cat]
        st = agg_stats(grp)
        holds = [rd["hold_days"] for rd in grp if rd["hold_days"] is not None]
        st["avg_hold_days"] = round(sum(holds) / len(holds), 2) if holds else None
        by_signal[cat] = st
    tot_pnl = sum(v["total_pnl"] for v in by_signal.values())
    for v in by_signal.values():
        v["pnl_share"] = round(v["total_pnl"] / tot_pnl, 4) if tot_pnl else None

    # --- 2. 信号 × 窗口交叉 ---
    cross = []
    for cat in sorted(by_signal):
        for wi in range(len(WINDOWS)):
            grp = [rd for rd in rounds if rd["cat"] == cat and rd["widx"] == wi]
            if not grp:
                continue
            st = agg_stats(grp)
            st["cat"], st["window"] = cat, WINDOW_TAGS[wi]
            cross.append(st)

    # --- 3. 持仓天数分桶 ---
    buckets = {}
    for b in ["1天", "2天", "3-5天", "5天+"]:
        grp = [rd for rd in rounds if bucket_of(rd["hold_days"]) == b]
        if not grp:
            continue
        st = agg_stats(grp)
        holds = [rd["hold_days"] for rd in grp]
        st["avg_hold_days"] = round(sum(holds) / len(holds), 2)
        buckets[b] = st

    # --- 4. 卖出规则归因（含卖飞成本） ---
    sells = [t for t in all_trades if t["side"] == "sell"]
    by_exit = {}
    for cat in sorted(set(classify_sell(t["reason"]) for t in sells)):
        grp = [t for t in sells if classify_sell(t["reason"]) == cat]
        fw = [t["fwd5"] for t in grp if t.get("fwd5") is not None]
        pnls = [t.get("pnl") or 0.0 for t in grp]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gw, gl = sum(wins), -sum(losses)
        st = {
            "count": len(grp),
            "avg_realized_pnl": round(sum(pnls) / len(grp), 2) if grp else None,
            "total_pnl": round(sum(pnls), 2),
            "win_rate": round(len(wins) / len(grp), 4) if grp else None,
            "profit_factor": round(gw / gl, 3) if gl > 0 else (99.0 if gw > 0 else None),
            # ★ 卖飞成本：卖出后 5 个交易日收盘价后续涨跌幅均值（正=卖早了）
            "fly5_mean": round(sum(fw) / len(fw), 6) if fw else None,
            "fly5_median": round(sorted(fw)[len(fw) // 2], 6) if fw else None,
            "fly5_valid_n": len(fw),
            "fly5_excluded": len(grp) - len(fw),
        }
        by_exit[cat] = st

    # --- 5. reason 样本枚举（映射表证据） ---
    sell_raw = Counter(t["reason"] for t in sells)
    tok_counter = Counter()
    tok_samples = defaultdict(list)
    for t in all_trades:
        if t["side"] != "buy":
            continue
        _, toks = parse_buy_reason(t["reason"])
        for tk in toks:
            c = norm_signal(tk)
            tok_counter[c] += 1
            if len(tok_samples[c]) < 3:
                tok_samples[c].append(t["reason"])
    unmapped = sorted(set(tok_counter) - KNOWN_SIGNALS)

    return {
        "rounds_count": len(rounds),
        "by_signal": by_signal,
        "cross_signal_window": cross,
        "holding_buckets": buckets,
        "by_exit": by_exit,
        "signal_token_counts": dict(tok_counter.most_common()),
        "signal_samples": dict((k, v) for k, v in tok_samples.items()),
        "sell_reason_counts": dict(sell_raw.most_common()),
        "unmapped_signals": unmapped,
    }, rounds


# ============ 模拟盘侧（audit.jsonl + account.json） ============
RE_BUY_MSG = re.compile(r"买入\s+(.+?)\((\d+)\)\s+([\d.]+)x(\d+)股\s*(.*)")
RE_SELL_MSG = re.compile(r"卖出\s+(.+?)\((\d+)\)\s+([\d.]+)x(\d+)股\s*(.*)")


def load_paper_trades():
    """audit.jsonl 的 BUY/SELL 事件 + account.json 结构化 trades 合并去重。
    返回 (trades, notes)：notes 记录数据缺口，如实写进报告。"""
    notes = []
    seen = set()
    trades = []
    if os.path.exists(AUDIT_JSONL):
        with open(AUDIT_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                lvl = rec.get("level")
                if lvl not in ("BUY", "SELL"):
                    continue
                side = "buy" if lvl == "BUY" else "sell"
                code = rec.get("code")
                price = rec.get("price")
                qty = rec.get("qty")
                reason = ""
                msg = rec.get("msg") or ""
                rx = RE_BUY_MSG if side == "buy" else RE_SELL_MSG
                m = rx.search(msg)
                if m:
                    name, code2, price2, qty2, tail = m.groups()
                    code = code or code2
                    price = price if price else float(price2)
                    qty = qty if qty else int(qty2)
                    reason = tail.strip()
                    name = name.strip()
                else:
                    name = rec.get("name") or ""
                    reason = msg[:80]
                if not code or not price or not qty:
                    notes.append("跳过缺字段记录: %s %s" % (rec.get("t"), msg[:60]))
                    continue
                key = (rec.get("t"), side, code, price, qty)
                if key in seen:
                    continue
                seen.add(key)
                trades.append({
                    "time": rec.get("t", ""), "date": rec.get("t", "")[:10],
                    "code": str(code), "name": name, "side": side,
                    "price": float(price), "qty": int(qty),
                    "pnl": rec.get("pnl"), "reason": reason or rec.get("event", ""),
                    "source": "audit",
                })
    else:
        notes.append("data/audit/audit.jsonl 不存在")
    # account.json 结构化 trades 为准（补 pnl/fee/reason），覆盖合并
    if os.path.exists(ACCOUNT_JSON):
        try:
            with open(ACCOUNT_JSON, encoding="utf-8") as f:
                acct = json.load(f)
            for t in acct.get("trades", []):
                key = (t.get("time", "")[:19], t.get("side"), str(t.get("code")))
                hit = None
                for ex in trades:
                    if (ex["time"], ex["side"], ex["code"]) == key:
                        hit = ex
                        break
                if hit is not None:
                    # account.json 结构化字段回填（audit 的 trading_event 无 pnl/fee）
                    hit.update({"fee": t.get("fee"), "pnl": t.get("pnl"),
                                "reason": t.get("reason", "") or hit["reason"],
                                "source": "audit+account"})
                    continue
                trades.append({
                    "time": t.get("time", ""), "date": t.get("time", "")[:10],
                    "code": str(t.get("code")), "name": t.get("name", ""),
                    "side": t.get("side"), "price": t.get("price"),
                    "qty": t.get("qty"), "fee": t.get("fee"),
                    "pnl": t.get("pnl"), "reason": t.get("reason", ""),
                    "source": "account",
                })
        except Exception as e:
            notes.append("account.json 解析失败: %s" % e)
    trades.sort(key=lambda x: x["time"])
    return trades, notes


def paper_forward5(trades):
    """模拟盘卖出后的 5 日后续涨跌（直接读 sqlite kline 表，period='day'）。
    数据不足（卖出日距库内最后一天 <5 个交易日）如实置 None。"""
    from app import config as C
    db = C.DB_FILE
    cache = {}
    out = 0
    for t in trades:
        if t["side"] != "sell":
            continue
        code = t["code"]
        if code not in cache:
            try:
                conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
                rows = conn.execute(
                    "SELECT date, close FROM kline WHERE code=? AND period='day' "
                    "ORDER BY date", (code,)).fetchall()
                conn.close()
                cache[code] = rows
            except Exception:
                cache[code] = []
        rows = cache[code]
        dates = [r[0] for r in rows]
        t["fwd5"] = None
        if t["date"] in dates:
            i = dates.index(t["date"])
            if i + 5 < len(rows) and rows[i][1]:
                t["fwd5"] = round(rows[i + 5][1] / rows[i][1] - 1.0, 6)
        if t["fwd5"] is None:
            out += 1
    return out


def analyze_paper():
    trades, notes = load_paper_trades()
    res = {"trade_total": len(trades),
           "buys": sum(1 for t in trades if t["side"] == "buy"),
           "sells": sum(1 for t in trades if t["side"] == "sell"),
           "notes": notes}
    sells = [t for t in trades if t["side"] == "sell"]
    if sells:
        excluded = paper_forward5(trades)
        res["fly5_excluded_no_future_data"] = excluded
        by_exit = {}
        for cat in sorted(set(classify_sell(t["reason"]) for t in sells)):
            grp = [t for t in sells if classify_sell(t["reason"]) == cat]
            pnls = [(t.get("pnl") or 0.0) for t in grp]
            fw = [t["fwd5"] for t in grp if t.get("fwd5") is not None]
            by_exit[cat] = {
                "count": len(grp),
                "avg_realized_pnl": round(sum(pnls) / len(grp), 2),
                "total_pnl": round(sum(pnls), 2),
                "fly5_mean": round(sum(fw) / len(fw), 6) if fw else None,
                "fly5_valid_n": len(fw),
            }
        res["by_exit"] = by_exit
    buys = [t for t in trades if t["side"] == "buy"]
    if buys:
        tok_counter = Counter()
        for t in buys:
            tok_counter[primary_category(t["reason"])] += 1
        res["buy_categories"] = dict(tok_counter.most_common())
        res["buy_reason_samples"] = sorted(set(t["reason"] for t in buys))[:20]
    # 已实现配对（有 pnl 的卖出直接呈现；无 pnl 的注明缺口）
    no_pnl = [t for t in sells if t.get("pnl") is None]
    if no_pnl:
        res["sells_without_pnl"] = [
            "%s %s %s" % (t["time"], t["code"], t["reason"][:30]) for t in no_pnl]
    return res


# ============ 主流程 ============
_TASKS = [(s, w) for s in ("score", "board") for w in range(len(WINDOWS))]


def _run_one_task(task):
    """多进程 worker 入口（Windows spawn：子进程重新 import 本模块，取模块级常量）"""
    strategy, widx = task
    codes, names = build_pool()
    return run_one(codes, names, strategy, widx)


def _run_all_parallel():
    """8 个 (策略×窗口) 回测并行执行（进程池，公平抢占 CPU 份额）；
    每完成一个落盘一次。进程池不可用时退回顺序执行。"""
    from concurrent.futures import ProcessPoolExecutor, as_completed
    runs = []
    t0 = time.time()
    try:
        ex = ProcessPoolExecutor(max_workers=min(6, len(_TASKS)))
        futs = {ex.submit(_run_one_task, t): t for t in _TASKS}
    except Exception as e:
        print("进程池创建失败(%s)，退回顺序执行" % e, flush=True)
        ex = None
    if ex is not None:
        try:
            for fut in as_completed(futs):
                try:
                    row = fut.result()
                except Exception as e:
                    t = futs[fut]
                    print("任务 %s w%d 异常: %s" % (t[0], t[1], e), flush=True)
                    continue
                runs.append(row)
                _dump_runs(runs)
                print("  进度 %d/8 完成（%s %s），累计 %.0fs" % (
                    len(runs), row.get("strategy"), WINDOW_TAGS[row.get("widx", 0)],
                    time.time() - t0), flush=True)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        # 补齐失败/缺漏的任务（顺序重试一次）
        done = {(r["strategy"], r["widx"]) for r in runs}
        for t in _TASKS:
            if t not in done:
                print("补跑失败任务 %s w%d" % (t[0], t[1]), flush=True)
                runs.append(_run_one_task(t))
                _dump_runs(runs)
        order = {tt: i for i, tt in enumerate(_TASKS)}
        return sorted(runs, key=lambda r: order.get((r["strategy"], r.get("widx")), 99))
    for t in _TASKS:
        runs.append(_run_one_task(t))
        _dump_runs(runs)
    return runs


def _dump_runs(runs):
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "30",
        "recipe": {
            "pool": "data/bt_pool.json 前%d只, names=代码兜底" % POOL_SIZE,
            "windows": WINDOWS,
            "board_params": BOARD_PARAMS,
            "score_params": "config 默认",
            "gates_off": BASE_PARAMS,
            "quotes_patch": "fetch_quotes置空(成交额×20近似)",
            "seed": 42,
            "source": "Backtest.trades 属性全量(非result截断版)",
            "parallel": "ProcessPoolExecutor(6)，每任务独立进程独立加载数据",
        },
        "runs": runs,
    }
    with open(TRADES_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze-only", action="store_true",
                    help="复用已落盘 backtest_trades.json，只重跑分析")
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(ATTR_DIR, exist_ok=True)

    if a.analyze_only:
        with open(TRADES_JSON, encoding="utf-8") as f:
            payload = json.load(f)
        runs = payload["runs"]
        print("analyze-only: 复用 %d 个已完成回测" % len(runs), flush=True)
    else:
        codes_num = len(json.load(open(
            os.path.join(BASE, "data", "bt_pool.json"), encoding="utf-8"))["codes"])
        print("池: %d 只（bt_pool.json 前 %d）；四窗口 × (score+board)，闸门全关" %
              (min(codes_num, POOL_SIZE), POOL_SIZE), flush=True)
        runs = _run_all_parallel()

    res, rounds = analyze(runs)
    res_paper = analyze_paper()

    by_signal_doc = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "30",
        "method": "FIFO 配对买卖 → 每笔回合归因到买入 reason 的主动信号类别；"
                  "部分卖出按数量比例分摊 pnl；ret=pnl/买入金额(含费前金额)",
        "backtest_summary": [
            {k: r[k] for k in ("strategy", "widx", "window", "total_return",
                               "max_drawdown", "win_rate", "trade_count")
             if k in r} for r in runs],
        **res,
        "paper": res_paper,
    }
    with open(SIGNAL_JSON, "w", encoding="utf-8") as f:
        json.dump(by_signal_doc, f, ensure_ascii=False, indent=1)

    by_exit_doc = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": "30",
        "method": "按卖出 reason 分类；fly5=卖出日收盘→后第5交易日收盘涨跌幅"
                  "(正均值=存在卖飞迹象)；期末清仓等窗口尾部卖出无未来K线则剔除计数",
        "by_exit_backtest": res["by_exit"],
        "by_round_exit_cross": {},
        "paper_by_exit": res_paper.get("by_exit", {}),
        "paper_notes": res_paper.get("notes", []),
    }
    # 附：回合维度 退出类别 × 盈亏（哪个规则在兑现利润/放大亏损）
    cross_exit = {}
    all_rounds_cross = defaultdict(list)
    for rd in rounds:
        all_rounds_cross[rd["exit_cat"]].append(rd)
    for cat, grp in sorted(all_rounds_cross.items()):
        cross_exit[cat] = agg_stats(grp)
    by_exit_doc["by_round_exit_cross"] = cross_exit
    with open(EXIT_JSON, "w", encoding="utf-8") as f:
        json.dump(by_exit_doc, f, ensure_ascii=False, indent=1)

    # 控制台摘要
    print("\n===== 买入信号归因（回合数/胜率/平均收益/总盈亏） =====", flush=True)
    for cat, st in sorted(res["by_signal"].items(),
                          key=lambda kv: kv[1]["total_pnl"], reverse=True):
        print("  %-16s n=%-5d 胜率=%-6s 平均收益=%-9s 总盈亏=%+.0f" % (
            cat, st["count"], st["win_rate"], st["avg_ret"], st["total_pnl"]))
    print("===== 卖出规则归因（次数/平均兑现盈亏/卖飞成本5日均値） =====", flush=True)
    for cat, st in sorted(res["by_exit"].items(),
                          key=lambda kv: kv[1]["total_pnl"]):
        print("  %-16s n=%-5d 平均兑现=%-10s 卖飞5日=%-9s (有效%d/剔除%d)" % (
            cat, st["count"], st["avg_realized_pnl"], st["fly5_mean"],
            st["fly5_valid_n"], st["fly5_excluded"]))
    print("===== 持仓天数分桶 =====", flush=True)
    for b in ["1天", "2天", "3-5天", "5天+"]:
        st = res["holding_buckets"].get(b)
        if st:
            print("  %-6s n=%-5d 胜率=%-6s 平均收益=%-9s 总盈亏=%+.0f" % (
                b, st["count"], st["win_rate"], st["avg_ret"], st["total_pnl"]))
    if res["unmapped_signals"]:
        print("★ 未映射信号类别（需人工补充映射表）:", res["unmapped_signals"], flush=True)
    print("模拟盘侧: %d 笔成交（买%d/卖%d）; notes=%s" % (
        res_paper["trade_total"], res_paper["buys"], res_paper["sells"],
        res_paper["notes"] or "无"), flush=True)
    print("已写出:\n  %s\n  %s\n  %s\n总耗时 %.0fs" % (
        TRADES_JSON, SIGNAL_JSON, EXIT_JSON, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
