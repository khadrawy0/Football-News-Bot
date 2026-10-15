"""Cards used as the picture of every post.

* headline cards (news posts): two sizes picked at random so the page does not look repetitive
    - "portrait"  1080 x 1350  (4:5, the big card)
    - "landscape" 1200 x 630   (the classic card)
  each in 6 background designs, with large text
* fixtures cards ("أهم مباريات اليوم"): 1080 x 1350 (4:5)

Arabic needs shaping (letters joined) and bidi reordering before drawing; Pillow is forced to the basic
layout engine so it does not reorder the text a second time.
"""
import hashlib
import io
import math
import re
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT_BOLD = ROOT / "assets" / "fonts" / "Cairo-ExtraBold.ttf"
FONT_MED = ROOT / "assets" / "fonts" / "Cairo-Medium.ttf"
LOGO = ROOT / "assets" / "logo.png"  # optional: if present it is pasted on the cards
SIZES = {"landscape": (1200, 630), "portrait": (1080, 1350)}
SIZE_NAMES = list(SIZES)
ACCENTS = ["#2F80ED", "#27AE60", "#F2994A", "#EB5757", "#9B51E0"]
GOLD = (255, 224, 102)


def _font(path, size):
    return ImageFont.truetype(str(path), int(size), layout_engine=ImageFont.Layout.BASIC)


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


def _gradient(c1, c2, W, H, angle=45):
    """Smooth linear gradient c1 -> c2 along a direction (angle in degrees, 0 = left to right)."""
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


