"""Member metadata: party, state, district, chamber, gender and birth date for every filer.

Data comes from the ``unitedstates/congress-legislators`` project (public domain), which
lists every member with their bioguide ID, biography and each term served. It is
downloaded once a week into ``cache/legislators.json`` (only members serving since 2012).

``enrich()`` runs after normalization, on the trade dicts of every source:

* **House trades** already carry a bioguide ID (Capitol API). Their party, state and
  district are replaced with the ones *on the trade date*, so a member who switched party
  is counted with the party they had when they traded. Capitol API reports today's party.
* **Senate trades** carry only the filer's name as eFD prints it ("Thomas H" "Tuberville",
  office "Tuberville, Tommy (Senator)"). The name is matched to a senator who was serving
  within a year of the trade; ``ALIASES`` fixes the few names the rules can't match.

Trades that can't be matched keep whatever the source reported and are listed in
``meta.json`` under ``members.unresolved``.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import requests

BASE_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/"
FILES = ("legislators-current.json", "legislators-historical.json")
MAX_AGE_DAYS = 7
KEEP_SINCE = "2012-01-01"  # STOCK Act; older members are dropped from the cache

PARTY_CODES = {"Democrat": "D", "Republican": "R", "Independent": "I", "Libertarian": "L"}
CHAMBER_TERM = {"house": "rep", "senate": "sen"}

#: eFD name (normalized "first last") -> bioguide ID, for names the matching rules miss.
ALIASES: dict[str, str] = {}

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|md|phd)\b\.?", re.I)
_NON_ALPHA = re.compile(r"[^a-z\s]")


def norm_name(text: str | None) -> str:
    """'Luján, Jr.' -> 'lujan'. Lowercase ASCII letters and single spaces only."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    t = t.replace("-", " ").replace("'", "")
    t = _SUFFIX.sub(" ", t)
    t = _NON_ALPHA.sub(" ", t)
    return " ".join(t.split())


# -- loading ----------------------------------------------------------------------


def _compact(person: dict) -> dict:
    """Keep only what enrichment needs."""
    terms = []
    for t in person.get("terms", []):
        if t.get("end", "") < KEEP_SINCE:
            continue
        term = {k: t[k] for k in ("type", "start", "end", "state", "district", "party") if k in t}
        if t.get("party_affiliations"):
            term["parties"] = [{k: a[k] for k in ("start", "end", "party") if k in a}
                               for a in t["party_affiliations"]]
        terms.append(term)
    return {"id": person["id"]["bioguide"], "name": person.get("name", {}),
            "bio": person.get("bio", {}), "terms": terms}


def download_legislators(session: requests.Session | None = None, base_url: str = BASE_URL) -> list[dict]:
    session = session or requests.Session()
    people: list[dict] = []
    for name in FILES:
        resp = session.get(base_url + name, timeout=120)
        resp.raise_for_status()
        people.extend(resp.json())
    keep = [_compact(p) for p in people if p.get("id", {}).get("bioguide")]
    return [p for p in keep if p["terms"]]


def load_legislators(cache_path: Path, *, session=None, max_age_days: int = MAX_AGE_DAYS,
                     refresh: bool = False, log=lambda m: None) -> list[dict] | None:
    """Cached legislators list; refreshed when older than ``max_age_days``.

    Falls back to a stale cache if the download fails, and returns None if there is
    neither (enrichment is then skipped).
    """
    cache_path = Path(cache_path)
    fresh = cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < max_age_days * 86400
    if fresh and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    try:
        people = download_legislators(session)
    except (requests.RequestException, ValueError) as err:
        if cache_path.exists():
            log(f"members: download failed ({err}); using the cached list")
            return json.loads(cache_path.read_text(encoding="utf-8"))
        log(f"members: download failed ({err}); member metadata not added")
        return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(people, separators=(",", ":")), encoding="utf-8")
    return people


# -- lookups ----------------------------------------------------------------------


