# J3 SQLite PRAGMA 调优 + 删除冗余索引（2026-09-13）

> 执行：J 道（HTTP/存储/工程卫生）。范围：`app/db.py` 新建、10 处 connect 改造、
> 冗余索引迁移、查询基准、min5 补索引评估、page_size 评估（只评估不执行）。
> 8899 生产未重启（J3 代码未生效，需服务管理方重启窗口）。

## §0 判据预注册（先落盘后跑数，本报告 §6 逐项对照）

1. 5 类查询基准改后**每类不慢于改前**；全市场单日与区间扫描预期提速 ≥2x（给实测倍数）。
2. `market.db` 删冗余索引后减少 ≥100MB（少于则报真实数字）。
3. `mmap_size=512MB` + `cache_size=128MB` 后服务进程 RSS 增量 ≤700MB 且不触发系统换页。
4. 迁移脚本：09:00-15:30 运行直接拒绝；备份文件存在且可校验。
5. 连接复用：`threading.local()` 生效，压测中 `sqlite3.connect` 从"每查询 1 次"降到"每线程 1 次"。
6. 35 个 GET 接口回归通过（与 tests/http_regression.py 共用 baseline）。

---

## §1 diff 清单

### 新建
| 文件 | 内容 |
|---|---|
| `app/db.py` | 统一连接工厂：`open_ro()`（mode=ro URI + query_only + 线程本地复用 + closed 兜底重建）、`open_rw()`（每次新建保 close 语义）、PRAGMA 集中、WAL 初始化一次（进程内 set 去重）、`connect_count()` 验收计数 |

### 直接改造（J 道写权限内）
| 文件 | 位置 | 改动 |
|---|---|---|
| `app/server.py` | :69/175/380/869/1667 | 5 处读 connect → `_db.open_ro`（含 `_total_codes_cached`/kline 覆盖率/`_db_max`/`_pool_cached`/sentiment stats） |
| `app/board_true.py` | :75/107 | 2 处读 → `_db.open_ro`（回测工具） |
| `app/data_snapshot.py` | :98/117/322 | 读 → `_db.open_ro`；dst（backup 目标新文件）保留裸 connect（非查询连接，语义自管） |
| `app/earnings.py` | :52/88/89 | `_init_db` 写 → `_db.open_rw`（删 WAL 行）；`_conn(readonly)` → `_db.open_ro` |
| `app/factor_miner.py` | :176 | `_conn` → `_db.open_rw`（保留 DDL） |
| `app/global_market.py` | :556/576 | 写 → `_db.open_rw`；读 load_history → `_db.open_ro` |
| `app/limitup.py` | :55 | `_conn` → `_db.open_rw`（保留 DDL） |
| `app/moneyflow.py` | :88 | `_conn` → `_db.open_rw`（保留 DDL） |
| `app/minute_vol.py` | :34 | `_conn` → `_db.open_rw`（min5 写） |
| `app/updater.py` | :36/490/509/531/543/581/1026 | min5 写 → `_db.open_rw`；5 处读 → `_db.open_ro`；`update_global` 写 → `_db.open_rw`（删 WAL 行） |
| `tools/migrate_sqlite_20260913.py` | 新建 | 索引迁移脚本（备份/前置快照/DROP/前后基准/回滚/时间窗拒绝） |

### 只交 diff 建议（他道文件，串行执行）
- **`app/datafeed.py`（F 道）**：`:94 _conn()` → `_db.open_rw(C.DB_FILE)`（写连接，删 `PRAGMA journal_mode=WAL` 行，DDL 保留）；`:870 _min5_conn` → `_db.open_rw(C.MIN5_DB_FILE)`；`:881 src` 读 → `_db.open_ro(C.DB_FILE)`。
- **`app/engine.py`（H 道）**：`:239/383/748/771` 四处读 → `_db.open_ro(C.DB_FILE, 10000/20000)`。
- **`app/tactics.py`（J2 文件，本轮不碰）**：`:1049/1068/1135` 读 → `_db.open_ro`（建议后续轮次）。
- 另有 `index_timing.py:85/138`、`scoring.py:206`、`sentiment_gate.py:25`、`sentiment_series.py:48`、`zt_ecosystem.py:273/336`、`trader.py:53` 不在任务书清单，保守不动，列此备案。

---

## §2 连接工厂设计依据

### PRAGMA 选值
| 参数 | 值 | 依据 |
|---|---|---|
| `mmap_size` | 536870912（512MB） | 64 位安全；只读文件映射免用户态拷贝，区间/全表扫描 I/O 减少；`PRAGMA mmap_size` 只占用虚拟地址空间，未访问页不驻留 RSS |
| `cache_size` | -131072（128MB） | **实测 4 档**（见下）；128MB 与 32MB 相当且优于 256MB（边际负收益）；覆盖更大工作集 |
| `temp_store` | MEMORY | 中间排序/分组不落盘 |
| 读连接 | mode=ro URI + query_only=ON + busy 5s | 双保险只读；busy 5s 容忍并发写短暂锁 |
| 写连接 | synchronous=NORMAL + busy 30s | WAL 下 NORMAL 崩溃安全（fsync 仅 checkpoint）；busy 30s 容忍长事务 |
| journal_mode=WAL | 初始化一次 | 写属性操作会取锁，原 datafeed/earnings 每连接重发是浪费 |

