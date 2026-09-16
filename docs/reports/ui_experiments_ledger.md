# W5 实验台账 UI — 交付报告（2026-09-01）

把 60+ 份研究资产从 markdown 坟场里捞出来：新增"🧪 实验台账"页，自动索引 `data/bt_*.json`（42 个）+ `docs/reports/*.md`（90 个）+ `docs/backlog.md`（23 个任务行 + 11 条风险看板），一屏看全"哪个结论可信、状态如何、证据在哪"。

## 0. 查重门

- `web/js/experiments.js` 开工前不存在 → 正常执行，未记 dup。

## 1. 改动 diff 汇总

| 文件 | 改动 | diff | 说明 |
|------|------|------|------|
| `app/server.py` | 新增 `elif api == "experiments"` 分支（overview 之后） | 1192 → 1215 行，**+23 / -0** | 读 `data/experiments_index.json` 缓存；`?refresh=1` 触发只读索引器重扫（subprocess，180s 超时）。**另：全文行尾 CRLF→LF 转换（内容零改动，`tmp/w5/server.py.orig` 备份验证内容等价）** |
| `web/index.html` | 3 处新增：①侧栏 nav-item `🧪 实验台账`（settings 前）②`<section id="page-experiments">` 骨架（40 行）③`<script src="js/experiments.js">` 引入 | 809 → 859 行，**+51 / -1** | 页面切换由 app.js `switchPage` 自动处理（`data-page` 钩子），**app.js 零改动**。行尾 CRLF→LF（备份 `tmp/w5/index.html.orig`） |
| `web/js/app.js` | **未动** | 0 行 | 新页面靠 experiments.js 预渲染 + app.js 既有 `switchPage` 逻辑，无需侵入 |
| `web/css/app.css` | 末尾追加 `.exp-*` 样式（56 条） | 561 → 618 行，**+57** | 纯追加，不改既有规则 |
| `tools/experiment_scan.py` | **新文件** | 438 行 | 三源索引器（见 §2），纯 stdlib、只读 |
| `web/js/experiments.js` | **新文件** | 303 行 | 独立命名空间 `window.ExperimentsLedger`，全部 `textContent` 渲染（XSS 安全） |

## 2. 架构

```
tools/experiment_scan.py  (只读索引器, ~86ms 全量)
  ├─ data/bt_*.json    → meta/verdict/judgement/generated_at/snapshot/cells/key_numbers
  │                       (schema 不统一: verdict dict|list / judgement dict|list / table /
  │                        cells_done<total=running / JSON 解析失败=running)
  ├─ docs/reports/*.md → 标题 / 报告状态(status) / 结论倾向(conclusion) / 预注册时刻 / 判据行 / P编号
  └─ docs/backlog.md   → P 任务表(23 行) + 遗留风险看板(11 条, 编号列表)
        ↓
data/experiments_index.json  {generated_at, scan_ms, counts, experiments[155], backlog_risks[11]}
        ↓
app/server.py  GET /api/experiments  (读缓存; ?refresh=1 → subprocess 重扫)
        ↓
web/js/experiments.js  统计条 / 主表(8 列) / 状态徽章 / 关键数字迷你条 / 过滤(状态·P编号·关键词) / 排序(日期·结论强度) / 声明库子页签
```

- **判定语义双维度**：`status`（报告/实验自身状态）+ `conclusion`（结论倾向）。例：`bt_act12_t4_pit` → status=failed（PIT 判据不通过）、`docs/reports/act12_t4_pit.md` → status=passed（报告完成）+ conclusion=failed（结论不通过），避免"报告完成"与"结论通过"混淆（P77 符号病教训）。
- **关键数字迷你条**：优先取 `judgement_pit[].bull_loss` / `bull_delta_pp` / `delta_min`，纯 CSS 正负条（≤25pp 刻度），不依赖 echarts。

## 3. 验证证据（重启前 + 重启后）

| 项 | 结果 |
|----|------|
| `py_compile` server.py / experiment_scan.py | ✅ 通过 |
| 路由单元级验证（未启动服务，直接调用 `Handler._api_get("experiments", …)`） | ✅ 无 refresh 返回 155 实验；`?refresh=1` 触发重扫成功（修复了 `sys` 未导入 bug 后验证通过） |
| 前端冒烟（node + 最小 DOM stub，加载真实 experiments.js 跑 init→fetch→render） | ✅ tbody 156 行 / 统计卡 6 / P编号选项 64，`SMOKE PASS` |
| `node --check web/js/experiments.js` | ✅ 语法 OK |
| index.html 结构断言 | ✅ 12 section 平衡闭合，experiments 在列 |
| 真实服务静态可达（旧进程读磁盘） | ✅ `/js/experiments.js` 200、`/` 含 page-experiments |
| 真实服务 `/api/experiments` | ⚠ 404（**旧进程无此路由，需重启后生效**） |
| 全改动文件行尾 | ✅ CRLF=0（server.py / index.html 已转 LF，css 本就 LF） |
| **重启后 `/api/experiments`** | ✅ HTTP 200，len=83,522，**156 实验**，counts 完整（bt_json 42 / reports 91 / backlog 23 / passed 32 / failed 10 / running 0 / pending 5 / partial 2 / recent7 114），generated_at=2026-09-01 00:35:21，backlog_risks=11 |
| **重启后 `?refresh=1` 重扫** | ✅ HTTP 200，420ms，**161 实验**（重扫捕获 W3 并发新产出的 bt_*.json），generated_at=2026-09-01 00:49:34 |
| **重启后静态资源** | ✅ `/js/experiments.js` 200（12,200B）；`/` 含 `page-experiments` / `实验台账` nav / `experiments.js` 引用 |