def _shift(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


class Directory:
    def __init__(self, people: list[dict]):
        self.by_id = {p["id"]: p for p in people}
        self.senators_by_last: dict[str, list[dict]] = defaultdict(list)
        for p in people:
            if not any(t["type"] == "sen" for t in p["terms"]):
                continue
            for key in self._last_keys(p["name"].get("last")):
                self.senators_by_last[key].append(p)

    @staticmethod
    def _last_keys(last: str | None) -> set[str]:
        n = norm_name(last)
        if not n:
            return set()
        tokens = n.split()
        return {n, tokens[-1], "".join(tokens)}

    @staticmethod
    def first_names(person: dict) -> set[str]:
        nm = person["name"]
        out = set()
        for key in ("first", "nickname", "middle"):
            for tok in norm_name(nm.get(key)).split():
                out.add(tok)
        full = norm_name(nm.get("official_full"))
        if full:
            out.add(full.split()[0])
        return out

    def term_on(self, person: dict, on: str, chamber: str | None = None) -> dict | None:
        """The term covering ``on`` (or the closest one) in the given chamber."""
        terms = [t for t in person["terms"] if not chamber or t["type"] == CHAMBER_TERM[chamber]]
        if not terms:
            terms = person["terms"]
        if not terms:
            return None
        for t in terms:
            if t["start"] <= on <= t["end"]:
                return t
        def distance(t):
            return 0 if t["start"] <= on <= t["end"] else min(abs((date.fromisoformat(on) - date.fromisoformat(x)).days)
                                                            for x in (t["start"], t["end"]))
        return min(terms, key=distance)

    @staticmethod
    def party_on(term: dict, on: str) -> str | None:
        for a in term.get("parties", []):
            if a["start"] <= on <= a["end"]:
                return a["party"]
        return term.get("party")

    def resolve_senator(self, first: str, last: str, office: str, on: str) -> dict | None:
        """Match an eFD filer name to a senator serving within a year of ``on``."""
        alias = ALIASES.get(f"{norm_name(first)} {norm_name(last)}".strip())
        if alias and alias in self.by_id:
            return self.by_id[alias]

        office_last, office_first = "", ""
        m = re.match(r"\s*([^,(]+),\s*([^(]+)", office or "")
        if m:
            office_last, office_first = m.group(1), m.group(2)

        lo, hi = _shift(on, -366), _shift(on, 366)
        candidates: dict[str, dict] = {}
        for last_name in (last, office_last):
            n = norm_name(last_name)
            for key in {n, n.split()[-1] if n else ""} - {""}:
                for p in self.senators_by_last.get(key, []):
                    if any(t["type"] == "sen" and t["start"] <= hi and t["end"] >= lo for t in p["terms"]):
                        candidates[p["id"]] = p
        if len(candidates) <= 1:
            return next(iter(candidates.values()), None)

        given = set(norm_name(first).split()) | set(norm_name(office_first).split())
        by_name = [p for p in candidates.values() if self.first_names(p) & given]
        if len(by_name) == 1:
            return by_name[0]
        initials = {g[0] for g in given if g}
        by_initial = [p for p in candidates.values() if {f[0] for f in self.first_names(p)} & initials]
        return by_initial[0] if len(by_initial) == 1 else None


# -- enrichment -------------------------------------------------------------------


@dataclass
class EnrichReport:
    trades: int = 0
    resolved: int = 0
    unresolved: Counter = field(default_factory=Counter)  # "chamber: name" -> trades

    def to_meta(self) -> dict:
        return {
            "source": "unitedstates/congress-legislators",
            "trades": self.trades,
            "resolved": self.resolved,
            "unresolved": dict(self.unresolved.most_common()),
        }


def enrich(trades: list[dict], directory: Directory, *, extras: dict[str, dict] | None = None) -> EnrichReport:
    """Fill in member metadata in place. ``extras`` maps trade id -> eFD name parts."""
    report = EnrichReport(trades=len(trades))
    extras = extras or {}
    cache: dict[tuple, dict | None] = {}
    for t in trades:
        on = t["txDate"]
        person = directory.by_id.get(t.get("memberId") or "")
        if person is None and t["chamber"] == "senate":
            x = extras.get(t["id"], {})
            first = x.get("efdFirst", "")
            last = x.get("efdLast", t["member"].split()[-1] if t["member"] else "")
            key = (first, last, x.get("office", ""), on[:7])
            if key not in cache:
                cache[key] = directory.resolve_senator(first, last, x.get("office", ""), on)
            person = cache[key]
        if person is None:
            report.unresolved[f"{t['chamber']}: {t['member']}"] += 1
            continue
        report.resolved += 1
        term = directory.term_on(person, on, t["chamber"])
        party = directory.party_on(term, on) if term else None
        bio = person.get("bio", {})
        t["memberId"] = person["id"]
        t["member"] = person["name"].get("official_full") or t["member"]
        if party:
            t["party"] = PARTY_CODES.get(party, party[:1].upper())
        if term:
            t["state"] = term.get("state") or t.get("state")
            t["district"] = term.get("district") if t["chamber"] == "house" else None
        t["gender"] = bio.get("gender") or t.get("gender")
        t["birthDate"] = bio.get("birthday") or t.get("birthDate")
    return report
