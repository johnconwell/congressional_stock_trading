"""Price step: Tiingo client, cache, split adjustment and the update loop (no network)."""
import json
from datetime import datetime, timezone

import pytest

import prices
from prices import (CacheEntry, PriceCache, RateLimited, TickerNotFound, TiingoClient,
                    price_file_stem, split_adjusted, update_prices)

NOW = datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc)


# -- fakes ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Serves Tiingo-style rows from a {ticker: [(date, close, split), ...]} table."""

    def __init__(self, table, *, limit_after=None, missing=(), fail=()):
        self.table, self.limit_after = table, limit_after
        self.missing, self.fail = set(missing), set(fail)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        ticker = url.split("/daily/")[1].split("/")[0].upper()
        self.calls.append((ticker, params["startDate"], params["endDate"]))
        assert headers["Authorization"] == "Token KEY"
        if self.limit_after is not None and len(self.calls) > self.limit_after:
            return FakeResponse(429, {"detail": "Error: You have run over your hourly request allocation."})
        if ticker in self.missing:
            return FakeResponse(404, {"detail": f"Error: Ticker '{ticker}' not found"})
        if ticker in self.fail:
            return FakeResponse(500, "server error")
        rows = [r for r in self.table.get(ticker, [])
                if params["startDate"] <= r[0] <= params["endDate"]]
        return FakeResponse(200, [
            {"date": f"{d}T00:00:00.000Z", "close": c, "adjClose": c, "splitFactor": s, "divCash": 0.0}
            for d, c, s in rows
        ])


def client(session):
    return TiingoClient("KEY", session=session, sleep=lambda s: None)


def trade(ticker, tx, lo=1001, hi=15000):
    return {"ticker": ticker, "txDate": tx, "amountLow": lo, "amountHigh": hi}


SPY = [("2026-10-26", 600.0, 1.0), ("2026-10-27", 601.0, 1.0), ("2026-10-28", 602.0, 1.0),
       ("2026-10-29", 603.0, 1.0), ("2026-10-30", 604.0, 1.0)]
NVDA = [("2026-10-26", 400.0, 1.0), ("2026-10-27", 404.0, 1.0),
        ("2026-10-28", 101.0, 4.0),  # 4-for-1 split takes effect
        ("2026-10-29", 102.0, 1.0), ("2026-10-30", 103.0, 1.0)]


def run(tmp_path, trades, session, **kw):
    return update_prices(trades, client=client(session) if session else None,
                         cache_dir=tmp_path / "cache", out_dir=tmp_path / "out", now=NOW, **kw)


def published(tmp_path, ticker):
    return json.loads((tmp_path / "out" / f"{price_file_stem(ticker)}.json").read_text())


# -- split adjustment and file names -------------------------------------------------


def test_split_adjusted_divides_closes_before_the_split():
    rows = [[d, c, c, s, 0.0] for d, c, s in NVDA]
    dates, closes = split_adjusted(rows)
    assert dates == [r[0] for r in NVDA]
    assert closes == [100.0, 101.0, 101.0, 102.0, 103.0]


def test_split_adjusted_handles_two_splits_and_missing_closes():
    rows = [["d1", 600.0, 0, 1.0, 0], ["d2", None, 0, 1.0, 0], ["d3", 300.0, 0, 2.0, 0],
            ["d4", 100.0, 0, 3.0, 0]]
    assert split_adjusted(rows) == (["d1", "d3", "d4"], [100.0, 100.0, 100.0])


def test_windows_reserved_names_get_a_suffix():
    assert price_file_stem("CON") == "CON_"
    assert price_file_stem("PRN") == "PRN_"
    assert price_file_stem("COM1") == "COM1_"
    assert price_file_stem("CONE") == "CONE"
    assert price_file_stem("BRK-B") == "BRK-B"


# -- client --------------------------------------------------------------------------


def test_client_classifies_errors():
    s = FakeSession({}, missing={"ZZZZ"})
    with pytest.raises(TickerNotFound):
        client(s).daily_prices("ZZZZ", "2026-01-01", "2026-01-02")
    with pytest.raises(RateLimited):
        client(FakeSession({}, limit_after=0)).daily_prices("SPY", "2026-01-01", "2026-01-02")


def test_client_treats_limit_message_with_200_as_rate_limited():
    class S(FakeSession):
        def get(self, *a, **k):
            return FakeResponse(200, {"detail": "You have run over your 500 symbol look up for this month."})
    with pytest.raises(RateLimited):
        client(S({})).daily_prices("SPY", "2026-01-01", "2026-01-02")


