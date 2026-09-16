import pathlib
p=pathlib.Path(r"C:\Users\26838\A股模拟盘\app\board_true.py")
t=p.read_text(encoding="utf-8")
old = '    def __init__(self, codes, names, start, end, initial_capital=100000.0,\n                 fill_model="M2", max_positions=2, position_pct=0.25,\n                 p_early=0.3, p_late=0.6, early_cutoff="10:30", seed=42):\n        self.codes = [c for c in codes'
print("old in t?", old in t)
b=p.read_bytes()
print("CR?", b"\r" in b)
print("LF", b.count(b"\n"))
