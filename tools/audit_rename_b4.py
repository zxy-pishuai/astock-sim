# -*- coding: utf-8 -*-
"""B4 一次性历史重命名脚本（2026-09-13，用户已确认映射表）。
只 os.replace 改名，内容零改动；先打印映射表，执行后打印 SHA256 前后比对。
执行顺序：11→part2 → 12→11 → 13→12（先移出 11 腾位）。"""
import os, hashlib, sys

AUDIT_DIR = r"C:\Users\26838\A股模拟盘\data\audit"

MAP = [
    ("audit_20260911.jsonl", "audit_20260910_part2.jsonl"),  # 主体 09-10（73条+09-11的3条）
    ("audit_20260912.jsonl", "audit_20260911.jsonl"),        # 内容全 09-11
    ("audit_20260913.jsonl", "audit_20260912.jsonl"),        # 内容全 09-12
]

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

print("映射表（确认执行）：")
for src, dst in MAP:
    p = os.path.join(AUDIT_DIR, src)
    print("  %-28s -> %-30s  %d bytes" % (src, dst, os.path.getsize(p) if os.path.exists(p) else -1))

# 前置校验：源必须存在；目标存在时仅当"目标恰是本链中稍后/已移走的源"（链式腾位）才允许
sources = {s for s, _ in MAP}
for src, dst in MAP:
    p = os.path.join(AUDIT_DIR, src)
    d = os.path.join(AUDIT_DIR, dst)
    if not os.path.exists(p):
        sys.exit("缺失源文件: %s" % src)
    if os.path.exists(d) and dst not in sources:
        sys.exit("目标已存在且非链式源，中止: %s" % dst)

print("\n执行重命名：")
for src, dst in MAP:
    p = os.path.join(AUDIT_DIR, src)
    d = os.path.join(AUDIT_DIR, dst)
    h_before = sha256(p)
    os.replace(p, d)
    h_after = sha256(d)
    ok = h_before == h_after
    print("  %s -> %s  SHA256 %s  %s" % (src, dst, h_before[:16], "MATCH" if ok else "MISMATCH!!"))
    if not ok:
        sys.exit(1)

print("\n完成。目录现状：")
for fn in sorted(os.listdir(AUDIT_DIR)):
    if fn.startswith("audit") and fn.endswith(".jsonl"):
        print("  %s  %d bytes" % (fn, os.path.getsize(os.path.join(AUDIT_DIR, fn))))
