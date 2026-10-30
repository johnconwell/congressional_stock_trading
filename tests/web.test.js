// Tests for the pure parts of the web UI (README phases 4 and 5): URL state, data loading
// and the member index.
// Run: npm test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_STATE, decodeState, encodeState, filtersOf } from '../web/js/state.js';
import { priceFileName, tickersWithPrices, tickersNeeded, createLoader } from '../web/js/data.js';
import { buildMemberIndex, memberLabel, seatLabel, searchMembers, memberAge } from '../web/js/members.js';
import { matchesFilters } from '../web/js/sim.js';

// ---------------------------------------------------------------- state.js

test('default state encodes to an empty query string', () => {
  assert.equal(encodeState(DEFAULT_STATE), '');
  assert.deepEqual(decodeState(''), { ...DEFAULT_STATE });
});

test('state round-trips through the query string', () => {
  const state = {
    ...DEFAULT_STATE,
    start: '2024-01-02',
    end: '2025-06-30',
    estimator: 'high',
    dateBasis: 'disclosure',
    party: ['D', 'R'],
    gender: ['F'],
    ageDecade: [50, 60],
    region: ['West', 'South'],
    logScale: true,
    showInvested: true,
    showNetFlow: false,
  };
  const qs = encodeState(state);
  assert.equal(qs, 'start=2024-01-02&end=2025-06-30&est=high&basis=disclosure&party=D,R&gender=F&age=50,60&region=West,South&scale=log&show=invested');
  assert.deepEqual(decodeState(`?${qs}`), state);
});

test('hiding every optional line survives the round trip', () => {
  const state = { ...DEFAULT_STATE, showInvested: false, showNetFlow: false };
  assert.equal(encodeState(state), 'show=none');
  assert.deepEqual(decodeState('show=none'), state);
});

test('invalid query values fall back to defaults', () => {
  const s = decodeState('?start=yesterday&est=max&basis=whenever&age=x,40&party=,D,D');
  assert.equal(s.start, null);
  assert.equal(s.estimator, 'mid');
  assert.equal(s.dateBasis, 'transaction');
  assert.deepEqual(s.ageDecade, [40]);
  assert.deepEqual(s.party, ['D']);
});

test('filtersOf passes the member filters to sim.js', () => {
  const s = decodeState('party=R&age=70&owner=spouse');
  assert.deepEqual(filtersOf(s), {
    chamber: [], party: ['R'], gender: [], ageDecade: [70], region: [], state: [], members: [], owner: ['spouse'],
  });
});

test('phase 5 filters round-trip through the query string', () => {
  const state = {
    ...DEFAULT_STATE,
    chamber: ['senate'],
    state: ['AL', 'FL'],
    members: ['T000278', 'S001217'],
    owner: ['self', 'joint'],
  };
  const qs = encodeState(state);
  assert.equal(qs, 'chamber=senate&state=AL,FL&members=T000278,S001217&owner=self,joint');
  assert.deepEqual(decodeState(qs), state);
});

test('decodeState cleans chamber and state values', () => {
  const s = decodeState('chamber=Senate,moon&state=ca,TX');
  assert.deepEqual(s.chamber, ['senate']);
  assert.deepEqual(s.state, ['CA', 'TX']);
});

test('member view: in the URL first, and only member + owner reach sim.js', () => {
  const state = { ...DEFAULT_STATE, member: 'T000278', party: ['D'], owner: ['spouse'], estimator: 'low' };
  const qs = encodeState(state);
  assert.equal(qs, 'member=T000278&est=low&party=D&owner=spouse');
  const back = decodeState(qs);
  assert.deepEqual(back, state);
  assert.deepEqual(filtersOf(back), { members: ['T000278'], owner: ['spouse'] });
  // Leaving the view keeps the group filters.
  assert.deepEqual(filtersOf({ ...back, member: null }).party, ['D']);
});

test('matchesFilters: state, chamber, members and owner', () => {
  const t = { chamber: 'senate', state: 'AL', memberId: 'T000278', member: 'Tommy Tuberville', owner: 'spouse', txDate: '2025-01-02' };
  assert.equal(matchesFilters(t, { state: ['AL'] }), true);
  assert.equal(matchesFilters(t, { state: ['FL'] }), false);
  assert.equal(matchesFilters(t, { chamber: ['house'] }), false);
  assert.equal(matchesFilters(t, { members: ['T000278'], owner: ['spouse', 'joint'] }), true);
  assert.equal(matchesFilters({ ...t, memberId: null }, { members: ['Tommy Tuberville'] }), true, 'name is the key without an ID');
});

// ---------------------------------------------------------------- data.js

const META = {
  tickers: ['AAA', 'BBB', 'CON', 'PEND', 'GONE'],
  tickersWithoutPrices: ['GONE'],
  prices: {
    benchmark: 'SPY',
    fileNames: { CON: 'CON_' },
    pending: ['PEND'],
    withoutPricesByReason: { not_found: ['GONE'] },
  },
};

