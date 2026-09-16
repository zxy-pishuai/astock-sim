# -*- coding: utf-8 -*-
"""K7-1（2026-09-16）：配置模块内同名常量不得重复定义（AST 扫描）。

背景：config.py 曾出现 I 道追加块整体重复（BACKTEST_OFFLINE 定义两处，
:746 与 :754），"后写覆盖先写"且无告警。本用例用 AST 扫描 app/config.py
顶层赋值，任何同名常量定义 >1 次直接判失败，防止多 AI 并行追加时再次撞车。
"""
import ast
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE, "app", "config.py")


def _top_level_assigned_names(tree):
    """返回顶层赋值目标名列表（name=value / a,b=c 展开；忽略推导式/注解内变量）。"""
    names = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for elt in t.elts:
                        if isinstance(elt, ast.Name):
                            names.append(elt.id)
    return names


def test_config_no_duplicate_constant_names():
    assert os.path.isfile(CONFIG_PATH), "config.py 不存在: %s" % CONFIG_PATH
    with open(CONFIG_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read(), CONFIG_PATH)
    names = _top_level_assigned_names(tree)
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, "config.py 存在重复顶层赋值（后写覆盖先写，属隐患）: %s" % dup


def test_backtest_offline_single_definition():
    """回归钉死本次去重目标：BACKTEST_OFFLINE 仅定义一次。"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read(), CONFIG_PATH)
    n = sum(1 for name in _top_level_assigned_names(tree) if name == "BACKTEST_OFFLINE")
    assert n == 1, "BACKTEST_OFFLINE 定义次数=%d（应为 1）" % n
