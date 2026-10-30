import json
from pathlib import Path

import build
import config

FIXTURE = Path(__file__).parent / "fixtures" / "capitol_api_trades.json"


def test_build_writes_trades_and_meta(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    out = tmp_path / "data"
    code = build.main(["--capitol-file", str(FIXTURE), "--since", "2018-01-01", "--out", str(out)])
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
    code = build.main(["--capitol-url", "http://127.0.0.1:9", "--out", str(tmp_path)])
    assert code == 1
    assert "Is Capitol API running" in capsys.readouterr().err
