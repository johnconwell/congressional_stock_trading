# Congressional Stock Tracker

A browser-based app that rebuilds the combined stock portfolio of members of the U.S. Congress from their public trade disclosures. It charts how that portfolio's value changes over time and compares it with an S&P 500 benchmark that is funded with the same cash flows.

> **Disclaimer:** This is for research and education, not investment advice. Disclosures show dollar *ranges*, are filed up to 45 days after the trade and are sometimes wrong. The reconstructed portfolio is an estimate.

---

## 1. Functional description

### 1.1 What the user sees

1. **Controls panel**
   - **Start date.** Trades before this date are ignored. Both pools start empty on this date.
   - **End date.** Defaults to the latest available closing price.
   - **Amount estimator.** Disclosures give ranges such as `$15,001 – $50,000`. The user picks `Low`, `Midpoint` (the default) or `High`.
   - **Date basis.** Choose `Transaction date` (the default) or `Disclosure date`. Disclosure date shows what an outside observer could actually have copied.
   - **Member filters** (see 1.3): party, gender, age decade and geographic region, each with multiple selections allowed.
   - **Other filters** (later phase): chamber, individual members, and owner (self, spouse, joint, dependent).
2. **Main chart.** A line chart of daily values:
   - **Congress pool total value.** Stock holdings plus cash.
   - **S&P 500 pool total value.** SPY holdings plus cash.
   - Optional dashed lines for the *invested* part of each pool (total minus cash). Optional cumulative net contributions (total bought minus total sold), so gains and losses read relative to money put in.
3. **Summary cards.** Current value of each pool, total contributed, cash balance, absolute and % gain, and the difference between the two pools.
4. **Holdings table.** The Congress pool's current positions: ticker, shares, value, % of pool and the members who hold it.
5. **Trade log.** A sortable, searchable list of every trade applied, with the dollar amount used. Trades that were skipped are flagged with the reason, for example *no price data* or *sale with no matching holdings*.

### 1.2 Simulation rules

The two pools are rebuilt day by day from the start date to the end date.

**Congress pool**

| Event | Effect |
|---|---|
| Member **buys** ticker `T` for amount `A` on day `d` | Buy `A / close(T, d)` shares of `T`. The shares go to that member's position in `T`. |
| Member **sells** ticker `T` for amount `A` on day `d` | Sold value `S = min(A, value of the member's T position on d)`. Remove `S / close(T, d)` shares and add `S` to **cash**. |
| Member reports a **full sale** of `T` | Sell the member's whole `T` position, whatever amount was reported. |
| Each trading day | Pool value = Σ(shares × close) + cash. Cash keeps a constant value. |

