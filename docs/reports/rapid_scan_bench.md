# 速度侦察｜L1/L2 行情开源生态对比 + 快速扫板工具落地

- 时间: 2026-08-27 晚（盘后网络诊断 + 工具原型验收）
- 目的: 回答「我的系统和开源比谁快」「多快能挖到潜力票」
- 关联: `vendor_vibe_astock.md`（数据源评估）、`app/tdx.py`（常驻池）、`tools/rapid_scan.py`（本工具）

## 1. 夜间实测（今晚，2026-08-27 盘后）

| 探针 | 结果 |
|---|---|
| TDX 服务器 TCP 直连（4 个可用 IP） | **30~60ms/次**（3/8 IP 可达，其余超时） |
| raw pytdx connect + 行情请求 | connect ~170ms；**get_security_quotes → 0 行** |
| pytdx 日K `get_security_bars` | **0 行**（夜间） |
| pytdx 分时 `get_minute_time_data` | **✓ 240 根**（当日完整） |
| pytdx 公司信息类目 | ✓ 正常 |
| mootdx（系统内 Quotes.factory） | bars/quotes 同样 0 行（同因） |
| app.tdx 常驻池 `fetch_quotes_fast(1878只)` | 724ms 首轮（含建连），后续轮快返 |

**发现（原文如此记录）**：免费 TDX 公共服务器对「实时行情 / 日K」端点存在**夜间冷档**（盘后返回空行，但分时与信息类目仍服务）。白天生产环境你系统注释留痕的实测是 **5000 只 ≈1.0s（55ms/百只批）**——所以速度基准需以盘中为准，夜间只有连接 RTT 可测（本轮已测：~0.2s 建连 + <0.1s 传输）。

## 2. 速度对表结论（你的系统 vs 开源）

- 协议同源（通达信二进制）、数据节拍同为 **L1 3秒/包**：**开源库不可能比你的快**；你的常驻线程池 + 8s 熔断 + 降级链在工程上已是最优姿势。
- **真正吃掉你速度的不是源，是调度环**：`limitup.py` 盘中 TTL=120s（`LIMITUP_MEM_TTL_LIVE`）→ 新封板 2 分钟后才进池；fflow 分钟级；信号热循环轮频。
- 真 L2（十档+逐笔委托+撤单流）只存在于券商授权（QMT 系/掘金）或商业 SDK（代码开源数据收费）；**开源生态免费真 L2 不存在**（eltdx 等"逐笔"实为 L1 3 秒分笔聚合）。

## 3. 交付物

| 文件 | 说明 |
|---|---|
| `tools/rapid_scan.py` | 快速扫板独立工具（只读 app.tdx，不改 config/app） |
| `data/vendor/rapid_scan/rapid_scan_summary.json` | 冒烟跑 summary（1878 宇宙，环均 ~280ms 首轮 / ~60ms 稳态快返） |
| `data/vendor/rapid_scan/rapid_scan_YYYYMMDD.jsonl` | 事件流（盘中触发时逐条落 C1/C2/C3，今晚 0 事件符合预期） |
| `data/vendor/astock/upstream/` | a-stock-data v3.6.1 SKILL/README 钉档（对照参考） |

## 4. rapid_scan 设计（预注册式触发规则）

- C1 板上：`price >= limit_up*0.998`（主板 ×1.10 / 创业科创 ×1.20 / B 股 ×1.30），涨停价按昨收自算
- C2 冲板：主板 `pct≥7` / 创业科创 `pct≥17` 未到板
- C3 加速：`外盘/内盘 ≥ 2.5 且 pct≥4`（主动买盘碾压哨兵）
- 每事件 8 字段：时刻/价/涨幅/涨停价/类别/板上yn/买一五档/内外盘——喂进 trader 前先过池/风控（本工具只记录）

## 5. 盘中验证清单（待跑）

1. 交易日 09:31/10:00/14:45 各跑 `python tools/rapid_scan.py --rounds 30 --interval 3`
2. 验收点：① 全 1878 只每轮 P50 <2.5s（含全部批次）；② C1 事件数与 `limitup.fetch_pool('zt')` 同拍对照（±5s 内）；③ C3 哨兵误报率记录（复盘比对 N 日）
3. 通过后再评估：替换 limitup 当日盘中的 120s 轮询为快环事件流（那是 app 内改动，须另行立项走预注册）

## 6. 下一步可选（不自动执行）

- eltdx（协议同源更新版）拉仓评估：仅知识增量，速度不会超过本链路
- 全市场 2607 只宇宙版 + 北交所开关：改 `--pool` 数据源即可
- **真 L2 决策点**：券商 QMT 授权（月费路径）——需要你开户/授权，属人工环节
