"""The photo of a post = the lead photo of the source article (instead of a headline card).

Steps: take the article's photo candidates (og:image, twitter:image, JSON-LD, then the feed's own thumbnail), drop logos /
placeholders / tiny images / photos credited to the big photo agencies / photos used recently, then crop the best one into
the range Facebook shows without cutting it (4:5 .. 1.91:1), shrink it, and put a small brand badge in the corner.
Returns None when nothing usable is found, and the caller falls back (headline card by default).
"""
import hashlib
import io
import re
import sys

from pathlib import Path
from urllib.parse import urlparse

import arabic_reshaper
import requests
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT_BOLD = Path(__file__).resolve().parent / "assets" / "fonts" / "Cairo-ExtraBold.ttf"
GOLD = (255, 224, 102)  # self-contained on purpose: this module does not depend on card.py

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
BAD_URL = re.compile(
    r"(logo|placeholder|avatar|sprite|favicon|blank\.|pixel|[/_-]icon[/_.-]|share[-_]default|default[-_]og|default[-_]image|"
    r"/ads?/|\.svg(\?|$)|\.gif(\?|$))",
    re.I,
)
MAX_BYTES = 12_000_000


def _download(url):
    r = requests.get(url, headers=UA, timeout=20, stream=True)
    r.raise_for_status()
    if not (r.headers.get("content-type", "").lower().startswith("image/")):
        raise ValueError(f"not an image ({r.headers.get('content-type')})")
    buf = io.BytesIO()
    for chunk in r.iter_content(65536):
        buf.write(chunk)
        if buf.tell() > MAX_BYTES:
            raise ValueError("image too large")
    return buf.getvalue()


def _fit(im, lo=0.8, hi=1.91, top_bias=0.3):
    """Crop only when the photo is outside 4:5 .. 1.91:1 (what the Facebook feed shows whole); heads stay: crop mostly from the bottom."""
    w, h = im.size
    r = w / h
    if r > hi:
        nw = int(h * hi)
        x = (w - nw) // 2
        return im.crop((x, 0, x + nw, h))
    if r < lo:
        nh = int(w / lo)
        y = int((h - nh) * top_bias)
        return im.crop((0, y, w, y + nh))
    return im


def _mark(im, brand):
    """Small brand badge, bottom-right, on a soft dark pill."""
    w, h = im.size
    f = ImageFont.truetype(str(FONT_BOLD), max(26, int(w * 0.034)), layout_engine=ImageFont.Layout.BASIC)
    txt = get_display(arabic_reshaper.reshape(brand))
    tw = f.getlength(txt)
    l, t, r, b = f.getbbox(txt)
    padx, pady = int(w * 0.018), int(w * 0.010)
    bw, bh = int(tw + 2 * padx), int((b - t) + 2 * pady)
    m = int(w * 0.03)
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x1, y1 = w - m, h - m
    d.rounded_rectangle((x1 - bw, y1 - bh, x1, y1), radius=bh // 2, fill=(0, 0, 0, 150))
    d.text((x1 - bw + padx, y1 - bh + pady - t), txt, font=f, fill=GOLD)
    return Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")


def process(raw, cfg, brand):
    """raw bytes -> JPEG bytes ready to post, or None when the photo is unusable."""
    im = Image.open(io.BytesIO(raw))
    im.load()
    im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    if w < cfg.get("min_width", 800) or h < cfg.get("min_height", 450):
        return None
    if not (0.5 <= w / h <= 3.0):  # banners / skyscrapers are ads or graphics, not lead photos
        return None
    im = _fit(im)
    longest = cfg.get("max_side", 1600)
    if max(im.size) > longest:
        k = longest / max(im.size)
        im = im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS)
    if cfg.get("brand_mark", True):
        try:
            im = _mark(im, brand)
        except Exception as e:  # a failing badge must never cost the post its photo
            print(f"[article-image] brand badge skipped ({e})", file=sys.stderr)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def pick(item, recent, cfg, brand="شيكس", verbose=True):
    """Best usable photo for this story -> image dict (kind 'article'), or None."""

    def say(msg):
        if verbose:
            print(f"[article-image] {msg}")

    if cfg.get("skip_agencies", True) and item.get("img_agency"):
        say(f"skipped (photo credited to a photo agency): {item['title'][:60]}")
        return None
    cands = list(item.get("img_cands") or []) + [u for u in (item.get("feed_imgs") or []) if u not in (item.get("img_cands") or [])]
    for url in cands[: cfg.get("max_candidates", 5)]:
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        if key in recent:
            say(f"skip (used recently): {url[:90]}")
            continue
        host = urlparse(url).netloc.lower()
        if any(h.lower() in host for h in cfg.get("skip_hosts") or []):
            say(f"skip (host in skip_hosts): {url[:90]}")
            continue
        if BAD_URL.search(url):
            say(f"skip (logo/placeholder-like url): {url[:90]}")
            continue
        try:
            jpg = process(_download(url), cfg, brand)
        except Exception as e:
            say(f"skip ({e}): {url[:90]}")
            continue
        if jpg is None:
            say(f"skip (too small / odd shape): {url[:90]}")
            continue
        say(f"OK {url[:100]}")
        return {"bytes": jpg, "mime": "image/jpeg", "title": key, "page": url, "license": "article photo",
                "credit_core": item.get("source", ""), "kind": "article"}
    return None
