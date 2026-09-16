# ML 选股 sidecar（阶段4，严格隔离）

> qlib Alpha158-style 特征 + LightGBM 选股器。**与主系统完全隔离**：
> 独立 venv（numpy / pandas / lightgbm / qlib 只安装在本目录 `.venv`），
> 主系统（app/）零第三方依赖，只**只读**本 sidecar 的输出文件。

## 目录 / 用法（在 tools\ml_sidecar 目录下执行）

```bat
:: 1) 建环境（首次）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2) 夜间离线训练（读 market.db 日K → qlib Alpha158-style 特征 → LightGBM 回归(未来5日收益)）
.venv\Scripts\python.exe train.py --codes 700
::    产出 model.txt + model_feats.json（含 train/valid/test IC）

:: 3) 推理 → 生成主系统只读文件 data\ml_scores.json（{code: 分数, date: 信号日}）
.venv\Scripts\python.exe predict.py --codes 700

:: 4) 离线对比报告（ML分 vs score_stock：IC / top20 五日收益 / 胜率）
.venv\Scripts\python.exe compare_report.py --codes 400 --test_days 40
::    产出 docs\reports\phase4_ml_sidecar.md —— 结论决定是否接入
```

## 数据与口径

- 数据源：`market.db` 日K（immutable 只读连接，不锁库、不写主库）。
- 特征：Alpha158-style（KBAR 族 MA/STD/BETA/RSQR/RESI/MAX/MIN/RANK + VOL 族比值/Z/RANK/相关 + 量价交叉），
  全部单股时序、t 日仅用 ≤t 数据（PIT 安全）。
- 标签：未来 5 日收益（close[t+5]/close[t]-1）。
- 切分：按**行数加权**日期分位 60% / 80%（train/valid/test），时间顺序无未来函数。
- qlib 说明：qlib 已安装于本 venv（满足隔离要求）；官方 Alpha158 依赖 bundle 数据格式，
  本实现按其特征族公式以 pandas 等价实现，避免 bundle 基建依赖 —— 特征族与口径保持一致。

## 主系统接入（默认关闭）

- config：`ML_SCORE_ENABLED = False`；`ML_SCORE_FILE`、`ML_SCORE_MAX_AGE_HOURS=26`。
- `app/scoring.py :: ml_score_bonus(code)`：只读 ml_scores.json，按全市场分位给分
  （前 20% +8 / 后 20% -6）；文件超过 26 小时自动失效降级（记为"ML分过期降级"）。
- 接入判定：`compare_report.py` 显示 ML 的 IC 与 top20 五日收益明显优于 score_stock 才建议接入；
  无显著优势 → 不接入（保留 sidecar 供夜间研究）。

## 隔离边界（勿破坏）

- 主系统不得 import 本目录模块；本目录不得 import app.*（除纯标准库 app/scoring 供对比报告复用评分口径）。
- `data/ml_scores.json` 是唯一跨边界文件，主系统只读 + 时效降级。