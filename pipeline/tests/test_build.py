import json
import pytest
from pathlib import Path

import build
import config

FIXTURE = Path(__file__).parent / "fixtures" / "capitol_api_trades.json"


def test_build_writes_trades_and_meta(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    out = tmp_path / "data"
    code = build.main(["--sources", "house", "--no-members", "--capitol-file", str(FIXTURE), "--since", "2018-01-01", "--out", str(out),
                       "--skip-prices"])
    assert code == 0

    trades = json.loads((out / "trades.json").read_text(encoding="utf-8"))
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))

    assert len(trades) == 6
    assert [t["txDate"] for t in trades] == sorted(t["txDate"] for t in trades)
    required = {"id", "chamber", "member", "party", "state", "owner", "ticker", "assetType", "type",
                "txDate", "discDate", "amountLow", "amountHigh", "source", "sourceUrl"}
    assert all(required <= t.keys() for t in trades)

    assert meta["counts"] == {
        "raw": 13, "kept": 6, "excluded": 7,
        "excludedByReason": meta["counts"]["excludedByReason"],
        "flags": {"amount_bracket_inferred": 2},  # ROL and BRK-B rows
    }
    assert sum(meta["counts"]["excludedByReason"].values()) == 7
    assert meta["coverage"]["house"]["trades"] == 6
    assert meta["coverage"]["house"]["firstTxDate"] == trades[0]["txDate"]
    assert "BRK-B" in meta["tickers"]
    assert meta["tickersWithoutPrices"] == []
    assert "kept: 6" in capsys.readouterr().out
    assert len(json.loads((tmp_path / "cache" / "excluded.json").read_text())) == 7


def test_build_reports_unreachable_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("sources.capitol_api.time.sleep", lambda s: None)
    code = build.main(["--sources", "house", "--no-members", "--capitol-url", "http://127.0.0.1:9", "--out", str(tmp_path), "--skip-prices"])
    assert code == 1
    assert "Is Capitol API running" in capsys.readouterr().err


class _FakeTiingo:
    """Stands in for TiingoClient: every ticker has two days of prices, except BRK-B."""

    def __init__(self, key, **kw):
        self.requests_made = 0

    def daily_prices(self, ticker, start, end):
        import prices
        self.requests_made += 1
        if ticker == "BRK-B":
            raise prices.TickerNotFound("not found")
        return [{"date": "2026-10-29T00:00:00.000Z", "close": 10.0, "splitFactor": 1.0},
                {"date": "2026-10-30T00:00:00.000Z", "close": 11.0, "splitFactor": 1.0}]


