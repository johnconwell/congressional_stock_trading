"""Turn RawTrade records from any source into the normalized trade schema (README 3.2).

Rules, applied in order. The first rule a trade fails is recorded as its exclusion reason:

1. Transaction type: P -> buy, S (partial) -> sell, S -> sell_full. Exchanges and
   everything else are excluded.
2. Dates: a valid transaction date is required, and it can't be after the disclosure date.
   Trades before ``since`` are dropped.
3. Asset type: only stocks, ETFs and listed REITs (ST, EF, RE). A record with no type
   code is kept if its ticker is valid.
4. Ticker: cleaned and checked (class shares become Tiingo style, BRK.B -> BRK-B).
5. Amount: the range is parsed from the text. A lone bracket floor (``$15,001``, from a
   line break in the PDF) is expanded to its standard bracket. An open top bracket uses
   its lower bound for both ends.
6. Amendments: if the same trade (member, owner, ticker, type, date, amount) appears in
   more than one filing, only the latest filing's copies are kept. Identical rows
   inside one filing are separate lots and are all kept.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from sources.base import RawTrade

# -- lookup tables ----------------------------------------------------------------

TX_TYPES = {
    "P": "buy",
    "PURCHASE": "buy",
    "S (PARTIAL)": "sell",
    "SALE (PARTIAL)": "sell",
    "S": "sell_full",
    "SALE (FULL)": "sell_full",
}

ASSET_TYPES = {"ST": "stock", "EF": "etf", "RE": "reit"}

OWNERS = {
    None: "self",
    "": "self",
    "SELF": "self",
    "SP": "spouse",
    "JT": "joint",
    "DC": "dependent",
    # Capitol API writes DC when it can't read the owner column. In House PTRs a blank
    # owner means the member, so its DC is usually the member and only sometimes a child.
    "SELF_OR_DC": "self_or_dependent",
}

PARTIES = {"REPUBLICAN": "R", "DEMOCRAT": "D", "DEMOCRATIC": "D", "INDEPENDENT": "I"}

# Disclosure amount brackets (House and Senate). None = open-ended top bracket.
BRACKETS: list[tuple[int, int | None]] = [
    (1_001, 15_000),
    (15_001, 50_000),
    (50_001, 100_000),
    (100_001, 250_000),
    (250_001, 500_000),
    (500_001, 1_000_000),
    (1_000_001, 5_000_000),
    (5_000_001, 25_000_000),
    (25_000_001, 50_000_000),
    (50_000_001, None),
]
_BRACKET_BY_FLOOR = {lo: hi for lo, hi in BRACKETS}

TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")
_EXCHANGE_PREFIX = re.compile(r"^(NYSE|NASDAQ|NYSEARCA|AMEX|BATS)\s*:\s*")
_EXCHANGE_SUFFIX = re.compile(r"[\s:](US|UN|UW|UQ|UA|UR|UP)$")
_CLASS_SHARE = re.compile(r"^([A-Z]{1,5})[./\s]([A-Z]{1,2})$")
_MONEY = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


# -- field helpers ----------------------------------------------------------------


def clean_ticker(raw: str | None) -> str | None:
    """Return a Tiingo-style ticker, or None if ``raw`` isn't a usable ticker."""
    if not raw:
        return None
    t = raw.strip().upper().lstrip("$").strip()
    t = _EXCHANGE_PREFIX.sub("", t)
    t = _EXCHANGE_SUFFIX.sub("", t).strip()
    m = _CLASS_SHARE.match(t)
    if m:
        t = f"{m.group(1)}-{m.group(2)}"
    return t if TICKER_RE.match(t) else None


def parse_amount(
    text: str | None, src_min: float | None = None, src_max: float | None = None
) -> tuple[float, float, list[str]] | None:
    """Return (low, high, flags) or None if there's no usable amount."""
    flags: list[str] = []
    lo = hi = None
    open_ended = False
    if text:
        nums = [float(n.replace(",", "")) for n in _MONEY.findall(text)]
        if nums:
            if re.search(r"\bover\b", text, re.I):
                lo, open_ended = nums[0], True
            elif len(nums) >= 2:
                lo, hi = nums[0], nums[1]
            else:
                lo = nums[0]
                if int(lo) == lo and int(lo) in _BRACKET_BY_FLOOR:
                    # "$15,001" alone: the upper bound was lost in the PDF parse.
                    hi = _BRACKET_BY_FLOOR[int(lo)]
                    if hi is None:
                        open_ended = True
                    else:
                        flags.append("amount_bracket_inferred")
                else:
                    hi = lo  # an exact amount, e.g. "$823"
    if lo is None and src_min is not None:
        lo = float(src_min)
        hi = float(src_max) if src_max is not None else None
        open_ended = hi is None
    if lo is None or lo <= 0:
        return None
    if open_ended or hi is None:
        hi = lo
        flags.append("amount_open_ended")
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi, flags


