# -*- coding: utf-8 -*-
"""v3.7 分钟数据导入工具：桌面 494 只股票一年 5 分钟 K → 独立分库 min5.db
开发期工具（需要 pandas 读 pkl），运行时模拟盘保持零依赖。
用法：python tools/import_min5.py
"""
import glob
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = r"C:\Users\26838\Desktop\两点半战法验证\min5_1y"
# v3.7：分钟数据独立分库（不再塞进 market.db，避免单库膨胀）
DB_FILE = os.path.join(BASE, "data", "min5.db")


def main():
    files = sorted(glob.glob(os.path.join(SRC_DIR, "*.pkl")))
    if not files:
        print("未找到 pkl 数据:", SRC_DIR)
        return 1
    print(f"发现 {len(files)} 个 pkl 文件")
    conn = sqlite3.connect(DB_FILE, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("""CREATE TABLE IF NOT EXISTS kline_min5(
        code TEXT, date TEXT,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(code, date))""")
    # conn.execute("CREATE INDEX IF NOT EXISTS idx_kmin5_c ON kline_min5(code)")
    total = 0
    t0 = time.time()
    for i, f in enumerate(files, 1):
        code = os.path.basename(f)[:-4]  # sh600519 -> sh600519
        try:
            import pandas as pd
            df = pd.read_pickle(f)
        except Exception as e:
            print(f"  [{i}/{len(files)}] 跳过 {code}: {e}")
            continue
        if df.empty or "day" not in df.columns:
            print(f"  [{i}/{len(files)}] 跳过 {code}: 空数据")
            continue
        # 批量转换（避免 iterrows）
        dates = df["day"].astype(str).str[:19].str.replace("T", " ", regex=False).tolist()
        rows = [(code, dates[j],
                 float(df["open"].iat[j]), float(df["high"].iat[j]),
                 float(df["low"].iat[j]), float(df["close"].iat[j]),
                 float(df["volume"].iat[j])) for j in range(len(df))]
        conn.executemany(
            "INSERT OR REPLACE INTO kline_min5(code,date,open,high,low,close,volume) "
            "VALUES(?,?,?,?,?,?,?)", rows)
        total += len(rows)
        if i % 100 == 0 or i == len(files):
            conn.commit()
            print(f"  [{i}/{len(files)}] {code} {len(rows)} 根 累计{total:,} 用时{time.time()-t0:.0f}s")
    conn.commit()
    conn.close()
    print(f"✅ 导入完成：{len(files)} 只，共 {total:,} 根 5 分钟 K，耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
