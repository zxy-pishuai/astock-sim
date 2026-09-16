# R3 工程两件套报告：kline 复合索引 + audit.jsonl 按日轮转（2026-09-10）

任务书：`tmp/pack19/R3_engineering.md`。本轮先评审后执行（用户指示"先看是否合理，再做修改"）。

## 0. 评审结论（执行前）

**总体合理，可执行。** 逐条核实后的结论与修改点：

| 任务书条目 | 评审 | 修改/补充 |
|---|---|---|
| E1 `idx_kline_pcd(period,code,date)` | ✅ 合理且有实证价值 | EXPLAIN 实测：`period='day' GROUP BY code` 走 idx_kline_pd 但 **USE TEMP B-TREE**（全表分组排序）；新索引可消除。**但任务书验证指标（单票排序）已走主键索引秒级，无法体现增益 → 验证指标改为聚合查询计时** |
| E2 每日 00:05 轮转 audit.jsonl | ✅ 主体合理 | **3 处修改**：① 外部裸 rename 与 `record` 的 `_cross_proc_lock` 不互斥有竞态 → 新增 `audit.rotate_daily()` **持锁入口**；② 现有 `_maybe_rotate` 归档名 `audit_YYYYmmdd_HHMMSS.jsonl`，清理须兼容两种命名；③ 轮转后须**新建空 audit.jsonl**（首版实现漏建，试跑暴露后修复） |
| E2"读路径零改动" | ✅ 成立 | `app/audit.py` L194-199 已支持 `audit_*.jsonl` 多文件读取（R2-P1.6 既有），verify 亦按文件切换重置 |
| E2 哈希链 | ✅ 兼容 | 链语义 = **文件级/每日链首（首条 prev=""）**（`_sync_prev_locked` L119-131）→ 轮转后新文件首条 prev="" 与既有约定一致 |

关键前提核查（执行时实测）：audit.py 每次 record 为 **open-append-close + 读尾重算 prev**（无常驻句柄）→ 轮转安全；现有 `_ROTATE_BYTES=50MB`（当前 audit.jsonl 仅 1MB，大小轮转多年不触发，按日轮转有独立价值）。

## 1. E1：kline 复合索引（✅ 完成）

- 执行环境：2026-09-10 12:0x（非交易时段；引擎未运行——8899 无监听、无 main.py 进程、app.lock 不存在；唯一 python 进程 14384 为桌面代理非项目）。
- **备份**：`data/market.db`（1.3GB）→ `tmp/r3_backup/market.db`，SHA256 源/备份 **match=True**（哈希见 manifest.txt；备份耗时 6.4s）。
- 建索引：`CREATE INDEX IF NOT EXISTS idx_kline_pcd ON kline(period, code, date)`，**elapsed=11.4s**。
- **前后计时**（同一聚合查询 `SELECT code, COUNT(1) FROM kline WHERE period='day' GROUP BY code`，各 3 次取中位）：

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 建索引前 | **58.211s** | 全表扫 + TEMP B-TREE（分钟级——验收方报告痛点的根源） |
| 建索引后 | **0.828s** | **加速 70.3 倍** |
| EXPLAIN（后） | — | `SEARCH kline USING COVERING INDEX idx_kline_pcd (period=?)`，无 TEMP B-TREE |

- 现有索引盘点（未动）：`sqlite_autoindex_kline_1`（code,period 唯一）、`idx_kline_cp(code,period)`、`idx_kline_pd(period,date)`——新索引与三者不重复，补齐"period+code 前缀且 date 有序"的覆盖聚合能力。
- **回滚**：`DROP INDEX IF EXISTS idx_kline_pcd;`（写连接执行即可）。

## 2. E2：audit.jsonl 按日轮转（✅ 完成）

### 实现（白名单内）
- `app/audit.py` **+45 行**：新增 `rotate_daily(keep_days=90)`——持 `_cross_proc_lock` 原子执行（与 record 互斥，杜绝 rename/写竞态）；归档为 `audit_YYYYMMDD.jsonl`（同名存在则跳过→幂等）；**归档后确保活动文件存在**（新建空 audit.jsonl）；清理超期归档（兼容 `YYYYMMDD` 与 `YYYYmmdd_HHMMSS` 两种命名，解析失败跳过）。
- `tools/audit_rotate.py`（新，26 行）：调 `rotate_daily()`，`--out` 打印 JSON，退出码校验活动文件存在。
- `tools/audit_rotate.cmd`（新）：计划任务包装（项目惯例，同 close_update.cmd）。
- 计划任务 **TianjiAuditRotate**：每日 00:05，`schtasks` 注册 **SUCCESS**（Status=Ready，下次 2026/9/11 0:05）。回滚：`schtasks /delete /tn TianjiAuditRotate /f`。

### 试跑验证（手动跑一次）
1. 首跑：`audit_20260910.jsonl`（1,019,017 字节 / **4,471 行**），新 audit.jsonl 建空 → **行数守恒 4,471+0** ✓；
2. 追加验证：`audit.record(...)` 写 1 条测试行 → 新文件 182 字节 1 行，**首条 prev=""**（文件级链首语义正确）✓；
3. 幂等重跑：`{"rotated": null, "removed": []}`（今日已有归档跳过）✓；
4. **哈希链只读自校验**（按日链首语义：每日首条 prev='' 合法，日内连续）：
   - 新 audit.jsonl：**chain_ok=True**（1 行）；
   - 归档 audit_20260910.jsonl：4,471 行，断点 **340 处全部位于 2026-08-16~08-27 历史段**（08-20:67 / 08-26:253 为主）——为既有历史并发断链基线（`app/audit.py` docstring 明示"历史断裂不修补，见 tools/audit_chain_diag.py 基线"），**轮转为 os.replace 原字节移动，零引入**；09-08 之后断点 0。

### 与既有机制的关系
- `_maybe_rotate()`（50MB 大小触发）保留不动；按日轮转与其并存（谁先触发谁归档，命名不同不冲突）。
- 读路径零改动：`audit.get`/verify 已支持多文件（R2-P1.6）；server `/api/log` 为控制台缓冲不读 audit.jsonl。

## 3. 改动文件与备份

| 文件 | 改动 | 备份（tmp/r3_backup/） |
|---|---|---|
| `data/market.db` | +1 索引（库内） | market.db SHA256 match |
| `app/audit.py` | +45 行 rotate_daily() | audit.py（SHA256 入 manifest） |
| `tools/audit_rotate.py` | 新建 | audit_rotate.py |
| `tools/audit_rotate.cmd` | 新建 | — |
| 计划任务 TianjiAuditRotate | 注册 | 回滚命令见 §2/§4 |

全部 Python 文件 ast.parse + py_compile 通过、CRLF=0。引擎/策略/回测逻辑零改动；未重启任何进程（服务本就不在运行，未启动）；零出网。

## 4. 回滚命令

```
schtasks /delete /tn TianjiAuditRotate /f
python -c "import sqlite3;c=sqlite3.connect(r'C:\Users\26838\A股模拟盘\data\market.db');c.execute('DROP INDEX IF EXISTS idx_kline_pcd');c.commit()"
# audit.py 还原：Copy-Item tmp\r3_backup\audit.py app\audit.py -Force
```

## 5. 遗留说明

- 明日 00:05 首次自动轮转：归档 09-10 全天行 → `audit_20260911.jsonl`（命名按执行时刻，含 09-10 全量+09-11 00:00-00:05 少量行；链校验按行内日期处理，兼容）。
- audit_rotate_cron.log 将记录每日轮转 exit code；建议首周抽查一次。
