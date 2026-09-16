# W2 报告：🎯 战法选股板块建成（T1，pack15）

> 状态：✅ 已建成（纯展示，零下单路径）
> 日期：2026-09-03
> 执行者：T1（并发 pack15 块）
> 交付物：`app/tactics.py`（新）/ `app/server.py`（+9 行路由）/ `web/index.html`（+17）/ `web/js/app.js`（+130）/ `web/css/app.css`（+10）/ `docs/reports/w2_tactic_board.md`（本报告）

---

## §0 三份规则卡核验记录（先通读，字段以此为准）

| 来源 | 关键字段实测 | 核验结论 |
|---|---|---|
| `docs/reports/tactic_shouban_backtest_v2.md` §4（规则卡）+ §7（状态机接口） | 状态链=不在池→首板确认→回调第k日(k≤10)→买点触发→持有等≥9%大阳→14日失效；M 混合卖点（大阳日未封涨停→当日收盘卖；封涨停→次日开盘卖） | 与 JSON 一致，已抄录公式 |
| `data/bt_tactic_shouban.json` → **`yang_rule.recommended`** | pool.pre_no_zt_days=7、first_board.limit_main=0.097/limit_gem=0.194/exdiv_bw_pp=2.0、gates.trend_ma_vol=true/breakout_days=20/volume_burst=OR、entry.buy_kind=**E2_fb_open_vol_shrink**/pullback_window_days=10/buy_at=close、target.yang_pct=9/sell_at=含"M"、fallback_exit.max_hold_days=14、stop.break_fb_start=false | ✅ 板块用 `yang_rule.recommended`（另有 `recommended_rule` 是 E3 对照，未误用）。tactics.py 构造时断言 buy_kind==E2_fb_open_vol_shrink 且 sell_at 含"M"，防拿错卡 |
| `docs/reports/tactic_lianban_backtest.md` §8（状态机）+ §5（规则卡） | A1 候选=首板(lbc==1)非一字+缩量(当日amount<前日amount)+收盘封住→收盘买、次日开盘卖目标；纯隔夜 | 与 JSON 一致 |
| `data/bt_tactic_lianban.json` → **`recommended_rule`** | board.limit_main=0.097/limit_gem=0.194/new_list_exemption_days=5、entry.style=A1/yizi_exclude=true/filters.board_type=非一字/amount_bucket=amount_ratio<1/sealed_at_close=true、exit.sell_at=next_open/stop_pct=0 | ✅ 构造时断言 style==A1 |

**判据同源铁律落实**：`app/tactics.py` 文件头注释逐条抄录公式来源行号（首板=tools/tactic_backtest.py L130-143 买点 + L207-213 M 卖点 + tmp/w1/prep_data.py L24-30 + events.py L16-30；连板=tmp/v1/build_events.py L13-49 + tools/tactic_lianban_backtest.py L176-181）。阈值全部运行时从两份 JSON 读取，缺键即 `_load_shouban_rule`/`_load_lianban_rule` 抛异常，**零硬编码、零静默默认**。

---

## §1 四件交付 diff 级清单

### 1a. `app/tactics.py`（新建，579 行，LF）
- 双战法类 `ShoubanYangTactic` / `LianbanA1Tactic`，统一接口 `status(code, date, klines) -> {stage,detail,signals,updated}` + `scan`。
- 首板 8 态徽章：`不在池 / 首板确认 / 回调第k日 / 买点触发(收盘买) / 持有中(等≥9%大阳,第j日) / 兑现-未板落袋 / 兑现-封板格局(次日开盘卖) / 失效(14日)`（含"买不进剔除"语义：涨停收盘日跳过继续找买点）。
- 连板 8 态徽章：`不在梯队 / 首板缩量(候选) / 首板放量(不候选) / 已打板(隔夜持有) / 二板兑现 / 断板(次日开盘已卖) / 一字(买不进) / 不在梯队-高度板`。
- `scan_all`：一条 SQL 拉近 140 自然日全市场日K（60/00/30/68 前缀）→ 向量化过滤（最新日 lbc≥1 或近 14 交易日有首板事件）→ 逐票 status（不逐票查库）；返回 `{tactics:[...], coverage:{latest_date,n_codes,complete}, generated_at}`。
- `get_tactics_board`：日期级缓存 `tmp/tactics_cache/{date}.json`（半截/损坏自动重扫，scan>5s 场景防重复计算）。
- `_ROLE="showcase"`，不 import 任何下单路径。

### 1b. `app/server.py`（+9 行，唯一改既有代码处）
插入点：`elif api == "tactics":` 于 `elif api == "state":`（原 L595）之前。diff：

