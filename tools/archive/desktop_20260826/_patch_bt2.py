import pathlib, py_compile
p = pathlib.Path(r"C:\Users\26838\A股模拟盘\app\board_true.py")
t = p.read_text(encoding="utf-8")
old = '    def __init__(self, codes, names, start, end, initial_capital=100000.0,\n                 fill_model="M2", max_positions=2, position_pct=0.25,\n                 p_early=0.3, p_late=0.6, early_cutoff="10:30", seed=42):\n        self.codes = [c for c in codes'
new = '    def __init__(self, codes, names, start, end, initial_capital=100000.0,\n                  fill_model="M2", max_positions=2, position_pct=0.25,\n                  p_early=None, p_late=None, early_cutoff=None, seed=42,\n                  fill_params=None):\n        # P65 参数化：fill_prob 进 params（fill_params 优先，其次 config，显式仍兼容）\n        fp = fill_params or {}\n        if p_early is None:\n            p_early = fp.get("p_early", getattr(C, "SHADOW_BOARD_FILL_P_EARLY", 0.30))\n        if p_late is None:\n            p_late = fp.get("p_late", getattr(C, "SHADOW_BOARD_FILL_P_LATE", 0.60))\n        if early_cutoff is None:\n            early_cutoff = fp.get("early_cutoff", getattr(C, "SHADOW_BOARD_EARLY_CUTOFF", "10:30"))\n        self.codes = [c for c in codes'
assert old in t, "not found2"
t2 = t.replace(old, new, 1)
p.write_text(t2, encoding="utf-8", newline="\n")
print("patched ok")
py_compile.compile(str(p), doraise=True)
print("compile ok")
print(t2[1200:2200])
