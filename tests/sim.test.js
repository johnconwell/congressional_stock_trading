// Fixture tests for web/js/sim.js (README phase 3). Run: npm test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  simulate, estimateAmount, matchesFilters, ageOn, ageDecadeOn, regionOf, SKIP_REASONS,
} from '../web/js/sim.js';

// ---------------------------------------------------------------- fixtures

// Trading calendar: Mon 2025-01-06 .. Fri 2025-01-17 (Sat/Sun 11-12 missing).
const DAYS = [
  '2025-01-06', '2025-01-07', '2025-01-08', '2025-01-09', '2025-01-10',
  '2025-01-13', '2025-01-14', '2025-01-15', '2025-01-16', '2025-01-17',
];

const series = (ticker, closes, dates = DAYS) => ({ ticker, dates: dates.slice(0, closes.length), close: closes });

// SPY: 100, 101, ... ; AAA: 10, 11, ... ; BBB flat 50.
const SPY = series('SPY', DAYS.map((_, i) => 100 + i));
const AAA = series('AAA', DAYS.map((_, i) => 10 + i));
const BBB = series('BBB', DAYS.map(() => 50));
const PRICES = { SPY, AAA, BBB };

let seq = 0;
function trade(over = {}) {
  seq += 1;
  return {
    id: `house-T${seq}`,
    chamber: 'house',
    member: 'Jane Doe',
    memberId: 'D000001',
    party: 'D',
    state: 'CA',
    gender: 'F',
    birthDate: '1960-06-15',
    owner: 'self',
    ticker: 'AAA',
    assetType: 'stock',
    type: 'buy',
    txDate: '2025-01-06',
    discDate: '2025-01-15',
    amountLow: 1000,
    amountHigh: 1000,
    flags: [],
    ...over,
  };
}

const close = (a, b, msg) => assert.ok(Math.abs(a - b) < 1e-6, `${msg ?? ''} expected ${b}, got ${a}`);
const at = (res, key, day) => res[key][res.dates.indexOf(day)];

/** Both pools receive the same buys; realized sales match unless SPY ran out. */
function assertEqualContributions(res) {
  for (let i = 0; i < res.dates.length; i++) {
    assert.ok(res.contributions[i] >= 0);
    if (res.summary.spyShortfall === 0) close(res.congressNetFlow[i], res.spyNetFlow[i], `net flow on ${res.dates[i]}`);
    assert.ok(res.congressValue[i] - res.congressCash[i] >= -1e-9, 'invested part not negative');
    assert.ok(res.spyValue[i] - res.spyCash[i] >= -1e-9, 'invested part not negative');
  }
}

// ---------------------------------------------------------------- helpers

test('estimateAmount: low / mid / high and open-ended top bracket', () => {
  const t = { amountLow: 15001, amountHigh: 50000, flags: [] };
  assert.equal(estimateAmount(t, 'low'), 15001);
  assert.equal(estimateAmount(t, 'high'), 50000);
  assert.equal(estimateAmount(t, 'mid'), 32500.5);
  assert.equal(estimateAmount(t), 32500.5);
  const top = { amountLow: 50000001, amountHigh: null, flags: ['amount_open_ended'] };
  for (const e of ['low', 'mid', 'high']) assert.equal(estimateAmount(top, e), 50000001);
  assert.throws(() => estimateAmount(t, 'avg'));
});

test('ageOn / ageDecadeOn / regionOf', () => {
  assert.equal(ageOn('1960-06-15', '2025-06-14'), 64);
  assert.equal(ageOn('1960-06-15', '2025-06-15'), 65);
  assert.equal(ageDecadeOn('1960-06-15', '2025-01-06'), 60);
  assert.equal(ageDecadeOn(null, '2025-01-06'), null);
  assert.equal(regionOf('CA'), 'West');
  assert.equal(regionOf('DC'), 'South');
  assert.equal(regionOf('PR'), 'Territories');
  assert.equal(regionOf('XX'), null);
});

test('matchesFilters: empty filters pass, each field restricts, unknowns fail', () => {
  const t = trade();
  assert.ok(matchesFilters(t, {}));
  assert.ok(matchesFilters(t, { party: [] }));
  assert.ok(matchesFilters(t, { party: ['D', 'R'], gender: ['F'], region: ['West'], ageDecade: [60] }));
  assert.ok(!matchesFilters(t, { party: ['R'] }));
  assert.ok(!matchesFilters(t, { region: ['South'] }));
  assert.ok(!matchesFilters(t, { ageDecade: [50] }));
  assert.ok(!matchesFilters(t, { owner: ['spouse'] }));
  assert.ok(!matchesFilters(t, { members: ['X000001'] }));
  assert.ok(!matchesFilters({ ...t, birthDate: null }, { ageDecade: [60] }));
});

