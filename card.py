"""Headline card (final fallback when no suitable real photo exists for a story).

Arabic needs shaping (letters joined) and bidi reordering before drawing; Pillow is forced to the basic
layout engine so it does not reorder the text a second time.
"""
import hashlib
import io
import re
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT_BOLD = ROOT / "assets" / "fonts" / "Cairo-ExtraBold.ttf"
FONT_MED = ROOT / "assets" / "fonts" / "Cairo-Medium.ttf"
LOGO = ROOT / "assets" / "logo.png"  # optional: if present it is pasted on the card
W, H = 1200, 630
MARGIN = 80
ACCENTS = ["#2F80ED", "#27AE60", "#F2994A", "#EB5757", "#9B51E0"]


def _font(path, size):
    return ImageFont.truetype(str(path), size, layout_engine=ImageFont.Layout.BASIC)


def _shape(text):
    return get_display(arabic_reshaper.reshape(text))


def _wrap(text, font, max_w):
    lines, cur = [], []
    for word in text.split():
        trial = " ".join(cur + [word])
        if cur and font.getlength(_shape(trial)) > max_w:
            lines.append(" ".join(cur))
            cur = [word]
        else:
            cur.append(word)
    if cur:
        lines.append(" ".join(cur))
    return lines


def _gradient(c1, c2, angle=45):
    """Smooth linear gradient c1 -> c2 along a direction (angle in degrees, 0 = left to right)."""
    import math
    t = math.radians(angle)
    dx, dy = math.cos(t), math.sin(t)
    small = Image.new("L", (64, 64))
    px = small.load()
    vals = [(x - 31.5) * dx + (y - 31.5) * dy for y in range(64) for x in range(64)]
    lo, hi = min(vals), max(vals)
    for i, v in enumerate(vals):
        px[i % 64, i // 64] = int(255 * (v - lo) / (hi - lo))
    mask = small.resize((W, H), Image.BILINEAR)
    return Image.composite(Image.new("RGB", (W, H), c2), Image.new("RGB", (W, H), c1), mask)


def _overlay():
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


# ---- backgrounds: each returns (RGBA image, text colour, footer line colour, brand colour, small text colour) ----
def _bg_navy(accent):
    img = _gradient((7, 18, 38), (16, 58, 128)).convert("RGBA")
    o = _overlay()
    d = ImageDraw.Draw(o)
    d.ellipse((-260, 280, 420, 960), outline=(255, 255, 255, 38), width=36)
    d.ellipse((-120, 420, 280, 820), outline=(255, 255, 255, 24), width=14)
    d.rectangle((W - 22, 0, W, H), fill=accent)
    return Image.alpha_composite(img, o), "white", (255, 255, 255, 70), accent, (255, 255, 255, 190)


def _bg_cream(accent):
    img = Image.new("RGBA", (W, H), (247, 242, 232, 255))
    o = _overlay()
    d = ImageDraw.Draw(o)
    d.polygon([(0, 0), (330, 0), (0, 330)], fill=accent)
    d.rectangle((W - 22, 0, W, H), fill=accent)
    return Image.alpha_composite(img, o), (20, 24, 40), (20, 24, 40, 90), accent, (20, 24, 40, 170)


def _bg_pitch(accent):
    img = _gradient((10, 80, 40), (20, 120, 60), 70).convert("RGBA")
    o = _overlay()
    d = ImageDraw.Draw(o)
    line = (255, 255, 255, 40)
    d.ellipse((W // 2 - 230, H // 2 - 230, W // 2 + 230, H // 2 + 230), outline=line, width=8)
    d.line((W // 2, 0, W // 2, H), fill=line, width=8)
    d.rectangle((-8, H // 2 - 180, 150, H // 2 + 180), outline=line, width=8)
    d.rectangle((W - 150, H // 2 - 180, W + 8, H // 2 + 180), outline=line, width=8)
    for i in range(0, W, 150):  # mowing stripes
        if (i // 150) % 2 == 0:
            d.rectangle((i, 0, i + 150, H), fill=(255, 255, 255, 10))
    return Image.alpha_composite(img, o), "white", (255, 255, 255, 90), (255, 224, 102), (255, 255, 255, 200)


def _bg_neon(accent):
    img = Image.new("RGBA", (W, H), (10, 10, 14, 255))
    o = _overlay()
    d = ImageDraw.Draw(o)
    for x in range(0, W, 60):
        d.line((x, 0, x, H), fill=(255, 255, 255, 12), width=1)
    for y in range(0, H, 60):
        d.line((0, y, W, y), fill=(255, 255, 255, 12), width=1)
    d.rectangle((MARGIN, 40, W - MARGIN - 20, 48), fill=accent)
    d.rectangle((0, 0, 14, H), fill=accent)
    return Image.alpha_composite(img, o), "white", accent, accent, (255, 255, 255, 170)


def _bg_bold(accent):
    img = Image.new("RGBA", (W, H), accent)
    o = _overlay()
    d = ImageDraw.Draw(o)
    for i in range(-H, W, 90):
        d.polygon([(i, H), (i + 45, H), (i + 45 + H, 0), (i + H, 0)], fill=(0, 0, 0, 22))
    d.rectangle((0, H - 120 - 6, W, H), fill=(15, 15, 20, 235))
    return Image.alpha_composite(img, o), (255, 255, 255), (255, 255, 255, 0), (255, 255, 255), (255, 255, 255, 200)


def _bg_sunset(accent):
    img = _gradient((60, 14, 84), (232, 84, 60), 60).convert("RGBA")
    o = _overlay()
    d = ImageDraw.Draw(o)
    d.ellipse((W - 420, -220, W + 160, 360), fill=(255, 214, 120, 60))
    d.ellipse((W - 300, -140, W + 60, 220), fill=(255, 230, 160, 70))
    d.rectangle((MARGIN, H - 135, MARGIN + 160, H - 129), fill=(255, 214, 120, 255))
    return Image.alpha_composite(img, o), "white", (255, 255, 255, 60), (255, 214, 120), (255, 255, 255, 200)


STYLES = {"navy": _bg_navy, "cream": _bg_cream, "pitch": _bg_pitch, "neon": _bg_neon, "bold": _bg_bold, "sunset": _bg_sunset}
STYLE_NAMES = list(STYLES)


def make_card(title, brand="شيكس", sub="chex", style=None):
    """Return JPEG bytes of a 1200x630 headline card. style: one of STYLE_NAMES (default: picked from the title)."""
    title = re.sub(r"\s+", " ", title or "").strip()[:120] or brand
    h = int(hashlib.sha1(title.encode()).hexdigest(), 16)
    accent = ACCENTS[h % len(ACCENTS)]
    if style not in STYLES:
        style = STYLE_NAMES[h % len(STYLE_NAMES)]
    img, fg, line_c, brand_c, small_c = STYLES[style](accent)
    d = ImageDraw.Draw(img)

    # headline: biggest size that fits in at most 4 lines
    max_w, top, bottom = W - 2 * MARGIN - 20, 70, H - 170
    size = 84
    while True:
        font = _font(FONT_BOLD, size)
        lines = _wrap(title, font, max_w)
        line_h = int(size * 1.55)
        if (len(lines) <= 4 and len(lines) * line_h <= bottom - top) or size <= 40:
            break
        size -= 4
    y = top + max(0, (bottom - top - len(lines) * line_h) // 2)
    for line in lines:
        shaped = _shape(line)
        x = W - MARGIN - 20 - font.getlength(shaped)
        d.text((x, y), shaped, font=font, fill=fg)
        y += line_h

    # footer: thin line, brand on the right, latin name on the left (or the logo if provided)
    d.line((MARGIN, H - 120, W - MARGIN - 20, H - 120), fill=line_c, width=2)
    bf = _font(FONT_BOLD, 54)
    shaped = _shape(brand)
    d.text((W - MARGIN - 20 - bf.getlength(shaped), H - 108), shaped, font=bf, fill=brand_c)
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo.thumbnail((260, 80))
        img.alpha_composite(logo, (MARGIN, H - 104))
    else:
        sf = _font(FONT_MED, 40)
        d.text((MARGIN, H - 100), sub.upper(), font=sf, fill=small_c)

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()