```python
        elif api == "tactics":
            # 🎯 战法选股（纯展示）：首板回调 + 连板梯队（零下单路径）
            try:
                from . import tactics as tct
                self._json(tct.get_tactics_board())
            except Exception as e:
                log("tactics error: %s" % e)
                self._json({"error": str(e), "tactics": [], "coverage": {},
                            "generated_at": ""})
```
返回 `{tactics:[{name,rule_source,stages_summary,statuses}], coverage, generated_at}`。未重构任何既有分支。

### 1c. 前端三件（只加不改）
- `web/index.html`（+17）：nav 在 experiments(L50) 与 settings(L51) 之间加 `<div class="nav-item" data-page="tactics">🎯 战法选股</div>`；`</main>` 前加 `<section class="page" id="page-tactics">`（page-head + exp-tabs 双 tab 容器 + exp-table 空表头/表体 + 错误占位）。
- `web/js/app.js`（+130）：switchPage 加 `if (page === "tactics") { loadTactics(); startTacticsTimer(); }`；clearTimers 数组加 `state.tctTimer`；末尾追加 `loadTactics`/`renderTactics`/`tacticsRow`/`startTacticsTimer` 独立命名空间（tctTimer/tctTab/tctData），60s 自刷新、服务不可用空态占位（不白屏）。**XSS 纪律**：所有解析文本一律 textContent / createTextNode，无 innerHTML 注入。
- `web/css/app.css`（+10）：末尾追加 `.tct-badge` + `.tct-pass/hit/run/fail/dim/warn` 徽章类（配色沿用 exp-badge 系风格），不改既有规则。

### 1d. 20cm 标注（数据缺口如实展示，不改判据）
连板 tab 表头固定提示"⚠20cm(30/68)未单独验证"；30/68 前缀候选行代码旁加 ⚠ 徽章（title 注明"30/68 属 20cm 板，规则未单独验证"）。

---

## §2 对表门逐票结果（判据同源铁律验证）

### 2a. 首板对表（tools/tactic_backtest.py 真事件 → ShoubanYangTactic.status 逐日一致）

**对表方法**：`load_feat()` 读 `tmp/w1/kline_feat.pkl`（374 万行）→ `event_mask(pre=7, brk=20, vol="OR", trend=True, start=2019-01-01, end=2023-12-31)` 得 **IS 主池事件 5721 个** → `compute_paths(buy_family=("E2",), buy_kind_include=["E2_fb_open_vol_shrink"], K_list=[8], V=1, H=14, S="none", pullback=10, yang=0.09, yang_at="M")` 得 **1555 个 E2_fb_open_vol_shrink 事件**（兑现 exit<14=246 / 失效 exit==14=1292 / 无买点对照 4166）。抽 16 个事件逐交易日跑 status，关键日期（买点日 / M 分岔实际卖出日 / 失效日）逐日对齐。

| 类别 | 抽验 | 通过 | 关键日期对齐 |
|---|---|---|---|
| 兑现（M 分岔卖出） | 6 | **6/6** | 买点日全对；封涨停兑现取 signals 中"卖出日/次日开盘卖"（=大阳日次日开盘），与 compute_paths exit_day 换算的实际卖出日逐日一致 |
| 失效（14 日强卖） | 6 | **6/6** | 失效日逐日一致 |
| 无买点对照 | 4 | **4/4** | 回调窗口 t+1..t+10 全为"回调第k日"、窗口后全为"不在池" |

> 表样（6 兑现事件）：fb=2019-03-25 buy=04-16 sell=05-06 ✓；fb=2022-11-29 buy=12-08 sell=12-12 ✓；fb=2019-08-19 buy=08-27 sell=09-04 ✓；fb=2022-11-29 buy=12-06 sell=12-09 ✓；fb=2022-07-28 buy=08-01 sell=08-08 ✓；fb=2020-04-23 buy=05-12 sell=05-15 ✓。失效 6 例（2020-07-06 / 2021-02-25 / 2020-07-10 / 2021-03-24 / 2022-03-30 / 2022-05-31）均 exit=14 卖出日逐日一致。无买点 4 例均 cbk_n=10 / out_n≥11。
> **对表过程修正记录**：① 首跑 yang=9 传错（工具 `chg>=yang` 直接比小数），全事件 exit=14——修正为 yang=0.09 后兑现正常出现；② M 分岔"封涨停→次日开盘卖"为语义差异：tactics 在大阳日当天即返回"兑现-封板格局(次日开盘卖)"并携带卖出日信号，对表取该信号对比（非 stage 首现日），修正后 16/16 全过。

