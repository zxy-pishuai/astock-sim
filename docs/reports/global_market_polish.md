# 全球市场页面打磨（Global Market Polish）

- 日期：2026-08-23
- 范围：`app/global_market.py`、`app/server.py`（仅 /api/global/*）、`web/index.html`、`web/js/app.js`、`web/css/app.css`
- 约束遵守：未触碰 engine.py / config.py / tools/ml_sidecar/ / docs/reports/ 既有文件；纯标准库；compile() 验证通过；node --check 通过

## 修复的 4 个缺陷

### 1. 美股/港股个股 chips 不渲染
根因有两层：
- 后端 `_TX_CODES` 缺少 usASML 及全部港股/日韩个股；
- 更隐蔽：腾讯返回的美股 sym 是**带后缀形式**（实测 `NVDA.OQ`），旧反向映射只接受
  `NVDA / .NVDA`，导致美股个股从未解析成功。已改为归一化候选匹配
  （原始 sym / 去后缀 / .前缀），并新增 kr/jp 的 sym 对应（`000660.KS`、`7203.T`）。

新增 `SYMBOLS` 注册表：全部跟踪标的带 `market / currency / kind` 标签
（USD/HKD/KRW/JPY）。前端 `GM_CARD_CONF` 与之对齐。

### 2. 日韩/欧卡片"数据暂不可用"
东财 push2 被本机 IP 阻断。新增 **Yahoo Finance chart 备用源**
（`query1.finance.yahoo.com/v8/finance/chart/%5E{SYM}?range=2d&interval=1d`，
取 `meta.regularMarketPrice` 与 `meta.chartPreviousClose` 自算涨跌幅），
仅补东财缺失的 N225/KS11/GDAXI/FTSE，独立熔断器 `yahoo`（复用 `_breakers`
模式：3 次失败冷却 10 分钟），东财解除阻断后自动恢复优先。
备用源启用时前端徽章显示 **"Yahoo备用源"**（黄色 warn 样式）而非"数据暂不可用"。

> ⚠ 实测备注：本机当前对 Yahoo 也返回 403（已尝试 UA/Referer/cookie 握手均被拒）。
> 备用链路代码就绪，解除网络阻断后即自动生效；在此之前日韩/欧卡片按降级逻辑显示
> "数据暂不可用"，不影响其他卡片。

### 3. 缺少重要外盘个股 + 无异动榜
- 新增跟踪：SK海力士(kr000660)、三星电子(kr005930)、丰田(jp7203)、索尼(jp6758)、
  阿斯麦ADR(usASML) 等，共 16 只个股（美9/港3/日2/韩2）。
- 新增 `/api/global/movers`（server.py 仅加路由）：|涨跌幅|≥2% 降序 top8；
  不足 3 只自动降至 1.5% 并标注 `lowered=true`。实测返回 5 只
  （特斯拉+5.14%、三星+3.87%、阿里-W −2.54%、SK海力士+2.31%、丰田+2.15%），
  未触发降阈值。
- 前端在情绪条下方新增 **"🔥 外盘异动"横条**，红涨绿跌，带市场角标（美/港/日/韩）。

### 4. 商品/外汇涨跌幅"—" + 布局粗糙
- ★实测校准新浪字段：`hf_` 期货 `[0]`=最新 `[7]`=昨结 → pct；
  `fx_s` 外汇 `[1]`=最新 `[2]`=昨收 → pct。DINIW 昨收字段与现价恒等不可信，
  自动转 `global_kline` 兜底（商品/外汇每轮顺带落库当日价，次日即可兜底）。
- 布局打磨：`.gm-name` min-width:90px + nowrap（"道琼斯"不再折行）；
  `.gm-price` flex:1 右对齐 + tabular-nums；`.gm-pct` 统一 76px 宽右对齐。
- KRW/JPY 价格千分位 + 币种后缀（如 `1,730,000 KRW`）。

## 字段校准结论（腾讯 qt.gtimg.cn，GBK）
实测 NVDA / 腾讯控股 / 000660.KS / 7203.T 一致：
`f[1]=名称 f[3]=最新 f[4]=昨收 f[31]=涨跌额 f[32]=涨跌幅%（指数/个股统一）`。
⚠ 旧代码对个股取 `f[31]` 当涨跌幅是错的（会把英伟达跌 2.13 美元显示成 -2.13%），
本次一并修正——当前会话英伟达实际涨跌幅为 -0.98%。

## 验收对照（重启后）
- ✅ 美股卡 9 chips、港股卡 3、日韩卡 日经/KOSPI + 4 chips（指数部分待 Yahoo/东财任一解阻）、欧洲卡 DAX/FTSE（同前）
- ✅ 异动条非空（实测 5 只 ≥2%；即使按真实涨跌幅不足 3 只也会降到 1.5%）
- ✅ 商品/外汇涨跌幅有值（黄金 +2.04%、WTI −0.30%、离岸人民币 −0.03%；美元指数待 kline 积累一天）
- ✅ 名称列不折行、价格/涨跌幅右对齐
- ✅ 60s 轮询与单源失败降级逻辑保留，无白屏路径

## 后续
- 手动重启服务后生效。
- 东财或 Yahoo 任一解除 IP 阻断后，日韩/欧指数自动恢复，无需改码。
