# 阶段5 LLM 因子挖掘闭环升级演示报告

- 股票池: 400 只｜每轮候选: 5｜轮数: 3｜provider: local（离线模板，无 LLM 也可验证管线）
- 开关（演示进程内开启）: MINER_FEEDBACK_ENABLED=True MINER_CORR_REJECT_ENABLED=True MINER_WF_ENABLED=True | IC门槛|0.03| 相关性阈值0.8 WF折数3 需同向2折

## 第 1 轮
- 回注提示（进入下一轮 prompt 的失败原因，第1轮新增 4 条）：
    · ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    · mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    · ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    · delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|
- 入池因子（过三道闸）: 1
    ✅ ts_std(volumes, 20): IC=-0.0517 ICIR=-0.48 WF=[0.0639, -0.043, -0.0513]
- 拒绝 4 条：
    ❌ ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    ❌ mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    ❌ ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    ❌ delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|

## 第 2 轮
- 回注提示（进入下一轮 prompt 的失败原因，第2轮新增 4 条）：
    · ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    · mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    · ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    · delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|
    · ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    · mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    · ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    · delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|
- 入池因子（过三道闸）: 1
    ✅ ts_std(volumes, 20): IC=-0.0517 ICIR=-0.48 WF=[0.0639, -0.043, -0.0513]
- 拒绝 4 条：
    ❌ ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    ❌ mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    ❌ ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    ❌ delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|

## 第 3 轮
- 回注提示（进入下一轮 prompt 的失败原因，第3轮新增 4 条）：
    · ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    · mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    · ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    · delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|
    · ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    · mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    · ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    · delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|
    · ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    · mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    · ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    · delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|
- 入池因子（过三道闸）: 1
    ✅ ts_std(volumes, 20): IC=-0.0517 ICIR=-0.48 WF=[0.0639, -0.043, -0.0513]
- 拒绝 4 条：
    ❌ ts_corr(rank(closes, 20), rank(volumes, 20), 10) :: 表达式无效: 表达式求值失败: unsupported operand type(s) for +: 'int' and 'NoneType'
    ❌ mul(rank(closes, 60), rank(volumes, 5)) :: IC=-0.0286 低于门槛|0.03|
    ❌ ts_corr(closes, volumes, 10) :: 与因子池[量价相关]相关性1.00>0.8，拒绝入池
    ❌ delta(closes, 5) :: IC=+0.0002 低于门槛|0.03|

## 结论
- 失败原因（IC不足/相关重叠/WF不稳定）随轮次累积并回注入下一轮生成提示 → 挖掘闭环成立
- 三道闸默认关闭（config 默认 False），保持原有挖掘行为不变；开启后才有拒绝/回注/门禁
- 总用时 112s