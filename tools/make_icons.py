# -*- coding: utf-8 -*-
r"""微信桥 · 图标生成器

生成三套图标（都是 .ico，含 16/24/32/48/64/128/256 多尺寸）：
  wx.ico      正常态：绿底 + 白色对话气泡 + 两只眼睛（已存在，本脚本不动它）
  wx_ok.ico   成功态：绿底 + 大号白色对勾   ← 闪烁时和正常态交替出现
  wx_err.ico  失败态：红底 + 大号白色叉

⚠️ 为什么成功态要做成「大对勾」而不是「气泡里加个小勾」：
   托盘图标实际只有 **16×16**，细节全糊。必须让两态在 16px 下有大面积色块差异
   （白气泡 vs 大白勾），闪烁才看得出来。生成后请务必看 icon_preview.png 复核。

用法：python make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent

GREEN = (7, 193, 96, 255)      # 微信绿
RED = (226, 75, 74, 255)       # 错误红
WHITE = (255, 255, 255, 255)

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _caps(d, pts, r, color):
    """给折线两端补圆帽，让勾/叉的末端是圆的（PIL 的 line 没有 round cap）"""
    for p in pts:
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)


def make_ok(size=256):
    """绿底 + 白色大对勾"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.23),
                        fill=GREEN)
    w = int(size * 0.145)                      # 笔画粗细
    a = (int(size * 0.26), int(size * 0.53))
    b = (int(size * 0.44), int(size * 0.71))
    c = (int(size * 0.75), int(size * 0.31))
    d.line([a, b, c], fill=WHITE, width=w, joint="curve")
    _caps(d, (a, c), w // 2, WHITE)
    return img


def make_err(size=256):
    """红底 + 白色大叉"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.23),
                        fill=RED)
    w = int(size * 0.155)
    p1, p2 = (int(size * 0.28), int(size * 0.28)), (int(size * 0.72), int(size * 0.72))
    p3, p4 = (int(size * 0.72), int(size * 0.28)), (int(size * 0.28), int(size * 0.72))
    d.line([p1, p2], fill=WHITE, width=w, joint="curve")
    d.line([p3, p4], fill=WHITE, width=w, joint="curve")
    _caps(d, (p1, p2, p3, p4), w // 2, WHITE)
    return img


def save_ico(img, path):
    img.save(str(path), format="ICO", sizes=SIZES)


def preview_sheet():
    """把三态在「真实 16px」和「放大 8 倍」两种尺度并排，用来肉眼复核闪烁是否看得清"""
    names = ["wx.ico", "wx_ok.ico", "wx_err.ico"]
    cell, gap, mag = 16, 16, 8
    col = cell * mag
    w = gap + len(names) * (col + gap)
    h = gap + cell + gap + col + gap
    sheet = Image.new("RGBA", (w, h), (245, 245, 245, 255))
    y_small = gap
    y_big = gap + cell + gap
    for i, n in enumerate(names):
        p = BASE / n
        if not p.exists():
            continue
        im = Image.open(str(p)).convert("RGBA")
        x = gap + i * (col + gap)
        small = im.resize((cell, cell), Image.LANCZOS)
        big = im.resize((col, col), Image.NEAREST)
        sheet.alpha_composite(small, (x + (col - cell) // 2, y_small))
        sheet.alpha_composite(big, (x, y_big))
    out = BASE / "icon_preview.png"
    sheet.save(str(out))
    return out


def main():
    save_ico(make_ok(), BASE / "wx_ok.ico")
    save_ico(make_err(), BASE / "wx_err.ico")
    print("已生成 wx_ok.ico / wx_err.ico")
    p = preview_sheet()
    print("对比图 ->", p)


if __name__ == "__main__":
    main()
