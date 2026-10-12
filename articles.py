"""Fetch the full text of a news article so the post can cover the whole story."""
import sys

import requests

BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.8,ar;q=0.7",
}


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


def article_text(url, min_chars):
    """Return the main article text, or None when it can't be fetched / is too short (paywall, block)."""
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

        text = trafilatura.extract(
            r.content, url=url, include_comments=False, include_tables=False, favor_precision=True
        )
    except Exception as e:
        print(f"[articles] fetch failed {url[:80]}: {e}", file=sys.stderr)
        return None
    text = (text or "").strip()
    return text if len(text) >= min_chars else None