// ---------------------------------------------------------------- phase 3 fixtures

test('single buy', () => {
  const res = simulate([trade({ amountLow: 1000, amountHigh: 1000 })], PRICES);
  assert.equal(res.dates.length, DAYS.length);
  // 1000 / 10 = 100 AAA shares; 1000 / 100 = 10 SPY shares.
  close(at(res, 'congressValue', '2025-01-06'), 1000);
  close(at(res, 'spyValue', '2025-01-06'), 1000);
  close(at(res, 'congressValue', '2025-01-17'), 100 * 19);
  close(at(res, 'spyValue', '2025-01-17'), 10 * 109);
  close(res.summary.contributed, 1000);
  close(res.summary.congress.gain, 900);
  assert.equal(res.holdings.length, 1);
  assert.equal(res.holdings[0].ticker, 'AAA');
  close(res.holdings[0].shares, 100);
  close(res.holdings[0].weight, 1);
  assert.equal(res.tradeLog[0].status, 'applied');
  assertEqualContributions(res);
});

test('buy then partial sell', () => {
  const res = simulate([
    trade({ txDate: '2025-01-06', amountLow: 1000, amountHigh: 1000 }),             // 100 sh @10
    trade({ txDate: '2025-01-08', type: 'sell', amountLow: 600, amountHigh: 600 }), // @12 -> 50 sh
  ], PRICES);
  const sale = res.tradeLog[1];
  assert.equal(sale.status, 'applied');
  close(sale.value, 600);
  close(sale.shares, -50);
  assert.equal(sale.capped, false);
  close(at(res, 'congressCash', '2025-01-08'), 600);
  close(at(res, 'congressValue', '2025-01-08'), 50 * 12 + 600);
  // SPY: 10 sh bought @100, sells $600 @102 -> 10 - 600/102 shares left.
  close(at(res, 'spyCash', '2025-01-08'), 600);
  close(at(res, 'spyValue', '2025-01-17'), (10 - 600 / 102) * 109 + 600);
  close(at(res, 'congressNetFlow', '2025-01-17'), 400);
  assertEqualContributions(res);
});

test('sale larger than the position is capped at its value', () => {
  const res = simulate([
    trade({ amountLow: 1000, amountHigh: 1000 }),
    trade({ txDate: '2025-01-07', type: 'sell', amountLow: 5000, amountHigh: 5000 }), // holds 100 sh @11 = 1100
  ], PRICES);
  const sale = res.tradeLog[1];
  close(sale.value, 1100);
  assert.equal(sale.capped, true);
  assert.equal(res.holdings.length, 0);
  // SPY only holds 10 sh @101 = 1010, so the benchmark sale is capped there.
  close(at(res, 'spyCash', '2025-01-07'), 1010);
  close(sale.spyShortfall, 90);
  assertEqualContributions(res);
});

test('sell with no holdings is skipped and the benchmark is untouched', () => {
  const res = simulate([
    trade({ type: 'sell', amountLow: 1000, amountHigh: 1000 }),
  ], PRICES);
  assert.equal(res.tradeLog[0].status, 'skipped');
  assert.equal(res.tradeLog[0].reason, SKIP_REASONS.NO_HOLDINGS);
  assert.ok(res.congressValue.every((v) => v === 0));
  assert.ok(res.spyValue.every((v) => v === 0));
  assert.equal(res.summary.skippedByReason.no_holdings, 1);
});

test('full sale sells the whole position whatever amount is reported', () => {
  const res = simulate([
    trade({ amountLow: 1000, amountHigh: 1000 }),
    trade({ txDate: '2025-01-09', type: 'sell_full', amountLow: 1001, amountHigh: 15000 }), // 100 sh @13
  ], PRICES);
  const sale = res.tradeLog[1];
  close(sale.value, 1300);
  close(sale.shares, -100);
  assert.equal(res.holdings.length, 0);
  close(at(res, 'congressValue', '2025-01-17'), 1300);
  close(at(res, 'congressCash', '2025-01-17'), 1300);
  // Cash holds a constant value afterwards.
  assert.ok(res.congressValue.slice(3).every((v) => Math.abs(v - 1300) < 1e-6));
  assertEqualContributions(res);
});

