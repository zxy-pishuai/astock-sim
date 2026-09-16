# -*- coding: utf-8 -*-
"""J4（2026-09-13）：测试套件公共配置。
- 项目根加入 sys.path（可 import app/）
- session 级 autouse 禁网守卫：测试套件任何 urlopen/urlretrieve/requests 外发 → 断言失败
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
# 有效性验证注入副本目录（tmp/j4/：injectcfg.py、badpkg/）可 import
_J4_INJECT_DIR = os.path.join(BASE, "tmp", "j4")
if os.path.isdir(_J4_INJECT_DIR) and _J4_INJECT_DIR not in sys.path:
    sys.path.insert(0, _J4_INJECT_DIR)

import pytest


@pytest.fixture(autouse=True, scope="session")
def _offline_guard():
    """测试套件全局禁网（J4 任务 2：test_no_network_in_tests）。"""
    import urllib.request

    def _block(*a, **k):
        raise AssertionError("测试套件禁止外发网络请求: %s" % (a[0] if a else ""))

    _orig_open = urllib.request.urlopen
    _orig_retr = urllib.request.urlretrieve
    urllib.request.urlopen = _block
    urllib.request.urlretrieve = _block
    _orig_req = None
    try:
        import requests

        def _block_req(self, *a, **k):
            raise AssertionError("测试套件禁止 requests 外发: %s" % (a[0] if a else ""))

        _orig_req = requests.Session.request
        requests.Session.request = _block_req
    except Exception:
        pass
    yield
    urllib.request.urlopen = _orig_open
    urllib.request.urlretrieve = _orig_retr
    if _orig_req is not None:
        import requests

        requests.Session.request = _orig_req
