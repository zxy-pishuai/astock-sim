# -*- coding: utf-8 -*-
"""E1 收尾：定点重拉 9/4 13:16 盘中残缺 bar（批 6，200 票）。

流程：
1. 读批 6 票单（tmp/pack19/batch6_final.json）
2. 备份受影响行（最近 7 天 kline）到 tmp/pack19/e1_backup/<ts>.jsonl，含 SHA256
3. 调用 app.updater._fetch_and_save(codes, days=5) 覆盖重写（腾讯→新浪→TDX 取数链）
4. 逐票复验：9/4 的 close/volume/amount 与全库同日同票分布对照

红线：只允许覆盖写（upsert），禁物理删除任何 kline 行；生产库查询只读 URI。
用法：py -3.13 tools/e1_fix_intraday_bars.py [--dry-run]
"""
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

BATCH6_FILE = os.path.join(ROOT, 'tmp', 'pack19', 'batch6_final.json')
BACKUP_DIR = os.path.join(ROOT, 'tmp', 'pack19', 'e1_backup')


def sha256_str(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def backup_affected_rows(codes, days=7):
    """备份受影响票最近 days 天的 kline 行到 jsonl，含每行 SHA256。"""
    import sqlite3
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    backup_file = os.path.join(BACKUP_DIR, 'e1_backup_%s.jsonl' % ts)

    conn = sqlite3.connect('file:%s?mode=ro&immutable=1' % os.path.join(ROOT, 'data', 'market.db'),
                           uri=True, timeout=180)
    # 取最近 days 天的日期
    max_date = conn.execute("SELECT MAX(date) FROM kline WHERE period='day'").fetchone()[0]
    dates = conn.execute(
        "SELECT DISTINCT date FROM kline WHERE period='day' AND date <= ? ORDER BY date DESC LIMIT ?",
        (max_date, days)).fetchall()
    date_list = [d[0] for d in dates]
    print('备份日期范围: %s ~ %s (%d 天)' % (date_list[-1], date_list[0], len(date_list)))

    ph = ','.join('?' * len(codes))
    dph = ','.join('?' * len(date_list))
    rows = conn.execute(
        "SELECT code, date, open, high, low, close, volume, amount, period "
        "FROM kline WHERE code IN (%s) AND date IN (%s) AND period='day'" % (ph, dph),
        (*codes, *date_list)).fetchall()
    conn.close()

    with open(backup_file, 'w', encoding='utf-8', newline='\n') as f:
        for r in rows:
            obj = {'code': r[0], 'date': r[1], 'open': r[2], 'high': r[3], 'low': r[4],
                   'close': r[5], 'volume': r[6], 'amount': r[7], 'period': r[8]}
            line = json.dumps(obj, ensure_ascii=False, sort_keys=True)
            obj['_sha256'] = sha256_str(line)
            f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + '\n')

    # 整体 SHA256
    with open(backup_file, 'rb') as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()
    print('备份完成: %s (%d 行, SHA256=%s...)' % (backup_file, len(rows), file_hash[:16]))
    return backup_file, len(rows), file_hash


def refetch(codes, days=5, dry_run=False):
    """调用 _fetch_and_save 覆盖重写。"""
    if dry_run:
        print('[DRY-RUN] 将对 %d 票调用 _fetch_and_save(days=%d)' % (len(codes), days))
        return {'updated': 0, 'errors': 0, 'dry_run': True}

    from app import updater as U
    print('开始重拉: %d 票, days=%d' % (len(codes), days))
    t0 = time.time()
    r = U._fetch_and_save(codes, days)
    print('重拉完成: updated=%d errors=%d 耗时=%.1fs' % (r['updated'], r['errors'], time.time() - t0))
    return r