def _vgradient(w, h, c1, c2):
    small = Image.new("RGB", (1, 64))
    px = small.load()
    for y in range(64):
        t = y / 63
        px[0, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return small.resize((w, h), Image.BILINEAR)


def _overlay(W, H):
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def _text_box(font, shaped):
    l, t, r, b = font.getbbox(shaped)
    return l, t, r, b


def _draw_right(d, shaped, font, x_right, y_top, h, fill):
    """Draw text right-aligned at x_right, vertically centred in a band of height h starting at y_top."""
    l, t, r, b = _text_box(font, shaped)
    d.text((x_right - font.getlength(shaped), y_top + (h - (b - t)) / 2 - t), shaped, font=font, fill=fill)


def _draw_left(d, shaped, font, x_left, y_top, h, fill):
    l, t, r, b = _text_box(font, shaped)
    d.text((x_left, y_top + (h - (b - t)) / 2 - t), shaped, font=font, fill=fill)


# ---- backgrounds: each returns (RGBA image, text colour, footer line colour, brand colour, small text colour) ----
def _bg_navy(accent, W, H, fh):
    s = W / 1200
    img = _gradient((7, 18, 38), (16, 58, 128), W, H).convert("RGBA")
    o = _overlay(W, H)
    d = ImageDraw.Draw(o)
    d.ellipse((-0.217 * W, 0.444 * H, 0.35 * W, 1.52 * H), outline=(255, 255, 255, 38), width=int(36 * s))
    d.ellipse((-0.1 * W, 0.667 * H, 0.233 * W, 1.30 * H), outline=(255, 255, 255, 24), width=int(14 * s))
    d.rectangle((W - 22 * s, 0, W, H), fill=accent)
    return Image.alpha_composite(img, o), "white", (255, 255, 255, 70), accent, (255, 255, 255, 190)


def _bg_cream(accent, W, H, fh):
    s = W / 1200
    img = Image.new("RGBA", (W, H), (247, 242, 232, 255))
    o = _overlay(W, H)
    d = ImageDraw.Draw(o)
    d.polygon([(0, 0), (330 * s, 0), (0, 330 * s)], fill=accent)
    d.rectangle((W - 22 * s, 0, W, H), fill=accent)
    return Image.alpha_composite(img, o), (20, 24, 40), (20, 24, 40, 90), accent, (20, 24, 40, 170)


def _bg_pitch(accent, W, H, fh):
    s = W / 1200
    img = _gradient((10, 80, 40), (20, 120, 60), W, H, 70).convert("RGBA")
    o = _overlay(W, H)
    d = ImageDraw.Draw(o)
    line = (255, 255, 255, 40)
    R = 230 * s
    d.ellipse((W / 2 - R, H / 2 - R, W / 2 + R, H / 2 + R), outline=line, width=int(8 * s))
    d.line((W / 2, 0, W / 2, H), fill=line, width=int(8 * s))
    d.rectangle((-8, H / 2 - 180 * s, 150 * s, H / 2 + 180 * s), outline=line, width=int(8 * s))
    d.rectangle((W - 150 * s, H / 2 - 180 * s, W + 8, H / 2 + 180 * s), outline=line, width=int(8 * s))
    step = int(150 * s)
    for i in range(0, W, step):  # mowing stripes
        if (i // step) % 2 == 0:
            d.rectangle((i, 0, i + step, H), fill=(255, 255, 255, 10))
    return Image.alpha_composite(img, o), "white", (255, 255, 255, 90), (255, 224, 102), (255, 255, 255, 200)


def _bg_neon(accent, W, H, fh):
    s = W / 1200
    img = Image.new("RGBA", (W, H), (10, 10, 14, 255))
    o = _overlay(W, H)
    d = ImageDraw.Draw(o)
    g = int(60 * s)
    for x in range(0, W, g):
        d.line((x, 0, x, H), fill=(255, 255, 255, 12), width=1)
    for y in range(0, H, g):
        d.line((0, y, W, y), fill=(255, 255, 255, 12), width=1)
    d.rectangle((80 * s, 40 * s, W - 100 * s, 48 * s), fill=accent)
    d.rectangle((0, 0, 14 * s, H), fill=accent)
    return Image.alpha_composite(img, o), "white", accent, accent, (255, 255, 255, 170)


def _bg_bold(accent, W, H, fh):
    img = Image.new("RGBA", (W, H), accent)
    o = _overlay(W, H)
    d = ImageDraw.Draw(o)
    for i in range(-H, W, 90):
        d.polygon([(i, H), (i + 45, H), (i + 45 + H, 0), (i + H, 0)], fill=(0, 0, 0, 22))
    d.rectangle((0, H - fh - 6, W, H), fill=(15, 15, 20, 235))
    return Image.alpha_composite(img, o), (255, 255, 255), (255, 255, 255, 0), (255, 255, 255), (255, 255, 255, 200)


def _bg_sunset(accent, W, H, fh):
    s = W / 1200
    img = _gradient((60, 14, 84), (232, 84, 60), W, H, 60).convert("RGBA")
    o = _overlay(W, H)
    d = ImageDraw.Draw(o)
    d.ellipse((W - 420 * s, -220 * s, W + 160 * s, 360 * s), fill=(255, 214, 120, 60))
    d.ellipse((W - 300 * s, -140 * s, W + 60 * s, 220 * s), fill=(255, 230, 160, 70))
    d.rectangle((80 * s, H - fh - 15, 80 * s + 160 * s, H - fh - 9), fill=(255, 214, 120, 255))
    return Image.alpha_composite(img, o), "white", (255, 255, 255, 60), (255, 214, 120), (255, 255, 255, 200)


STYLES = {"navy": _bg_navy, "cream": _bg_cream, "pitch": _bg_pitch, "neon": _bg_neon, "bold": _bg_bold, "sunset": _bg_sunset}
STYLE_NAMES = list(STYLES)


def make_card(title, brand="شيكس", sub="chex", style=None, size="landscape"):
    """JPEG bytes of a headline card. size: 'portrait' (1080x1350, 4:5) or 'landscape' (1200x630); style: one of STYLE_NAMES."""
    if size not in SIZES:
        size = "landscape"
    W, H = SIZES[size]
    portrait = size == "portrait"
    s = W / 1200
    title = re.sub(r"\s+", " ", title or "").strip()[:120] or brand
    h = int(hashlib.sha1(title.encode()).hexdigest(), 16)
    accent = ACCENTS[h % len(ACCENTS)]
    if style not in STYLES:
        style = STYLE_NAMES[h % len(STYLE_NAMES)]
    fh = 170 if portrait else 120  # footer height
    img, fg, line_c, brand_c, small_c = STYLES[style](accent, W, H, fh)
    d = ImageDraw.Draw(img)

    margin = 90 if portrait else 80
    right = W - margin - 20
    max_w = W - 2 * margin - 20
    top, bottom = (230, H - fh - 70) if portrait else (60, H - fh - 40)
    max_lines, size_px, min_px = (8, 150, 56) if portrait else (4, 104, 44)
    lh_mul = 1.42 if portrait else 1.5
    while True:
        font = _font(FONT_BOLD, size_px)
        lines = _wrap(title, font, max_w)
        line_h = int(size_px * lh_mul)
        if (len(lines) <= max_lines and len(lines) * line_h <= bottom - top) or size_px <= min_px:
            break
        size_px -= 4
    y = top + max(0, (bottom - top - len(lines) * line_h) // 2)
    if "«" in title or "»" in title:  # a statement: big quote mark behind the headline
        qf = _font(FONT_BOLD, 520 if portrait else 380)
        q = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        qc = brand_c if isinstance(brand_c, tuple) else tuple(int(brand_c[i:i + 2], 16) for i in (1, 3, 5))
        ImageDraw.Draw(q).text((right - qf.getlength("”") - 10, (-140 if portrait else -205)), "”", font=qf, fill=qc[:3] + (80,))
        img.alpha_composite(q)
        d = ImageDraw.Draw(img)
    if portrait:  # filled label pill on top
        lf = _font(FONT_BOLD, 38)
        lab = _shape("أخبار كرة القدم")
        lw = lf.getlength(lab)
        d.rounded_rectangle((right - lw - 64, 84, right, 154), radius=35, fill=brand_c)
        _draw_right(d, lab, lf, right - 32, 84, 70, (14, 20, 30))
    for i, line in enumerate(lines):
        shaped = _shape(line)
        # first line in the accent colour makes the headline pop (only when there are 3+ lines)
        d.text((right - font.getlength(shaped), y), shaped, font=font, fill=brand_c if (len(lines) >= 3 and i == 0) else fg)
        y += line_h

    # footer: thin line, brand on the right, latin name on the left (or the logo if provided)
    d.line((margin, H - fh, W - margin - 20, H - fh), fill=line_c, width=2)
    bf = _font(FONT_BOLD, 70 if portrait else 58)
    shaped = _shape(brand)
    _draw_right(d, shaped, bf, W - margin - 20, H - fh, fh, brand_c)
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo.thumbnail((300, 96))
        img.alpha_composite(logo, (margin, H - fh + (fh - logo.height) // 2))
    else:
        sf = _font(FONT_MED, 46 if portrait else 40)
        _draw_left(d, sub.upper(), sf, margin, H - fh, fh, small_c)

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


# ----------------------------------------------------------------------------------------- fixtures card
FW, FH = 1080, 1350  # 4:5


def make_fixtures_card(date_ar, groups, brand="شيكس", sub="chex", part=None, title="أهم مباريات اليوم"):
    """4:5 card listing the important matches of the day.
    groups = [(competition_ar, [(home_ar, away_ar, "9:00 م"), ...]), ...]   -> JPEG bytes (1080 x 1350)."""
    W, H = FW, FH
    PAD = 64
    right = W - PAD
    head_h, foot_h = 330, 135
    body_h = H - head_h - foot_h
    comp_h0, row_h0 = 92, 80
    groups = [(comp, list(ms)) for comp, ms in groups]
    while groups and sum(comp_h0 + len(ms) * row_h0 for _, ms in groups) * 0.7 > body_h:  # safety: never overflow the card
        groups[-1][1].pop()
        if not groups[-1][1]:
            groups.pop()
        print("[card] too many rows for one card: dropped the last match")
    n_rows = sum(len(ms) for _, ms in groups)
    need = len(groups) * comp_h0 + n_rows * row_h0
    k = max(0.7, min(1.3, body_h / need)) if need else 1.0  # fewer rows -> bigger, many rows -> a bit smaller
    comp_h, row_h = int(comp_h0 * k), int(row_h0 * k)
    used = len(groups) * comp_h + n_rows * row_h
    y0 = head_h + max(0, min(36, (body_h - used) // 2))

    img = _vgradient(W, H, (8, 70, 38), (11, 36, 28)).convert("RGBA")
    o = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(o)
    for i in range(0, W, 135):  # mowing stripes
        if (i // 135) % 2 == 0:
            od.rectangle((i, 0, i + 135, H), fill=(255, 255, 255, 9))
    od.ellipse((W // 2 - 330, H // 2 - 330, W // 2 + 330, H // 2 + 330), outline=(255, 255, 255, 20), width=8)
    od.line((0, H // 2, W, H // 2), fill=(255, 255, 255, 14), width=6)
    od.rectangle((W - 22, 0, W, H), fill=GOLD + (255,))
    img = Image.alpha_composite(img, o)
    white = (255, 255, 255)
    dark = (12, 38, 28)

    # translucent bars / zebra rows live on their own layer (ImageDraw would overwrite, not blend)
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    yy = y0
    for comp, matches in groups:
        ld.rounded_rectangle((PAD, yy + 8, right, yy + comp_h - 8), radius=16, fill=(0, 0, 0, 110))
        ld.rounded_rectangle((right - 12, yy + 22, right - 4, yy + comp_h - 22), radius=4, fill=GOLD + (255,))
        yy += comp_h
        for i, mt in enumerate(matches):
            hot = len(mt) > 3 and mt[3]
            if hot:  # a match of a big-crowd club: gold-tinted row with a gold edge
                ld.rounded_rectangle((PAD, yy + 3, right, yy + row_h - 3), radius=14, fill=GOLD + (40,))
                ld.rounded_rectangle((PAD, yy + 10, PAD + 8, yy + row_h - 10), radius=4, fill=GOLD + (255,))
            elif i % 2 == 0:
                ld.rounded_rectangle((PAD, yy + 3, right, yy + row_h - 3), radius=14, fill=(255, 255, 255, 14))
            yy += row_h
    ld.line((PAD, H - foot_h, right, H - foot_h), fill=(255, 255, 255, 80), width=2)
    img = Image.alpha_composite(img, lay)
    d = ImageDraw.Draw(img)

    # header: title (auto-fit) + date on its own gold pill underneath (clear gap, never touching the title)
    badge_w = 0
    if part:
        pf = _font(FONT_BOLD, 54)
        badge = f"{part[0]}/{part[1]}"
        bw = pf.getlength(badge)
        badge_w = bw + 50 + 30
        d.rounded_rectangle((PAD, 64, PAD + bw + 50, 144), radius=18, fill=GOLD)
        d.text((PAD + 25, 64 + (80 - (pf.getbbox(badge)[3] - pf.getbbox(badge)[1])) / 2 - pf.getbbox(badge)[1]), badge, font=pf, fill=dark)
    tsize = 118
    while tsize > 60:
        tf = _font(FONT_BOLD, tsize)
        if tf.getlength(_shape(title)) <= W - 2 * PAD - 20 - badge_w:
            break
        tsize -= 4
    d.text((right - 10 - tf.getlength(_shape(title)), 40), _shape(title), font=tf, fill=white)
    sf = _font(FONT_BOLD, 44)
    dt_sh = _shape(date_ar)
    pw = sf.getlength(dt_sh) + 64
    pill_top = 40 + int(tsize * 1.5) + 8
    d.rounded_rectangle((right - 10 - pw, pill_top, right - 10, pill_top + 74), radius=37, fill=GOLD)
    _draw_right(d, dt_sh, sf, right - 10 - 32, pill_top, 74, dark)

    y = y0
    for comp, matches in groups:
        cf = _font(FONT_BOLD, 44 * k)
        _draw_right(d, _shape(comp), cf, right - 34, y, comp_h, GOLD)
        y += comp_h
        for home, away, tm, *_hot in matches:
            tfont = _font(FONT_BOLD, 38 * k)
            tt = _shape(tm)
            tw = tfont.getlength(tt)
            _draw_left(d, tt, tfont, PAD + 26, y, row_h, GOLD)
            avail = right - 34 - (PAD + 26 + tw + 48)
            size = 48 * k
            line = f"{home} × {away}"
            while size > 24:
                mf = _font(FONT_BOLD, size)
                if mf.getlength(_shape(line)) <= avail:
                    break
                size -= 2
            mf = _font(FONT_BOLD, size)
            _draw_right(d, _shape(line), mf, right - 34, y, row_h, white)
            y += row_h

    # footer
    bf = _font(FONT_BOLD, 62)
    _draw_right(d, _shape(brand), bf, right, H - foot_h, foot_h, GOLD)
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA")
        logo.thumbnail((300, 90))
        img.alpha_composite(logo, (PAD, H - foot_h + (foot_h - logo.height) // 2))
    else:
        _draw_left(d, sub.upper(), _font(FONT_MED, 42), PAD, H - foot_h, foot_h, (255, 255, 255, 190))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()
