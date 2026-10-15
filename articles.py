"""Fetch the source article: its full text (so the post can cover the whole story) and the photo candidates it carries."""
import json
import re
import sys
from urllib.parse import urljoin

import requests

BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.8,ar;q=0.7",
}
# photo agencies that actively enforce their rights: a photo credited to one of them is flagged (see images.article_images.skip_agencies)
AGENCY_RE = re.compile(
    r"getty\s*images|gettyimages|\bgetty\b|reuters|associated press|\bap\s*photo\b|\bap\s*images?\b|\bafp\b|shutterstock|alamy|"
    r"imago|\bpa\s*(images?|media|wire)\b|action\s*images|\bepa\b|\bsipa\b|icon\s*sport|\bbpi\b|offside|"
    r"\bnurphoto\b|\bzuma\b|\bproshots\b|\bcolorsport\b|\bpixathlon\b|\bpresse\s*sports\b|\bdppi\b|\banadolu\b",
    re.I,
)


def decode_gnews(url):
    """Google News RSS links are redirects; resolve them to the publisher's real URL."""
    try:
        from googlenewsdecoder import gnewsdecoder

        r = gnewsdecoder(url, interval=1)
        if r.get("success", r.get("status")):
            return r.get("decoded_url")
        print(f"[articles] decode failed: {str(r.get('message'))[:120]}", file=sys.stderr)
    except Exception as e:
        print(f"[articles] decode error: {e}", file=sys.stderr)
    return None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _image_urls(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _image_urls(v)
    elif isinstance(value, dict):
        yield from _image_urls(value.get("url") or value.get("contentUrl"))


def page_images(doc, base):
    """Photo URLs of the article page, best first: Open Graph / Twitter card, then schema.org JSON-LD."""
    out = []

    def add(u):
        if isinstance(u, str) and u.strip():
            u = urljoin(base, u.strip())
            if u.startswith("http") and u not in out:
                out.append(u)

    for k in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
        for v in doc.xpath(f'//meta[@property="{k}" or @name="{k}"]/@content'):
            add(v)
    for s in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            data = json.loads(s)
        except Exception:
            continue
        for node in _walk(data):
            for key in ("image", "thumbnailUrl"):
                if key in node:
                    for u in _image_urls(node[key]):
                        add(u)
    return out


def credit_text(doc):
    """The photo credit / caption texts of the page (figcaption, credit blocks, image alt metas, JSON-LD credits)."""
    parts = []
    parts += [t.strip() for t in doc.xpath("//figcaption//text()")][:200]
    for el in doc.xpath(
        '//*[contains(@class,"credit") or contains(@class,"photographer") or contains(@class,"copyright") or contains(@class,"caption")]'
    )[:80]:
        parts.append(el.text_content()[:300])
    for k in ("og:image:alt", "twitter:image:alt"):
        parts += doc.xpath(f'//meta[@property="{k}" or @name="{k}"]/@content')
    for s in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            data = json.loads(s)
        except Exception:
            continue
        for node in _walk(data):
            for key in ("creditText", "copyrightHolder", "copyrightNotice", "credit", "author", "creator"):
                v = node.get(key)
                if isinstance(v, (str, dict, list)):
                    parts.append(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
    return " ".join(parts)[:30000]


def fetch_article(url, min_chars):
    """-> {"text", "url", "images": [urls], "agency": bool, "site": str} or None when the text can't be fetched / is too short."""
    if "news.google.com" in url:
        url = decode_gnews(url)
        if not url:
            return None
    try:
        r = requests.get(url, headers=BROWSER_UA, timeout=20)
        r.raise_for_status()
        if len(r.content) > 3_000_000:
            return None
        import trafilatura

        text = trafilatura.extract(r.content, url=url, include_comments=False, include_tables=False, favor_precision=True)
        images, agency, site = [], False, ""
        try:
            import lxml.html

            doc = lxml.html.fromstring(r.content)
            base = r.url or url
            images = page_images(doc, base)
            agency = bool(AGENCY_RE.search(credit_text(doc)))
            site = (doc.xpath('//meta[@property="og:site_name"]/@content') or [""])[0].strip()
        except Exception as e:
            print(f"[articles] image scan failed {url[:80]}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[articles] fetch failed {url[:80]}: {e}", file=sys.stderr)
        return None
    text = (text or "").strip()
    if len(text) < min_chars:
        return None
    return {"text": text, "url": r.url or url, "images": images, "agency": agency, "site": site}


def article_text(url, min_chars):
    """Text only (kept for older callers)."""
    page = fetch_article(url, min_chars)
    return page["text"] if page else None
