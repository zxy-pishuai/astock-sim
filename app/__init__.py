# -*- coding: utf-8 -*-
"""A股模拟盘 Pro 后端包

★B-3（2026-09-16，验收方）：国内行情/情绪/资金流源显式绕开系统代理。
背景：壳环境 HTTP(S)_PROXY=127.0.0.1:7890（用户代理工具）。代理一旦关闭，
本进程所有 urllib/requests 外网请求 ConnectionRefused → 全市场行情拉空 →
上涨占比=0 → 崩溃(防守)级禁开仓 + 外围面板冻结（当日实测事故）。
国内源（腾讯/新浪/东财）直连实测 0.21s 成功，不存在需要代理的场景；
声明 NO_PROXY 后 urllib(proxy_bypass) 与 requests(trust_env) 两条栈都遵守。
墙外源（yahoo 等）不受影响——它们不在此清单，仍走系统代理。
"""
import os as _os

_CN_HOSTS = ("gtimg.cn", "sinajs.cn", "sina.com.cn", "eastmoney.com",
             "10jqka.com.cn", "thsstar.com.cn", "mootdx/tdx")
_have = _os.environ.get("NO_PROXY", "") or _os.environ.get("no_proxy", "")
_add = [h for h in _CN_HOSTS if h not in _have]
if _add:
    _joined = (_have + "," if _have else "") + ",".join(_add)
    _os.environ["NO_PROXY"] = _joined
    _os.environ["no_proxy"] = _joined
