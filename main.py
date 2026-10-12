#!/usr/bin/env python3
"""Football news -> Gemini (free tier) -> Facebook Page. Runs from GitHub Actions.

Env vars:
  GEMINI_API_KEY, FB_PAGE_ID, FB_PAGE_TOKEN   (required unless --check / DRY_RUN)
  DRY_RUN=1   print posts, don't publish, don't touch state
  FORCE=1     ignore quiet hours and daily cap (for testing)
Flags:
  --check     test every feed and print how many fresh items each returns
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

import articles
import card
import images

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "state.json"
CAIRO = ZoneInfo("Africa/Cairo")
UA = {"User-Agent": "Mozilla/5.0 (compatible; FootballNewsBot/1.0)"}
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
S = CFG["settings"]
DROPPED = Counter()
SKIP_RE = [re.compile(p) for p in S.get("skip_title_patterns", [])]
IMG = CFG.get("images", {})


# ----------------------------------------------------------------- helpers
def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def norm_ar(t):
    t = re.sub(r"[ً-ْـ]", "", t)
    return re.sub("[أإآ]", "ا", t).replace("ى", "ي").replace("ة", "ه")


STOP = set(
    "the and for with after before from into over will vs about this that "
    "has have its are was were not but they their his her who how what "
    "في من على الى عن مع بعد قبل هذا هذه ذلك التي الذي ان كان بين حتى".split()
)


def tokens(t):
    t = re.sub(r"[^\w\s]", " ", norm_ar(t.lower()))
    return {w for w in t.split() if len(w) > 2 and w not in STOP}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


TRUSTED = [norm_ar(p.lower()) for p in S.get("trusted_publishers", [])]


def is_trusted(label):
    """Google News items only count if the publisher is on the allowlist."""
    n = norm_ar((label or "").lower()).strip()
    return any(n == p or n.startswith(p + " ") or n.startswith(p + ".") for p in TRUSTED)


# ------------------------------------------------------------------- state
def load_state():
    try:
        st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st.setdefault("seen", [])
    st.setdefault("recent_titles", [])
    st.setdefault("recent_headlines", [])
    st.setdefault("recent_images", [])
    st.setdefault("posts", {})
    return st


def save_state(st):
    st["seen"] = st["seen"][-5000:]
    st["recent_titles"] = st["recent_titles"][-300:]
    st["recent_headlines"] = st["recent_headlines"][-30:]
    st["recent_images"] = st["recent_images"][-40:]
    st["posts"] = {k: st["posts"][k] for k in sorted(st["posts"])[-7:]}
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------- fetch
def fetch_source(src):
    try:
        r = requests.get(src["url"], headers=UA, timeout=20)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:  # one dead feed must never kill the run
        print(f"[feed-fail] {src['name']}: {e}", file=sys.stderr)
        return src, []
    items = []
    for e in feed.entries[:40]:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        if not ts:
            continue
        published = dt.datetime(*ts[:6], tzinfo=dt.timezone.utc)
        title = clean(e.get("title", ""))
        source_label = src["name"]
        if src.get("gnews"):
            pub = (e.get("source") or {}).get("title")
            title = re.sub(r"\s+[-–—|]\s+[^-–—|]+$", "", title)
            source_label = pub or src["name"].replace("GNews - ", "")
            if TRUSTED and not is_trusted(source_label):
                DROPPED[source_label] += 1
                continue
            summary = ""
        else:
            summary = clean(e.get("summary", ""))[:400]
        link = e.get("link", "")
        if len(title) < 15 or not link:  # empty/garbled headlines waste a slot
            continue
        if any(p.search(title) for p in SKIP_RE):  # live blogs, streaming/TV listings
            continue
        items.append(
            {
                "id": hashlib.sha1((link or title).encode()).hexdigest()[:16],
                "title": title,
                "summary": summary,
                "url": link,
                "source": source_label,
                "weight": src["weight"],
                "region": src["region"],
                "published": published,
                "tok": tokens(title),
            }
        )
    return src, items


def fetch_all():
    cutoff = utcnow() - dt.timedelta(hours=S["max_age_hours"])
    out = []
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for _src, items in ex.map(fetch_source, CFG["sources"]):
            out.extend(i for i in items if i["published"] >= cutoff)
    return out


def check_feeds():
    for src in CFG["sources"]:
        _s, items = fetch_source(src)
        newest = max((i["published"] for i in items), default=None)
        age = f"{(utcnow() - newest).total_seconds() / 3600:.1f}h" if newest else "-"
        print(f"{'OK ' if items else 'BAD'} {src['name']:<34} items={len(items):<3} newest_age={age}")


# -------------------------------------------------------------------- rank
def rank(items, st):
    seen = set(st["seen"])
    recent = [set(t) for t in st["recent_titles"]]
    fresh = [
        i for i in items
        if i["id"] not in seen and not any(jaccard(i["tok"], r) >= 0.6 for r in recent)
    ]
    clusters = []
    for it in sorted(fresh, key=lambda x: -x["weight"]):
        for c in clusters:
            if jaccard(it["tok"], c["tok"]) >= 0.45:
                c["members"].append(it)
                break
        else:
            clusters.append({"tok": it["tok"], "members": [it]})
    ranked = []
    for c in clusters:
        m = c["members"]
        lead = max(m, key=lambda x: (x["weight"], x["published"]))
        age_h = (utcnow() - lead["published"]).total_seconds() / 3600
        n_src = len({x["source"] for x in m})
        lead["score"] = lead["weight"] + 1.5 * (n_src - 1) + max(0, 3 - age_h / 4) + random.random() * 0.8
        lead["all_ids"] = [x["id"] for x in m]
        ranked.append(lead)
    return sorted(ranked, key=lambda x: -x["score"])


def select(ranked, n):
    """Top n, but make sure at least one Egyptian story is included when available."""
    picked = ranked[:n]
    if n >= 2 and picked and not any(p["region"] == "eg" for p in picked):
        eg = next((r for r in ranked[n:] if r["region"] == "eg"), None)
        if eg:
            picked[-1] = eg
    return picked


# ------------------------------------------------------------------ gemini
def pick_styles(n):
    """n styles, all different within one run when possible (weighted by config)."""
    pool, out = list(CFG["styles"]), []
    for _ in range(n):
        if not pool:
            pool = list(CFG["styles"])
        s = random.choices(pool, weights=[x["w"] for x in pool])[0]
        out.append(s)
        pool.remove(s)
    return out


def build_prompt(items, recent=()):
    recent_block = ""
    if recent:
        recent_block = (
            "\n\nمنشورات نُشرت مؤخرًا على الصفحة (ضع skip=true لأي خبر يتناول نفس الحدث):\n"
            + "\n".join(f"- {h}" for h in recent)
        )
    blocks = []
    for it in items:
        st = it["style"]
        blocks.append(
            f'id: {it["id"]}\nheadline: {it["title"]}\n'
            f'style: {st["name"]} — {st["desc"]}\n'
            f'article:\n{(it.get("article") or "")[:5000]}'
        )
    return (
        "أنت محرر محتوى محترف لصفحة فيسبوك كبيرة عن كرة القدم، جمهورها مصري وعربي.\n"
        "اكتب منشورًا واحدًا لكل خبر من الأخبار التالية، بالأسلوب المحدد له. تحت كل خبر نص المقال المصدر.\n\n"
        "قواعد المحتوى:\n"
        "- المنشور هو الخبر كاملًا: غطِّ كل المعلومات الأساسية (من؟ ماذا؟ متى؟ أين؟ الأرقام والنتائج والتصريحات المهمة "
        "والخلفية الضرورية) بحيث لا يحتاج القارئ إلى فتح المصدر. الطول من 4 إلى 9 جمل، موزعة على فقرتين أو ثلاث "
        "بأسطر فارغة بينها (\\n\\n). لا تحذف معلومة جوهرية.\n"
        "- استخدم فقط المعلومات الموجودة في نص المقال. لا تخترع أرقامًا أو تصريحات أو نتائج أو خلفيات من عندك.\n"
        "- اكتب بكلماتك أنت، ولا تنسخ جملًا من المقال. يجوز اقتباس عبارة قصيرة جدًا بين علامتي تنصيص إن كانت جوهرية.\n"
        "- المصدر قد يكون بالإنجليزية، والمنشور دائمًا بالعربية الفصحى المبسطة الرسمية. ممنوع العامية المصرية أو أي لهجة أخرى.\n"
        "- ممنوع الهاشتاجات والروابط. ولا تذكر اسم الموقع أو الصحيفة أو الوكالة التي نشرت الخبر ولا تكتب «بحسب موقع كذا». "
        "أما لو نسب المقال كلامًا أو خبرًا إلى لاعب أو مدرب أو نادٍ أو جهة رسمية فاذكر ذلك لأنه جزء من الخبر.\n"
        "- ممنوع إضافة أي حكم أو وصف غير مذكور في المقال، مثل «أداء لافتًا» أو «تألق» أو «ضربة موجعة» أو «سيكتب التاريخ». "
        "ولا تصف فوزًا بأنه «مستحق» أو «كبير»، ولا مباراة بأنها «كلاسيكو» أو «قمة» أو «تاريخية»، إلا لو قال المقال ذلك صراحة.\n"
        "- لا تتوقع نتيجة مباراة لم تنتهِ. الحماس والأسلوب يخصان الصياغة فقط ولا يضيفان وقائع ولا يسخران من شخص.\n"
        "- لا تكتب «الكل بيتكلم» أو «الجميع يرى» إلا لو قال المقال ذلك. راجع الإملاء والنحو (مثل «ثلاث نقاط»).\n"
        "- لو الخبر شائعة أو غير مؤكد، صغه بحذر (ذكرت تقارير / يتردد أن) أو ضع skip=true.\n"
        "- ضع skip=true لو الخبر ليس عن كرة القدم، أو ضعيف جدًا، أو تافه، أو نص المقال ناقص أو غير واضح.\n"
        "- لو خبران أو أكثر يتناولان نفس الحدث، اكتب منشورًا لواحد فقط وضع skip=true للباقي.\n"
        "- ابدأ بجملة قوية تشد القارئ، ونوّع في الافتتاحيات. إيموجي من 0 إلى 2 بذوق.\n"
        "- لا إساءة لأي لاعب أو نادٍ أو جمهور أو دولة.\n\n"
        "حقل images: قائمة من 0 إلى 4 مواضيع للصورة، مرتبة من الأنسب إلى الأقل. كل عنصر واحد من هذه الأنواع "
        "(الأسماء بالإنجليزية كما في ويكيبيديا):\n"
        '- {"kind":"person","person":"Mohamed Salah","team":"Liverpool"}: لاعب أو مدرب يدور حوله الخبر، و team هو النادي '
        "أو المنتخب الذي يرتدي قميصه في سياق الخبر (قد يكون مختلفًا عن ناديه الحالي).\n"
        '- {"kind":"match","team":"Liverpool","opponent":"Manchester City"}: لخبر مباراة بين فريقين محددين.\n'
        '- {"kind":"team","team":"Al Ahly SC"}: لخبر عن نادٍ أو منتخب بعينه.\n'
        '- {"kind":"venue","venue":"Anfield"}: ملعب محدد مذكور في المقال أو ملعب صاحب الأرض.\n'
        '- {"kind":"competition","name":"UEFA Champions League"}: لخبر عن بطولة أو مسابقة.\n'
        "اترك القائمة فارغة لو الخبر إداري أو عام ولا يرتبط بشخص أو فريق أو ملعب (قرار اتحاد، تعديل لائحة، "
        "جدول مواعيد...).\n"
        "حقل card_title: عنوان قصير جذاب غير مضلل (حتى 70 حرفًا) بالفصحى يلخص الخبر، يُكتب على بطاقة الصورة.\n\n"
        'أخرج JSON فقط، مصفوفة عناصرها: {"id": "...", "skip": false, "text": "...", "card_title": "...", "images": []}\n\n'
        "الأخبار:\n\n" + "\n\n".join(blocks) + recent_block
    )


class GeminiQuotaError(Exception):
    """Every (key, model) pair is out of quota or unavailable right now."""


_DEAD = set()  # (key_index, model) pairs that returned 429 / 403 during this run


def gemini_keys():
    raw = os.environ.get("GEMINI_API_KEYS") or (os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY")) or ""
    keys = [k.strip() for k in re.split(r"[,\s]+", raw) if k.strip()]
    if not keys:
        raise RuntimeError("no Gemini key: set GEMINI_API_KEYS (comma/newline separated) or GEMINI_API_KEY")
    return keys


def gemini_models():
    ms = S.get("gemini_models") or [S["gemini_model"], S.get("gemini_fallback_model")]
    return [m for m in ms if m]


def call_gemini(parts, model=None, temperature=1.0):
    """Try every model, and for each model every key (quota is per project + model).
    A pair that hits its quota is skipped for the rest of the run. Raises GeminiQuotaError when all are exhausted."""
    if isinstance(parts, str):
        parts = [{"text": parts}]
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
    }
    keys = gemini_keys()
    models = [model] + [m for m in gemini_models() if m != model] if model else gemini_models()
    last, quota_only = None, True
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        for ki, key in enumerate(keys):
            if (ki, m) in _DEAD:
                continue
            headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
            for attempt in range(2):
                try:
                    r = requests.post(url, headers=headers, json=body, timeout=120)
                except requests.RequestException as e:
                    last, quota_only = f"{m}: {e}", False
                    time.sleep(3)
                    continue
                if r.status_code in (429, 403):  # quota / rate limit / key problem -> next key or model
                    _DEAD.add((ki, m))
                    last = f"key#{ki + 1} {m}: {r.status_code} {r.text[:120]}"
                    print(f"[gemini] {last} -> trying next", file=sys.stderr)
                    break
                if r.status_code in (500, 503):
                    last, quota_only = f"{m}: {r.status_code}", False
                    time.sleep(5 * (attempt + 1))
                    continue
                try:
                    r.raise_for_status()
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
                    return json.loads(text)
                except (requests.HTTPError, KeyError, IndexError, ValueError) as e:
                    last, quota_only = f"{m}: {type(e).__name__} {str(e)[:100]}", False
                    break
    if quota_only:
        raise GeminiQuotaError(last or "no usable key/model")
    raise RuntimeError(f"Gemini failed: {last}")


def vision_call(parts):
    return call_gemini(parts, None, 0.0)


def write_posts(items, recent=()):
    return call_gemini(build_prompt(items, recent), None)


# ---------------------------------------------------------------- facebook
class FacebookAuthError(Exception):
    pass


def _fb_check(r):
    if r.status_code != 200:
        err = (r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else {}) or {}
        msg = f"FB error {r.status_code}: code={err.get('code')} {err.get('message')}"
        if err.get("code") in (190, 102, 200, 10):
            raise FacebookAuthError(msg)
        raise RuntimeError(msg)
    j = r.json()
    return j.get("post_id") or j.get("id")


def publish(text):
    url = f"https://graph.facebook.com/{S['graph_version']}/{os.environ['FB_PAGE_ID']}/feed"
    r = requests.post(url, data={"message": text, "access_token": os.environ["FB_PAGE_TOKEN"]}, timeout=30)
    return _fb_check(r)


def publish_photo(caption, img_bytes, mime):
    url = f"https://graph.facebook.com/{S['graph_version']}/{os.environ['FB_PAGE_ID']}/photos"
    ext = {"image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
    r = requests.post(
        url,
        data={"caption": caption, "access_token": os.environ["FB_PAGE_TOKEN"]},
        files={"source": (f"photo.{ext}", img_bytes, mime)},
        timeout=90,
    )
    return _fb_check(r)


# -------------------------------------------------------------------- main
def diagnose_token():
    """Say whose token FB_PAGE_TOKEN is, without printing it."""
    pid = os.environ.get("FB_PAGE_ID", "").strip()
    try:
        r = requests.get(
            f"https://graph.facebook.com/{S['graph_version']}/me",
            params={"fields": "id,name", "access_token": os.environ["FB_PAGE_TOKEN"].strip()},
            timeout=20,
        )
        j = r.json()
    except Exception as e:
        print(f"[token-check] could not query Facebook: {e}")
        return
    if "error" in j:
        err = j["error"]
        print(f"[token-check] token rejected: code={err.get('code')} {err.get('message')}")
        return
    print(f"[token-check] FB_PAGE_TOKEN belongs to: {j.get('name')} (id={j.get('id')})")
    print(f"[token-check] FB_PAGE_ID secret is:     {pid}")
    if j.get("id") == pid:
        print("[token-check] OK: this is a token for the right page.")
    else:
        print(
            "[token-check] PROBLEM: the token is NOT the page's token. If the name above is a person, "
            "this is a User token: take the page's own access_token from me/accounts instead."
        )


def sanitize(text):
    """Safety net: the post must never contain links, hashtags or a source line."""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"(?m)^\s*(?:[#＃]\S+\s*)+$", "", t)
    t = re.sub(r"[#＃]\w+", "", t)
    t = re.sub(r"(?m)^.*(?:المصدر|المصادر)\s*:.*$", "", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def valid_text(t):
    return (
        isinstance(t, str)
        and S.get("min_post_chars", 200) <= len(t) <= 3000
        and bool(re.search(r"[؀-ۿ]", t))
    )


def as_specs(res):
    specs = res.get("images")
    if specs is None and isinstance(res.get("image"), dict):  # old format
        specs = [res["image"]]
    return [x for x in (specs if isinstance(specs, list) else []) if isinstance(x, dict)]


def headline_for_card(res, text):
    t = re.sub(r"\s+", " ", str(res.get("card_title") or "")).strip()
    if len(t) < 8:
        first = re.split(r"(?<=[.!؟?])\s", text.strip())[0]
        t = first
    if len(t) > 90:
        t = t[:90].rsplit(" ", 1)[0] + "…"
    return t


def photo_credit_line(img):
    if img.get("kind") == "card":
        return ""
    label = "صورة أرشيفية" if IMG.get("archive_label", True) else ""
    core = img.get("credit_core", "")
    if not label and not core:
        return ""
    return "📷 " + (f"{label} — {core}" if label and core else label or core)


def save_preview(img, idx):
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    ext = {"image/png": "png", "image/webp": "webp"}.get(img["mime"], "jpg")
    path = out / f"{idx:02d}-{img.get('kind', 'img')}.{ext}"
    path.write_bytes(img["bytes"])
    return path


def fill_articles(pool, st, dry):
    """Attach the full article text to each candidate; drop those we can't read."""
    mc = S.get("min_article_chars", 350)
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        texts = list(ex.map(lambda it: articles.article_text(it["url"], mc), pool))
    ok = []
    for it, txt in zip(pool, texts):
        if txt:
            it["article"] = txt
            ok.append(it)
        elif not dry:
            st["seen"].extend(it["all_ids"])  # unreadable (paywall/blocked): don't retry forever
    print(f"articles: {len(ok)}/{len(pool)} candidates had readable full text")
    return ok