**S&P 500 pool** (mirrors the Congress pool's cash flows)

| Event | Effect |
|---|---|
| Congress buy of amount `A` on day `d` | Buy `A / close(SPY, d)` shares of SPY. |
| Congress sell that produced sold value `S` | Convert `S` worth of SPY at `close(SPY, d)` to cash, capped at the SPY the pool holds. |
| Each trading day | Pool value = SPY shares × close(SPY) + cash. |

**Edge cases and design decisions**

- **Positions are tracked per member** (one ledger per member and ticker), and the pool total is the sum. A sale by member X therefore can't sell shares bought by member Y.
- **Sales with nothing to sell.** A member may sell a stock bought before the start date, or one the pool never saw. The sale is skipped and logged. The benchmark is not changed either, so both pools stay comparable.
- **The benchmark sells the realized amount `S`**, not the reported amount `A`. Both pools then have the same net contributions, unless the benchmark holds less SPY than `S`; it then sells all it has and the shortfall is logged (see 3.3).
- **Weekends and holidays.** A trade on a non-trading day executes at the next trading day's close.
- **Missing or delisted tickers.** If no price is available on the trade date, the trade is skipped and logged. If a held ticker stops reporting prices (delisting or merger), its last known close carries forward and the position is flagged.
- **Price series.** Split-adjusted closing prices are used, so a split doesn't look like a crash. Dividends are not reinvested in either pool by default. An optional *total return* mode would use dividend-adjusted prices for both pools.
- **Scope of assets.** Only exchange-listed stocks and ETFs with a usable ticker are included. Options, bonds, mutual funds, crypto, private holdings and "exchange" transactions are excluded in v1 and counted in the skipped log.
- **Amount ranges.** The top bracket is open-ended ("Over $50,000,000"), so its lower bound is used whichever estimator is selected.

---

## 2. Data sources

### 2.1 Congressional trades

Members, their spouses and dependent children must disclose securities transactions over $1,000 within 45 days (STOCK Act, 2012). Each record has the member, owner, ticker or asset description, transaction type, transaction date, disclosure date and an amount range.

| Source | Notes |
|---|---|
| **House Clerk Financial Disclosure portal** (official) | Periodic Transaction Reports (PTRs). There is a yearly index file, but many PTRs are PDFs that have to be parsed. This is the authoritative source. |
| **Senate eFD** (official) | Electronic PTRs as HTML, plus some scanned paper filings. |
| **Capitol API** (`crnicholson/capitol-api`, open source) | Self-hostable. Parses House PTRs from 2018 onward into JSON. A good first source for House data. |
| Free-tier commercial APIs (Disclosed Capitol, Bargo, FMP, etc.) | Clean JSON for both chambers, but they need a key, have rate limits and sometimes cap history. Bargo's free tier, for example, covers only about the last 3 months. They can be added later as adapters. |
| House/Senate Stock Watcher | **Do not depend on these.** House Stock Watcher is deprecated and its S3 archive has been reported as unreachable. |

Federal law limits how financial disclosure data can be used (no commercial use, for example). This project is non-commercial research. Check each source's terms before you publish the data again.

### 2.2 Daily closing prices

| Source | Notes |
|---|---|
| **Tiingo** (recommended) | Free account with an API key. Daily end-of-day data with raw and split- and dividend-adjusted closes, plus split and dividend factors. |
| Stooq / `yfinance` | Fallbacks that need no key. They are unofficial, so they may break or have gaps. |
| Alpha Vantage | **Not suitable** on the free tier: 25 requests a day, and daily history limited to the latest 100 data points. |

The benchmark uses **SPY** (SPDR S&P 500 ETF Trust).

---

## 3. Architecture

### 3.1 Why there are two parts

A page running in the browser can't reliably call these data sources itself:

- Most of them block cross-origin requests (CORS), and the official portals serve PDFs and HTML.
- API keys would be visible to anyone who views the page.
- Fetching thousands of price histories on every page load is slow and would exhaust rate limits.

The app is therefore split into an **offline data pipeline**, which produces static JSON files, and a **static web front end**, which loads those files and runs the simulation in the browser. The simulation runs on the client, so changing the start date, estimator or filters gives an instant result without calling a server.

```
┌──────────────────────── Data pipeline (Python, run on a schedule) ──────────────────────────┐
│                                                                                             │
│  sources/house.py ─┐                                                                        │
│  sources/senate.py ┼─► normalize.py ─► trades.json ─┐                                       │
│  sources/<api>.py ─┘   (tickers, types,             │                                       │
│                         amount ranges,              ├─► prices.py ─► prices/<TICKER>.json    │
│                         dedupe)                     │   (Tiingo, cached, incremental)       │
│                                                     └─► meta.json (build time, coverage,     │
│                                                          skipped-asset counts)              │
└──────────────────────────────────────────┬──────────────────────────────────────────────────┘
                                           │ static files committed to web/data/
┌──────────────────────────────────────────▼──────── Web app (static HTML/JS) ────────────────┐
│  index.html                                                                                 │
│  js/data.js     – fetches trades.json + only the price files needed for the chosen range   │
│  js/sim.js      – pure simulation engine (no DOM), unit-tested                              │
│  js/chart.js    – Chart.js time-series rendering                                            │
│  js/ui.js       – controls, summary cards, holdings table, trade log                        │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

Hosting: any static host, such as GitHub Pages or Netlify, or `python -m http.server` locally. A scheduled GitHub Actions workflow can run the pipeline every night, commit the refreshed data and redeploy.

### 3.2 Data formats

**`web/data/trades.json`**, normalized and sorted by transaction date:

```json
[
  {
    "id": "house-20012345-3",     // <chamber>-<source id>
    "chamber": "house",
    "member": "Jane Doe",
    "memberId": "D000001",      // bioguide ID
    "party": "D",
    "state": "CA",
    "district": 12,
    "gender": "F",
    "birthDate": "1960-01-01",
    "owner": "spouse",          // self | spouse | joint | dependent | self_or_dependent | other
    "ticker": "NVDA",           // Tiingo style: BRK.B -> BRK-B
    "assetName": "NVIDIA Corporation - Common Stock",
    "assetType": "stock",       // stock | etf | reit
    "type": "buy",              // buy | sell | sell_full
    "txDate": "2025-03-14",
    "discDate": "2025-04-02",
    "amountLow": 15001,
    "amountHigh": 50000,
    "flags": [],                // amount_bracket_inferred | amount_open_ended
    "source": "capitol-api",
    "filingId": "20012345",
    "sourceUrl": "https://..."
  }
]
```

**`web/data/prices/NVDA.json`**: compact columns to keep the files small. `close` is split-adjusted (not dividend-adjusted), rounded to 4 decimals, and starts at the ticker's first trade date (SPY starts at the earliest trade overall):

```json
{ "ticker": "NVDA", "dates": ["2025-01-02", "..."], "close": [138.31, "..."] }
```

The file name is `<TICKER>.json`, with one exception: Windows can't create files named after devices (`CON`, `PRN`, `AUX`, `NUL`, `COM0`–`COM9`, `LPT0`–`LPT9`), so those get a trailing underscore, e.g. `CON_.json`. `meta.json` lists these in `prices.fileNames`, and the web app must use that map.

**`web/data/meta.json`**: build timestamp, date coverage by chamber, number of raw, kept and excluded records, exclusion counts by reason, flag counts, the list of tickers, and `tickersWithoutPrices`. The price step adds a `prices` block:

| Key | Meaning |
|---|---|
| `lastDate` | SPY's latest close, i.e. the default end date |
| `files` / `fileNames` | number of price files; tickers whose file isn't `<TICKER>.json` |
| `pending` | tickers not fetched yet (Tiingo limit reached, `--max-requests` used up, or no API key) |
| `withoutPricesByReason` | `not_found` (Tiingo doesn't know the ticker), `no_data` (known, but no prices in range), `invalid_ticker` |
| `endedEarly` | ticker → last close, for tickers that stopped reporting more than 10 days before SPY (delisted, merged or renamed) |
| `errors`, `stopped` | per-ticker errors that will be retried; why fetching stopped early, if it did |
 The full list of excluded records goes to `pipeline/cache/excluded.json`, which isn't published, for checking the rules.

**Source data quirks (Capitol API).** Found while building phase 1, and handled in `normalize.py`:

- **Owner.** Capitol API writes `DC` (dependent child) whenever it can't read the owner column. In a House PTR a blank owner means the member, so most `DC` rows are really the member's own trades. These are stored as `self_or_dependent`. Only `SP` (spouse) and `JT` (joint) are reliable.
- **Truncated amounts.** When a range wraps onto two lines in the PDF, only the floor survives (`$15,001`). A floor that matches a standard bracket is expanded to the full bracket and flagged `amount_bracket_inferred`. That was about 18% of kept trades in the sample.
- **Amendments.** Records carry no amendment flag. A trade that appears identically (member, owner, ticker, type, date, amount) in more than one filing is kept only from the latest filing. Identical rows inside one filing are separate lots and are all kept. An amendment that *changes* a trade can't be matched to the original, so both copies stay.
- **Dates.** A few transaction dates fall after the filing date (typos). Those trades are excluded as `tx_after_disclosure`.

**Price cache.** `pipeline/cache/prices/<TICKER>.json` keeps Tiingo's *raw* rows (close, adjClose, splitFactor, divCash). Each run only requests dates after the last cached one, then rebuilds the split-adjusted series from the full raw history, so a new split also re-adjusts older prices. The cached `adjClose` and `divCash` are there for the later total-return mode. A ticker fetched in the last 12 hours is skipped; an unknown ticker is asked about again after 30 days. `--refresh-prices` refetches everything (to pick up corrections Tiingo makes to old data).

**Tiingo limits.** The free plan allows 50 requests an hour, 1,000 a day and **500 unique symbols a month**. Each ticker costs one request per run. The price step fetches SPY first, then tickers in order of dollar volume traded, and stops cleanly when Tiingo refuses a request. Everything fetched so far is cached, so the next run continues where it stopped. With the current ~1,160 tickers this means the free plan can't fill the cache in one month or keep every ticker updated daily; the paid Power plan removes the limit.

### 3.3 Simulation engine (`js/sim.js`)

It is a pure function, so it is easy to test and has no side effects. It never changes its inputs.

```
simulate(trades, prices, { start, end, estimator, dateBasis, filters, benchmark, staleDays })
  → { dates[], congressValue[], congressCash[], spyValue[], spyCash[],
      contributions[], congressNetFlow[], spyNetFlow[],
      holdings[], tradeLog[], summary }
```

| Option | Values | Default |
|---|---|---|
| `start`, `end` | ISO dates, inclusive | first / last SPY date |
| `estimator` | `low`, `mid`, `high` | `mid` |
| `dateBasis` | `transaction`, `disclosure` | `transaction` |
| `filters` | lists for `party`, `gender`, `ageDecade` (age on the trade date, e.g. `60` = 60–69), `region` (Census regions; DC is South, territories are `Territories`), `chamber`, `members` (bioguide IDs), `owner`, `tickers`. An empty list means no restriction. | none |
| `benchmark` | ticker whose dates are the trading calendar | `SPY` |
| `staleDays` | calendar days without a close before a held ticker is flagged `ended` | `10` |

`prices` is `{ TICKER: { dates, close } }`, i.e. the price files keyed by ticker.

How it works:

1. Filter the trades by date range (on the chosen date basis) and the user's filters, then move each one to its effective trading day (the next SPY date on or after it).
2. Build the trading calendar from SPY's price dates.
3. For each trading day: apply that day's trades in order (buys before sells, so a member's same-day round trip resolves correctly). Then value every position at that day's close, carrying the last known price forward when one is missing.
4. Return the time series and the final ledgers.

**Contributions.** Sale proceeds stay in the pool as cash, and buys are always new money (they don't draw down cash). So:

- `contributions` is the cumulative dollars bought. It is identical for both pools by construction, and `gain = value − contributions`.
- `congressNetFlow` / `spyNetFlow` are contributions minus realized sales, the "total bought minus total sold" line in 1.1. They are equal except when the benchmark can't sell the full `S` because it holds less SPY than that. This happens when the Congress positions have outperformed SPY by enough. The unsold part is logged per trade as `spyShortfall` and totalled in `summary.spyShortfall`.

**Trade log entries** carry `basisDate`, effective `date`, `amount` (from the estimator), `status` (`applied` / `skipped`), `reason`, `price`, `shares` (negative for sales), `value` (dollars bought, or realized `S`), `capped` (the sale was reduced to the position's value), `spyShares` and `spyShortfall`. Skip reasons: `no_price_data` (no price file), `no_price_on_date` (outside the ticker's price range, e.g. after delisting), `no_holdings`, `no_trading_day` (no SPY close yet), `after_end` (a weekend trade whose next session is after the end date), `bad_amount`. Trades removed by the user's filters are not logged.

**Holdings** list each open ticker with `shares`, `price`, `priceDate`, `value`, `weight` (share of the whole pool including cash), `ended` and the members holding it. **`summary`** has final value, cash, invested, gain and gain % for each pool, the difference, applied/skipped counts by reason, the total benchmark shortfall and the list of ended tickers.

A price gap inside a ticker's range (a missing day) uses the previous close, both for valuation and for executing a trade.

Complexity is O(days × open positions). On the current data (7,765 trades, 911 trading days) a full run takes under 100 ms in Node.

### 3.4 Technology choices

| Layer | Choice | Reason |
|---|---|---|
| Pipeline | Python 3.11+, `requests`, `pdfplumber` (for House PDFs), `pandas` (optional) | Mature tools for scraping and PDF parsing |
| Front end | Plain HTML, CSS and ES modules, with no build step | Simple to host and maintain |
| Charting | Chart.js with a date adapter (vendored) | Light and handles time series well |
| Tests | `pytest` (pipeline) and `vitest` or Node's built-in test runner (`sim.js`) | The simulation rules are the core logic and need fixture tests |
| Automation | GitHub Actions on a nightly cron | Keeps the data fresh without a server |

### 3.5 Proposed repository layout

```
congressional_stock_tracker/
├── README.md
├── pipeline/
│   ├── requirements.txt
│   ├── build.py              # entry point: fetch → normalize → prices → write web/data
│   ├── config.py             # API keys from env vars (TIINGO_API_KEY), paths
│   ├── sources/
│   │   ├── base.py           # TradeSource interface
│   │   ├── capitol_api.py
│   │   ├── house_clerk.py
│   │   └── senate_efd.py
│   ├── normalize.py          # ticker cleanup, type mapping, amount-range parsing, dedupe
│   ├── prices.py             # Tiingo client + on-disk cache, incremental updates
│   └── tests/
├── web/
│   ├── index.html
│   ├── css/style.css
│   ├── js/{data,sim,chart,ui}.js
│   ├── vendor/chart.umd.min.js
│   └── data/                 # generated; committed for static hosting
├── tests/sim.test.js
└── .github/workflows/refresh-data.yml
```

---

## 4. Implementation plan

### Phase 0: Project setup
- [x] Create the repository layout, `.gitignore` (cache folders, `.env`) and `requirements.txt`.
- [ ] Get a Tiingo API key and store it in `.env` / repository secrets as `TIINGO_API_KEY`. (`.env.example` is the template; `python pipeline/build.py` reports whether the key is found.)

### Phase 1: Trade data pipeline
- [x] Define the `TradeSource` interface and the normalized trade schema (section 3.2).
- [x] Write the first adapter, **Capitol API** (House, 2018 onward).
- [x] `normalize.py`: map transaction types (`P` → buy, `S (partial)` → sell, `S` → sell_full, `E` → excluded), parse amount ranges, clean up tickers (strip suffixes, map class shares such as `BRK.B`), drop non-equity assets and record why, and remove trades filed by both an original and an amended PTR.
- [x] Write `trades.json` and `meta.json`.
- [x] Unit tests with recorded fixture responses.

### Phase 2: Price pipeline
- [x] `prices.py`: fetch split-adjusted daily closes from Tiingo for every traded ticker plus SPY, from the earliest trade date to today.
- [x] Local cache with incremental updates (only fetch dates after the last cached one), retries and rate limiting.
- [x] Record tickers that can't be found in `meta.json`.

### Phase 3: Simulation engine
- [x] Write `sim.js` following the rules in section 1.2.
- [x] Fixture tests (`tests/sim.test.js`, run with `npm test`): a single buy; buy then partial sell; sell with no holdings (skipped); full sale; weekend trade; split day; delisted ticker; two members trading the same ticker; checking that both pools always have equal net contributions.

### Phase 4: Web UI
- [ ] `index.html` with the controls panel, chart, summary cards, holdings table and trade log.
- [ ] Load data on demand: fetch only the price files for tickers in the filtered trade set.
- [ ] Chart.js rendering with tooltips, zoom/pan (optional) and a log-scale toggle.
- [ ] Store the chosen settings in the URL query string so a view can be shared.

### Phase 5: Senate data and filters
- [ ] Senate eFD adapter, or a free-tier API adapter for both chambers.
- [ ] Member metadata (party, state, chamber) and filter controls.
- [ ] Per-member view: the same chart restricted to one member.

### Phase 6: Automation and deployment
- [ ] GitHub Actions workflow that runs `pipeline/build.py` every night, commits `web/data/` and deploys to GitHub Pages.
- [ ] Freshness indicator in the UI ("Data as of …").

### Possible later additions
- Total-return mode (dividends reinvested in both pools).
- A "follow the disclosures" mode that trades on the disclosure date, showing what a copycat investor could have achieved.
- Other benchmarks (QQQ, an equal-weight index).
- Attribution: which members, sectors or tickers contributed most to the gap with the benchmark.

---

## 5. Running locally (once implemented)

```bash
# 0. Run Capitol API (separate folder). Set YEARS_START=2018 in its .env for full history;
#    the first start parses every PTR PDF and takes a while.
git clone https://github.com/crnicholson/capitol-api && cd capitol-api && npm install && npm start

# 1. Build data
cd pipeline
pip install -r requirements.txt
export TIINGO_API_KEY=...        # Windows PowerShell: $env:TIINGO_API_KEY="..."
python build.py --since 2018-01-01                         # reads CAPITOL_API_URL (default localhost:3000)
python build.py --capitol-file ../../capitol-api/cache/trades.json   # or use its cache file directly
python build.py --prices-only            # continue the price step after a Tiingo limit (reuses trades.json)
python build.py --skip-prices ...        # trades only, no Tiingo calls
python build.py --prices-only --max-requests 45   # stay under the free plan's hourly limit

# Tests
python -m pytest      # pipeline
npm test              # simulation engine (Node 20+, from the repository root)

# 2. Serve the web app
cd ../web
python -m http.server 8000
# open http://localhost:8000
```
