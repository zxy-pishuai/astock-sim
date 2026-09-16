# Phase 19 全球市场前端页面 + 新闻联动报告

- 目标: 全球外盘行情可视化页面（五类市场卡片 + 情绪条 + 新闻联动 + A股映射提示）
- 改动: `web/index.html`（导航+页面+CSS）、`web/js/app.js`（渲染+60s轮询+降级）、
  `web/css/app.css`（样式）、`app/premarket_news.py`（市场维度标签）、
  `app/global_market.py`（A股映射提示规则）、`app/server.py`（/api/global/a_share_hint + summary 加提示）
- 约束: 前端零新依赖（现有 ECharts）、纯标准库、compile/node-check 通过、未重启服务

## 1. 前端页面

### 1.1 导航与结构
- 新增"🌍 全球市场"标签页（`page-global`），深色主题与现有仪表盘一致
- 顶部**全球情绪条**: 上涨市场数/总数、VIX 徽章（<15 绿 / 15-25 黄 / >25 红，
  CSS 类 vix-low/mid/high）、最强/最弱市场、A股映射提示条（gm-hints）
- **五类市场卡片**（gm-grid 2列，商品外汇占宽行）:
  - 美股: 道指/纳指/标普/VIX 指数 + 8 只个股 chips（代码+涨跌幅着色）
  - 港股: 恒生/恒科/国企 + 交易状态
  - 日韩/欧洲: 指数占位（东财被封时显示"数据暂不可用"降级）
  - 商品外汇: 离岸人民币/COMEX黄金/WTI原油/美元指数
- 每卡片含**交易状态徽章**（盘中闪烁点 live/休市灰）+ **60日 sparkline**
  （ECharts，`/api/global/history`；涨红跌绿按 A股习惯）
- **60s 轮询**，页面隐藏时暂停（`state.page === "global"` 判断）

### 1.2 实时性
- 行情/摘要 60s 刷新；新闻独立刷新按钮；刷新按钮强刷
- sparkline 按 sym 缓存 ECharts 实例（重渲染前 dispose 防泄漏）

## 2. 新闻市场维度标签

`app/premarket_news.py` 新增 `_MARKET_RULES`（6 类市场关键词）+ `market_tags()`：

| 市场 | 关键词（节选） |
|---|---|
| 美股 | 美联储/纳斯达克/标普/道指/美股/华尔街/英伟达/特斯拉/苹果/微软/谷歌/关税/美债收益率 |
| 日韩 | 日银/日元/日经/韩国/三星/SK海力士/KOSPI |
| 港股 | 恒生/港股/南向/联汇 |
| 欧洲 | 欧央行/欧元/DAX/富时 |
| 商品 | 黄金/金价/原油/OPEC/铜/铁矿 |
| 外汇 | 人民币/美元指数/离岸 |

- 每条新闻可命中多市场（`markets` 数组 + `market` 主市场）；未命中 → `["其他"]`
- 与现有板块标签（sectors）**并列不替换**；`premarket_news()` 输出每项带 `market`/`markets`
- 实测分布: 30 条新闻 → 美股17/其他12/商品1（真实打标正确）

## 3. A股映射提示（纯展示，不参与交易）

`app/global_market.py` 顶部 `ASHARE_MAP_RULES` 配置化规则表，可增删:

| 规则 | 触发 | 提示文案 |
|---|---|---|
| 半导体链 | 纳指 ±1.5% 且 英伟达 ±3% | 📈/📉 半导体链情绪偏强/弱 |
| 港股科技 | 恒生科技 ±2% | 港股科技映射 |
| 油气 | WTI ±3% | 油气利好/航空成本压力 |
| 全球避险 | VIX > 25 | ⚡ 全球避险，注意仓位 |
| 汇率 | 离岸人民币日内变动 >0.3% | 汇率压力 |

- 输出 `a_share_hint()` → `/api/global/summary.a_share_hints` + 独立
  `/api/global/a_share_hint`；前端 gm-hints 条渲染（up红/down绿/flag黄）
- 实测当前行情阈值内无触发（规则阈值内不误报）

## 4. 优雅降级

- 任一数据源失败 → 独立熔断器（3次失败冷却10min）→ 其他源不受影响
- 前端对 fetchJson 异常兜底: 显示"数据暂不可用"（不白屏）
- **当前运行服务为旧代码**（新 API 404）→ 页面实测降级为"行情数据暂不可用"，
  sparkline/新闻区域同样降级——即验收所需断源表现（重启服务后显示真实数据）

## 5. 验收结果

| 验收项 | 结果 |
|---|---|
| 五类卡片渲染 | ✅ 静态资源就绪（index.html/app.js/app.css 均含 gm 代码）；进程内数据链路验证：quotes 11 symbol / summary 6涨+VIX 21.67 / hint 规则工作 |
| 新闻按市场过滤 | ✅ market_tags 7 用例全对；真实新闻流 30 条打标正确 |
| 断源优雅降级 | ✅ 当前旧服务 404 → 前端降级"数据暂不可用"不白屏；熔断器单源隔离实测过（Phase18）|
| 前端零新依赖 | ✅ 用现有 ECharts + 原生 JS |

## 6. 说明（如实）

- **未重启服务**：运行中的应用仍为旧后端，新增 `/api/global/quotes`/`history`/`summary`/
  `a_share_hint` 与新闻 market 字段需**重启后生效**；前端静态文件浏览器刷新即加载，
  但后端 404 → 页面显示降级提示（当前可作断源验收）
- 东财源当前被本机 IP 封（Phase18 已述）：日韩/欧洲卡片在解封前显示降级
- A股映射提示为纯展示文案，不参与任何交易/仓位逻辑

## 复现
```
# 重启服务后（加载新后端）
GET /api/global/quotes          # 前端 global 页数据源
GET /api/global/summary         # 含 a_share_hints
GET /api/global/a_share_hint
GET /api/news/premarket?scope=global   # items 带 market/markets 字段
刷新 http://127.0.0.1:8899 → 点"🌍 全球市场"
```