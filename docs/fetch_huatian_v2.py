#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
华天科技(002185) 最近5年数据分析 - 第二次尝试
使用不同的数据源
"""

import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import warnings
warnings.filterwarnings('ignore')

# 方法1: 使用 stock_zh_a_hist_163 (网易)
print("尝试方法1: 网易数据源...")
try:
    df1 = ak.stock_zh_a_hist_163(symbol="002185", start_date="20210801", end_date="20260814")
    print(f"网易数据源成功! 共{len(df1)}条")
    print(df1.tail(5))
except Exception as e:
    print(f"网易失败: {e}")
    df1 = None

time.sleep(2)

# 方法2: 使用 stock_zh_a_daily (腾讯)
print("\n尝试方法2: 腾讯数据源...")
try:
    df2 = ak.stock_zh_a_daily(symbol="sz002185", adjust="qfq")
    print(f"腾讯数据源成功! 共{len(df2)}条")
    print(df2.tail(5))
except Exception as e:
    print(f"腾讯失败: {e}")
    df2 = None

time.sleep(2)

# 方法3: 使用 stock_zh_a_hist 带重试
print("\n尝试方法3: 东方财富重试...")
for i in range(3):
    try:
        df3 = ak.stock_zh_a_hist(
            symbol="002185",
            period="daily",
            start_date="20210801",
            end_date="20260814",
            adjust="qfq"
        )
        print(f"东方财富成功! 共{len(df3)}条")
        print(df3.tail(5))
        break
    except Exception as e:
        print(f"尝试{i+1}失败: {e}")
        time.sleep(3)
        df3 = None

# 找到可用的DataFrame
main_df = None
if df1 is not None and len(df1) > 0:
    main_df = df1
    data_source = "网易"
elif df2 is not None and len(df2) > 0:
    main_df = df2
    data_source = "腾讯"
elif df3 is not None and len(df3) > 0:
    main_df = df3
    data_source = "东方财富"

if main_df is not None:
    print(f"\n使用{data_source}数据源进行分析")
    print(f"列名: {list(main_df.columns)}")
    print(f"\n数据预览:")
    print(main_df.head(3))
    print("...")
    print(main_df.tail(3))
else:
    print("\n所有数据源均失败!")
