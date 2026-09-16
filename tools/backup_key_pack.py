# -*- coding: utf-8 -*-
"""极小关键包打包工具 —— 同盘防误删/防写坏，不防盘坏。

打包"不可重建"档为单个 tar.gz，落 data/keypack/，内含 SHA256SUMS.txt + manifest.json。
打包后当场自校验（解包逐文件比 SHA256）；保留最近 10 份。

用法：
  py -3.13 tools/backup_key_pack.py                  # 打包 + 自校验 + 清理
  py -3.13 tools/backup_key_pack.py --verify <归档>  # 只校验不打包
  py -3.13 tools/backup_key_pack.py --keep 5         # 保留 5 份
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, 'data', 'keypack')
KEEP_DEFAULT = 10

# 固定成员（不可重建档）
FIXED_MEMBERS = [
    'data/account.json',
    'data/experiments_index.json',
    'data/ml_scores_ledger.jsonl',
    'data/forward_eval.jsonl',
    'data/update_state.json',
    'data/sector_map.json',
    'data/delisted_universe.json',
    'data/stock_list.json',
    'data/listing_dates.json',
    'data/audit/audit.jsonl',
    'data/quality_alert.jsonl',
    'data/risk_state.json',
    'data/watchlist.json',
    'data/notify.json',
    'data/price_alerts.json',
    'data/llm_config.json',
    'data/archive_index.json',
    'data/qfq_factor_sample.json',
    'data/qfq_triage.json',
    'data/qfq_defect_codes.json',
    'data/qfq_repair_progress.json',
    'data/ak_probe_results.json',
    'data/live_vs_backtest_weekly.json',
    'data/dual_price_weekly.json',
]

# 通配成员
GLOB_MEMBERS = [
    'data/bt_*.json',
    'docs/reports/*.md',
]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def count_lines(path):
    """行数摘要（二进制安全，按 \n 计数）。"""
    n = 0
    with open(path, 'rb') as f:
        for _ in f:
            n += 1
    return n


def collect_members():
    """收集所有成员，返回 [(relpath, abspath), ...]，去重，跳过不存在。"""
    seen = set()
    members = []
    for rel in FIXED_MEMBERS:
        abspath = os.path.join(BASE, rel)
        if os.path.isfile(abspath) and rel not in seen:
            seen.add(rel)
            members.append((rel, abspath))
    for pat in GLOB_MEMBERS:
        for abspath in glob_safe(pat):
            rel = os.path.relpath(abspath, BASE).replace('\\', '/')
            if rel not in seen:
                seen.add(rel)
                members.append((rel, abspath))
    return members


def glob_safe(pat):
    import glob
    return glob.glob(os.path.join(BASE, pat))


def account_summary(rel, abspath):
    """account.json 额外摘要：cash + 成交笔数 + 持仓数。"""
    if rel != 'data/account.json':
        return None
    try:
        d = json.load(open(abspath, encoding='utf-8'))
        return {
            'cash': d.get('cash'),
            'trades_count': len(d.get('trades', [])),
            'positions_count': len(d.get('positions', {})),
        }
    except Exception as e:
        return {'error': str(e)}


def build_manifest(members):
    """构建 manifest.json 内容。"""
    entries = []
    total_bytes = 0
    for rel, abspath in members:
        sz = os.path.getsize(abspath)
        total_bytes += sz
        entry = {
            'path': rel,
            'bytes': sz,
            'sha256': sha256_file(abspath),
            'lines': count_lines(abspath),
        }
        acct = account_summary(rel, abspath)
        if acct is not None:
            entry['account_summary'] = acct
        entries.append(entry)
    return {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'tool': 'tools/backup_key_pack.py',
        'member_count': len(entries),
        'total_bytes': total_bytes,
        'note': '同盘极小关键包，只防误删/写坏，不防盘坏；不含行情库 .db / snapshots / cyq_cache',
        'members': entries,
    }


def write_sha256sums(staging_dir, manifest):
    """写 SHA256SUMS.txt（兼容 sha256sum -c 格式）。"""
    lines = []
    for m in manifest['members']:
        lines.append('%s  %s' % (m['sha256'], m['path']))
    with open(os.path.join(staging_dir, 'SHA256SUMS.txt'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')


def pack(keep=KEEP_DEFAULT):
    """打包主流程。返回归档路径。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    members = collect_members()
    if not members:
        print('ERROR: no members found', file=sys.stderr)
        sys.exit(1)

    manifest = build_manifest(members)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    archive_name = 'keypack_%s.tar.gz' % stamp
    archive_path = os.path.join(OUT_DIR, archive_name)

    # staging：先拷贝到临时目录（避免源文件在打包时被修改导致撕裂）
    staging = tempfile.mkdtemp(prefix='keypack_staging_')
    try:
        for rel, abspath in members:
            dst = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(abspath, dst)
        # 写 manifest + SHA256SUMS 到 staging
        with open(os.path.join(staging, 'manifest.json'), 'w', encoding='utf-8', newline='\n') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        write_sha256sums(staging, manifest)

        # tar.gz 打包
        with tarfile.open(archive_path, 'w:gz') as tar:
            tar.add(staging, arcname='.')
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print('packed: %s (%.1f KB, %d members)' % (
        archive_path, os.path.getsize(archive_path) / 1024, len(members)))

    # 自校验
    ok = verify_archive(archive_path)
    if not ok:
        print('ERROR: self-verify FAILED, archive kept for inspection: %s' % archive_path,
              file=sys.stderr)
        sys.exit(2)

    # 清理
    cleanup(keep)
    return archive_path


