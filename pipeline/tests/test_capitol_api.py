"""Capitol API adapter, using recorded records from the server's cache/trades.json."""
import json
from pathlib import Path

import pytest
import requests

from normalize import normalize
from sources.capitol_api import CapitolApiSource, extract_trades, to_raw_trade

FIXTURE = Path(__file__).parent / "fixtures" / "capitol_api_trades.json"


@pytest.fixture
def records():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["trades"]


def test_maps_fields(records):
    rec = next(r for r in records if r["id"] == "20030608-1")
    t = to_raw_trade(rec)
    assert t.source == "capitol-api" and t.source_id == "20030608-1" and t.filing_id == "20030608"
    assert (t.ticker, t.asset_type_code, t.tx_code, t.owner_code) == ("INTU", "ST", "P", "SP")
    assert t.tx_date == "2025-06-20" and t.disclosure_date == "2025-07-15"
    assert t.member.member_id == rec["person"]["bioguideId"] and t.member.chamber == "house"
    assert t.source_url.endswith("20030608.pdf")


def test_dc_owner_is_marked_ambiguous(records):
    rec = next(r for r in records if r["owner"] == "DC")
    assert to_raw_trade(rec).owner_code == "SELF_OR_DC"


def test_fixture_normalizes_as_expected(records):
    result = normalize(CapitolApiSource(file=FIXTURE).fetch())
    by_id = {t["id"]: t for t in result.trades}

    assert set(by_id) == {
        "house-20030608-1",   # spouse buy
        "house-20026724-1",   # joint partial sale
        "house-20032062-1",   # full sale
        "house-20032149-22",  # BRK.B stock
        "house-20026537-1",   # truncated "$15,001"
        "house-20030275-9",   # amended copy of 20030155-1 (later filing)
    }
    assert by_id["house-20026724-1"]["type"] == "sell"
    assert by_id["house-20032062-1"]["type"] == "sell_full"
    assert by_id["house-20032149-22"]["ticker"] == "BRK-B"
    assert by_id["house-20026537-1"]["amountHigh"] == 50000
    assert by_id["house-20026537-1"]["flags"] == ["amount_bracket_inferred"]
    assert by_id["house-20032062-1"]["party"] == "R"

    assert result.excluded_by_reason() == {
        "exchange": 1,
        "asset_type:GS": 1,
        "asset_type:OP": 1,      # BRK.B option, not the stock
        "asset_type:HN": 1,
        "no_ticker": 1,
        "tx_after_disclosure": 1,
        "duplicate_filing": 1,
    }


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeSession:
    """Serves ``records`` the way GET /api/trades does with limit/offset."""

    def __init__(self, records, fail_first=0):
        self.records, self.calls, self.fail_first = records, [], fail_first

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        if self.fail_first:
            self.fail_first -= 1
            raise requests.ConnectionError("boom")
        off, lim = params["offset"], params["limit"]
        page = self.records[off: off + lim]
        return FakeResponse({"total": len(self.records), "offset": off, "limit": lim,
                             "returned": len(page), "trades": page})


def test_download_pages_and_caches(records, tmp_path):
    session = FakeSession(records)
    cache = tmp_path / "raw.json"
    src = CapitolApiSource("http://api.test/", session=session, page_size=5, cache_path=cache)
    got = list(src.fetch(since="2018-01-01"))

    assert len(got) == len(records)
    assert [c[1]["offset"] for c in session.calls] == [0, 5, 10]
    assert all(c[0] == "http://api.test/api/trades" for c in session.calls)
    assert session.calls[0][1]["from"] == "2018-01-01" and session.calls[0][1]["sort"] == "oldest"
    assert len(json.loads(cache.read_text())["trades"]) == len(records)


def test_download_retries(records, monkeypatch):
    monkeypatch.setattr("sources.capitol_api.time.sleep", lambda s: None)
    session = FakeSession(records, fail_first=2)
    assert len(list(CapitolApiSource(session=session, page_size=100).fetch())) == len(records)


def test_download_gives_helpful_error(monkeypatch):
    monkeypatch.setattr("sources.capitol_api.time.sleep", lambda s: None)
    session = FakeSession([], fail_first=99)
    with pytest.raises(RuntimeError, match="Is Capitol API running"):
        list(CapitolApiSource(session=session).fetch())


def test_extract_trades_shapes(records):
    assert extract_trades(records) == records
    assert extract_trades({"trades": records}) == records
    with pytest.raises(ValueError):
        extract_trades({"nope": 1})
