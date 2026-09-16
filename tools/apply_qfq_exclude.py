# -*- coding: utf-8 -*-
"""一键落库 QFQ 109 只建议清单到 app/config.py（需验收后显式执行，幂等，去重，LF）。

用法: python tools/apply_qfq_exclude.py --confirm

只改 app/config.py 的 DATA_EXCLUDE_CODES 列表，追加 data/qfq_triage.json 的 suggested_DATA_EXCLUDE_CODES 中 code，去重排序后写回。
日志记 audit(kind=daily, event=qfq_exclude_applied)。
"""
import json, sys, pathlib, re
BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="确认后才写入")
    ap.add_argument("--dry", action="store_true", help="只预览，不写入")
    a = ap.parse_args()
    if not a.confirm and not a.dry:
        print("需加 --confirm 才写入（或 --dry 预览）")
        sys.exit(2)
    qfq_json = BASE / "data" / "qfq_triage.json"
    cfg_path = BASE / "app" / "config.py"
    d = json.loads(qfq_json.read_text(encoding="utf-8"))
    suggested = [x["code"] for x in d.get("suggested_DATA_EXCLUDE_CODES", [])]
    if not suggested:
        print("无建议清单")
        sys.exit(0)
    text = cfg_path.read_text(encoding="utf-8")
    m = re.search(r"DATA_EXCLUDE_CODES\s*=\s*\[(.*?)\]", text, flags=re.S)
    if not m:
        print("未找到 DATA_EXCLUDE_CODES")
        sys.exit(1)
    inner = m.group(1)
    existing = re.findall(r'"([^"]+)"', inner)
    existing_set = set(existing)
    new_codes = [c for c in suggested if c not in existing_set]
    if a.dry:
        print(f"现有 {len(existing)} 只，建议新增 {len(new_codes)} 只")
        for c in new_codes[:20]:
            print(" +", c)
        if len(new_codes) > 20:
            print(f" ... 余 {len(new_codes)-20} 只")
        sys.exit(0)
    if not new_codes:
        print("无新增，已是最新")
        sys.exit(0)
    # 保持原有顺序，新增追加到末尾（去重），按出现顺序
    all_codes = existing + new_codes
    # 重建块：每行一个 code，保留注释为 P54 标记
    # 取 suggested 的 config_line 作为注释
    code_to_line = {x["code"]: x.get("config_line","    \"%s\","%x["code"]) for x in d.get("suggested_DATA_EXCLUDE_CODES", [])}
    # 保留原有行的注释（若有）
    lines = []
    for c in existing:
        # 在原 inner 中找该 code 的行
        pat = re.search(r'"' + re.escape(c) + r'"[^,\n]*,?', inner)
        # 简化：直接用裸 code 行
        lines.append(f'    "{c}",')
    for c in new_codes:
        lines.append(code_to_line.get(c, f'    "{c}",'))
    new_block = "DATA_EXCLUDE_CODES = [\n" + "\n".join(lines) + "\n]"
    text2 = text[:m.start()] + new_block + text[m.end():]
    cfg_path.write_text(text2, encoding="utf-8", newline="\n")
    print(f"已落库 {len(new_codes)} 只，当前共 {len(all_codes)} 只 -> {cfg_path}")
    try:
        import py_compile
        py_compile.compile(str(cfg_path), doraise=True)
        print("compile ok")
    except Exception as e:
        print(f"compile failed: {e}")
        sys.exit(1)
    try:
        from app import audit
        audit.record(kind="daily", event="qfq_exclude_applied", level="INFO", added=len(new_codes), total=len(all_codes))
    except Exception:
        pass

if __name__ == "__main__":
    main()
