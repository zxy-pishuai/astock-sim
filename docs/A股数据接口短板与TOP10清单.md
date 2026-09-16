# A股实战数据接口短板与龙虎榜策略 — TOP 10 清单（模拟盘 4.0）

> 结论基于 2026-08-16 完成的 web 调研 + 关键接口活体实测。北向盘中实时已停（2024-08-19 起）；龙虎榜/涨停池/资金流接口实测仍存活（拉到 2026-08-14 交易日数据）。完整接口参数见《A股资金流调研报告_模拟盘4.0.md》。

---

## 一、模拟盘的数据接口短板

### A. 免费拿不到 / 已缺失的数据（6 项）

| 缺口 | 现状 | 替代/绕行方案 |
|---|---|---|
| **Level-2 真十档/千档盘口** | 免费仅有五档（腾讯/东财/通达信）；十档、逐笔撤单还原、千档委托队列是付费 Level-2（同花顺/东财/量化厂商） | 五档 + 逐笔 + 分钟资金流做"近似 Level-2" |
| **盘中实时逐笔成交** | 无免登录 HTTP 实时逐笔 | 通达信 TCP（需第三方库）或新浪历史逐笔（T+1 回看）；盘中只能近似 |
| **北向资金盘中实时** | 2024-08-19 起交易所取消盘中实时披露，仅存盘后日频（成交总额 + HKEX 持股） | "北向实时流入"维度放弃；日频走 HKEX 官方 `hkex.com.hk/chi/csm/DailyStat/data_tab_daily_{YYYYMMDD}c.js` |
| **筹码分布 / 获利比例** | 无直接免费接口（东财 `RPT_CUSTOM_CYQ` 实测"报表配置不存在"；同花顺/通达信客户端是本地算法） | 东财日K(含换手率) + 本地移植 CYQCalculator 算法（120日窗口/150档） |
| **游资席位权威数据库** | 无官方/免费权威映射库，只有社区静态名单；营业部会换、会改名、同名不同人 | 本地维护 游资→营业部 映射表 + 席位胜率滚动统计，持续修正 |
| **龙虎榜时效** | T+1 盘后披露（约 17:00 后），无盘中实时；三日榜/机构明细同为盘后 | 盘后信号 → 次日竞价强度决定参与，不做盘中 |

> 另：**逐笔级大单明细**（实时大单追踪）也属付费 Level-2 范畴——东财资金流的"超大/大/中/小单"只是按单笔金额阈值做的聚合，不是逐笔明细。

### B. 不稳定 / 有坑的接口（9 项，实测与社区口径）

1. **东财 push2 系（clist/stock/get）**：实测本机间歇"连接被重置"（WAF 风控）。封禁阈值：>5 req/s、单 IP 并发 ≥10、1 分钟 ≥200、5 分钟 ≥300 → IP 级封禁可达 20+ 小时。**但子域独立**：datacenter-web / push2ex 不受 push2 封禁牵连。
2. **东财报表名/列名会变**：如 `RPT_LIFT_STAGE` 列名 2026 变更（FREE_SHARES_TYPE）→ 代码必须字段容错 + 定期验证。
3. **腾讯 K 线**：连续 5000+ 次请求后返回空（限流非封 IP）；**分钟K 第 7 字段是换手率基点不是成交额**（差 3 个数量级的经典坑），成交额需自算。
4. **百度 PAE 接口**：`fundflow` / `fundsortlist` 2026-05 已下线 → 旧资金流代码全部失效。
5. **财联社 nodeapi**：旧接口下线；新版 `v1/roll/get_roll_list` 强制本地签名（`sign=md5(sha1(按key字典序拼接的query))`，零 key 可算）。
6. **同花顺**：部分接口反爬 401（行业板块排行需登录态）；涨停揭秘 `field` 是内部字段 ID 需照抄；`first_limit_up_time` 是 Unix 秒时间戳。
7. **mootdx 库烂尾**：2024 停更，0.11.x 有 BESTIP 空串 bug；协议本身正常 → 用显式服务器列表（tdx_client 模式）或换 easy_tdx。
8. **北交所老号段**：43/83/87 老码返回僵尸数据（腾讯定格报价、东财研报静默 0 篇），需映射到 920xxx 新码。
9. **大陆住宅 IP 偶发风控**：push2 对部分住宅宽带 IP 间歇 HTTP 000/空数据 → 重试 / 换网络 / 调大限流间隔。

