"""Capitol API adapter (House PTRs, 2018 onward).

Capitol API (https://github.com/crnicholson/capitol-api) is self-hosted: run it with
``npm start`` and point ``CAPITOL_API_URL`` at it (default http://localhost:3000).
Its public demo site is not meant for projects, so it is not the default.

The adapter can also read a saved response or the server's own ``cache/trades.json``
(``--capitol-file``), which is how the tests run without a network.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from .base import Member, RawTrade, TradeSource

DEFAULT_URL = "http://localhost:3000"
PAGE_SIZE = 1000


class CapitolApiSource(TradeSource):
    name = "capitol-api"

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        file: str | Path | None = None,
        session: requests.Session | None = None,
        page_size: int = PAGE_SIZE,
        cache_path: str | Path | None = None,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.file = Path(file) if file else None
        self.session = session or requests.Session()
        self.page_size = page_size
        self.cache_path = Path(cache_path) if cache_path else None
        self.retries = retries

    # -- public -----------------------------------------------------------------

    def fetch(self, since: str | None = None) -> Iterable[RawTrade]:
        records = self._load_file() if self.file else self._download(since)
        for rec in records:
            yield to_raw_trade(rec)

    # -- loading ----------------------------------------------------------------

    def _load_file(self) -> list[dict]:
        with open(self.file, encoding="utf-8") as fh:
            return extract_trades(json.load(fh))

    def _download(self, since: str | None) -> list[dict]:
        records: list[dict] = []
        offset = 0
        while True:
            params: dict[str, Any] = {"sort": "oldest", "limit": self.page_size, "offset": offset}
            if since:
                params["from"] = since
            body = self._get_json(f"{self.base_url}/api/trades", params)
            page = extract_trades(body)
            records.extend(page)
            total = body.get("total") if isinstance(body, dict) else None
            offset += len(page)
            if not page or len(page) < self.page_size or (total is not None and offset >= total):
                break
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump({"trades": records}, fh)
        return records

    def _get_json(self, url: str, params: dict) -> Any:
        last_err: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, params=params, timeout=60)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as err:
                last_err = err
                if attempt + 1 < self.retries:
                    time.sleep(2**attempt)
        raise RuntimeError(
            f"Could not read {url} ({last_err}). Is Capitol API running? "
            "Start it with `npm start` in its folder, set CAPITOL_API_URL, "
            "or pass --capitol-file with a saved trades.json."
        ) from last_err


def extract_trades(body: Any) -> list[dict]:
    """Accept an /api/trades response, the server's cache file, or a bare list."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("trades"), list):
        return body["trades"]
    raise ValueError("Unrecognized Capitol API payload: expected a list or an object with 'trades'")


def to_raw_trade(rec: dict) -> RawTrade:
    asset = rec.get("asset") or {}
    tx = rec.get("transaction") or {}
    filing = rec.get("filing") or {}
    person = rec.get("person") or {}

    member = Member(
        name=person.get("name") or "Unknown",
        member_id=person.get("bioguideId"),
        chamber="house",
        party=person.get("party"),
        state=person.get("state"),
        district=person.get("district"),
        gender=person.get("gender"),
        birth_date=person.get("birthday"),
    )
    source_id = str(rec.get("id") or "")
    return RawTrade(
        source=CapitolApiSource.name,
        source_id=source_id,
        filing_id=str(filing.get("docId") or source_id.split("-")[0]),
        member=member,
        # Capitol API writes "DC" both for dependent children and whenever it can't
        # read the owner column (which in a House PTR usually means the member).
        owner_code="SELF_OR_DC" if rec.get("owner") == "DC" else rec.get("owner"),
        asset_name=asset.get("name"),
        asset_type_code=asset.get("type"),
        ticker=asset.get("ticker"),
        tx_code=tx.get("type"),
        tx_date=tx.get("tradeDate"),
        # filingDate is when the report reached the Clerk, i.e. when it became public.
        disclosure_date=tx.get("filingDate"),
        amount_text=tx.get("amount"),
        amount_min=tx.get("amountMin"),
        amount_max=tx.get("amountMax"),
        source_url=filing.get("pdfUrl"),
        extra={"notificationDate": tx.get("notificationDate")},
    )

