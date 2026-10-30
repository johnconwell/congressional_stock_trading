"""Price step: daily closes from Tiingo, cached on disk and published as compact JSON.

For every traded ticker plus the SPY benchmark this module

1. keeps the *raw* Tiingo rows (close, adjClose, splitFactor, divCash) in
   ``pipeline/cache/prices/<TICKER>.json`` and only asks Tiingo for dates after the
   last cached one;
2. rebuilds split-adjusted closes from the full raw history on every run, so a new
   split also re-adjusts the older prices;
3. writes ``web/data/prices/<FILE>.json`` as ``{"ticker", "dates", "close"}``.

The free Tiingo plan allows 50 requests an hour, 1,000 a day and 500 unique symbols
a month. Tickers are fetched SPY first, then new tickers by dollar volume, then the
least recently fetched (``fetch_order``). A run stops cleanly when Tiingo reports a limit (or when
``max_requests`` is used up): everything fetched so far is already cached, and the
next run carries on with the tickers that are still pending.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import requests

BENCHMARK = "SPY"
TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"

# A ticker that Tiingo doesn't know is asked about again after this many days.
RETRY_MISSING_DAYS = 30
# A ticker fetched less than this many hours ago is not fetched again.
MAX_AGE_HOURS = 12
# A ticker whose last close is this many calendar days before SPY's last close is
# reported as "ended early" (delisted, merged or renamed).
ENDED_EARLY_DAYS = 10

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(10)} | {f"LPT{i}" for i in range(10)}
)


# -- errors -------------------------------------------------------------------------


class TickerNotFound(Exception):
    """Tiingo has no such ticker."""


class RateLimited(Exception):
    """Tiingo refused the request because a usage limit was reached."""


# -- file names ----------------------------------------------------------------------


def price_file_stem(ticker: str) -> str:
    """File name (without .json) for a ticker's price file.

    Usually the ticker itself. Windows can't create files named after devices such
    as CON or PRN (even with an extension), so those get a trailing underscore:
    ``CON`` -> ``CON_.json``. The web app must apply the same rule.
    """
    return ticker + "_" if ticker.split(".")[0] in _WINDOWS_RESERVED else ticker


def valid_ticker(ticker: str) -> bool:
    return bool(_TICKER_RE.match(ticker or ""))


# -- Tiingo client -------------------------------------------------------------------


class TiingoClient:
    """Minimal client for Tiingo's end-of-day price endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        retries: int = 3,
        min_interval: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key
        self.session = session or requests.Session()
        self.retries = retries
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.requests_made = 0

    def daily_prices(self, ticker: str, start: str, end: str) -> list[dict]:
        """Return Tiingo's daily rows for ``ticker`` between two YYYY-MM-DD dates."""
        url = TIINGO_URL.format(ticker=ticker.lower())
        params = {"startDate": start, "endDate": end, "format": "json", "resampleFreq": "daily"}
        headers = {"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"}
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=60)
            except requests.RequestException as err:
                last_err = err
            else:
                self.requests_made += 1
                body = _json_or_text(resp)
                detail = body.get("detail", "") if isinstance(body, dict) else ""
                text = (detail or (body if isinstance(body, str) else "")).lower()
                if resp.status_code == 429 or _is_limit_message(text):
                    raise RateLimited(detail or str(body)[:200] or f"HTTP {resp.status_code}")
                if resp.status_code == 404 or "not found" in text:
                    raise TickerNotFound(detail or f"HTTP {resp.status_code}")
                if resp.status_code in (401, 403):
                    raise RuntimeError(f"Tiingo rejected the API key (HTTP {resp.status_code}): {detail}")
                if resp.ok and isinstance(body, list):
                    return body
                last_err = RuntimeError(f"HTTP {resp.status_code}: {str(body)[:200]}")
                if resp.status_code < 500 and resp.ok is False:
                    break  # other 4xx: retrying won't help
            if attempt + 1 < self.retries:
                self._sleep(2**attempt)
        raise RuntimeError(f"Tiingo request for {ticker} failed: {last_err}")

    def _throttle(self) -> None:
        if self.min_interval and self._last_request is not None:
            wait = self.min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()