def test_client_retries_server_errors_then_gives_up():
    s = FakeSession({}, fail={"SPY"})
    with pytest.raises(RuntimeError, match="failed"):
        client(s).daily_prices("SPY", "2026-01-01", "2026-01-02")
    assert len(s.calls) == 3


def test_client_bad_key_is_fatal():
    class S(FakeSession):
        def get(self, *a, **k):
            return FakeResponse(401, {"detail": "Invalid token."})
    with pytest.raises(RuntimeError, match="rejected the API key"):
        client(S({})).daily_prices("SPY", "2026-01-01", "2026-01-02")


def test_client_throttles_between_requests():
    slept, t = [], [0.0]
    c = TiingoClient("KEY", session=FakeSession({"SPY": SPY}), min_interval=2.0,
                     sleep=lambda s: slept.append(s), clock=lambda: t[0])
    c.daily_prices("SPY", "2026-10-26", "2026-10-30")
    t[0] = 0.5
    c.daily_prices("SPY", "2026-10-26", "2026-10-30")
    assert slept == [1.5]


# -- update loop ---------------------------------------------------------------------


def test_first_run_fetches_and_publishes(tmp_path):
    s = FakeSession({"SPY": SPY, "NVDA": NVDA})
    report = run(tmp_path, [trade("NVDA", "2026-10-27")], s)

    assert [c[0] for c in s.calls] == ["SPY", "NVDA"]  # benchmark first
    assert ("NVDA", "2026-10-27", "2026-10-30") in s.calls  # from the ticker's first trade
    assert report.fetched == ["SPY", "NVDA"] and report.pending == []
    assert report.last_date == "2026-10-30"
    nvda = published(tmp_path, "NVDA")
    assert nvda == {"ticker": "NVDA", "dates": ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"],
                    "close": [101.0, 101.0, 102.0, 103.0]}


def test_second_run_is_incremental_and_respects_max_age(tmp_path):
    s = FakeSession({"SPY": SPY[:3]})
    run(tmp_path, [trade("SPY", "2026-10-26")], s)

    s2 = FakeSession({"SPY": SPY})
    report = run(tmp_path, [trade("SPY", "2026-10-26")], s2)  # same hour: skipped
    assert s2.calls == [] and report.up_to_date == ["SPY"]

    later = update_prices([trade("SPY", "2026-10-26")], client=client(s2), cache_dir=tmp_path / "cache",
                          out_dir=tmp_path / "out", now=NOW.replace(day=31))
    assert s2.calls == [("SPY", "2026-10-29", "2026-10-31")]
    assert later.files_written == 1
    assert published(tmp_path, "SPY")["dates"][-1] == "2026-10-30"


def test_new_split_readjusts_cached_history(tmp_path):
    s = FakeSession({"SPY": SPY, "NVDA": NVDA[:2]})
    run(tmp_path, [trade("NVDA", "2026-10-26")], s)
    assert published(tmp_path, "NVDA")["close"] == [400.0, 404.0]

    s2 = FakeSession({"SPY": SPY, "NVDA": NVDA})
    update_prices([trade("NVDA", "2026-10-26")], client=client(s2), cache_dir=tmp_path / "cache",
                  out_dir=tmp_path / "out", now=NOW.replace(day=31))
    assert ("NVDA", "2026-10-28", "2026-10-31") in s2.calls  # only new dates requested
    assert published(tmp_path, "NVDA")["close"] == [100.0, 101.0, 101.0, 102.0, 103.0]


def test_earlier_trade_triggers_backfill(tmp_path):
    run(tmp_path, [trade("NVDA", "2026-10-29")], FakeSession({"SPY": SPY, "NVDA": NVDA}))
    s2 = FakeSession({"SPY": SPY, "NVDA": NVDA})
    run(tmp_path, [trade("NVDA", "2026-10-26"), trade("NVDA", "2026-10-29")], s2)
    assert ("NVDA", "2026-10-26", "2026-10-30") in s2.calls
    assert len(published(tmp_path, "NVDA")["dates"]) == 5


def test_rate_limit_stops_cleanly_and_next_run_resumes(tmp_path):
    table = {"SPY": SPY, "AAA": SPY, "BBB": SPY, "CCC": SPY}
    trades = [trade("AAA", "2026-10-26", 1_000_001, 5_000_000), trade("BBB", "2026-10-26", 15_001, 50_000),
              trade("CCC", "2026-10-26")]
    s = FakeSession(table, limit_after=2)
    report = run(tmp_path, trades, s)
    assert report.fetched == ["SPY", "AAA"]  # biggest dollar volume after SPY
    assert report.pending == ["BBB", "CCC"]
    assert report.stopped.startswith("rate limited")
    assert len(s.calls) == 3  # stopped after the refusal, didn't keep hammering

    s2 = FakeSession(table)
    report2 = run(tmp_path, trades, s2)
    assert [c[0] for c in s2.calls] == ["BBB", "CCC"]
    assert report2.pending == [] and report2.files_total == 4


