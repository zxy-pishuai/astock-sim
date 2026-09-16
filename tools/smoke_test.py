# -*- coding: utf-8 -*-
"""冒烟测试（零依赖）—— 覆盖关键路径，抓"静默吞错"类 bug
用法：python tools/smoke_test.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILS = []


def check(name, fn):
    try:
        fn()
        print("PASS", name)
    except Exception as e:
        FAILS.append(name)
        print("FAIL", name, "→", e)


# 1. 涨跌停取整（7.85 → 8.64 边界）
def t_limit():
    from app import engine as eng
    lu, ld = eng.limit_prices("600000", 7.85, "测试")
    assert abs(lu - 8.64) < 1e-9, f"涨停价 {lu} != 8.64"
    assert abs(ld - 7.07) < 1e-9, f"跌停价 {ld} != 7.07"
check("涨跌停取整", t_limit)


# 2. T+1 卖出检查
def t_t1():
    from app import state as st
    # ★ 修复：用临时账户文件隔离（原实现直接覆盖真实 account.json，清空了实盘账户记录！）
    orig = st.C.ACCOUNT_FILE
    tmp = os.path.join(os.path.dirname(orig), "account_smoke_test.json")
    st.C.ACCOUNT_FILE = tmp
    try:
        st.save_account({"cash": 100000.0, "positions": {}, "trades": []})
        ok, msg = st.manual_sell("000001", 100)
        assert not ok, "无持仓卖出应拒绝"
    finally:
        st.C.ACCOUNT_FILE = orig
        if os.path.exists(tmp):
            os.remove(tmp)
check("T+1/无持仓卖出拒绝", t_t1)


# 3. 费率
def t_fee():
    from app import engine as eng
    buy = eng.buy_fee(10000)
    assert buy >= 5.0, f"佣金最低5元，实际{buy}"
    sell = eng.sell_fee(10000)
    assert sell > buy, "卖出含印花税应更高"
check("费率模型", t_fee)


# 4. 哈希链（跨日）
def t_chain():
    import json, tempfile, os
    import app.audit as audit
    orig = audit._AUDIT_FILE
    tmp = os.path.join(tempfile.gettempdir(), "smoke_audit.jsonl")
    audit._AUDIT_FILE = tmp
    if os.path.exists(tmp):
        os.remove(tmp)
    audit._prev_hash = ""
    audit._prev_date = ""
    audit._buf = []
    audit.record("test", "s1", level="INFO")
    audit.record("test", "s2", level="INFO")
    ok, checked, broken = audit.verify_chain()
    audit._AUDIT_FILE = orig
    os.remove(tmp)
    assert ok, f"哈希链校验失败 broken={broken}"
check("哈希链", t_chain)


# 5. 交易日历
def t_cal():
    from app import trading_calendar as tcal
    assert tcal.is_trading_day("2026-08-14"), "周五应交易"
    assert not tcal.is_trading_day("2026-08-15"), "周六应休市"
    assert not tcal.is_trading_day("2026-10-01"), "国庆应休市"
check("交易日历", t_cal)


# 6. score_stock 冒烟（P0-1 教训：传 code 不抛错）
def t_score():
    from app import datafeed as df
    from app import scoring as sc
    k = df.fetch_kline("600519", "day", 80)
    assert len(k) >= 60, "K线不足"
    s, sigs = sc.score_stock(k, code="600519")
    assert isinstance(s, (int, float)), "评分非数字"
check("score_stock 冒烟", t_score)


# 7. 情绪快照
def t_senti():
    from app import sentiment as senti
    s = senti.cached_sentiment(max_age=600)
    assert s.get("phase") in ("冰点", "发酵", "高潮", "退潮"), s.get("phase")
check("情绪快照", t_senti)


# 8. 账户事务锁
def t_txn():
    from app import state as st
    with st.transaction() as acct:
        before = acct["cash"]
        acct["cash"] = before  # 不变
    after = st.load_account()["cash"]
    assert abs(after - before) < 1e-6
check("账户事务锁", t_txn)


print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(0 if not FAILS else 1)