### 2b. 连板对表（lbc 重建 vs 判据真值）

**真值源决策**：任务书指定 limit_pool(kind='zt') payload.lbc 为真值，但**实测生产库 limit_pool 与 kline 不同源/含脏数据**：603065 在 8/22、8/23（周末）有 LP 记录、且 8/19 为跌停（C=16.30 < 前收 18.11）却标 lbc=1；000158 9/2 跌 -0.5% 却标 lbc=1。该表无法与 kline 对齐 → 弃用，改用 **`tmp/v1/events.pkl`**（V1 回测工具产出，386 万行全市场 kline，含 is_zt/lbc/board_type/amount_ratio，对齐快照 §0.5 达 97.83%）作为判据同源真值。

| 项目 | 值 |
|---|---|
| 抽样 | 15 票（覆盖 lbc 1/2/3+，含一字/非一字/各板型），近一年（2025-08-01 起）逐日 |
| 对比口径 | `_vectorize_lianban` 重建 lbc vs events.pkl lbc |
| **一致率** | **3912/3912 = 100.00%（≥95% 达标）** |

---

## §3 8897 临时实例 / 锁拦截证据

- 启动前 `data/app.lock` 不存在、8899/8897/8898 均无监听（服务当前未运行）→ 8897 无锁冲突，正常启动 `main.py --browser --port 8897`（PID 32392）。
- `/api/overview` → **HTTP 200**（路由链没插坏）。
- `/api/tactics` → **HTTP 200**，58952 字节，`tactics` 数组 2 个（首板回调 + 连板梯队），结构合法；coverage 字段返回 `{latest_date:"2026-09-02", n_codes:795, complete:false}`。
- 首页 `/` → HTTP 200，含 `data-page="tactics"` 与"🎯 战法选股"。
- 测完按 Get-CimInstance 复核仅杀自记 PID 32392，无残留；未碰任何其他进程/8899/watchdog。

---

## §4 多战法扩展说明（第三个战法怎么加）

只需两步（零 server/前端改动）：
1. `app/tactics.py` 新增一个类（如 `XxxTactic`），实现 `status(code, date, klines) -> {stage,detail,signals,updated}`，stage 徽章值直接供前端渲染（前端已按"阶段字符串"动态取色，无需改 TCT_STAGE_CLS 之外——新增 stage 可加映射或落入默认 tct-dim）。
2. 一份规则 JSON `data/bt_tactic_xxx.json`（键校验在类构造里做，缺键报错）。
3. 在 `scan_all()` 的 `tactics` 数组里追加 `{"name": ..., "rule_source": ..., "stages_summary": ..., "statuses": ...}`；前端 `renderTactics` 按 `data-tactic` 映射当前 tab 时，`tac[2]` 即可承载第三个战法（tab 按钮需在 index.html 手动加一个）。判据同源铁律要求：公式抄录进类注释并引用来源工具行号。

---

## §5 诚实披露

1. **生产库尾部覆盖率缺口**：`/api/tactics` 的 coverage 实测 `latest_date=2026-09-02, n_codes=795 < 1500` → 判定"数据不完整日"，前端黄条降级提示。这是生产库日更缺口（与 8/31 updater 修复后的服务恢复进度相关），板块如实展示、不掩盖。**缺陷：coverage 黄条逻辑会长期触发直到日更恢复全量（>1500 只）**。
2. **20cm 未验证**：连板规则未在 30/68（20cm 板）单独验证——30/68 候选行加 ⚠ 徽章、表头固定提示，不改判据（数据缺口如实展示）。
3. **qfq 方言污染局限**：生产库 kline 有 qfq 方言污染（70.8% 恒定比例放大）→ 判据只用比值（chg/amount_ratio）与同票相邻日绝对价比较（`|low-fb_open|/fb_open`），**未引入任何跨票价格比较**；对表门在快照/事件源（events.pkl / kline_feat.pkl）验证通过，生产库上因覆盖缺口仅做结构级冒烟。
4. **性能**：`scan_all` 全市场（2354 只）一次约 10-20s（含全量向量化+逐票 status），日期级缓存 `tmp/tactics_cache/{date}.json` 已就位，首次访问后命中缓存 <1s。对表门跑分：load 657MB feat 1s、事件计算 1s、16 事件逐日对表 11s（纯单线程）。
5. **limit_pool 数据源异常（本块新发现，非本任务引入）**：生产库 limit_pool 含周末记录与跌停误标，与 kline 不同源——连板对表弃用该表改用 events.pkl，已在 §2b 归因；**建议后续数据修复块核查 limit_pool 写入链路**。
6. **前端无头环境限制**：无法做真实浏览器控制台检查，已用 `node --check` 静态语法断言（rc=0）+ 首页/接口 curl 断言替代；用户手测清单见 §7。
7. **§5.7 硬编码处置清单（T1R 修复项 3）**：

