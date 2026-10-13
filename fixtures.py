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


def todays_matches(leagues, day, max_matches=30, fetch=_fetch, verbose=True, after=None):
    """day: a datetime.date in Cairo. A match counts if its kick-off is on that Cairo day."""
    seen, out = set(), []
    # ESPN buckets by (US) calendar day, so ask for the day before as well and filter by Cairo date ourselves
    asks = [(day - dt.timedelta(days=1)).strftime("%Y%m%d"), day.strftime("%Y%m%d")]
    for prio, lg in enumerate(leagues):
        got = 0
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
                out.append(m)
                got += 1
        if verbose and got:
            print(f"[fixtures] {lg['code']}: {got} match(es)")
    out.sort(key=lambda m: (m["prio"], m["kickoff"]))
    return out[:max_matches]


def as_prompt_block(matches):
    """Plain, fully-resolved data for the model: times are already Cairo local time."""
    lines, last = [], None
    for m in matches:
        if m["competition"] != last:
            lines.append(f"\n[{m['competition']}]")
            last = m["competition"]
        lines.append(f"- {m['home']} vs {m['away']} | {m['kickoff']:%H:%M}")
    return "\n".join(lines).strip()
