# -*- coding: utf-8 -*-
"""★ Phase70: 统一判定库 —— 所有回测扫描工具的判定逻辑从这里 import，禁止手写。

单位约定（全库统一，违者即是 bug）：
  - 损失（loss）一律为【正数百分点 pp】：base_ret − new_ret > 0 表示变体更差；
  - 收益/回撤保留原始小数（0.05 = 5%），比较时注意换算；
  - 最大回撤为负数或 0（-0.20 = 回撤 20%）。
  历史教训：P37/P68 两轮曾因 "损失(pp) >= 负阈值" 恒真、原始小数对比百分数上限
  两类错误得出错误判定——本库把符号与单位收敛在一处。

用法：
  from tools.judge_kit import window_pass, bull_ok, ...   # 或
  sys.path.insert(0, tools_dir); import judge_kit

自测：python tools/judge_kit.py --selftest   （全绿 = 库可信）
"""
import argparse
import json
import os
import sys


# ---------------- 窗口级判定 ----------------

def window_pass(loss_pp_list, tol_loss_pp=1.0, min_windows=3):
    """逐窗收益损失判定。
    loss_pp_list: 各窗口 (基线收益−变体收益) 的百分点列表（正=变体更差）。
    通过条件：≥ min_windows 个窗口满足 loss_pp <= tol_loss_pp
    （默认容忍上限 +1pp；负值表示该窗改善）。
    ⚠ 历史教训：初版误写成 "loss_pp >= -1.0"，恶化越多的窗口越"达标"
    （与 P37/P68 同款符号病），Phase70 复验时抓出并全局修正。
    返回 (达标窗数, bool)。"""
    ok = sum(1 for x in loss_pp_list if x <= tol_loss_pp)
    return ok, ok >= min_windows


def bull_ok(bull_loss_pp, max_loss_pp=2.0):
    """牛市窗口判定：损失（正数 pp）不得超过 max_loss_pp（默认 2pp）。
    bull_loss_pp 为正数；负值表示牛市反而改善。"""
    return bull_loss_pp is not None and bull_loss_pp <= max_loss_pp


def occupancy_ok(halt_days, half_days, total_days, half_weight=0.5,
                 cap=0.40, min_days=1):
    """gate 占用率判定：(halt + half_weight×half) / total_days ≤ cap。"""
    if total_days < min_days:
        return False, None
    occ = (halt_days + half_weight * half_days) / float(total_days)
    return occ <= cap, round(occ, 4)


# ---------------- 多种子稳健性 ----------------

def seed_stability(returns_by_seed):
    """方向翻转检测。returns_by_seed: {seed: total_return(小数)}。
    返回 dict：direction_flip（多空并存即翻转）、spread_pp（极差）、n。
    seeds 数不足 2 时 n<2，flip 记 None（无法判定）。"""
    vals = [v for v in returns_by_seed.values()]
    n = len(vals)
    if n < 2:
        return {"n": n, "direction_flip": None,
                "spread_pp": None, "returns": returns_by_seed}
    pos = sum(1 for v in vals if v > 0)
    neg = sum(1 for v in vals if v < 0)
    flip = pos > 0 and neg > 0
    spread = (max(vals) - min(vals)) * 100.0
    return {"n": n, "direction_flip": flip, "spread_pp": round(spread, 2),
            "returns": returns_by_seed}


# ---------------- PIT 对照 ----------------

def pit_compare(static_ret, pit_ret):
    """静态池 vs PIT 池结构化对照。输入小数收益。
    返回 {static, pit, delta_pp, direction_same, overstatement_pp}。
    overstatement_pp = static−pit（正值=现行池高估的幅度）。"""
    delta = (pit_ret - static_ret) * 100.0
    return {
        "static_ret": static_ret,
        "pit_ret": pit_ret,
        "delta_pp": round(delta, 2),
        "overstatement_pp": round(-delta, 2),
        "direction_same": (static_ret > 0) == (pit_ret > 0),
    }


# ---------------- 引擎收敛/DD-Gate 辅助 ----------------

