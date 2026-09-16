# -*- coding: utf-8 -*-
"""K14　EPS 一致预期采集器（独立工具，只写 data/vendor/k14，不改 app/config）

==============================================================
★ 预注册判据（K14——先于任何因子评比固定；全文见 docs/reports/K14_eps_factor.md §0）
  F1 横截面构造：PE_fwd = close / eps_consensus(next-year)（用最远"未完成年度"的均值）；
  F2 机构门：n_inst < 3 的样本剔除（预测噪声太大）；
  F3 PIT 纪律：快照当日入档；因子值 = 当日快照与当日 close 结合，
     不得用后续日期快照回填历史（无历史回放，只能"攒"）；
  F4 样本判据：累计 ≥ 20 交易日快照才允许第一次 fwd5 IC 计算；
     IC 显著（月度聚合 ICIR>0.3）且多空单调 → 才可谈因子落地（进 scoring 需另行预注册）。
  F5 结论分级：exploratory（<60 日） / provisional（60~120 日） / confirmed（>120 日）。
==============================================================

端点：basic.10jqka.com.cn/new/{code}/worth.html —— 机构一致预期 EPS 表
     （年度 / 预测机构数 / 最小值 / 均值 / 最大值；r.encoding=gbk）
策略：requests.Session + 串行节流（>=0.8s + 抖动）；失败重试 1 次。
用法：
    python tools/k14_eps_collect.py --limit 10   # 试跑前 N 只
    python tools/k14_eps_collect.py --full       # 全 bt_pool top500
    python tools/k14_eps_collect.py --backfill   # 当日失败码补采（可重复收尾）
"""
import argparse
import json
import pathlib
import random
import re
import sys
import time

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = BASE / "data" / "vendor" / "k14"
OUT.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def parse_eps_rows(txt_html):
    """HTML → 预测表族。含「预测机构数」的两张表依次为：每股收益、净利润。
    行布局: <th>2026</th> <td class=tc>30</td> <td>20.97(最小)</td> <td>均值</td> <td>最大</td> <td>行业</td>。
    返回 {"eps": [...], "profit": [...]}（年度行 dict）。"""
    tables = re.findall(r"<table[\s\S]*?</table>", txt_html)
    kinds = []
    for t in tables:
        if "预测机构数" not in t:
            continue
        out = []
        for tr in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", t):
            cells = []
            for tag, inner in re.findall(r"<(th|td)[^>]*>([\s\S]*?)</\1>", tr):
                c = re.sub(r"<[^>]+>", " ", inner).replace("&nbsp;", " ").strip()
                c = re.sub(r"\s+", " ", c)
                if c:
                    cells.append((tag, c))
            if len(cells) < 3:
                continue
            tags = [t0 for t0, _ in cells]
            vals = [v for _, v in cells]
            if tags[0] == "th" and re.match(r"^(20\d{2})$", vals[0]):
                year = vals[0]
                vals = vals[1:]
            else:
                m_y = re.match(r"^(20\d{2})", vals[0])
                if not m_y:
                    continue
                year = m_y.group(1)
                rest = vals[0][len(year):].strip()
                rest = [v for v in rest.split(" ") if v]
                vals = rest + vals[1:]
            mm_n = re.match(r"^(\d+)$", vals[0]) if vals else None
            if not mm_n:
                continue
            n_inst = int(mm_n.group(1))
            nums = []
            for c in vals[1:]:
                cc = c.replace(",", "")
                try:
                    nums.append(float(cc) if cc not in ("-", "--") else None)
                except ValueError:
                    nums.append(None)
            out.append({
                "year": year, "n_inst": n_inst,
                "v1_min": nums[0] if len(nums) > 0 else None,
                "v2_mean": nums[1] if len(nums) > 1 else None,
                "v3_max": nums[2] if len(nums) > 2 else None,
                "v4_ind_mean": nums[3] if len(nums) > 3 else None,
            })
        if out:
            kinds.append(out)
    return {"eps": kinds[0] if kinds else [], "profit": kinds[1] if len(kinds) > 1 else []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="读当日已有快照，只重采失败码并合并回写")
    args = ap.parse_args()
    import requests
    day = time.strftime("%Y%m%d")
    outpath = OUT / f"eps_snap_{day}.json"
    pool = json.loads((BASE / "data" / "bt_pool.json").read_text(encoding="utf-8"))
    S = requests.Session()
    S.headers.update({"User-Agent": UA, "Referer": "https://basic.10jqka.com.cn/"})

    if args.backfill:
        if not outpath.exists():
            print("无当日快照，退出（先 --full）")
            return
        snap = json.loads(outpath.read_text(encoding="utf-8"))
        todo = [c for c, r in snap.get("rows", {}).items() if not r]
        print(f"[backfill] 目标 {len(todo)} 只（前次失败）")
        ok2 = 0
        for i, code in enumerate(todo):
            try:
                r = S.get(f"https://basic.10jqka.com.cn/new/{code}/worth.html", timeout=12)
                r.encoding = "gbk"
                rows = parse_eps_rows(r.text)
                if rows["eps"] or rows["profit"]:
                    snap["rows"][code] = rows
                    ok2 += 1
            except Exception as e:
                print(f"[WARN] {code} {type(e).__name__} {str(e)[:60]}", flush=True)
            if (i + 1) % 30 == 0:
                print(f"[backfill] {i+1}/{len(todo)} ok={ok2}", flush=True)
            time.sleep(1.6 + random.random() * 0.8)   # 重采节流更保守
        snap["generated_at"] += "+backfill@" + time.strftime("%H:%M:%S")
        outpath.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                           encoding="utf-8", newline="\n")
        print(f"backfill merged: +{ok2}/{len(todo)} → retry 后再跑一遍 --backfill 可收尾")
        return

    codes = pool["codes"][: (len(pool["codes"]) if args.full else args.limit)]
    S = requests.Session()
    S.headers.update({"User-Agent": UA, "Referer": "https://basic.10jqka.com.cn/"})
    snap = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "universe": len(codes), "rows": {}}
    ok = 0
    t0 = time.time()
    for i, code in enumerate(codes):
        try:
            url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
            r = S.get(url, timeout=12)
            r.encoding = "gbk"
            rows = parse_eps_rows(r.text)
            if rows["eps"] or rows["profit"]:
                snap["rows"][code] = rows
                ok += 1
            else:
                snap["rows"][code] = None
        except Exception as e:
            snap["rows"][code] = None
            print(f"[WARN] {code} {type(e).__name__} {str(e)[:60]}", flush=True)
        if (i + 1) % 25 == 0:
            print(f"[collect] {i+1}/{len(codes)} ok={ok} elapsed={time.time()-t0:.0f}s", flush=True)
        time.sleep(0.8 + random.random() * 0.6)
    day = time.strftime("%Y%m%d")
    outpath = OUT / f"eps_snap_{day}.json"
    outpath.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                       encoding="utf-8", newline="\n")
    print(f"written {outpath}  ok={ok}/{len(codes)} ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
