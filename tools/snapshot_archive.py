# -*- coding: utf-8 -*-
"""Y4 快照/备份归档工具（一期：压缩→三验→入暂存，零删除）

纪律（红线段落）：
- 一期零删除：源文件压缩后移入 data/snapshots/_archive_pending_del/ 暂存，
  暂存区 7 天人工无异议才归二期删；_archive_pending_del 之外不存在"原文件消失"。
- 完整性三验：压缩前 SHA256 + 压缩产物 SHA256 + 解压实测后 SHA256 三值一致；
  全部记入 data/archive_index.json。
- 禁碰热库 market.db/min5.db、data/audit、account、manifest 原文件、任何代码。
- 压缩算法：优先 zstandard（若可用），否则回退 stdlib lzma（本环境实测回退）。

用法：
  python tools/snapshot_archive.py --test --src <file> --levels 3 9     # 两档实测
  python tools/snapshot_archive.py --archive --tag 2026-08-30 [--level 9]
  python tools/snapshot_archive.py --archive-backups [--level 9]
  python tools/snapshot_archive.py --restore --tag 2026-08-30 --file market.db.xz [--to <dir>]
  python tools/snapshot_archive.py --verify --tag 2026-08-30
  python tools/snapshot_archive.py --verify-all
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(BASE, "data", "snapshots")
STAGE = os.path.join(SNAP_DIR, "_archive_pending_del")
INDEX = os.path.join(BASE, "data", "archive_index.json")

try:
    import zstandard as _zstd
    METHOD = "zstd"
except Exception:
    _zstd = None
    METHOD = "lzma"

if METHOD == "zstd":
    def _compress_bytes(data, level=3):
        c = _zstd.ZstdCompressor(level=level)
        return c.compress(data)

    def _decompress_bytes(data):
        d = _zstd.ZstdDecompressor()
        return d.decompress(data)
    EXT = ".zst"
else:
    import lzma as _lzma

    def _compress_bytes(data, level=9):
        return _lzma.compress(data, preset=level)

    def _decompress_bytes(data):
        return _lzma.decompress(data)
    EXT = ".xz"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest().upper()


def load_index():
    if os.path.isfile(INDEX):
        with open(INDEX, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": METHOD + "(level 见各item)",
            "tool": "tools/snapshot_archive.py", "items": {}}


def save_index(idx):
    with open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)


def compress_file(src, dst_archive, dst_src, level):
    """压缩 src→dst_archive(.xz/.zst)，源移动到 dst_src（暂存）。
    返回 item dict（含双哈希+解压实测哈希）。全绿才移动源文件。"""
    t0 = time.time()
    h_orig = sha256(src)
    os.makedirs(os.path.dirname(dst_archive), exist_ok=True)
    with open(src, "rb") as f:
        data = f.read()
    comp = _compress_bytes(data, level)
    with open(dst_archive, "wb") as f:
        f.write(comp)
    h_arch = sha256_bytes(comp)
    # 解压实测
    rest = _decompress_bytes(comp)
    h_rest = sha256_bytes(rest)
    ok = (h_orig == h_rest)
    if not ok:
        raise RuntimeError("解压哈希不匹配: %s (orig=%s rest=%s)" % (src, h_orig, h_rest))
    # 全绿才移动源文件入暂存
    os.makedirs(os.path.dirname(dst_archive), exist_ok=True)
    os.makedirs(os.path.dirname(dst_src), exist_ok=True)
    shutil.move(src, dst_src)
    return {
        "orig_path": src, "archive_path": dst_archive, "staged_src_path": dst_src,
        "size_orig": os.path.getsize(dst_src), "size_archive": os.path.getsize(dst_archive),
        "sha256_orig": h_orig, "sha256_archive": h_arch, "sha256_restored": h_rest,
        "level": level, "method": METHOD, "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1), "verified": True,
    }


def archive_path_for(rel):
    """暂存区归档产物路径：_archive_pending_del/<rel>.<EXT>"""
    return os.path.join(STAGE, rel + EXT)


def staged_src_for(rel):
    """暂存区源文件路径：_archive_pending_del/src/<rel>"""
    return os.path.join(STAGE, "src", rel)


def archive_tree(root, rel_prefix, level, idx, dry_srcs):
    """压缩 root 下全部文件（rel 相对 root），返回 item 列表。dry_srcs: 禁碰清单。"""
    items = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, BASE).replace("\\", "/")
            # 跳过禁碰清单
            if any(d in rel for d in dry_srcs):
                print("  [skip-禁碰] %s" % rel)
                continue
            arc = archive_path_for(rel)
            ssrc = staged_src_for(rel)
            item = compress_file(src, arc, ssrc, level)
            idx["items"][rel] = item
            items.append(item)
            print("  [ok] %s -> %.1fMB/%.1fMB (%.1fs)" % (
                rel, item["size_archive"] / 1048576.0,
                item["size_orig"] / 1048576.0, item["elapsed_s"]))
    return items


def cmd_test(args):
    """两档实测：压缩 src 到 tmp，报告 ratio/time。"""
    for lv in args.levels:
        t0 = time.time()
        with open(args.src, "rb") as f:
            data = f.read()
        comp = _compress_bytes(data, lv)
        dt = time.time() - t0
        print("level=%d: %d -> %d (%.2fx) %.1fs" % (
            lv, len(data), len(comp), len(data) / max(1, len(comp)), dt))
    return 0


def cmd_archive(args):
    idx = load_index()
    if args.tag:
        tag = args.tag
        snap = os.path.join(SNAP_DIR, tag)
        if not os.path.isdir(snap):
            print("快照目录不存在:", snap); return 2
        print("归档快照 %s（method=%s level=%s）..." % (tag, METHOD, args.level))
        items = archive_tree(snap, "", args.level, idx, dry_srcs=["manifest.json"])
        # manifest 保留原位（禁碰）
        print("  已归档 %d 文件；manifest.json 保留原位" % len(items))
    if args.backups:
        bk = os.path.join(BASE, "data", "backups")
        print("归档 backups（method=%s level=%s）..." % (METHOD, args.level))
        items = archive_tree(bk, "", args.level, idx, dry_srcs=[])
        print("  已归档 %d 文件" % len(items))
    save_index(idx)
    print("索引已更新:", INDEX)
    return 0


def cmd_restore(args):
    idx = load_index()
    # 找匹配 item
    if args.tag:
        rels = [r for r in idx["items"] if ("snapshots/%s/" % args.tag) in r]
        if not rels:
            print("未找到 tag=%s 的归档" % args.tag); return 2
        rel = rels[0] if not args.file else \
            next((r for r in rels if r.endswith(args.file)), None)
    elif args.file:
        rel = next((r for r in idx["items"] if r.endswith(args.file)), None)
    else:
        rel = None
    if not rel:
        print("未找到归档文件:", args.file or args.tag); return 2
    item = idx["items"][rel]
    with open(item["archive_path"], "rb") as f:
        data = f.read()
    rest = _decompress_bytes(data)
    # 默认还原到原路径；--to 指定目标
    dest = os.path.join(BASE, rel) if not args.to else args.to
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(rest)
    h = sha256_bytes(rest)
    print("还原 %s -> %s (%.1fMB) sha256=%s" % (item["archive_path"], dest,
                                                len(rest) / 1048576.0, h))
    print("  vs orig %s : %s" % (item["sha256_orig"], "MATCH" if h == item["sha256_orig"] else "MISMATCH"))
    return 0 if h == item["sha256_orig"] else 3


def cmd_verify(args):
    idx = load_index()
    if args.tag:
        rels = [r for r in idx["items"] if ("snapshots/%s/" % args.tag) in r]
    elif args.all:
        rels = list(idx["items"])
    else:
        rels = list(idx["items"])
    bad = 0
    for rel in sorted(rels):
        item = idx["items"][rel]
        ap = item["archive_path"]
        if not os.path.isfile(ap):
            print("  [MISS] %s 归档文件丢失" % rel); bad += 1; continue
        with open(ap, "rb") as f:
            data = f.read()
        rest = _decompress_bytes(data)
        h = sha256_bytes(rest)
        ok = (h == item["sha256_orig"])
        print("  [%s] %s  (restored=%s orig=%s)" % (
            "OK" if ok else "BAD", rel, h[:12], item["sha256_orig"][:12]))
        if not ok:
            bad += 1
    print("验证完成: %d/%d 全绿" % (len(rels) - bad, len(rels)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="快照/备份归档（一期，零删除）")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--src")
    ap.add_argument("--levels", nargs="+", type=int, default=[3, 9])
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--tag")
    ap.add_argument("--backups", action="store_true")
    ap.add_argument("--level", type=int, default=9)
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--to")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    print("方法: %s（zstd 不可用回退 lzma） 扩展: %s" % (METHOD, EXT))
    if a.test:
        return cmd_test(a)
    if a.archive:
        return cmd_archive(a)
    if a.restore:
        return cmd_restore(a)
    if a.verify:
        return cmd_verify(a)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
