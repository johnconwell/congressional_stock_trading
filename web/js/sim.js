// sim.js: pure simulation engine (README sections 1.2 and 3.3).
//
// No DOM access and no side effects, so it runs unchanged in the browser and
// under `node --test`.
//
//   simulate(trades, prices, options) -> result
//
// trades   normalized records from web/data/trades.json (README 3.2)
// prices   { [ticker]: { dates: [...], close: [...] } }, must include the
//          benchmark (SPY by default), whose dates are the trading calendar
// options  {
//   start, end           ISO dates (inclusive). Defaults: first / last benchmark date.
//   estimator            'low' | 'mid' (default) | 'high'
//   dateBasis            'transaction' (default) | 'disclosure'
//   filters              see matchesFilters()
//   benchmark            ticker of the benchmark, default 'SPY'
//   staleDays            calendar days without a close before a held ticker is
//                        flagged as ended (delisted/merged). Default 10.
// }

export const DEFAULTS = Object.freeze({
  estimator: 'mid',
  dateBasis: 'transaction',
  benchmark: 'SPY',
  staleDays: 10,
});

export const SKIP_REASONS = Object.freeze({
  NO_PRICE_DATA: 'no_price_data',       // no price file for the ticker at all
  NO_PRICE_ON_DATE: 'no_price_on_date', // file exists but doesn't cover the trade day
  NO_HOLDINGS: 'no_holdings',           // sale with no matching holdings
  NO_TRADING_DAY: 'no_trading_day',     // no benchmark close on or after the trade date
  AFTER_END: 'after_end',               // next trading day falls after the end date
  BAD_AMOUNT: 'bad_amount',             // amount missing or not positive
});

// U.S. Census Bureau regions. DC counts as South; territories get their own group.
export const STATE_REGION = Object.freeze({
  CT: 'Northeast', ME: 'Northeast', MA: 'Northeast', NH: 'Northeast', RI: 'Northeast',
  VT: 'Northeast', NJ: 'Northeast', NY: 'Northeast', PA: 'Northeast',
  IL: 'Midwest', IN: 'Midwest', MI: 'Midwest', OH: 'Midwest', WI: 'Midwest', IA: 'Midwest',
  KS: 'Midwest', MN: 'Midwest', MO: 'Midwest', NE: 'Midwest', ND: 'Midwest', SD: 'Midwest',
  DE: 'South', DC: 'South', FL: 'South', GA: 'South', MD: 'South', NC: 'South', SC: 'South',
  VA: 'South', WV: 'South', AL: 'South', KY: 'South', MS: 'South', TN: 'South', AR: 'South',
  LA: 'South', OK: 'South', TX: 'South',
  AZ: 'West', CO: 'West', ID: 'West', MT: 'West', NV: 'West', NM: 'West', UT: 'West',
  WY: 'West', AK: 'West', CA: 'West', HI: 'West', OR: 'West', WA: 'West',
  AS: 'Territories', GU: 'Territories', MP: 'Territories', PR: 'Territories', VI: 'Territories',
});

const EPS = 1e-9;

// ---------------------------------------------------------------- helpers

export function regionOf(state) {
  return STATE_REGION[state] ?? null;
}

/** Whole years between birthDate and onDate (both ISO strings), or null. */
export function ageOn(birthDate, onDate) {
  if (!birthDate || !onDate) return null;
  const [by, bm, bd] = birthDate.split('-').map(Number);
  const [y, m, d] = onDate.split('-').map(Number);
  let age = y - by;
  if (m < bm || (m === bm && d < bd)) age -= 1;
  return age;
}

/** Age decade (40 for ages 40-49) on a date, or null. */
export function ageDecadeOn(birthDate, onDate) {
  const age = ageOn(birthDate, onDate);
  return age == null ? null : Math.floor(age / 10) * 10;
}

/** Dollar amount used for a trade under the chosen estimator. */
export function estimateAmount(trade, estimator = DEFAULTS.estimator) {
  const low = trade.amountLow;
  const high = trade.amountHigh;
  const openEnded = high == null || (trade.flags ?? []).includes('amount_open_ended');
  if (low == null) return null;
  if (openEnded) return low; // top bracket: lower bound whatever the estimator
  switch (estimator) {
    case 'low': return low;
    case 'high': return high;
    case 'mid': return (low + high) / 2;
    default: throw new Error(`unknown estimator: ${estimator}`);
  }
}

