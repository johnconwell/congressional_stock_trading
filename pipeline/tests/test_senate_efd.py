import json
from pathlib import Path

import pytest
import requests

from normalize import normalize
from sources import senate_efd
from sources.senate_efd import (
    LANDING_URL, REPORTS_URL, ROOT, SenateEfdSource, parse_ptr_html, parse_report_row,
)

FIX = Path(__file__).parent / "fixtures" / "senate_efd"
TUB = "aaaaaaaa-1111-4111-8111-111111111111"
SCOTT = "cccccccc-3333-4333-8333-333333333333"


class FakeResponse:
    def __init__(self, text="", status=200, url="", json_body=None):
        self.text = text
        self.status_code = status
        self.url = url
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeEfd:
    """Plays the eFD site: agreement page, paged report search and PTR pages."""

    def __init__(self, *, expire_once=False):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = []
        self.agreed = False
        self.expire_once = expire_once

    def request(self, method, url, timeout=None, data=None, headers=None):
        self.calls.append((method, url, dict(data or {})))
        if url == LANDING_URL and method == "GET":
            return FakeResponse((FIX / "landing.html").read_text(), url=url)
        if url == LANDING_URL and method == "POST":
            assert data["csrfmiddlewaretoken"] == "FORMTOKEN123"
            assert data["prohibition_agreement"] == "1"
            self.agreed = True
            self.cookies.set("csrftoken", "COOKIETOKEN")
            return FakeResponse("ok", url=SEARCH_URL_HOME)
        if url == REPORTS_URL:
            assert data["report_types"] == "[11]"
            assert data["csrfmiddlewaretoken"] == "COOKIETOKEN"
            page = "search_page1.json" if data["start"] == "0" else "search_page2.json"
            return FakeResponse(json_body=json.loads((FIX / page).read_text()), url=url)
        if url.startswith(f"{ROOT}/search/view/ptr/"):
            if self.expire_once:
                self.expire_once = False
                return FakeResponse((FIX / "landing.html").read_text(), url=LANDING_URL)
            name = "ptr_tuberville.html" if TUB in url else "ptr_scott.html"
            return FakeResponse((FIX / name).read_text(), url=url)
        return FakeResponse("not found", status=404, url=url)


SEARCH_URL_HOME = f"{ROOT}/search/"


def make_source(tmp_path, session=None, **kw):
    return SenateEfdSource(cache_dir=tmp_path / "efd", session=session or FakeEfd(), delay=0, page_size=2, **kw)


def test_parse_ptr_html_reads_every_column():
    rows = parse_ptr_html((FIX / "ptr_tuberville.html").read_text())
    assert len(rows) == 6
    assert rows[0] == {"n": "1", "tx_date": "02/03/2025", "owner": "Self", "ticker": "NVDA",
                       "asset_name": "NVIDIA Corporation", "asset_type": "Stock", "type": "Purchase",
                       "amount": "$15,001 - $50,000", "comment": "--"}
    assert rows[2]["asset_name"].startswith("Apple Inc. Option Type: Call Strike price: $250")
    assert rows[4]["comment"] == "Sold & closed"


def test_parse_report_row():
    row = json.loads((FIX / "search_page1.json").read_text())["data"][0]
    rep = parse_report_row(row)
    assert rep == {"first": "Thomas H", "last": "Tuberville", "office": "Tuberville, Tommy (Senator)",
                   "kind": "ptr", "uuid": TUB, "url": f"{ROOT}/search/view/ptr/{TUB}/",
                   "title": "Periodic Transaction Report for 02/03/2025", "received": "2025-02-10"}
    assert parse_report_row(["a", "b", "c", "<a href='x'>no link</a>", "01/01/2025"]) is None


