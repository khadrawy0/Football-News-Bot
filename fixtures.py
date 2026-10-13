"""Today's matches (the daily morning post).

Source: ESPN's public scoreboard JSON (no key). It is an unofficial endpoint, so every league is fetched
independently and a dead/unknown league is just skipped. Run `--check-fixtures` to see what it returns.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import requests

CAIRO = ZoneInfo("Africa/Cairo")
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard"
UA = {"User-Agent": "Mozilla/5.0 (compatible; FootballNewsBot/1.0)"}


def _fetch(code, date_yyyymmdd):
    r = requests.get(BASE.format(code=code), params={"dates": date_yyyymmdd, "limit": 100}, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def _parse(event, comp_name, prio):
    try:
        comp = event["competitions"][0]
        teams = {c["homeAway"]: c["team"] for c in comp["competitors"]}
        home, away = teams["home"], teams["away"]
        when = dt.datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone(CAIRO)
        return {
            "id": str(event.get("id") or event["date"] + home["displayName"]),
            "competition": comp_name,
            "prio": prio,
            "home": home.get("displayName") or home.get("name"),
            "away": away.get("displayName") or away.get("name"),
            "kickoff": when,
            "state": (event.get("status") or {}).get("type", {}).get("state", "pre"),
        }
    except Exception:
        return None


def _fold(t):
    import unicodedata
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(t.replace("-", " ").replace(".", " ").split())


def _involves(m, names):
    h, a = _fold(m["home"]), _fold(m["away"])
    return [any(_fold(n) in side for n in names) for side in (h, a)]


def select_for_league(matches, lg):
    """Apply the league rule from config:
       teams: [..]     keep only matches involving one of these clubs (both: true = both sides must be listed)
       featured: [..]  when there are more than `max`, matches involving these clubs come first
       max: N          cap per league (default: no cap)"""
    chosen = list(matches)
    if lg.get("teams"):
        keep = []
        for m in chosen:
            hit = _involves(m, lg["teams"])
            if (all(hit) if lg.get("both") else any(hit)):
                keep.append(m)
        chosen = keep
    cap = lg.get("max")
    if cap and len(chosen) > cap:
        feat = lg.get("featured") or lg.get("teams") or []

        def score(m):
            return -sum(_involves(m, feat)) if feat else 0

        chosen = sorted(chosen, key=lambda m: (score(m), m["kickoff"]))[:cap]
    return sorted(chosen, key=lambda m: m["kickoff"])


def todays_matches(leagues, day, max_matches=30, fetch=_fetch, verbose=True, after=None):
    """Matches of the Cairo day `day`, filtered per league by the config rules, in config order."""
    seen, out = set(), []
    # ESPN buckets by (US) calendar day, so ask for the day before as well and filter by Cairo date ourselves
    asks = [(day - dt.timedelta(days=1)).strftime("%Y%m%d"), day.strftime("%Y%m%d")]
    for prio, lg in enumerate(leagues):
        found = []
        for d in asks:
            try:
                data = fetch(lg["code"], d)
            except Exception as e:
                if verbose:
                    print(f"[fixtures] {lg['code']} {d}: {e}")
                continue
            for ev in data.get("events", []):
                m = _parse(ev, lg["name"], prio)
                if not m or m["id"] in seen or m["kickoff"].date() != day:
                    continue
                if m["state"] == "post" or (after and m["kickoff"] < after - dt.timedelta(hours=2)):
                    continue  # already finished: not part of a "today's matches" post
                seen.add(m["id"])
                found.append(m)
        picked = select_for_league(found, lg)
        if verbose and found:
            pid = {m["id"] for m in picked}
            print(f"[fixtures] {lg['code']}: {len(found)} found, {len(picked)} selected")
            for m in sorted(found, key=lambda m: m["kickoff"]):
                print(f"    {'+' if m['id'] in pid else '-'} {m['kickoff']:%H:%M}  {m['home']} vs {m['away']}")
        out.extend(picked)
    out.sort(key=lambda m: (m["prio"], m["kickoff"]))
    return out[:max_matches]


def group_for_card(matches, ar_names, time_fmt):
    """-> [(competition_ar, [(home_ar, away_ar, time_str), ...]), ...] in display order."""
    groups, idx = [], {}
    for m in matches:
        if m["competition"] not in idx:
            idx[m["competition"]] = []
            groups.append((m["competition"], idx[m["competition"]]))
        idx[m["competition"]].append((ar_names.get(m["home"], m["home"]), ar_names.get(m["away"], m["away"]), time_fmt(m["kickoff"])))
    return groups


def team_names(matches):
    return sorted({n for m in matches for n in (m["home"], m["away"]) if n})


def split_pages(groups, rows_per_card=9, max_cards=4):
    """Spread the groups over as few, evenly filled cards as possible (a long competition may continue on the next card)."""
    total = sum(len(ms) for _, ms in groups)
    if total == 0:
        return []
    k = min(max_cards, max(1, -(-total // rows_per_card)))
    per = -(-total // k)  # balanced target rows per card
    pages, cur, n = [], [], 0
    for comp, ms in groups:
        ms = list(ms)
        while ms:
            room = per - n
            take, ms = ms[:room], ms[room:]
            cur.append((comp, take))
            n += len(take)
            if n >= per and len(pages) < k - 1:
                pages.append(cur)
                cur, n = [], 0
    if cur:
        pages.append(cur)
    return pages