def _iso(d: str | None) -> str | None:
    if not d:
        return None
    try:
        return date.fromisoformat(d[:10]).isoformat()
    except ValueError:
        return None


def _party(p: str | None) -> str | None:
    if not p:
        return None
    return PARTIES.get(p.strip().upper(), p.strip()[:1].upper())


def _number(x: float) -> int | float:
    return int(x) if x == int(x) else x


# -- main -------------------------------------------------------------------------


@dataclass
class Exclusion:
    reason: str
    raw: RawTrade


@dataclass
class NormalizeResult:
    trades: list[dict] = field(default_factory=list)
    excluded: list[Exclusion] = field(default_factory=list)

    def excluded_by_reason(self) -> dict[str, int]:
        return dict(Counter(e.reason for e in self.excluded).most_common())


def normalize_one(raw: RawTrade, since: str | None = None) -> dict | str:
    """Return the normalized trade dict, or the exclusion reason as a string."""
    code = (raw.tx_code or "").strip().upper()
    tx_type = TX_TYPES.get(code)
    if tx_type is None:
        return "exchange" if code in ("E", "EXCHANGE") else f"tx_type:{code or 'missing'}"

    tx_date = _iso(raw.tx_date)
    if tx_date is None:
        return "missing_tx_date"
    disc_date = _iso(raw.disclosure_date)
    if disc_date and tx_date > disc_date:
        return "tx_after_disclosure"
    if since and tx_date < since:
        return "before_since"

    atype = (raw.asset_type_code or "").strip().upper()
    if atype and atype not in ASSET_TYPES:
        return f"asset_type:{atype}"

    ticker = clean_ticker(raw.ticker)
    if ticker is None:
        return "no_ticker" if not (raw.ticker or "").strip() else "bad_ticker"

    amount = parse_amount(raw.amount_text, raw.amount_min, raw.amount_max)
    if amount is None:
        return "no_amount"
    lo, hi, flags = amount

    owner_key = (raw.owner_code or "").strip().upper() or None
    owner = OWNERS.get(owner_key, "other")

    m = raw.member
    return {
        "id": f"{m.chamber}-{raw.source_id}",
        "chamber": m.chamber,
        "member": m.name,
        "memberId": m.member_id,
        "party": _party(m.party),
        "state": m.state,
        "district": m.district,
        "gender": m.gender,
        "birthDate": m.birth_date,
        "owner": owner,
        "ticker": ticker,
        "assetName": raw.asset_name,
        "assetType": ASSET_TYPES.get(atype, "unknown"),
        "type": tx_type,
        "txDate": tx_date,
        "discDate": disc_date,
        "amountLow": _number(lo),
        "amountHigh": _number(hi),
        "flags": flags,
        "source": raw.source,
        "filingId": raw.filing_id,
        "sourceUrl": raw.source_url,
    }


def _dedupe_key(t: dict) -> tuple:
    return (
        t["source"],
        t["memberId"] or t["member"],
        t["owner"],
        t["ticker"],
        t["type"],
        t["txDate"],
        t["amountLow"],
        t["amountHigh"],
    )


def drop_amended_duplicates(
    pairs: list[tuple[dict, RawTrade]],
) -> tuple[list[tuple[dict, RawTrade]], list[Exclusion]]:
    """Keep only the latest filing's copies of trades that appear in several filings."""
    groups: dict[tuple, list[tuple[dict, RawTrade]]] = defaultdict(list)
    for pair in pairs:
        groups[_dedupe_key(pair[0])].append(pair)

    kept, dropped = [], []
    for group in groups.values():
        filings = {t["filingId"] for t, _ in group}
        if len(filings) == 1:
            kept.extend(group)
            continue
        latest = max(
            filings,
            key=lambda f: max((t["discDate"] or "", f) for t, _ in group if t["filingId"] == f),
        )
        for t, raw in group:
            if t["filingId"] == latest:
                kept.append((t, raw))
            else:
                dropped.append(Exclusion("duplicate_filing", raw))
    return kept, dropped


def normalize(raws, since: str | None = None) -> NormalizeResult:
    result = NormalizeResult()
    pairs: list[tuple[dict, RawTrade]] = []
    for raw in raws:
        out = normalize_one(raw, since)
        if isinstance(out, str):
            result.excluded.append(Exclusion(out, raw))
        else:
            pairs.append((out, raw))

    pairs, dropped = drop_amended_duplicates(pairs)
    result.excluded.extend(dropped)

    trades = [t for t, _ in pairs]
    seen: Counter[str] = Counter()
    for t in trades:  # ids must be unique even if a source repeats one
        seen[t["id"]] += 1
        if seen[t["id"]] > 1:
            t["id"] = f"{t['id']}~{seen[t['id']]}"
    trades.sort(key=lambda t: (t["txDate"], t["discDate"] or "", t["id"]))
    result.trades = trades
    return result