def test_build_runs_price_step(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("TIINGO_API_KEY", "KEY")
    monkeypatch.setattr(build.prices, "TiingoClient", _FakeTiingo)
    out = tmp_path / "data"
    assert build.main(["--sources", "house", "--no-members", "--capitol-file", str(FIXTURE), "--out", str(out)]) == 0

    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    tickers = meta["tickers"]
    assert meta["tickersWithoutPrices"] == ["BRK-B"]
    assert meta["prices"]["withoutPricesByReason"] == {"not_found": ["BRK-B"]}
    assert meta["prices"]["lastDate"] == "2026-10-30" and meta["prices"]["pending"] == []
    files = {p.stem for p in (out / "prices").glob("*.json")}
    assert files == (set(tickers) - {"BRK-B"}) | {"SPY"}
    assert "prices:" in capsys.readouterr().out


def test_prices_only_without_key_reports_pending(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    out = tmp_path / "data"
    assert build.main(["--sources", "house", "--no-members", "--capitol-file", str(FIXTURE), "--out", str(out), "--skip-prices"]) == 0
    assert build.main(["--prices-only", "--out", str(out)]) == 0

    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert "SPY" in meta["prices"]["pending"]
    assert meta["counts"]["kept"] == 6  # trade metadata preserved
    assert "TIINGO_API_KEY: MISSING" in capsys.readouterr().out


def test_prices_only_needs_existing_trades(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    assert build.main(["--prices-only", "--out", str(tmp_path / "empty")]) == 1
    assert "needs an existing trades.json" in capsys.readouterr().err


# ---------------------------------------------------------------- phase 5: several sources

from tests.test_members import FakeGithub
from tests.test_senate_efd import FakeEfd


@pytest.fixture
def two_sources(tmp_path, monkeypatch):
    """House from the fixture file, Senate from the fake eFD site, members from fixtures."""
    import members
    from sources import SenateEfdSource

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")

    class FakeSenate(SenateEfdSource):
        def __init__(self, **kw):
            kw.update(session=FakeEfd(), delay=0, page_size=2)
            super().__init__(**kw)

    monkeypatch.setattr(build, "SenateEfdSource", FakeSenate)
    real_load = members.load_legislators
    monkeypatch.setattr(members, "load_legislators",
                        lambda path, **kw: real_load(path, **{**kw, "session": FakeGithub()}))
    return tmp_path / "data"


def _read(out):
    return (json.loads((out / "trades.json").read_text(encoding="utf-8")),
            json.loads((out / "meta.json").read_text(encoding="utf-8")))


def test_build_combines_house_and_senate_with_member_metadata(two_sources, capsys):
    out = two_sources
    assert build.main(["--capitol-file", str(FIXTURE), "--out", str(out), "--skip-prices"]) == 0
    trades, meta = _read(out)
    assert {t["chamber"] for t in trades} == {"house", "senate"}
    assert meta["sources"] == ["capitol-api", "senate-efd"]
    assert meta["countsBySource"]["senate-efd"]["kept"] == 4
    assert meta["counts"]["kept"] == len(trades) == 10
    assert set(meta["coverage"]) == {"house", "senate"}
    assert meta["sourceStats"]["senate-efd"]["paper_reports_skipped"] == 1

    tub = next(t for t in trades if t["ticker"] == "NVDA" and t["chamber"] == "senate")
    assert (tub["memberId"], tub["member"], tub["party"], tub["state"], tub["gender"]) == \
        ("T000278", "Tommy Tuberville", "R", "AL", "M")
    scott = next(t for t in trades if t["ticker"] == "JPM")
    assert (scott["memberId"], scott["state"]) == ("S001217", "FL")
    assert meta["members"]["resolved"] == 7  # 4 Senate + the 3 House trades by members in the legislators fixture
    assert len(meta["members"]["unresolved"]) == 3
    assert "members: metadata for 7 of 10 trades" in capsys.readouterr().out


def test_sources_left_out_keep_their_trades(two_sources, capsys):
    out = two_sources
    assert build.main(["--capitol-file", str(FIXTURE), "--out", str(out), "--skip-prices"]) == 0
    first_trades, first_meta = _read(out)

    assert build.main(["--sources", "house", "--capitol-file", str(FIXTURE), "--out", str(out),
                       "--skip-prices"]) == 0
    trades, meta = _read(out)
    assert sorted(t["id"] for t in trades) == sorted(t["id"] for t in first_trades)
    assert meta["carriedOver"] == {"senate-efd": 4}
    assert meta["countsBySource"]["senate-efd"] == first_meta["countsBySource"]["senate-efd"]
    assert "kept 4 trades from the last build" in capsys.readouterr().out


def test_failed_source_is_carried_over_and_exit_code_is_1(two_sources, monkeypatch, capsys):
    out = two_sources
    assert build.main(["--capitol-file", str(FIXTURE), "--out", str(out), "--skip-prices"]) == 0
    monkeypatch.setattr("sources.capitol_api.time.sleep", lambda s: None)
    code = build.main(["--capitol-url", "http://127.0.0.1:9", "--out", str(out), "--skip-prices"])
    assert code == 1
    trades, meta = _read(out)
    assert meta["carriedOver"] == {"capitol-api": 6}
    assert "capitol-api" in meta["sourceErrors"]
    assert {t["chamber"] for t in trades} == {"house", "senate"}
    assert "Is Capitol API running" in capsys.readouterr().err


def test_senate_offline_rebuilds_from_cache(two_sources):
    out = two_sources
    assert build.main(["--sources", "senate", "--out", str(out), "--skip-prices"]) == 0
    assert build.main(["--sources", "senate", "--senate-offline", "--out", str(out), "--skip-prices"]) == 0
    trades, meta = _read(out)
    assert len(trades) == 4 and meta["sourceStats"]["senate-efd"]["reports_downloaded"] == 0


def test_bad_sources_argument(capsys):
    with pytest.raises(SystemExit):
        build.main(["--sources", "house,moon"])
    assert "choose from house, senate" in capsys.readouterr().err
