# -*- coding: utf-8 -*-
"""模拟盘账户：现金/持仓/交易流水（JSON 持久化）
首次运行自动迁移 v2.0 旧数据（paper_trades.json / watchlist.json / notify.json）
"""
import copy
import json
import os
import threading
import time

from . import config as C
from . import datafeed as df
from . import engine as eng

_lock = threading.Lock()
_acct_lock = threading.RLock()   # ★ P1-1：账户事务锁（load→改→save 全程持锁，防并发丢更新）

# ★SPEED-1（2026-09-16，验收方）：账户读缓存——mtime 感知 + deepcopy 出参。
# 效益：fast_watch(2s)/主循环(5s)/哨兵/面板每次 load_account 不再冷读磁盘+
# 重解析整个 trades 台账；mtime 变化即失效（外部改文件同样安全）。
_ACCT_MEM = {"path": None, "mtime": None, "obj": None}


def _acct_cache_put(path, mtime, obj):
    _ACCT_MEM["path"] = path
    _ACCT_MEM["mtime"] = mtime
    _ACCT_MEM["obj"] = obj


def _acct_cache_get(mtime):
    c = _ACCT_MEM
    if c["obj"] is not None and mtime is not None and c["mtime"] == mtime:
        return copy.deepcopy(c["obj"])
    return None

DEFAULT_ACCOUNT = {
    "cash": C.INITIAL_CAPITAL,
    "positions": {},   # code -> {name, qty, entry_price, entry_date, peak, days, board_trade}
    "trades": [],      # [{time, code, name, side, price, qty, fee, pnl, reason}]
}


def load_account():
    # ★SPEED-1：mtime 缓存命中路径（禁止跳过 deepcopy——调用方会原地改 dict）
    _p = C.ACCOUNT_FILE
    try:
        _mt = os.path.getmtime(_p)
    except OSError:
        _mt = None
    hit = _acct_cache_get(_mt)
    if hit is not None:
        return hit
    with _lock:
        if os.path.exists(_p):
            try:
                with open(_p, "r", encoding="utf-8") as f:
                    acct = json.load(f)
                acct.setdefault("positions", {})
                acct.setdefault("trades", [])
                acct.setdefault("cash", C.INITIAL_CAPITAL)
                try:
                    _acct_cache_put(_p, _mt, acct)
                except Exception:
                    pass
                return acct
            except Exception:
                _acct_cache_put(_p, None, None)
    # 迁移必须在锁外执行（内部会再次加锁）
    return migrate_legacy()


def save_account(acct):
    # ★ P1-1 修复：原子写（临时文件 + os.replace，崩溃中断不损坏账户文件）
    #        + _acct_lock（与 transaction 共用，保证任意时刻只有一个写入者）
    with _acct_lock:
        tmp = C.ACCOUNT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(acct, f, ensure_ascii=False, indent=1)
        os.replace(tmp, C.ACCOUNT_FILE)
        try:
            _acct_cache_put(C.ACCOUNT_FILE, os.path.getmtime(C.ACCOUNT_FILE),
                            copy.deepcopy(acct))   # ★SPEED-1：写后同步缓存
        except Exception:
            _acct_cache_put(C.ACCOUNT_FILE, None, None)


class transaction:
    """★ P1-1 修复：账户事务上下文 —— 锁住整个 load→修改→save 周期，
    防止交易线程（_exits_round/_board_round）与 HTTP 线程（手动买卖）并发丢更新。
    用法：with st.transaction() as acct: acct["cash"] -= 100;  # 自动 save
    """
    def __init__(self, auto_save=True):
        self.auto_save = auto_save

    def __enter__(self):
        _acct_lock.acquire()
        self.acct = load_account()
        return self.acct

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and self.auto_save:
                save_account(self.acct)
        finally:
            _acct_lock.release()
        return False


def reset_account():
    acct = DEFAULT_ACCOUNT.copy()
    acct["positions"] = {}
    acct["trades"] = []
    acct["cash"] = C.INITIAL_CAPITAL
    save_account(acct)
    return acct


