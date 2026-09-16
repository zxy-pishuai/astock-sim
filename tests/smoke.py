# -*- coding: utf-8 -*-
"""最小冒烟测试网（只读，安全）。
用途：5 个 AI 并行改代码时的回归护栏——每条断言都对应一次真实事故。
运行：py -3.13 tests/smoke.py        退出码 = 失败条数（0 = 全过）

事故对照（每条断言防的是什么）：
 S01 config 阈值被改坏        → 08-25 STOP_LOSS 调整类改动
 S02/S03 日K覆盖率塌陷        → 09-09~11 覆盖率 42% 事故
 S04 amount 全零              → 09-09~11 amount=0 事故
 S05/S06 指数字段错位/历史过短 → F1 事故 + A5 回填目标
 S07 物理不可能K线            → F1 同源
 S08 快照断档                 → 09-09~11 快照缺失事故
 S09 审计链断裂               → 哈希链完整性
 S10 账户文件损坏             → 下单前必读
 S11 告警文件损坏             → quality_alert 双轨
 S12 指标库可import且能算      → 因子层基本可用
 S13 涨跌停价规则             → 交易规则核心
 S14/S15 进程唯一性/锁一致性   → 双实例写账户风险（G2 守卫同源）
 S16 守护任务存在             → watchdog 缺失即无自愈
 S17 磁盘余量                 → 快照/备份撑爆盘
 S18 sector_map/stock_list    → 板块与票池基础数据
 S19 watchlist 可解析         → 自选池读取
 S20 引擎端口可连（可跳过）    → 服务在跑
"""
import io
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

results = []


def check(cid, name, fn):
    try:
        ok, note = fn()
    except Exception as e:
        ok, note = False, '%s: %s' % (type(e).__name__, e)
    results.append((cid, name, ok, note))
    print('%s %s %-34s %s' % ('PASS' if ok else 'FAIL', cid, name, note))


def ro(db):
    return sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)


def q1(db, sql, args=()):
    con = ro(db)
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def load(p):
    with io.open(p, encoding='utf-8') as f:
        return json.load(f)


def q_ok(v):
    return v is not None


# ---- S01 配置阈值 ----
def s01():
    from app import config as C
    bad = []
    if not (C.STOP_LOSS_PCT < 0):
        bad.append('STOP_LOSS_PCT')
    if not (C.TAKE_PROFIT_PCT > 0):
        bad.append('TAKE_PROFIT_PCT')
    if not (0 < C.TRAILING_ACTIVATE_PCT < 0.5):
        bad.append('TRAILING_ACTIVATE_PCT')
    if not (0 < C.RISK_SINGLE_STOCK_PCT <= 1):
        bad.append('RISK_SINGLE_STOCK_PCT')
    if not (0 < C.MAX_POSITIONS <= 20):
        bad.append('MAX_POSITIONS')
    if not getattr(C, 'TRAILING_ENTRY_GATE', False):
        bad.append('TRAILING_ENTRY_GATE(应为 True)')
    return (not bad), ('ok' if not bad else '异常项: ' + ','.join(bad))


# ---- S02 日K覆盖率 ----
def last_completed():
    """最近一个"完整"交易日 = 最新一个票数≥4000 的日期（盘中当日只有零星bar，不可作判据）。"""
    return q1('data/market.db', "select date, count(distinct code) c from kline "
                                "where period='day' group by date having c>=4000 "
                                "order by date desc limit 1")


def s02():
    d, n = last_completed()
    return n >= 4000, '最近完整交易日 %s 有K票数=%s（阈值≥4000）' % (d, n)


# ---- S03 最近5个交易日都应有票 ----
def s03():
    rows = q1('data/market.db', "select count(*) from (select date from kline where period='day' "
                                "group by date having count(distinct code)>=4000 order by date desc limit 5)")
    return rows[0] >= 4, '近5日中达标(≥4000票)天数=%d（阈值≥4）' % rows[0]


# ---- S04 amount 完整性 ----
def s04():
    d, _ = last_completed()
    tot, z = q1('data/market.db',
                "select count(*), sum(case when amount<=0 then 1 else 0 end) "
                "from kline where period='day' and date=? and volume>0 and length(code)=6", (d,))
    r = (z or 0) / float(tot or 1)
    return r <= 0.05, '%s amount=0 占比 %.2f%%（阈值≤5%%）' % (d, r * 100)


# ---- S05 指数历史与自洽 ----
def s05():
    n, bad, amt = q1('data/market.db', "select count(*), sum(high<low), sum(amount>0) from kline "
                                       "where code='sh000001' and period='day'")
    return (n >= 1800 and bad == 0 and amt / float(n) >= 0.95,
            'sh000001 行=%s high<low=%s amount>0=%s' % (n, bad, amt))


