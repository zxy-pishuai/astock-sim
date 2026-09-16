# AKShare API 策略备忘录

本文件记录哪些 AKShare/HTTP 接口在当前 Linux 服务器环境下可用的策略知识。
因 EastMoney 服务器可能对当前 IP 段实施限流或连接断开，需使用备用数据源。

## 连接状态

| 数据源 | 状态 | 说明 |
|--------|------|------|
| EastMoney (push2.eastmoney.com) | ❌ **不稳定** | 高概率被限流/拒绝连接。用作备选 |
| 新浪财经 (Sina) | ✅ 稳定 | AKShare 新浪底层接口可靠 |
| 巨潮资讯 (cninfo.com.cn) | ✅ 稳定 | 官方信息披露平台 |
| 同花顺 (10jqka.com.cn) | ✅ 部分可用 | 板块排行 JSONP 接口可用 |

## 推荐的 AKShare 接口

### A股 — 优先使用的稳定接口

| 功能 | 推荐接口 | 备注 |
|------|----------|------|
| 实时行情 | `stock_zh_a_spot()` | 新浪数据源，5514只A股，含名称/价格/涨跌幅/成交量等 |
| 历史K线 | `stock_zh_a_hist(symbol, period, adjust='qfq')` | 前复权，支持 daily/weekly/monthly |
| 大盘指数 | `stock_zh_index_spot_sina()` | 新浪指数数据，返回主要市场指数 |
| 个股基本信息 | `stock_individual_info_em(symbol)` | 总股本/流通市值/行业/上市日期 |
| 资金流向 | `stock_individual_fund_flow(stock, market)` | 主力/超大单/大单/中单/小单明细 |
| 公司公告 | `stock_zh_a_disclosure_report_cninfo(symbol, start_date, end_date)` | 巨潮资讯，支持日期范围 |
| 券商研报 | `stock_research_report_em(symbol)` | 含评级/机构/PDF链接 |

### 港股 — 使用日K接口

| 功能 | 推荐接口 | 备注 |
|------|----------|------|
| 行情 | `stock_hk_daily(symbol, adjust='qfq')` | 返回完整日K数据，取最新行作为近似行情 |
| K线 | `stock_hk_daily(symbol, adjust='qfq')` | 同上，取 tail(limit) 行 |

### 美股 — 使用日K接口

| 功能 | 推荐接口 | 备注 |
|------|----------|------|
| 行情 | `stock_us_daily(symbol, adjust='qfq')` | 返回完整日K数据，取最新行作为近似行情 |
| K线 | `stock_us_daily(symbol, adjust='qfq')` | 同上，取 tail(limit) 行 |

## 不推荐使用（当前环境不稳定）

| 接口 | 原因 |
|------|------|
| `stock_board_industry_name_em()` | EastMoney 连接失败 |
| `stock_board_concept_name_em()` | EastMoney 连接失败 |
| `stock_hk_spot_em()` | EastMoney 连接失败 |
| `stock_individual_info_em()` | EastMoney 连接失败（个股信息） |
| `stock_hk_hist()` | EastMoney 连接失败 |
| `stock_us_hist()` | EastMoney 连接失败 |

## 执行注意事项

1. **必须使用 venv 的 Python**：`~/.hermes/skills/financial/tonghuashun/.venv/bin/python3`
2. **tqdm 进度条**输出到 stderr，管道路由时加 `2>/dev/null` 得到纯 JSON
3. **港股/美股的行情**来自日K数据，不是实时分笔，但足够日常参考
4. **AKShare 函数名**随版本变化（v1.18.60），迁移时先 `dir(ak)` 确认

