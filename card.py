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


def _gradient(c1, c2):
    base = Image.new("RGB", (W, H), c1)
    top = Image.new("RGB", (W, H), c2)
    mask = Image.linear_gradient("L").rotate(45, expand=True).resize((W, H))
    return Image.composite(top, base, mask)


def make_card(title, brand="شيكس", sub="chex"):
    """Return JPEG bytes of a 1200x630 headline card."""
    title = re.sub(r"\s+", " ", title or "").strip()[:120] or brand
    accent = ACCENTS[int(hashlib.sha1(title.encode()).hexdigest(), 16) % len(ACCENTS)]
    img = _gradient((7, 18, 38), (16, 58, 128)).convert("RGBA")

    # decoration: a big soft ring and a thick accent bar on the reading side (right)
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(deco)
    d.ellipse((-260, 280, 420, 960), outline=(255, 255, 255, 38), width=36)
    d.ellipse((-120, 420, 280, 820), outline=(255, 255, 255, 24), width=14)
    img = Image.alpha_composite(img, deco)
    d = ImageDraw.Draw(img)
    d.rectangle((W - 22, 0, W, H), fill=accent)

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
        d.text((x, y), shaped, font=font, fill="white")
        y += line_h

    # footer: thin line, brand on the right, latin name on the left (or the logo if provided)
    d.line((MARGIN, H - 120, W - MARGIN - 20, H - 120), fill=(255, 255, 255, 70), width=2)
    bf = _font(FONT_BOLD, 54)
    shaped = _shape(brand)
    d.text((W - MARGIN - 20 - bf.getlength(shaped), H - 108), shaped, font=bf, fill=accent)
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo.thumbnail((260, 80))
        img.alpha_composite(logo, (MARGIN, H - 104))
    else:
        sf = _font(FONT_MED, 40)
        d.text((MARGIN, H - 100), sub.upper(), font=sf, fill=(255, 255, 255, 190))

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()