def test_fetch_pages_the_search_and_skips_paper_reports(tmp_path):
    session = FakeEfd()
    src = make_source(tmp_path, session)
    raws = list(src.fetch("2025-01-01"))
    assert len(raws) == 8  # 6 Tuberville rows + 2 Scott rows
    search_calls = [c for c in session.calls if c[1] == REPORTS_URL]
    assert [c[2]["start"] for c in search_calls] == ["0", "2"]
    assert search_calls[0][2]["submitted_start_date"] == "01/01/2025 00:00:00"
    assert src.stats["paper_reports_skipped"] == 1
    assert src.stats["reports_downloaded"] == 2

    nvda = raws[0]
    assert nvda.source == "senate-efd" and nvda.source_id == f"{TUB}-1" and nvda.filing_id == TUB
    assert nvda.member.chamber == "senate" and nvda.member.name == "Thomas H Tuberville"
    assert nvda.member.member_id is None
    assert (nvda.tx_date, nvda.disclosure_date) == ("2025-02-03", "2025-02-10")
    assert nvda.extra["office"] == "Tuberville, Tommy (Senator)"
    assert raws[3].ticker is None  # "--"


def test_reports_are_cached_and_offline_mode_rebuilds(tmp_path):
    make_source(tmp_path).fetch(None)
    assert (tmp_path / "efd" / "ptr" / f"{TUB}.html").exists()
    assert (tmp_path / "efd" / "reports.json").exists()

    # Second online run: only the search is requested, no report pages.
    session = FakeEfd()
    src = make_source(tmp_path, session)
    assert len(list(src.fetch(None))) == 8
    assert not [c for c in session.calls if "/view/ptr/" in c[1]]
    assert src.stats["reports_downloaded"] == 0

    # Offline: no requests at all.
    offline = make_source(tmp_path, FakeEfd(), offline=True)
    assert len(list(offline.fetch(None))) == 8
    assert offline.session.calls == []


def test_offline_without_cache_explains(tmp_path):
    with pytest.raises(RuntimeError, match="run once without --senate-offline"):
        list(make_source(tmp_path, offline=True).fetch(None))


def test_expired_session_is_renewed(tmp_path):
    session = FakeEfd(expire_once=True)
    raws = list(make_source(tmp_path, session).fetch(None))
    assert len(raws) == 8
    assert sum(1 for c in session.calls if c[0] == "POST" and c[1] == LANDING_URL) == 2


def test_max_reports_defers_the_rest(tmp_path):
    src = make_source(tmp_path, max_reports=1)
    assert len(list(src.fetch(None))) == 6
    assert src.stats["reports_deferred"] == 1


def test_server_errors_raise_a_clear_message(tmp_path, monkeypatch):
    monkeypatch.setattr(senate_efd.time, "sleep", lambda s: None)

    class Down(FakeEfd):
        def request(self, method, url, **kw):
            return FakeResponse("down", status=503, url=url)

    with pytest.raises(RuntimeError, match="Senate eFD request failed"):
        list(make_source(tmp_path, Down()).fetch(None))


def test_senate_rows_normalize():
    src = SenateEfdSource(cache_dir=None, session=FakeEfd(), delay=0, page_size=2)
    result = normalize(src.fetch(None))
    by_ticker = {t["ticker"]: t for t in result.trades}
    assert set(by_ticker) == {"NVDA", "BRK-B", "MSFT", "JPM"}
    nvda = by_ticker["NVDA"]
    assert (nvda["chamber"], nvda["owner"], nvda["type"], nvda["assetType"]) == ("senate", "self", "buy", "stock")
    assert nvda["id"] == f"senate-{TUB}-1"
    assert (nvda["amountLow"], nvda["amountHigh"]) == (15001, 50000)
    assert by_ticker["BRK-B"]["owner"] == "spouse" and by_ticker["BRK-B"]["type"] == "sell"
    assert by_ticker["MSFT"]["type"] == "sell_full"
    assert by_ticker["MSFT"]["flags"] == ["amount_open_ended"]
    assert by_ticker["JPM"]["type"] == "sell"  # plain "Sale"
    reasons = result.excluded_by_reason()
    assert reasons == {"asset_type:STOCK OPTION": 1, "asset_type:MUNICIPAL SECURITY": 1,
                       "exchange": 1, "asset_type:OTHER SECURITIES": 1}
