import pytest

from normalize import clean_ticker, normalize, normalize_one, parse_amount
from sources.base import Member, RawTrade


def raw(**kw) -> RawTrade:
    base = dict(
        source="test", source_id="1-1", filing_id="1", member=Member(name="Jane Doe", member_id="D000001",
                                                                      party="Democrat", state="CA"),
        owner_code="SP", asset_name="Nvidia", asset_type_code="ST", ticker="NVDA", tx_code="P",
        tx_date="2025-03-14", disclosure_date="2025-04-02", amount_text="$15,001 - $50,000",
    )
    base.update(kw)
    return RawTrade(**base)


# -- transaction types --------------------------------------------------------------

@pytest.mark.parametrize("code, expected", [
    ("P", "buy"), ("S (partial)", "sell"), ("S", "sell_full"), (" s ", "sell_full"),
])
def test_tx_types(code, expected):
    assert normalize_one(raw(tx_code=code))["type"] == expected


@pytest.mark.parametrize("code, reason", [("E", "exchange"), ("G", "tx_type:G"), (None, "tx_type:missing")])
def test_excluded_tx_types(code, reason):
    assert normalize_one(raw(tx_code=code)) == reason


# -- amounts ------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("$1,001 - $15,000", (1001, 15000, [])),
    ("$15,001", (15001, 50000, ["amount_bracket_inferred"])),          # upper bound lost in parse
    ("$1,000,001", (1000001, 5000000, ["amount_bracket_inferred"])),
    ("Over $50,000,000", (50000000, 50000000, ["amount_open_ended"])),
    ("$50,000,001", (50000001, 50000001, ["amount_open_ended"])),       # floor of the top bracket
    ("Over $1,000,000", (1000000, 1000000, ["amount_open_ended"])),     # spouse-only bracket
    ("$823", (823, 823, [])),                                          # exact amount
])
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_amount_falls_back_to_source_parse():
    assert parse_amount(None, 1001, 15000) == (1001, 15000, [])
    assert parse_amount("", 5000001, None) == (5000001, 5000001, ["amount_open_ended"])


def test_missing_amount_excluded():
    assert normalize_one(raw(amount_text=None)) == "no_amount"


# -- tickers ------------------------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("NVDA", "NVDA"), (" nvda ", "NVDA"), ("$AAPL", "AAPL"), ("BRK.B", "BRK-B"), ("BF/B", "BF-B"),
    ("BRK B", "BRK-B"), ("HEI.A", "HEI-A"), ("AAPL US", "AAPL"), ("NASDAQ: MSFT", "MSFT"),
    ("", None), (None, None), ("912797KJ5", None), ("SPXW 250117C05000000", None),
])
def test_clean_ticker(given, expected):
    assert clean_ticker(given) == expected


def test_bad_and_missing_tickers():
    assert normalize_one(raw(ticker=None)) == "no_ticker"
    assert normalize_one(raw(ticker="91282CJP7")) == "bad_ticker"


# -- assets, dates, owners, parties -------------------------------------------------

@pytest.mark.parametrize("code, kept", [("ST", True), ("EF", True), ("RE", True), (None, True),
                                         ("GS", False), ("OP", False), ("CT", False), ("MF", False)])
def test_asset_types(code, kept):
    out = normalize_one(raw(asset_type_code=code))
    assert isinstance(out, dict) is kept
    if not kept:
        assert out == f"asset_type:{code}"


def test_dates():
    assert normalize_one(raw(tx_date=None)) == "missing_tx_date"
    assert normalize_one(raw(tx_date="2025-13-40")) == "missing_tx_date"
    assert normalize_one(raw(tx_date="2025-05-17", disclosure_date="2025-05-11")) == "tx_after_disclosure"
    assert normalize_one(raw(tx_date="2017-12-29"), since="2018-01-01") == "before_since"
    assert normalize_one(raw(disclosure_date=None))["discDate"] is None


@pytest.mark.parametrize("code, owner", [("SP", "spouse"), ("JT", "joint"), ("DC", "dependent"),
                                          (None, "self"), ("SELF_OR_DC", "self_or_dependent"),
                                          ("JP", "other")])
def test_owners(code, owner):
    assert normalize_one(raw(owner_code=code))["owner"] == owner


def test_schema_fields():
    t = normalize_one(raw())
    assert t["id"] == "house-1-1"
    assert t["party"] == "D" and t["state"] == "CA" and t["memberId"] == "D000001"
    assert (t["amountLow"], t["amountHigh"]) == (15001, 50000)
    assert isinstance(t["amountLow"], int)
    assert t["assetType"] == "stock" and t["flags"] == []


# -- amendments and ordering --------------------------------------------------------

def test_duplicate_across_filings_keeps_latest():
    original = raw(source_id="100-1", filing_id="100", disclosure_date="2025-04-02")
    amended = raw(source_id="200-1", filing_id="200", disclosure_date="2025-05-01")
    result = normalize([original, amended])
    assert [t["id"] for t in result.trades] == ["house-200-1"]
    assert result.excluded_by_reason() == {"duplicate_filing": 1}


def test_identical_lots_in_one_filing_are_kept():
    a = raw(source_id="100-1", filing_id="100")
    b = raw(source_id="100-2", filing_id="100")
    assert len(normalize([a, b]).trades) == 2


def test_amended_filing_with_two_lots_keeps_both_lots():
    rows = [raw(source_id=f"{f}-{i}", filing_id=f, disclosure_date=d)
            for f, d in (("100", "2025-04-02"), ("200", "2025-05-01")) for i in (1, 2)]
    result = normalize(rows)
    assert sorted(t["id"] for t in result.trades) == ["house-200-1", "house-200-2"]


def test_different_trades_are_not_deduped():
    a = raw(source_id="100-1", filing_id="100")
    b = raw(source_id="200-1", filing_id="200", owner_code="JT")
    assert len(normalize([a, b]).trades) == 2


def test_sorted_by_tx_date_and_ids_unique():
    rows = [raw(source_id="9-1", filing_id="9", tx_date="2025-03-20", ticker="AAPL"),
            raw(source_id="8-1", filing_id="8", tx_date="2025-01-02", ticker="MSFT"),
            raw(source_id="8-1", filing_id="8", tx_date="2025-01-03", ticker="AMZN")]
    trades = normalize(rows).trades
    assert [t["txDate"] for t in trades] == ["2025-01-02", "2025-01-03", "2025-03-20"]
    assert len({t["id"] for t in trades}) == 3
