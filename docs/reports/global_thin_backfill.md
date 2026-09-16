# A4 全球薄源回补报告：GC / USDCNH / DJIA（主备切换接入 updater 全球段）

- 状态：**完成**（回补 PASS + 哨兵复验 thin 全 false + 主备链固化进 `app/updater.py`）
- 授权范围：本轮独占 `app/updater.py` 全球段（Phase63 区块）；写库仅 `global_kline` 三序列，先例同 data_repair.md 任务2（DINIW 回补）

## 一、根因

| 序列 | 回补前 | 根因 |
|---|---|---|
| GC | **4 行**（实时占位） | Phase63 链唯一主源 = 东财 `102.GC00Y`；东财 push2his 整族当期**间歇掐连接**（实测全部 secid 连续失败），备腿 GLD 走 `_g_sina_pipe` 但该端点返回 dict 数组——解析器口径错误恒失败 |
| USDCNH | **4 行** | updater 全球段**从未配置该序列链** |
| DJIA | **1 行** | 同上，无链 |

## 二、候选源实测（2026-08-27）

| 源 | 端点/代码 | 结果 |
|---|---|---|
| 新浪全球期货 | `GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=GC` | ✅ **2588 行，2016-08-29..2026-08-26**，0.4s |
| 新浪环球外汇 | `NewForexService.getDayKLine?symbol=fx_susdcnh` | ✅ **3077 行，2014-11-07 起** |
| 新浪美日线 | `US_MinKService.getDailyK?symbol=.DJI` | ✅ **5699 行，2004-01-02 起** |
| 同上 GLD/USO | getDailyK 数组口径 | ✅ 5474/5125 行（顺带修复 CL/GC 备腿解析器） |
| 东财 push2his | `102.GC00Y / 133.USDCNH / 100.DJIA / 100.UDI…` | ❌ 当期整族断连（保留为备腿，主机轮换恢复期可用） |
| FRED fredgraph.csv DJIA | — | ❌ 双次超时（60s），弃用 |
| 腾讯 fqkline usDJI | 分页翻页 | ❌ 仅返回最新1日，无历史，弃用 |
| 新浪 INDU 变体 | getDailyK | ❌ 死序列（2020 止且含0值），已排除 |

## 三、落地的主备链（app/updater.py `_global_source_chains()`）

```
GC      主: sina全球期货GC全史   → 备1: eastmoney GC00Y → 备2: sina GLD代理(dict修复)
USDCNH  主: sina fx_susdcnh     → 备: eastmoney 133.USDCNH
DJIA    主: sina .DJI           → 备: eastmoney 100.DJIA
(顺带) CL 备腿 USO 改用正确的 dict 解析器 _g_sina_us_daily；本次回补运行中 CL 即实际降级走了该腿（东财在断）→ 链路自证可用
```
切换规则沿用 Phase63：依序尝试、首个产出 ≥30 行者胜；库内 ≥30 行后只增量大追加快照。

## 四、执行记录（红线逐项）

| 步骤 | 结果 |
|---|---|
| mtime 守卫 | market.db 静默确认 90s 后才动库 |
| 备份先行 | `data/backups/global_kline_thin_before_20260827_025456.json`（GC 4 / USDCNH 4 / DJIA 1 行原样导出） |
| 写库 | `update_global()` 内单事务 INSERT OR REPLACE（只动目标 sym，VIX/CL/DINIW 零写入=零扰动） |
| 占位行处置 | 与主键 (sym,date) 冲突的占位行被真值覆盖；未覆盖日期自然保留 |

## 五、回补前后对照（哨兵复验 quality_report.json⑥）

| 序列 | 前 | 后 | 首..尾 | thin |
|---|---|---|---|---|
| GC | 4 | **2589** ✅≥1000 | 2016-08-29..2026-08-26 | false |
| USDCNH | 4 | **3079** ✅ | 2014-11-07..2026-08-26 | false |
| DJIA | 1 | **5700** ✅ | 2004-01-02..2026-08-25 | false（latest 落后1个交易日=美股时区正常态） |
| CL/DINIW/VIX 对照组 | 不变 | 不变 | — | false |

## 六、交付物与使用

- `app/updater.py` 全球段：新增 `_g_sina_futures/_g_sina_us_daily` 两解析器 + 六链固化（compile 通过；服务重启后夜间更新自动走新链做每日增量）
- `tools/global_thin_backfill.py --check/--apply`（可重复执行；重跑幂等，仅补增量）
- `data/global_thin_backfill.json`（本次 before/after/verdict 摘要）、备份 json 一份
- 本报告；临时探针脚本已清理

## 七、遗留提示

- 东财族恢复时间未知；现主源全为新浪，若新浪日后限流，GLD/USO/EM 三级降级仍在链内
- `hf_GC` 类新浪期货接口深度止于 2016 年起；更早 COMEX 史如需（当前无消费方）另议 paid/双边源