test('weekend trade executes at the next trading day close', () => {
  const res = simulate([trade({ txDate: '2025-01-11', amountLow: 1500, amountHigh: 1500 })], PRICES); // Saturday
  const e = res.tradeLog[0];
  assert.equal(e.basisDate, '2025-01-11');
  assert.equal(e.date, '2025-01-13');
  close(e.price, 15);
  close(e.shares, 100);
  close(at(res, 'congressValue', '2025-01-10'), 0);
  close(at(res, 'congressValue', '2025-01-13'), 1500);
});

test('split day: split-adjusted closes keep the value continuous', () => {
  // A 2-for-1 split on 01-09. Raw closes 100,100,100,50,50... become adjusted 50 throughout.
  const SPLT = series('SPLT', DAYS.map(() => 50));
  const res = simulate([trade({ ticker: 'SPLT', amountLow: 5000, amountHigh: 5000 })], { ...PRICES, SPLT });
  assert.ok(res.congressValue.every((v) => Math.abs(v - 5000) < 1e-6));
  close(res.holdings[0].shares, 100);
});

test('delisted ticker: last close carries forward and the holding is flagged', () => {
  // DEAD stops reporting after 2025-01-07 (close 20). Calendar runs to 01-17 (10 days later).
  const DEAD = series('DEAD', [10, 20]);
  const res = simulate([
    trade({ ticker: 'DEAD', amountLow: 1000, amountHigh: 1000 }),
    trade({ ticker: 'DEAD', txDate: '2025-01-14', type: 'sell_full' }),       // no price that day
    trade({ ticker: 'DEAD', txDate: '2025-01-14', type: 'buy', member: 'X' }), // no price that day
  ], { ...PRICES, DEAD }, { staleDays: 5 });
  close(at(res, 'congressValue', '2025-01-17'), 2000);
  const h = res.holdings[0];
  assert.equal(h.ticker, 'DEAD');
  assert.equal(h.priceDate, '2025-01-07');
  assert.equal(h.ended, true);
  assert.deepEqual(res.summary.endedTickers, ['DEAD']);
  assert.ok(res.tradeLog.slice(1).every((e) => e.reason === SKIP_REASONS.NO_PRICE_ON_DATE));
});

test('ticker without a price file is skipped as no_price_data', () => {
  const res = simulate([trade({ ticker: 'NOPE' })], PRICES);
  assert.equal(res.tradeLog[0].reason, SKIP_REASONS.NO_PRICE_DATA);
  assert.equal(res.summary.contributed, 0);
});

test('two members trading the same ticker keep separate ledgers', () => {
  const X = { member: 'Xavier', memberId: 'X000001' };
  const Y = { member: 'Yolanda', memberId: 'Y000001' };
  const res = simulate([
    trade({ ...X, amountLow: 1000, amountHigh: 1000 }),                                    // X: 100 sh
    trade({ ...Y, txDate: '2025-01-07', type: 'sell', amountLow: 500, amountHigh: 500 }),  // Y holds none
    trade({ ...Y, txDate: '2025-01-08', amountLow: 1200, amountHigh: 1200 }),              // Y: 100 sh @12
    trade({ ...X, txDate: '2025-01-09', type: 'sell_full' }),                              // X sells 100 @13
  ], PRICES);
  assert.equal(res.tradeLog[1].reason, SKIP_REASONS.NO_HOLDINGS);
  close(res.tradeLog[3].value, 1300);
  assert.equal(res.holdings.length, 1);
  assert.deepEqual(res.holdings[0].members.map((m) => m.memberId), ['Y000001']);
  close(res.holdings[0].shares, 100);
  assertEqualContributions(res);
});

test('same-day round trip: buys apply before sells', () => {
  const res = simulate([
    trade({ txDate: '2025-01-08', type: 'sell_full' }),                       // listed first
    trade({ txDate: '2025-01-08', amountLow: 1200, amountHigh: 1200 }),
  ], PRICES);
  assert.ok(res.tradeLog.every((e) => e.status === 'applied'));
  close(at(res, 'congressCash', '2025-01-08'), 1200);
  assert.equal(res.holdings.length, 0);
});

