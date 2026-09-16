# -*- coding: utf-8 -*-
"""K15 提案一：竞价哨兵（S1 判据预注册：docs/reports/K15_speedup_proposals.md §0）。
09:20-09:26 每 10s 拉 eltdx auctions.series，算"竞价撮合增量 + 未撮合挂单
净变化（撤单/加单方向）"，落 data/logs/auction_sentinel.jsonl。只读独立工具，
不碰 trader 主链；09:25 前 60s 内报出净买增量越过阈值的票。
用法：
  python tools/auction_sentinel.py --once --codes 000993,600519     # 盘后验证
  python tools/auction_sentinel.py --codes ...                     # 常驻（窗口外空转）
"""
import argparse
import json
import time

import sys
sys.path.insert(0, '.')


def sentinel_round(codes):
    """单轮采样。返回逐码记录；失败码记 err 行（判据"零异常"的原料）。"""
    from app import l2_auction
    c = l2_auction._get_client()
    if c is None:
        return [{"t": time.strftime("%H:%M:%S"), "err": "eltdx client 无法建立"}]
    out = []
    for code in codes:
        rec = {"t": time.strftime("%H:%M:%S"), "code": code}
        try:
            s = c.auctions.series(code)
            pts = [p for p in s.points if p.time_label <= "09:24:59"]
            if len(pts) < 3:
                rec["no_data"] = True
            else:
                fin, prev = pts[-1], pts[-2]
                rec["d_match"] = fin.matched_volume - prev.matched_volume
                # 未撮合净变化：正=卖方撤单方向净增；负=买方方向净减（撤单语义）
                rec["d_unmatch"] = fin.unmatched_signed_raw - prev.unmatched_signed_raw
                rec["unmatch_dir"] = fin.unmatched_direction_raw
                rec["price"] = round(fin.price, 3)
        except Exception as e:
            rec["err"] = str(e)[:80]
        out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单轮（盘后验证用）")
    ap.add_argument("--codes", required=True, help="逗号分隔 6 位代码")
    ap.add_argument("--threshold_unmatch", type=int, default=1000000,
                    help="撤单/加单增量告警阈值（股）")
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    log = open("data/logs/auction_sentinel.jsonl", "a", encoding="utf-8", buffering=1)
    try:
        while True:
            hm = time.strftime("%H:%M")
            if args.once or ("09:20" <= hm <= "09:26"):
                for r in sentinel_round(codes):
                    du = r.get("d_unmatch") or 0
                    if abs(du) >= args.threshold_unmatch and r.get("err") is None:
                        r["alert"] = ("放量撤单(卖向撤)" if du > 0 else "加单(买向挂)")
                        # 口径按 eltdx 实测：signed_raw>0 = 买撤方向占比减 → 卖撤
                    log.write(json.dumps(r, ensure_ascii=False) + "\n")
                if args.once:
                    break
            elif args.once:
                break
            else:
                time.sleep(10)
    finally:
        log.close()


if __name__ == "__main__":
    main()
