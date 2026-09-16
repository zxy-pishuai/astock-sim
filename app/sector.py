# -*- coding: utf-8 -*-
"""板块数据层（v3.2）：新浪行业板块
- fetch_board_spot():  49个行业板块实时强弱（1次请求，内存TTL缓存）
- get_sector_map():    个股→板块映射（后台逐板块拉成分股，SQLite/JSON缓存每日刷新）
- 映射缺失时回落名称关键词分类（scoring.classify_sector）
"""
import json
import os
import re
import threading
import time

from . import config as C
from . import datafeed as df

_BOARD_SPOT_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"

_spot_cache = {"ts": 0, "data": []}       # [{label,name,count,avg_pct,amount,leader...}]
_map_cache = None                          # {code: [sector,...]}
_map_lock = threading.Lock()
_map_loading = False


def _decode(text):
    try:
        return text.decode("utf-8")
    except Exception:
        return text.decode("gbk", errors="replace")


def fetch_board_spot(force=False):
    """新浪行业板块实时行情。返回 [{label,name,count,avg_pct,up_ratio,amount,leader_code,leader_pct,leader_name}]"""
    now = time.time()
    if not force and _spot_cache["data"] and now - _spot_cache["ts"] < 30:
        return _spot_cache["data"]
    try:
        text = df._http(_BOARD_SPOT_URL, timeout=12, ref="https://finance.sina.com.cn/")
        m = re.search(r"=\s*(\{.*\})\s*;?\s*$", text.strip(), re.S)
        if not m:
            return _spot_cache["data"]
        raw = json.loads(m.group(1))
        out = []
        for label, payload in raw.items():
            f = payload.split(",")
            if len(f) < 8:
                continue
            try:
                out.append({
                    "label": label,
                    "name": f[1],
                    "count": int(float(f[2])),
                    "avg_pct": round(float(f[3]), 2),
                    "amount": float(f[7]),
                    "leader_code": f[8][2:] if len(f) > 8 and f[8] else "",
                    "leader_pct": float(f[9]) if len(f) > 9 else 0.0,
                    "leader_name": f[12] if len(f) > 12 else "",
                })
            except (ValueError, IndexError):
                continue
        if out:
            _spot_cache["data"] = out
            _spot_cache["ts"] = now
        return out
    except Exception:
        return _spot_cache["data"]


def get_top_sectors(top_n=None):
    """TopN 强势板块（涨幅排序，过滤成交额过低）"""
    top_n = top_n or C.SECTOR_TOP_N
    spots = fetch_board_spot()
    spots = [s for s in spots if s["count"] >= C.HERDING_MIN_STOCKS and s["amount"] > 1e8]
    spots.sort(key=lambda s: s["avg_pct"], reverse=True)
    return spots[:top_n]


# ---------- 个股→板块映射（后台预热 + 磁盘缓存） ----------
_MAP_FILE = os.path.join(C.DATA_DIR, "sector_map.json")

_last_fetch_stats = {}     # {板块名: {total,got,pages}} 最近一次全量拉取的诊断计数


def _fetch_all_cons():
    """逐板块拉成分股（新浪 Market_Center 分页），返回 {code: [板块名...]}"""
    mapping = {}
    _last_fetch_stats.clear()
    spots = fetch_board_spot(force=True)
    for s in spots:
        label, name = s["label"], s["name"]
        try:
            cons = _fetch_cons(label)
        except Exception:
            cons = []      # 单板块异常不外抛，不影响其他板块
        for c in cons:
            mapping.setdefault(c, []).append(name)
    return mapping


