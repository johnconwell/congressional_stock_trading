"""Senate eFD adapter (official Senate Electronic Financial Disclosures, efdsearch.senate.gov).

How the site works:

1. ``GET /search/home/`` shows a usage agreement. Posting it back with
   ``prohibition_agreement=1`` and the form's CSRF token starts a session.
2. ``POST /search/report/data/`` is the DataTables endpoint behind the search page. With
   ``report_types=[11]`` it lists Periodic Transaction Reports, 100 at a time. Each row is
   ``[first name, last name, office, '<a href="/search/view/ptr/<uuid>/">title</a>', 'MM/DD/YYYY']``,
   where the date is when the Senate received the report (it became public then).
3. ``GET /search/view/ptr/<uuid>/`` is the electronic report: one HTML table with
   ``# | Transaction Date | Owner | Ticker | Asset Name | Asset Type | Type | Amount | Comment``.
   Paper filings (``/search/view/paper/...``) are scanned images; they are counted and skipped.

A filed report never changes (an amendment is a new report with its own uuid), so each
report page is cached on disk and only new reports are downloaded on later runs. The list
of reports is saved too, so ``offline=True`` can rebuild the trades with no network.

Like every adapter this one copies the source's wording as it is ("Purchase", "Spouse",
"Stock"); ``normalize.py`` maps it. The filer is identified only by name here; ``members.py``
matches the name to a bioguide ID and adds party, state, gender and birth date.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import requests

from .base import Member, RawTrade, TradeSource

ROOT = "https://efdsearch.senate.gov"
LANDING_URL = f"{ROOT}/search/home/"
SEARCH_URL = f"{ROOT}/search/"
REPORTS_URL = f"{ROOT}/search/report/data/"
PTR_REPORT_TYPE = 11
PAGE_SIZE = 100
USER_AGENT = "congressional-stock-tracker/0.5 (non-commercial research; github.com)"

_HREF = re.compile(r'href="([^"]+)"')
_UUID = re.compile(r"/search/view/(ptr|paper)/([0-9a-fA-F-]{8,})/?")
_CSRF_INPUT = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"|value="([^"]+)"\s+name="csrfmiddlewaretoken"')
_WS = re.compile(r"\s+")


# -- HTML parsing (stdlib only) ---------------------------------------------------


class _TableParser(HTMLParser):
    """Collects every <table> as a header row plus body rows of cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._table: dict | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_head = False
        self._row_has_th = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = {"head": [], "rows": []}
        elif self._table is None:
            return
        elif tag == "thead":
            self._in_head = True
        elif tag == "tr":
            self._row = []
            self._row_has_th = False
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._row_has_th = self._row_has_th or tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if self._table is None:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_WS.sub(" ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._in_head or (not self._table["head"] and not self._table["rows"] and self._row_has_th):
                self._table["head"] = self._row
            elif self._row:
                self._table["rows"].append(self._row)
            self._row = None
        elif tag == "thead":
            self._in_head = False
        elif tag == "table":
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


_COLUMNS = {
    "#": "n",
    "transaction date": "tx_date",
    "owner": "owner",
    "ticker": "ticker",
    "asset name": "asset_name",
    "asset type": "asset_type",
    "type": "type",
    "amount": "amount",
    "comment": "comment",
}
_POSITIONAL = ["n", "tx_date", "owner", "ticker", "asset_name", "asset_type", "type", "amount", "comment"]


def parse_ptr_html(html: str) -> list[dict]:
    """Return the transaction rows of an electronic PTR page as dicts of cell text."""
    parser = _TableParser()
    parser.feed(html)
    for table in parser.tables:
        head = [_COLUMNS.get(h.strip().lower()) for h in table["head"]]
        if "tx_date" not in head and "ticker" not in head:
            if not table["head"] and table["rows"] and len(table["rows"][0]) >= 8:
                head = _POSITIONAL  # no header row: assume the standard column order
            else:
                continue
        rows = []
        for cells in table["rows"]:
            row = {key: cells[i] for i, key in enumerate(head) if key and i < len(cells)}
            if row.get("tx_date") or row.get("ticker"):
                rows.append(row)
        return rows
    return []


def _us_date(text: str | None) -> str | None:
    """'01/15/2025' -> '2025-01-15'; anything else -> None."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip()[:10], "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _blank(text: str | None) -> str | None:
    t = (text or "").strip()
    return None if t in ("", "--", "-", "N/A") else t


def parse_report_row(row: list) -> dict | None:
    """One search result row -> {first, last, office, kind, uuid, url, title, received}."""
    if not isinstance(row, list) or len(row) < 5:
        return None
    first, last, office, link_html, received = row[:5]
    href_m = _HREF.search(link_html or "")
    href = href_m.group(1) if href_m else ""
    m = _UUID.search(href)
    if not m:
        return None
    title = _WS.sub(" ", re.sub(r"<[^>]+>", " ", link_html or "")).strip()
    return {
        "first": (first or "").strip(),
        "last": (last or "").strip(),
        "office": (office or "").strip(),
        "kind": m.group(1),
        "uuid": m.group(2).lower(),
        "url": ROOT + href if href.startswith("/") else href,
        "title": title,
        "received": _us_date(received),
    }


def ptr_rows_to_raw(report: dict, rows: list[dict]) -> list[RawTrade]:
    member = Member(name=f"{report['first']} {report['last']}".strip() or "Unknown", chamber="senate")
    out = []
    for i, r in enumerate(rows, start=1):
        n = (r.get("n") or "").strip() or str(i)
        out.append(RawTrade(
            source=SenateEfdSource.name,
            source_id=f"{report['uuid']}-{n}",
            filing_id=report["uuid"],
            member=member,
            owner_code=_blank(r.get("owner")),
            asset_name=_blank(r.get("asset_name")),
            asset_type_code=_blank(r.get("asset_type")),
            ticker=_blank(r.get("ticker")),
            tx_code=_blank(r.get("type")),
            tx_date=_us_date(r.get("tx_date")),
            disclosure_date=report["received"],
            amount_text=_blank(r.get("amount")),
            source_url=report["url"],
            extra={"efdFirst": report["first"], "efdLast": report["last"], "office": report["office"],
                   "reportTitle": report["title"], "comment": _blank(r.get("comment"))},
        ))
    return out


# -- the adapter --------------------------------------------------------------------


class SenateEfdSource(TradeSource):
    name = "senate-efd"

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        session: requests.Session | None = None,
        offline: bool = False,
        delay: float = 1.0,
        retries: int = 3,
        page_size: int = PAGE_SIZE,
        max_reports: int | None = None,
        log=None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.offline = offline
        self.delay = delay
        self.retries = retries
        self.page_size = page_size
        self.max_reports = max_reports
        self.log = log or (lambda msg: None)
        self._csrf: str | None = None
        self._last_request = 0.0
        self.stats: Counter[str] = Counter()

    # -- public -----------------------------------------------------------------

    def fetch(self, since: str | None = None) -> Iterable[RawTrade]:
        reports = self._load_index() if self.offline else self._search(since)
        if since:
            reports = [r for r in reports if not r["received"] or r["received"] >= since]
        self.stats["reports"] = len(reports)
        downloads = 0
        trades: list[RawTrade] = []
        for rep in reports:
            if rep["kind"] != "ptr":
                self.stats["paper_reports_skipped"] += 1
                continue
            html = self._cached_page(rep["uuid"])
            if html is None:
                if self.offline:
                    self.stats["reports_not_cached"] += 1
                    continue
                if self.max_reports is not None and downloads >= self.max_reports:
                    self.stats["reports_deferred"] += 1
                    continue
                html = self._get_report(rep["url"])
                downloads += 1
                self._save_page(rep["uuid"], html)
                if downloads % 50 == 0:
                    self.log(f"senate-efd: downloaded {downloads} new reports…")
            rows = parse_ptr_html(html)
            if not rows:
                self.stats["empty_reports"] += 1
            trades.extend(ptr_rows_to_raw(rep, rows))
        self.stats["reports_downloaded"] = downloads
        self.stats["transactions"] = len(trades)
        return trades

    # -- network ----------------------------------------------------------------

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.session.request(method, url, timeout=60, **kw)
                if resp.status_code in (429, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                return resp
            except requests.RequestException as err:
                last_err = err
                if attempt + 1 < self.retries:
                    time.sleep(2 ** (attempt + 1))
        raise RuntimeError(
            f"Senate eFD request failed: {url} ({last_err}). The site may be down or blocking "
            "automated requests; try again later, or use --senate-offline to rebuild from the cache."
        ) from last_err

    def _accept_agreement(self) -> str:
        landing = self._request("GET", LANDING_URL)
        m = _CSRF_INPUT.search(landing.text)
        if not m:
            raise RuntimeError("Senate eFD: no CSRF token on the agreement page (site layout changed?)")
        form_token = m.group(1) or m.group(2)
        self._request("POST", LANDING_URL, data={"csrfmiddlewaretoken": form_token, "prohibition_agreement": "1"},
                      headers={"Referer": LANDING_URL})
        cookies = self.session.cookies
        self._csrf = cookies.get("csrftoken") or cookies.get("csrf") or form_token
        return self._csrf

    def _search(self, since: str | None) -> list[dict]:
        self._accept_agreement()
        start_date = "01/01/2012 00:00:00"
        if since:
            y, m, d = since.split("-")
            start_date = f"{m}/{d}/{y} 00:00:00"
        reports: dict[str, dict] = {}
        offset = 0
        while True:
            body = self._search_page(offset, start_date)
            rows = body.get("data") or []
            for row in rows:
                rep = parse_report_row(row)
                if rep is None:
                    self.stats["unparsed_search_rows"] += 1
                    continue
                if "candidate" in rep["office"].lower():
                    self.stats["candidate_reports_skipped"] += 1
                    continue
                reports[rep["uuid"]] = rep
            total = body.get("recordsFiltered", body.get("recordsTotal"))
            offset += len(rows)
            if not rows or len(rows) < self.page_size or (total is not None and offset >= int(total)):
                break
        out = sorted(reports.values(), key=lambda r: (r["received"] or "", r["uuid"]))
        self._save_index(out)
        self.log(f"senate-efd: {len(out)} reports listed since {since or '2012'}")
        return out

    def _search_page(self, offset: int, start_date: str) -> dict:
        data = {
            "start": str(offset),
            "length": str(self.page_size),
            "report_types": f"[{PTR_REPORT_TYPE}]",
            "filer_types": "[]",
            "submitted_start_date": start_date,
            "submitted_end_date": "",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
            "csrfmiddlewaretoken": self._csrf,
        }
        headers = {"Referer": SEARCH_URL, "X-CSRFToken": self._csrf or ""}
        for attempt in range(2):
            resp = self._request("POST", REPORTS_URL, data=data, headers=headers)
            try:
                return resp.json()
            except ValueError:
                # The session expired and we were sent back to the agreement page.
                if attempt == 0:
                    self._accept_agreement()
                    data["csrfmiddlewaretoken"] = self._csrf
                    headers["X-CSRFToken"] = self._csrf or ""
        raise RuntimeError("Senate eFD: the report search did not return JSON")

    def _get_report(self, url: str) -> str:
        if self._csrf is None:
            self._accept_agreement()
        resp = self._request("GET", url, headers={"Referer": SEARCH_URL})
        if resp.url.rstrip("/").endswith("/search/home") or "prohibition_agreement" in resp.text:
            self._accept_agreement()
            resp = self._request("GET", url, headers={"Referer": SEARCH_URL})
        return resp.text

    # -- cache ------------------------------------------------------------------

    def _page_path(self, uuid: str) -> Path | None:
        return self.cache_dir / "ptr" / f"{uuid}.html" if self.cache_dir else None

    def _cached_page(self, uuid: str) -> str | None:
        p = self._page_path(uuid)
        return p.read_text(encoding="utf-8") if p and p.exists() else None

    def _save_page(self, uuid: str, html: str) -> None:
        p = self._page_path(uuid)
        # Only cache real report pages (never an agreement or error page).
        if p and (parse_ptr_html(html) or "<table" in html.lower()):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(html, encoding="utf-8")

    def _save_index(self, reports: list[dict]) -> None:
        if not self.cache_dir:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "reports.json").write_text(json.dumps(reports, indent=1), encoding="utf-8")

    def _load_index(self) -> list[dict]:
        p = self.cache_dir / "reports.json" if self.cache_dir else None
        if not p or not p.exists():
            raise RuntimeError("Senate eFD offline mode needs a cached report list "
                               f"({p or 'no cache folder'}); run once without --senate-offline first.")
        return json.loads(p.read_text(encoding="utf-8"))
