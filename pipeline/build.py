"""Pipeline entry point: fetch -> normalize -> (prices, phase 2) -> write web/data.

    python build.py --since 2018-01-01                       # from a running Capitol API
    python build.py --capitol-file path/to/trades.json       # from a saved response
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import config
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
        "tickersWithoutPrices": [],  # filled in by the price step (phase 2)
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


def run(*, since: str | None, source: CapitolApiSource, out_dir: Path, cache_dir: Path) -> dict:
    raws = list(source.fetch(since))
    result = normalize(raws, since=since)
    meta = build_meta(result, since=since, sources=[source.name], raw_count=len(raws))
    write_trades(result.trades, out_dir / "trades.json")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    write_excluded(result, cache_dir / "excluded.json")
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build web/data from disclosures and prices.")
    parser.add_argument("--since", default="2018-01-01", help="earliest transaction date (YYYY-MM-DD)")
    parser.add_argument("--capitol-url", default=config.capitol_api_url(),
                        help="Capitol API base URL (env CAPITOL_API_URL)")
    parser.add_argument("--capitol-file", help="read a saved Capitol API trades.json instead of the server")
    parser.add_argument("--out", default=str(config.WEB_DATA_DIR), help="output folder")
    args = parser.parse_args(argv)

    out_dir, cache_dir = Path(args.out), config.CACHE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    source = CapitolApiSource(
        args.capitol_url, file=args.capitol_file, cache_path=cache_dir / "capitol_api.json"
    )
    try:
        meta = run(since=args.since, source=source, out_dir=out_dir, cache_dir=cache_dir)
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
    has_key = config.tiingo_api_key(required=False) is not None
    print(f"TIINGO_API_KEY: {'set' if has_key else 'MISSING'} (needed for the price step, phase 2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