test('priceFileName applies the Windows reserved-name map', () => {
  assert.equal(priceFileName('NVDA', META), 'NVDA.json');
  assert.equal(priceFileName('CON', META), 'CON_.json');
  assert.equal(priceFileName('NVDA', {}), 'NVDA.json');
});

test('tickersWithPrices leaves out pending and missing tickers', () => {
  assert.deepEqual([...tickersWithPrices(META)].sort(), ['AAA', 'BBB', 'CON']);
  assert.equal(tickersWithPrices({}), null);
});

const T = (over) => ({
  ticker: 'AAA', party: 'D', txDate: '2025-01-06', discDate: '2025-02-01', ...over,
});

test('tickersNeeded applies the date range on the chosen basis and the filters', () => {
  const trades = [
    T({ ticker: 'AAA' }),
    T({ ticker: 'BBB', party: 'R' }),
    T({ ticker: 'CCC', txDate: '2024-12-01', discDate: '2025-01-10' }),
  ];
  const range = { start: '2025-01-01', end: '2025-01-31' };
  assert.deepEqual([...tickersNeeded(trades, range)].sort(), ['AAA', 'BBB']);
  assert.deepEqual([...tickersNeeded(trades, { ...range, filters: { party: ['R'] } })], ['BBB']);
  assert.deepEqual([...tickersNeeded(trades, { ...range, dateBasis: 'disclosure' })], ['CCC']);
});

function fakeFetch(files) {
  const calls = [];
  const fn = async (url) => {
    calls.push(url);
    const body = files[url];
    if (body === undefined) return { ok: false, status: 404, json: async () => null };
    if (body instanceof Error) return { ok: false, status: 500, json: async () => null };
    return { ok: true, status: 200, json: async () => structuredClone(body) };
  };
  return { fn, calls };
}

test('loader fetches each price file once and skips missing ones', async () => {
  const series = { dates: ['2025-01-06'], close: [1] };
  const { fn, calls } = fakeFetch({
    'd/meta.json': META,
    'd/prices/AAA.json': series,
    'd/prices/CON_.json': series,
  });
  const data = createLoader({ base: 'd/', fetchFn: fn });
  const progress = [];
  const p1 = await data.prices(['AAA', 'CON', 'NOPE'], { onProgress: (d, t) => progress.push(`${d}/${t}`) });
  assert.deepEqual(Object.keys(p1).sort(), ['AAA', 'CON']);
  assert.equal(progress.at(-1), '3/3');
  const p2 = await data.prices(['AAA', 'CON']);
  assert.deepEqual(Object.keys(p2).sort(), ['AAA', 'CON']);
  assert.equal(calls.filter((u) => u.includes('/prices/')).length, 3, 'second call is served from the cache');
});

test('loader retries a price file after a server error', async () => {
  const files = { 'd/meta.json': META, 'd/prices/AAA.json': new Error('boom') };
  const { fn, calls } = fakeFetch(files);
  const data = createLoader({ base: 'd/', fetchFn: fn });
  assert.deepEqual(await data.prices(['AAA']), {});
  files['d/prices/AAA.json'] = { dates: ['2025-01-06'], close: [1] };
  assert.deepEqual(Object.keys(await data.prices(['AAA'])), ['AAA']);
  assert.equal(calls.filter((u) => u.endsWith('AAA.json')).length, 2);
});

test('loader reports a missing trades.json clearly', async () => {
  const { fn } = fakeFetch({});
  const data = createLoader({ base: 'd/', fetchFn: fn });
  await assert.rejects(data.trades(), /trades\.json not found/);
});

// ---------------------------------------------------------------- members.js

const MT = (over) => ({
  id: 'x', chamber: 'house', member: 'Markwayne Mullin', memberId: 'M001190', party: 'R', state: 'OK', district: 2,
  gender: 'M', birthDate: '1977-07-26', ticker: 'AAA', type: 'buy', txDate: '2021-05-01', discDate: '2021-05-20', ...over,
});

test('buildMemberIndex: one record per member, profile from the latest trade', () => {
  const index = buildMemberIndex([
    MT({ ticker: 'AAA' }),
    MT({ ticker: 'BBB', type: 'sell', txDate: '2022-02-01', discDate: '2022-03-01' }),
    MT({ chamber: 'senate', district: null, ticker: 'AAA', txDate: '2024-04-01', discDate: '2024-04-20' }),
    MT({ member: 'Tommy Tuberville', memberId: 'T000278', chamber: 'senate', state: 'AL', district: null,
      birthDate: '1954-09-18', txDate: '2023-01-05', discDate: '2023-01-30' }),
    MT({ member: 'Unmatched Filer', memberId: null, chamber: 'senate', party: null, state: null, district: null }),
  ]);
  assert.deepEqual(index.map((m) => m.id), ['M001190', 'T000278', 'Unmatched Filer']);
  const mullin = index[0];
  assert.equal(mullin.chamber, 'senate');
  assert.deepEqual(mullin.chambers, ['house', 'senate']);
  assert.equal(mullin.district, null);
  assert.deepEqual([mullin.trades, mullin.buys, mullin.sells, mullin.tickers], [3, 2, 1, 2]);
  assert.deepEqual([mullin.firstTx, mullin.lastTx, mullin.lastDisc], ['2021-05-01', '2024-04-01', '2024-04-20']);
  assert.equal(memberLabel(mullin), 'Markwayne Mullin (R-OK, Senate)');
  assert.equal(memberLabel(index[2]), 'Unmatched Filer (Senate)');
  assert.equal(memberAge(index[1], '2025-01-01'), 70);
});