def _json_or_text(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text or ""


def _is_limit_message(text: str) -> bool:
    return any(s in text for s in ("run over", "allocation", "rate limit", "too many requests",
                                   "symbol look up", "upgrade"))


# -- cache ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """Raw Tiingo history for one ticker, plus bookkeeping."""

    ticker: str
    status: str = "ok"  # ok | not_found
    start: str | None = None  # first date requested (history is complete from here)
    fetched_at: str | None = None  # ISO timestamp of the last successful request
    # [date, close, adjClose, splitFactor, divCash], sorted by date
    rows: list[list] = field(default_factory=list)

    @property
    def last_date(self) -> str | None:
        return self.rows[-1][0] if self.rows else None

    def merge(self, new_rows: Iterable[list]) -> None:
        by_date = {r[0]: r for r in self.rows}
        for r in new_rows:
            by_date[r[0]] = r
        self.rows = [by_date[d] for d in sorted(by_date)]

    def to_json(self) -> dict:
        return {"ticker": self.ticker, "status": self.status, "start": self.start,
                "fetchedAt": self.fetched_at, "rows": self.rows}

    @classmethod
    def from_json(cls, d: dict) -> "CacheEntry":
        return cls(ticker=d["ticker"], status=d.get("status", "ok"), start=d.get("start"),
                   fetched_at=d.get("fetchedAt"), rows=d.get("rows") or [])


class PriceCache:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def _path(self, ticker: str) -> Path:
        return self.folder / f"{price_file_stem(ticker)}.json"

    def load(self, ticker: str) -> CacheEntry | None:
        p = self._path(ticker)
        if not p.exists():
            return None
        try:
            return CacheEntry.from_json(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, KeyError):
            return None  # corrupt cache file: treat as missing and refetch

    def save(self, entry: CacheEntry) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._path(entry.ticker).with_suffix(".tmp")
        tmp.write_text(json.dumps(entry.to_json(), separators=(",", ":")), encoding="utf-8")
        tmp.replace(self._path(entry.ticker))


def to_row(r: dict) -> list:
    """Tiingo row -> [date, close, adjClose, splitFactor, divCash]."""
    return [str(r["date"])[:10], r.get("close"), r.get("adjClose"),
            r.get("splitFactor", 1.0) or 1.0, r.get("divCash", 0.0) or 0.0]


# -- split adjustment ----------------------------------------------------------------


def split_adjusted(rows: list[list]) -> tuple[list[str], list[float]]:
    """Dates and split-adjusted closes (dividends are *not* folded in).

    Tiingo's ``splitFactor`` on day d is the split ratio taking effect that day
    (4.0 for a 4-for-1 split), so every close *before* d is divided by it.
    Rows without a close are dropped.
    """
    dates: list[str] = []
    closes: list[float] = []
    factor = 1.0
    for d, close, _adj, split, _div in reversed(rows):
        if close is not None:
            dates.append(d)
            closes.append(round(close / factor, 4))
        if split and split != 1.0:
            factor *= split
    dates.reverse()
    closes.reverse()
    return dates, closes


def write_price_file(folder: Path, ticker: str, dates: list[str], closes: list[float]) -> bool:
    """Write web/data/prices/<FILE>.json. Returns True if the file changed."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{price_file_stem(ticker)}.json"
    text = json.dumps({"ticker": ticker, "dates": dates, "close": closes}, separators=(",", ":")) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


# -- the price step ------------------------------------------------------------------


@dataclass
class PriceReport:
    fetched: list[str] = field(default_factory=list)  # tickers requested this run
    up_to_date: list[str] = field(default_factory=list)  # skipped: fetched recently
    pending: list[str] = field(default_factory=list)  # not fetched yet (limit, no key, budget)
    without_prices: dict[str, list[str]] = field(default_factory=dict)  # reason -> tickers
    errors: dict[str, str] = field(default_factory=dict)  # ticker -> message
    ended_early: dict[str, str] = field(default_factory=dict)  # ticker -> last date
    files_written: int = 0
    files_total: int = 0
    last_date: str | None = None  # SPY's last close
    requests: int = 0
    stopped: str | None = None  # why fetching stopped early, if it did

    def to_meta(self) -> dict:
        names = {t: price_file_stem(t) for t in self.published if price_file_stem(t) != t}
        return {
            "source": "tiingo",
            "adjustment": "split",
            "benchmark": BENCHMARK,
            "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lastDate": self.last_date,
            "files": self.files_total,
            "fileNames": names,  # tickers whose file name isn't <TICKER>.json
            "pending": sorted(self.pending),
            "withoutPricesByReason": {k: sorted(v) for k, v in sorted(self.without_prices.items())},
            "endedEarly": dict(sorted(self.ended_early.items())),
            "errors": dict(sorted(self.errors.items())),
            "stopped": self.stopped,
        }

    published: list[str] = field(default_factory=list)  # tickers with a price file

    @property
    def tickers_without_prices(self) -> list[str]:
        return sorted({t for v in self.without_prices.values() for t in v})


def ticker_start_dates(trades: Iterable[dict]) -> dict[str, str]:
    """Earliest transaction date per ticker, plus SPY from the earliest trade overall."""
    starts: dict[str, str] = {}
    for t in trades:
        tk, d = t.get("ticker"), t.get("txDate")
        if tk and d and (tk not in starts or d < starts[tk]):
            starts[tk] = d
    if starts:
        starts[BENCHMARK] = min(min(starts.values()), starts.get(BENCHMARK, "9999"))
    return starts


def ticker_priority(trades: Iterable[dict]) -> dict[str, float]:
    """Dollar volume per ticker (range midpoints): the order in which to fetch."""
    vol: dict[str, float] = {}
    for t in trades:
        lo, hi = t.get("amountLow") or 0, t.get("amountHigh") or t.get("amountLow") or 0
        vol[t["ticker"]] = vol.get(t["ticker"], 0.0) + (lo + hi) / 2
    return vol


def fetch_order(starts: dict[str, str], volume: dict[str, float],
                entries: dict[str, "CacheEntry | None"]) -> list[str]:
    """The order in which to ask Tiingo for tickers.

    SPY first; then tickers with no cached prices, most money traded first; then the
    cached ones, least recently fetched first. With a request budget smaller than the
    number of tickers (Tiingo's free plan, or ``--max-requests``), successive runs
    therefore cycle through every ticker instead of refreshing the same top ones.
    """
    def key(tk: str):
        entry = entries.get(tk)
        cached = entry is not None and entry.status == "ok" and bool(entry.rows)
        fetched = (entry.fetched_at or "") if cached else ""
        return (tk != BENCHMARK, cached, _fetched_sort_key(fetched), -volume.get(tk, 0.0), tk)

    return sorted(starts, key=key)


def _fetched_sort_key(stamp: str) -> float:
    """Seconds since the epoch for a fetched_at stamp; 0 for a missing or bad one."""
    if not stamp:
        return 0.0
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return then.timestamp()


def update_prices(
    trades: list[dict],
    *,
    client: TiingoClient | None,
    cache_dir: Path,
    out_dir: Path,
    today: date | None = None,
    now: datetime | None = None,
    max_requests: int | None = None,
    max_age_hours: float = MAX_AGE_HOURS,
    retry_missing_days: int = RETRY_MISSING_DAYS,
    refresh: bool = False,
    log: Callable[[str], None] = lambda s: None,
) -> PriceReport:
    """Bring the price cache up to date and publish one file per ticker.

    With ``client=None`` nothing is fetched: files are published from the cache and
    every ticker that would need a request is reported as pending.
    """
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    end = today.isoformat()
    cache = PriceCache(cache_dir)
    report = PriceReport()

    starts = ticker_start_dates(trades)
    volume = ticker_priority(trades)
    loaded = {tk: cache.load(tk) for tk in starts if valid_ticker(tk)}
    order = fetch_order(starts, volume, loaded)

    entries: dict[str, CacheEntry] = {}
    budget_left = max_requests
    for tk in order:
        if not valid_ticker(tk):
            report.without_prices.setdefault("invalid_ticker", []).append(tk)
            continue
        entry = loaded[tk]
        need_from = starts[tk]
        plan = _plan(entry, need_from, now, max_age_hours, retry_missing_days, refresh)
        if plan is None:
            if entry and entry.status == "not_found":
                report.without_prices.setdefault("not_found", []).append(tk)
            else:
                report.up_to_date.append(tk)
                entries[tk] = entry
            continue

        fetch_from, full = plan
        if fetch_from > end:  # nothing new can exist yet
            report.up_to_date.append(tk)
            entries[tk] = entry
            continue
        if client is None or report.stopped or (budget_left is not None and budget_left <= 0):
            if report.stopped is None and client is not None:
                report.stopped = "max_requests reached"
            report.pending.append(tk)
            if entry and entry.status == "ok":
                entries[tk] = entry  # publish what we already have
            continue

        try:
            raw = client.daily_prices(tk, fetch_from, end)
        except RateLimited as err:
            report.stopped = f"rate limited: {err}"
            log(f"Tiingo limit reached at {tk}: {err}")
            report.pending.append(tk)
            if entry and entry.status == "ok":
                entries[tk] = entry
            continue
        except TickerNotFound:
            entry = CacheEntry(tk, status="not_found", start=need_from, fetched_at=now.isoformat())
            cache.save(entry)
            report.fetched.append(tk)
            report.without_prices.setdefault("not_found", []).append(tk)
            continue
        except RuntimeError as err:
            if "rejected the API key" in str(err):
                raise
            report.errors[tk] = str(err)
            report.pending.append(tk)
            if entry and entry.status == "ok":
                entries[tk] = entry
            continue
        finally:
            if budget_left is not None:
                budget_left -= 1

        if full or entry is None or entry.status != "ok":
            entry = CacheEntry(tk, start=need_from)
        entry.merge(to_row(r) for r in raw if r.get("date"))
        entry.fetched_at = now.isoformat()
        cache.save(entry)
        report.fetched.append(tk)
        entries[tk] = entry

    report.requests = client.requests_made if client else 0

    # Publish and look for tickers that stopped trading.
    spy = entries.get(BENCHMARK)
    report.last_date = spy.last_date if spy else None
    cutoff = None
    if report.last_date:
        cutoff = (date.fromisoformat(report.last_date) - timedelta(days=ENDED_EARLY_DAYS)).isoformat()
    for tk, entry in entries.items():
        dates, closes = split_adjusted(entry.rows)
        if not dates:
            if tk not in report.pending:
                report.without_prices.setdefault("no_data", []).append(tk)
            continue
        if write_price_file(out_dir, tk, dates, closes):
            report.files_written += 1
        report.files_total += 1
        report.published.append(tk)
        if cutoff and dates[-1] < cutoff:
            report.ended_early[tk] = dates[-1]
    return report


def _plan(entry: CacheEntry | None, need_from: str, now: datetime, max_age_hours: float,
          retry_missing_days: int, refresh: bool) -> tuple[str, bool] | None:
    """Decide what to fetch: (start date, replace history?) or None to skip."""
    if entry is None or refresh:
        return need_from, True
    age = _age_hours(entry.fetched_at, now)
    if entry.status == "not_found" or (entry.start and not entry.rows):
        # Unknown ticker, or known but no prices in range: don't ask again every run.
        return (need_from, True) if age >= retry_missing_days * 24 else None
    if entry.start is None or entry.start > need_from:
        return need_from, True  # trades now go back further than the cache: backfill
    if age < max_age_hours:
        return None
    if entry.last_date is None:
        return need_from, True
    nxt = (date.fromisoformat(entry.last_date) + timedelta(days=1)).isoformat()
    return nxt, False


def _age_hours(stamp: str | None, now: datetime) -> float:
    if not stamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 3600
