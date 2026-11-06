#!/usr/bin/env python3
"""Draw the logo, app icons and the link-preview image (assets/). Needs skia-python:

    pip install skia-python
    python3 make_assets.py

Everything is drawn from shapes and text (no manga art), so it is safe to publish. The page uses:
    assets/icon.svg          the favicon and manifest icon
    assets/icon-192.png      app icon (manifest)
    assets/icon-512.png      app icon (manifest)
    assets/apple-touch-icon.png   iPhone home-screen icon
    assets/og-image.png      the picture people see when the link is shared (1200 x 630)
"""

import os

import skia

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets")
VIOLET, PINK = 0xFF7A68FF, 0xFFFF6B9D
BOOK_PATH = (
    "M9 9.5h6.2c1.2 0 2.3.5 2.8 1.4.5-.9 1.6-1.4 2.8-1.4H23v13h-2.9c-1.1 0-2.1.4-2.6 1.1h-3"
    "c-.5-.7-1.5-1.1-2.6-1.1H9z"
)

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#8b7bff"/><stop offset="1" stop-color="#ff6b9d"/>
  </linearGradient></defs>
  <rect width="32" height="32" rx="9" fill="url(#g)"/>
  <path d="%s" fill="none" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
  <path d="M16 11v12.5" stroke="#fff" stroke-width="1.8"/>
</svg>
""" % BOOK_PATH


def book_path():
    """BOOK_PATH written out by hand (skia-python has no SVG path parser): an open book."""
    p = skia.Path()
    p.moveTo(9, 9.5)
    p.lineTo(15.2, 9.5)
    p.cubicTo(16.4, 9.5, 17.5, 10.0, 18.0, 10.9)
    p.cubicTo(18.5, 10.0, 19.6, 9.5, 20.8, 9.5)
    p.lineTo(23, 9.5)
    p.lineTo(23, 22.5)
    p.lineTo(20.1, 22.5)
    p.cubicTo(19.0, 22.5, 18.0, 22.9, 17.5, 23.6)
    p.lineTo(14.5, 23.6)
    p.cubicTo(14.0, 22.9, 13.0, 22.5, 11.9, 22.5)
    p.lineTo(9, 22.5)
    p.close()
    return p


def font(size, bold=True):
    style = skia.FontStyle.Bold() if bold else skia.FontStyle.Normal()
    return skia.Font(skia.Typeface.MakeFromName("Helvetica Neue", style), size)


def paint(color=None, shader=None, blur=0, stroke=None, alpha=None):
    p = skia.Paint(AntiAlias=True)
    if color is not None:
        p.setColor(color)
    if shader is not None:
        p.setShader(shader)
    if blur:
        p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur))
    if stroke:
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(stroke)
        p.setStrokeJoin(skia.Paint.kRound_Join)
        p.setStrokeCap(skia.Paint.kRound_Cap)
    if alpha is not None:
        p.setAlphaf(alpha)
    return p


def logo(canvas, x, y, size):
    """The 32x32 logo (rounded gradient square with an open book), scaled to `size` px."""
    canvas.save()
    canvas.translate(x, y)
    s = size / 32
    canvas.scale(s, s)
    grad = skia.GradientShader.MakeLinear([(0, 0), (32, 32)], [VIOLET, PINK])
    canvas.drawRoundRect(skia.Rect.MakeWH(32, 32), 9, 9, paint(shader=grad))
    canvas.drawPath(book_path(), paint(0xFFFFFFFF, stroke=1.8))
    canvas.drawLine(16, 11, 16, 23.5, paint(0xFFFFFFFF, stroke=1.8))
    canvas.restore()


def save(surface, name):
    image = surface.makeImageSnapshot()
    image.save(os.path.join(OUT, name), skia.kPNG)
    print("wrote", os.path.join("assets", name))


def icon(size, name, margin=0.0):
    surface = skia.Surface(size, size)
    c = surface.getCanvas()
    c.clear(0xFF090B10)
    pad = size * margin
    logo(c, pad, pad, size - 2 * pad)
    save(surface, name)


def og_image():
    w, h = 1200, 630
    surface = skia.Surface(w, h)
    c = surface.getCanvas()
    c.clear(0xFF090B10)
    # the page's signature glow, top left and top right
    c.drawCircle(120, 40, 360, paint(0x33 << 24 | 0x8B7BFF, blur=140))
    c.drawCircle(1120, 20, 300, paint(0x22 << 24 | 0xFF6B9D, blur=130))
    # a fan of abstract "covers" (plain gradients, no artwork)
    shades = [(0xFF1B2030, 0xFF8B7BFF), (0xFF1B2030, 0xFFFF6B9D), (0xFF1B2030, 0xFF3DDC97)]
    for i, (a, b) in enumerate(shades):
        c.save()
        c.translate(880 + i * 98, 372 + (i % 2) * 18)
        c.rotate(-8 + i * 9)
        grad = skia.GradientShader.MakeLinear([(0, 0), (190, 285)], [a, b])
        c.drawRoundRect(skia.Rect.MakeXYWH(-95, -142, 190, 285), 18, 18, paint(shader=grad))
        c.drawRoundRect(skia.Rect.MakeXYWH(-95, -142, 190, 285), 18, 18, paint(0x33FFFFFF, stroke=2))
        c.drawRoundRect(skia.Rect.MakeXYWH(-75, 100, 150, 8), 4, 4, paint(0x55FFFFFF))
        c.drawRoundRect(skia.Rect.MakeXYWH(-75, 118, 90, 8), 4, 4, paint(0x33FFFFFF))
        c.restore()
    logo(c, 80, 96, 96)
    c.drawString("Manga Tracker", 80, 300, font(96), paint(0xFFEEF0F6))
    c.drawString("Never wonder if your manga is on break.", 84, 362, font(38, bold=False), paint(0xFF8E95A8))
    # what makes it different, as three small chips
    x = 84
    for label in ("6 sources compared", "daily bot", "77 tests"):
        width = font(28).measureText(label) + 44
        c.drawRoundRect(skia.Rect.MakeXYWH(x, 452, width, 58), 29, 29, paint(0xFF131722))
        c.drawRoundRect(skia.Rect.MakeXYWH(x, 452, width, 58), 29, 29, paint(0x28FFFFFF, stroke=2))
        c.drawString(label, x + 22, 490, font(28), paint(0xFFEEF0F6))
        x += width + 16
    save(surface, "og-image.png")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "icon.svg"), "w") as f:
        f.write(ICON_SVG)
    print("wrote assets/icon.svg")
    icon(512, "icon-512.png", margin=0.1)
    icon(192, "icon-192.png", margin=0.1)
    icon(180, "apple-touch-icon.png", margin=0.08)
    og_image()
