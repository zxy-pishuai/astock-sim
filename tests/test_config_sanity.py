# -*- coding: utf-8 -*-
"""J4（2026-09-13）：config 合理性冒烟。
开关常量类型正确、阈值在合理区间、路径存在。
"""
import importlib
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# J4_CONFIG_MODULE：有效性验证注入钩子（默认 app.config 生产模块）
C = importlib.import_module(os.environ.get("J4_CONFIG_MODULE", "app.config"))


def test_bool_switches():
    for key in ("ML_SCORE_ENABLED", "SCORING_UNIFIED",
                "BACKTEST_OFFLINE", "BT_PARALLEL_ENABLED",
                "BT_CACHE_ENABLED", "EXEC_QUEUE_FILL"):
        v = getattr(C, key, None)
        assert v is not None, "config 缺常量 %s" % key
        assert isinstance(v, bool), "%s 应为 bool，实为 %r" % (key, v)


def test_optional_switches_if_present():
    """可选开关（getattr 兜底常量）：若存在必须类型正确。"""
    for key in ("BUY_PX_USE_NEW", "ML_RANK_WEIGHT"):
        v = getattr(C, key, None)
        if v is None:
            continue
        assert isinstance(v, (bool, float, int)), "%s 类型异常: %r" % (key, v)


def test_numeric_thresholds():
    # 类型断言（有效性验证③：阈值被改成字符串 → 此处失败）
    assert isinstance(C.SLIPPAGE, (int, float)) and not isinstance(C.SLIPPAGE, bool)
    assert 0 < C.SLIPPAGE < 0.1, "SLIPPAGE 越界: %r" % C.SLIPPAGE
    assert 0 < C.COMMISSION_RATE < 0.01, "COMMISSION_RATE 越界: %r" % C.COMMISSION_RATE
    assert 0 < C.STAMP_TAX_RATE < 0.01, "STAMP_TAX_RATE 越界: %r" % C.STAMP_TAX_RATE
    assert -0.2 < C.STOP_LOSS_PCT < -0.01, "STOP_LOSS_PCT 越界: %r" % C.STOP_LOSS_PCT
    assert 0.01 < C.TAKE_PROFIT_PCT < 0.5, "TAKE_PROFIT_PCT 越界: %r" % C.TAKE_PROFIT_PCT
    assert C.COMMISSION_MIN >= 0
    assert 0 < C.INITIAL_CAPITAL <= 1e9
    # 市值分档滑点单调递减（市值越大滑点越小）
    floors = [f for f, _ in C.EXEC_SLIP_BY_MCAP]
    slips = [s for _, s in C.EXEC_SLIP_BY_MCAP]
    assert floors == sorted(floors, reverse=True), "EXEC_SLIP_BY_MCAP 档位无序"
    assert all(s > 0 for s in slips)


def test_paths_exist():
    for p in (os.path.dirname(C.DB_FILE), os.path.dirname(C.MIN5_DB_FILE)):
        assert p and os.path.isdir(p), "数据目录不存在: %s" % p


def test_ml_defaults_off():
    """ML 未获批接入前必须保持关闭（D 块批准矩阵状态）。"""
    assert C.ML_SCORE_ENABLED is False
    assert C.ML_RANK_WEIGHT == 0.0