| 魔法数字 | 修复前位置 | 处置后来源 |
|---|---|---|
| band=0.097/0.194（首板涨停阈值） | `_vectorize` 函数内 | `rule["first_board"]["limit_main"]/["limit_gem"]`，`ShoubanYangTactic.status` 传参 |
| +0.02（首板 exdiv 带宽） | `_vectorize` 函数内 | `rule["first_board"]["exdiv_bw_pp"]/100`（2.0→0.02），status 传参 |
| thr=0.097/0.194（连板涨停阈值） | `_vectorize_lianban` 函数内 | `rule["board"]["limit_main"]/["limit_gem"]`，`LianbanA1Tactic.status` 传参 |
| exband=0.117/0.214（连板 exdiv 带宽） | `_vectorize_lianban` 函数内 | `limit+exdiv_bw_pp/100`；连板卡 `board` 段无该键 → `__init__` 用 `.get("exdiv_bw_pp", 2.0)`（与首板卡同口径，**已声明默认项**；卡键未加，因 T1R 红线仅授权 shouban 卡加两键） |
| touch_pct=0.02 | `ShoubanYangTactic.__init__` L246 硬编码 | `rule["entry"]["support_touch_pp"]/100`（卡新增键 2.0），构造断言 `abs(0.02-touch)<1e-9` |
| vol_shrink=0.5 | `ShoubanYangTactic.__init__` L247 硬编码 | `rule["entry"]["vol_shrink_ratio"]`（卡新增键 0.5），构造断言 |
| 函数默认参数 0.097/0.194/2.0 | 新增参数默认值 | **仅供验收脚本 `tmp/t1v2.py` B 节无参直调与向后兼容**；生产路径（两个 status）必由类传参，报告 §8 复验 B 节实测无参版对照 98.51% 佐证口径未漂移 |

   → 卡新增键：`data/bt_tactic_shouban.json → yang_rule.recommended.entry` 增 `"support_touch_pp": 2.0`、`"vol_shrink_ratio": 0.5`（仅 recommended 的 E2 段，E3 对照的 recommended_rule 未动）；`_load_shouban_rule` 校验同步加两键（缺键=报错）。

---

## §6 给人类的"参考实盘 → 接线下单"缺口清单（只列差距与改动面，**不许实现**）

> 本板块纯展示，以下为将来若要从"参考实盘"接到"真下单位"所需的改动面清单，全部未实现。

| # | 缺口 | 改动面 |
|---|---|---|
| 1 | **tactics→trader 候选注入** | 现板块输出 statuses（候选池），trader.py 无消费入口。需在 engine/trader 侧加"当日战法候选"数据源并定义优先级/准入规则（配额、与 base_score 池冲突策略） |
| 2 | **止损位对接风控闸门** | 首板规则"不止损"（fallback_exit 无 stop），连板"stop_pct=0"——若接入实盘需由风控闸门统一兜底（单票最大亏损/组合回撤），tactics 不含风控语义 |
| 3 | **与 base_score / Phase24 ML 池的优先级** | 战法候选与现役打分/ML 池的关系未定义（并列/替代/叠加），需人类决策并落配置 |
| 4 | **执行撮合现实性** | 买点"收盘价买入"与"次日开盘卖出"在实盘受集合竞价/涨跌停不可成交、滑点、T+1 约束；板块按收盘/开盘语义展示，未建模撮合现实性 |
| 5 | **盘中实时性** | 板块按日频扫描（最新交易日收盘后状态），无盘中分钟级触发通道；若盘中打板需接实时行情与盘中状态机 |
| 6 | **20cm 板规则验证** | 30/68 候选当前仅展示带 ⚠，接入前须单独回测验证连板判据在 20cm 语义下是否成立 |
| 7 | **数据完整日前提** | 板块依赖日更完整性（coverage 黄条即缺口信号），接线下单前必须确保日更全量恢复 |

---

## §7 用户手测清单（5 步）