def verify(codes):
    """逐票复验：9/4 的 close/volume/amount 与全库同日同票分布对照。"""
    import sqlite3
    conn = sqlite3.connect('file:%s?mode=ro&immutable=1' % os.path.join(ROOT, 'data', 'market.db'),
                           uri=True, timeout=180)

    # 全库 9/4 分布
    all_94 = conn.execute(
        "SELECT volume, amount, close FROM kline WHERE date='2026-09-04' AND period='day' AND volume>0"
    ).fetchall()
    all_vol = sorted([r[0] for r in all_94])
    all_amt = sorted([r[1] for r in all_94 if r[1]])
    print('全库 9/4: %d 票, volume 中位数=%d, amount 中位数=%.0f' % (
        len(all_vol), all_vol[len(all_vol)//2], all_amt[len(all_amt)//2] if all_amt else 0))

    # 批 6 票 9/4 数据
    ph = ','.join('?' * len(codes))
    batch_94 = conn.execute(
        "SELECT code, close, volume, amount FROM kline WHERE code IN (%s) AND date='2026-09-04' AND period='day'" % ph,
        codes).fetchall()
    print('批 6 票 9/4 有数据: %d/%d' % (len(batch_94), len(codes)))

    # 异常检测：volume 低于全库 5 分位 或 amount 异常
    if all_vol:
        p5 = all_vol[int(len(all_vol) * 0.05)]
        p95 = all_vol[int(len(all_vol) * 0.95)]
        anomalies = [r for r in batch_94 if r[2] < p5 or r[2] > p95]
        print('volume 异常（<P5=%d 或 >P95=%d）: %d 票' % (p5, p95, len(anomalies)))
        for r in anomalies[:10]:
            print('  %s: close=%.2f volume=%d amount=%.0f' % (r[0], r[1], r[2], r[3] or 0))

    # 重拉前后对比：9/4 volume 是否显著增加（残缺 bar 修复标志）
    # 读备份文件对比
    backup_files = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith('e1_backup_')])
    if backup_files:
        latest_backup = os.path.join(BACKUP_DIR, backup_files[-1])
        old_data = {}
        with open(latest_backup, encoding='utf-8') as f:
            for line in f:
                obj = json.loads(line)
                if obj['date'] == '2026-09-04':
                    old_data[obj['code']] = obj['volume']
        improved = 0
        for code, close, vol, amt in batch_94:
            old_vol = old_data.get(code, 0)
            if old_vol > 0 and vol > old_vol * 1.1:
                improved += 1
        print('重拉后 9/4 volume 增加 >10%% 的票: %d/%d（残缺 bar 修复标志）' % (improved, len(batch_94)))

    conn.close()
    return {'batch_94_count': len(batch_94), 'anomalies': len(anomalies) if all_vol else 0}


def main():
    ap = argparse.ArgumentParser(description='E1 定点重拉 9/4 盘中残缺 bar')
    ap.add_argument('--dry-run', action='store_true', help='只备份不重拉')
    ap.add_argument('--days', type=int, default=5, help='重拉天数，默认 5')
    args = ap.parse_args()

    # 1. 读票单
    with open(BATCH6_FILE, encoding='utf-8') as f:
        batch6 = json.load(f)
    codes = batch6['codes']
    print('批 6 票单: %d 票, ratio 范围 %.3f~%.3f' % (
        len(codes), batch6['ratio_range'][0], batch6['ratio_range'][1]))
    print('前 5: %s' % codes[:5])
    print('后 5: %s' % codes[-5:])

    # 2. 备份
    backup_file, backup_rows, backup_hash = backup_affected_rows(codes, days=7)

    # 3. 重拉
    result = refetch(codes, days=args.days, dry_run=args.dry_run)

    # 4. 复验
    if not args.dry_run:
        verify_result = verify(codes)
    else:
        verify_result = {'dry_run': True}

    # 5. 输出总结
    print()
    print('=== 执行总结 ===')
    print('票单: %d 票（批 6，9/4 13:16 盘中残缺 bar）' % len(codes))
    print('备份: %s (%d 行, SHA256=%s...)' % (backup_file, backup_rows, backup_hash[:16]))
    print('重拉: updated=%d errors=%d' % (result.get('updated', 0), result.get('errors', 0)))
    print('复验: %s' % json.dumps(verify_result, ensure_ascii=False))


if __name__ == '__main__':
    main()
