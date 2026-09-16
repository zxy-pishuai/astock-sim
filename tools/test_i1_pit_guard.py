# -*- coding: utf-8 -*-
"""I1 未来函数护栏：静态扫描 app/ 中所有 earnings_signal( 调用点，
断言回测可达路径上的每一处都显式传了 as_of（且 as_of 非空时 offline=True）。

规则：
  - 回测可达路径：函数签名含 as_of 参数（如 score_stock(..., as_of=)）内部
    的调用点，必须传 as_of=... 且 offline=bool(as_of)（或等价显式）；
  - 非回测路径（函数无 as_of 参数，如 consensus 投票聚合）：as_of=None 语义
    正确（=今天），列为"观察项"（不违规，但未来接入回测需补）；
  - 多行调用：合并至括号闭合再判定；docstring/注释行跳过；
  - 输出违规清单（violations）与观察项（observations），退出码 0=无违规。

用法：py -3.13 tools/test_i1_pit_guard.py
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(_ROOT, "app")

CALL_RE = re.compile(r"\bearnings_signal\s*\(")
SIG_RE = re.compile(r"^def\s+(\w+)\s*\(([^)]*)\)")


def _in_string_doc(lines, idx):
    """跳过三引号 docstring 块与整行注释（三引号奇偶状态机）。"""
    if lines[idx].lstrip().startswith("#"):
        return True
    tri = 0
    for j in range(0, idx + 1):
        tri += lines[j].count('"""')
    return tri % 2 == 1


def _call_block(lines, i):
    """合并多行调用至括号闭合，返回 (起始行, 结束行, 完整文本)。"""
    ln = lines[i]
    depth = ln.count("(") - ln.count(")")
    j = i
    while depth > 0 and j + 1 < len(lines):
        j += 1
        depth += lines[j].count("(") - lines[j].count(")")
    return i, j, "".join(lines[i:j + 1])


def enclosing_function(lines, idx):
    for j in range(idx - 1, -1, -1):
        m = SIG_RE.match(lines[j])
        if m:
            return m.group(1), m.group(2)
    return None, None


def scan():
    violations = []
    observations = []
    for fname in sorted(os.listdir(APP)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(APP, fname)
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for i, ln in enumerate(lines):
            if not CALL_RE.search(ln):
                continue
            if _in_string_doc(lines, i):
                continue
            _, j, block = _call_block(lines, i)
            func, sig = enclosing_function(lines, i)
            if func is None:
                violations.append("%s:%d 无法定位所属函数" % (fname, i + 1))
                continue
            has_as_of = "as_of" in (sig or "")
            call_ok = "as_of=" in block
            offline_ok = "offline=" in block
            if not has_as_of:
                observations.append(
                    "%s:%d 函数 %s() 无 as_of 参数（实盘路径，as_of=None=今天语义正确；"
                    "未来接入回测需显式传 as_of+offline）→ %s"
                    % (fname, i + 1, func, block.strip()[:60]))
            elif not call_ok or not offline_ok:
                violations.append(
                    "%s:%d 回测可达路径 %s() 调用未显式传 as_of/offline → %s"
                    % (fname, i + 1, func, block.strip()[:70]))
    return violations, observations


def main():
    violations, observations = scan()
    print("=== I1 未来函数护栏扫描结果 ===")
    print("违规（回测可达路径未显式传 as_of/offline）：%d 处" % len(violations))
    for v in violations:
        print("  ✗ " + v)
    print("观察项（非回测路径，as_of=None 语义正确）：%d 处" % len(observations))
    for o in observations:
        print("  · " + o)
    if violations:
        print("\n结论：FAIL —— 存在违规调用点，修法：回测可达函数内调用必须 "
              "ea.earnings_signal(code, as_of=as_of, offline=bool(as_of))")
        sys.exit(1)
    print("\n结论：PASS —— 回测可达路径全部显式传 as_of/offline")


if __name__ == "__main__":
    main()
