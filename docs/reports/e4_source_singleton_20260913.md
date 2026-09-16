# E4 关键数据"单点源"风险盘点 + 降级补偿钩子 报告

- 日期：2026-09-13（周日）
- 任务：E4（盘点 + 实现"降级即补偿"）
- 状态：✅ 完成（含验收 A/B/C 全过）

## §0 背景与三源定论（本块实测，非转述）

任务书称"腾讯/新浪结构上不含 amount（F1 已证实腾讯是 6 字段）"。本块实测：

| 源 | 日K字段 | amount | 实测证据 |
|---|---|---|---|
| tdx（mootdx） | open/close/high/low/vol/**amount** | **有** | app/tdx.py `_bars_one` L212 `r.get("amount")`；mootdx bars 带额 |
| 腾讯 fqkline | `[date,open,close,high,low,volume]` **6 字段** | **无** | 实测 `sh600000` 返回 6 字段；`datafeed._fetch_kline_tencent` L507 `e[6]` 永远取不到 |
| 新浪日K | `{day,open,high,low,close,volume}` 6 字段 | **无** | 实测 `sz000001` 返回 6 字段；`e.get("amount",0)` 恒 0 |
| 东财 push2his | `date,open,close,high,low,volume,**amount**,...` | **有** | 实测 7 字段，amount 第 7 位（806326225 与 A/V 校验吻合） |
| 腾讯 qt.gtimg.cn 实时 | 88 字段（成交额在 37 位，**万元**） | **有（当日）** | 实测 600000=60463 万=6.05 亿（与 volume×close 校验吻合） |

**结论：tdx 是唯一有额主源；一旦 tdx 熔断/失效，日K降级到腾讯或新浪，当日 amount 必然写 0（_kline_save 的 amount 保护只防覆盖旧值、不防新日期写 0）。**

## §1 数据源盘点表

| 数据 | 主源 | 备用 | 字段完整性 | 降级顺序 | 历史失效记录 | 被封风险 |
|---|---|---|---|---|---|---|
| 日K（个股） | tdx(mootdx) | 腾讯 fqkline → 新浪 | tdx 全（含 amount）；腾讯/新浪 **缺 amount**（6 字段） | tdx→腾讯→新浪（datafeed `_fetch_daily` L723-732 / updater `_fetch_daily_one`） | 8/21、8/24、8/28 `daily_errors=300` 全量失败（updater_repair_20260831.md）；6/4-6/22 000001 腾讯降级 amount=0（audit kline_amount_missing 236 条）；**本块实测 web.ifzq.gtimg.cn 当前 501**（ifzq.gtimg.cn / proxy.finance.qq.com 存活） | **高**（web 主机已见 501；TDX 无 8s 整体超时问题已由 C 块修复） |
| 指数 | 腾讯 fqkline | **无** | 6 字段**无 amount**（F1 实证：三大指数全量错位 1,815 行已修复，amount 恒 0 属结构性） | 单源 | F1_index_kline_field_fix_20260908.md；9/8 指数陈旧补拉才暴露错位 | 高（唯一源） |
| 分钟K（min5） | 腾讯 mkline | 新浪分钟接口 | 无 amount 列（kline_min5 表结构不含 amount，**无需补偿**） | 腾讯主（updater `_fetch_tx_min5`） | 8/21/24/28 `min5_empty=1004`（拉到但空，非报错——updater_repair 根因分析） | 中（按 IP 限流 P62：16 并发反而慢，8 并发稳态） |
| 实时行情 | 腾讯 qt.gtimg.cn | 新浪 hq | 含 amount（万元） | 腾讯→新浪 | 无独立失效记录 | 中（web.ifzq 501 已见，qt 存活） |
| 全市场列表 | 新浪分页（~78 页并发） | 磁盘缓存 6h | 代码/名称/现价 | 单源+缓存 | 无（失败用旧缓存） | 低 |
| 全球序列 | CBOE（VIX）/ 东财（CL，主机轮换）/ 新浪外汇/美股 | 各链主备（`_global_source_chains`） | close 单值 | 主→备 | global_source_fix_20260911.md：东财 CL 间歇性掐连接（_g_em_close 六连退避）；本块实测 push2his 全主机 RemoteDisconnected（风控） | 中（东财间歇性） |
| **补偿源（新增）** | 腾讯 qt.gtimg.cn（当日批量） | 东财 push2his（逐票） | amount 有 | qt→东财 | qt 当前存活；东财当前风控断连（实测） | qt 与主链同域；东财为备 |

**单点源结论**：
1. **日K amount 结构性依赖 tdx**——腾讯/新浪无额 → tdx 失效即 amount 全 0（P0-B 已实际发生三天）。
2. **指数唯一源腾讯**（且无 amount，F1 后显式写 0）——指数成交额暂无源（任务书"指数降级后如何补 amount"：**指数 amount 需另接源**，见 §2）。
3. **min5 无 amount 列，不受影响**（盘点澄清：任务书 min5 通道与 amount 无关）。

## §2 补偿方案表（"降级后字段缺失"组合 → 补偿）

| 组合 | 缺失 | 补偿方案 | 实现 |
|---|---|---|---|
| 日K 降级到腾讯（当日） | amount | **腾讯 qt.gtimg.cn 批量**（`q=sh600000,sz000001,...` 每批 ≤60，成交额=parts[37] 万元×10000，校验 parts[30] 前 8 位==target 日） | `_compensate_amount` 主链 ✓ 已实现 |
| 日K 降级到腾讯（历史日） | amount | 东财 push2his（fqt=1，主机轮换 `_G_HOSTS`）逐票拉区间 | `_compensate_amount` 备链 ✓ 已实现（当前只补 target 日，历史日属 D4a 回灌范畴，报告披露） |
| 日K 降级到新浪（当日/历史） | amount | 同上（qt 主 / 东财备） | ✓（同路径，missing 记录 source=sina） |
| 指数降级/无额 | amount | **需另接指数成交额源**（新浪指数实时 `hq.sinajs.cn` 的 `s_sh000001` 或东财指数 K 线 secid=1.000001 有 amount）——**建议项**，本期未实现（指数 amount 无消费方：INDEX_TIMING_ENABLED=False，见 §5 遗留①） | 建议 |
| min5 降级 | — | **无需**（kline_min5 无 amount 列） | — |
| 实时行情降级 | — | 已有腾讯→新浪链，amount 两源都有 | — |

## §3 实现 diff 摘要（2 文件）

### app/datafeed.py（~775→816 行；py_compile rc=0，LF）
- `_record_degradation(code, source, missing_fields, reason="")`：append
  `data/source_degradation.jsonl`（线程锁 `_sd_lock`，LF，失败静默）——
  行格式 `{t, code, date(拉取日), source(tencent/sina), missing_fields:["amount"]}`
- `_fetch_kline_tencent` / `_fetch_kline_sina`：日K被调用即记降级（两函数只被
  tdx 失败后的降级路径调用，一处实现覆盖 datafeed `_fetch_daily` 与 updater
  `_fetch_daily_one` 两条链）

### app/updater.py（~1456→1520 行；py_compile rc=0，LF）
- `_fetch_and_save`：返回加 `missing_by_src`（JSON 化：list 代替 tuple）
- `_update_daily_stale_first`：聚合各批 missing → 返回加 `missing_by_src`
- `_compensate_amount(missing_by_src, verbose=False)`：
  - 只补 `target` 日（当日）缺失（历史日属 D4a 范畴）
  - 主链：腾讯 qt.gtimg.cn 批量（每批 60，decode=gbk，`parts[30][:8]==target`
    校验，万元×10000）
  - 备链：东财 push2his（复用 `_G_HOSTS` 主机轮换 + `_g_http`）
  - 写库：读库内现值 OHLCV → `df._kline_save(code,"day",[row])`——amount>0
    条件覆盖，**OHLCV/volume 保持降级源值不动**；amount 为真实成交额不受
    复权基准差异影响
  - 返回 `{attempted, ok, failed, remaining_zero, total_rows, ratio, by_source}`
- `_audit_source_degradation(comp)`：`source_degradation_daily` 汇总事件——
  `degradation_by_source`（当日 jsonl 计数，真实降级记录）+
  `missing_by_source` + `compensate_attempted/ok/failed` +
  `remaining_zero` + `amount_ratio`
- `run_update`：`_amount_zero_guard()` 之后挂 `_compensate_amount` +
  `_audit_source_degradation`（异常隔离，不影响主流程）

## §4 验收证据（全过）

### 验收 A：降级留痕（tmp/e4/accept_a.py）
- 直调 `_fetch_kline_tencent("600000")`：**腾讯 web.ifzq 当前 501**（HTTPError，
  降级记录仍在解析前写入——web 主机被封是本块实测的活证据）
- monkeypatch `tdx.available=False` + `_kline_meta=(0,"")` → `fetch_kline("000001")`
  全链降级（tdx→腾讯 501→新浪）→ 返回 10 条（库内旧值兜底）
- **source_degradation.jsonl：0→4 行**（600000×2 + 000001×1 tencent + 新浪行），
  格式 `{code, date, source, missing_fields:["amount"]}` ✓

### 验收 B：降级补偿 + 完整率（tmp/e4/accept_b.py，mini 副本库）
- mini 库 2 票（600000/000001，target=09-11，amount=0 模拟腾讯降级写入）
- monkeypatch `C.DB_FILE=mini`（运行时属性，独立进程，**生产 config.py 未动、
  生产 market.db 未动**）
- `_compensate_amount({"tencent":[...]})` → **ok=2 failed=0**，qt 主链命中：
  600000→604,630,000（6.05 亿，与 volume×close 校验吻合）、
  000001→979,990,000（9.8 亿，与库内 tdx 旧值 979,990,600 差 600 元=取整）
- **当日 amount 完整率 = 1.0 ≥99%** ✓

### 验收 C：audit 汇总事件
- `_audit_source_degradation(comp)` → audit 尾部出现
  `source_degradation_daily`（degradation_by_source={"tencent":4}——jsonl 当日
  真实计数；compensate_attempted=2/ok=2/ratio=1.0）✓

## §5 生效与遗留

- **生效**：datafeed/updater 改动**下次服务重启生效**（运行中 8899 为旧代码）；
  独立调用 `_compensate_amount`/`_audit_source_degradation` 即时可用（本块已验）。
- **遗留① 指数 amount**：三大指数腾讯 6 字段无额且唯一源——本期未补（指数
  成交额无消费方，`INDEX_TIMING_ENABLED=False`）；建议后续接新浪/东财指数
  成交额源（§2 建议项）。
- **遗留② 历史日 amount 缺失**：补偿只覆盖 target 日；历史日（如 6 月 000001
  腾讯降级段）属 D4a 历史回灌方案范畴（8,003 行可回灌，未执行）。
- **遗留③ 东财风控**：本块实测 push2his 当前 RemoteDisconnected（此前可用）
  ——东财作为备链存在间歇性；主链 qt.gtimg.cn 当前存活且为腾讯自家域。
- **遗留④ 双跑假失败**：9/1-9/2 `data_update_failed` 为重复触发空跑（Z4 已诊断），
  非真实降级——本次盘点计入"重复触发"与"真实降级"两类语义（降级记录只认
  `source_degradation.jsonl`，不受双跑影响）。
- **遗留⑤ 审计链**：验收 C 向生产 audit 写入 1 条 `source_degradation_daily`
  （真实语义事件，合法；哈希链 `broken_at=2026-08-16` 已知历史基线未变）。

## §6 文件清单

| 文件 | 改动 | 行数 | mtime |
|---|---|---|---|
| app/datafeed.py | +`_record_degradation` +2 处调用 | 775→816 | 09-13 16:21:38 |
| app/updater.py | +`_compensate_amount`/`_audit_source_degradation` +missing 上抛 | 1456→1520 | 09-13 16:30:43 |
| data/source_degradation.jsonl | 新（验收 4 行） | 4 | — |
| tmp/e4/accept_a.py / accept_b.py | 验收脚本 | — | — |
| docs/reports/e4_source_singleton_20260913.md | 本报告 | — | — |

全部 py_compile rc=0、LF（CRLF=0）、`import app.server` 回归通过。
