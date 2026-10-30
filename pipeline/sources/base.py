"""The TradeSource interface and the source-neutral raw trade record.

Every adapter (Capitol API, House Clerk, Senate eFD, commercial APIs) turns its own
response format into ``RawTrade`` objects. Adapters do no cleanup: they copy the
source's codes and strings as they are. ``normalize.py`` then applies the same rules
to every source, so a trade is handled the same whichever adapter it came from.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Member:
    """The filer, as reported by the source (party/state are as of the source's data)."""

    name: str
    member_id: str | None = None  # bioguide ID when the source has it
    chamber: str = "house"  # house | senate
    party: str | None = None  # source wording, e.g. "Republican"
    state: str | None = None  # two-letter code
    district: int | None = None
    gender: str | None = None  # "M" | "F"
    birth_date: str | None = None  # YYYY-MM-DD


@dataclass
class RawTrade:
    """One transaction line from a disclosure, before normalization."""

    source: str  # adapter name, e.g. "capitol-api"
    source_id: str  # unique within the source, e.g. "20032062-1"
    filing_id: str  # the report (PTR) this line came from
    member: Member
    owner_code: str | None  # source owner code: SP, JT, DC, ... (None = self)
    asset_name: str | None
    asset_type_code: str | None  # e.g. ST, EF, GS, OP
    ticker: str | None  # as printed in the filing
    tx_code: str | None  # e.g. P, S, "S (partial)", E
    tx_date: str | None  # YYYY-MM-DD
    disclosure_date: str | None  # date the report was filed (became public)
    amount_text: str | None  # e.g. "$1,001 - $15,000"
    amount_min: float | None = None  # source's own parse of amount_text, if any
    amount_max: float | None = None
    source_url: str | None = None
    extra: dict = field(default_factory=dict)


class TradeSource(ABC):
    """A provider of congressional trades."""

    #: Short identifier written to each trade's ``source`` field.
    name: str = ""

    @abstractmethod
    def fetch(self, since: str | None = None) -> Iterable[RawTrade]:
        """Yield every trade with a transaction date on or after ``since`` (YYYY-MM-DD).

        Sources that can't filter by date may yield older trades too; normalize
        drops them.
        """