def rel_mdd_improve(base_mdd, new_mdd):
    """最大回撤相对改善比例（正=改善）。mdd 为负数或 0。"""
    bm = abs(base_mdd or 0)
    nm = abs(new_mdd or 0)
    if bm == 0:
        return 0.0
    return (bm - nm) / bm


def calmar_ratio(annual_return, max_drawdown):
    """Calmar = 年化收益 / |最大回撤|；回撤缺失或非负时返回 None。"""
    if annual_return is None or max_drawdown is None or max_drawdown >= 0:
        return None
    return annual_return / abs(max_drawdown)


def calmar_improved(base_annual, base_mdd, new_annual, new_mdd):
    bc = calmar_ratio(base_annual, base_mdd)
    nc = calmar_ratio(new_annual, new_mdd)
    return (bc is not None and nc is not None and nc > bc)


# ---------------- 预注册组合规则（常用打包） ----------------

def landing_rule(loss_pp_list, bull_loss_pp, tol_loss_pp=1.0,
                 min_windows=3, bull_max_loss_pp=2.0):
    """"落地建议"标准规则（Phase36/41 系口径）：
    ≥min_windows 个窗口损失 ≤ tol_loss_pp 且 牛市损失 ≤ bull_max_loss_pp。"""
    _, w_ok = window_pass(loss_pp_list, tol_loss_pp=tol_loss_pp,
                          min_windows=min_windows)
    b_ok = bull_ok(bull_loss_pp, max_loss_pp=bull_max_loss_pp)
    return w_ok and b_ok, {"windows_ok": _, "bull_ok": b_ok}


# ---------------- 自测 ----------------

_SELFTEST = [
    # (描述, 判定lambda, expect)。损失正数=更差；tol_loss_pp=容忍上限。
    ("window_pass 基本", lambda: window_pass([0.5, -2.0, 1.0, 3.0],
                                             tol_loss_pp=1.0, min_windows=3)[1], True),
    ("window_pass 计数", lambda: window_pass([0.5, -2.0, 1.0, 3.0])[0], 3),
    ("window_pass 不达标", lambda: window_pass([5.0, 6.0, -3.0, 1.0],
                                               tol_loss_pp=1.0)[1], False),
    ("window_pass 全恶化不可过关", lambda: window_pass([30.7, 6.6, 4.1, 0.75])[1], False),
    ("bull_ok 正常", lambda: bull_ok(2.63), False),
    ("bull_ok 改善", lambda: bull_ok(-28.33), True),
    ("bull_ok None 安全", lambda: bull_ok(None), False),
    ("occupancy 达标", lambda: occupancy_ok(30, 60, 240)[0], True),
    ("occupancy 数值", lambda: occupancy_ok(30, 60, 240)[1], 0.25),
    ("occupancy 超限", lambda: occupancy_ok(200, 50, 250)[0], False),
    ("seed_stability 无翻转", lambda: seed_stability(
        {"7": 0.15, "42": 0.16, "123": 0.15})["direction_flip"], False),
    ("seed_stability 翻转", lambda: seed_stability(
        {"7": 0.15, "42": -0.16})["direction_flip"], True),
    ("pit_compare 高估", lambda: pit_compare(0.354, -0.323)["overstatement_pp"], 67.7),
    ("rel_mdd_improve", lambda: round(rel_mdd_improve(-0.278, -0.156), 4), 0.4388),
    ("calmar_ratio", lambda: calmar_ratio(0.20, -0.10), 2.0),
    ("landing_rule 通过", lambda: landing_rule([0.5, -2.0, 3.0, 0.2], -5.0)[0], True),
    ("landing_rule 牛市超限", lambda: landing_rule([0.5, -2.0, 3.0, 2.63], 2.63)[0], False),
]


def selftest():
    fails = []
    for desc, fn, expect in _SELFTEST:
        got = fn()
        ok = (got == expect)
        if not ok:
            fails.append("%s got=%r expect=%r" % (desc, got, expect))
        print("  [%s] %-24s got=%s" % ("PASS" if ok else "FAIL", desc, got))
    if fails:
        print("SELFTEST FAIL:", fails)
        return 1
    print("SELFTEST ALL PASS (%d cases)" % len(_SELFTEST))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Phase70 统一判定库")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    print(__doc__)