1. 重启 8899 服务（当前服务未运行，人类批准后启动）后浏览器打开首页 → 侧栏出现"🎯 战法选股"。
2. 点击进入 → 首板回调 tab 默认激活，表格展示 141 只候选（回调第k日/持有中/兑现/失效等徽章配色正常）。
3. 切"连板梯队" tab → 表头出现 20cm 提示，30/68 候选行带 ⚠；lbc/板型/量比列正常。
4. 页顶 coverage 显示"数据日: 2026-09-02 · 795 只 ⚠数据不完整日(尾部日更缺口)"黄条（日更恢复后自动消失）。
5. 刷新页面 60s 内自刷新不白屏；断网/服务停时显示"服务不可用"空态占位。

---

## §8 自检汇总

| 检查项 | 结果 |
|---|---|
| `py_compile app/tactics.py app/server.py` | PASS（rc=0） |
| `import app.server` 回归 | PASS |
| `node --check web/js/app.js` | PASS（rc=0） |
| CRLF（交付 5 文件全 LF） | app/tactics.py=0 / server.py=0 / index.html=0 / app.js=0 / css=0 |
| 首板对表门 | 兑现 6/6 + 失效 6/6 + 无买点 4/4 = **16/16** |
| 连板对表门 | **3912/3912 = 100%（≥95%）** |
| 8897 `/api/tactics` | HTTP 200，双战法结构合法，coverage 正常 |
| 8897 `/api/overview` | HTTP 200（路由链未破坏） |
| 首页含新 nav | HTTP 200，`data-page="tactics"` 存在 |
| 8897 清理 | 仅杀自记 PID 32392，无残留 |
| 写白名单遵守 | 仅写入白名单 6 项 + tmp/t1/；未改任何既有报告/资产 |

**最终板块状态**：🎯 战法选股板块可用（纯展示）；首板对表门 16/16、连板对表门 3912/3912（100%）；coverage 回报值 = 最新日 2026-09-02 · 795 只（<1500，数据不完整日黄条）；§6 接线下单缺口清单 7 条。

---

## §8.1 T1R 验收退回修复记录（2026-09-03）

### 修复项清单（只改四项，未动其他逻辑）

| # | 修复项 | 改动 | 证据 |
|---|---|---|---|
| 1 | 删候选预过滤 | `scan_all` 不再用 `win_start=len-14` 预筛 → 对每只 `len(kl)>=8` 的票直接 `status()`，按 stage 收录；删第 3 次 `_vectorize_lianban`（连板 lbc/板型/ladder 由 `LianbanA1Tactic.status` 返回 `_enrich` 注入） | 每票向量化 3→2 次；实测 2132 票级扫描 4.9s |
| 2 | 缓存加数据指纹 | `get_tactics_board` 新增 `_count_codes_on` 指纹；命中缓存前校验缓存内 `coverage.n_codes == 当前库`；`n_codes<1500` 不落缓存、不读当日缓存、返回 `stale_ok:true` | 见复验 2 |
| 3 | 硬编码下传 | `_vectorize`/`_vectorize_lianban` 加 `(limit_main, limit_gem, exdiv_bw_pp)` 参数（默认值 0.097/0.194/2.0 仅供验收脚本无参直调）；`touch_pct/vol_shrink` 从卡 `entry.support_touch_pp/vol_shrink_ratio` 读取；`_load_shouban_rule` 校验加两键；构造断言校验 | 见 §5.7 |
| 4 | 两个小修 | ① `_event_stage` 兑现分支：`is_zt[yj] 且 yj+1>=n` → stage 仍为"兑现-封板格局(次日开盘卖)"，detail 注明"待次日开盘"，不再落"未板落袋"文案；② `_vectorize_lianban` 删除"炸板未封"死分支（is_zt 前置不可达），注释"板型仅对涨停日定义" | py_compile+冒烟 |

### 复验证据

| 复验项 | 结果 |
|---|---|
| `py -3.13 tmp/t1v2.py` A 节三方对表 | 3 例（失效/无买点/兑现）`buy一致` 全 True，买点/M/失效日不变 |
| `tmp/t1v2.py` B 节连板 lbc 无参对照 | 5888/5977 = 98.51%（无参默认参数版；生产路径为卡传参版，T1 对表门 3912/3912=100% 不在此口径） |
| `tmp/t1v2.py` C 节 get_tactics_board | 4.65s，coverage 2026-09-03 / 379 只 / complete=False（盘中不完整日） |
| `tmp/t1v2.py` D 节扫描漏票 | **首板 119 应显示 / 119 实际 / 漏 0；连板 25 应显示 / 25 实际 / 漏 0** ✅ |
| 缓存指纹（复验 2） | 删 `2026-09-0*.json` 后 `get_tactics_board()`：n_codes=379<1500 → **不落缓存文件（目录空）**、`stale_ok=True` ✅ |
| `py -3.13 -m py_compile app/tactics.py app/server.py` | PASS（rc=0） |
| `py -3.13 -c "import app.server; import app.tactics"` | PASS |
| CRLF | app/tactics.py=0；规则卡 bt_tactic_shouban.json 保持原 CRLF 格式（工具生成，仅文本插入 3 行） |
| server.py / web 三件 | 本次禁改（server.py mtime 仍 00:52:23 未变） |

