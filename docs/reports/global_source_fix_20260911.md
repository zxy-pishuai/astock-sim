# 全球外盘·日韩欧指数数据源修复（2026-09-11）

## §0 症状
用户反馈："日韩数据源老是不可用，而且不能刷新最新数据，只能停留在打开的那一瞬间。"
对应前端「全球市场」页的**日本/韩国/欧洲指数卡片**（N225 日经、KS11 KOSPI、TWII 台指、FTSE、DAX、FCHI）恒显"数据暂不可用"；页面打开瞬间若有数据则此后不再更新（60s 轮询拿到的仍是旧渲染）。

## §1 根因（2026-09-11 09:1x 实测）
日韩欧台指数的两条实时源**同时不可达**：

| 源 | 用途 | 实测结果 |
|---|---|---|
| 东财 `push2.eastmoney.com` ulist.np | 日韩欧台指数**主源** | **RemoteDisconnected**（本机 IP 被风控，代码注释早有预警） |
| Yahoo Finance chart | 日韩欧指数**备用源** | **HTTP 403 Forbidden**（被封） |
| 腾讯 `qt.gtimg.cn` | 美股/港股指数 + 日韩个股 | OK（不覆盖 N225/KS11 等指数） |
| 新浪 `hq.sinajs.cn` | FX/商品/美元指数 | OK（不覆盖日韩指数） |
| 东财 `push2his.eastmoney.com` | 指数日K历史 | OK（仅历史序列） |

**连带缺陷**（顺带发现并修复）：`_fetch_em()` 把东财 `f4` 字段当作昨收 `prev_close`，实测 `f4` 是**涨跌额**（如 -1726.83）——昨收正确值 = price − chg。修复前该字段显示负数/错误值。

## §2 修复内容
### 2.1 `app/global_market.py` — 东财双宿主降级
- 新增 `_EM_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")`：
  - 主源 push2 失败/空结果 → 自动降级 **push2delay（东财延迟行情镜像，同协议同字段）**；
  - 两宿主**独立熔断器**（src=`em` / `emdelay`），主源恢复后自动回到优先位；
  - `_fetch_em()` 两宿主都失败才返回 `{}`（交给 Yahoo 备用 / 前端"不可用"）。
- 抽出 `_em_pull(host)` / `_em_quote(code, x, src)`：字段解析统一，`source` 标注 `em`/`emdelay` 供可观测性。
- **修字段 bug**：`prev_close = round(price - chg, 2)`（f2 − f4），不再把涨跌额当昨收。
- DAX/FCHI 变体探测（100.GDAXI / 100.CAC）保留，按宿主逐次探测。

### 2.2 `web/js/app.js` — 失败时标注旧数据时间
`showGmDown()` 在源断开时显示"（保留 HH:MM:SS 旧数据）"，不再把旧渲染静默挂在页面上造成"不刷新"错觉。

## §3 校验（改后实测输出）
```
[1] py -3.13 -m py_compile app/global_market.py  → OK
[2] _fetch_em() 返回 6 指数，sources={'emdelay'}（降级路径实际触发）
  DAX   德国DAX30     pct=-0.84  prev_close=25576.45  src=emdelay
  FCHI  法国CAC40     pct=-0.49  prev_close=8156.67   src=emdelay
  FTSE  英国富时100   pct=-0.57  prev_close=10670.06  src=emdelay
  KS11  韩国KOSPI     pct=-2.56  prev_close=7033.92   src=emdelay
  N225  日经225       pct=-2.77  prev_close=65270.95  src=emdelay
  TWII  台湾加权      pct=-1.57  prev_close=46940.49  src=emdelay
[3] 自洽校验：price - chg == prev_close 全部通过（如 63532.2+1738.75=65270.95 ✓）
[4] node --check web/js/app.js → OK
```
- 修复前：N225/KS11/TWII/FTSE/DAX/FCHI 全缺失 → 日韩/欧洲卡片"数据暂不可用"。
- 修复后：6 指数全返回（经 push2delay 延迟镜像），`prev_close` 为正确昨收。

## §4 生效条件（重要）
改动**下次服务重启后生效**。当前生产进程（PID 13108，持 127.0.0.1:8899）加载的是旧代码，未热加载。
按项目纪律**未自行重启**——重启时机由用户决定（重启后前端 60s 轮询即拉新数据，交易时段缓存 TTL 30s）。

## §5 已知限制与遗留
1. **push2delay 是延迟行情镜像**（东财免费延迟档，通常延迟分钟级）——对日韩欧指数展示可接受，但非逐笔实时；若用户要盘中精确价，需等 push2 主源对本机解封（或换代理）。
2. **Yahoo 备用源 403** 仍挂（该域名整体被封）；东财镜像已兜住日韩欧指数，Yahoo 仅作第三保险，暂不动。
3. 腾讯不提供 N225/KS11 指数（实测 `q=N225,KS11` 返回 `pv_none_match`），新浪 `int_nikkei` 可用但无 KOSPI——未采用。
4. 备份：`tmp/global_market.py.bak_20260911_091904`（SHA256 与原文件双哈希一致，见 §附录）。

## §附录 备份哈希
- 原文件 `app/global_market.py` SHA256: `C4A3B4287ADECD63564A3347733C80C244E742ED2D8D182D088C2101CDE16DBB`
- 备份 `tmp/global_market.py.bak_20260911_091904` 同哈希，比对一致。
