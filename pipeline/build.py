"""Pipeline entry point: fetch -> normalize -> member metadata -> prices -> write web/data.

    python build.py --since 2018-01-01                       # House (Capitol API) + Senate (eFD)
    python build.py --capitol-file path/to/trades.json       # House from a saved response
    python build.py --sources senate                         # refresh the Senate only, keep House
    python build.py --senate-offline                         # Senate from the eFD page cache
    python build.py --prices-only                            # reuse web/data/trades.json
    python build.py --capitol-file ... --skip-prices         # trades only, no Tiingo calls

A source that isn't fetched (not in --sources, or its fetch failed) keeps its trades from
the previous trades.json, so one source being down never wipes the other's data.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config
import members
import prices
from normalize import NormalizeResult, normalize
from sources import CapitolApiSource, SenateEfdSource, TradeSource

#: --sources names -> adapter ``name`` (the trade's ``source`` field)
SOURCE_NAMES = {"house": CapitolApiSource.name, "senate": SenateEfdSource.name}


def write_trades(trades: list[dict], path: Path) -> None:
    """One trade per line, so git diffs of refreshed data stay readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(t, ensure_ascii=False, separators=(",", ":")) for t in trades]
    path.write_text("[\n" + ",\n".join(lines) + "\n]\n", encoding="utf-8")


def source_counts(result: NormalizeResult, raw_count: int) -> dict:
    flags: dict[str, int] = {}
    for t in result.trades:
        for f in t["flags"]:
            flags[f] = flags.get(f, 0) + 1
    return {
        "raw": raw_count,
        "kept": len(result.trades),
        "excluded": len(result.excluded),
        "excludedByReason": result.excluded_by_reason(),
        "flags": flags,
    }


def sum_counts(blocks: list[dict]) -> dict:
    total = {"raw": 0, "kept": 0, "excluded": 0, "excludedByReason": {}, "flags": {}}
    for b in blocks:
        for k in ("raw", "kept", "excluded"):
            total[k] += b.get(k, 0)
        for k in ("excludedByReason", "flags"):
            for reason, n in b.get(k, {}).items():
                total[k][reason] = total[k].get(reason, 0) + n
    total["excludedByReason"] = dict(sorted(total["excludedByReason"].items(), key=lambda kv: -kv[1]))
    return total


def build_meta(trades: list[dict], *, since: str | None, counts_by_source: dict[str, dict]) -> dict:
    coverage: dict[str, dict] = {}
    for t in trades:
        c = coverage.setdefault(
            t["chamber"],
            {"trades": 0, "members": set(), "firstTxDate": t["txDate"], "lastTxDate": t["txDate"],
             "lastDiscDate": None},
        )
        c["trades"] += 1
        c["members"].add(t["memberId"] or t["member"])
        c["firstTxDate"] = min(c["firstTxDate"], t["txDate"])
        c["lastTxDate"] = max(c["lastTxDate"], t["txDate"])
        if t["discDate"]:
            c["lastDiscDate"] = max(c["lastDiscDate"] or "", t["discDate"])
    for c in coverage.values():
        c["members"] = len(c["members"])

    meta = {
        "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since,
        "sources": sorted(counts_by_source),
        "counts": sum_counts(list(counts_by_source.values())),
        "countsBySource": counts_by_source,
        "coverage": dict(sorted(coverage.items())),
        "tickers": sorted({t["ticker"] for t in trades}),
        "tickersWithoutPrices": [],  # filled in by the price step
    }
    run = config.ci_run()
    if run:
        meta["buildRun"] = run  # the GitHub Actions run that built this data
    return meta