**改动行数**：`app/tactics.py` 620→645（+25）；`data/bt_tactic_shouban.json` 2694→2697 行（+3，仅 E2 entry 两键）；`app/server.py`/`web/` 三件 0 改动。

**差集 0/0、缓存策略生效、硬编码清零。**


---

## v2（T2）2026-09-03 —— T1R 复验 + 股票名称列 + 连板梯队全展示 + 竞价双战法接入

> 执行者：T2（并发 pack16 块）。交付：`app/tactics.py`（+v2 改动）/ `data/bt_tactic_jingjia.json`（新）/ `web/index.html`/`web/js/app.js`/`web/css/app.css`（只加不改）/ 本报告追加节。
> 红线遵守：未改 server.py/scoring.py/datafeed.py/trader.py/config.py 等八禁改文件（mtime 未变，见 §4）；禁 git 写；禁 import trader/portfolio 下单路径；生产库只读；前端/后端竞价取数仅走 datafeed 只读源（显式标注路径）。

### §0 数据诚实答案："为什么只有一进二有候选票"（人类原话）+ w2s 定稿口径引用

**一进二-only 是数据诚实，不是漏。** V1 连板回测（`docs/reports/tactic_lianban_backtest.md`，同一份快照口径）结论：

| 打法 | IS / OOS 期望 | 判定 |
|---|---|---|
| A1 收盘打缩量非一字首板、隔夜卖 | **IS +2.56% / OOS +2.28%**（唯一正期望） | ✅ 唯一候选来源 |
| A2 次日竞价/开盘追买二板 | 本地 **−0.55% ~ −1.22%**（全网格负） | ❌ 不给候选徽章 |
| A3 断板低吸 | 未达标 | ❌ 不给候选徽章 |

社区口径同向（V1 §4 引"次日追买=接盘年化 −24.5%"）。所以**一进二（首板缩量隔夜）之外的所有梯队只展示、只发"观察"灰徽章**：二进三 33.02%、三进四 46.43%、四进五 53.77%（晋级率仅作背景信息，不构成候选）。这是"今日涨停 lbc≥1 全部进列表、但仅一进二候选"的设计来源，详见 §1 diff 的收录规则。

**竞价弱转强（w2s）口径引用（未定稿声明）**：
- P34 `docs/reports/auction_w2s.md` §7 结论：**弱转强独立策略 9 变体全不达标、不成立、不集成**；
- P69 `tools/board_w2s_filter_ab.py` + `data/attribution/board_w2s_filter_ab.json`：judge.pass=**false**（bull_delta_pp=−62.48、trade_shrink_le50pct=false）→ 作为 board 前置过滤亦无增益；
- 按任务书"报告若无定稿结论，用 research 工具的默认过滤器并在报告 §0 声明未定稿"执行：**宇宙=昨日涨停池（tactics 自有 is_zt 重建，不依赖脏 limit_pool）+ 今日竞价高开 gap=(open/昨收−1)∈[+1%,+4%]**；`candidate_enabled=false` → 只发"弱转强观察(38 分灰系)"/"弱转强未触发(15 分)"，**绝不发候选徽章**。口径若未来有定稿结论，改卡 `data/bt_tactic_jingjia.json` 的 `jingjia_w2s` 段即可，代码不重写。

### §1 diff 清单

