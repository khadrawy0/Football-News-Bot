#!/usr/bin/env python3
"""Football news -> Gemini (free tier) -> Facebook Page. Runs from GitHub Actions.

Env vars:
  GEMINI_API_KEY, FB_PAGE_ID, FB_PAGE_TOKEN   (required unless --check / DRY_RUN)
  DRY_RUN=1   print posts, don't publish, don't touch state
  FORCE=1     ignore quiet hours and daily cap (for testing)
Flags:
  --check     test every feed and print how many fresh items each returns
  --check-article-images   for the top stories: fetch the article, show which photo would be used (saved to out/)
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

import articleimg
import articles
import card
import fixtures
import images

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "state.json"
CAIRO = ZoneInfo("Africa/Cairo")
UA = {"User-Agent": "Mozilla/5.0 (compatible; FootballNewsBot/1.0)"}
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
S = CFG["settings"]
DROPPED = Counter()
SKIP_RE = [re.compile(p) for p in S.get("skip_title_patterns", [])]
SKIP_URL_RE = [re.compile(p, re.I) for p in S.get("skip_url_patterns", [])]  # non-football sections of mixed feeds (Sky's boxing, F1...)
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
        if any(p.search(link) for p in SKIP_URL_RE):  # boxing / F1 / tennis... links
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
                "feed_imgs": feed_images(e),
                "tok": tokens(title),
            }
        )
    return src, items


def feed_images(e):
    """Photo URLs the RSS entry itself carries (media:content / media:thumbnail / image enclosure)."""
    urls = []
    for k in ("media_content", "media_thumbnail"):
        for mm in e.get(k) or []:
            if mm.get("url"):
                urls.append(mm["url"])
    for l in e.get("links") or []:
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image/") and l.get("href"):
            urls.append(l["href"])
    return urls


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
P = CFG.get("priority", {})


def _fold(text):
    import unicodedata
    t = unicodedata.normalize("NFKD", text)
    return norm_ar("".join(c for c in t if not unicodedata.combining(c))).lower()


def _star_patterns():
    pats = []
    for name in P.get("stars", []):
        n = re.escape(_fold(name))
        if re.search(r"[\u0600-\u06ff]", n):  # Arabic: allow one attached prefix (و ف ب ل ك ال ...)
            rx = r"(?<![\u0600-\u06ff])(?:[وفبلك]|ال|وال|بال|لل)?" + n + r"(?![\u0600-\u06ff])"
        else:
            rx = r"(?<![a-z0-9])" + n + r"(?![a-z0-9])"
        pats.append((name, re.compile(rx)))
    return pats


STARS = _star_patterns()
STMT_RE = re.compile("|".join(f"(?:{p})" for p in P["statement_patterns"]), re.I) if P.get("statement_patterns") else None


def star_hit(text):
    t = _fold(text)
    for name, rx in STARS:
        if rx.search(t):
            return name
    return None


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
        blob = " ".join(x["title"] + " " + x.get("summary", "") for x in m)
        star = star_hit(blob)
        if star:
            lead["star"] = star
            recent_stars = st.get("recent_stars", [])[-P.get("star_window", 6):]
            # a small nudge only, and none when stars already fill the recent posts or this same star was just covered
            if sum(1 for x in recent_stars if x) < P.get("star_max_in_window", 2) and star not in recent_stars[-3:]:
                lead["score"] += P.get("star_boost", 1.5)
        if n_src >= P.get("buzz_sources", 3):  # many outlets cover it = the audience cares
            lead["score"] += P.get("buzz_boost", 4)
            lead["buzz"] = n_src
        if STMT_RE and any(STMT_RE.search(x["title"]) for x in m):  # a coach / player statement: what readers share most
            lead["score"] += P.get("statement_boost", 1.5)
            lead["statement"] = True
        lead["all_ids"] = [x["id"] for x in m]
        seen_src = {lead["source"]}
        lead["alts"] = []  # the same story at other outlets: their own photos are extra candidates
        for x in sorted(m, key=lambda y: -y["weight"]):
            if x["source"] not in seen_src and x is not lead:
                seen_src.add(x["source"])
                lead["alts"].append({"url": x["url"], "source": x["source"], "feed_imgs": x.get("feed_imgs") or []})
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
        note = "note: العنوان يوحي بتصريحات؛ ابحث في المقال عن كلام المدرب أو اللاعب واجعله محور المنشور.\n" if it.get("statement") else ""
        blocks.append(
            f'id: {it["id"]}\nheadline: {it["title"]}\n'
            f'style: {st["name"]} — {st["desc"]}\n{note}'
            f'article:\n{(it.get("article") or "")[:5000]}'
        )
    return (
        "أنت محرر محتوى محترف لصفحة فيسبوك كبيرة عن كرة القدم، جمهورها مصري وعربي.\n"
        "اكتب منشورًا واحدًا لكل خبر من الأخبار التالية، بالأسلوب المحدد له. تحت كل خبر نص المقال المصدر.\n\n"
        "قواعد المحتوى:\n"
        "- المنشور هو الخبر كاملًا: غطِّ كل المعلومات الأساسية (من؟ ماذا؟ متى؟ أين؟ الأرقام والنتائج والتصريحات المهمة "
        "والخلفية الضرورية) بحيث لا يحتاج القارئ إلى فتح المصدر. الطول من 5 إلى 10 جمل في المجموع. لا تحذف معلومة جوهرية.\n"
        "- تنسيق المنشور (مهم جدًا، فالمنشور ليس كتلة نص واحدة): السطر الأول عنوان قوي قصير (من 6 إلى 14 كلمة) يبدأ بإيموجي واحد "
        "مناسب للخبر (⚽ 🚨 🔥 ✅ 🏆 💬 ⚠️ ...) ولا ينتهي بنقطة ويلخص أهم ما في الخبر. بعده سطر فارغ، ثم المحتوى في 2 إلى 4 فقرات قصيرة "
        "(من جملة إلى ثلاث) بينها سطر فارغ (\\n\\n). ابدأ كل فقرة برمز مناسب: 🔹 للمعلومة الأساسية، 📌 للتفصيلة المهمة، 💬 للتصريح، "
        "📊 للأرقام والنتائج، ⏰ للتوقيت، 🔙 للخلفية؛ نوّع الرموز ولا تكرر الرمز نفسه في فقرتين متتاليتين. الأسلوب المحدد لكل خبر "
        "يغيّر النبرة وشكل المحتوى (نقاط، سؤال ختامي...) لكن العنوان يبقى في أول سطر دائمًا.\n"
        "- لو في المقال تصريح لمدرب أو لاعب أو مسؤول فاجعله محور المنشور: انقل أهم عبارة فيه (أقل من 15 كلمة) بين «» مع اسم قائلها "
        "وصفته، ثم أكمل ما قاله بصياغتك.\n"
        "- اكتب لجمهور مصري وعربي: أبرز الجانب الذي يهم القارئ المصري (محمد صلاح، المنتخب المصري، الأهلي والزمالك، أي لاعب أو نادٍ "
        "مصري أو عربي) متى كان مذكورًا في المقال، دون اختلاق أي صلة.\n"
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
        "- نوّع في صياغة العناوين والافتتاحيات من منشور إلى آخر ولا تكرر القالب نفسه.\n"
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


_DISCOVERED = None
_BAD = ("image", "tts", "live", "audio", "embedding", "robotics", "computer", "native", "exp", "vision", "learnlm", "gemma", "aqa", "imagen", "veo")


def _discover(key):
    """Ask the API which text models this key can actually call (names change/retire over time)."""
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": key}, timeout=30)
        r.raise_for_status()
        out = []
        for mo in r.json().get("models", []):
            name = mo.get("name", "").split("/")[-1]
            if "generateContent" not in mo.get("supportedGenerationMethods", []):
                continue
            if "flash" not in name or any(b in name for b in _BAD):
                continue
            out.append(name)
        def ver(n):
            v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            return float(v.group(1)) if v else 0.0
        # newest generation first; within it plain flash before flash-lite; stable before preview
        out.sort(key=lambda n: (-ver(n), "lite" in n, "preview" in n, n))
        return out
    except Exception as e:
        print(f"[gemini] model discovery failed: {e}", file=sys.stderr)
        return []


def gemini_models():
    """Configured models first, then whatever the API says is available (so a retired model never stops the bot)."""
    global _DISCOVERED
    ms = list(S.get("gemini_models") or [S.get("gemini_model"), S.get("gemini_fallback_model")])
    if S.get("gemini_auto_discover", True):
        if _DISCOVERED is None:
            _DISCOVERED = _discover(gemini_keys()[0])[:4]
            print(f"[gemini] discovered models: {_DISCOVERED}")
        ms += _DISCOVERED
    seen, out = set(), []
    for x in ms:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def check_gemini():
    """Per key: which models are listed and whether a tiny call works (no keys are printed)."""
    for i, key in enumerate(gemini_keys(), 1):
        found = _discover(key)
        print(f"key#{i}: models={found[:6]}")
        for mo in (S.get("gemini_models") or []) + found[:3]:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{mo}:generateContent",
                              headers={"x-goog-api-key": key}, timeout=60,
                              json={"contents": [{"parts": [{"text": "قل: تم"}]}]})
            print(f"   {mo}: {r.status_code} {'OK' if r.status_code == 200 else r.text[:110].replace(chr(10), ' ')}")


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
                if r.status_code in (429, 403, 404) or (r.status_code == 400 and "API key" in r.text):  # quota / key problem / retired model -> next
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


def publish_photos(caption, jpgs):
    """One post with several photos: upload each unpublished, then attach them all to a single feed post."""
    if len(jpgs) == 1:
        return publish_photo(caption, jpgs[0], "image/jpeg")
    base = f"https://graph.facebook.com/{S['graph_version']}/{os.environ['FB_PAGE_ID']}"
    ids = []
    for i, jpg in enumerate(jpgs):
        r = requests.post(
            f"{base}/photos",
            data={"published": "false", "access_token": os.environ["FB_PAGE_TOKEN"]},
            files={"source": (f"card{i + 1}.jpg", jpg, "image/jpeg")},
            timeout=90,
        )
        ids.append(_fb_check(r))
    data = {"message": caption, "access_token": os.environ["FB_PAGE_TOKEN"]}
    for i, pid in enumerate(ids):
        data[f"attached_media[{i}]"] = json.dumps({"media_fbid": pid})
    return _fb_check(requests.post(f"{base}/feed", data=data, timeout=60))


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
        t = text.strip().split("\n")[0]  # the post's first line is its headline
    t = re.sub(r"[^\w\s\u0600-\u06FF«»\"'’:،؟?!.\-–%/]", "", t).strip()  # the card font has no emoji
    if len(t) > 90:
        t = t[:90].rsplit(" ", 1)[0] + "…"
    return t


def pick_card_size(st):
    """'portrait' (big 4:5) or 'landscape' (classic), weighted by images.card_sizes, never the same size 3 times in a row."""
    weights = IMG.get("card_sizes") or {"portrait": 3, "landscape": 2}
    names = [n for n in weights if n in card.SIZES]
    recent = st.setdefault("recent_card_sizes", [])
    pool = [n for n in names if recent[-2:].count(n) < 2] or names
    size = random.choices(pool, weights=[weights[n] for n in pool])[0]
    recent.append(size)
    del recent[:-6]
    return size


def want_photo(st):
    """True = try a real photo for this post, False = headline card. Steers the long-run share to images.photo_share (default 0.5)."""
    share = float(IMG.get("photo_share", 0.5))
    recent = st.setdefault("recent_kinds", [])
    if len(recent) >= 6:  # drift correction: if photos ran ahead/behind the target, lean the other way
        actual = recent.count("photo") / len(recent)
        share = min(1.0, max(0.1, share + 1.5 * (share - actual)))
    return random.random() < share


def photo_credit_line(img):
    if img.get("kind") == "card":
        return ""
    if img.get("kind") == "article":
        ai = IMG.get("article_images") or {}
        if not ai.get("credit", True) or not img.get("credit_core"):
            return ""
        return f"📷 {ai.get('credit_prefix', 'الصورة:')} {img['credit_core']}"
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
        pages = list(ex.map(lambda it: articles.fetch_article(it["url"], mc), pool))
    ok = []
    for it, pg in zip(pool, pages):
        if pg:
            it["article"] = pg["text"]
            it["img_cands"] = pg["images"]  # lead photo candidates of the article page
            it["img_agency"] = pg["agency"]  # the page credits a photo agency (Getty, Reuters, AP...)
            it["page_url"] = pg["url"]
            ok.append(it)
        elif not dry:
            st["seen"].extend(it["all_ids"])  # unreadable (paywall/blocked): don't retry forever
    print(f"articles: {len(ok)}/{len(pool)} candidates had readable full text")
    return ok


def load_alts(it, limit):
    """Fetch the pages of the same story at other outlets (only when the lead article has no usable photo) -> it["alt_pages"]."""
    if "alt_pages" in it:
        return
    alts = (it.get("alts") or [])[:limit]
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        pages = list(ex.map(lambda a: articles.fetch_article(a["url"], 0), alts))
    it["alt_pages"] = [
        {"source": a["source"], "img_cands": pg["images"], "agency": pg["agency"], "feed_imgs": a["feed_imgs"]}
        for a, pg in zip(alts, pages) if pg
    ] + [{"source": a["source"], "img_cands": [], "agency": False, "feed_imgs": a["feed_imgs"]} for a, pg in zip(alts, pages) if not pg and a["feed_imgs"]]


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


def check_article_images():
    """--check-article-images: fetch the top stories' articles and show which photo each post would get. Posts nothing."""
    ai = IMG.get("article_images") or {}
    st = load_state()
    ranked = rank(fetch_all(), st)
    pool = fill_articles(ranked[: int(os.environ.get("N", "12"))], st, True)
    got = 0
    for i, it in enumerate(pool, 1):
        print("=" * 60, f"\n{it['source']}: {it['title'][:90]}")
        print(f"  page: {it.get('page_url', '')[:110]}\n  candidates: {len(it.get('img_cands') or [])} | feed thumbnails: {len(it.get('feed_imgs') or [])}"
              f" | credited to an agency: {it.get('img_agency')}")
        vf = vision_call if (ai.get("verify", True) and (os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY"))) else None
        img = articleimg.pick(it, st["recent_images"], ai, vision_fn=vf)
        if img is None and ai.get("other_outlets", 3):
            print(f"  lead article has no usable photo -> trying {len(it.get('alts') or [])} other outlet(s) with the same story")
            load_alts(it, ai.get("other_outlets", 3))
            img = articleimg.pick(it, st["recent_images"], ai, vision_fn=vf, use_alts=True)
        if img:
            got += 1
            print(f"  RESULT: photo saved: {save_preview(img, i)}  | credit line: {photo_credit_line(img) or '(none)'}")
        else:
            print(f"  RESULT: no usable photo -> fallback '{ai.get('fallback', 'card')}'")
    print(f"\n{got}/{len(pool)} stories would get a real article photo")


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
    for k, size in enumerate(card.SIZE_NAMES):  # both card sizes
        p = save_preview({"bytes": card.make_card(sample, size=size), "mime": "image/jpeg", "kind": "card"}, 99 - k)
        print(f"\ncard sample ({size}) saved: {p}")


FX = CFG.get("fixtures", {})


def _t12(k):
    h = k.hour % 12 or 12
    return f"{h}:{k.minute:02d} {'ص' if k.hour < 12 else 'م'}"


def _date_ar(day):
    months = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
    days = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    return f"{days[day.weekday()]} {day.day} {months[day.month - 1]}"


def arabic_team_names(matches, st):
    """English club/national-team names -> Arabic (cached in state so each name is translated once)."""
    cache = st.setdefault("team_ar", {})
    need = [n for n in fixtures.team_names(matches) if n not in cache]
    if need:
        prompt = (
            "ترجم أسماء الأندية والمنتخبات الرياضية التالية إلى العربية كما تُكتب في وسائل الإعلام الرياضي العربية "
            "(مثال: Real Madrid = ريال مدريد، Paris Saint-Germain = باريس سان جيرمان، Al Ahly = الأهلي). "
            "أعد JSON فقط على شكل كائن مفاتيحه الأسماء الإنجليزية كما وردت وقيمه الأسماء العربية، دون أي شرح.\n\n"
            + "\n".join(need)
        )
        res = call_gemini(prompt, None, 0.0)
        if isinstance(res, list):
            res = res[0] if res and isinstance(res[0], dict) else {}
        for k, v in (res or {}).items():
            if k in need and isinstance(v, str) and re.search(r"[\u0600-\u06ff]", v):
                cache[k] = v.strip()
    return cache


def fixtures_caption(day):
    return (f"⚽ أهم مباريات اليوم — {_date_ar(day)}\n\n"
            "📌 مواعيد أبرز مباريات اليوم في الكروت المرفقة\n"
            "🕒 جميع المواعيد بتوقيت القاهرة")


def fixtures_image(matches, st, day):
    names = arabic_team_names(matches, st)
    missing = [n for n in fixtures.team_names(matches) if n not in names]
    if missing:
        raise RuntimeError(f"no Arabic name for: {missing}")
    groups = fixtures.group_for_card(matches, names, _t12, FX.get("highlight"))
    pages = fixtures.split_pages(groups, FX.get("rows_per_card", 11), FX.get("max_cards", 3))
    return [card.make_fixtures_card(_date_ar(day), g, part=(i + 1, len(pages)) if len(pages) > 1 else None)
            for i, g in enumerate(pages)]


def daily_fixtures_post(st, now_c, today, dry, force, test=False):
    """One post per Cairo day, from FX['hour_cairo'] on: a short caption + a card listing the selected matches.
    test=True (FIXTURES_ONLY run): ignore the hour / already-posted checks and leave the daily state untouched,
    so a manual test never blocks (or counts as) the real morning post."""
    if not test and st.get("fixtures_date") == today and not (force and dry):
        return
    if now_c.hour < FX.get("hour_cairo", 10) and not force:
        return
    if now_c.hour >= FX.get("give_up_hour_cairo", 17) and not force:
        return  # too late to be a "today's matches" post
    matches = fixtures.todays_matches(FX["leagues"], now_c.date(), FX.get("max_matches", 36), after=None if test else now_c, sources=FX.get("sources"))
    if not matches:
        print("[fixtures] no important matches found for today (or the source is unreachable)")
        if test:
            return
        tries = st.setdefault("fixtures_tries", {})
        tries[today] = tries.get(today, 0) + 1
        if tries[today] >= 3 or now_c.hour >= FX.get("give_up_hour_cairo", 17) - 1:
            st["fixtures_date"] = today  # stop trying today
        if not dry:
            save_state(st)
        return
    print(f"[fixtures] {len(matches)} match(es) selected")
    try:
        jpgs = fixtures_image(matches, st, now_c.date())
    except GeminiQuotaError as e:
        print(f"::warning::fixtures post postponed, Gemini quota exhausted ({e})")
        return
    except Exception as e:
        print(f"::warning::fixtures card failed: {e}", file=sys.stderr)
        return
    text = fixtures_caption(now_c.date())
    if dry:
        saved = [save_preview({"bytes": j, "mime": "image/jpeg", "kind": "card"}, 90 + i) for i, j in enumerate(jpgs)]
        print("-" * 60, f"\n[FIXTURES]\n{text}\n{len(jpgs)} card(s) | saved: {saved}")
        return
    try:
        post_id = publish_photos(text, jpgs)
    except FacebookAuthError as e:
        print(f"::error::{e}")
        diagnose_token()
        sys.exit(1)
    except Exception as e:
        print(f"::warning::fixtures publish failed: {e}", file=sys.stderr)
        return
    print(f"posted fixtures {post_id}")
    if not test:
        st["fixtures_date"] = today
        st["posts"][today] = st["posts"].get(today, 0) + 1
    save_state(st)


def check_fixtures():
    now_c = dt.datetime.now(CAIRO)
    ms = fixtures.todays_matches(FX["leagues"], now_c.date(), FX.get("max_matches", 36), after=now_c, sources=FX.get("sources"))
    print(f"\n{len(ms)} match(es) selected for {now_c.date()} (Cairo)  [+ selected / - dropped above]")
    if ms and (os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY")):
        st = {}
        jpgs = fixtures_image(ms, st, now_c.date())
        print("caption:", fixtures_caption(now_c.date()))
        print("arabic names:", st.get("team_ar"))
        print(f"{len(jpgs)} card(s) saved:", [save_preview({"bytes": j, "mime": "image/jpeg"}, 90 + i) for i, j in enumerate(jpgs)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--check-token", action="store_true")
    ap.add_argument("--check-images", action="store_true")
    ap.add_argument("--check-article-images", action="store_true")
    ap.add_argument("--check-fixtures", action="store_true")
    ap.add_argument("--check-gemini", action="store_true")
    args = ap.parse_args()
    if args.check_gemini:
        return check_gemini()
    if args.check_fixtures:
        return check_fixtures()
    if args.check:
        return check_feeds()
    if args.check_token:
        return diagnose_token()
    if args.check_images:
        return check_images()
    if args.check_article_images:
        return check_article_images()

    dry = os.environ.get("DRY_RUN") == "1"
    force = os.environ.get("FORCE") == "1"
    st = load_state()
    now_c = dt.datetime.now(CAIRO)
    today = now_c.strftime("%Y-%m-%d")

    if os.environ.get("FIXTURES_ONLY") == "1":  # manual test of the daily matches post (see post.yml: post_fixtures)
        print(f"[fixtures-test] dry_run={dry}: building today's matches post now (hour / quiet-hours / already-posted checks skipped)")
        daily_fixtures_post(st, now_c, today, dry, True, test=True)
        return

    lo, hi = S["quiet_hours_cairo"]
    if not force and lo <= now_c.hour < hi:
        print(f"quiet hours ({now_c:%H:%M} Cairo) - skipping")
        return
    remaining = S["daily_cap"] - st["posts"].get(today, 0)
    if not force and remaining <= 0:
        print("daily cap reached - skipping")
        return
    n = S["max_posts_per_run"] if force else min(S["max_posts_per_run"], remaining)

    if FX.get("enabled") and (force or not (lo <= now_c.hour < hi)):
        daily_fixtures_post(st, now_c, today, dry, force)

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
    except Exception as e:  # never fail the whole run because the model call failed; stories stay unseen
        print(f"::warning::Gemini call failed ({e}); will retry next run")
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
        ai = IMG.get("article_images") or {}
        use_cards = os.environ.get("CARDS_ONLY") == "1" or IMG.get("cards_only")
        if not use_cards and ai.get("enabled") and not want_photo(st):
            use_cards = True  # this post is a card by the 50/50 mix
        if not use_cards and ai.get("enabled"):
            vf = vision_call if (ai.get("verify", True) and (os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY"))) else None
            img = articleimg.pick(it, st["recent_images"], ai, vision_fn=vf)  # the lead photo of the source article
            if img is None and ai.get("other_outlets", 3):
                load_alts(it, ai.get("other_outlets", 3))
                img = articleimg.pick(it, st["recent_images"], ai, vision_fn=vf, use_alts=True)  # same story, other outlets
            if img is None:
                fb = ai.get("fallback", "card")
                if fb == "skip":
                    print(f"skipped (no usable article photo): {it['title'][:70]}")
                    continue
                use_cards = fb == "card"  # "commons" falls through to the stock-photo search below
        if img is not None:
            pass
        elif use_cards:
            recent = st.setdefault("recent_cards", [])
            pool_styles = [n for n in card.STYLE_NAMES if n not in recent[-2:]] or card.STYLE_NAMES
            cstyle = random.choice(pool_styles)
            recent.append(cstyle)
            del recent[:-6]
            csize = pick_card_size(st)
            img = {"bytes": card.make_card(headline_for_card(res, text), style=cstyle, size=csize), "mime": "image/jpeg",
                   "title": "card", "page": f"(headline card: {cstyle}, {csize})", "license": "", "credit_core": "", "kind": "card"}
        elif IMG.get("enabled", True):
            vf = vision_call if IMG.get("vision_check", True) else None
            img = images.find_for_story(as_specs(res), st["recent_images"], IMG, vision_fn=vf)
            if img is None:
                policy = IMG.get("no_image_policy", "card")
                if policy == "card":
                    img = {"bytes": card.make_card(headline_for_card(res, text), size=pick_card_size(st)), "mime": "image/jpeg", "title": "card",
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
        kinds = st.setdefault("recent_kinds", [])
        kinds.append("card" if (not img or img.get("kind") == "card") else "photo")
        del kinds[:-20]
        if img and img.get("kind") != "card":
            st["recent_images"].append(img["title"])
        st["recent_titles"].append(sorted(it["tok"]))
        st["recent_headlines"].append(it["title"])
        st.setdefault("recent_stars", []).append(it.get("star"))
        del st["recent_stars"][:-12]
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
