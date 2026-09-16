#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
华天科技(002185) 最近5年数据分析及下月走势预测
"""

import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import warnings
warnings.filterwarnings('ignore')

# ============ 1. 获取历史股价数据 ============
print("=" * 60)
print("【1】获取华天科技(002185)历史股价数据...")
print("=" * 60)

try:
    # 获取日K线数据
    stock_zh_a_hist_df = ak.stock_zh_a_hist(
        symbol="002185",
        period="daily",
        start_date="20210801",
        end_date="20260814",
        adjust="qfq"  # 前复权
    )
    print(f"股价数据获取成功，共 {len(stock_zh_a_hist_df)} 条记录")
    print(f"数据列: {list(stock_zh_a_hist_df.columns)}")
    print(f"\n最新数据:\n{stock_zh_a_hist_df.tail(5)}")
except Exception as e:
    print(f"获取日K线失败: {e}")
    stock_zh_a_hist_df = None

# ============ 2. 获取月K线数据 ============
print("\n" + "=" * 60)
print("【2】获取月K线数据...")
print("=" * 60)

try:
    stock_monthly = ak.stock_zh_a_hist(
        symbol="002185",
        period="monthly",
        start_date="20210801",
        end_date="20260814",
        adjust="qfq"
    )
    print(f"月K线数据获取成功，共 {len(stock_monthly)} 条记录")
    print(f"\n月K线数据:\n{stock_monthly.to_string()}")
except Exception as e:
    print(f"获取月K线失败: {e}")
    stock_monthly = None

# ============ 3. 获取财务数据 ============
print("\n" + "=" * 60)
print("【3】获取华天科技财务数据...")
print("=" * 60)

# 3.1 利润表
try:
    profit_df = ak.stock_financial_report_sina(stock="002185", symbol="利润表")
    print(f"\n利润表数据获取成功")
    print(profit_df.head(10).to_string())
except Exception as e:
    print(f"获取利润表失败: {e}")

# 3.2 关键财务指标
try:
    indicator_df = ak.stock_financial_analysis_indicator(symbol="002185")
    print(f"\n财务指标数据获取成功")
    print(indicator_df.head(10).to_string())
except Exception as e:
    print(f"获取财务指标失败: {e}")

# ============ 4. 年度统计 ============
print("\n" + "=" * 60)
print("【4】近5年年度股价统计...")
print("=" * 60)

if stock_zh_a_hist_df is not None:
    stock_zh_a_hist_df['日期'] = pd.to_datetime(stock_zh_a_hist_df['日期'])
    stock_zh_a_hist_df['年份'] = stock_zh_a_hist_df['日期'].dt.year
    
    yearly_stats = stock_zh_a_hist_df.groupby('年份').agg(
        年初价=('开盘', 'first'),
        年末价=('收盘', 'last'),
        最高价=('最高', 'max'),
        最低价=('最低', 'min'),
        年均成交量=('成交量', 'mean'),
        总成交额=('成交额', 'sum'),
        年涨跌幅=('涨跌幅', lambda x: (x/100 + 1).prod() - 1)
    )
    yearly_stats['年涨跌幅'] = (yearly_stats['年涨跌幅'] * 100).round(2)
    print(yearly_stats.to_string())

# ============ 5. 最近6个月统计 ============
print("\n" + "=" * 60)
print("【5】最近6个月每月股价统计...")
print("=" * 60)

if stock_zh_a_hist_df is not None:
    recent = stock_zh_a_hist_df[stock_zh_a_hist_df['日期'] >= (datetime(2026, 8, 14) - timedelta(days=180))]
    recent['年月'] = recent['日期'].dt.to_period('M')
    monthly_stats = recent.groupby('年月').agg(
        开盘价=('开盘', 'first'),
        收盘价=('收盘', 'last'),
        最高价=('最高', 'max'),
        最低价=('最低', 'min'),
        月涨跌幅=('涨跌幅', lambda x: (x/100 + 1).prod() - 1),
        平均成交量=('成交量', 'mean')
    )
    monthly_stats['月涨跌幅'] = (monthly_stats['月涨跌幅'] * 100).round(2)
    print(monthly_stats.to_string())

# ============ 6. 技术分析 ============
print("\n" + "=" * 60)
print("【6】技术分析指标...")
print("=" * 60)

if stock_zh_a_hist_df is not None:
    df = stock_zh_a_hist_df.copy()
    df = df.sort_values('日期').reset_index(drop=True)
    
    # 均线系统
    df['MA5'] = df['收盘'].rolling(window=5).mean()
    df['MA10'] = df['收盘'].rolling(window=10).mean()
    df['MA20'] = df['收盘'].rolling(window=20).mean()
    df['MA60'] = df['收盘'].rolling(window=60).mean()
    df['MA120'] = df['收盘'].rolling(window=120).mean()
    df['MA250'] = df['收盘'].rolling(window=250).mean()
    
    # MACD
    df['EMA12'] = df['收盘'].ewm(span=12, adjust=False).mean()
    df['EMA26'] = df['收盘'].ewm(span=26, adjust=False).mean()
    df['DIF'] = df['EMA12'] - df['EMA26']
    df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
    df['MACD'] = 2 * (df['DIF'] - df['DEA'])
    
    # RSI
    delta = df['收盘'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI14'] = 100 - (100 / (1 + rs))
    
    # 布林带
    df['BB_MID'] = df['收盘'].rolling(window=20).mean()
    df['BB_STD'] = df['收盘'].rolling(window=20).std()
    df['BB_UP'] = df['BB_MID'] + 2 * df['BB_STD']
    df['BB_LOW'] = df['BB_MID'] - 2 * df['BB_STD']
    
    # KDJ
    low_min = df['最低'].rolling(window=9).min()
    high_max = df['最高'].rolling(window=9).max()
    df['RSV'] = (df['收盘'] - low_min) / (high_max - low_min) * 100
    df['K'] = df['RSV'].ewm(com=2, adjust=False).mean()
    df['D'] = df['K'].ewm(com=2, adjust=False).mean()
    df['J'] = 3 * df['K'] - 2 * df['D']
    
    # 打印最新技术指标
    latest = df.iloc[-1]
    print(f"\n最新日期: {latest['日期'].strftime('%Y-%m-%d')}")
    print(f"收盘价: {latest['收盘']:.2f}")
    print(f"\n--- 均线系统 ---")
    print(f"MA5:   {latest['MA5']:.2f}")
    print(f"MA10:  {latest['MA10']:.2f}")
    print(f"MA20:  {latest['MA20']:.2f}")
    print(f"MA60:  {latest['MA60']:.2f}")
    print(f"MA120: {latest['MA120']:.2f}")
    print(f"MA250: {latest['MA250']:.2f}")
    
    print(f"\n--- MACD ---")
    print(f"DIF:  {latest['DIF']:.4f}")
    print(f"DEA:  {latest['DEA']:.4f}")
    print(f"MACD: {latest['MACD']:.4f}")
    
    print(f"\n--- RSI ---")
    print(f"RSI14: {latest['RSI14']:.2f}")
    
    print(f"\n--- 布林带 ---")
    print(f"上轨: {latest['BB_UP']:.2f}")
    print(f"中轨: {latest['BB_MID']:.2f}")
    print(f"下轨: {latest['BB_LOW']:.2f}")
    
    print(f"\n--- KDJ ---")
    print(f"K: {latest['K']:.2f}")
    print(f"D: {latest['D']:.2f}")
    print(f"J: {latest['J']:.2f}")
    
    # 趋势判断
    print(f"\n--- 趋势判断 ---")
    bullish_signals = 0
    bearish_signals = 0
    
    # 1. 均线多头/空头排列
    if latest['MA5'] > latest['MA10'] > latest['MA20']:
        print("均线: 短期均线多头排列 ↑")
        bullish_signals += 1
    elif latest['MA5'] < latest['MA10'] < latest['MA20']:
        print("均线: 短期均线空头排列 ↓")
        bearish_signals += 1
    else:
        print("均线: 均线交织，方向不明 →")
    
    # 2. 价格与MA60关系
    if latest['收盘'] > latest['MA60']:
        print("价格在60日均线上方 ↑")
        bullish_signals += 1
    else:
        print("价格在60日均线下方 ↓")
        bearish_signals += 1
    
    # 3. MACD
    if latest['DIF'] > latest['DEA']:
        print("MACD: DIF在DEA上方(金叉) ↑")
        bullish_signals += 1
    else:
        print("MACD: DIF在DEA下方(死叉) ↓")
        bearish_signals += 1
    
    # 4. RSI
    if latest['RSI14'] > 70:
        print(f"RSI: {latest['RSI14']:.1f} 超买区域 ⚠")
        bearish_signals += 1
    elif latest['RSI14'] < 30:
        print(f"RSI: {latest['RSI14']:.1f} 超卖区域 ⚠")
        bullish_signals += 1
    else:
        print(f"RSI: {latest['RSI14']:.1f} 中性区域 →")
    
    # 5. KDJ
    if latest['K'] > latest['D'] and latest['J'] > 80:
        print("KDJ: 超买区域 ⚠")
        bearish_signals += 1
    elif latest['K'] > latest['D']:
        print("KDJ: K在D上方 ↑")
        bullish_signals += 1
    else:
        print("KDJ: K在D下方 ↓")
        bearish_signals += 1
    
    # 6. 布林带位置
    if latest['收盘'] > latest['BB_UP']:
        print("布林带: 价格突破上轨 ⚠")
        bearish_signals += 1
    elif latest['收盘'] < latest['BB_LOW']:
        print("布林带: 价格跌破下轨 ⚠")
        bullish_signals += 1
    elif latest['收盘'] > latest['BB_MID']:
        print("布林带: 价格在中轨和上轨之间 ↑")
        bullish_signals += 1
    else:
        print("布林带: 价格在中轨和下轨之间 ↓")
        bearish_signals += 1
    
    # 7. 近期趋势
    last_20 = df.tail(20)
    price_change_20d = (latest['收盘'] / last_20.iloc[0]['收盘'] - 1) * 100
    last_60 = df.tail(60)
    price_change_60d = (latest['收盘'] / last_60.iloc[0]['收盘'] - 1) * 100
    
    print(f"\n--- 近期表现 ---")
    print(f"近20个交易日涨跌幅: {price_change_20d:.2f}%")
    print(f"近60个交易日涨跌幅: {price_change_60d:.2f}%")
    
    # 成交量分析
    avg_vol_20 = last_20['成交量'].mean()
    avg_vol_60 = last_60['成交量'].mean()
    vol_ratio = avg_vol_20 / avg_vol_60
    print(f"近20日平均成交量/近60日平均成交量: {vol_ratio:.2f}")
    if vol_ratio > 1.2:
        print("成交量放大 ↑")
    elif vol_ratio < 0.8:
        print("成交量萎缩 ↓")
    
    print(f"\n--- 综合信号统计 ---")
    print(f"看多信号: {bullish_signals}")
    print(f"看空信号: {bearish_signals}")
    
    if bullish_signals > bearish_signals + 2:
        print("综合判断: 偏多，下月大概率震荡上行 ↑")
    elif bearish_signals > bullish_signals + 2:
        print("综合判断: 偏空，下月大概率震荡下行 ↓")
    elif bullish_signals > bearish_signals:
        print("综合判断: 略偏多，下月可能小幅上行 ↑")
    elif bearish_signals > bullish_signals:
        print("综合判断: 略偏空，下月可能小幅回调 ↓")
    else:
        print("综合判断: 多空平衡，下月大概率震荡整理 →")

# ============ 7. 价格区间预测 ============
print("\n" + "=" * 60)
print("【7】下月价格区间预测...")
print("=" * 60)

if stock_zh_a_hist_df is not None:
    df = stock_zh_a_hist_df.copy().sort_values('日期').reset_index(drop=True)
    
    # 计算历史波动率
    df['日收益率'] = df['收盘'].pct_change()
    vol_20d = df['日收益率'].tail(20).std() * np.sqrt(252)  # 年化波动率
    vol_60d = df['日收益率'].tail(60).std() * np.sqrt(252)
    
    current_price = df.iloc[-1]['收盘']
    
    # 预测下月(约22个交易日)的价格区间
    daily_vol = df['日收益率'].tail(20).std()
    monthly_move = daily_vol * np.sqrt(22)
    
    upper_1sd = current_price * (1 + monthly_move)
    lower_1sd = current_price * (1 - monthly_move)
    upper_2sd = current_price * (1 + 2 * monthly_move)
    lower_2sd = current_price * (1 - 2 * monthly_move)
    
    print(f"\n当前价格: {current_price:.2f} 元")
    print(f"20日年化波动率: {vol_20d*100:.2f}%")
    print(f"60日年化波动率: {vol_60d*100:.2f}%")
    print(f"\n--- 下月(22个交易日)价格预测区间 ---")
    print(f"1倍标准差区间: {lower_1sd:.2f} ~ {upper_1sd:.2f} 元 (约68%概率)")
    print(f"2倍标准差区间: {lower_2sd:.2f} ~ {upper_2sd:.2f} 元 (约95%概率)")
    
    # 支撑位和压力位
    latest = df.iloc[-1]
    recent_high = df.tail(60)['最高'].max()
    recent_low = df.tail(60)['最低'].min()
    
    print(f"\n--- 关键支撑和压力位 ---")
    print(f"近60日最高价(压力位): {recent_high:.2f}")
    print(f"近60日最低价(支撑位): {recent_low:.2f}")

print("\n" + "=" * 60)
print("数据分析完成！")
print("=" * 60)