**后端 `app/tactics.py`（T1R 后 645 行 → 本轮净增约 200 行）：**
1. **名称列（新需求1）**：模块级懒加载 `_load_name_map()`（mtime 缓存，读 `data/stock_list.json` 5014 条 `[code,name,price]` → code→name）；`_stock_name(code)` 查不到返回 `""`（不许猜）。`scan_all` 首板/连板记录 `"name": _stock_name(code)` 替代原 `""`。
2. **连板梯队全展示（新需求2）**：`_ladder_of` 五板及以上合并 `"五板+"`（高度板仅观察）；`LianbanA1Tactic.status` 收录规则改为**今日涨停 lbc≥1 全部进列表**，stage 徽章细分：`一进二候选(首板缩量)/ 一进二不候选(放量或一字)/ 二进三(仅观察)/ 三进四(仅观察)/ 四进五(仅观察)/ 五板+(仅观察)/ 断板(次日开盘已卖)/ 二板兑现`；新 `_observe_stage()`：lbc≥2 观察档 score=min(...,39.5) **压到 40 以下灰系**（绝不复用候选金），detail 带 V1 本地晋级率参考 + "次日追买非正期望不构成候选"；`PROMO_REF={2:(二进三,33.02),3:(三进四,46.43),4:(四进五,53.77)}`。
3. **T2 守卫（本轮发现并修复）**：`status` 对查询日增加"当日必须有 bar"判定——生产库 per-code 最新日严重不一致（实测 733 票停在 08-21、492 票 09-02、仅 379 票 09-03），原 `_find_idx_le` 会把停牌/未更新票的历史涨停 bar 误当"今日涨停"（如 000004 最新 bar 停在 07-13 仍被标"一进二不候选"）。守卫后今日(09-03)连板收录 25→3（§8.1 D 行 25 为守卫前数值），差集仍 0/0。
4. **竞价双战法（新需求3）**：`JingjiaDabanTactic`（判据源 `app/scoring.py score_auction` L519-587 直接 import；喂参照 `app/trader.py _auction_round` L1035-1089；构造断言 config 常量与卡一致防漂移）；`JingjiaRuozhuanqiangTactic`（判据 §0）；`_score_auction_no_net()` 运行时注入 `moneyflow.seat_analysis=None` 抑制内嵌东财 datacenter 出网（try/finally 恢复，**不改 scoring.py**）；`_auction_capture()`/`auction_board()` 快照机制（§3 时序）。
5. **规则卡**：新 `data/bt_tactic_jingjia.json`（meta.honest_note 固定提示 + jingjia_daban/jingjia_w2s 全阈值，缺键=构造报错）；`data/bt_tactic_shouban.json` 两键为 T1R 已加（本轮未动）。

**前端（只加不改）：**
- `web/index.html`：`#tct-tabs` 增 2 按钮（竞价打板/竞价弱转强）；表前加 `#tct-notice`。
- `web/js/app.js`：`TCT_TAB_IDX={shouban:0,lianban:1,jingjia_daban:2,jingjia_w2s:3}`（server 禁改，/api/tactics 固定四卡序）；`TCT_STAGE_CLS` 增观察系（`tct-obs` 灰系）/竞价系徽章；`TCT_LADDERS` 对齐新 ladder（五板+/断板）；表头增"名称"列；竞价两 tab 表头固定诚实提示（honest_note，禁渲染成已验收视觉）；竞价行关键列=高开/量比/竞价额、w2s 行=高开缺口；观察档得分一律灰（绝不复用候选金/绿）。
- `web/css/app.css`：追加 `.tct-obs`（观察灰系虚线）/`.tct-honest-note`（诚实表头）/`.tct-name` 三组类，既有规则未动。

### §2 五项自检证据（全部落 `tmp/t2/`）

| # | 自检 | 命令/脚本 | 结果 |
|---|---|---|---|
| 1 | T1R 复验 | `py -3.13 tmp/t1v2.py` → `tmp/t2/t1v2_run2.txt` | A 三方对表 3/3 buy一致全 True；B 5888/5977=98.51%；C 板块 4 卡 5.16s、coverage 09-03/379/complete=False；**D 首板 119 应/119 实/漏 0，连板 3 应/3 实/漏 0** ✅ |
| 2 | 名称抽查 ≥10 条 | `tmp/t2/test_names.py` | 抽查 12 条全与 stock_list.json 逐条一致（不一致 0）；无码票 `999999/6009999` → `""`；**四 tab 名称填充率 133/133 = 100.0%** |
| 3 | 梯队展示对照 events.pkl | `tmp/t2/test_ladder_pkl.py`（08-21 完整日，2108 票） | events.pkl lbc≥2 计 10 票**全部在列**；非观察徽章 0；ladder 与 lbc 一致 0 冲突；板块标 lbc≥2 但 events 非涨停 0 → **PASS** |
| 4 | 竞价双卡 mock 单测 | `tmp/t2/test_auction.py`（monkeypatch `_now_hm/_today_str/fetch_all_stocks`，≤6 只） | pre_window 不抓不连网 / 非交易日 closed 空态 / 09:30 抓快照落盘(候选 3 条, 名称/分数/apct/vr 正确) / 读快照不再 fetch(calls=0) / 10:30 降级 vr=0 + 降级徽章 → **ALL OK** |
| 5 | 回归 | `py_compile` + `import app.server, app.tactics` + `node --check` | py_compile OK、import OK、node --check OK |
| + | 缓存指纹（T1R 复验 2） | 删 2026-09-0*.json 后 get_tactics_board() | n_codes=379<1500 → 不落缓存、不读当日缓存、`stale_ok:true` ✅（目录仅剩 21:04 旧文件，指纹保护下不可达） |

