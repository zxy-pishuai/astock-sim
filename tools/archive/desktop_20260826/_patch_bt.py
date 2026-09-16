import pathlib, py_compile
p = pathlib.Path(r"C:\Users\26838\A股模拟盘\app\board_true.py")
t = p.read_text(encoding="utf-8")
old = """    def __init__(self, codes, names, start, end, initial_capital=100000.0,
                  fill_model="M2", max_positions=2, position_pct=0.25,
                  p_early=0.3, p_late=0.6, early_cutoff="10:30", seed=42):
        self.codes = [c for c in codes"""
new = """    def __init__(self, codes, names, start, end, initial_capital=100000.0,
                  fill_model="M2", max_positions=2, position_pct=0.25,
                  p_early=None, p_late=None, early_cutoff=None, seed=42,
                  fill_params=None):
        # P65 参数化：fill_prob 进 params（fill_params 优先，其次 config，显式仍兼容）
        fp = fill_params or {}
        if p_early is None:
            p_early = fp.get("p_early", getattr(C, "SHADOW_BOARD_FILL_P_EARLY", 0.30))
        if p_late is None:
            p_late = fp.get("p_late", getattr(C, "SHADOW_BOARD_FILL_P_LATE", 0.60))
        if early_cutoff is None:
            early_cutoff = fp.get("early_cutoff", getattr(C, "SHADOW_BOARD_EARLY_CUTOFF", "10:30"))
        self.codes = [c for c in codes"""
if old not in t:
    print("OLD NOT FOUND")
    # dump around
    idx = t.find("p_early=0.3")
    print(repr(t[idx-200:idx+400]))
else:
    t2 = t.replace(old, new, 1)
    p.write_text(t2, encoding="utf-8", newline="\n")
    print("patched")
    py_compile.compile(str(p), doraise=True)
    print("compile ok")