## 4. 重启批准矩阵

- **状态：✅ 已批准、已重启生效**（用户回复"批准"，2026-09-01 00:45 后执行）。
- 重启窗口：凌晨 00:45（00:20-06:00 内），无交易、无收盘更新、无计划任务，符合最安全窗口。
- 实际执行（含一次过程修正）：
  1. 停旧进程（PID 28260）→ 首次 `python main.py`（默认 pywebview 桌面窗口）启动成功但**窗口在无头后台环境被关闭 → 服务随即退出**（日志"窗口已关闭，退出服务"），端口虽短暂 LISTENING 但连接即断——证明后台环境必须无窗口模式。
  2. 改用 `main.py --no-window`（L149 参数，仅启动服务不打开窗口）重启 → 进程 PID 44104（00:48:51）→ `/api/overview` HTTP 200 就绪。
  3. 新路由/静态资源全量验证通过（见 §3 重启后行）。
- 服务现状：`--no-window` 常驻（PID 44104），`http://127.0.0.1:8899/` 可访问；TianjiService_Watchdog 仍守护（重启窗口已过，属批准后的受控变更）。

## 5. 用户自检清单（重启后手点 5 步）

1. 打开 `http://127.0.0.1:8899/`，侧栏出现 **🧪 实验台账**，点击进入（显示 `display:""` 页面切换正常）。
2. 顶部统计条显示 5 个数字 + 索引时间（重启后实测：161 实验 / ✅32 ❌10 ⏳5 近7日 114 量级，随重扫变化）。
3. 主表按日期倒序渲染；行内状态徽章（✅/❌/⏳/—）、关键数字迷你条、判定摘要可见；搜索"act12"能过滤出 PIT 复验相关行。
4. 状态过滤选"❌ 否决/不通过"、P 编号下拉选某 P（如 P73），表随之过滤；排序切"按结论强度"变化。
5. 点"↻ 刷新"按钮（约 1-2s 后索引时间更新）；切"声明库"子页签看到风险看板 11 条 + overstatement 行。

## 6. 已知局限（如实披露）

1. **bt_*.json schema 不统一 → 降级策略**：有 `verdict`/`judgement` 的按字段判（enabled/passed/pass/rule 文本）；只有 `table`/`runs` 的无判定字段（如 `bt_it_snapshot_retest`）→ status=unknown，仅给 delta 摘要；JSON 解析失败 → 标 running（可能被并发 writer 生成中）。
2. **报告状态启发式**：`> 状态：` 行缺失的报告（90 份中多数）靠标题/正文关键词判 status（完成/待办），存在误判可能；`conclusion` 取正文 6000 字内"不通过/通过"关键词，个别报告可能双向命中（优先判不通过）。解析结果以索引 JSON 为准，可随时 `?refresh=1` 重扫。
3. **P64 overstatement 规则解析**：仅能抽到 `key_numbers` 中带 `overstatement`/`beautify.overstatement_pp` 键的实验（如 act12/board_pit_audit）；未解析到的留空位显示"未解析到 overstatement 数据"，不硬编数字。
4. **报告链接**：`md_path` 显示为文本路径（静态服务只供 `web/`，`docs/reports/` 不可直达），不提供跳转——用户可在项目目录打开。
5. **前端过滤/排序为内存态**（156 行全量渲染），刷新按钮重新拉取；性能实测索引 86ms，首屏 <1s 满足。

## 7. 交付物清单

- `tools/experiment_scan.py`（新）
- `web/js/experiments.js`（新）
- `app/server.py`（+23 行，CRLF→LF）
- `web/index.html`（+3 处，CRLF→LF）
- `web/css/app.css`（+57 行追加）
- `data/experiments_index.json`（索引产物，156 实验）
- 备份：`tmp/w5/server.py.orig` / `tmp/w5/index.html.orig`（原 CRLF 版）
- 测试：`tmp/w5/fe_smoke.js`（node 冒烟）