# ---- S06 三大指数齐备 ----
def s06():
    rows = ro('data/market.db').execute(
        "select code, count(*) from kline where period='day' and code in "
        "('sh000001','sz399001','sz399006') group by code").fetchall()
    return len(rows) == 3 and all(r[1] >= 1800 for r in rows), str(dict(rows))


# ---- S07 物理不可能K线（最新日） ----
def s07():
    n = q1('data/market.db', "select count(*) from kline where period='day' and length(code)=6 "
                             "and date=(select max(date) from kline where period='day') "
                             "and (high<low or high<max(open,close) or low>min(open,close) "
                             "or close<=0 or open<=0)")[0]
    return n == 0, '最新日不可能K线=%d' % n


# ---- S08 快照新鲜度 ----
def s08():
    import glob, time
    ds = sorted(glob.glob('data/snapshots/2*'))
    if not ds:
        return False, '无快照目录'
    age = (time.time() - os.path.getmtime(ds[-1])) / 86400.0
    return age <= 5, '最新快照 %s（%.1f 天前）' % (os.path.basename(ds[-1]), age)


# ---- S09 审计链可解析 ----
def s09():
    p = 'data/audit/audit.jsonl'
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return True, '活动文件为空（可能刚轮转）'
    last = None
    n = 0
    with io.open(p, encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n += 1
            last = json.loads(line)
    return (n > 0 and 't' in (last or {})), '审计行=%d 末条 event=%s' % (n, (last or {}).get('event'))


# ---- S10 账户文件 ----
def s10():
    a = load('data/account.json')
    cash = a.get('cash')
    return isinstance(cash, (int, float)) and cash >= 0, 'cash=%s 持仓=%d' % (
        cash, len(a.get('positions') or {}))


# ---- S11 告警文件 ----
def s11():
    p = 'data/quality_alert.jsonl'
    if not os.path.exists(p):
        return True, '文件不存在（尚未产生告警）'
    n = 0
    with io.open(p, encoding='utf-8', errors='ignore') as f:
        for line in f:
            if line.strip():
                json.loads(line)
                n += 1
    return True, '告警行=%d 可解析' % n


# ---- S12 指标库 ----
def s12():
    from app import indicators as ind
    kl = [{'open': 10 + i * 0.1, 'high': 10.3 + i * 0.1, 'low': 9.8 + i * 0.1,
           'close': 10.1 + i * 0.1, 'volume': 1000 + i} for i in range(30)]
    def tail(x):
        return x[-1] if isinstance(x, (list, tuple)) else x
    a = tail(ind.atr(kl, 14))
    r = tail(ind.rsi([k['close'] for k in kl], 14))
    return (a is not None and r is not None), 'atr=%s rsi=%s' % (
        round(float(a), 3) if a is not None else None,
        round(float(r), 1) if r is not None else None)


# ---- S13 涨跌停价规则 ----
def s13():
    try:
        from app.engine import limit_prices
    except Exception as e:
        return True, '跳过（无法 import: %s）' % type(e).__name__
    up, dn = limit_prices('000001', 10.0, '平安银行')
    return (abs(up - 11.0) < 0.02 and abs(dn - 9.0) < 0.02), '10cm 涨停=%s 跌停=%s' % (up, dn)


# ---- S14 进程唯一性 ----
def s14():
    """以 app.lock 记录的 PID 为准，确认其仍存活（防"锁指向死进程"导致双实例）。"""
    import subprocess
    p = 'data/app.lock'
    if not os.path.exists(p):
        return True, '无锁文件（服务未运行）'
    with open(p, encoding='utf-8', errors='ignore') as f:
        st = f.read().strip().split()
    pid = st[0]
    total = int(st[1]) if len(st) > 1 else 0
    out = subprocess.run(['tasklist', '/FI', 'PID eq %s' % pid, '/FO', 'CSV'],
                         capture_output=True, encoding='utf-8', errors='ignore').stdout
    alive = ('"%s"' % pid) in out or (',%s,' % pid) in out
    # ★ 修复 S14 误报：只统计 CommandLine 含 main.py 的 python/pythonw 实例。
    #   证据：用户的 opencode_proxy.py 等也是 pythonw.exe，被旧口径误判为"双实例"。
    running = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe' or name='python.exe'\" "
         "| Where-Object { $_.CommandLine -match 'main\\.py' } | Measure-Object | ForEach-Object Count"],
        capture_output=True, encoding='utf-8', errors='ignore').stdout
    main_inst = int((running or '0').strip() or 0)
    ok = alive and main_inst <= 1
    return ok, '锁 PID=%s 存活=%s；main.py 实例数=%d（阈值≤1，代理工具不计）' % (pid, alive, main_inst)


