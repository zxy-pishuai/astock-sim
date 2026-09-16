# 外部项目评估｜vibe-astock 能否接入 A股模拟盘系统

- 时间: 2026-08-27 收盘后
- 结论: **可以接入，建议只取其数据底座（打板层+信号层），不必迁入看板本体**
- 许可: a-stock-data / vibe-astock 同作者（Simon Lin, simonlin1212），**Apache 2.0**——vendor 无障碍
- 昨日待办: 无（本次纯评估+验证物料）

## 1. 项目是什么

- **[vibe-astock](https://github.com/simonlin1212/vibe-astock)**（看板层）：A 股短线复盘看板——涨停池·连板梯队·龙虎榜·板块资金一屏看完；赚钱效应/晋级率/梯队断层/情绪周期等**派生指标纯计算直出（不经 AI）**，AI 只负责把数据串成盘面研判 narratives。全本地运行，无 API key。
- **[a-stock-data](https://gitee.com/simonlin1212/a-stock-data)**（数据层，v3.6.1）：自包含 **Skill 文件 = 结构化 Markdown + 内嵌 Python**（40 端点 / 13 数据源 / 10 层架构），零第三方数据封装依赖（已移除 akshare，V3.0 起全直连 HTTP）。已钉纹到 `data/vendor/astock/upstream/{SKILL.md,README.md}`。
- 两者关系：vibe-astock 的数据底座 ≈ a-stock-data 的端点集（尤其 **打板层** 东财四池 + 同花顺涨停揭秘）。

## 2. 现场 live 验证（本机，绕开离线代理直连国内源）

| 端点 | 结果 |
|---|---|
| 东财涨停池 `getTopicZTPool`（push2ex） | ✓ 今日 **77 只**涨停（捷荣技术 3 连板） |
| 东财炸板池 | ✓ 17 只（炸板率 18.1%） |
| 东财跌停池 / 昨日涨停池 | ✓ 3 只 / 晋级率 30.8% |
| 同花顺涨停揭秘 | ✓ 题材归因＋板型＋封板率（例：一拖股份"扣非增长+农机出海+高端农机+无人驾驶"换手板） |
| 连板梯队 | ✓ {1板:61, 2板:8, 3板:5, 4板:1, 5板:1, 6板:1} 最高 6 板 |

验收物料：`data/vendor/astock/zt_pools_20260827.json`（涨停/炸板/跌停/昨涨停 + 题材 + 情绪速算全量快照）。
**坑记录**：① 东财四池价格字段 /1000；② `date` 仅支持**最近交易日**（20260825 反而空池——push2ex 明显只保留近期日历，历史回补不能靠它）；③ 非 `rc=0` 表参数错；④ 同花顺 `first_limit_up_time` 是 Unix 秒。

## 3. 与本系统互补性分析

| 能力 | 本系统现状 | vibe-astock/a-stock-data 补足 |
|---|---|---|
| 涨停池/封单/炸板/连板梯队 | ❌（zt_ecosystem.py 用自家 K 线推的温度计，P45 后口径已复核但缺"事实涨停池"外部验证） | ✅ 东财官方口径 + 封单资金 + 首末封板时间 |
| 涨停题材归因 | ❌ | ✅ 同花顺 reason_type（独家能力） |
| 龙虎榜（个股+全市场） | ❌ | ✅ §3.5/§3.8 席位 TOP5 + 机构动向 |
| 板块/个股资金流 | ❌（仅 amount） | ✅ push2 分钟级 + 120 日日线 |
| 北向资金 | ❌ | ✅ §3.2 实时分钟流 + 本地自缓存历史 |
| 情绪周期/赚钱效应 | 部分（P45 涨停生态温度计、计划任务扩展过） | 口径互补：炸板率/晋级率/梯队断层可直接注入既有温度计 |
| 历史复盘 | ✅ min5.db + market.db 自有优势，PIT 可信 | ❌（外部接口无历史深度） |

明确结论：**外接的独有价值=盘中/盘后实时族数据；历史 K 线、复权、回测完全不必引入**（mootdx/腾讯行情层与我们既有 datafeed 重复）。

## 4. 建议接入方案（分三级，供选择）

- **T1（已做，验证物料）**：`data/vendor/astock/zt_pools_{YYYYMMDD}.json` sidecar + 上游 SKILL.md 钉档。
- **T2（建议采纳，正式接入）**：新增独立工具 `tools/vendor_astock_daily.py`（不动 app/config.py）：
  1. 收盘后拉 打板层四池 + 涨停揭秘 → sidecar JSON（同 T1 格式）；
  2. 遵守上游**防封铁律**：东财请求走统一节流（间隔 ≥1s + 随机抖动 + 会话复用），大陆住宅 IP 间歇风控有预案（同花顺已验证可替代拉题材）；
  3. 挂**新计划任务**每日 15:05（先例 A3_TRIAGE_qfq_weekly 注册方式），与 AStockForwardEvalDaily（18:30）错峰，互不重启；
  4. `zt_ecosystem.py` 温度计可**并行对照**外部池数（只读 sidecar，不进交易路径）。
- **T3（可选，叙事层）**：把 vibe-astock 的"AI 串研判"思路接进现有 DeepSeek harness 会话——sidecar JSON 喂给会话产 `docs/reports/ztpan_daily/` 盘面研判 md。看板 web UI 本体不迁（DeepSeek Web GUI 已有看板能力）。

## 5. 风险与边界

1. **代理依赖**：GitHub 直连本机不可达（Clash 离线时 10061）——上游更新经 Gitee/GitCode 镜像（simonlin1212 账号有镜像）或恢复代理后拉。
2. **接口漂移**：作者 changelog 显示端点频遭失效改造（V3.0→V3.6 连环替换），**锁定钉档版本 v3.6.1**，每季度人工比对新版 changelog 再升级，禁止自动拉最新 SKILL.md。
3. **东财非官方接口**：仅作研究/复盘参考输入，严禁进实盘交易路径（与 zt_ecosystem 同级定位）。
4. **历史缺口**：涨停池/龙虎榜历史需另行找源回补（push2ex 无远期日历），如需构成"状态标签"须另行 PIT 评估后再接策略层。

## 5. 复现

- 验证脚本已即用即删；核心调用：`push2ex.eastmoney.com/{getTopicZTPool|getTopicZBPool|getTopicDTPool|getYesterdayZTPool}?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&date=YYYYMMDD`（headers 带 UA+Referer）；同花顺 `data.10jqka.com.cn/dataapi/limit_up/limit_up_pool`。
- 上游 v3.6.1 SKILL.md/README 已整文件 vendor 在 `data/vendor/astock/upstream/`。

## 6. ★ 修正（2026-08-27 晚，源码核对后）

初版低估了本系统既有覆盖。逐文件核对后：

- **涨停四池/涨停题材归因本系统已有**：`app/limitup.py` 用**同一组端点**（push2ex 四池 +
  `data.10jqka.com.cn` 涨停揭秘），且工程化更完整（mem/DB 双缓存、当日实时刷新 TTL 120s、
  周末脏写清洗门、`backfill()` 历史回补）——ztpan 数据与 a-stock-data 打板层基本等价，
  初版 T2 建议与新 sidecar 属**重复建设**，作废。
- **龙虎榜/两融/北向/主力资金流/板块资金流已有**：`app/moneyflow.py`（龙虎榜明细+席位深化
  4.5/游资合力/席位画像 fuse 座）+ `fflow.py`（个股主力分钟级+120日）+ `sector_flow.py`（板块）。
- **SKILL 真正独有的增量（本系统缺口）只余：** 解禁日历、大宗交易、股东户数、分红送转、
  个股/行业研报+PDF+一致预期EPS+PEG、ETF期权T型报价+IV、互动易问答、
  同花顺热榜/东财人气榜/个股概念命中、iwencai NL 搜索、东财 slist 概念板块归属。
- β 结论更新：本系统的数据源基座（tdx 加速+腾讯+新浪+东财降级链+快照 PIT+复权修复）。
  强于 a-stock-data 的"无库函数库"；外接价值从"实时族"收窄为上列**事件/基本面补充族**。

## 合规

- 未改 `app/config.py`、未改 app 代码、未重启服务；market.db / 快照零接触。
- 本次属只读外部拉取 + 新增 sidecar 与评估文档。
