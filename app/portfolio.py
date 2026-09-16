# -*- coding: utf-8 -*-
"""组合优化与再平衡（v3.6）—— 借鉴 PyPortfolioOpt / Riskfolio-Lib / riskparity.py / qlib Portfolio Strategy
纯标准库实现（无 numpy/scipy）：
  1. risk_parity():  风险平价（坐标下降法）—— 每项资产风险贡献相等
  2. hrp():          层次风险平价（手写层次聚类 + 递归二分）—— 不依赖协方差可逆
  3. vol_target():   波动率目标调仓 —— 按目标年化波动率缩放组合杠杆/仓位
  4. risk_contrib(): 风险贡献分解（诊断用）
回测引擎与实盘交易中心共用，替换固定比例仓位（POSITION_PCT）。
"""
import math


def mean_returns(closes_by_code, days=60):
    """各股票近 N 日平均日收益。closes_by_code: {code: [close,...]}"""
    out = {}
    for code, closes in closes_by_code.items():
        if len(closes) < 2:
            continue
        seg = closes[-days:] if len(closes) > days else closes
        rets = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg)) if seg[i - 1] > 0]
        out[code] = sum(rets) / len(rets) if rets else 0.0
    return out


def cov_matrix(rets_by_code):
    """协方差矩阵（手写）。rets_by_code: {code: [日收益,...]}（需对齐长度）"""
    codes = [c for c, r in rets_by_code.items() if len(r) >= 5]
    if not codes:
        return {}, []
    n = min(len(rets_by_code[c]) for c in codes)
    n = max(n, 2)
    means = {c: sum(rets_by_code[c][-n:]) / n for c in codes}
    cov = {}
    for i in codes:
        for j in codes:
            s = 0.0
            ri, rj = rets_by_code[i][-n:], rets_by_code[j][-n:]
            for k in range(n):
                s += (ri[k] - means[i]) * (rj[k] - means[j])
            cov[(i, j)] = s / max(n - 1, 1)
    return cov, codes


def corr_matrix(rets_by_code):
    """相关系数矩阵（手写）"""
    cov, codes = cov_matrix(rets_by_code)
    corr = {}
    for i in codes:
        si = math.sqrt(cov[(i, i)]) if cov[(i, i)] > 0 else 0.0
        for j in codes:
            sj = math.sqrt(cov[(j, j)]) if cov[(j, j)] > 0 else 0.0
            corr[(i, j)] = cov[(i, j)] / (si * sj) if si > 0 and sj > 0 else (1.0 if i == j else 0.0)
    return corr, codes


def risk_contrib(weights, cov, codes):
    """风险贡献（MCR/RC）。weights: {code: w}"""
    rc = {}
    total_var = 0.0
    for i in codes:
        wi = weights.get(i, 0.0)
        for j in codes:
            total_var += wi * weights.get(j, 0.0) * cov[(i, j)]
    total_vol = math.sqrt(total_var) if total_var > 0 else 0.0
    if total_vol <= 0:
        return {c: 1.0 / max(1, len(codes)) for c in codes}
    for i in codes:
        wi = weights.get(i, 0.0)
        mcr = sum(weights.get(j, 0.0) * cov[(i, j)] for j in codes)
        rc[i] = wi * mcr / total_vol if total_vol > 0 else 0.0
    return rc


def risk_parity(rets_by_code, iters=300, tol=1e-7):
    """风险平价权重（Spinu 坐标下降法，Riskfolio-Lib/riskparity.py 同款）。
    对每个资产解析求解：w_i * (Σw)_i = b_i * σ_p
    返回 {code: weight}；数据不足时回退等权。
    """
    cov, codes = cov_matrix(rets_by_code)
    n = len(codes)
    if n < 2:
        return {c: 1.0 for c in codes}
    b = 1.0 / n
    # 初始：按各自波动倒数归一化（比等权更接近解，收敛更快）
    vols = {c: math.sqrt(cov[(c, c)]) if cov[(c, c)] > 0 else 1e-6 for c in codes}
    s0 = sum(1.0 / v for v in vols.values())
    w = {c: (1.0 / vols[c]) / s0 for c in codes}

    def portfolio_var():
        var = 0.0
        for i in codes:
            for j in codes:
                var += w[i] * w[j] * cov[(i, j)]
        return max(var, 1e-12)

    for _ in range(iters):
        sigma_p = math.sqrt(portfolio_var())
        old = dict(w)
        for i in codes:
            # 固定其他权重，解 A*w_i^2 + B*w_i - b*σ_p = 0
            A = cov[(i, i)]
            B = sum(w[j] * cov[(i, j)] for j in codes if j != i)
            if A <= 0:
                w[i] = 0.0
                continue
            disc = B * B + 4 * A * b * sigma_p
            if disc < 0:
                continue
            w[i] = (-B + math.sqrt(disc)) / (2 * A)
            if w[i] < 0:
                w[i] = 0.0
        # 归一化 + 收敛检查
        s = sum(w.values())
        if s <= 0:
            w = {c: 1.0 / n for c in codes}
            break
        for c in codes:
            w[c] /= s
        diff = max(abs(w[c] - old[c]) for c in codes)
        if diff < tol:
            break
    return w


