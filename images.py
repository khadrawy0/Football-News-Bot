"""Find a matching, legally reusable photo for a news story on Wikimedia Commons.

Flow for a story about a person (e.g. Salah, team=Liverpool):
  1. search Commons files; keep only reusable licenses (CC0 / public domain / CC BY / CC BY-SA)
  2. identity guard: the person's name must appear in the file's title/description/categories
  3. rank by team match, resolution, recency
  4. optionally ask Gemini (vision) whether the shirt matches the team's kit; it is NEVER asked
     to identify anyone from their face, only to look at clothing and photo quality
For stories without a person, a photo of the stadium is used when the story names one.
"""
import base64
import difflib
import html
import io
import os
import re
import time
import unicodedata

import requests

API = "https://commons.wikimedia.org/w/api.php"
META = "LicenseShortName|Artist|ImageDescription|DateTimeOriginal|Categories|ObjectName"
GENERIC_TEAM = {"fc", "cf", "afc", "club", "the", "national", "football", "team", "de", "sc", "ac", "cd", "ud", "sv", "fk"}
GENERIC_VENUE = {"stadium", "stadion", "estadio", "arena", "ground", "park", "stade"}
EXCLUDE = re.compile(
    r"\b(signature|autograph|statue|mural|graffiti|cartoon|caricature|drawing|painting|poster|stamp|"
    r"cover|screenshot|tweet|instagram|logo|banner|wax|figurine|lego|toy|cards?|panini|sticker|"
    r"crowd|fans|supporters|ultras|bus|train|plane|protest|portrait painting)\b",
    re.I,
)
GROUP = re.compile(r"\b(and|with|vs|versus|v)\b", re.I)


def _headers():
    repo = os.environ.get("GITHUB_REPOSITORY", "local")
    return {"User-Agent": f"ChexFootballBot/1.0 (https://github.com/{repo}; football news page bot)"}


def fold(t):
    t = unicodedata.normalize("NFKD", t or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def words(t):
    return set(re.findall(r"[a-z0-9]+", fold(t)))


def _plain(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()


def token_present(tok, blob_words):
    if tok in blob_words:
        return True
    if len(tok) < 5:
        return False
    return any(abs(len(w) - len(tok)) <= 2 and difflib.SequenceMatcher(None, tok, w).ratio() >= 0.86 for w in blob_words)


def license_kind(short, allow_attr):
    """'free' (no credit needed), 'attr' (credit line needed) or None (not usable)."""
    s = (short or "").lower().strip()
    if not s or re.search(r"\b(nc|nd)\b|gfdl|fair use|non-free|copyrighted", s):
        return None
    if s.startswith("cc0") or "public domain" in s or s.startswith("pd"):
        return "free"
    if s.startswith("cc by"):
        return "attr" if allow_attr else None
    return None


def year_of(c):
    m = re.search(r"\b(19[89]\d|20[0-3]\d)\b", c.get("date", "") + " " + c.get("title", ""))
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ commons
def search_files(query, limit=20, width=1280):
    params = {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": query,
        "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": width, "iiextmetadatafilter": META,
    }
    try:
        r = requests.get(API, params=params, headers=_headers(), timeout=25)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception as e:
        print(f"[images] commons search failed for {query!r}: {e}")
        return []
    pages = pages.values() if isinstance(pages, dict) else pages
    out = []
    for p in pages:
        ii = (p.get("imageinfo") or [None])[0]
        if not ii:
            continue
        md = ii.get("extmetadata") or {}
        g = lambda k: _plain((md.get(k) or {}).get("value", ""))
        out.append(
            {
                "title": p.get("title", ""), "index": p.get("index", 999),
                "url": ii.get("url"), "thumb": ii.get("thumburl") or ii.get("url"),
                "w": ii.get("width") or 0, "h": ii.get("height") or 0, "mime": ii.get("mime", ""),
                "license": g("LicenseShortName"), "artist": g("Artist"), "desc": g("ImageDescription"),
                "date": g("DateTimeOriginal"), "cats": g("Categories"), "objname": g("ObjectName"),
                "page": ii.get("descriptionurl", ""),
            }
        )
    return out


def _score(c, name_tokens, team_tokens, recent, icfg):
    """Return a score, or None when the file must not be used."""
    if c["mime"] not in ("image/jpeg", "image/png", "image/webp"):
        return None
    if c["w"] < icfg.get("min_width", 900) or c["h"] < 500:
        return None
    kind = license_kind(c["license"], icfg.get("allow_attribution_licenses", True))
    if not kind:
        return None
    text = " ".join([c["title"], c["desc"], c["cats"], c["objname"]])
    if EXCLUDE.search(c["title"]) or EXCLUDE.search(c["objname"]):
        return None
    bw = words(text)
    if not all(token_present(t, bw) for t in name_tokens):
        return None
    score = 0.0
    c["team_match"] = bool(team_tokens) and all(token_present(t, bw) for t in team_tokens)
    if c["team_match"]:
        score += 3
    score += min(c["w"], 2000) / 2000
    y = year_of(c)
    if y:
        score += max(0.0, min(1.5, (y - 2012) / 8))
    score += max(0, 20 - c["index"]) / 20
    if GROUP.search(re.sub(r"^file:|\.\w+$", "", c["title"], flags=re.I)):
        score -= 1.5
    if c["title"] in recent:
        score -= 5
    c["kind"] = kind
    return score


def _tokens(name, generic, min_len=2):
    return [t for t in words(name) if t not in generic and len(t) >= min_len]


def download(c):
    r = requests.get(c["thumb"], headers=_headers(), timeout=40)
    r.raise_for_status()
    mime = r.headers.get("content-type", "").split(";")[0]
    if not mime.startswith("image/") or len(r.content) > 8_000_000:
        raise ValueError(f"bad image response: {mime} {len(r.content)} bytes")
    return r.content, mime


def _small_jpeg(data):
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((640, 640))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def vision_check(cands, team, vision_fn):
    """Ask Gemini about clothing/quality only. Returns list of dicts aligned with cands, or None."""
    parts = [
        {
            "text": (
                "You are screening candidate photos for a football news post. The main person in the photo "
                f"should be wearing the kit of: {team}. Do NOT identify anyone from their face; only look at "
                "clothing and image quality.\n"
                'Return a JSON array like [{"i":0,"kit":"yes","usable":true}] with one object per photo.\n'
                'kit = "yes" only if the main person\'s shirt clearly matches that team\'s colours/crest; '
                '"no" if they clearly wear a different team\'s kit; "unclear" if no football kit is visible '
                "or it cannot be identified.\n"
                "usable = false if the photo is a crowd shot, cartoon/drawing, statue, screenshot, trading card, "
                "very blurry, or has several equally prominent people."
            )
        }
    ]
    try:
        for i, c in enumerate(cands):
            data, c["_mime"] = download(c)
            c["_bytes"] = data
            parts.append({"text": f"Photo {i}:"})
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(_small_jpeg(data)).decode()}})
        res = vision_fn(parts)
        by_i = {int(r.get("i")): r for r in res if isinstance(r, dict) and str(r.get("i", "")).isdigit()}
        return [by_i.get(i, {"kit": "unclear", "usable": True}) for i in range(len(cands))]
    except Exception as e:
        print(f"[images] vision check failed: {e}")
        return None


