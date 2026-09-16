# Phase 70+72+76：统一判定库 judge_kit、回测报告双列化、哨兵补项

- 时间: 2026-08-26 深夜
- 状态: **全部完成**（零 app/ 改动；服务未重启）

## 1. tools/judge_kit.py（统一判定库）

- 单位约定收敛一处：**损失 = 正数 pp**；收益/回撤 = 原始小数；回撤为负。
- API：`window_pass` / `bull_ok` / `occupancy_ok` / `seed_stability` /
  `pit_compare` / `rel_mdd_improve` / `calmar_ratio` / `landing_rule`
- `--selftest` 16 用例全绿（编写过程自身还抓出两处用例期望值错误——
  库正确、测试错，修正后通过）。

## 2. 存量工具迁移（5 个，重算结论零翻转 ✅）

| 工具 | 迁移点 |
|---|---|
| exit_pack_v2_scan.py | window_pass + bull_ok |
| bt_convergence_runner.py | seed_stability（seeds 模式判定块）|
| backtest_dd_gate_v2.py | calmar_ratio + rel_mdd_improve（_calmar 委托）|
| board_weight_ab.py | window_pass（-0.5pp 不恶化口径）|
| exit_param_scan.py | window_pass + bull_ok |

**结论翻转清单：无。** 迁移验证脚本: `tools/rejudge_with_judge_kit.py`
（从五个已存档 JSON 重算全部判定，逐项与原结论比对）。

⚠ 过程事故如实记录：迁移 exit_param_scan 时曾把"Δ 收益(正=更好)"误当
"损失"传入 `bull_ok`，导致 exit_scan 三变体被误判失败——rejudge 对照旧结论
时暴露，已修正符号并复验为零翻转。此坑已写入 judge_kit.md「高频坑位」节。

## 3. 回测报告双列化（P72）

- `tools/report_pit_columns.py`：输入任意含 runs 的回测 JSON → 自动生成
  "静态池 vs PIT 池"双列 markdown 表（PIT 数据源 data/bt_pit_compare.json，
  缺失窗口标"未构建"）。实测输出示例（bt_conv_after board）：

| 策略 | 窗口 | 静态池 | PIT 池 | Δ(pp) | 高估? |
|---|---|---|---|---|---|
| board | 2021-22 | +23.34% | -0.08% | -23.42 | 是 |
| board | 近1年 | +44.95% | +19.06% | -25.89 | 是 |

- `docs/templates/backtest_report_template.md`：报告模板（七段式，
  判定段强制 import judge_kit），后续阶段按此撰写。

## 4. 哨兵补项（P76）

- data_sentinel.py 新增 `check_ml_scores_coverage`：
  scores 数 / pool 数覆盖率 <30% 或信号日滞后 >5 交易日 → WARN。
  首跑即告警命中：**177/1878 = 9% 覆盖率**（predict.py 最新 bar 特征 NaN
  大量剔除所致，诊断归 P75/C 任务）。
- 报告新增键 `ml_scores_coverage`；终端摘要增加 ⑤b 行。

## 5. app.lock 文档化（P76）

main.py 单实例锁（PID 文件 + 存活/端口双重校验）语义与运维要点已写入
docs/operations.md 尾部。

## 6. 交付物清单

- tools/judge_kit.py、tools/rejudge_with_judge_kit.py
- 五个工具的判定迁移（见 §2）
- docs/templates/backtest_report_template.md、tools/report_pit_columns.py
- docs/reports/judge_kit.md、docs/operations.md 补充
- 全部文件 compile 通过、LF 行尾
