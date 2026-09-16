# Phase 18 全球外盘行情模块报告

- 时间: 2026-08-23（数据截至当日盘中）
- 模块: `app/global_market.py`（新建）+ `app/server.py` 3 个只读 API
- 数据源: 腾讯 qt.gtimg.cn（主力）/ 东财 push2+push2his / 新浪 hq.sinajs.cn
- 约束: 纯标准库、熔断器、交易时段 TTL 缓存、未重启服务

## 1. 数据源与字段解析

### 1.1 腾讯 qt.gtimg.cn（主力，GBK，~ 分隔）
- 覆盖: 道琼斯/纳斯达克/标普500/VIX/恒生/恒生科技/国企指数 + 美股核心个股
  （NVDA/AAPL/TSLA/MSFT/GOOGL/AMZN/TSM/META）
- 字段校准（实测）: 指数 f[31]=涨跌额、f[32]=涨跌幅%；个股 f[31]=涨跌幅%
  （用道琼斯 + 英伟达各校验一次；返回 sym 为 .DJI/NVDA.OQ 等，按请求 code 映射）
- 实测样例: 道琼斯 53277.01 +0.98%、恒生 26009.46 +1.21%、VIX 21.67

### 1.2 东财 push2（日韩欧台）
- secids: 100.N225/100.KS11/100.TWII/100.FTSE/100.DAX/100.FCHI
- ⚠️ **当前本机 IP 被东财 push2 封禁**（RemoteDisconnected，README 已记录的住宅 IP 间歇风控）
  —— 接口按任务表格实现含 DAX→GDAXI/DAX30、FCHI→CAC 变体探测；熔断器自动兜底，
  待解封后自动恢复（腾讯/新浪时段可正常显示全球核心市场）
- push2his 指数日K: 同源被封 → 历史走"本地轮询积累"路径（每次收盘记录 global_kline）

### 1.3 新浪 hq.sinajs.cn（独占品种，GBK）
- fx_susdcnh 离岸人民币（字段1=价格 6.7193）、hf_GC COMEX黄金（4664.48）、
  hf_CL WTI原油（86.573）、DINIW 美元指数（98.85）
- 必须带 Referer https://finance.sina.com.cn/（否则 403）

## 2. 熔断与缓存

- 每源独立熔断器（参考 limitup._breakers）: 连续失败 3 次 → 冷却 10 分钟，源间互不影响
- 内存缓存: 各市场交易时段内 30s、休市 5min（北京时间；美股/欧洲夏令时规则实现）
- 交易时段判定: 美股 21:30-04:00（冬 22:30-05:00）、港股 9:30-16:00、日股 8:00-14:00、
  韩股 8:30-15:30、欧股 15:00-23:30（夏令时 14:00 开盘）

## 3. API（server.py，只读）

| API | 返回 |
|---|---|
| `GET /api/global/quotes` | `{quotes: {code: {name,price,pct,market,trading,updated}}, updated}` |
| `GET /api/global/history?sym=DJIA&days=60` | `{sym, days, rows: [{date, close}]}`（push2his 优先，否则本地积累）|
| `GET /api/global/summary` | `{markets_up/down/total, vix, vix_level, strongest, weakest, updated}` |

## 4. 验收结果

### 4.1 行情数据（实测，腾讯+新浪正常出数）
```
usDJI 道琼斯 53277.01 0.98% | usIXIC 纳斯达克 26180.45 0.43%
usINX 标普500 7674.37 0.43% | usVIX 21.67
hkHSI 恒生 26009.46 1.21% | hkHSTECH 恒生科技 4766.16 1.4% | hkHSCEI 国企 8634.34 1.01%
USDCNH 6.7193 | GC黄金 4664.48 | CL原油 86.573 | DINIW 98.85
```

### 4.2 熔断降级（改错 URL 模拟拔源）
- 强制腾讯源 3 次失败 → 熔断器激活 → `all_quotes()` 中 **tx=0（跳过）、sina=4（独立正常）** ✅
- 恢复后 tx=7 ✅  → **单源故障不影响其他源**

### 4.3 summary（实测）
```
markets_up=6 markets_down=0 total=6  VIX=21.67 中等（18-25 正常波动）
strongest=恒生科技 +1.4%  weakest=纳斯达克 +0.43%
```

### 4.4 history（本地积累路径）
- push2his 被封 → `global_kline` 每次收盘落库（INSERT OR REPLACE，主键 sym,date）
- 实测 `history('DJIA',5)` 返回已积累的收盘序列

## 5. 已知局限（如实）

1. **东财源当前被封**（本机 IP）：N225/KS11/TWII/FTSE/DAX 等待解封后自动恢复；
   冲击: 日韩欧台行情暂缺失，全球核心（美港）不受影响
2. **push2his 历史不可用期**：历史序列靠本地轮询积累（每天收盘记录），
   需要运行数天才有 60 日序列；解封后自动切 push2his
3. 新浪品种无涨跌幅百分比（仅价格）
4. 全球情绪摘要基于可得市场子集（当前腾讯 7 + 新浪 4，缺日韩欧台）

## 复现
```
GET http://127.0.0.1:8899/api/global/quotes
GET http://127.0.0.1:8899/api/global/history?sym=DJIA&days=60
GET http://127.0.0.1:8899/api/global/summary
# 熔断验证: 改 app/global_market.py 的 _TX_URL 为无效域名 → 3 次失败后 tx 源熔断
```