def verify_archive(archive_path):
    """解包到临时目录，逐文件比 SHA256 + manifest 一致性。"""
    if not os.path.isfile(archive_path):
        print('ERROR: archive not found: %s' % archive_path, file=sys.stderr)
        return False

    tmpdir = tempfile.mkdtemp(prefix='keypack_verify_')
    try:
        with tarfile.open(archive_path, 'r:gz') as tar:
            tar.extractall(tmpdir, filter='data')

        manifest_path = os.path.join(tmpdir, 'manifest.json')
        if not os.path.isfile(manifest_path):
            print('ERROR: manifest.json missing in archive', file=sys.stderr)
            return False
        manifest = json.load(open(manifest_path, encoding='utf-8'))

        failures = []
        for m in manifest['members']:
            rel = m['path']
            extracted = os.path.join(tmpdir, rel)
            if not os.path.isfile(extracted):
                failures.append('%s: MISSING' % rel)
                continue
            actual = sha256_file(extracted)
            if actual != m['sha256']:
                failures.append('%s: SHA256 mismatch (expected %s, got %s)' % (
                    rel, m['sha256'][:16], actual[:16]))
            if os.path.getsize(extracted) != m['bytes']:
                failures.append('%s: size mismatch (expected %d, got %d)' % (
                    rel, m['bytes'], os.path.getsize(extracted)))

        if failures:
            print('VERIFY FAILED (%d failures):' % len(failures))
            for f in failures[:20]:
                print('  %s' % f)
            return False

        print('VERIFY OK: %d members, all SHA256/size match (manifest generated_at=%s)' % (
            len(manifest['members']), manifest.get('generated_at')))
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cleanup(keep=KEEP_DEFAULT):
    """保留最近 keep 份，超出按时间戳（文件名）排序删最旧。"""
    if not os.path.isdir(OUT_DIR):
        return
    archives = sorted(
        f for f in os.listdir(OUT_DIR)
        if f.startswith('keypack_') and f.endswith('.tar.gz')
    )
    while len(archives) > keep:
        old = archives.pop(0)
        os.remove(os.path.join(OUT_DIR, old))
        print('pruned: %s' % old)


def main():
    ap = argparse.ArgumentParser(description='极小关键包打包/校验（同盘防误删/写坏）')
    ap.add_argument('--verify', metavar='ARCHIVE', help='只校验指定归档，不打包')
    ap.add_argument('--keep', type=int, default=KEEP_DEFAULT, help='保留份数（默认 10）')
    args = ap.parse_args()

    if args.verify:
        ok = verify_archive(args.verify)
        sys.exit(0 if ok else 1)

    pack(keep=args.keep)


if __name__ == '__main__':
    main()