def _fetch_cons(label):
    """单板块全部成分股代码：按 getHQNodeStockCount 总数完整分页。
    单页失败重试 2 次；重试耗尽止于此板块已取到的部分（容错不外抛），
    单板块失败不影响整体。"""
    import urllib.parse
    base = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData")
    node = urllib.parse.quote(str(label))
    out, seen = [], set()
    # 成分股总数（拿不到则进未知总数模式：按短页停止 + 硬上界防死循环）
    total = 0
    for attempt in (1, 2, 3):
        try:
            cnt_url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                       "Market_Center.getHQNodeStockCount?node=" + node)
            text = df._http(cnt_url, timeout=8, ref="https://finance.sina.com.cn/")
            total = int(text.strip().strip('"'))
            break
        except Exception:
            if attempt < 3:
                time.sleep(0.8 * attempt)
    num = 80
    known_pages = ((total + num - 1) // num) if total > 0 else None
    max_pages = known_pages or 40          # 40×80=3200 > 最大行业成员数
    page = 1
    while page <= max_pages:
        url = (f"{base}?page={page}&num={num}&sort=symbol&asc=1&node={node}"
               f"&symbol=&_s_r_a=page")
        data = None
        for attempt in (1, 2, 3):
            try:
                text = df._http(url, timeout=10, ref="https://finance.sina.com.cn/")
                d = json.loads(text)
                if isinstance(d, list):
                    data = d
                    break
            except Exception:
                if attempt < 3:
                    time.sleep(0.8 * attempt)
        if data is None:
            break                          # 本页重试耗尽：保留已取到的成分
        for item in data:
            sym = item.get("symbol", "")
            code = sym[2:] if sym.startswith(("sh", "sz")) else ""
            if code and code not in seen:
                seen.add(code)
                out.append(code)
        if len(data) < num:
            break                          # 短页 = 尾页
        page += 1
        time.sleep(0.25)                   # 页间温和限速
    _last_fetch_stats[str(label)] = {"total": total, "got": len(out),
                                     "pages": min(page, max_pages)}
    return out


_SHRINK_FLOOR = 0.75     # 新映射代码数低于旧文件的 75% 视为缩水，拒绝写回


def _shrink_guard(old_map, new_map):
    """缩水保护判定。返回 (blocked, (old_n, new_n))。
    无旧文件（首次生成）不拦截；旧文件为空不拦截；比较失败视为不拦截。"""
    try:
        old_n = len(old_map or {})
        new_n = len(new_map or {})
        if old_n > 0 and new_n > 0 and new_n < old_n * _SHRINK_FLOOR:
            return True, (old_n, new_n)
    except Exception:
        pass
    return False, None


def load_sector_map(force=False):
    """个股→板块映射（优先缓存；后台刷新当日数据）"""
    global _map_cache, _map_loading
    if _map_cache and not force:
        return _map_cache
    if os.path.exists(_MAP_FILE) and not force:
        age = time.time() - os.path.getmtime(_MAP_FILE)
        if age < 24 * 3600:
            try:
                with open(_MAP_FILE, "r", encoding="utf-8") as f:
                    _map_cache = json.load(f)
                return _map_cache
            except Exception:
                pass
    with _map_lock:
        if _map_loading:
            return _map_cache or {}
        _map_loading = True
    def _run():
        global _map_cache, _map_loading
        try:
            mapping = _fetch_all_cons()
            if not mapping:
                return
            # ★ 缩水保护：新映射代码数比旧文件少 25% 以上 → 保留旧文件 + 告警，不写回
            old = None
            if os.path.exists(_MAP_FILE):
                try:
                    with open(_MAP_FILE, "r", encoding="utf-8") as f:
                        old = json.load(f)
                except Exception:
                    old = None
            blocked, pair = _shrink_guard(old, mapping)
            if blocked:
                try:
                    from . import audit
                    audit.record(kind="daily", event="sector_map_shrink_blocked",
                                 level="WARN", old_codes=pair[0], new_codes=pair[1],
                                 threshold=_SHRINK_FLOOR,
                                 note="新板块映射疑似拉取不全，已保留旧 sector_map.json")
                except Exception:
                    try:
                        print("[sector] 警告: 新映射 %d 票 < 旧文件 %d 票的75%%，"
                              "疑拉取不全，保留旧文件未写回" % (pair[1], pair[0]))
                    except Exception:
                        pass
                return                  # 缓存与磁盘都保持旧映射的权威地位
            _map_cache = mapping
            try:
                with open(_MAP_FILE, "w", encoding="utf-8") as f:
                    json.dump(mapping, f, ensure_ascii=False)
            except Exception:
                pass
        finally:
            _map_loading = False
    threading.Thread(target=_run, daemon=True).start()
    return _map_cache or {}


def sectors_of(code):
    """个股所属板块列表（真实映射；空则空列表，由调用方回落关键词）"""
    m = load_sector_map()
    return m.get(code, [])


def warm_up():
    """后台预热板块映射（启动时调用）"""
    threading.Thread(target=lambda: (fetch_board_spot(), load_sector_map()),
                     daemon=True).start()
