# -*- coding: utf-8 -*-
"""data/ \u5907\u4efd\u5de5\u5177 \u2014\u2014 SQLite \u5728\u7ebf\u5907\u4efd\uff08\u670d\u52a1\u8fd0\u884c\u65f6\u5b89\u5168\uff09+ \u914d\u7f6e JSON \u5feb\u7167
\u7528\u6cd5\uff1a
  python tools/backup_data.py            # \u5907\u4efd market.db + \u5168\u90e8 JSON
  python tools/backup_data.py --big      # \u540c\u65f6\u5907\u4efd min5.db\uff08\u7ea6 679MB\uff0c\u8f83\u6162\uff09
  python tools/backup_data.py --keep 5   # \u4fdd\u7559\u6700\u8fd1 5 \u4efd\uff08\u9ed8\u8ba4 7\uff09
"""
import argparse
import os
import shutil
import sqlite3
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')
JSONS = ['account.json', 'risk_state.json', 'signal_pool.json', 'watchlist.json',
         'ml_scores.json', 'notify.json', 'price_alerts.json', 'llm_config.json',
         'update_state.json', 'sector_map.json', 'rigorous_result.json']


def backup_db(src, dst):
    conn = sqlite3.connect(src)
    try:
        bck = sqlite3.connect(dst)
        try:
            with bck:
                conn.backup(bck)
        finally:
            bck.close()
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--big', action='store_true', help='also backup min5.db (~679MB, slow)')
    ap.add_argument('--keep', type=int, default=7)
    args = ap.parse_args()
    stamp = time.strftime('%Y%m%d_%H%M%S')
    out = os.path.join(DATA, 'backups', stamp)
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    dbs = ['market.db'] + (['min5.db'] if args.big else [])
    for db in dbs:
        src = os.path.join(DATA, db)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(out, db)
        t = time.time()
        backup_db(src, dst)
        print('%s -> %.1fMB, %.1fs' % (db, os.path.getsize(dst) / 1048576, time.time() - t))
    n = 0
    for fn in JSONS:
        src = os.path.join(DATA, fn)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out, fn))
            n += 1
    print('json files: %d' % n)
    bdir = os.path.join(DATA, 'backups')
    dirs = sorted(d for d in os.listdir(bdir) if os.path.isdir(os.path.join(bdir, d)))
    while len(dirs) > args.keep:
        old = dirs.pop(0)
        shutil.rmtree(os.path.join(bdir, old))
        print('pruned old backup: %s' % old)
    print('done in %.1fs -> %s' % (time.time() - t0, out))


if __name__ == '__main__':
    main()