def find_image(spec, recent, icfg, vision_fn=None, verbose=False):
    """spec: {"person","team","venue"} -> dict(bytes, mime, title, page, license, credit) or None."""
    person = (spec.get("person") or "").strip()
    team = (spec.get("team") or "").strip()
    venue = (spec.get("venue") or "").strip()
    log = print if verbose else (lambda *a, **k: None)

    if person:
        name_tokens = _tokens(person, set())
        team_tokens = _tokens(team, GENERIC_TEAM, 3) if team else []
        queries = ([f'"{person}" {team} filetype:bitmap'] if team else []) + [
            f'"{person}" filetype:bitmap',
            f'incategory:"{person}" filetype:bitmap',
        ]
    elif venue:
        name_tokens = _tokens(venue, GENERIC_VENUE)
        team_tokens, team = [], ""
        queries = [f'"{venue}" filetype:bitmap']
    else:
        return None
    if not name_tokens:
        return None

    seen, cands = set(), []
    for q in queries:
        for c in search_files(q):
            if c["title"] in seen:
                continue
            seen.add(c["title"])
            s = _score(c, name_tokens, team_tokens, recent, icfg)
            if s is not None:
                c["score"] = s
                cands.append(c)
        time.sleep(0.3)
    cands.sort(key=lambda c: -c["score"])
    top = cands[: icfg.get("vision_candidates", 3)]
    log(f"  {len(seen)} files found, {len(cands)} passed license/identity filters")
    for c in top:
        log(f"  - {c['score']:.1f} {c['title']} | {c['license']} | team_match={c['team_match']} | {c['page']}")
    if not top:
        return None

    chosen = None
    if team_tokens and icfg.get("vision_check", True) and vision_fn:
        verdicts = vision_check(top, team, vision_fn)
        if verdicts is not None:
            for c, v in zip(top, verdicts):
                log(f"  vision: {c['title']} -> kit={v.get('kit')} usable={v.get('usable')}")
                if v.get("usable") is False or v.get("kit") == "no":
                    continue
                if v.get("kit") == "yes" or (v.get("kit") == "unclear" and c["team_match"]):
                    chosen = c
                    break
        else:  # vision unavailable: only trust metadata that names the team
            chosen = next((c for c in top if c["team_match"]), None)
    elif team_tokens:
        chosen = next((c for c in top if c["team_match"]), None)
    else:
        chosen = top[0]
    if not chosen:
        return None

    try:
        data = chosen.get("_bytes")
        mime = chosen.get("_mime", "image/jpeg")
        if data is None:
            data, mime = download(chosen)
    except Exception as e:
        print(f"[images] download failed: {e}")
        return None
    credit = ""
    if chosen["kind"] == "attr":
        artist = (chosen["artist"] or "Wikimedia Commons")[:70]
        credit = f"📷 {artist} / Wikimedia Commons / {chosen['license']}"
    return {"bytes": data, "mime": mime, "title": chosen["title"], "page": chosen["page"],
            "license": chosen["license"], "credit": credit}
