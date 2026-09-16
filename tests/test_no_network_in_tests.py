# -*- coding: utf-8 -*-
"""J4（2026-09-13）：测试套件零外发请求——守卫生效自证。"""
import pytest


def test_urlopen_blocked():
    import urllib.request

    with pytest.raises(AssertionError):
        urllib.request.urlopen("http://127.0.0.1:1/x")


def test_urlretrieve_blocked():
    import urllib.request

    with pytest.raises(AssertionError):
        urllib.request.urlretrieve("http://127.0.0.1:1/x")


def test_requests_blocked():
    import requests

    with pytest.raises(AssertionError):
        requests.Session().request("GET", "http://127.0.0.1:1/x")