def previous_build(out_dir: Path) -> tuple[list[dict], dict]:
    """The trades.json and meta.json of the last build, if there is one."""
    try:
        trades = json.loads((out_dir / "trades.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {}
    try:
        meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return trades, meta


def previous_counts(meta: dict, source: str, trades: list[dict]) -> dict:
    by_source = meta.get("countsBySource") or {}
    if source in by_source:
        return by_source[source]
    if meta.get("sources") == [source] and "counts" in meta:  # built before countsBySource existed
        return meta["counts"]
    return {"raw": len(trades), "kept": len(trades), "excluded": 0, "excludedByReason": {}, "flags": {}}


def write_excluded(result: NormalizeResult, path: Path) -> None:
    """Full list of dropped records, for checking the rules. Not published."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for e in result.excluded:
        r = asdict(e.raw)
        rows.append({"reason": e.reason, "sourceId": r["source_id"], "member": r["member"]["name"],
                     "ticker": r["ticker"], "asset": r["asset_name"], "assetType": r["asset_type_code"],
                     "tx": r["tx_code"], "txDate": r["tx_date"], "amount": r["amount_text"]})
    path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")


def write_meta(meta: dict, out_dir: Path) -> None:
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


@dataclass
class BuildResult:
    meta: dict
    trades: list[dict]
    fetched: list[str] = field(default_factory=list)      # sources fetched this run
    carried: dict[str, int] = field(default_factory=dict)  # source -> trades kept from the last build
    errors: dict[str, str] = field(default_factory=dict)   # source -> error message


def run(*, since: str | None, sources: list[TradeSource], out_dir: Path, cache_dir: Path,
        legislators: list[dict] | None = None, all_sources: list[str] | None = None) -> BuildResult:
    """Fetch each source, normalize, add member metadata and write trades.json / meta.json.

    ``all_sources`` names every source that may be in trades.json; those not fetched this
    run (or whose fetch failed) keep their trades from the previous build.
    """
    prev_trades, prev_meta = previous_build(out_dir)
    raws, fetched, errors, stats = [], [], {}, {}
    raw_count: dict[str, int] = {}
    for src in sources:
        try:
            batch = list(src.fetch(since))
        except RuntimeError as err:
            errors[src.name] = str(err)
            continue
        raws.extend(batch)
        raw_count[src.name] = len(batch)
        fetched.append(src.name)
        if getattr(src, "stats", None):
            stats[src.name] = dict(src.stats)

    if not fetched:
        raise RuntimeError("; ".join(errors.values()) or "no sources selected")

    result = normalize(raws, since=since)
    counts_by_source: dict[str, dict] = {}
    for name in fetched:
        part = NormalizeResult(
            trades=[t for t in result.trades if t["source"] == name],
            excluded=[e for e in result.excluded if e.raw.source == name],
        )
        counts_by_source[name] = source_counts(part, raw_count[name])

    carried: dict[str, int] = {}
    trades = list(result.trades)
    keep_names = set(all_sources or []) | {s.name for s in sources}
    for name in sorted(keep_names - set(fetched)):
        old = [t for t in prev_trades if t.get("source") == name]
        if old:
            trades.extend(old)
            carried[name] = len(old)
            counts_by_source[name] = previous_counts(prev_meta, name, old)
    trades.sort(key=lambda t: (t["txDate"], t["discDate"] or "", t["id"]))

    member_report = None
    if legislators is not None:
        extras = {tid: raw.extra for tid, raw in result.raw_by_id.items() if raw.extra}
        member_report = members.enrich(trades, members.Directory(legislators), extras=extras)

    meta = build_meta(trades, since=since, counts_by_source=counts_by_source)
    if member_report:
        meta["members"] = member_report.to_meta()
    if stats:
        meta["sourceStats"] = stats
    if carried:
        meta["carriedOver"] = carried
    if errors:
        meta["sourceErrors"] = errors
    write_trades(trades, out_dir / "trades.json")
    write_meta(meta, out_dir)
    write_excluded(result, cache_dir / "excluded.json")
    return BuildResult(meta=meta, trades=trades, fetched=fetched, carried=carried, errors=errors)


def run_prices(trades: list[dict], meta: dict, *, out_dir: Path, cache_dir: Path,
               client: prices.TiingoClient | None, max_requests: int | None = None,
               refresh: bool = False) -> prices.PriceReport:
    """Update the price cache, publish web/data/prices/ and record the result in meta."""
    report = prices.update_prices(
        trades, client=client, cache_dir=cache_dir / "prices", out_dir=out_dir / "prices",
        max_requests=max_requests, refresh=refresh, log=lambda m: print(m, file=sys.stderr),
    )
    meta["tickersWithoutPrices"] = report.tickers_without_prices
    meta["prices"] = report.to_meta()
    write_meta(meta, out_dir)
    return report


def print_price_report(report: prices.PriceReport, has_client: bool) -> None:
    print(f"prices: {report.requests} requests, {len(report.fetched)} tickers fetched, "
          f"{len(report.up_to_date)} already up to date, {report.files_total} files "
          f"({report.files_written} changed), SPY through {report.last_date or '-'}")
    for reason, tickers in sorted(report.without_prices.items()):
        print(f"  {len(tickers):>6}  without prices: {reason}")
    if report.ended_early:
        print(f"  {len(report.ended_early):>6}  ended early (delisted/renamed?)")
    if report.errors:
        print(f"  {len(report.errors):>6}  errors (will retry): {', '.join(sorted(report.errors)[:5])}")
    if report.pending:
        why = report.stopped or ("TIINGO_API_KEY missing" if not has_client else "")
        print(f"  {len(report.pending):>6}  pending ({why}). Run `python build.py --prices-only` "
              "again later to continue.")
        print("          Tiingo free tier: 50 requests/hour, 1,000/day, 500 unique symbols/month.")


def make_sources(args, cache_dir: Path) -> list[TradeSource]:
    out: list[TradeSource] = []
    for name in args.sources:
        if name == "house":
            out.append(CapitolApiSource(args.capitol_url, file=args.capitol_file,
                                        cache_path=cache_dir / "capitol_api.json"))
        elif name == "senate":
            out.append(SenateEfdSource(cache_dir=cache_dir / "senate_efd", offline=args.senate_offline,
                                       delay=args.senate_delay, max_reports=args.senate_max_reports,
                                       log=lambda m: print(m, file=sys.stderr)))
    return out


def parse_sources(text: str) -> list[str]:
    names = [n.strip().lower() for n in text.split(",") if n.strip()]
    bad = [n for n in names if n not in SOURCE_NAMES]
    if bad or not names:
        raise argparse.ArgumentTypeError(f"choose from {', '.join(SOURCE_NAMES)} (got {text!r})")
    return list(dict.fromkeys(names))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build web/data from disclosures and prices.")
    parser.add_argument("--since", default="2018-01-01", help="earliest transaction date (YYYY-MM-DD)")
    parser.add_argument("--sources", type=parse_sources, default=["house", "senate"],
                        help="comma-separated: house (Capitol API), senate (eFD). Default: both. "
                             "Sources left out keep their trades from the last build.")
    parser.add_argument("--capitol-url", default=config.capitol_api_url(),
                        help="Capitol API base URL (env CAPITOL_API_URL)")
    parser.add_argument("--capitol-file", help="read a saved Capitol API trades.json instead of the server")
    parser.add_argument("--senate-offline", action="store_true",
                        help="build Senate trades from the cached eFD reports only (no requests)")
    parser.add_argument("--senate-delay", type=float, default=1.0,
                        help="seconds between eFD requests (default 1)")
    parser.add_argument("--senate-max-reports", type=int, default=None,
                        help="download at most this many new eFD reports this run")
    parser.add_argument("--no-members", action="store_true",
                        help="don't add member metadata from congress-legislators")
    parser.add_argument("--refresh-members", action="store_true",
                        help="download the congress-legislators data even if the cache is fresh")
    parser.add_argument("--out", default=str(config.WEB_DATA_DIR), help="output folder")
    prices_group = parser.add_mutually_exclusive_group()
    prices_group.add_argument("--skip-prices", action="store_true", help="don't run the price step")
    prices_group.add_argument("--prices-only", action="store_true",
                              help="skip the trade fetch; update prices for the existing trades.json")
    parser.add_argument("--max-requests", type=int, default=None,
                        help="stop the price step after this many Tiingo requests")
    parser.add_argument("--refresh-prices", action="store_true",
                        help="refetch every ticker's full history instead of only new dates")
    args = parser.parse_args(argv)

    out_dir, cache_dir = Path(args.out), config.CACHE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.prices_only:
        return prices_only(out_dir, cache_dir, args)

    legislators = None
    if not args.no_members:
        legislators = members.load_legislators(cache_dir / "legislators.json", refresh=args.refresh_members,
                                               log=lambda m: print(m, file=sys.stderr))
    try:
        res = run(since=args.since, sources=make_sources(args, cache_dir), out_dir=out_dir,
                  cache_dir=cache_dir, legislators=legislators, all_sources=list(SOURCE_NAMES.values()))
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    meta = res.meta

    print(f"since={args.since}  sources fetched: {', '.join(res.fetched)}")
    for name, err in res.errors.items():
        print(f"error: {name}: {err}", file=sys.stderr)
    for name, n in res.carried.items():
        print(f"{name}: not fetched this run; kept {n} trades from the last build")
    for name in res.fetched:
        c = meta["countsBySource"][name]
        print(f"{name}: raw records {c['raw']}   kept {c['kept']}   excluded {c['excluded']}")
        for reason, n in c["excludedByReason"].items():
            print(f"  {n:>6}  {reason}")
        for key, n in sorted(meta.get("sourceStats", {}).get(name, {}).items()):
            print(f"  {key}: {n}")
    c = meta["counts"]
    print(f"total: kept: {c['kept']}   excluded: {c['excluded']}")
    for chamber, cov in meta["coverage"].items():
        print(f"{chamber}: {cov['trades']} trades, {cov['members']} members, "
              f"{cov['firstTxDate']} to {cov['lastTxDate']}")
    if "members" in meta:
        m = meta["members"]
        print(f"members: metadata for {m['resolved']} of {m['trades']} trades")
        for name, n in list(m["unresolved"].items())[:10]:
            print(f"  unresolved: {name} ({n} trades)")
    elif not args.no_members:
        print("members: congress-legislators data unavailable; using the sources' own metadata")
    print(f"wrote {out_dir / 'trades.json'} and meta.json ({len(meta['tickers'])} tickers)")
    code = 1 if res.errors else 0
    if args.skip_prices:
        return code
    return price_step(res.trades, meta, out_dir, cache_dir, args) or code


def price_step(trades: list[dict], meta: dict, out_dir: Path, cache_dir: Path, args) -> int:
    key = config.tiingo_api_key(required=False)
    client = prices.TiingoClient(key) if key else None
    if not key:
        print("TIINGO_API_KEY: MISSING. Publishing cached prices only; add the key to .env to fetch.")
    try:
        report = run_prices(trades, meta, out_dir=out_dir, cache_dir=cache_dir, client=client,
                            max_requests=args.max_requests, refresh=args.refresh_prices)
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print_price_report(report, client is not None)
    return 0


def prices_only(out_dir: Path, cache_dir: Path, args) -> int:
    try:
        trades = json.loads((out_dir / "trades.json").read_text(encoding="utf-8"))
        meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"error: --prices-only needs an existing trades.json and meta.json in {out_dir} ({err})",
              file=sys.stderr)
        return 1
    return price_step(trades, meta, out_dir, cache_dir, args)


if __name__ == "__main__":
    sys.exit(main())