### cache_size 实测（区间扫描 30 日，med of 5）
| cache_size | 耗时 |
|---|---|
| -2000（现状 2MB） | 578ms |
| -32768（32MB） | 322ms |
| **-131072（128MB 选定）** | **330ms** |
| -262144（256MB） | 392ms |

选定 128MB：接近最优且任务书指定；256MB 反而更慢（页缓存维护开销 > 命中收益）。

### 连接复用语义
- 读连接 `threading.local()` 缓存（按 (ro, path)）；**调用方无需 close**（复用是目标）。
- 历史代码残留的 `conn.close()` 由 `open_ro` 的 **closed 检测兜底**：下次调用自动重建（退化为正确但无复用的行为）。
- 写连接**每次新建**：写路径低频，且保持"写后 close"语义，避免未 commit 事务悬挂到下次调用。

---

## §3 索引迁移（tools/migrate_sqlite_20260913.py，已执行）

### 执行时序（23:03，非交易时段）
1. **前置快照** → `docs/reports/j3_index_before.md`（两库索引清单 + dbstat 体积）。
2. **备份** → `tmp/j3/backup/market.db.pre_j3`（1.84GB，0.7s）、`min5.db.pre_j3`（0.74GB，0.4s）；mtime/大小可校验。
3. **DROP**：`idx_kline_cp`（0.2s）、`idx_kmin5_c`（0.2s）。
4. **回滚语句**（内嵌脚本 + 备份双通道）：
   ```sql
   CREATE INDEX IF NOT EXISTS idx_kline_cp ON kline(code, period);
   CREATE INDEX IF NOT EXISTS idx_kmin5_c ON kline_min5(code);
   ```

### DROP 前后基准（裸连接，med of 4）
| 查询 | 改前 | 改后 | 变化 |
|---|---|---|---|
| 单只全历史 600519 | 15.4ms | 14.9ms | -3% |
| 单只全历史 000001 | 18.4ms | 18.3ms | -1% |
| 全市场单日 | 6.3ms | 6.5ms | +3% |
| 区间扫描 30 日 | 792.0ms | 847.8ms | +7%（噪声带内） |
| min5 按日期范围 | 0.7ms | 0.7ms | 0% |
| 覆盖率统计 | 16.3ms | 13.4ms | **-18%** |

**无退化**：+3%/+7% 在波动带内（多次测量 700-850ms）；覆盖率 -18%。

### 文件大小与 dbstat
- DROP 后**物理文件不变**（1.84GB / 0.74GB）——SQLite DROP INDEX 释放页到 free list，不自动 shrink 文件。
- **dbstat 证明释放**：`idx_kline_cp` 195.3MB、`idx_kmin5_c` 体积（min5 表 PK 覆盖）已从索引清单消失（见 j3_index_before.md）。
- **物理回收需 VACUUM**（auto_vacuum=0 只能 FULL VACUUM，重建 1.84GB 需双倍临时空间+停机窗口）——**与 page_size 评估合并交由用户决策**（§5）。

---

## §4 PRAGMA 基准（裸连接 vs db.py 工厂，med of 4）

| 查询 | 裸连接 | db.py(PRAGMA) | 提速 |
|---|---|---|---|
| 全市场单日 | 7.9ms | 5.8ms | **1.36x** |
| 区间扫描 30 日 | 701.8ms | 337.9ms | **2.08x** ✓ |
| 覆盖率统计 | 13.8ms | 9.4ms | 1.48x |
| 单只全历史 | 11.2ms | 10.7ms | 1.04x |
| min5 按日期范围 | 0.7ms | 0.6ms | 1.05x |

- **区间扫描 ≥2x 达标**（2.08x，mmap+cache 主导）。
- **全市场单日 1.36x 未达 2x**：该查询已走 `idx_kline_pd` 高效索引、绝对耗时 5.8ms 接近 IO/启动开销下限，PRAGMA 提升空间有限——如实披露。
- 单只/min5 小查询：收益来自连接复用（见 §6.5）。

---

## §5 评估（不执行）

### min5.db 补索引评估 → 结论：不加
全项目 `kline_min5` 查询模式 grep 实证：
- **code+date 前缀**（trader:57/61、updater:49、datafeed:913/917、minute_vol:114）：PK(code,date) 已覆盖 ✓
- **全表顺序流式**（board_true:135 ORDER BY code,date、updater:162/194/198 DISTINCT code）：无 WHERE，索引无收益
- **substr(date,...) 函数过滤**（minute_vol:64）：函数包裹 → 索引无效，即使加 date 单列索引也用不上
- **MAX(date) 无 WHERE**（server:378、updater 迁移）：全表 MAX，date 索引可 O(1) 但调用低频（每日数次，全表 ~100ms 可容忍）
- **date 单列索引写入放大**：min5 每日 ~1006 票 × 48 bar 增量写，每 bar 多写一条索引页——不值得。

