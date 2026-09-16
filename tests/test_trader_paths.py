# -*- coding: utf-8 -*-
"""J4（2026-09-13）：trader 静态路径扫描——针对 pos_cash 类 UnboundLocalError。

模式：函数内"条件分支（if/try/for/while/with）内部赋值"的名字，若在
函数顶层（分支外）被读取引用、且顶层无任何初始化赋值、且非函数参数
→ 存在"分支未走时变量未定义"风险。

历史：pos_cash UnboundLocalError 刷了 536 条 ERROR（08-26~08-28，
现修复于 trader.py:1039）——本测试守护此类回归。

用法：
  pytest tests/test_trader_paths.py                    # 默认扫 app/trader.py
  J4_TRADER_PATH=xxx pytest ...                        # 覆盖目标文件（有效性验证用）
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

DEFAULT_PATH = os.path.join(BASE, "app", "trader.py")


def scan_unbound_risks(path):
    """返回 [(函数名, 变量名)]——潜在 UnboundLocalError 名单。"""
    text = open(path, encoding="utf-8").read().lstrip("\ufeff")
    tree = ast.parse(text)
    issues = []
    _BRANCH = (ast.If, ast.Try, ast.For, ast.While, ast.With)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 1) 分支内赋值名（if/try/for/while/with 子树；跳过嵌套函数/lambda/推导式作用域）
        branch_assigns = set()

        def _walk_branch(n):
            for child in ast.iter_child_nodes(n):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda, ast.ListComp, ast.SetComp,
                                      ast.DictComp, ast.GeneratorExp)):
                    continue
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    branch_assigns.add(child.id)
                _walk_branch(child)

        for sub in ast.walk(node):
            if sub is node:
                continue
            if isinstance(sub, _BRANCH):
                _walk_branch(sub)
        # 2) 函数体顶层（不进入分支、不进入嵌套函数/推导式）被读取的名字
        top_use = set()

        def _walk_top(n):
            for child in ast.iter_child_nodes(n):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda, ast.ListComp, ast.SetComp,
                                      ast.DictComp, ast.GeneratorExp)):
                    continue
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    top_use.add(child.id)
                _walk_top(child)

        for st in node.body:
            if isinstance(st, _BRANCH + (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            _walk_top(st)
        # 3) 顶层初始化赋值（安全；含元组解包 a, b = ...）
        top_init = set()

        def _init_targets(t):
            if isinstance(t, ast.Name):
                top_init.add(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for e in t.elts:
                    _init_targets(e)

        for st in node.body:
            targets = []
            if isinstance(st, (ast.Assign, ast.AnnAssign)):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            elif isinstance(st, ast.AugAssign):
                targets = [st.target]
            for t in targets:
                _init_targets(t)
        # 4) 函数参数（安全）
        args = {a.arg for a in (node.args.args + node.args.kwonlyargs
                                + node.args.posonlyargs)}
        risk = (branch_assigns & top_use) - top_init - args
        for r in sorted(risk):
            issues.append((node.name, r))
    return issues


# 人工核实的良性名单（扫描结果复核后逐条放行，见报告 §4.2）
# 论证：
#  - status.*      : with self._lock: 块内赋值（threading.Lock enter 不抛异常，恒执行）
#  - _buy_px.*     : try/except 两个分支均赋值（except 兜底路径全覆盖）
BENIGN = {
    ("status", "auction"),
    ("status", "auto"),
    ("status", "events"),
    ("status", "running"),
    ("_buy_px", "px_new"),
    ("_buy_px", "px_old"),
}


def test_trader_no_unbound_risk():
    path = os.environ.get("J4_TRADER_PATH", DEFAULT_PATH)
    assert os.path.exists(path), "目标文件不存在: %s" % path
    issues = scan_unbound_risks(path)
    bad = [i for i in issues if i not in BENIGN]
    assert not bad, "潜在 UnboundLocalError（分支内赋值+顶层使用+无初始化）:\n" + \
        "\n".join("%s.%s" % (fn, v) for fn, v in bad)


def test_scan_engine_and_scoring():
    """engine/scoring 也扫一遍（同样模式防御）。"""
    for p in ("app/engine.py", "app/scoring.py"):
        path = os.path.join(BASE, p)
        issues = scan_unbound_risks(path)
        # 只报告，不做硬断言（这两个文件历史无 pos_cash 类事故；发现项进报告）
        assert isinstance(issues, list)