def _hierarchical_cluster(corr, codes):
    """层次聚类（单链接），返回聚类的合并顺序（模拟 scipy linkage）"""
    n = len(codes)
    clusters = {c: [c] for c in codes}
    remaining = list(codes)
    order = []
    while len(remaining) > 1:
        # 找最相似（相关系数最高）的两簇
        best_pair = None
        best_sim = -2.0
        for i in range(len(remaining)):
            for j in range(i + 1, len(remaining)):
                ci, cj = remaining[i], remaining[j]
                sims = [corr[(a, b)] for a in clusters[ci] for b in clusters[cj]]
                sim = sum(sims) / len(sims) if sims else -1.0
                if sim > best_sim:
                    best_sim = sim
                    best_pair = (ci, cj)
        if not best_pair:
            break
        a, b = best_pair
        merged = clusters[a] + clusters[b]
        new_id = f"cluster{len(order)}"
        clusters[new_id] = merged
        del clusters[a], clusters[b]
        remaining = [x for x in remaining if x not in (a, b)] + [new_id]
        order.append((a, b, new_id))
    return order, clusters


def _recursive_bisect(order, clusters, w, codes, cov):
    """HRP 递归二分分配（仿 Lopez de Prado）"""
    def vol_of(members):
        var = 0.0
        for i in members:
            for j in members:
                var += w[i] * w[j] * cov[(i, j)]
        return math.sqrt(var) if var > 0 else 0.0

    def allocate(members):
        if len(members) <= 1:
            return
        # 二分：在合并顺序里找到把 members 分成两半的最后一次合并
        left, right = members, []
        # 简单实现：按聚类顺序对半切（members 已含两个子簇）
        if len(members) == 2:
            left, right = [members[0]], [members[1]]
        elif len(members) > 2:
            mid = len(members) // 2
            left, right = members[:mid], members[mid:]
        v_left = vol_of(left) + 1e-12
        v_right = vol_of(right) + 1e-12
        a = v_left / (v_left + v_right)
        for c in left:
            w[c] *= a
        for c in right:
            w[c] *= (1 - a)
        allocate(left)
        allocate(right)

    # 初始化等权再递归
    n = len(codes)
    for c in codes:
        w[c] = 1.0 / n
    if order:
        # 顶层是最后合并的 cluster 的成员
        final = clusters.get(order[-1][2], codes)
        allocate(final)
    # 归一化
    s = sum(w.values())
    if s > 0:
        for c in codes:
            w[c] /= s
    return w


def hrp(rets_by_code):
    """层次风险平价（HRP）。返回 {code: weight}"""
    corr, codes = corr_matrix(rets_by_code)
    if len(codes) < 2:
        return {c: 1.0 for c in codes}
    cov, _ = cov_matrix(rets_by_code)
    order, clusters = _hierarchical_cluster(corr, codes)
    w = {c: 0.0 for c in codes}
    _recursive_bisect(order, clusters, w, codes, cov)
    s = sum(w.values())
    if s <= 0:
        w = {c: 1.0 / len(codes) for c in codes}
    return w


def vol_target(rets_by_code, target_vol=0.15, max_leverage=1.0):
    """波动率目标：组合年化波动率对齐 target_vol。
    返回 {code: weight}（未归一化的目标权重，总和可能<1 表示仓位不足）。
    年化波动率 = 日波动 * sqrt(244)。
    """
    w_rp = risk_parity(rets_by_code)
    cov, codes = cov_matrix(rets_by_code)
    if not codes:
        return w_rp
    var = 0.0
    for i in codes:
        for j in codes:
            var += w_rp[i] * w_rp[j] * cov[(i, j)]
    daily_vol = math.sqrt(var) if var > 0 else 0.0
    annual_vol = daily_vol * math.sqrt(244)
    if annual_vol <= 0:
        return w_rp
    scale = target_vol / annual_vol
    scale = min(scale, max_leverage)
    return {c: w * scale for c, w in w_rp.items()}


def compute_weights(closes_by_code, method="risk_parity", target_vol=0.15,
                    days=60, max_leverage=1.0):
    """统一入口：按 method 计算组合权重。
    method: equal | risk_parity | hrp | vol_target
    返回 {code: weight}
    """
    if method in ("equal", None):
        n = len(closes_by_code)
        return {c: 1.0 / n for c in closes_by_code} if n else {}
    rets_by_code = {}
    for code, closes in closes_by_code.items():
        if len(closes) < 2:
            continue
        seg = closes[-days:] if len(closes) > days else closes
        rets_by_code[code] = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg))
                              if seg[i - 1] > 0]
    if not rets_by_code:
        n = len(closes_by_code)
        return {c: 1.0 / n for c in closes_by_code} if n else {}
    if method == "risk_parity":
        return risk_parity(rets_by_code)
    if method == "hrp":
        return hrp(rets_by_code)
    if method == "vol_target":
        return vol_target(rets_by_code, target_vol, max_leverage)
    return {c: 1.0 / max(1, len(closes_by_code)) for c in closes_by_code}
