import json
from pathlib import Path

import build
import config

FIXTURE = Path(__file__).parent / "fixtures" / "capitol_api_trades.json"


def test_build_writes_trades_and_meta(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    out = tmp_path / "data"
    code = build.main(["--capitol-file", str(FIXTURE), "--since", "2018-01-01", "--out", str(out),
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
    code = build.main(["--capitol-url", "http://127.0.0.1:9", "--out", str(tmp_path), "--skip-prices"])
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
    assert build.main(["--capitol-file", str(FIXTURE), "--out", str(out)]) == 0

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
    assert build.main(["--capitol-file", str(FIXTURE), "--out", str(out), "--skip-prices"]) == 0
    assert build.main(["--prices-only", "--out", str(out)]) == 0

    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert "SPY" in meta["prices"]["pending"]
    assert meta["counts"]["kept"] == 6  # trade metadata preserved
    assert "TIINGO_API_KEY: MISSING" in capsys.readouterr().out


def test_prices_only_needs_existing_trades(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    assert build.main(["--prices-only", "--out", str(tmp_path / "empty")]) == 1
    assert "needs an existing trades.json" in capsys.readouterr().err