def migrate_legacy():
    """从 v2.0 的 paper_trades.json 迁移账户数据"""
    legacy = os.path.join(C.BASE_DIR, "legacy", "paper_trades.json")
    acct = DEFAULT_ACCOUNT.copy()
    acct["positions"] = {}
    acct["trades"] = []
    if os.path.exists(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                old = json.load(f)
            acct["cash"] = float(old.get("cash", C.INITIAL_CAPITAL))
            for code, p in (old.get("positions", {}) or {}).items():
                acct["positions"][code] = {
                    "name": p.get("name", code), "qty": int(p.get("qty", 0)),
                    "entry_price": float(p.get("entry_price", 0)),
                    "entry_date": p.get("entry_date", ""),
                    "peak": float(p.get("peak", 0) or p.get("entry_price", 0)),
                    "days": int(p.get("days", 0)),
                    "board_trade": bool(p.get("board_trade", False)),
                }
            for t in (old.get("trades", []) or []):
                if not isinstance(t, dict) or "side" not in t:
                    continue
                acct["trades"].append({
                    "time": t.get("time", ""), "code": t.get("code", ""),
                    "name": t.get("name", ""), "side": t.get("side", ""),
                    "price": float(t.get("price", 0)), "qty": int(t.get("qty", 0)),
                    "fee": 0.0, "pnl": t.get("pnl"),
                    "reason": t.get("reason", "历史导入"),
                })
        except Exception:
            pass
    save_account(acct)
    return acct


# ============ 自选股 ============
_WLMEM = None
_WLM_MTIME = None


def load_watchlist():
    """★SPEED-2：自选列表 mtime 缓存（快环每 2s 调用；保存者/外部编辑均失效）。"""
    global _WLM_MTIME, _WLM_MEM
    path = os.path.join(C.DATA_DIR, "watchlist.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = None
    if mt is not None and mt == _WLM_MTIME and _WLM_MEM is not None:
        return copy.deepcopy(_WLM_MEM)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("watchlist", [])
            _WLM_MEM, _WLM_MTIME = d, mt
            return copy.deepcopy(d)
        except Exception:
            pass
    # 迁移旧自选
    legacy = os.path.join(C.BASE_DIR, "legacy", "watchlist.json")
    d = {"watchlist": []}
    if os.path.exists(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("watchlist", [])
        except Exception:
            pass
    save_watchlist(d)
    return d


def save_watchlist(data):
    with open(os.path.join(C.DATA_DIR, "watchlist.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    global _WLM_MTIME, _WLM_MEM
    try:
        _WLM_MEM = copy.deepcopy(data)   # ★SPEED-2：写后同布缓存
        _WLM_MTIME = os.path.getmtime(os.path.join(C.DATA_DIR, "watchlist.json"))
    except Exception:
        _WLM_MEM, _WLM_MTIME = None, None


def add_watch(code, name="", group="默认"):
    """加入自选（v3.9：支持分组）。group 为空则归入'默认'"""
    d = load_watchlist()
    if any(w["code"] == code for w in d["watchlist"]):
        # 已存在：仅更新分组
        for w in d["watchlist"]:
            if w["code"] == code:
                if group:
                    w["group"] = group
                    save_watchlist(d)
                    return True, f"已更新分组:{group}"
                return False, "已在自选"
    import datetime
    d["watchlist"].append({"code": code, "name": name,
                           "added": datetime.date.today().strftime("%Y-%m-%d"),
                           "group": group or "默认"})
    save_watchlist(d)
    return True, "已添加"


def watch_groups():
    """自选分组列表（含每组数量）"""
    d = load_watchlist()
    groups = {}
    for w in d["watchlist"]:
        g = w.get("group") or "默认"
        groups[g] = groups.get(g, 0) + 1
    return groups


def set_watch_group(code, group):
    """移动自选股到指定分组"""
    d = load_watchlist()
    for w in d["watchlist"]:
        if w["code"] == code:
            w["group"] = group or "默认"
            save_watchlist(d)
            return True
    return False


def remove_watch(code):
    d = load_watchlist()
    d["watchlist"] = [w for w in d["watchlist"] if w["code"] != code]
    save_watchlist(d)


# ============ 手动交易（模拟盘，真实规则） ============
def manual_buy(code, qty, price=None, name=""):
    """手动买入（限价 price=None 则按现价）。
    v3.9：支持分批加仓（已持有则摊薄成本，保留 entry_date 为首次建仓日）。
    ★ P1-1：事务锁包住读改写（防与交易线程并发丢更新）；行情拉取在锁外。
    """
    q = df.fetch_quotes([code])
    quote = q.get(code, {})
    px = price or quote.get("price", 0)
    if not px:
        return False, "无行情"
    lu, ld = eng.limit_prices(code, quote.get("yest_close", px) or px,
                              quote.get("name", ""))   # ★ 4.3 ST识别
    if px >= lu * 0.999:
        return False, f"涨停价不可买入（{lu:.2f}）"
    if px <= ld * 1.001:
        return False, f"跌停价不可买入（{ld:.2f}）"
    # ★ P2-3：限价单偏离现价保护（防"自我作弊"式超低/超高报价）
    if price:
        mkt = quote.get("price", 0) or 0
        if mkt > 0:
            dev = abs(px - mkt) / mkt
            if dev > 0.10:   # 偏离现价超 10% 拒绝（防刷单/误操作）
                return False, f"报价偏离现价{dev*100:.0f}%（限±10%）"
    qty = int(qty / 100) * 100
    if qty < 100:
        return False, "最少100股"
    with transaction() as acct:
        cost = px * qty
        fee = eng.buy_fee(cost)
        if cost + fee > acct["cash"]:
            return False, "现金不足"
        # ★ v3.9：分批加仓 → 摊薄成本（首次建仓记录 entry_date）
        if code in acct["positions"]:
            pos = acct["positions"][code]
            old_qty, old_cost = pos["qty"], pos["entry_price"] * pos["qty"]
            new_qty = old_qty + qty
            avg = (old_cost + cost) / new_qty if new_qty > 0 else px
            pos["qty"] = new_qty
            pos["entry_price"] = round(avg, 3)
            pos["peak"] = max(pos.get("peak", px), px)
            acct["cash"] -= cost + fee
            msg = f"加仓成功：{name or quote.get('name', code)}({code}) {px:.2f}x{qty}股，成本摊薄至{avg:.3f}"
        else:
            acct["cash"] -= cost + fee
            acct["positions"][code] = {
                "name": name or quote.get("name", code), "qty": qty,
                "entry_price": px, "entry_date": df._today_str(),
                "peak": px, "days": 0,
            }
            msg = f"买入成功：{name or quote.get('name', code)}({code}) {px:.2f}x{qty}股"
        import datetime
        acct["trades"].append({
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": name or quote.get("name", code), "side": "buy",
            "price": px, "qty": qty, "fee": round(fee, 2), "pnl": None,
            "reason": "手动买入",
        })
    try:
        from . import audit
        audit.record("order", "manual_buy", level="BUY", code=code,
                     name=name or quote.get("name", code), price=px, qty=qty,
                     fee=round(fee, 2))
    except Exception:
        pass
    return True, msg


def manual_sell(code, qty=None, price=None):
    # ★ P1-1：事务锁包住读改写（防并发丢更新）；行情拉取在锁外
    q = df.fetch_quotes([code])
    quote = q.get(code, {})
    px = price or quote.get("price", 0)
    if not px:
        return False, "无行情"
    with transaction() as acct:
        pos = acct["positions"].get(code)
        if not pos:
            return False, "无此持仓"
        # T+1 检查
        entry_date = pos.get("entry_date", "")
        if entry_date >= df._today_str():
            return False, "T+1：当日买入不可卖出"
        qty = qty or pos["qty"]
        qty = min(int(qty / 100) * 100 if qty else pos["qty"], pos["qty"])
        if qty < 100:
            return False, "最少100股"
        lu, ld = eng.limit_prices(code, quote.get("yest_close", px) or px,
                                  quote.get("name", ""))   # ★ 4.3 ST识别
        if px <= ld * 1.001:
            return False, f"跌停价不可卖出（{ld:.2f}）"
        amount = px * qty
        fee = eng.sell_fee(amount)
        pnl = amount - fee - pos["entry_price"] * qty
        acct["cash"] += amount - fee
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            del acct["positions"][code]
        import datetime
        acct["trades"].append({
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": pos["name"], "side": "sell",
            "price": px, "qty": qty, "fee": round(fee, 2),
            "pnl": round(pnl, 2), "reason": "手动卖出",
        })
    try:
        from . import audit
        audit.record("position", "manual_sell", level="SELL", code=code,
                     name=pos["name"], price=px, qty=qty,
                     pnl=round(pnl, 2), fee=round(fee, 2))
    except Exception:
        pass
    return True, "卖出成功"


# ============ 微信通知配置 ============
def load_notify():
    path = os.path.join(C.DATA_DIR, "notify.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    legacy = os.path.join(C.BASE_DIR, "legacy", "notify.json")
    cfg = {"sendkey": "", "enabled": False}
    if os.path.exists(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    save_notify(cfg)
    return cfg


# ============ 佣金档位配置（4.3） ============
def load_commission_tier():
    path = os.path.join(C.DATA_DIR, "commission.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f).get("tier", "万2.5")
        except Exception:
            pass
    return "万2.5"


def save_commission_tier(tier):
    from . import config as C
    if tier not in C.COMMISSION_TIERS:
        return False
    with open(os.path.join(C.DATA_DIR, "commission.json"), "w", encoding="utf-8") as f:
        json.dump({"tier": tier}, f, ensure_ascii=False)
    # 更新运行时配置
    C.COMMISSION_TIER = tier
    C.COMMISSION_RATE = C.COMMISSION_TIERS[tier]
    return True


def save_notify(cfg):
    with open(os.path.join(C.DATA_DIR, "notify.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def wechat_notify(title, message):
    """Server酱微信推送（需先在设置页配置 SendKey）。成功返回 True"""
    cfg = load_notify()
    if not cfg.get("enabled") or not cfg.get("sendkey"):
        return False
    try:
        import urllib.parse
        import urllib.request
        url = f"https://sctapi.ftqq.com/{cfg['sendkey']}.send"
        data = urllib.parse.urlencode(
            {"title": title, "desp": message}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception:
        return False


# ============ LLM 复盘配置（v3.5） ============
def load_llm_config():
    path = os.path.join(C.DATA_DIR, "llm_config.json")
    cfg = {"ollama_url": "http://127.0.0.1:11434", "ollama_model": "",
           "api_key": "", "api_base": "https://api.deepseek.com/v1", "model": ""}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_llm_config(cfg):
    with open(os.path.join(C.DATA_DIR, "llm_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


# ============ ★ 4.5：价格提醒（条件单） ============
_ALERT_FILE = os.path.join(C.DATA_DIR, "price_alerts.json")


def load_price_alerts():
    """价格提醒列表。每条 {id, code, name, direction(up/down), price, enabled, triggered, note}"""
    if os.path.exists(_ALERT_FILE):
        try:
            with _lock:
                with open(_ALERT_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
    return []


def save_price_alerts(alerts):
    with _lock:
        with open(_ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump(alerts, f, ensure_ascii=False, indent=1)


def add_price_alert(code, name, direction, price, note=""):
    """新增价格提醒。direction: up(涨到) / down(跌到)。price: 触发价"""
    alerts = load_price_alerts()
    # 唯一 ID：时间戳 + 随机后缀
    import random
    a = {
        "id": str(int(time.time() * 1000)) + str(random.randint(10, 99)),
        "code": code, "name": name,
        "direction": direction, "price": float(price),
        "enabled": True, "triggered": False, "note": note or "",
    }
    alerts.append(a)
    save_price_alerts(alerts)
    return a


def remove_price_alert(aid):
    alerts = load_price_alerts()
    new_alerts = [a for a in alerts if a.get("id") != aid]
    save_price_alerts(new_alerts)
    return True


def check_price_alerts(quotes=None):
    """检查所有启用的价格提醒（交易时段调用）。
    quotes: {code: quote}，缺省时自动拉取。触发后标记 triggered 并返回触发列表。
    """
    alerts = load_price_alerts()
    if not alerts:
        return []
    active = [a for a in alerts if a.get("enabled") and not a.get("triggered")]
    if not active:
        return []
    codes = list({a["code"] for a in active})
    if quotes is None:
        try:
            quotes = df.fetch_quotes(codes)
        except Exception:
            quotes = {}
    triggered = []
    changed = False
    for a in active:
        q = quotes.get(a["code"]) or {}
        px = q.get("price", 0) or 0
        if px <= 0:
            continue
        hit = False
        if a["direction"] == "up" and px >= a["price"]:
            hit = True
        elif a["direction"] == "down" and px <= a["price"]:
            hit = True
        if hit:
            a["triggered"] = True
            a["trigger_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            triggered.append(a)
            changed = True
    if changed:
        save_price_alerts(alerts)
    return triggered