def parse_image_line(line):
    """'Person|Team' | 'match:A|B' | 'team:X' | 'venue:X' | 'competition:X'"""
    low = line.lower()
    if low.startswith("match:"):
        a, _, b = line.split(":", 1)[1].partition("|")
        return {"kind": "match", "team": a.strip(), "opponent": b.strip()}
    if low.startswith("team:"):
        return {"kind": "team", "team": line.split(":", 1)[1].strip()}
    if low.startswith("venue:"):
        return {"kind": "venue", "venue": line.split(":", 1)[1].strip()}
    if low.startswith("competition:"):
        return {"kind": "competition", "name": line.split(":", 1)[1].strip()}
    person, _, team = line.partition("|")
    return {"kind": "person", "person": person.strip(), "team": team.strip() or None}


def check_images():
    """--check-images: print the photo chosen for each line of IMAGE_QUERIES and save it to out/. Posts nothing."""
    vf = vision_call if (os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY")) else None
    lines = [l.strip() for l in os.environ.get("IMAGE_QUERIES", "").splitlines() if l.strip()]
    for i, line in enumerate(lines, 1):
        spec = parse_image_line(line)
        print("=" * 60, f"\n{spec}")
        img = images.find_image(spec, [], IMG, vision_fn=vf, verbose=True)
        if img:
            path = save_preview(img, i)
            print(f"  RESULT: {img['page']}\n  license: {img['license']}\n  caption line: {photo_credit_line(img) or '(none)'}\n  saved: {path}")
        else:
            print("  RESULT: no suitable photo found")
    # headline-card preview
    sample = os.environ.get("CARD_SAMPLE", "اتحاد الكرة يعلن تعديل لائحة الدوري: 20 فريقًا في موسم 2027/2028")
    p = save_preview({"bytes": card.make_card(sample), "mime": "image/jpeg", "kind": "card"}, 99)
    print(f"\ncard sample saved: {p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--check-token", action="store_true")
    ap.add_argument("--check-images", action="store_true")
    args = ap.parse_args()
    if args.check:
        return check_feeds()
    if args.check_token:
        return diagnose_token()
    if args.check_images:
        return check_images()

    dry = os.environ.get("DRY_RUN") == "1"
    force = os.environ.get("FORCE") == "1"
    st = load_state()
    now_c = dt.datetime.now(CAIRO)
    today = now_c.strftime("%Y-%m-%d")

    lo, hi = S["quiet_hours_cairo"]
    if not force and lo <= now_c.hour < hi:
        print(f"quiet hours ({now_c:%H:%M} Cairo) - skipping")
        return
    remaining = S["daily_cap"] - st["posts"].get(today, 0)
    if not force and remaining <= 0:
        print("daily cap reached - skipping")
        return
    n = S["max_posts_per_run"] if force else min(S["max_posts_per_run"], remaining)

    items = fetch_all()
    print(f"fetched {len(items)} fresh items from {len(CFG['sources'])} sources")
    if DROPPED:
        print("dropped (publisher not trusted):", ", ".join(f"{k}({v})" for k, v in DROPPED.most_common(15)))
    ranked = rank(items, st)
    if not ranked:
        print("nothing new")
        return

    pool = select(ranked, S.get("candidate_pool", 12))
    chosen = fill_articles(pool, st, dry)[: n + 2]  # spares in case Gemini skips some / no photo is found
    if not chosen:
        print("no candidate with readable full text")
        if not dry:
            save_state(st)
        return
    for it, stl in zip(chosen, pick_styles(len(chosen))):
        it["style"] = stl
    try:
        written = write_posts(chosen, st["recent_headlines"][-25:])
    except GeminiQuotaError as e:
        # nothing was marked as seen, so these stories are tried again in the next run (10 minutes later)
        print(f"::warning::Gemini quota exhausted on all keys/models ({e}); will retry next run")
        if not dry:
            save_state(st)
        return
    results = {r.get("id"): r for r in written if isinstance(r, dict)}

    published = 0
    for it in chosen:
        if published >= n:
            break
        res = results.get(it["id"], {})
        # whatever happens, never retry this story in a later run
        if not dry:
            st["seen"].extend(it["all_ids"])
        if res.get("skip") or not valid_text(res.get("text")):
            print(f"skipped: {it['title'][:70]}")
            continue
        text = sanitize(res["text"])
        if not valid_text(text):
            print(f"skipped (text invalid after cleanup): {it['title'][:70]}")
            continue
        img = None
        if os.environ.get("CARDS_ONLY") == "1" or IMG.get("cards_only"):
            recent = st.setdefault("recent_cards", [])
            pool_styles = [n for n in card.STYLE_NAMES if n not in recent[-2:]] or card.STYLE_NAMES
            cstyle = random.choice(pool_styles)
            recent.append(cstyle)
            del recent[:-6]
            img = {"bytes": card.make_card(headline_for_card(res, text), style=cstyle), "mime": "image/jpeg",
                   "title": "card", "page": f"(headline card: {cstyle})", "license": "", "credit_core": "", "kind": "card"}
        elif IMG.get("enabled", True):
            vf = vision_call if IMG.get("vision_check", True) else None
            img = images.find_for_story(as_specs(res), st["recent_images"], IMG, vision_fn=vf)
            if img is None:
                policy = IMG.get("no_image_policy", "card")
                if policy == "card":
                    img = {"bytes": card.make_card(headline_for_card(res, text)), "mime": "image/jpeg", "title": "card",
                           "page": "(headline card)", "license": "", "credit_core": "", "kind": "card"}
                elif policy == "skip":
                    print(f"skipped (no suitable photo): {it['title'][:70]}  images={as_specs(res)}")
                    continue
        credit = photo_credit_line(img) if img else ""
        caption = text + (f"\n\n{credit}" if credit else "")
        if dry:
            print("-" * 60, f"\n[{it['style']['name']}] {it['title']}\n{caption}")
            if img:
                print(f"image: {img.get('kind')} | {img['page']} | {img['license']} | saved: {save_preview(img, published + 1)}")
            else:
                print("image: -")
            published += 1
            continue
        try:
            post_id = publish_photo(caption, img["bytes"], img["mime"]) if img else publish(caption)
        except FacebookAuthError as e:
            # token/permission problem, not the story's fault: keep it available for the next run
            burned = set(it["all_ids"])
            st["seen"] = [x for x in st["seen"] if x not in burned]
            save_state(st)
            print(f"::error::{e}")
            diagnose_token()
            sys.exit(1)
        except Exception as e:
            print(f"::warning::{e}", file=sys.stderr)
            continue
        if img and img.get("kind") != "card":
            st["recent_images"].append(img["title"])
        st["recent_titles"].append(sorted(it["tok"]))
        st["recent_headlines"].append(it["title"])
        st["posts"][today] = st["posts"].get(today, 0) + 1
        published += 1
        print(f"posted {post_id}: {it['title'][:70]}")
        save_state(st)
        if published < n:
            time.sleep(random.uniform(*S["delay_between_posts_sec"]))

    if not dry:
        save_state(st)
    print(f"done: {published} post(s)")


if __name__ == "__main__":
    main()
