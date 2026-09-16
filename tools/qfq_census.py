# -*- coding: utf-8 -*-
"""Y1 tools: qfq 复权口径全库普查（判型 v4）

判型模型（决策口径：std_qfq vs 非 std）：
  - fixed（progress 修复成功 / batchA applied）        -> std_qfq（conf 0.9）
  - 有 >阈值 除权型跳变（主板>11%、创/科>21%，排除前30日/停牌） -> reverse_amp（conf 0.8，抽检 6/6 + X3 4/4 支持）
  - 无跳变 & 上市<3 年                                   -> raw_eq（次新无除权三态等价，conf 0.75）
  - 无跳变 & 上市>=3 年                                  -> unknown_old（conf 0.3，抽检 2/2 倾向 reverse_amp，待因子确认）
注意：300628/600188 类"早期 reverse + 后期 raw 混合态"在本模型主标签=reverse_amp
     （与 X3 按后期段标 raw 的主标签不同，决策层面等价：均属非 std 需统一重算）。

用法：python tools/qfq_census.py --check [--gold]
  --check 全量判型 -> data/qfq_census_result.json + 控制台摘要
  --gold  仅金标准 9 票校验（决策口径）
只读 market.db + data/qfq_repair_progress.json；不写库、不改代码。
"""
import sqlite3, pandas as pd, numpy as np, json, sys, os, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_URI = 'file:data/market.db?mode=ro&immutable=1'
PROGRESS = os.path.join(ROOT, 'data', 'qfq_repair_progress.json')
OUT = os.path.join(ROOT, 'data', 'qfq_census_result.json')

GOLD = {"001400": "std_qfq", "001359": "std_qfq", "002831": "std_qfq",
        "300628": "raw", "600188": "raw",
        "600519": "reverse_amp", "600036": "reverse_amp",
        "601318": "reverse_amp", "000333": "reverse_amp"}
GOLD_DECISION = {"001400": "std_qfq", "001359": "std_qfq", "002831": "std_qfq",
                 "300628": "nonstd", "600188": "nonstd",
                 "600519": "nonstd", "600036": "nonstd",
                 "601318": "nonstd", "000333": "nonstd"}


def load_fixed_set():
    """progress.json: values == '修复成功' -> fixed std_qfq"""
    fixed = set()
    if os.path.exists(PROGRESS):
        try:
            p = json.load(open(PROGRESS, encoding='utf-8'))
        except Exception:
            p = {}
        for k, v in (p.items() if isinstance(p, dict) else []):
            if isinstance(v, str) and v == "修复成功":
                fixed.add(str(k))
    return fixed


def load_kline_features():
    conn = sqlite3.connect(DB_URI, uri=True, timeout=180)
    df = pd.read_sql("SELECT code, date, close FROM kline WHERE period='day'", conn)
    conn.close()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values(['code', 'date']).reset_index(drop=True)
    g = df.groupby('code', sort=False)
    df['prev'] = g['close'].shift(1)
    df['gap'] = (df['date'] - g['date'].shift(1)).dt.days
    df['chg'] = (df['close'] / df['prev'] - 1) * 100
    df['rn'] = g.cumcount()
    is300 = df['code'].str.startswith(('300', '688', '301'))
    thr = np.where(is300, 21.0, 11.0)
    df['bigjump'] = (df['chg'].abs() > thr) & (df['gap'] <= 20) & (df['rn'] >= 30)
    df['minmax'] = g['close'].transform(lambda x: x.max())
    df['cnt'] = g.cumcount()
    rows = []
    for code, sub in df.groupby('code'):
        n = len(sub)
        span_days = int((sub['date'].max() - sub['date'].min()).days)
        rows.append(dict(
            code=code, n=n, span_days=span_days,
            n_bigjump=int(sub['bigjump'].sum()),
            latest_close=float(sub['close'].iloc[-1]),
            max_close=float(sub['close'].max()),
            first_close=float(sub['close'].iloc[0]),
        ))
    return pd.DataFrame(rows)


def classify(feats, fixed):
    """returns (dialect, confidence, note)"""
    code = feats['code']
    span_years = feats['span_days'] / 365.25
    nj = feats['n_bigjump']
    if code in fixed:
        return "std_qfq", 0.9, "progress 修复成功"
    if nj >= 1:
        return "reverse_amp", 0.8, "有 %d 处除权型跳变（未正确前复权）" % nj
    if span_years < 3:
        return "raw_eq", 0.75, "次新无除权，三态等价"
    return "unknown_old", 0.3, "无跳变老票，抽检 2/2 倾向 reverse_amp"


def run():
    t0 = time.time()
    print("[Y1] loading kline...")
    feats = load_kline_features()
    fixed = load_fixed_set()
    print("[Y1] %d codes, %d fixed" % (len(feats), len(fixed)))
    recs = []
    for _, r in feats.iterrows():
        d, conf, note = classify(r, fixed)
        recs.append(dict(
            code=r['code'], dialect=d, confidence=conf, note=note,
            n_bigjump=int(r['n_bigjump']),
            span_days=int(r['span_days']),
            latest_close=r['latest_close'],
        ))
    out = dict(generated_at=time.strftime('%Y-%m-%d %H:%M:%S'),
               threshold_pct="main>11 / cy_ks>21, exclude first30d & gap>20d",
               total=len(recs), stocks=recs)
    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False)
    # summary
    from collections import Counter
    cnt = Counter(r['dialect'] for r in recs)
    tot = len(recs)
    print("\n[Y1] dialect distribution:")
    for k, v in cnt.most_common():
        print("  %-12s %5d  %5.1f%%" % (k, v, 100.0 * v / tot))
    print("nonstd(reverse_amp)  = %d (%.1f%%)" % (
        cnt.get('reverse_amp', 0), 100.0 * cnt.get('reverse_amp', 0) / tot))
    print("std_qfq              = %d (%.1f%%)" % (
        cnt.get('std_qfq', 0), 100.0 * cnt.get('std_qfq', 0) / tot))
    print("elapsed %.1fs -> %s" % (time.time() - t0, OUT))


def gold_check():
    feats = load_kline_features()
    fixed = load_fixed_set()
    fm = {r['code']: r for _, r in feats.iterrows()}
    strict_ok = dec_ok = 0
    print("\n[Y1] gold standard (decision-oriented):")
    print("%-7s %-10s %-11s %-11s %-6s" % ("code", "X3_dialect", "census", "decision", "conf"))
    for code, gold in GOLD.items():
        if code not in fm:
            print(code, "not in db"); continue
        d, conf, _ = classify(fm[code], fixed)
        dec = "std_qfq" if d == "std_qfq" else "nonstd"
        gold_dec = GOLD_DECISION[code]
        strict_ok += (d == gold)
        dec_ok += (dec == gold_dec)
        print("%-7s %-10s %-11s %-11s %.2f  %s%s" % (
            code, gold, d, dec, conf,
            "HIT" if dec == gold_dec else "MISS",
            "" if dec == gold_dec else " (strict: %s vs %s)" % (d, gold)))
    print("\n[Y1] strict 4-dialect accuracy: %d/9 ; decision(std vs nonstd): %d/9" % (strict_ok, dec_ok))


if __name__ == '__main__':
    if "--gold" in sys.argv:
        gold_check()
    else:
        run()