test('equal net contributions across a mixed trade history', () => {
  const trades = [];
  const members = ['A', 'B', 'C'];
  DAYS.forEach((d, i) => {
    members.forEach((m, j) => {
      trades.push(trade({ member: m, memberId: m, ticker: j % 2 ? 'BBB' : 'AAA', txDate: d, amountLow: 1000 + 100 * i, amountHigh: 2000 }));
      if (i % 3 === 2) trades.push(trade({ member: m, memberId: m, ticker: j % 2 ? 'BBB' : 'AAA', txDate: d, type: 'sell', amountLow: 1500, amountHigh: 1500 }));
    });
  });
  const res = simulate(trades, PRICES);
  assert.equal(res.summary.spyShortfall, 0);
  assertEqualContributions(res);
  for (let i = 0; i < res.dates.length; i++) {
    close(res.congressCash[i], res.spyCash[i], 'cash equal when no shortfall');
  }
});

test('benchmark sale is capped at the SPY it holds and the shortfall is reported', () => {
  // AAA rises 10 -> 19 while SPY rises only 100 -> 109; selling the AAA gain exceeds SPY held.
  const BIG = series('BIG', DAYS.map((_, i) => 10 * (i + 1)));
  const res = simulate([
    trade({ ticker: 'BIG', amountLow: 1000, amountHigh: 1000 }),
    trade({ ticker: 'BIG', txDate: '2025-01-17', type: 'sell_full' }),
  ], { ...PRICES, BIG });
  const sale = res.tradeLog[1];
  close(sale.value, 10000);
  close(sale.spyShortfall, 10000 - 10 * 109);
  close(res.summary.spyShortfall, 10000 - 1090);
  close(res.summary.spy.cash, 1090);
});

// ---------------------------------------------------------------- options

test('start and end dates bound the trades and the calendar', () => {
  const trades = [
    trade({ txDate: '2025-01-06' }),
    trade({ txDate: '2025-01-10' }),
    trade({ txDate: '2025-01-16' }),
  ];
  const res = simulate(trades, PRICES, { start: '2025-01-08', end: '2025-01-14' });
  assert.equal(res.dates[0], '2025-01-08');
  assert.equal(res.dates.at(-1), '2025-01-14');
  assert.equal(res.tradeLog.length, 1);
  assert.equal(res.tradeLog[0].basisDate, '2025-01-10');
  close(res.summary.contributed, 1000);
});

test('weekend end date: a Saturday trade whose next session is after end is logged', () => {
  const res = simulate([trade({ txDate: '2025-01-11' })], PRICES, { end: '2025-01-11' });
  assert.equal(res.dates.at(-1), '2025-01-10');
  assert.equal(res.tradeLog[0].reason, SKIP_REASONS.AFTER_END);
});

test('disclosure date basis trades on discDate', () => {
  const res = simulate([trade({ txDate: '2025-01-06', discDate: '2025-01-15' })], PRICES, { dateBasis: 'disclosure' });
  assert.equal(res.tradeLog[0].date, '2025-01-15');
  close(res.tradeLog[0].price, 17);
  assert.throws(() => simulate([], PRICES, { dateBasis: 'filed' }));
});

test('disclosure after the last price is logged as no_trading_day', () => {
  const res = simulate([trade({ discDate: '2025-02-01' })], PRICES, { dateBasis: 'disclosure', end: '2025-03-01' });
  assert.equal(res.tradeLog[0].reason, SKIP_REASONS.NO_TRADING_DAY);
});

test('estimator option changes the amount used', () => {
  const t = [trade({ amountLow: 1001, amountHigh: 15000 })];
  close(simulate(t, PRICES, { estimator: 'low' }).summary.contributed, 1001);
  close(simulate(t, PRICES, { estimator: 'mid' }).summary.contributed, 8000.5);
  close(simulate(t, PRICES, { estimator: 'high' }).summary.contributed, 15000);
});

test('filters drop non-matching trades entirely (not logged)', () => {
  const res = simulate([
    trade({ party: 'D' }),
    trade({ party: 'R', member: 'Rob', memberId: 'R1' }),
  ], PRICES, { filters: { party: ['R'] } });
  assert.equal(res.tradeLog.length, 1);
  assert.equal(res.tradeLog[0].memberId, 'R1');
});

test('missing benchmark throws', () => {
  assert.throws(() => simulate([], { AAA }), /benchmark SPY/);
});

test('does not mutate its inputs', () => {
  const trades = [trade(), trade({ type: 'sell_full', txDate: '2025-01-08' })];
  const snapshot = JSON.stringify({ trades, PRICES });
  simulate(trades, PRICES);
  assert.equal(JSON.stringify({ trades, PRICES }), snapshot);
});
