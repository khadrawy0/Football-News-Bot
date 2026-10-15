"""The photo of a post = the lead photo of the source article (instead of a headline card).

Steps: take the article's photo candidates (og:image, twitter:image, JSON-LD, then the feed's own thumbnail), drop logos /
placeholders / tiny images / photos credited to the big photo agencies / photos used recently, then crop the best one into
the range Facebook shows without cutting it (4:5 .. 1.91:1), shrink it, and put a small brand badge in the corner.
Returns None when nothing usable is found, and the caller falls back (headline card by default).
"""
import base64
import datetime as dt
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
URL_DATE = re.compile(r"/(20\d{2})[/_-](0?[1-9]|1[0-2])(?:[/_-](0?[1-9]|[12]\d|3[01]))?/")


def url_age_days(url):
    """Age in days when the image URL carries a /YYYY/MM/ (/DD/) date (most CMSs do), else None."""
    m = URL_DATE.search(url)
    if not m:
        return None
    try:
        d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 15))
    except ValueError:
        return None
    return (dt.date.today() - d).days


def verify(jpg, headline, summary, vision_fn):
    """Does this photo fit the story? -> (ok, reason). Gemini looks at kit colours / setting / text only, never at faces.
    A failing vision call (quota) returns (True, 'unchecked'): the photo is the article's own lead photo, so it is a good fit anyway."""
    small = Image.open(io.BytesIO(jpg)).convert("RGB")
    small.thumbnail((768, 768))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=80)
    parts = [
        {"text": (
            "You screen a photo for a football news post. Do NOT identify anyone from their face; judge only clothing/kit colours, "
            "setting, visible text and image quality.\n"
            f"Story headline: {headline}\nStory summary: {summary[:400]}\n"
            'Return JSON: {"football": bool, "fits": bool, "clean": bool, "reason": "short"}.\n'
            "football = the photo shows football (players, match, stadium, training, coach, fans, trophy) and not another sport, a studio "
            "graphic, a logo or an unrelated scene.\n"
            "fits = nothing in the photo contradicts the story (e.g. kits/crests of a clearly different club than the one the story is about, "
            "or a different sport).\n"
            "clean = no ad, betting banner, watermark of another site, collage, screenshot or heavy text overlay.\n"
            "good = a photo a big football page would be proud to post: sharp (not blurry or pixelated), well framed with the main subject "
            "clearly visible (not cut at an odd place, not a tiny figure far away), decent lighting, not a stock-looking graphic or a screenshot "
            "of a video/TV broadcast."
        )},
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}},
    ]
    try:
        res = vision_fn(parts)
        if isinstance(res, list):
            res = res[0] if res else {}
        ok = bool(res.get("football", True)) and bool(res.get("fits", True)) and bool(res.get("clean", True)) and bool(res.get("good", True))
        return ok, str(res.get("reason", ""))[:120]
    except Exception as e:
        return True, f"unchecked ({type(e).__name__})"


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


def process(raw, cfg, brand, mark=True):
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
    if mark and cfg.get("brand_mark", True):
        try:
            im = _mark(im, brand)
        except Exception as e:  # a failing badge must never cost the post its photo
            print(f"[article-image] brand badge skipped ({e})", file=sys.stderr)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def pick(item, recent, cfg, brand="شيكس", verbose=True, vision_fn=None, use_alts=False):
    """Best usable photo for this story -> image dict (kind 'article'), or None.
    use_alts=False: the lead article's photos. use_alts=True: photos of the same story at other outlets (item["alt_pages"]).
    All downloadable candidates are compared and the highest-resolution one is tried first (then the next...) until the vision check passes."""

    def say(msg):
        if verbose:
            print(f"[article-image] {msg}")

    if use_alts:
        sources = [(p.get("source", ""), p.get("agency"), list(p.get("img_cands") or []) + list(p.get("feed_imgs") or []))
                   for p in item.get("alt_pages") or []]
    else:
        lead = list(item.get("img_cands") or [])
        sources = [(item.get("source", ""), item.get("img_agency"), lead + [u for u in (item.get("feed_imgs") or []) if u not in lead])]
    found = []  # (pixels, credit, url, jpg_plain, jpg_final)
    tried = set()
    for credit, agency, cands in sources:
        if cfg.get("skip_agencies", True) and agency:
            say(f"skipped (photo credited to a photo agency) [{credit}]: {item['title'][:50]}")
            continue
        for url in cands[: cfg.get("max_candidates", 6)]:
            key = hashlib.sha1(url.encode()).hexdigest()[:16]
            if url in tried:
                continue
            tried.add(url)
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
            age = url_age_days(url)
            if age is not None and age > cfg.get("max_age_days", 21):
                say(f"skip (old photo, ~{age} days by its URL date): {url[:90]}")
                continue
            try:
                raw = _download(url)
                plain = process(raw, cfg, brand, mark=False)
                final = process(raw, cfg, brand) if plain else None
            except Exception as e:
                say(f"skip ({e}): {url[:90]}")
                continue
            if final is None:
                say(f"skip (too small / odd shape): {url[:90]}")
                continue
            px = Image.open(io.BytesIO(plain)).size
            found.append((px[0] * px[1], credit, url, plain, final, key))
    found.sort(key=lambda x: -x[0])
    for _px, credit, url, plain, final, key in found:
        if vision_fn and cfg.get("verify", True):
            ok, why = verify(plain, item["title"], item.get("summary", ""), vision_fn)
            say(f"vision check: {'FITS' if ok else 'REJECTED'} - {why} | {url[:70]}")
            if not ok:
                continue
        say(f"OK ({_px // 1000}k px) {url[:100]}")
        return {"bytes": final, "mime": "image/jpeg", "title": key, "page": url, "license": "article photo",
                "credit_core": credit, "kind": "article"}
    return None