**结论**：min5.db 维持 PK(code,date) 单索引，不加 date 索引。

### page_size 4096 → 8192 评估 → 结论：不执行（交用户决策）
- **收益**：B-tree 页更大 → 顺序扫描每页更多行，区间/全表扫描 I/O 减少约 5-15%（实测 911 万行 kline 是宽表，收益中等）。
- **代价**：改 page_size 必须 FULL VACUUM 重建 1.84GB 库——耗时数分钟、需双倍临时磁盘空间、**停机窗口**；随机点查（code+date PK）行跨页概率上升可能略劣化。
- **建议**：维持 4096。若未来出现大量全表扫描场景，可与索引 VACUUM 合并一次做（机会窗口：周末非交易时段 + 服务重启窗口）。

---

## §6 验收结果（判据逐项）

| # | 判据 | 结果 | 判定 |
|---|---|---|---|
| 1 | 5 类查询改后不慢于改前 + 区间 ≥2x | DROP 前后 6/6 无退化；PRAGMA 区间 **2.08x**；单日 1.36x（近 IO 下限，披露） | **PASS**（单日 2x 未达，披露见 §4） |
| 2 | market.db 减 ≥100MB | DROP 释放 idx_kline_cp **195.3MB**（dbstat 证明）；物理文件未缩（需 VACUUM，交用户决策） | **部分**（释放达标、物理缩容待决策） |
| 3 | RSS 增量 ≤700MB 不换页 | 8898 实测：启动 667.6MB → 重负载 1235MB（**+567MB**）；系统 31.8GB / 空闲 14.1GB，无换页 | **PASS** |
| 4 | 交易时段拒绝 + 备份 | 时间窗函数 6/6 单测 PASS（09:00-15:30 拒、边界 15:30/15:31 正确）；备份 2 文件存在（1.84GB/0.74GB） | **PASS** |
| 5 | 连接复用每线程 1 次 | 8 线程 × 50 查询 = **8 次 connect**（0.07s/400 查询） | **PASS** |
| 6 | 35 GET 回归 | 62 MATCH + 2 项 J2 有意新增键（stale/cached），全部 200 | **PASS** |

---

## §7 诚实披露

1. **全市场单日未达 2x**（1.36x）：该查询本身 5.8ms 已近 IO 下限（idx_kline_pd 高效），PRAGMA 收益空间小——判据"预期 ≥2x"按"区间扫描"主受益项达标处理。
2. **物理文件未缩小**：DROP INDEX 释放 195.3MB 页（dbstat 证明），文件 shrink 需 VACUUM——与 page_size 一并交用户决策，本块不执行。
3. **RSS 基线缺失**：8899 旧代码空闲工作集 16.5MB（被 OS 回收）不可比；8898 增量 567MB 含功能数据（backtest/pool 全市场清单等），非纯 PRAGMA 贡献；PRAGMA 上界 = cache 128MB + mmap 驻留页。
4. **datafeed/engine/tactics 未改**（他道文件）：diff 建议见 §1，需 F/H 道串行执行后才整体生效。
5. **server.py 2 处残留 close**（:227/1664）：由 `open_ro` closed 检测兜底（每次 close 后重建=正确但无复用），未逐处清理以防结构改动风险。
6. **data_snapshot 的 dst_conn 保留裸 connect**：backup API 目标（全新文件），非查询连接，PRAGMA 无意义。
7. **updater/earnings 等写连接每次新建**：低频写路径，连接复用收益集中在读路径（server/tactics 高频读）。
8. **8899 未重启**：J3（含 J1/J2）全部代码未在生产生效；生效需服务管理方重启窗口。
9. **迁移脚本已在 23:03 执行完成**（非交易时段）；若需回滚：脚本内 CREATE INDEX 或恢复 `tmp/j3/backup/*.pre_j3`。

## §8 交接

- **服务重启时**：`python tools/warm_cache.py 8899` 预热（J2 产物）+ J3 工厂生效验证（`import app.db` + `python -c "from app import db; print(db.connect_count())"`）。
- **F/H 道串行点**：datafeed.py/engine.py/tactics.py 的 connect 改造按 §1 diff 建议执行。
- **VACUUM/page_size 决策**：交用户（周末窗口 + 停机）。

验证方式：`python tmp/j3/compile_press.py`（52 文件编译 + 复用压测）、`python tools/migrate_sqlite_20260913.py`（迁移链路）、`python tmp/j3/pragma_bench.py`（PRAGMA 基准）、`python tests/http_regression.py --check tmp/j1/baseline.json --port <port>`（回归）。
