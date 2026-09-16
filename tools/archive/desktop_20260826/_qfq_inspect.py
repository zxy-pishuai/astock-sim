import json, os, sqlite3, pathlib, collections
p=r"C:\Users\26838\A股模拟盘\data\quality_report.json"
d=json.loads(open(p,encoding='utf-8').read())
print("keys", list(d.keys()))
# find candidates field
# print top keys
for k,v in d.items():
    if isinstance(v, dict):
        print(k, list(v.keys())[:20], len(v) if v else 0)
    elif isinstance(v, list):
        print(k, "list len", len(v), v[:2] if len(v)<5 else v[:1])
    else:
        print(k, v if not isinstance(v, str) or len(str(v))<200 else str(v)[:200])

# deeper: find where candidates
import json as js
# try to locate qfq candidates
text=open(p,encoding='utf-8').read()
# search for high counts
# print sections containing candidate
for sec in ["qfq","jump","sentinel","candidates","anomaly","high","exclude"]:
    if sec in text.lower():
        idx=text.lower().find(sec)
        print("\n---",sec,"at",idx)
        print(text[max(0,idx-500):idx+1500][:3000])
