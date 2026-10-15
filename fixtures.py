"""Today's matches (the daily morning post) from SEVERAL sources with automatic fallback.

Sources (order = priority; set `fixtures.sources` in config.yaml):
  espn          ESPN public scoreboard JSON (no key, unofficial; per league)
  sofascore     Sofascore public JSON (no key, unofficial; one call per day, all leagues)
  footballdata  football-data.org v4 (free key in secret FOOTBALL_DATA_KEY; PL, La Liga, Serie A, Bundesliga,
                Ligue 1, Champions League, World Cup). Skipped automatically when the key is not set.
  thesportsdb   TheSportsDB eventsday (free test key "3", or your own in THESPORTSDB_KEY; one call per day)

For every league of the config the sources are tried in order and the FIRST one that returns matches for that
league wins, so a blocked/dead source (e.g. ESPN answering 403 to GitHub's servers) never stops the post.
Run `--check-fixtures` to see which source served which league.
"""
import datetime as dt
import os
import re
import unicodedata
from zoneinfo import ZoneInfo

import requests

CAIRO = ZoneInfo("Africa/Cairo")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_SOURCES = ["espn", "sofascore", "footballdata", "thesportsdb"]


def _http_get(url, params=None, headers=None, timeout=25):
    h = dict(HEADERS)
    h.update(headers or {})
    r = requests.get(url, params=params, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _fold(t):
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(t.replace("-", " ").replace(".", " ").split())


# Different sources spell clubs differently; each configured name also matches these variants.
ALIASES = {
    "paris saint germain": ["paris sg", "psg"],
    "atletico": ["atl madrid"],
    "manchester city": ["man city"],
    "manchester united": ["man united", "man utd"],
    "tottenham": ["spurs"],
    "bayern": ["fc bayern", "bayern munchen"],
    "inter": ["internazionale"],
    "sporting cp": ["sporting lisbon", "sporting lisboa"],
}


def _variants(name):
    n = _fold(name)
    return {n} | {_fold(a) for a in ALIASES.get(n, [])}


def _involves(m, names):
    h, a = _fold(m["home"]), _fold(m["away"])
    vs = set().union(*[_variants(n) for n in names]) if names else set()
    return [any(v in side for v in vs) for side in (h, a)]


# ------------------------------------------------------------------ providers
class Provider:
    name = "?"

    def __init__(self):
        self.ok = 0
        self.errors = 0
        self.served = {}
        self._warned = False

    def available(self):
        return True

    @property
    def dead(self):
        return self.errors >= 4 and self.ok == 0  # nothing works from here: stop hammering it

    def _fail(self, what, err):
        self.errors += 1
        if not self._warned:
            self._warned = True
            print(f"[fixtures] source '{self.name}' failed ({what}): {err}  (further errors of this source are not printed)")

    def league(self, code, asks):
        """-> list of raw-normalised matches, [] when the source has none, None when the source failed."""
        raise NotImplementedError


class ESPN(Provider):
    name = "espn"
    BASES = [
        "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard",
        "https://site.web.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard",
    ]

    @staticmethod
    def _parse(event):
        try:
            comp = event["competitions"][0]
            teams = {c["homeAway"]: c["team"] for c in comp["competitors"]}
            home, away = teams["home"], teams["away"]
            when = dt.datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone(CAIRO)
            return {
                "id": "espn" + str(event.get("id") or event["date"] + home["displayName"]),
                "home": home.get("displayName") or home.get("name"),
                "away": away.get("displayName") or away.get("name"),
                "kickoff": when,
                "state": (event.get("status") or {}).get("type", {}).get("state", "pre"),
            }
        except Exception:
            return None

    def league(self, code, asks):
        if self.dead:
            return None
        out, failed = [], 0
        for d in asks:
            data, err = None, None
            for base in self.BASES:
                try:
                    data = _http_get(base.format(code=code), {"dates": d, "limit": 100})
                    break
                except Exception as e:
                    err = e
            if data is None:
                failed += 1
                self._fail(f"{code} {d}", err)
                continue
            self.ok += 1
            out += [m for m in map(self._parse, data.get("events", [])) if m]
        return None if failed == len(asks) else out


class _Bulk(Provider):
    """One request per day returns every league; we then keep the events of the wanted league."""

    def __init__(self):
        super().__init__()
        self._cache = {}

    def _raw(self, d):
        raise NotImplementedError

    def code_of(self, raw):
        raise NotImplementedError

    def parse(self, raw):
        raise NotImplementedError

    def league(self, code, asks):
        if self.dead:
            return None
        out, failed = [], 0
        for d in asks:
            if d not in self._cache:
                try:
                    self._cache[d] = self._raw(d)
                    self.ok += 1
                except Exception as e:
                    self._cache[d] = None
                    self._fail(d, e)
            raw = self._cache[d]
            if raw is None:
                failed += 1
                continue
            for r in raw:
                try:
                    if self.code_of(r) == code:
                        m = self.parse(r)
                        if m:
                            out.append(m)
                except Exception:
                    continue
        return None if failed == len(asks) else out


def _iso_day(d):  # "20261014" -> "2026-10-14"
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


# (league code, category regex or None, tournament-name regex) - matched against Sofascore's names
SOFA_RULES = [
    ("uefa.champions", None, r"^UEFA Champions League$"),
    ("uefa.europa", None, r"^UEFA Europa League$"),
    ("eng.1", r"^England$", r"^Premier League$"),
    ("esp.1", r"^Spain$", r"^LaLiga$"),
    ("ita.1", r"^Italy$", r"^Serie A$"),
    ("ger.1", r"^Germany$", r"^Bundesliga$"),
    ("fra.1", r"^France$", r"^Ligue 1$"),
    ("egy.1", r"^Egypt$", r"Premier League"),
    ("ksa.1", r"^Saudi", r"(Pro League|Professional League)"),
    ("caf.champions", None, r"^CAF Champions League$"),
    ("caf.confed", None, r"^CAF Confederation Cup$"),
    ("eng.league_cup", r"^England$", r"(EFL Cup|League Cup|Carabao)"),
    ("eng.fa", r"^England$", r"^FA Cup$"),
    ("esp.copa_del_rey", r"^Spain$", r"Copa del Rey"),
    ("usa.1", r"^USA$", r"^MLS$"),
    ("fifa.world", None, r"^(FIFA )?World Cup$"),
    ("uefa.nations", None, r"^UEFA Nations League$"),
    ("fifa.worldq.caf", None, r"(World Cup|WC).*Qual.*(CAF|Africa)"),
    ("fifa.worldq.uefa", None, r"(World Cup|WC).*Qual.*(UEFA|Europe)"),
    ("fifa.friendly", None, r"^(Friendly International|International Friendl)"),
    ("tur.1", r"^(Turkey|T[uü]rkiye)$", r"S[uü]per Lig"),
    ("por.1", r"^Portugal$", r"(Liga Portugal|Primeira Liga)"),
    ("ned.1", r"^Netherlands$", r"^Eredivisie$"),
    ("caf.nations", None, r"^Africa Cup of Nations$"),
]


class Sofascore(_Bulk):
    name = "sofascore"
    URL = "https://api.sofascore.com/api/v1/sport/football/scheduled-events/{d}"
    STATES = {"finished": "post", "canceled": "post", "cancelled": "post", "postponed": "post",
              "inprogress": "in", "notstarted": "pre"}

    def _raw(self, d):
        return _http_get(self.URL.format(d=_iso_day(d)), headers={"Referer": "https://www.sofascore.com/"}).get("events", [])

    def code_of(self, ev):
        t = ev.get("tournament") or {}
        name = (t.get("uniqueTournament") or {}).get("name") or t.get("name") or ""
        cat = (t.get("category") or {}).get("name") or ""
        for code, cre, nre in SOFA_RULES:
            if (cre is None or re.search(cre, cat, re.I)) and re.search(nre, name, re.I):
                return code
        return None

    def parse(self, ev):
        when = dt.datetime.fromtimestamp(ev["startTimestamp"], dt.timezone.utc).astimezone(CAIRO)
        st = ((ev.get("status") or {}).get("type") or "notstarted").lower()
        return {"id": f"sofa{ev['id']}", "home": ev["homeTeam"]["name"], "away": ev["awayTeam"]["name"],
                "kickoff": when, "state": self.STATES.get(st, "pre")}


FD_CODES = {"PL": "eng.1", "PD": "esp.1", "SA": "ita.1", "BL1": "ger.1", "FL1": "fra.1", "CL": "uefa.champions", "WC": "fifa.world"}


class FootballData(_Bulk):
    name = "footballdata"
    URL = "https://api.football-data.org/v4/matches"

    def available(self):
        return bool(os.environ.get("FOOTBALL_DATA_KEY", "").strip())

    def _raw(self, d):
        a = dt.datetime.strptime(d, "%Y%m%d").date()
        data = _http_get(self.URL, {"dateFrom": a.isoformat(), "dateTo": (a + dt.timedelta(days=1)).isoformat()},
                         headers={"X-Auth-Token": os.environ["FOOTBALL_DATA_KEY"].strip()})
        return data.get("matches", [])

    def code_of(self, m):
        return FD_CODES.get((m.get("competition") or {}).get("code"))

    def parse(self, m):
        when = dt.datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(CAIRO)
        st = (m.get("status") or "").upper()
        state = "post" if st in ("FINISHED", "AWARDED", "CANCELLED", "POSTPONED", "SUSPENDED") else "in" if st in ("IN_PLAY", "PAUSED") else "pre"
        h, a = m["homeTeam"], m["awayTeam"]
        return {"id": f"fd{m['id']}", "home": h.get("name") or h.get("shortName"), "away": a.get("name") or a.get("shortName"),
                "kickoff": when, "state": state}


TSDB_RULES = [
    ("uefa.champions", r"^UEFA Champions League$"),
    ("uefa.europa", r"^UEFA Europa League$"),
    ("eng.1", r"^English Premier League$"),
    ("esp.1", r"^Spanish La Liga$"),
    ("ita.1", r"^Italian Serie A$"),
    ("ger.1", r"^German Bundesliga$"),
    ("fra.1", r"^French Ligue 1$"),
    ("egy.1", r"egypt.*premier|egyptian.*(premier|league)"),
    ("ksa.1", r"saudi.*(pro|league)"),
    ("caf.champions", r"^CAF Champions League$"),
    ("caf.confed", r"^CAF Confederation Cup$"),
    ("eng.league_cup", r"(English League Cup|EFL Cup|Carabao)"),
    ("eng.fa", r"^(English )?FA Cup$"),
    ("esp.copa_del_rey", r"Copa del Rey"),
    ("usa.1", r"(Major League Soccer|^MLS$)"),
    ("fifa.world", r"^FIFA World Cup$"),
    ("uefa.nations", r"^UEFA Nations League$"),
    ("fifa.friendly", r"^International Friendlies$"),
    ("tur.1", r"Turkish.*(Super|S[uü]per)"),
    ("por.1", r"Portuguese.*(Primeira|Liga)"),
    ("ned.1", r"Eredivisie"),
    ("caf.nations", r"Africa Cup of Nations"),
]


class TheSportsDB(_Bulk):
    name = "thesportsdb"
    URL = "https://www.thesportsdb.com/api/v1/json/{key}/eventsday.php"

    def _raw(self, d):
        key = os.environ.get("THESPORTSDB_KEY", "").strip() or "3"
        return _http_get(self.URL.format(key=key), {"d": _iso_day(d), "s": "Soccer"}).get("events") or []

    def code_of(self, ev):
        lg = ev.get("strLeague") or ""
        for code, rx in TSDB_RULES:
            if re.search(rx, lg, re.I):
                return code
        return None

    def parse(self, ev):
        ts = ev.get("strTimestamp")
        if not ts and ev.get("dateEvent"):
            ts = f"{ev['dateEvent']}T{(ev.get('strTime') or '00:00:00')[:8]}"
        when = dt.datetime.fromisoformat(ts.replace("Z", "")).replace(tzinfo=dt.timezone.utc).astimezone(CAIRO)  # times are UTC
        st = (ev.get("strStatus") or "").lower()
        done = any(x in st for x in ("finished", "ft", "aet", "pen", "postponed", "cancel", "abandon"))
        return {"id": f"tsdb{ev.get('idEvent')}", "home": ev["strHomeTeam"], "away": ev["strAwayTeam"],
                "kickoff": when, "state": "post" if done else "pre"}


PROVIDERS = {"espn": ESPN, "sofascore": Sofascore, "footballdata": FootballData, "thesportsdb": TheSportsDB}


def build_providers(names=None):
    out = []
    for n in names or DEFAULT_SOURCES:
        cls = PROVIDERS.get(n)
        if not cls:
            print(f"[fixtures] unknown source '{n}' in config - ignored")
            continue
        p = cls()
        if p.available():
            out.append(p)
        else:
            print(f"[fixtures] source '{n}' skipped (no key set)")
    return out


# ------------------------------------------------------------------ selection
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


def todays_matches(leagues, day, max_matches=30, providers=None, verbose=True, after=None, sources=None):
    """Matches of the Cairo day `day`, filtered per league by the config rules, in config order."""
    providers = providers if providers is not None else build_providers(sources)
    if not providers:
        print("[fixtures] no usable source configured")
        return []
    seen, out = set(), []
    # sources bucket by (US/UTC) calendar day, so ask for the day before as well and filter by Cairo date ourselves
    asks = [(day - dt.timedelta(days=1)).strftime("%Y%m%d"), day.strftime("%Y%m%d")]
    for prio, lg in enumerate(leagues):
        found, used = [], None
        for p in providers:
            res = p.league(lg["code"], asks)
            if not res:
                continue
            cand = []
            for m in res:
                if m["id"] in seen or m["kickoff"].date() != day:
                    continue
                if m["state"] == "post" or (after and m["kickoff"] < after - dt.timedelta(hours=2)):
                    continue  # already finished: not part of a "today's matches" post
                cand.append(m)
            if cand:
                found, used = cand, p
                break
        for m in found:
            seen.add(m["id"])
            m["competition"], m["prio"] = lg["name"], prio
        picked = select_for_league(found, lg)
        if used:
            used.served[lg["code"]] = len(found)
        if verbose and found:
            pid = {m["id"] for m in picked}
            print(f"[fixtures] {lg['code']}: {len(found)} found via {used.name}, {len(picked)} selected")
            for m in sorted(found, key=lambda m: m["kickoff"]):
                print(f"    {'+' if m['id'] in pid else '-'} {m['kickoff']:%H:%M}  {m['home']} vs {m['away']}")
        out.extend(picked)
    if verbose:
        for p in providers:
            print(f"[fixtures] source {p.name}: {p.ok} request(s) ok, {p.errors} failed, served {len(p.served)} league(s) {sorted(p.served)}")
    out.sort(key=lambda m: (m["prio"], m["kickoff"]))
    return out[:max_matches]


def group_for_card(matches, ar_names, time_fmt, hot_names=None):
    """-> [(competition_ar, [(home_ar, away_ar, time_str, hot), ...]), ...] in display order.
    hot = the match involves one of `hot_names` (big-crowd clubs): the card marks those rows in gold."""
    groups, idx = [], {}
    for m in matches:
        if m["competition"] not in idx:
            idx[m["competition"]] = []
            groups.append((m["competition"], idx[m["competition"]]))
        hot = bool(hot_names) and any(_involves(m, hot_names))
        idx[m["competition"]].append((ar_names.get(m["home"], m["home"]), ar_names.get(m["away"], m["away"]), time_fmt(m["kickoff"]), hot))
    return groups


def team_names(matches):
    return sorted({n for m in matches for n in (m["home"], m["away"]) if n})


def split_pages(groups, rows_per_card=11, max_cards=3, comp_weight=1.15):
    """Spread the matches over as few, evenly filled cards as possible.
    A card holds about `rows_per_card` units: one match = 1 unit, a competition heading = `comp_weight` units
    (a long competition may continue, with its heading repeated, on the next card).
    The target per card grows until everything fits in `max_cards`, so the last card is never the overloaded one."""
    total = sum(comp_weight + len(ms) for _, ms in groups)
    if total == 0:
        return []
    k0 = max(1, -(-int(total * 100) // int(rows_per_card * 100)))
    k = min(max_cards, k0)

    def fill(per):
        pages, cur, w = [], [], 0.0
        for comp, ms in groups:
            for m in ms:
                need = (comp_weight if not cur or cur[-1][0] != comp else 0.0) + 1
                if cur and w + need > per + 0.01:
                    pages.append(cur)
                    cur, w = [], 0.0
                    need = comp_weight + 1
                if cur and cur[-1][0] == comp:
                    cur[-1][1].append(m)
                else:
                    cur.append((comp, [m]))
                w += need
        if cur:
            pages.append(cur)
        return pages

    per = total / k
    pages = fill(per)
    while len(pages) > k:  # headings repeated on a continued competition add weight: raise the target a little
        per += 0.5
        pages = fill(per)
    return pages
