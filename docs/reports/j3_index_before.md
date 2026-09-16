# J3 索引迁移前置快照（2026-09-13，迁移脚本自动生成）

> 由 tools/migrate_sqlite_20260913.py 在执行 DROP 前导出。
> 回滚语句：
> ```sql
> CREATE INDEX IF NOT EXISTS idx_kline_cp ON kline(code, period);
> CREATE INDEX IF NOT EXISTS idx_kmin5_c ON kline_min5(code);
> ```

## market.db（1.84 GB）

| 索引 | 表 | 列数 | SQL |
|---|---|---|---|
| idx_earn_d | earnings | 1 | CREATE INDEX idx_earn_d ON earnings(notice_date) |
| sqlite_autoindex_earnings_1 | earnings | 6 | (auto) |
| sqlite_autoindex_factor_pool_1 | factor_pool | 1 | (auto) |
| sqlite_autoindex_global_kline_1 | global_kline | 2 | (auto) |
| idx_kline_cp | kline | 2 | CREATE INDEX idx_kline_cp ON kline(code, period) |
| idx_kline_pcd | kline | 3 | CREATE INDEX idx_kline_pcd ON kline(period, code, date) |
| idx_kline_pd | kline | 2 | CREATE INDEX idx_kline_pd ON kline(period, date) |
| sqlite_autoindex_kline_1 | kline | 3 | (auto) |
| idx_lp_dk | limit_pool | 2 | CREATE INDEX idx_lp_dk ON limit_pool(kind, date) |
| sqlite_autoindex_limit_pool_1 | limit_pool | 3 | (auto) |
| idx_mlpred_d | ml_pred | 1 | CREATE INDEX idx_mlpred_d ON ml_pred(date) |
| sqlite_autoindex_ml_pred_1 | ml_pred | 2 | (auto) |
| sqlite_autoindex_moneyflow_1 | moneyflow | 3 | (auto) |
| sqlite_autoindex_qg_sentiment_history_1 | qg_sentiment_history | 1 | (auto) |
| sqlite_autoindex_qg_zt_full_1 | qg_zt_full | 1 | (auto) |

### dbstat 体积（top 15）

| 对象 | 字节 | MB |
|---|---|---|
| kline | 711966720 | 679.0 |
| sqlite_autoindex_kline_1 | 313106432 | 298.6 |
| idx_kline_pcd | 295288832 | 281.6 |
| idx_kline_pd | 230453248 | 219.8 |
| idx_kline_cp | 204832768 | 195.3 |
| earnings | 87584768 | 83.5 |
| ml_pred | 48332800 | 46.1 |
| sqlite_autoindex_ml_pred_1 | 41787392 | 39.9 |
| idx_mlpred_d | 30617600 | 29.2 |
| sqlite_autoindex_earnings_1 | 4870144 | 4.6 |
| moneyflow | 2736128 | 2.6 |
| idx_earn_d | 2056192 | 2.0 |
| global_kline | 1196032 | 1.1 |
| sqlite_autoindex_global_kline_1 | 913408 | 0.9 |
| limit_pool | 520192 | 0.5 |

## min5.db（0.74 GB）

| 索引 | 表 | 列数 | SQL |
|---|---|---|---|
| idx_kmin5_c | kline_min5 | 1 | CREATE INDEX idx_kmin5_c ON kline_min5(code) |
| sqlite_autoindex_kline_min5_1 | kline_min5 | 2 | (auto) |

### dbstat 体积（top 15）

| 对象 | 字节 | MB |
|---|---|---|
| kline_min5 | 441438208 | 421.0 |
| sqlite_autoindex_kline_min5_1 | 240607232 | 229.5 |
| idx_kmin5_c | 110850048 | 105.7 |
| sqlite_schema | 4096 | 0.0 |
