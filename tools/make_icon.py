# -*- coding: utf-8 -*-
"""生成天玑量化终端应用图标（纯标准库，32-bit BMP ICO）
设计：深蓝渐变底 + 金色上升K线蜡烛 + 白色上升箭头（"天玑"定位财富坐标）
"""
import struct
import os

W = H = 256  # 大尺寸，Windows 会自动缩放

def lerp(a, b, t):
    return int(a + (b - a) * t)

def make_pixels():
    """返回 256x256 BGRA 像素列表"""
    px = []
    # 深蓝渐变背景 (顶部 #0d1b3d → 底部 #0b0e14)
    for y in range(H):
        for x in range(W):
            t = y / (H - 1)
            r = lerp(0x14, 0x0b, t)
            g = lerp(0x24, 0x0e, t)
            b = lerp(0x4a, 0x14, t)
            # 圆角外缘
            cx, cy, rad = W / 2, H / 2, W / 2 - 6
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d > rad + 4:
                r, g, b = 0x0b, 0x0e, 0x14
            px.append((b, g, r, 255))
    # 中心光环（淡金扩散）
    cx, cy = W / 2, H / 2
    for y in range(H):
        for x in range(W):
            i = y * W + x
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d < 78:
                glow = 1.0 - d / 78
                b, g, r, a = px[i]
                r = min(255, r + int(40 * glow))
                g = min(255, g + int(36 * glow))
                b = min(255, b + int(18 * glow))
                px[i] = (b, g, r, 255)

    # 金色 K 线蜡烛（三根，从左下到右上）
    GOLD = (0x40, 0xC8, 0xFF, 255)      # 金色 BGRA
    GOLD_DK = (0x20, 0x9E, 0xE0, 255)
    def candle(x0, y0, body_w, body_h, wick_w, wick_h, col=GOLD):
        # 实体
        for yy in range(y0 - body_h, y0 + 1):
            for xx in range(x0 - body_w // 2, x0 + body_w // 2 + 1):
                if 0 <= xx < W and 0 <= yy < H:
                    px[yy * W + xx] = col
        # 影线
        for yy in range(y0 - body_h - wick_h, y0 + body_h + wick_h + 1):
            for xx in range(x0 - wick_w // 2, x0 + wick_w // 2 + 1):
                if 0 <= xx < W and 0 <= yy < H:
                    px[yy * W + xx] = col
    # 第一根：低位 (70, 200) 实体高50
    candle(70, 205, 26, 55, 6, 12)
    # 第二根：中位 (128, 150) 实体高40
    candle(128, 152, 28, 42, 6, 10)
    # 第三根：高位 (186, 100) 实体高30
    candle(186, 102, 30, 32, 6, 8)

    # 白色上升箭头（右上，金色描边感）
    def arrow(ax, ay, size):
        col = (0xF0, 0xF0, 0xF0, 255)
        for i in range(size):
            # 箭头杆
            for yy in range(ay - size // 3, ay + size // 3 + 1):
                for xx in range(ax - 4, ax + 5):
                    if 0 <= xx < W and 0 <= yy < H:
                        px[yy * W + xx] = col
            # 箭头头部三角
            for dy in range(size // 2):
                half = dy * 2
                for xx in range(ax - half, ax + half + 1):
                    yy = ay - size // 3 - dy
                    if 0 <= xx < W and 0 <= yy < H:
                        px[yy * W + xx] = col
        return
    arrow(208, 62, 34)

    # 底部文字条（模拟"天玑"意象：三颗星）
    stars = [(64, 232), (128, 232), (192, 232)]
    for sx, sy in stars:
        for yy in range(sy - 3, sy + 4):
            for xx in range(sx - 3, sx + 4):
                d = ((xx - sx) ** 2 + (yy - sy) ** 2) ** 0.5
                if d <= 3.5 and 0 <= xx < W and 0 <= yy < H:
                    px[yy * W + xx] = GOLD
    return px


def write_ico(path, sizes=(256, 64, 48, 32, 16)):
    px = make_pixels()
    images = []
    for s in sizes:
        # 缩采样到 s×s
        small = []
        for y in range(s):
            for x in range(s):
                # 取源区域均值（简化：中心点）
                sx = min(W - 1, int((x + 0.5) * W / s))
                sy = min(H - 1, int((y + 0.5) * H / s))
                small.append(px[sy * W + sx])
        # 32-bit BMP 编码（BGRA，自下而上）
        bmp_header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0,
                                 len(small) * 4, 0, 0, 0, 0)
        xor = b"".join(bytes(p) for p in small)
        and_mask = b"\x00" * (((s + 31) // 32) * 4 * s)
        images.append((s, bmp_header + xor + and_mask))

    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    offset = 6 + 16 * count
    for s, data in images:
        entries += struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0,
                               0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries)
        for _, data in images:
            f.write(data)
    print("ICO written:", path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "tianji.ico")
    write_ico(out)
