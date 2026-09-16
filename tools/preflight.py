# -*- coding: utf-8 -*-
"""J4（2026-09-13）：AI 提交前门禁（preflight）。

用法：
  py -3.13 tools/preflight.py                        # 全量：语法 + 冒烟 + 禁区 + 越界 + import 锁定
  py -3.13 tools/preflight.py --scope scope.json     # 指定写权限白名单（allowed glob 列表）
  py -3.13 tools/preflight.py --diff-from-file x.txt # 用文件内容替代 git diff（每行一个路径；验收/假 diff 用）
  py -3.13 tools/preflight.py --no-pytest            # 跳过冒烟（仅静态检查）

检查项（任一失败 → 退出码 1 并列出）：
  1. compileall app tools        —— 语法
  2. pytest tests                —— 冒烟集（离线、只读）
  3. 禁区文件                    —— data/account.json、data/app.lock、data/audit/、tmp/watchdog.log
  4. 越界检查（--scope）         —— diff 文件必须匹配白名单
  5. config.py 保护              —— 只允许"末尾追加"（删行=0、新增行在文件末尾区）
  6. import 锁定                 —— diff 新增 .py 的第三方 import 必须在 requirements.txt 直接依赖集合内
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ_TXT = os.path.join(ROOT, "requirements.txt")

FORBIDDEN_PATTERNS = [
    "data/account.json",
    "data/app.lock",
    "data/audit/",
    "tmp/watchdog.log",
]
# 追加区判定：config.py 新增行必须落在文件末尾最后 N 行内
CONFIG_APPEND_MARGIN = 40


def _py():
    return sys.executable


def _run(args, cwd=None):
    return subprocess.run(args, cwd=cwd or ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def check_compile():
    r = _run([_py(), "-m", "compileall", "-q", "app", "tools"])
    return r.returncode == 0, r.stderr.strip()[:2000]


def check_pytest():
    r = _run([_py(), "-m", "pytest", "tests", "-q", "--no-header"])
    if r.returncode == 0:
        return True, "pytest 全绿"
    tail = [l for l in r.stdout.splitlines() if l.strip()][-15:]
    return False, "\n".join(tail)


def _diff_files(diff_from_file):
    if diff_from_file:
        with open(diff_from_file, encoding="utf-8-sig") as f:
            return [l.strip().replace("\\", "/") for l in f if l.strip()]
    r = _run(["git", "diff", "--name-only", "HEAD"])
    if r.returncode != 0:
        return []
    return [l.strip().replace("\\", "/") for l in r.stdout.splitlines() if l.strip()]


def check_forbidden(files):
    bad = []
    for f in files:
        for pat in FORBIDDEN_PATTERNS:
            if f == pat or f.startswith(pat):
                bad.append(f)
                break
    return (not bad), "禁区文件: %s" % ", ".join(bad) if bad else "无禁区文件"


def check_scope(files, scope_path):
    if not scope_path:
        return True, "未提供 --scope，越界检查跳过（仅查禁区）"
    with open(scope_path, encoding="utf-8-sig") as f:
        allowed = json.load(f).get("allowed", [])
    bad = []
    for f in files:
        if not any(__import__("fnmatch").fnmatch(f, a) for a in allowed):
            bad.append(f)
    return (not bad), "越界文件: %s" % ", ".join(bad) if bad else "均在白名单内"


def check_config_append(files):
    if "app/config.py" not in files:
        return True, "config.py 未变更"
    r = _run(["git", "diff", "-U0", "--", "app/config.py"])
    if r.returncode != 0:
        return True, "无法读 git diff（跳过 config 保护）"
    dels = 0
    adds_before_end = 0
    total_lines = len(open(os.path.join(ROOT, "app", "config.py"),
                           encoding="utf-8").read().splitlines())
    for line in r.stdout.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            dels += 1
        elif line.startswith("+") and not line.startswith("+++"):
            m = re.match(r"\+(\d+),?(\d*)", line)  # hunk 头
            if m:
                line_no = int(m.group(1))
                if line_no < total_lines - CONFIG_APPEND_MARGIN:
                    adds_before_end += 1
    ok = (dels == 0) and (adds_before_end == 0)
    reason = "删行=%d, 末尾区外新增=%d" % (dels, adds_before_end)
    return ok, reason


def _requirements_direct():
    """解析 requirements.txt 直接依赖集合。"""
    deps = set()
    with open(REQ_TXT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = re.split(r"[<>=!~\[;]", line)[0].strip().lower()
            if name:
                deps.add(name.replace("_", "-"))
    return deps


def check_import_lock(files):
    deps = _requirements_direct()
    bad = []
    for f in files:
        if not (f.startswith("app/") or f.startswith("tools/")) or not f.endswith(".py"):
            continue
        p = os.path.join(ROOT, f)
        if not os.path.exists(p):
            continue
        try:
            tree = ast.parse(open(p, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for n in names:
                key = n.replace("_", "-").lower()
                if key in sys.stdlib_module_names:
                    continue
                if key in deps:
                    continue
                # 项目内部包（app/tools/tests 的顶层模块名）
                if key in ("app", "tools", "tests"):
                    continue
                bad.append("%s → %s" % (f, n))
    return (not bad), "未锁定第三方 import: %s" % ", ".join(bad) if bad else "import 均在 requirements.txt 内"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default=None)
    ap.add_argument("--diff-from-file", default=None)
    ap.add_argument("--no-pytest", action="store_true")
    args = ap.parse_args()

    files = _diff_files(args.diff_from_file)
    checks = [("语法 compileall", check_compile())]
    if not args.no_pytest:
        checks.append(("冒烟 pytest", check_pytest()))
    checks.append(("禁区文件", check_forbidden(files)))
    checks.append(("越界 --scope", check_scope(files, args.scope)))
    checks.append(("config 追加保护", check_config_append(files)))
    checks.append(("import 锁定", check_import_lock(files)))

    failed = [(name, why) for name, (ok, why) in checks if not ok]
    print("=== J4 preflight ===")
    print("diff 文件数: %d" % len(files))
    for name, (ok, why) in checks:
        print("  [%s] %s — %s" % ("PASS" if ok else "FAIL", name, why))
    if failed:
        print("结果: FAIL（%d 项）" % len(failed))
        sys.exit(1)
    print("结果: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