---

## 二、龙虎榜 / 游资策略实战用法

### 数据层（东财 datacenter 三报表 + 本地游资映射表，全部免费零鉴权）
- 上榜明细：`RPT_DAILYBILLBOARD_DETAILSNEW`（TRADE_DATE / SECURITY_CODE / EXPLANATION 上榜原因 / BILLBOARD_NET_AMT 净买额 / TURNOVERRATE 换手）
- 买/卖席位 TOP5：`RPT_BILLBOARD_DAILYDETAILSBUY` / `...SELL`（OPERATEDEPT_NAME 营业部 / BUY / SELL / NET）
- 机构专用席位：`OPERATEDEPT_CODE=="0"`（在买卖席位明细中筛选聚合）
- 游资映射：社区静态名单（[东财财富号一览表](https://caifuhao.eastmoney.com/news/20250209224756905041420)、[龙虎榜元老及席位](https://caifuhao.eastmoney.com/news/20251114025804913640970)、[360doc 各路游资席位](http://www.360doc.com/content/24/0502/06/52230674_1122117303.shtml)、[淘股吧席位一览](https://m.tgb.cn/a/1Y24ItWmSB4)）+ 本地 json 维护

### 策略层（10 条实战用法）
1. **机构净买信号**：机构专用席位净买入 > 阈值（如 5000 万）且买入占比高 → "机构真金白银进场"，次日溢价与持续性统计显著优于普通上榜。
2. **机构+游资合力**：机构买入与知名游资买入**同日同股上榜** → 资金合力（机构定方向、游资点火），是高溢价组合；反之机构买 + 游资大幅卖 → 分歧。
3. **游资席位跟踪**：本地维护"游资→营业部"映射，生成每日席位事件流（谁买了谁、买了多少、是否连续操作），重点跟踪一线游资（章盟主/佛山系/量化系等）的**连续动作**而非单日。
4. **席位胜率画像**：滚动 60 日统计每个游资/营业部的**上榜次日溢价中位数与胜率** → 区分"接力型"（次日续强）与"一日游型"（次日砸盘）游资，只跟胜率高的。
5. **三日榜识别趋势拉升**：EXPLANATION 含"连续三个交易日…"的三日榜条目 → 区别于单日异动，识别趋势性资金（非一日游）。
6. **净买额排名 + 板块聚集**：全市场龙虎榜按净买额排序，若**同一题材多股同日上榜** → 题材资金强度确认，比单股更有信号价值。
7. **上榜次数聚合**：30 日回看窗口内多次上榜 = 活跃资金持续关注（区别于一次性的公告驱动上榜）。
8. **三维验证**：龙虎榜净买为正 + 当日主力资金净流入为正 + 属于涨停池/连板梯队 → 高置信度资金面共振；任一维为负 = 降级信号。
9. **时机选择**：龙虎榜是 T+1 盘后信号 → 次日**竞价强度**（高开幅度/竞价量）决定参与；机构净买 + 次日不破分时均线 = 低吸机会。
10. **风险校准**：游资一日游、营业部换手、机构席位也卖 → 对每类信号回测"上榜后 1/3/5 日收益分布"，用数据校准阈值，不迷信席位名号。

---

## 三、TOP 10 清单（免费可用 × 实战价值 × 落地难度）

| # | 方案 | 数据源/接口 | 实战价值 | 落地难度 |
|---|---|---|---|---|
| 1 | **个股+板块资金流** | 东财 push2 `fflow/kline/get`（分钟）、`fflow/daykline/get`（120日）、`clist/get` 板块（fs=m:90+t:2/3/1） | 主力/超大/大/中/小单全维度；板块轮动 + 价量背离信号，资金面评分主源 | 低 |
| 2 | **涨停四池 + 情绪温度计** | 东财 push2ex 四池（实测 2026-08-14 可用）+ 本地速算 | 涨停家数/连板梯队/炸板率/晋级率/封板资金/首末封板时间 —— 打板情绪全套 | 低 |
| 3 | **龙虎榜深化** | 东财 datacenter 三报表 + OPERATEDEPT_CODE=0 机构筛选 + 本地游资映射表 | 机构净买、游资席位跟踪、三日榜、净买额排名；与项目现有接入同款 | 低 |
| 4 | **腾讯五档行情底座** | `qt.gtimg.cn`（GBK，字段索引 [9]–[27] 买卖五档，实测可用） | 实时五档/PE/PB/市值/换手/涨跌停价，批量不封 IP，Level-2 近似基座 | 极低 |
| 5 | **本地 CYQ 筹码引擎** | 东财 push2his 日K（含换手率）+ 自移植 CYQ 算法（参考 akshare `stock_cyq_em` / [stock-sdk chips](https://stock-sdk.linkdiary.cn/api/chips)） | 获利比例/平均成本/90%·70%成本区间/集中度 —— 成本分布维度，与资金流"钱+筹码"双验证 | 中 |
| 6 | **新浪历史逐笔** | `transHis.php?symbol=sh600519&date=YYYY-MM-DD&page=N`（实测存活，GBK 表格翻页） | 历史逐笔成交（价/量/主动买卖性质），回测盘口强度、验证资金流真实性 | 中 |
| 7 | **同花顺涨停揭秘** | `data.10jqka.com.cn/dataapi/limit_up/limit_up_pool` | 涨停原因题材（reason_type）/封板成功率/板型/炸板次数 —— 涨停归因免费独有源 | 低 |
| 8 | **两融/大宗/股东户数** | 东财 datacenter `RPTA_WEB_RZRQ_GGMX` / `RPT_DATA_BLOCKTRADE` / `RPT_HOLDERNUMLATEST` | 融资余额趋势、大宗折溢价+买卖方营业部、股东户数减少=筹码集中 —— 资金面辅助 | 低 |
| 9 | **交易所官方龙虎榜备用** | 深交所 `szse.cn/api/report/ShowReport/data?CATALOGID=1842_xxpl`、上交所 `query.sse.com.cn/showTradePublicFile.do` | 东财被封时的权威一手降级源（含营业部席位） | 中 |
| 10 | **新浪资金流降级 + 重点监控池** | 新浪 `MoneyFlow.ssl_qsfx_zjlrqs`（日度四档）+ 东财 `mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json` | 资金流兜底（不同风控面）+ 交易所风险警示名单/异动监管信号 | 低 |

**落地顺序建议**：1→2→3 并行先行（全部东财 HTTP、一周可通，改动与现有接入同构）；4 作为行情底座随时可加；6/7/9/10 作为增强与降级；5 放 4.0 二期（算法移植工作量最大、独有性最高）。

---

## 参考链接
- [simonlin1212/a-stock-data（43+ 端点实测代码）](https://github.com/simonlin1212/a-stock-data)
- [AKShare 个股资金流（push2 fflow 封装）](https://cloud.tencent.cn/developer/article/2236726)
- [AKShare 筹码分布说明](https://zhuanlan.zhihu.com/p/662935398) / [akshare 源码](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_cyq_em.py)
- [stock-sdk chips（筹码算法 TS 移植）](https://stock-sdk.linkdiary.cn/api/chips)
- [quantskills/skill-smart-money-profiler（LHB 席位画像）](https://github.com/quantskills/skill-smart-money-profiler)
- [hjhgogo/stockapi（免费聚合 API）](https://github.com/hjhgogo/stockapi)
- 游资静态名单：[东财财富号一览表](https://caifuhao.eastmoney.com/news/20250209224756905041420) / [龙虎榜元老及席位](https://caifuhao.eastmoney.com/news/20251114025804913640970) / [360doc 各路游资席位](http://www.360doc.com/content/24/0502/06/52230674_1122117303.shtml) / [淘股吧席位一览](https://m.tgb.cn/a/1Y24ItWmSB4)
- [mootdx](https://github.com/mootdx/mootdx) / [easy_tdx](https://zread.ai/handsomejustin/easy_tdx) / [eltdx（通达信逐笔/五档）](https://github.com/electkismet/eltdx)
