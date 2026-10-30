// data.js: loads web/data/*.json on demand (README section 3.1, phase 4).
//
// trades.json and meta.json are fetched once. Price files are fetched only for
// the tickers in the filtered trade set, cached for the session, and requested a
// few at a time. The pure helpers at the top are unit-tested under Node.

import { matchesFilters } from './sim.js';

/** Price file name for a ticker, honouring meta.prices.fileNames (e.g. CON -> CON_.json). */
export function priceFileName(ticker, meta) {
  const stem = meta?.prices?.fileNames?.[ticker] ?? ticker;
  return `${stem}.json`;
}

/**
 * Tickers that should have a price file: every traded ticker minus those the
 * price step hasn't fetched yet (`pending`) or found nothing for. Returns null
 * when meta has no price block, meaning "unknown, try them all".
 */
export function tickersWithPrices(meta) {
  if (!meta?.prices || !Array.isArray(meta.tickers)) return null;
  const missing = new Set([...(meta.prices.pending ?? []), ...(meta.tickersWithoutPrices ?? [])]);
  for (const list of Object.values(meta.prices.withoutPricesByReason ?? {})) {
    for (const t of list) missing.add(t);
  }
  return new Set(meta.tickers.filter((t) => !missing.has(t)));
}

/**
 * Unique tickers of the trades that pass the date range (on the chosen date
 * basis) and the member filters, i.e. the trades simulate() will look at.
 */
export function tickersNeeded(trades, { start, end, dateBasis = 'transaction', filters } = {}) {
  const key = dateBasis === 'disclosure' ? 'discDate' : 'txDate';
  const out = new Set();
  for (const t of trades) {
    const d = t[key];
    if (!d || (start && d < start) || (end && d > end)) continue;
    if (!matchesFilters(t, filters, d)) continue;
    if (t.ticker) out.add(t.ticker);
  }
  return out;
}

/** Run async `fn` over `items` with at most `limit` in flight. */
async function mapLimit(items, limit, fn) {
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      await fn(items[i], i);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
}

/**
 * Data loader bound to a base URL.
 *   const data = createLoader({ base: 'data/' });
 *   await data.meta(); await data.trades();
 *   const prices = await data.prices(['SPY', 'NVDA'], { onProgress });
 */
export function createLoader({ base = 'data/', fetchFn = (...a) => fetch(...a), concurrency = 8 } = {}) {
  let metaPromise = null;
  let tradesPromise = null;
  const priceCache = new Map(); // ticker -> Promise<series | null>

  const getJson = async (path) => {
    const res = await fetchFn(base + path);
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  };

  return {
    meta() {
      metaPromise ??= getJson('meta.json').then((m) => m ?? {});
      return metaPromise;
    },
    trades() {
      tradesPromise ??= getJson('trades.json').then((t) => {
        if (!t) throw new Error('trades.json not found. Run the pipeline first (README section 5).');
        return t;
      });
      return tradesPromise;
    },
    /** Resolve to { TICKER: { dates, close } } for the tickers that have a file. */
    async prices(tickers, { onProgress } = {}) {
      const meta = await this.meta();
      const list = [...new Set(tickers)];
      const todo = list.filter((t) => !priceCache.has(t));
      let done = list.length - todo.length;
      onProgress?.(done, list.length);
      await mapLimit(todo, concurrency, async (t) => {
        const p = getJson(`prices/${encodeURIComponent(priceFileName(t, meta))}`)
          .catch((err) => { priceCache.delete(t); throw err; });
        priceCache.set(t, p);
        await p.catch(() => null);
        done += 1;
        onProgress?.(done, list.length);
      });
      const out = {};
      for (const t of list) {
        const series = await priceCache.get(t)?.catch(() => null);
        if (series?.dates?.length) out[t] = series;
      }
      return out;
    },
    cachedPriceCount: () => priceCache.size,
  };
}