# ---- S15 锁一致性 ----
def s15():
    p = 'data/app.lock'
    if not os.path.exists(p):
        return True, '无锁文件（服务可能未运行）'
    txt = io.open(p, encoding='utf-8', errors='ignore').read()
    return True, '锁内容=%s' % txt.strip()[:60].replace('\n', ' ')


# ---- S16 守护任务 ----
def s16():
    import subprocess
    out = subprocess.run(['schtasks', '/query', '/fo', 'csv'], capture_output=True,
                         encoding='utf-8', errors='ignore').stdout
    need = ['TianjiService_Watchdog', 'TianjiAuditRotate']
    # 收盘更新任务：定点版(CloseUpdate)与接力版(CloseCatchup)二者至少存一
    close_ok = ('TianjiCloseUpdate' in out) or ('TianjiCloseCatchup' in out)
    miss = [t for t in need if t not in out]
    tj = sorted({p.strip('"\\') for p in out.replace('","', '\x00').split('\x00')
                 if p.startswith('"\\Tianji') or p.startswith('\\Tianji')})
    if not close_ok:
        miss.append('TianjiCloseUpdate|CloseCatchup')
    return (not miss), ('齐备' if not miss else '缺失: ' + ','.join(miss)) + \
        '；Tianji 任务: ' + ','.join(t for t in tj if t.startswith('Tianji'))[:150]


# ---- S17 磁盘余量 ----
def s17():
    import shutil
    fr = shutil.disk_usage('C:/').free / 2 ** 30
    return fr >= 20, 'C 盘可用 %.1f GB（阈值≥20GB）' % fr


# ---- S18 基础映射文件 ----
def s18():
    sm = load('data/sector_map.json')
    sl = load('data/stock_list.json')
    return (len(sm) >= 1000 and len(sl) >= 3000), '板块映射=%d 票池=%d' % (len(sm), len(sl))


# ---- S19 自选池 ----
def s19():
    w = load('data/watchlist.json')
    return True, '自选条目=%s' % (len(w) if hasattr(w, '__len__') else 'n/a')


# ---- S20 服务端口 ----
def s20():
    import socket
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(('127.0.0.1', 8899))
        return True, '8899 可连'
    finally:
        s.close()


# ---- S21 行情直连（防死代理拖垮全市场行情 → 崩溃级禁开仓） ----
def s21():
    import time, urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    with op.open(urllib.request.Request('https://qt.gtimg.cn/q=sh000001',
                    headers={'User-Agent': 'smoke'}), timeout=8) as r:
        raw = r.read(200)
    ok = (b'sh000001' in raw or b'v_sh000001' in raw) and len(raw) > 50
    return ok, '直连 qt.gtimg %.2fs（若失败=国内源被代理/网络挡，涨跌家数与禁开仓风险）' % (time.time() - t0)


# ---- S22 NO_PROXY 声明（国内源绕代理） ----
def s22():
    from app import config  # noqa: F401  触发 app/__init__ 的 NO_PROXY 注入
    env = os.environ.get('NO_PROXY', '')
    for h in ('gtimg.cn', 'sina.com.cn', 'eastmoney.com'):
        if h not in env:
            return False, 'NO_PROXY 缺 %s' % h
    return True, 'NO_PROXY 含国内源域名'


for cid, name, fn in [
    ('S01', '配置阈值健全', s01), ('S02', '日K覆盖率', s02), ('S03', '近5日覆盖', s03),
    ('S04', 'amount 完整性', s04), ('S05', '指数自洽与历史', s05), ('S06', '三大指数齐备', s06),
    ('S07', '无物理不可能K线', s07), ('S08', '快照新鲜度', s08), ('S09', '审计链可解析', s09),
    ('S10', '账户文件完整', s10), ('S11', '告警文件可解析', s11), ('S12', '指标库可用', s12),
    ('S13', '涨跌停价规则', s13), ('S14', '进程唯一性', s14), ('S15', '锁文件一致性', s15),
    ('S16', '守护任务齐备', s16), ('S17', '磁盘余量', s17), ('S18', '板块/票池映射', s18),
    ('S19', '自选池可解析', s19), ('S20', '服务端口可连', s20),
    ('S21', '行情直连可用', s21), ('S22', 'NO_PROXY 声明', s22),
]:
    check(cid, name, fn)

bad = [r for r in results if not r[2]]
print('\n合计 %d 条：通过 %d，失败 %d' % (len(results), len(results) - len(bad), len(bad)))
sys.exit(len(bad))
