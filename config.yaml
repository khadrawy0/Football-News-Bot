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

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "state.json"
CAIRO = ZoneInfo("Africa/Cairo")
UA = {"User-Agent": "Mozilla/5.0 (compatible; FootballNewsBot/1.0)"}
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
S = CFG["settings"]
DROPPED = Counter()
SKIP_RE = [re.compile(p) for p in S.get("skip_title_patterns", [])]


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
    st.setdefault("posts", {})
    return st


def save_state(st):
    st["seen"] = st["seen"][-5000:]
    st["recent_titles"] = st["recent_titles"][-300:]
    st["recent_headlines"] = st["recent_headlines"][-30:]
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
                "link": link if src.get("attach_link") else "",
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
            f'id: {it["id"]}\nsource: {it["source"]}\nheadline: {it["title"]}\n'
            f'details: {it["summary"] or "(headline only)"}\n'
            f'style: {st["name"]} — {st["desc"]}'
        )
    return (
        "أنت محرر محتوى محترف لصفحة فيسبوك كبيرة عن كرة القدم، جمهورها مصري وعربي.\n"
        "اكتب منشورًا واحدًا لكل خبر من الأخبار التالية، بالأسلوب المحدد له.\n\n"
        "القواعد:\n"
        "- اكتب بكلماتك أنت، ولا تنسخ عبارات من المصدر. المصدر قد يكون بالإنجليزية، والمنشور دائمًا بالعربية.\n"
        "- اكتب بالعربية الفصحى المبسطة الرسمية فقط. ممنوع العامية المصرية أو أي لهجة أخرى، حتى في الأسئلة والتعليقات القصيرة.\n"
        "- استخدم فقط المعلومات الموجودة في العنوان والتفاصيل. لا تخترع أرقامًا أو تصريحات أو نتائج.\n"
        "- ممنوع إضافة أي حكم أو وصف غير مذكور في المصدر، مثل «أداء لافتًا» أو «تألق» أو «ضربة موجعة» أو «هيكتب التاريخ». "
        "لو المتاح عنوان فقط، فاكتفِ بمعلومات العنوان وصغها بحماس بدون أي إضافة.\n"
        "- لا تتوقع ولا تعلّق بمبالغة على مباراة لم تنتهِ. الأسلوب الساخر أو الحماسي يخص الصياغة فقط، ولا يضيف وقائع ولا يسخر من شخص.\n"
        "- لا تصف فوزًا بأنه «مستحق» أو «كبير»، ولا مباراة بأنها «كلاسيكو» أو «قمة» أو «تاريخية» أو «لا تُنسى»، إلا لو المصدر قال ذلك صراحة.\n"
        "- لو خبران أو أكثر في القائمة يتناولان نفس الحدث (مثل نفس نتيجة المباراة)، اكتب منشورًا لواحد فقط وضع skip=true للباقي.\n"
        "- لا تكتب «الكل بيتكلم» أو «الجميع يرى» أو ما شابه إلا لو المصدر قال ذلك. راجع الإملاء والنحو (مثل «ثلاث نقاط» وليس «ثلاثة نقطة»).\n"
        "- لو الخبر شائعة أو غير مؤكد، صغه بحذر (بحسب تقارير / يتردد أن) أو ضع skip=true.\n"
        "- ضع skip=true لو الخبر ليس عن كرة القدم، أو ضعيف جدًا، أو تافه.\n"
        "- الطول من سطرين إلى خمسة أسطر. إيموجي من 1 إلى 3 بذوق.\n"
        "- قسّم المنشور بأسطر فارغة (\\n\\n): سطر افتتاحي، ثم الخبر، ثم سؤال أو تعليق قصير اختياري، "
        "ثم سطر منفصل أخير للهاشتاجات (من 2 إلى 4).\n"
        "- ابدأ بجملة قوية تشد القارئ، ونوّع في الافتتاحيات بين المنشورات.\n"
        "- لا تذكر المصدر في النص (سيُضاف تلقائيًا). لا روابط في النص.\n"
        "- لا إساءة لأي لاعب أو نادٍ أو جمهور أو دولة.\n\n"
        'أخرج JSON فقط، مصفوفة عناصرها: {"id": "...", "skip": false, "text": "..."}\n\n'
        "الأخبار:\n\n" + "\n\n".join(blocks) + recent_block
    )


def call_gemini(prompt, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 1.0, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"}
    last = None
    for attempt in range(3):
        r = requests.post(url, headers=headers, json=body, timeout=90)
        if r.status_code in (429, 500, 503):
            last = f"{r.status_code} {r.text[:200]}"
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)
    raise RuntimeError(f"Gemini failed on {model}: {last}")


def write_posts(items, recent=()):
    prompt = build_prompt(items, recent)
    try:
        return call_gemini(prompt, S["gemini_model"])
    except Exception as e:
        print(f"[gemini] primary failed ({e}); trying fallback", file=sys.stderr)
        return call_gemini(prompt, S["gemini_fallback_model"])


# ---------------------------------------------------------------- facebook
class FacebookAuthError(Exception):
    pass


def publish(text, link):
    url = f"https://graph.facebook.com/{S['graph_version']}/{os.environ['FB_PAGE_ID']}/feed"
    data = {"message": text, "access_token": os.environ["FB_PAGE_TOKEN"]}
    if link:
        data["link"] = link
    r = requests.post(url, data=data, timeout=30)
    if r.status_code != 200:
        err = (r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else {}) or {}
        msg = f"FB error {r.status_code}: code={err.get('code')} {err.get('message')}"
        if err.get("code") in (190, 102, 200, 10):
            raise FacebookAuthError(msg)
        raise RuntimeError(msg)
    return r.json().get("id")


# -------------------------------------------------------------------- main
def valid_text(t):
    return isinstance(t, str) and 20 <= len(t) <= 1500 and re.search(r"[؀-ۿ]", t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        return check_feeds()

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

    chosen = select(ranked, n + 3)  # spares in case Gemini skips some or finds duplicates
    for it, stl in zip(chosen, pick_styles(len(chosen))):
        it["style"] = stl
    results = {r.get("id"): r for r in write_posts(chosen, st["recent_headlines"][-25:]) if isinstance(r, dict)}

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
        text = res["text"].strip() + f"\n\n📰 المصدر: {it['source']}"
        if dry:
            print("-" * 60, f"\n[{it['style']['name']}] {it['title']}\n{text}\nlink: {it['link'] or '-'}")
            published += 1
            continue
        try:
            post_id = publish(text, it["link"])
        except FacebookAuthError as e:
            save_state(st)
            print(f"::error::{e}")
            sys.exit(1)
        except Exception as e:
            print(f"::warning::{e}", file=sys.stderr)
            continue
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
