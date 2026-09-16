# -*- coding: utf-8 -*-
"""J4（2026-09-13）：import 冒烟——全部 app 模块可导入。
任何 ImportError/SyntaxError 立即失败（5 秒内抓住 pos_cash 那类低级错误的一大半）。
"""
import importlib
import os
import pkgutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import app


def test_import_all_app_modules():
    # J4_APP_PACKAGE：有效性验证注入钩子（默认 app 生产包）
    pkg_name = os.environ.get("J4_APP_PACKAGE", "app")
    pkg = importlib.import_module(pkg_name)
    mods = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__))
    assert len(mods) >= 40, "app 模块数异常: %d" % len(mods)
    failures = []
    for m in mods:
        try:
            importlib.import_module("%s.%s" % (pkg_name, m))
        except Exception as e:  # noqa: BLE001
            failures.append("%s: %r" % (m, e))
    assert not failures, "import 失败:\n" + "\n".join(failures)


def test_import_exit_policy_and_fingerprint_tools():
    """I3/I4 交付物同样可导入（新模块回归防线）。"""
    import app.exit_policy  # noqa: F401
    import tools.bt_fingerprint  # noqa: F401