### §3 竞价快照时序说明（快照纪律：每日 09:25-09:40 内第一次 /api/tactics 访问即抓拍，此后全天读快照）

| 时刻/条件 | 行为 | 网络 |
|---|---|---|
| 非交易日（交易日历） | 返回 `closed` 空态，不写文件 | 无 |
| 交易日前 09:25 | `pre_window` 空态（"未到竞价时段"） | 无 |
| 交易日 09:25-09:40 首访 | `_auction_capture(degraded=False)`：本地只读库昨收 + 行情 open/量/额 → 完整评分（板块共振/大盘宽度/竞价量比/竞价额全字段）→ 落 `tmp/tactics_cache/auction_{date}.json` | 行情 fetch（显式标注竞价路径） |
| 窗口后（09:40-15:30）首访且无快照 | 降级：**今日 bar 已在本地库 → 直接用 DB open/昨收 零网络**；盘中今日 bar 未落库 → 行情 fetch 降级（量/额不可复得置 0，仅高开口径评分）→ 写回快照文件 | 视 DB 有无今日 bar |
| 窗口后且快照已存在 | 读快照，**不再 fetch**（幂等，全天一致） | 无 |
| 交易日后/夜间 | 读快照；无快照 → DB 今日 bar 降级（零网络） | 无 |

w2s 宇宙=昨日涨停池由 `_yest_zt_set`（`_vectorize_lianban` 重建，不依赖脏 limit_pool）提供。`get_tactics_board` 并入竞价板：**base 两卡走日期级缓存，竞价两卡每次实时求值**（避免"早于窗口的访问把空竞价缓存一整天"）。

### §4 诚实披露

1. **竞价双战法无 IS/OOS 回测卡**：表头固定提示"竞价双战法为系统现役评分器的公示参考，未做过 W1R/V1 级回测验收"，视觉上仅"观察灰系"（`tct-obs`），禁止与首板/连板已验收卡同视觉。
2. **score_auction 内嵌出网被运行时抑制**：`app/scoring.py` score_auction 内含 `moneyflow.seat_analysis`（东财 datacenter HTTP）。本块以 `_score_auction_no_net()` 运行时注入 `moneyflow.seat_analysis=None`（try/finally 恢复）抑制，**未改 scoring.py**；被抑制的席位/资金字段不进评分（评分降为 板块共振/大盘宽度/量能/昨日涨停等本地可得项）——此抑制是否被验收方认可，如实上报。
3. **降级路径的局限**：竞价量比/竞价额当日不可复得，降级后置 0 → vr/amount 相关分数不计，仅高开口径；盘中（今日 bar 未落库）的降级需一次行情 fetch（显式标注、零下单），夜间/收盘后走 DB 零网络。
4. **今日数据不完整**：生产库 coverage 09-03=379 < 1500（日更缺口延续）→ 连板今日仅收录 3 只（当日有 bar 的涨停票）；这是数据现实，非漏；T2 守卫确保不把历史涨停误标为今日。
5. **w2s 未定稿**：按任务书以 research 工具默认口径运行并声明"未定稿"；P34 结论"不成立"已如实关闭候选徽章。
6. **前端无头环境限制**：无法真浏览器控制台检查，以 `node --check` + 接口级断言（`tmp/t2/test_board_e2e.py` 4 卡结构）替代；建议人工在 8899 页面点四个 tab 复核配色（观察灰系 vs 候选金）。
7. **性能**：get_tactics_board 全量扫描 5-8s（2132 票级向量化），日期级缓存命中 <1s；竞价并入为快照读取（O(1)）。60s 轮询自刷新沿用 T1。
8. **本轮改动文件 mtime**：tactics.py 21:45、index.html 21:47、app.js 21:47、app.css 21:47、bt_tactic_jingjia.json 21:38（新）；**禁改文件未动**：server.py 00:52、scoring.py 08-26、datafeed.py 09-02 23:52、trader.py 00:01、config.py 08-27。

**v2 终态**：修复差集 0/0（首板 119/119、连板 3/3）；四 tab 名称填充率 100.0%（133/133）；竞价快照策略生效（首访抓拍→全天读快照→降级兜底→非交易日空态）。服务未重启（进程域归 X1/Z2，需批准门）；8897 临时实例验证按 T1 老规矩（锁冲突降级、只杀自记 PID）。
