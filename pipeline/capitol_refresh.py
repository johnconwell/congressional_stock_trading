"""Bring a running Capitol API server up to date and wait until it has finished.

Used by the nightly GitHub Actions job (README phase 6), which starts Capitol API
next to the pipeline with its ``cache/`` folder restored from the previous run:

    python capitol_refresh.py --timeout-minutes 180

1. waits for ``GET /api/status`` to answer (the server may still be starting);
2. starts an incremental refresh (``POST /api/refresh``: only filings that aren't
   cached yet are downloaded and parsed) unless a fetch is already running, which
   is the case on the very first run, when the server starts with an empty cache;
3. polls ``/api/status`` until the fetch stops, printing progress now and then.

Exit codes: 0 the cache is complete, so House trades can be built from it;
1 the server couldn't be reached or the fetch failed; 2 the fetch was still running
at the timeout (the job then keeps the House trades of the previous build, and the
next run resumes where this one stopped); 3 the fetch stopped but the cache isn't
marked complete.

When ``GITHUB_OUTPUT`` is set, ``complete=true|false`` and ``trades=<n>`` are
written to it for later workflow steps.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable

import requests

import config

EXIT_COMPLETE, EXIT_ERROR, EXIT_TIMEOUT, EXIT_INCOMPLETE = 0, 1, 2, 3


class CapitolRefresher:
    def __init__(self, base_url: str, *, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self._sleep, self._clock, self._log = sleep, clock, log
        self.last_status: dict = {}

    def status(self) -> dict:
        resp = self.session.get(f"{self.base_url}/api/status", timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("unexpected /api/status response")
        self.last_status = body
        return body

    def wait_until_up(self, timeout: float) -> dict:
        deadline = self._clock() + timeout
        while True:
            try:
                return self.status()
            except (requests.RequestException, ValueError) as err:
                if self._clock() >= deadline:
                    raise RuntimeError(f"Capitol API at {self.base_url} didn't answer: {err}") from err
                self._sleep(2)

    def run(self, *, timeout: float, poll: float = 15, startup_timeout: float = 120,
            log_every: float = 300) -> int:
        try:
            st = self.wait_until_up(startup_timeout)
        except RuntimeError as err:
            self._log(f"error: {err}")
            return EXIT_ERROR

        if st.get("fetchRunning"):
            self._log(f"Capitol API is already fetching ({st.get('fetchProgress') or 'starting'}); waiting.")
        else:
            self._log(f"Capitol API cache: {st.get('totalTrades', 0)} trades, status "
                      f"{st.get('cacheStatus')}, updated {st.get('cacheLastUpdated')}. Refreshing.")
            try:
                self.session.post(f"{self.base_url}/api/refresh", timeout=30).raise_for_status()
            except requests.RequestException as err:
                self._log(f"error: POST /api/refresh failed: {err}")
                return EXIT_ERROR

        deadline = self._clock() + timeout
        next_log = self._clock() + log_every
        while True:
            self._sleep(poll)
            try:
                st = self.status()
            except (requests.RequestException, ValueError) as err:
                self._log(f"error: lost Capitol API: {err}")
                return EXIT_ERROR
            if not st.get("fetchRunning"):
                break
            if self._clock() >= deadline:
                self._log(f"Still fetching after {timeout / 60:.0f} min ({st.get('fetchProgress')}); "
                          "stopping here. The next run resumes from the saved cache.")
                return EXIT_TIMEOUT
            if self._clock() >= next_log:
                self._log(f"  {st.get('fetchProgress')} ({st.get('totalTrades', 0)} trades so far)")
                next_log = self._clock() + log_every

        if st.get("fetchError"):
            self._log(f"error: Capitol API fetch failed: {st['fetchError']}")
            return EXIT_ERROR
        if st.get("cacheStatus") != "complete":
            self._log(f"Capitol API stopped with cache status {st.get('cacheStatus')!r}.")
            return EXIT_INCOMPLETE
        self._log(f"Capitol API up to date: {st.get('totalTrades', 0)} trades. {st.get('fetchProgress') or ''}")
        return EXIT_COMPLETE


def write_github_output(code: int, status: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"complete={'true' if code == EXIT_COMPLETE else 'false'}\n")
        fh.write(f"trades={int(status.get('totalTrades') or 0)}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh a running Capitol API server and wait for it.")
    parser.add_argument("--url", default=config.capitol_api_url(), help="Capitol API base URL (env CAPITOL_API_URL)")
    parser.add_argument("--timeout-minutes", type=float, default=180,
                        help="give up waiting after this long (default 180)")
    parser.add_argument("--poll-seconds", type=float, default=15)
    args = parser.parse_args(argv)

    refresher = CapitolRefresher(args.url, log=lambda m: print(m, flush=True))
    code = refresher.run(timeout=args.timeout_minutes * 60, poll=args.poll_seconds)
    write_github_output(code, refresher.last_status)
    return code


if __name__ == "__main__":
    sys.exit(main())
