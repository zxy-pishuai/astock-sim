# -*- coding: utf-8 -*-
"""★ Phase46: 数据快照 CLI（核心逻辑在 app/data_snapshot.py，供 updater 自动调用）。

用法：
  python tools/data_snapshot.py [--keep 10] [--force] [--list]
"""
import sys
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from app import data_snapshot as ds   # noqa: E402


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Phase46 数据快照（前复权重锚定治理）")
    ap.add_argument("--keep", type=int, default=ds.KEEP_N_DEFAULT,
                    help="保留最近 N 份（默认 %d）" % ds.KEEP_N_DEFAULT)
    ap.add_argument("--force", action="store_true", help="当日快照已存在也重新生成")
    ap.add_argument("--list", action="store_true", help="仅列出现有快照")
    a = ap.parse_args()
    if a.list:
        for tag, path, mb in ds.list_snapshots():
            print("%s  %6.1fMB  %s" % (tag, mb, path))
        if not ds.list_snapshots():
            print("(无快照)")
        return
    p, created = ds.create_snapshot(force=a.force, keep_n=a.keep)
    print("[snapshot] %s (%s)" % (p, "新建" if created else "已存在"))
    print("[latest]", ds.latest_snapshot_path())


if __name__ == "__main__":
    main()