function normalizeBasis(basis) {
  if (basis == null || basis === 'transaction' || basis === 'tx' || basis === 'txDate') return 'txDate';
  if (basis === 'disclosure' || basis === 'disc' || basis === 'discDate') return 'discDate';
  throw new Error(`unknown dateBasis: ${basis}`);
}

/** Index of the first element >= value in a sorted string array (arr.length if none). */
export function lowerBound(arr, value) {
  let lo = 0;
  let hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (arr[mid] < value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function daysBetween(a, b) {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86400000);
}

function memberKey(trade) {
  return trade.memberId || trade.member;
}

/**
 * True if a trade passes the user's member filters. Every filter is a list;
 * a missing or empty list means "no restriction". A trade with an unknown
 * value fails any active filter on that field.
 *
 *   party      ['D', 'R', 'I']
 *   gender     ['F', 'M']
 *   ageDecade  [40, 50]          age on the trade's basis date
 *   region     ['West', ...]     STATE_REGION values
 *   state      ['CA', 'TX']      two-letter state of the seat on the trade date
 *   chamber    ['house', 'senate']
 *   members    [memberId, ...]
 *   owner      ['self', 'spouse', 'joint', 'dependent', 'self_or_dependent', 'other']
 *   tickers    ['NVDA', ...]
 */
export function matchesFilters(trade, filters = {}, onDate = trade.txDate) {
  const active = (list) => Array.isArray(list) && list.length > 0;
  const f = filters ?? {};
  if (active(f.party) && !f.party.includes(trade.party)) return false;
  if (active(f.gender) && !f.gender.includes(trade.gender)) return false;
  if (active(f.chamber) && !f.chamber.includes(trade.chamber)) return false;
  if (active(f.owner) && !f.owner.includes(trade.owner)) return false;
  if (active(f.tickers) && !f.tickers.includes(trade.ticker)) return false;
  if (active(f.members) && !f.members.includes(memberKey(trade))) return false;
  if (active(f.state) && !f.state.includes(trade.state)) return false;
  if (active(f.region) && !f.region.includes(regionOf(trade.state))) return false;
  if (active(f.ageDecade)) {
    const dec = ageDecadeOn(trade.birthDate, onDate);
    if (dec == null || !f.ageDecade.map(Number).includes(dec)) return false;
  }
  return true;
}

/**
 * Per-ticker price lookup. `closeOn(d)` returns the close on d, or the most
 * recent earlier close when d is inside the series but missing (a data gap).
 * Days before the first or after the last close return null.
 */
function makeSeries(raw) {
  const dates = raw.dates;
  const close = raw.close;
  const first = dates[0];
  const last = dates[dates.length - 1];
  return {
    first,
    last,
    closeOn(d) {
      if (!dates.length || d < first || d > last) return null;
      let i = lowerBound(dates, d);
      if (dates[i] !== d) i -= 1;
      return i >= 0 ? close[i] : null;
    },
    /** Last close on or before d (carry-forward), with its date. */
    lastOn(d) {
      let i = lowerBound(dates, d);
      if (i >= dates.length || dates[i] !== d) i -= 1;
      return i >= 0 ? { date: dates[i], close: close[i] } : null;
    },
  };
}

// ---------------------------------------------------------------- engine

export function simulate(trades, prices, options = {}) {
  const opts = { ...DEFAULTS, ...options };
  const basis = normalizeBasis(opts.dateBasis);
  const bench = prices[opts.benchmark];
  if (!bench || !bench.dates?.length) {
    throw new Error(`benchmark ${opts.benchmark} has no price data`);
  }

  const fullCalendar = bench.dates;
  const start = opts.start ?? fullCalendar[0];
  const end = opts.end ?? fullCalendar[fullCalendar.length - 1];
  const i0 = lowerBound(fullCalendar, start);
  const iEnd = lowerBound(fullCalendar, end);
  const i1 = fullCalendar[iEnd] === end ? iEnd + 1 : iEnd;
  const calendar = fullCalendar.slice(i0, i1);

  const series = new Map();
  const seriesFor = (ticker) => {
    if (!series.has(ticker)) series.set(ticker, prices[ticker] ? makeSeries(prices[ticker]) : null);
    return series.get(ticker);
  };
  const benchSeries = seriesFor(opts.benchmark);

  // 1. Filter trades and attach their effective trading day.
  const tradeLog = [];
  const byDay = new Map();
  trades.forEach((trade, index) => {
    const basisDate = trade[basis];
    if (!basisDate || basisDate < start || basisDate > end) return;
    if (!matchesFilters(trade, opts.filters, basisDate)) return;

    const entry = {
      id: trade.id,
      member: trade.member,
      memberId: memberKey(trade),
      owner: trade.owner,
      ticker: trade.ticker,
      type: trade.type,
      basisDate,
      date: null,         // effective trading day
      amount: estimateAmount(trade, opts.estimator),
      status: 'skipped',
      reason: null,
      price: null,
      shares: null,       // shares bought (+) or sold (-) in the Congress pool
      value: null,        // dollars bought, or realized sale value S
      capped: false,      // sale amount reduced to the position's value
      spyShares: null,
      spyShortfall: 0,    // part of S the benchmark couldn't sell (no SPY left)
      flags: trade.flags ?? [],
      _index: index,
    };
    tradeLog.push(entry);

    if (!(entry.amount > 0)) { entry.reason = SKIP_REASONS.BAD_AMOUNT; return; }
    const di = lowerBound(fullCalendar, basisDate);
    if (di >= fullCalendar.length) { entry.reason = SKIP_REASONS.NO_TRADING_DAY; return; }
    const day = fullCalendar[di];
    if (day > end) { entry.reason = SKIP_REASONS.AFTER_END; return; }
    entry.date = day;
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push(entry);
  });

  // Buys before sells on the same day, then by basis date, then input order.
  const rank = (t) => (t === 'buy' ? 0 : 1);
  for (const list of byDay.values()) {
    list.sort((a, b) => rank(a.type) - rank(b.type)
      || (a.basisDate < b.basisDate ? -1 : a.basisDate > b.basisDate ? 1 : 0)
      || a._index - b._index);
  }

  // 2. State.
  const positions = new Map(); // ticker -> Map(memberKey -> { shares, member })
  const lastPrice = new Map(); // ticker -> { date, close }
  let congressCash = 0;
  let spyShares = 0;
  let spyCash = 0;
  let contributed = 0;
  let congressSold = 0;
  let spySold = 0;

  const out = {
    dates: [],
    congressValue: [],
    congressCash: [],
    spyValue: [],
    spyCash: [],
    contributions: [],    // cumulative dollars bought (same for both pools)
    congressNetFlow: [],  // contributions minus realized sales, Congress pool
    spyNetFlow: [],       // contributions minus realized sales, S&P 500 pool
  };

  const memberPos = (ticker, key, name) => {
    if (!positions.has(ticker)) positions.set(ticker, new Map());
    const byMember = positions.get(ticker);
    if (!byMember.has(key)) byMember.set(key, { shares: 0, member: name });
    return byMember.get(key);
  };
  const dropIfEmpty = (ticker, key) => {
    const byMember = positions.get(ticker);
    if (!byMember) return;
    const pos = byMember.get(key);
    if (pos && pos.shares <= EPS) byMember.delete(key);
    if (byMember.size === 0) positions.delete(ticker);
  };

  // 3. Walk the calendar.
  for (const day of calendar) {
    const spyClose = benchSeries.closeOn(day);
    lastPrice.set(opts.benchmark, { date: day, close: spyClose });

    for (const e of byDay.get(day) ?? []) {
      const s = seriesFor(e.ticker);
      if (!s) { e.reason = SKIP_REASONS.NO_PRICE_DATA; continue; }
      const px = s.closeOn(day);
      if (px == null || !(px > 0)) { e.reason = SKIP_REASONS.NO_PRICE_ON_DATE; continue; }
      e.price = px;
      const key = e.memberId;

      if (e.type === 'buy') {
        const shares = e.amount / px;
        memberPos(e.ticker, key, e.member).shares += shares;
        lastPrice.set(e.ticker, { date: day, close: px });
        const bought = e.amount / spyClose;
        spyShares += bought;
        contributed += e.amount;
        Object.assign(e, { status: 'applied', shares, value: e.amount, spyShares: bought });
        continue;
      }

      // sell or sell_full
      const pos = positions.get(e.ticker)?.get(key);
      if (!pos || pos.shares <= EPS) { e.reason = SKIP_REASONS.NO_HOLDINGS; continue; }
      const held = pos.shares * px;
      let sold;
      let sharesSold;
      if (e.type === 'sell_full' || e.amount >= held * (1 - 1e-12)) {
        sold = held;
        sharesSold = pos.shares;
        e.capped = e.type !== 'sell_full' && e.amount > held;
      } else {
        sold = e.amount;
        sharesSold = sold / px;
      }
      pos.shares = sold === held ? 0 : pos.shares - sharesSold;
      dropIfEmpty(e.ticker, key);
      lastPrice.set(e.ticker, { date: day, close: px });
      congressCash += sold;
      congressSold += sold;

      // Benchmark mirrors the realized amount S, capped at the SPY it holds.
      const spyHeld = spyShares * spyClose;
      const spySell = Math.min(sold, spyHeld);
      const spySharesSold = spySell >= spyHeld ? spyShares : spySell / spyClose;
      spyShares -= spySharesSold;
      if (spyShares < EPS) spyShares = 0;
      spyCash += spySell;
      spySold += spySell;
      Object.assign(e, {
        status: 'applied',
        shares: -sharesSold,
        value: sold,
        spyShares: -spySharesSold,
        spyShortfall: sold - spySell,
      });
    }

    // Value every open position at today's close, carrying forward when missing.
    let holdingsValue = 0;
    for (const [ticker, byMember] of positions) {
      const s = seriesFor(ticker);
      const lp = s.lastOn(day) ?? lastPrice.get(ticker);
      lastPrice.set(ticker, lp);
      let shares = 0;
      for (const p of byMember.values()) shares += p.shares;
      holdingsValue += shares * lp.close;
    }

    out.dates.push(day);
    out.congressValue.push(holdingsValue + congressCash);
    out.congressCash.push(congressCash);
    out.spyValue.push(spyShares * spyClose + spyCash);
    out.spyCash.push(spyCash);
    out.contributions.push(contributed);
    out.congressNetFlow.push(contributed - congressSold);
    out.spyNetFlow.push(contributed - spySold);
  }

  // 4. Final ledgers.
  const lastDay = calendar[calendar.length - 1] ?? null;
  const finalCongress = out.congressValue.at(-1) ?? 0;
  const holdings = [];
  for (const [ticker, byMember] of positions) {
    const lp = lastPrice.get(ticker);
    const members = [];
    let shares = 0;
    for (const [memberId, p] of byMember) {
      shares += p.shares;
      members.push({ memberId, member: p.member, shares: p.shares, value: p.shares * lp.close });
    }
    members.sort((a, b) => b.value - a.value);
    const value = shares * lp.close;
    holdings.push({
      ticker,
      shares,
      price: lp.close,
      priceDate: lp.date,
      value,
      weight: finalCongress > 0 ? value / finalCongress : 0,
      ended: lastDay != null && daysBetween(lp.date, lastDay) > opts.staleDays,
      members,
    });
  }
  holdings.sort((a, b) => b.value - a.value);

  for (const e of tradeLog) delete e._index;
  tradeLog.sort((a, b) => ((a.date ?? a.basisDate) < (b.date ?? b.basisDate) ? -1
    : (a.date ?? a.basisDate) > (b.date ?? b.basisDate) ? 1 : 0));

  const skippedByReason = {};
  for (const e of tradeLog) {
    if (e.status === 'skipped') skippedByReason[e.reason] = (skippedByReason[e.reason] ?? 0) + 1;
  }

  const finalSpy = out.spyValue.at(-1) ?? 0;
  const summary = {
    start: calendar[0] ?? null,
    end: lastDay,
    contributed,
    congress: poolSummary(finalCongress, out.congressCash.at(-1) ?? 0, contributed),
    spy: poolSummary(finalSpy, out.spyCash.at(-1) ?? 0, contributed),
    difference: finalCongress - finalSpy,
    applied: tradeLog.filter((e) => e.status === 'applied').length,
    skipped: tradeLog.filter((e) => e.status === 'skipped').length,
    skippedByReason,
    spyShortfall: tradeLog.reduce((acc, e) => acc + e.spyShortfall, 0),
    endedTickers: holdings.filter((h) => h.ended).map((h) => h.ticker),
  };

  return { ...out, holdings, tradeLog, summary };
}

function poolSummary(value, cash, contributed) {
  const gain = value - contributed;
  return {
    value,
    cash,
    invested: value - cash,
    gain,
    gainPct: contributed > 0 ? gain / contributed : null,
  };
}