test('seatLabel shows the district for House seats only, none for at-large', () => {
  assert.equal(seatLabel({ chamber: 'house', party: 'D', state: 'CA', district: 12 }), 'D-CA-12');
  assert.equal(seatLabel({ chamber: 'house', party: 'R', state: 'WY', district: 0 }), 'R-WY');
  assert.equal(seatLabel({ chamber: 'senate', party: 'I', state: 'VT', district: null }), 'I-VT');
});

test('searchMembers matches every word against name and seat', () => {
  const index = buildMemberIndex([
    MT({}),
    MT({ member: 'Rick Scott', memberId: 'S001217', chamber: 'senate', state: 'FL', district: null }),
    MT({ member: 'Tim Scott', memberId: 'S001184', chamber: 'senate', state: 'SC', district: null }),
  ]);
  assert.deepEqual(searchMembers(index, 'scott').map((m) => m.id), ['S001217', 'S001184']);
  assert.deepEqual(searchMembers(index, 'scott fl').map((m) => m.id), ['S001217']);
  assert.equal(searchMembers(index, '').length, 3);
});

// ---------------------------------------------------------------- freshness.js (phase 6)

import { freshness, relativeAge, formatDay, STALE_BUILD_HOURS } from '../web/js/freshness.js';

const FRESH_META = {
  builtAt: '2026-10-30T09:40:00+00:00',
  prices: { lastDate: '2026-10-29' },
  coverage: { house: { lastDiscDate: '2026-10-28' }, senate: { lastDiscDate: '2026-10-29' } },
  buildRun: { url: 'https://github.com/me/tracker/actions/runs/1', event: 'schedule' },
};

test('freshness of a nightly build', () => {
  const f = freshness(FRESH_META, new Date('2026-10-30T15:40:00Z'));
  assert.equal(f.pricesThrough, '2026-10-29');
  assert.deepEqual(f.disclosures.map((d) => [d.label, d.date]), [['House', '2026-10-28'], ['Senate', '2026-10-29']]);
  assert.equal(f.ageHours, 6);
  assert.equal(f.updated, '6 hours ago');
  assert.equal(f.stale, false);
  assert.equal(f.runUrl, 'https://github.com/me/tracker/actions/runs/1');
});

test('a build older than two days is stale', () => {
  const now = new Date(Date.parse(FRESH_META.builtAt) + (STALE_BUILD_HOURS + 30) * 3_600_000);
  const f = freshness({ ...FRESH_META, prices: { lastDate: '2026-11-01' } }, now);
  assert.equal(f.stale, true);
  assert.equal(f.updated, '3 days ago');
  assert.deepEqual(f.staleReasons, ["The data hasn't been refreshed for 3 days."]);
});

test('prices behind a fresh build are stale, but a weekend is not', () => {
  // Monday morning: Friday's close is the latest one.
  const weekend = freshness({ ...FRESH_META, builtAt: '2026-11-02T09:30:00Z', prices: { lastDate: '2026-10-30' } },
    new Date('2026-11-02T12:00:00Z'));
  assert.equal(weekend.stale, false);
  // Tiingo stopped answering a week ago while the nightly build kept running.
  const behind = freshness({ ...FRESH_META, prices: { lastDate: '2026-10-21' } }, new Date('2026-10-30T12:00:00Z'));
  assert.equal(behind.stale, true);
  assert.deepEqual(behind.staleReasons, ['The latest closing price is from Oct 21, 2026, 9 days ago.']);
});

test('freshness copes with an empty or old meta.json', () => {
  const f = freshness({}, new Date());
  assert.equal(f.builtAt, null);
  assert.equal(f.updated, '');
  assert.equal(f.stale, false);
  assert.deepEqual(f.disclosures, []);
  assert.equal(freshness({ buildRun: { url: 'javascript:alert(1)' } }).runUrl, null);
  assert.equal(freshness({ builtAt: 'not a date' }).builtAt, null);
});

test('relative ages and day formatting', () => {
  assert.equal(relativeAge(0.2), 'just now');
  assert.equal(relativeAge(1), '1 hour ago');
  assert.equal(relativeAge(35), '35 hours ago');
  assert.equal(relativeAge(36), '2 days ago');
  assert.equal(relativeAge(24 * 1.2 + 12), '2 days ago');
  assert.equal(relativeAge(null), '');
  assert.equal(formatDay('2026-01-02'), 'Jan 2, 2026');
  assert.equal(formatDay('garbage'), 'garbage');
});
