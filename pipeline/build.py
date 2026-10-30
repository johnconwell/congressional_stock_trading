"""Pipeline entry point: fetch -> normalize -> prices -> write web/data.

    python build.py --since 2018-01-01                       # from a running Capitol API
    python build.py --capitol-file path/to/trades.json       # from a saved response
    python build.py --prices-only                            # reuse web/data/trades.json
    python build.py --capitol-file ... --skip-prices         # trades only, no Tiingo calls
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import config
import prices
from normalize import NormalizeResult, normalize
from sources import CapitolApiSource


def write_trades(trades: list[dict], path: Path) -> None:
    """One trade per line, so git diffs of refreshed data stay readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(t, ensure_ascii=False, separators=(",", ":")) for t in trades]
    path.write_text("[\n" + ",\n".join(lines) + "\n]\n", encoding="utf-8")


def build_meta(result: NormalizeResult, *, since: str | None, sources: list[str], raw_count: int) -> dict:
    coverage: dict[str, dict] = {}
    for t in result.trades:
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

    flags: dict[str, int] = {}
    for t in result.trades:
        for f in t["flags"]:
            flags[f] = flags.get(f, 0) + 1

    return {
        "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since,
        "sources": sources,
        "counts": {
            "raw": raw_count,
            "kept": len(result.trades),
            "excluded": len(result.excluded),
            "excludedByReason": result.excluded_by_reason(),
            "flags": flags,
        },
        "coverage": coverage,
        "tickers": sorted({t["ticker"] for t in result.trades}),
        "tickersWithoutPrices": [],  # filled in by the price step
    }


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


def run(*, since: str | None, source: CapitolApiSource, out_dir: Path, cache_dir: Path) -> tuple[dict, list[dict]]:
    raws = list(source.fetch(since))
    result = normalize(raws, since=since)
    meta = build_meta(result, since=since, sources=[source.name], raw_count=len(raws))
    write_trades(result.trades, out_dir / "trades.json")
    write_meta(meta, out_dir)
    write_excluded(result, cache_dir / "excluded.json")
    return meta, result.trades


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build web/data from disclosures and prices.")
    parser.add_argument("--since", default="2018-01-01", help="earliest transaction date (YYYY-MM-DD)")
    parser.add_argument("--capitol-url", default=config.capitol_api_url(),
                        help="Capitol API base URL (env CAPITOL_API_URL)")
    parser.add_argument("--capitol-file", help="read a saved Capitol API trades.json instead of the server")
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

    source = CapitolApiSource(
        args.capitol_url, file=args.capitol_file, cache_path=cache_dir / "capitol_api.json"
    )
    try:
        meta, trades = run(since=args.since, source=source, out_dir=out_dir, cache_dir=cache_dir)
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    c = meta["counts"]
    print(f"since={args.since}  source={'file ' + args.capitol_file if args.capitol_file else args.capitol_url}")
    print(f"raw records: {c['raw']}   kept: {c['kept']}   excluded: {c['excluded']}")
    for reason, n in c["excludedByReason"].items():
        print(f"  {n:>6}  {reason}")
    for chamber, cov in meta["coverage"].items():
        print(f"{chamber}: {cov['trades']} trades, {cov['members']} members, "
              f"{cov['firstTxDate']} to {cov['lastTxDate']}")
    print(f"wrote {out_dir / 'trades.json'} and meta.json ({len(meta['tickers'])} tickers)")
    if args.skip_prices:
        return 0
    return price_step(trades, meta, out_dir, cache_dir, args)


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
