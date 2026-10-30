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
   - **Member filters**: chamber, party, gender, age decade, geographic region, state and specific members, each with multiple selections allowed.
   - **Owner filter**: self, spouse, joint, dependent (whose account the trade was in).
   - **One member**: a search box that opens the per-member view (see 1.1 item 6).
2. **Main chart.** A line chart of daily values:
   - **Congress pool total value.** Stock holdings plus cash.
   - **S&P 500 pool total value.** SPY holdings plus cash.
   - Optional dashed lines for the *invested* part of each pool (total minus cash). Optional cumulative net contributions (total bought minus total sold), so gains and losses read relative to money put in.
3. **Summary cards.** Current value of each pool, total contributed, cash balance, absolute and % gain, and the difference between the two pools.
4. **Holdings table.** The Congress pool's current positions: ticker, shares, value, % of pool and the members who hold it.
5. **Trade log.** A sortable, searchable list of every trade applied, with the dollar amount used. Trades that were skipped are flagged with the reason, for example *no price data* or *sale with no matching holdings*.
6. **Per-member view.** The same chart, cards and tables restricted to one member's trades, with a profile (party, seat, chamber, age, number of trades, dates) and a link to the member's congressional biography. Member names in the holdings table and trade log open it; *All members* (or the browser's Back button) returns.

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
| **Senate eFD** (official) | Electronic PTRs as HTML, plus some scanned paper filings. **Used** (phase 5, `sources/senate_efd.py`). |
| **Capitol API** (`crnicholson/capitol-api`, open source) | Self-hostable. Parses House PTRs from 2018 onward into JSON. **Used** for House data (phase 1). |
| Free-tier commercial APIs (Disclosed Capitol, Bargo, FMP, etc.) | Clean JSON for both chambers, but they need a key, have rate limits and sometimes cap history. Bargo's free tier, for example, covers only about the last 3 months. They can be added later as adapters. |
| House/Senate Stock Watcher | **Do not depend on these.** House Stock Watcher is deprecated and its S3 archive has been reported as unreachable. |

### 2.2 Member metadata

Party, state, district, gender and birth date come from **`unitedstates/congress-legislators`** (public domain, on GitHub), which lists every member's bioguide ID, biography and terms served. See 3.2 *Member metadata*.

Federal law limits how financial disclosure data can be used (no commercial use, for example). This project is non-commercial research. Check each source's terms before you publish the data again.

### 2.3 Daily closing prices

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
│  sources/capitol_api.py ─┐                                                                  │
│  sources/senate_efd.py  ─┼─► normalize.py ─► members.py ─► trades.json ─┬─► prices.py       │
│  sources/<api>.py       ─┘   (tickers, types, (party, state,           │   (Tiingo, cached) │
│                               amounts,        gender, birth            │   ► prices/*.json  │
│                               dedupe)         date)                    └─► meta.json        │
└──────────────────────────────────────────┬──────────────────────────────────────────────────┘
                                           │ static files committed to web/data/
┌──────────────────────────────────────────▼──────── Web app (static HTML/JS) ────────────────┐
│  index.html                                                                                 │
│  js/data.js     – fetches trades.json + only the price files needed for the chosen range   │
│  js/sim.js      – pure simulation engine (no DOM), unit-tested                              │
│  js/chart.js    – Chart.js time-series rendering                                            │
│  js/ui.js       – controls, summary cards, holdings table, trade log                        │
│  js/state.js    – view settings <-> URL query string (pure, unit-tested)                    │
│  js/members.js  – one record per member, for the member view and search (pure, unit-tested) │
│  js/freshness.js – "Data as of …" line and out-of-date warning (pure, unit-tested)          │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

Hosting: any static host, such as GitHub Pages or Netlify, or `python -m http.server` locally. A scheduled GitHub Actions workflow runs the pipeline every night, commits the refreshed data and redeploys to GitHub Pages (section 6).

### 3.2 Data formats

**`web/data/trades.json`**, normalized and sorted by transaction date:

```json
[
  {
    "id": "house-20012345-3",     // <chamber>-<source id>; Senate: senate-<report uuid>-<row>
    "chamber": "house",
    "member": "Jane Doe",
    "memberId": "D000001",      // bioguide ID (null if the filer couldn't be matched)
    "party": "D",               // D | R | I | L, as of the transaction date
    "state": "CA",              // as of the transaction date
    "district": 12,             // House only; 0 = at-large; null for the Senate
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
    "source": "capitol-api",    // capitol-api | senate-efd
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

**`web/data/meta.json`**: build timestamp, date coverage by chamber, number of raw, kept and excluded records (`counts` in total, `countsBySource` per source), exclusion counts by reason, flag counts, the list of tickers, and `tickersWithoutPrices`. Phase 5 adds:

| Key | Meaning |
|---|---|
| `members` | `resolved` / `trades` with member metadata, and `unresolved` (filer → number of trades) |
| `sourceStats` | per-source counters, e.g. eFD reports listed, downloaded, paper reports skipped |
| `carriedOver` | source → trades kept from the previous build because the source wasn't fetched this run |
| `sourceErrors` | source → error message, when a source's fetch failed (its old trades were kept) |
| `buildRun` | `{url, event}` of the GitHub Actions run that built the data (only when built in Actions) |

The price step adds a `prices` block:

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

**Senate eFD.** The adapter accepts the site's usage agreement (a form post with a CSRF token), lists Periodic Transaction Reports through the search page's JSON endpoint (`/search/report/data/`, 100 per request) and reads each electronic report's HTML table. Notes:

- **Paper filings** (scanned images) are counted in `sourceStats` and skipped.
- **Wording.** eFD writes values out (`Purchase`, `Sale (Partial)`, `Spouse`, `Stock`). `normalize.py` maps them; a plain `Sale` is treated as a partial sale. Asset types other than `Stock` (`Stock Option`, `Corporate Bond`, `Municipal Security`, `Other Securities`, `Cryptocurrency`, ...) are excluded, like their House codes. `Other Securities` includes some ETFs, but also funds and notes, so it isn't kept.
- **Amendments** are new reports with their own uuid; the same duplicate rule as the House applies.
- **Cache.** A filed report never changes, so each report page is saved in `pipeline/cache/senate_efd/ptr/` and only new reports are downloaded. The first full run (2018 onward, a few thousand reports at one request a second) takes about an hour; later runs take a minute. `--senate-offline` rebuilds from the cache with no requests.

**Member metadata.** `members.py` downloads the congress-legislators data once a week (`pipeline/cache/legislators.json`, members serving since 2012). For House trades it uses the bioguide ID from Capitol API. Senate filings only name the filer (`Thomas H` `Tuberville`, office `Tuberville, Tommy (Senator)`), so the name is matched to a senator serving within a year of the trade: by last name, then first name, nickname or middle name if two senators share a last name (the two Scotts). `ALIASES` in `members.py` fixes names the rules miss. Party, state and district are taken **as of the trade date**, so a party switch (Sinema, Manchin) or a move from the House to the Senate (Mullin) is reflected trade by trade. If the download fails the stale cache is used; with no cache at all, trades keep the sources' own metadata.

**Several sources.** Each run fetches the sources in `--sources` (default `house,senate`). A source that isn't fetched, or whose fetch fails, keeps its trades from the previous `trades.json`, so the Capitol API server being down doesn't erase the House data. A failed source makes `build.py` exit with code 1 after writing the rest.

**Price cache.** `pipeline/cache/prices/<TICKER>.json` keeps Tiingo's *raw* rows (close, adjClose, splitFactor, divCash). Each run only requests dates after the last cached one, then rebuilds the split-adjusted series from the full raw history, so a new split also re-adjusts older prices. The cached `adjClose` and `divCash` are there for the later total-return mode. A ticker fetched in the last 12 hours is skipped; an unknown ticker is asked about again after 30 days. `--refresh-prices` refetches everything (to pick up corrections Tiingo makes to old data).

**Tiingo limits.** The free plan allows 50 requests an hour, 1,000 a day and **500 unique symbols a month**. Each ticker costs one request per run. The price step fetches SPY first, then tickers that have no cached prices yet (largest dollar volume first), then cached tickers, least recently fetched first; it stops cleanly when Tiingo refuses a request. Everything fetched so far is cached, so the next run continues where it stopped, and a nightly run with a small budget cycles through every ticker instead of refreshing the same top ones. With the current ~1,160 tickers this means the free plan can't fill the cache in one month or keep every ticker updated daily; the paid Power plan removes the limit.

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
| `filters` | lists for `party`, `gender`, `ageDecade` (age on the trade date, e.g. `60` = 60–69), `region` (Census regions; DC is South, territories are `Territories`), `state`, `chamber`, `members` (bioguide IDs, or names for unmatched filers), `owner`, `tickers`. An empty list means no restriction. | none |
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
│   │   └── senate_efd.py
│   ├── normalize.py          # ticker cleanup, type mapping, amount-range parsing, dedupe
│   ├── members.py            # party/state/gender/birth date from congress-legislators
│   ├── prices.py             # Tiingo client + on-disk cache, incremental updates
│   ├── capitol_refresh.py    # CI helper: refresh a running Capitol API and wait for it
│   └── tests/
├── web/
│   ├── index.html
│   ├── css/style.css
│   ├── js/{data,sim,chart,ui,state,members,freshness}.js
│   ├── vendor/               # Chart.js, date-fns adapter, zoom plugin, Hammer.js (see VERSIONS.md)
│   └── data/                 # generated; committed for static hosting
├── tests/{sim,web}.test.js
└── .github/workflows/
    ├── refresh-data.yml      # nightly: tests → build → commit web/data → deploy
    ├── deploy-pages.yml      # publish web/ to GitHub Pages (also on pushes to web/)
    └── test.yml              # pytest + npm test on pushes and pull requests
```

### 3.6 Web UI (`web/`)

- **Loading.** `data.js` fetches `meta.json` and `trades.json` once, then only the price files for tickers in the trades that pass the current date range and member filters, eight at a time, cached for the session. Tickers listed in `meta.prices.pending` or without prices are never requested; their trades show up in the log as *no price data*, and a notice above the cards says how many tickers have prices so far.
- **Controls.** Start/end date (bounded by SPY's dates), estimator, date basis, multi-select chips for chamber, party, gender, age decade, region and owner, and search-and-add lists for states and specific members. The options come from the trade data. Any change re-runs the simulation (a few ms on the full data set).
- **Per-member view.** `?member=<bioguide ID>` restricts the run to one member (plus the owner filter). The group filters stay in the URL but are switched off, so *All members* restores them. Opening and leaving the view adds a browser history entry; other changes replace the current one.
- **Chart.** Congress and S&P 500 pool totals; optional dashed *invested* lines and the net contributions line; log-scale toggle (zero values become gaps). Drag to zoom, Shift+drag to pan, Ctrl+scroll to zoom, and a *Reset zoom* button.
- **Tables.** Holdings and trade log sort by clicking a column header. The trade log can be searched by member, ticker or skip reason, filtered by status and is paged 100 rows at a time. Trades moved to the next trading day show the reported date under the effective one.
- **Data freshness.** The header reads *Data as of <latest close> · Disclosures through House …, Senate … · Updated 6 hours ago*. *Updated* links to the GitHub Actions run that built the data. An *Out of date* badge appears when the last build is more than 48 hours old or the latest close is more than 5 calendar days old (a weekend or holiday doesn't trigger it); the tooltip says which.
- **Shareable links.** Settings that differ from the defaults go in the query string, e.g. `?start=2024-01-02&est=high&chamber=senate&party=R&age=60,70&state=TX,FL&owner=spouse&scale=log&show=invested,netflow`, or `?member=T000278` for a member view. Dates equal to the data's first/last day are left out, so a link without dates always shows the latest data. *Copy link* copies the current URL.

The page needs a web server (ES modules and `fetch` don't work from `file://`).

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
- [x] `index.html` with the controls panel, chart, summary cards, holdings table and trade log.
- [x] Load data on demand: fetch only the price files for tickers in the filtered trade set.
- [x] Chart.js rendering with tooltips, zoom/pan and a log-scale toggle.
- [x] Store the chosen settings in the URL query string so a view can be shared.
- [x] Tests for the URL state and data loader (`tests/web.test.js`).

### Phase 5: Senate data and filters
- [x] Senate eFD adapter (`sources/senate_efd.py`) with a page cache and an offline mode; `build.py --sources` combines sources and keeps the trades of any source not fetched.
- [x] Member metadata from congress-legislators (`members.py`): Senate filers matched to bioguide IDs; party, state and district as of the trade date. Filter controls for chamber, state, specific members and owner.
- [x] Per-member view: the same chart restricted to one member, with a profile and shareable `?member=` links.
- [x] Tests: eFD fixtures (`test_senate_efd.py`), name matching and enrichment (`test_members.py`), multi-source builds (`test_build.py`), URL state and member index (`tests/web.test.js`).
- [ ] Run the first full Senate fetch (`python build.py`, about an hour the first time) and check `members.unresolved` in `meta.json`.

### Phase 6: Automation and deployment
- [x] GitHub Actions workflow that runs `pipeline/build.py` every night, commits `web/data/` and deploys to GitHub Pages (`refresh-data.yml`, `deploy-pages.yml`, `test.yml`; see section 6). Capitol API runs inside the job (`capitol_refresh.py`); pipeline and Capitol API caches persist in the Actions cache.
- [x] Price fetch order rotates (stalest first), so the free Tiingo budget covers every ticker over successive nights.
- [x] Freshness indicator in the UI ("Data as of …", *Out of date* badge; `js/freshness.js`, tested in `tests/web.test.js`).
- [ ] One-time GitHub setup (section 6.1) and a first manual run.

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
python build.py --since 2018-01-01                         # House (CAPITOL_API_URL, default localhost:3000) + Senate eFD
python build.py --capitol-file ../../capitol-api/cache/trades.json   # or use its cache file directly
python build.py --prices-only            # continue the price step after a Tiingo limit (reuses trades.json)
python build.py --skip-prices ...        # trades only, no Tiingo calls
python build.py --sources senate         # Senate only; House trades are kept from the last build
python build.py --sources senate --senate-max-reports 500   # spread the first eFD download over several runs
python build.py --senate-offline ...     # Senate from the cached eFD reports, no requests
python build.py --prices-only --max-requests 45   # stay under the free plan's hourly limit

# Tests
python -m pytest      # pipeline
npm test              # simulation engine (Node 20+, from the repository root)

# 2. Serve the web app
cd ../web
python -m http.server 8000
# open http://localhost:8000
```

---

## 6. Automation and deployment (GitHub Actions)

### 6.1 One-time setup

1. Push the repository to GitHub (the workflows assume the default branch is `main`).
2. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
3. **Settings → Secrets and variables → Actions → Secrets:** add `TIINGO_API_KEY`. Without it the run still builds trades and republishes the cached prices.
4. Optional **variables** on the same page:

   | Variable | Effect | Default |
   |---|---|---|
   | `TIINGO_MAX_REQUESTS` | stop the price step after this many requests | until Tiingo refuses |
   | `CAPITOL_API` | `off` skips the House refresh; House trades are kept from the last build | on |
   | `CAPITOL_API_REF` | Capitol API branch, tag or commit to run | `master` |
   | `CAPITOL_TIMEOUT_MINUTES` | how long to wait for Capitol API to parse new PTRs | `150` |

5. **Actions → Refresh data → Run workflow** for the first run, instead of waiting for the schedule.

### 6.2 What runs

| Workflow | When | What it does |
|---|---|---|
| `refresh-data.yml` | daily 09:23 UTC, or by hand | tests → restore caches → start Capitol API and refresh it → `build.py` → save caches → commit `web/data/` to `main` → deploy |
| `deploy-pages.yml` | pushes to `main` that touch `web/`, by hand, or called by the refresh | tests (unless called by the refresh) → publish `web/` to GitHub Pages |
| `test.yml` | pushes and pull requests | `pytest` and `npm test` |

A run started by hand can choose its sources (e.g. `senate`), a Tiingo request limit, and `refresh_prices` (refetch all history; needs a paid plan).

**House data in CI.** The job checks out `crnicholson/capitol-api`, restores its `cache/` folder from the previous run, starts it with `YEARS_START=2018` and the current year as `YEARS_END`, and calls `capitol_refresh.py`. That posts `/api/refresh` (only filings not yet cached are downloaded) and polls `/api/status` until the fetch stops. Only a cache marked `complete` is used (`--capitol-file`); otherwise the House trades of the previous build are kept.

**Caches.** `pipeline/cache/` (Tiingo raw prices, eFD report pages, congress-legislators) and Capitol API's `cache/` are kept in the Actions cache, not in git. They are saved even when a step fails, so an interrupted run is resumed by the next one. GitHub drops cache entries that go unused for 7 days; if the schedule is paused that long, the next run starts the caches from scratch (slow, but nothing is lost from `web/data/`).

**Failures.** A source that fails keeps its previous trades (section 3.2), the rest is still committed and deployed, and the run is then marked failed so GitHub emails you. A Capitol API refresh that runs out of time is only a warning: that's expected on the first runs.

### 6.3 What to expect on the first runs

- **Capitol API** has to parse every House PTR PDF since 2018. That likely takes several nightly runs at 150 minutes each; each run continues where the last stopped. Until it finishes, the House trades committed from your machine are kept.
- **Senate eFD** downloads a few thousand reports on the first run (about an hour), then only new ones.
- **Tiingo free plan:** about 50 requests per run before the hourly limit, so roughly 50 tickers are refreshed each night, SPY and new tickers first, then the stalest. The *pending* notice in the app shows how far the price cache has got. The paid Power plan refreshes everything every night.
- `web/data/meta.json` changes on every build (`builtAt`), so there is one data commit a day.