def test_max_requests_budget(tmp_path):
    s = FakeSession({"SPY": SPY, "AAA": SPY})
    report = run(tmp_path, [trade("AAA", "2026-10-26")], s, max_requests=1)
    assert report.fetched == ["SPY"] and report.pending == ["AAA"]
    assert report.stopped == "max_requests reached"


def test_unknown_ticker_is_cached_and_not_retried_soon(tmp_path):
    s = FakeSession({"SPY": SPY}, missing={"ZZZZ"})
    report = run(tmp_path, [trade("ZZZZ", "2026-10-26")], s)
    assert report.tickers_without_prices == ["ZZZZ"]
    assert report.without_prices == {"not_found": ["ZZZZ"]}

    s2 = FakeSession({"SPY": SPY}, missing={"ZZZZ"})
    later = update_prices([trade("ZZZZ", "2026-10-26")], client=client(s2), cache_dir=tmp_path / "cache",
                          out_dir=tmp_path / "out", now=NOW.replace(day=31))
    assert [c[0] for c in s2.calls] == ["SPY"]  # ZZZZ not asked again for 30 days
    assert later.tickers_without_prices == ["ZZZZ"]


def test_server_error_on_one_ticker_does_not_stop_the_run(tmp_path):
    s = FakeSession({"SPY": SPY, "BBB": SPY}, fail={"AAA"})
    report = run(tmp_path, [trade("AAA", "2026-10-26", 50_001, 100_000), trade("BBB", "2026-10-26")], s)
    assert "AAA" in report.errors and "AAA" in report.pending
    assert "BBB" in report.fetched


def test_ended_early_and_no_data(tmp_path):
    old = [("2026-09-01", 10.0, 1.0), ("2026-09-02", 11.0, 1.0)]
    s = FakeSession({"SPY": SPY, "GONE": old, "EMPTY": []})
    report = run(tmp_path, [trade("GONE", "2026-09-01"), trade("EMPTY", "2026-10-26")], s)
    assert report.ended_early == {"GONE": "2026-09-02"}
    assert report.without_prices == {"no_data": ["EMPTY"]}


def test_without_client_publishes_cache_and_reports_pending(tmp_path):
    run(tmp_path, [trade("SPY", "2026-10-26")], FakeSession({"SPY": SPY}))
    (tmp_path / "out" / "SPY.json").unlink()
    report = update_prices([trade("SPY", "2026-10-26"), trade("NVDA", "2026-10-26")], client=None,
                           cache_dir=tmp_path / "cache", out_dir=tmp_path / "out",
                           now=NOW.replace(day=31))
    assert report.pending == ["SPY", "NVDA"]
    assert (tmp_path / "out" / "SPY.json").exists()  # republished from cache
    assert report.stopped is None


def test_reserved_ticker_round_trips_through_cache_and_meta(tmp_path):
    run(tmp_path, [trade("CON", "2026-10-26")], FakeSession({"SPY": SPY, "CON": SPY}))
    assert (tmp_path / "cache" / "CON_.json").exists()
    assert (tmp_path / "out" / "CON_.json").exists()
    report = run(tmp_path, [trade("CON", "2026-10-26")], FakeSession({}))
    assert report.to_meta()["fileNames"] == {"CON": "CON_"}


def test_corrupt_cache_file_is_refetched(tmp_path):
    cache = PriceCache(tmp_path / "cache")
    cache.folder.mkdir(parents=True)
    (cache.folder / "SPY.json").write_text("{not json")
    assert cache.load("SPY") is None
    s = FakeSession({"SPY": SPY})
    run(tmp_path, [trade("SPY", "2026-10-26")], s)
    assert s.calls and cache.load("SPY").last_date == "2026-10-30"


def test_cache_entry_merge_overrides_by_date():
    e = CacheEntry("X", rows=[["2026-01-01", 1, 1, 1, 0], ["2026-01-02", 2, 2, 1, 0]])
    e.merge([["2026-01-02", 3, 3, 1, 0], ["2026-01-03", 4, 4, 1, 0]])
    assert [r[1] for r in e.rows] == [1, 3, 4]
