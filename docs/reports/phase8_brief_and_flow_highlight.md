# Phase 8：新闻页空白修复 + 盘前简报弹窗 + 资金流新闻高亮

日期：2026-08-22（周六休市）

## 1. 新闻页空白 bug（已修复）
- 根因：phase7 写入不完整——app.js 只有事件绑定，`initPnews/refreshPnews/renderPnews/startPnewsTimer/_pnEsc`
  五个函数定义缺失，点击「📰 盘前新闻」时 switchPage 抛 ReferenceError，页面空白。
  后端接口本身正常（实测 200 + 30 条）。
- 修复：补写全部五个函数（重要度着色、AI/规则标记、点击展开、60s 轮询接 AI 分），node --check 通过。

## 2. 盘前简报自动弹窗（★4.8）
- 后端 `premarket_news.premarket_brief(force)` + 路由 `/api/news/brief`：
  非交易日返回 enabled=false（走 trading_calendar）；只取重要度≥3 的前 12 条；120s 缓存。
- 前端 checkBrief：启动 2.5s 后 + 每 30s 检查，仅在 06:00–09:40 窗口且当天未弹过才弹（localStorage 记日期）；
  空结果 5 分钟重试节流。新闻页加「📋 盘前简报」按钮可随时手动弹（force 忽略交易日）。
- 弹窗内容：分数着色 + AI 影响说明 + 分类/来源/时间，一键跳转新闻页。

## 3. 资金流新闻关联高亮（★4.8）
- 后端 `premarket_news.news_sector_hits()`：新闻关联个股代码经 sector_map 映射到行业名，
  另对标题/正文直接匹配行业名；60s 缓存，只用现有新闻缓存（空则后台线程预热，不阻塞资金流请求）。
- `/api/sector/flow` 响应增加 `news_sectors` 字段。
- 前端：桑基图关联板块金边 + 📰 前缀，tooltip 显示关联新闻条数，
  TOP6 列表行加 📰 角标，页面顶部显示「📰 盘前新闻关联板块：…」热度排序行。

## 4. 验证记录（2026-08-22 晚）
- brief 自动请求（周六）→ enabled=false reason=非交易日 ✅
- brief?force=1 → 12 条≥ 3 分新闻 ✅
- sector/flow → news_sectors 返回 7 个关联行业，与 TOP6 实际重叠（家电行业/机械行业）✅
- premarket 回归：30 条、三源全活 ✅；前端资源均含新代码 ✅
- switchPage 全部函数定义完整性检查 ✅；HTML 所有新增 ID 齐全 ✅

## 5. 注意
- 需重启软件生效（python main.py）。
- 周末休市：自动弹窗不会触发（非交易日），可用新闻页「📋 盘前简报」按钮手动体验。
- 板块名匹配基于 sector_map 行业名，概念板块页暂不参与高亮。
