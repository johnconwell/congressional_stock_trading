// ui.js: controls, summary cards, holdings table, trade log and the per-member view
// (README phases 4 to 6).

import { simulate, ageDecadeOn, regionOf, SKIP_REASONS } from './sim.js';
import { createLoader, tickersNeeded, tickersWithPrices } from './data.js';
import { DEFAULT_STATE, decodeState, encodeState, filtersOf } from './state.js';
import { buildMemberIndex, memberAge, memberLabel, seatLabel } from './members.js';
import { renderChart, resetZoom, isZoomed, onZoomChange } from './chart.js';
import { freshness, formatDay, formatStamp } from './freshness.js';

// ---------------------------------------------------------------- formatting

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const usd2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const shares4 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 });
const pct = new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
const signedUsd = (v) => (v > 0 ? '+' : v < 0 ? '−' : '') + usd.format(Math.abs(v));
const signedPct = (v) => (v == null ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + pct.format(Math.abs(v)));
const tone = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

const PARTY_LABEL = { D: 'Democrat', R: 'Republican', I: 'Independent', L: 'Libertarian' };
const CHAMBER_LABEL = { house: 'House', senate: 'Senate' };
const CHIP_FILTERS = ['chamber', 'party', 'gender', 'ageDecade', 'region', 'owner'];
const PICKERS = ['state', 'members'];
const GENDER_LABEL = { F: 'Female', M: 'Male' };
const REGIONS = ['Northeast', 'Midwest', 'South', 'West', 'Territories'];
const OWNER_LABEL = {
  self: 'Self', spouse: 'Spouse', joint: 'Joint', dependent: 'Dependent',
  self_or_dependent: 'Self/dep.', other: 'Other',
};
const TYPE_LABEL = { buy: 'Buy', sell: 'Sell', sell_full: 'Sell (full)' };
const REASON_LABEL = {
  [SKIP_REASONS.NO_PRICE_DATA]: 'No price data',
  [SKIP_REASONS.NO_PRICE_ON_DATE]: 'No price on date',
  [SKIP_REASONS.NO_HOLDINGS]: 'Sale with no matching holdings',
  [SKIP_REASONS.NO_TRADING_DAY]: 'No trading day yet',
  [SKIP_REASONS.AFTER_END]: 'Executes after end date',
  [SKIP_REASONS.BAD_AMOUNT]: 'Missing amount',
};

// ---------------------------------------------------------------- DOM

const $ = (sel) => document.querySelector(sel);
const form = $('#controls');
const el = {
  start: $('#start'),
  end: $('#end'),
  status: $('#status'),
  coverage: $('#coverage'),
  cards: $('#cards'),
  asof: $('#asof'),
  canvas: $('#chart'),
  chartTitle: $('#chart-title'),
  memberView: $('#member-view'),
  memberSearch: $('#member-search'),
  memberOptions: $('#member-options'),
  stateOptions: $('#state-options'),
  groupFilters: $('#group-filters'),
  showInvested: $('#showInvested'),
  showNetFlow: $('#showNetFlow'),
  logScale: $('#logScale'),
  resetZoom: $('#reset-zoom'),
  holdingsBody: $('#holdings tbody'),
  holdingsCount: $('#holdings-count'),
  logBody: $('#tradelog tbody'),
  logCount: $('#log-count'),
  logSearch: $('#log-search'),
  logStatus: $('#log-status'),
  logPager: $('#log-pager'),
};

// ---------------------------------------------------------------- app state

const data = createLoader({ base: 'data/' });
let meta = null;
let trades = [];
let calendar = [];        // benchmark dates: bounds for the date inputs
let available = null;     // Set of tickers that have a price file, or null = unknown
let state = decodeState(location.search);
let memberIndex = [];     // buildMemberIndex(trades)
let memberById = new Map();
const picked = { state: [], members: [] }; // picker selections (mirrors state)
const pickerOptions = { state: new Map(), members: new Map() }; // label -> value
let result = null;
let runId = 0;

const holdingsView = { key: 'value', dir: -1 };
const logView = { key: 'date', dir: -1, page: 0, pageSize: 100, query: '', status: '' };

function setStatus(text, kind = '') {
  el.status.textContent = text;
  el.status.className = `status ${kind}`;
}

// ---------------------------------------------------------------- controls

/** Distinct values for each member filter, from the trades. */
function filterOptions() {
  const sets = { chamber: new Set(), party: new Set(), gender: new Set(), ages: new Set(), regions: new Set(),
    owner: new Set(), state: new Set() };
  for (const t of trades) {
    if (t.chamber) sets.chamber.add(t.chamber);
    if (t.party) sets.party.add(t.party);
    if (t.gender) sets.gender.add(t.gender);
    if (t.owner) sets.owner.add(t.owner);
    if (t.state) sets.state.add(t.state);
    const dec = ageDecadeOn(t.birthDate, t.txDate);
    if (dec != null) sets.ages.add(dec);
    const r = regionOf(t.state);
    if (r) sets.regions.add(r);
  }
  const order = (labels) => (a, b) => (labels.indexOf(a) + 1 || 99) - (labels.indexOf(b) + 1 || 99) || String(a).localeCompare(String(b));
  const labelled = (set, labels) => [...set].sort(order(Object.keys(labels))).map((v) => [v, labels[v] ?? v]);
  return {
    chamber: labelled(sets.chamber, CHAMBER_LABEL),
    party: labelled(sets.party, PARTY_LABEL),
    gender: labelled(sets.gender, GENDER_LABEL),
    ageDecade: [...sets.ages].sort((a, b) => a - b).map((v) => [String(v), `${v}s`]),
    region: REGIONS.filter((r) => sets.regions.has(r)).map((r) => [r, r]),
    owner: labelled(sets.owner, OWNER_LABEL),
    state: [...sets.state].sort().map((v) => [v, `${v} · ${regionOf(v) ?? ''}`.replace(/ · $/, '')]),
    members: memberIndex.map((m) => [m.id, memberLabel(m)]),
  };
}

function buildChips() {
  const opts = filterOptions();
  for (const box of form.querySelectorAll('.chips')) {
    const key = box.dataset.filter;
    box.innerHTML = opts[key].map(([value, label]) => (
      `<label class="chip"><input type="checkbox" name="${key}" value="${esc(value)}"><span>${esc(label)}</span></label>`
    )).join('') || '<span class="hint">No data</span>';
  }
  for (const key of PICKERS) {
    pickerOptions[key] = new Map(opts[key].map(([value, label]) => [label, value]));
  }
  el.stateOptions.innerHTML = opts.state.map(([, label]) => `<option value="${esc(label)}">`).join('');
  el.memberOptions.innerHTML = opts.members.map(([, label]) => `<option value="${esc(label)}">`).join('');
}

/** Label shown on a picked chip. */
function pickedLabel(key, value) {
  if (key === 'members') return memberById.get(value)?.name ?? value;
  return value;
}

function renderPickers() {
  for (const key of PICKERS) {
    const box = form.querySelector(`.picker[data-picker="${key}"] .picked`);
    box.innerHTML = picked[key].map((v) => (
      `<button type="button" class="picked-chip" data-picker-key="${key}" data-value="${esc(v)}" title="Remove">${esc(pickedLabel(key, v))}</button>`
    )).join('');
  }
}

/** Value for what the user typed or picked in a picker, or null. */
function pickerValue(key, text) {
  const t = text.trim();
  if (!t) return null;
  if (pickerOptions[key].has(t)) return pickerOptions[key].get(t);
  const values = new Set(pickerOptions[key].values());
  if (values.has(t.toUpperCase())) return t.toUpperCase(); // a typed state code or bioguide ID
  if (values.has(t)) return t;
  return null;
}

function writeControls() {
  el.start.value = state.start ?? calendar[0] ?? '';
  el.end.value = state.end ?? calendar.at(-1) ?? '';
  form.querySelector(`input[name="estimator"][value="${state.estimator}"]`).checked = true;
  form.querySelector(`input[name="dateBasis"][value="${state.dateBasis}"]`).checked = true;
  for (const key of CHIP_FILTERS) {
    const selected = new Set(state[key].map(String));
    for (const box of form.querySelectorAll(`input[name="${key}"]`)) box.checked = selected.has(box.value);
  }
  for (const key of PICKERS) picked[key] = [...state[key]];
  renderPickers();
  el.showInvested.checked = state.showInvested;
  el.showNetFlow.checked = state.showNetFlow;
  el.logScale.checked = state.logScale;
}

function readControls() {
  const checked = (name) => [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
  const first = calendar[0];
  const last = calendar.at(-1);
  let start = el.start.value || null;
  let end = el.end.value || null;
  if (start && end && start > end) [start, end] = [end, start];
  return {
    // Store dates only when they differ from the data's bounds, so links stay current.
    start: start && start !== first ? start : null,
    end: end && end !== last ? end : null,
    estimator: form.querySelector('input[name="estimator"]:checked')?.value ?? 'mid',
    dateBasis: form.querySelector('input[name="dateBasis"]:checked')?.value ?? 'transaction',
    member: state.member,
    chamber: checked('chamber'),
    party: checked('party'),
    gender: checked('gender'),
    ageDecade: checked('ageDecade').map(Number),
    region: checked('region'),
    state: [...picked.state],
    members: [...picked.members],
    owner: checked('owner'),
    showInvested: el.showInvested.checked,
    showNetFlow: el.showNetFlow.checked,
    logScale: el.logScale.checked,
  };
}

function urlFor(s) {
  const qs = encodeState(s);
  return `${location.pathname}${qs ? `?${qs}` : ''}${location.hash}`;
}

/** Write the state to the URL. `push` adds a history entry (opening or leaving a member view). */
function syncUrl(push = false) {
  const url = urlFor(state);
  if (url === `${location.pathname}${location.search}${location.hash}`) return;
  if (push) history.pushState(null, '', url);
  else history.replaceState(null, '', url);
}

/** Open the per-member view (id = member key), or go back to all members (null). */
function showMember(id) {
  state = { ...readControls(), member: id || null };
  syncUrl(true);
  el.memberSearch.value = '';
  scheduleRun(0);
  if (id) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/** href for a member link, so it also works with middle-click / open in new tab. */
const memberHref = (id) => urlFor({ ...state, member: id });
const memberLink = (id, name) => (
  id ? `<a class="member-link" href="${esc(memberHref(id))}" data-member="${esc(id)}">${esc(name)}</a>` : esc(name)
);

let debounce = null;
function scheduleRun(delay = 120) {
  clearTimeout(debounce);
  debounce = setTimeout(run, delay);
}

// ---------------------------------------------------------------- run

async function run() {
  const id = ++runId;
  state = readControls();
  syncUrl();
  const focus = state.member ? memberById.get(state.member) : null;
  renderMemberView(focus);
  const start = state.start ?? calendar[0];
  const end = state.end ?? calendar.at(-1);
  const options = {
    start, end, estimator: state.estimator, dateBasis: state.dateBasis, filters: filtersOf(state),
  };

  const bench = meta?.prices?.benchmark ?? 'SPY';
  let needed = [...tickersNeeded(trades, options)];
  if (available) needed = needed.filter((t) => available.has(t));
  needed.push(bench);

  setStatus(`Loading prices for ${num.format(needed.length)} tickers…`, 'busy');
  let prices;
  try {
    prices = await data.prices(needed, {
      onProgress: (done, total) => {
        if (id === runId && done < total) setStatus(`Loading prices… ${done} / ${total}`, 'busy');
      },
    });
  } catch (err) {
    if (id === runId) setStatus(`Could not load prices: ${err.message}`, 'error');
    return;
  }
  if (id !== runId) return; // a newer run started while we were loading

  const t0 = performance.now();
  try {
    result = simulate(trades, prices, { ...options, benchmark: bench });
  } catch (err) {
    setStatus(`Simulation failed: ${err.message}`, 'error');
    console.error(err);
    return;
  }
  const ms = performance.now() - t0;

  renderChart(el.canvas, result, chartOpts());
  el.resetZoom.hidden = !isZoomed();
  renderCards(result.summary);
  renderCoverage(result.summary);
  logView.page = 0;
  renderHoldings();
  renderTradeLog();
  const s = result.summary;
  if (state.member && !focus) {
    setStatus(`No trades found for member "${state.member}". Use "All members" to go back.`, 'error');
    return;
  }
  setStatus(`${num.format(s.applied)} trades applied, ${num.format(s.skipped)} skipped · ${s.start ?? '—'} to ${s.end ?? '—'} · ${ms.toFixed(0)} ms`);
}

// ---------------------------------------------------------------- member view

function poolName() {
  const m = state.member ? memberById.get(state.member) : null;
  return m ? `${m.name}'s trades` : 'Congress pool';
}

function chartOpts() {
  return { ...state, congressLabel: poolName() };
}

function renderMemberView(m) {
  const on = Boolean(state.member);
  el.groupFilters.classList.toggle('off', on);
  el.groupFilters.querySelector('.group-off').hidden = !on;
  el.chartTitle.textContent = m ? `Pool value over time · ${m.name}` : 'Pool value over time';
  if (!on) {
    el.memberView.hidden = true;
    el.memberView.innerHTML = '';
    return;
  }
  el.memberView.hidden = false;
  if (!m) {
    el.memberView.innerHTML = `<div><h2>Unknown member</h2><p class="seat">No trades for “${esc(state.member)}” in the data.</p></div>
      <div class="member-actions"><button type="button" class="btn" data-action="all-members">← All members</button></div>`;
    return;
  }
  const age = memberAge(m);
  const chambers = m.chambers.map((c) => CHAMBER_LABEL[c] ?? c).join(' and ');
  const bioguide = /^[A-Z]\d{6}$/.test(m.id) ? `https://bioguide.congress.gov/search/bio/${m.id}` : null;
  const party = PARTY_LABEL[m.party] ?? m.party ?? '';
  el.memberView.innerHTML = `
    <div>
      <h2>${esc(m.name)}</h2>
      <p class="seat"><span class="party-${esc(m.party ?? '')}">${esc(party)}</span>${party ? ' · ' : ''}${esc(seatLabel(m))} · ${esc(chambers)}${age != null ? ` · age ${age}` : ''}</p>
      <dl class="member-facts">
        <div><dt>Disclosed trades</dt><dd>${num.format(m.trades)} (${num.format(m.buys)} buys, ${num.format(m.sells)} sales)</dd></div>
        <div><dt>Tickers</dt><dd>${num.format(m.tickers)}</dd></div>
        <div><dt>Trade dates</dt><dd>${esc(m.firstTx)} to ${esc(m.lastTx)}</dd></div>
        <div><dt>Last disclosure</dt><dd>${esc(m.lastDisc ?? '—')}</dd></div>
      </dl>
    </div>
    <div class="member-actions">
      ${bioguide ? `<a href="${bioguide}" target="_blank" rel="noopener">Biography ↗</a>` : ''}
      <button type="button" class="btn" data-action="all-members">← All members</button>
    </div>`;
}

// ---------------------------------------------------------------- summary cards

function renderCards(s) {
  const card = (title, pool, color) => `
    <div class="card">
      <div class="card-title"><span class="swatch" style="background:${color}"></span>${title}</div>
      <div class="card-value">${usd.format(pool.value)}</div>
      <div class="card-gain ${tone(pool.gain)}">${signedUsd(pool.gain)} <span>(${signedPct(pool.gainPct)})</span></div>
      <dl class="card-rows">
        <div><dt>Invested</dt><dd>${usd.format(pool.invested)}</dd></div>
        <div><dt>Cash</dt><dd>${usd.format(pool.cash)}</dd></div>
      </dl>
    </div>`;
  const diffPct = s.spy.value > 0 ? s.difference / s.spy.value : null;
  el.cards.innerHTML = `
    ${card(esc(poolName()), s.congress, '#c2410c')}
    ${card('S&amp;P 500 pool', s.spy, '#1d4ed8')}
    <div class="card">
      <div class="card-title">Difference (${state.member ? 'member' : 'Congress'} − S&amp;P 500)</div>
      <div class="card-value ${tone(s.difference)}">${signedUsd(s.difference)}</div>
      <div class="card-gain ${tone(s.difference)}">${signedPct(diffPct)} <span>vs S&amp;P 500 pool</span></div>
      <dl class="card-rows">
        <div><dt>Total contributed</dt><dd>${usd.format(s.contributed)}</dd></div>
        <div><dt>Gain difference</dt><dd>${s.congress.gainPct == null ? '—' : `${((s.congress.gainPct - s.spy.gainPct) * 100).toFixed(1)} pts`}</dd></div>
      </dl>
    </div>`;
}

function renderCoverage(s) {
  const notes = [];
  const pending = meta?.prices?.pending?.length ?? 0;
  const total = meta?.tickers?.length ?? 0;
  if (pending > 0 && total > 0) {
    notes.push(`Prices have been downloaded for ${num.format(total - pending)} of ${num.format(total)} tickers so far.
      Trades in the other ${num.format(pending)} are skipped as <em>no price data</em>, so the Congress pool
      only covers part of the trades. Run <code>python build.py --prices-only</code> to fetch more.`);
  }
  if (meta?.coverage && !meta.coverage.senate) {
    notes.push('No Senate trades in the data yet. Run <code>python build.py</code> (or <code>--sources senate</code>) to add them.');
  }
  for (const [source, err] of Object.entries(meta?.sourceErrors ?? {})) {
    notes.push(`The last data build couldn't reach <strong>${esc(source)}</strong>, so its trades are from an earlier build (${esc(String(err).slice(0, 140))}).`);
  }
  const noPrice = s.skippedByReason[SKIP_REASONS.NO_PRICE_DATA] ?? 0;
  if (noPrice > 0 && s.applied + s.skipped > 0) {
    notes.push(`${num.format(noPrice)} of ${num.format(s.applied + s.skipped)} trades in this view (${pct.format(noPrice / (s.applied + s.skipped))}) have no price data.`);
  }
  if (s.spyShortfall > 0.5) {
    notes.push(`The S&amp;P 500 pool ran out of SPY to sell on some sales (shortfall ${usd.format(s.spyShortfall)}), so its net contributions are higher than the Congress pool's.`);
  }
  if (s.endedTickers.length) {
    notes.push(`Stopped reporting prices (last close carried forward): ${s.endedTickers.map(esc).join(', ')}.`);
  }
  el.coverage.hidden = notes.length === 0;
  el.coverage.innerHTML = notes.map((n) => `<p>${n}</p>`).join('');
}

// ---------------------------------------------------------------- tables

function compare(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b));
}

function markSorted(table, view) {
  for (const th of document.querySelectorAll(`${table} th[data-sort]`)) {
    const on = th.dataset.sort === view.key;
    th.setAttribute('aria-sort', on ? (view.dir > 0 ? 'ascending' : 'descending') : 'none');
  }
}

function renderHoldings() {
  const rows = [...(result?.holdings ?? [])];
  const key = holdingsView.key;
  const val = (h) => (key === 'members' ? h.members.length : h[key]);
  rows.sort((a, b) => holdingsView.dir * compare(val(a), val(b)));
  markSorted('#holdings', holdingsView);
  el.holdingsCount.textContent = rows.length ? `(${num.format(rows.length)})` : '';
  if (!rows.length) {
    el.holdingsBody.innerHTML = '<tr><td colspan="6" class="empty">No open positions.</td></tr>';
    return;
  }
  el.holdingsBody.innerHTML = rows.map((h) => {
    const names = h.members.map((m) => m.member);
    const shown = h.members.slice(0, 3).map((m) => memberLink(m.memberId, m.member)).join(', ');
    const more = names.length > 3 ? ` <span class="muted" title="${esc(names.slice(3).join(', '))}">+${names.length - 3} more</span>` : '';
    const ended = h.ended ? ` <span class="badge" title="No close since ${esc(h.priceDate)}">ended</span>` : '';
    return `<tr>
      <td class="ticker">${esc(h.ticker)}${ended}</td>
      <td class="num">${shares4.format(h.shares)}</td>
      <td class="num" title="Close on ${esc(h.priceDate)}">${usd2.format(h.price)}</td>
      <td class="num">${usd.format(h.value)}</td>
      <td class="num">${pct.format(h.weight)}</td>
      <td class="members">${shown}${more}</td>
    </tr>`;
  }).join('');
}

function filteredLog() {
  const q = logView.query.trim().toLowerCase();
  let rows = result?.tradeLog ?? [];
  if (logView.status) rows = rows.filter((e) => e.status === logView.status);
  if (q) {
    rows = rows.filter((e) => (e.ticker ?? '').toLowerCase().includes(q)
      || (e.member ?? '').toLowerCase().includes(q)
      || (REASON_LABEL[e.reason] ?? '').toLowerCase().includes(q));
  }
  const key = logView.key;
  const val = (e) => (key === 'date' ? e.date ?? e.basisDate : key === 'status' ? `${e.status}:${e.reason ?? ''}` : e[key]);
  return [...rows].sort((a, b) => logView.dir * compare(val(a), val(b)) || compare(a.id, b.id));
}

function renderTradeLog() {
  const rows = filteredLog();
  const total = result?.tradeLog.length ?? 0;
  const pages = Math.max(1, Math.ceil(rows.length / logView.pageSize));
  logView.page = Math.min(logView.page, pages - 1);
  const slice = rows.slice(logView.page * logView.pageSize, (logView.page + 1) * logView.pageSize);
  markSorted('#tradelog', logView);
  el.logCount.textContent = total ? `(${num.format(rows.length)}${rows.length !== total ? ` of ${num.format(total)}` : ''})` : '';

  el.logBody.innerHTML = slice.length ? slice.map((e) => {
    const shifted = e.date && e.date !== e.basisDate;
    const dateCell = `${esc(e.date ?? e.basisDate)}${shifted ? `<span class="muted small" title="Reported ${esc(e.basisDate)}; executed on the next trading day"> ↳ ${esc(e.basisDate)}</span>` : ''}`;
    const status = e.status === 'applied'
      ? `<span class="pill ok">Applied</span>${e.capped ? ' <span class="pill warn" title="Sale reduced to the value of the position">capped</span>' : ''}${e.spyShortfall > 0.005 ? ` <span class="pill warn" title="SPY shortfall ${usd.format(e.spyShortfall)}">SPY short</span>` : ''}`
      : `<span class="pill skip">${esc(REASON_LABEL[e.reason] ?? e.reason)}</span>`;
    const flags = (e.flags ?? []).length ? ` <span class="muted small" title="${esc(e.flags.join(', '))}">*</span>` : '';
    return `<tr class="${e.status}">
      <td class="date">${dateCell}</td>
      <td>${memberLink(e.memberId, e.member)}</td>
      <td>${esc(OWNER_LABEL[e.owner] ?? e.owner ?? '')}</td>
      <td class="ticker">${esc(e.ticker)}</td>
      <td class="type-${esc(e.type)}">${esc(TYPE_LABEL[e.type] ?? e.type)}</td>
      <td class="num">${e.amount == null ? '—' : usd.format(e.amount)}${flags}</td>
      <td class="num">${e.price == null ? '—' : usd2.format(e.price)}</td>
      <td class="num">${e.value == null ? '—' : usd.format(e.value)}</td>
      <td>${status}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="9" class="empty">No trades match.</td></tr>';

  el.logPager.innerHTML = pages > 1 ? `
    <button type="button" class="btn btn-small" data-page="prev" ${logView.page === 0 ? 'disabled' : ''}>‹ Prev</button>
    <span>Page ${logView.page + 1} of ${pages}</span>
    <button type="button" class="btn btn-small" data-page="next" ${logView.page >= pages - 1 ? 'disabled' : ''}>Next ›</button>` : '';
}

function bindSortable(table, view, render, numericKeys) {
  document.querySelector(`${table} thead`).addEventListener('click', (ev) => {
    const th = ev.target.closest('th[data-sort]');
    if (!th) return;
    const key = th.dataset.sort;
    if (view.key === key) view.dir = -view.dir;
    else { view.key = key; view.dir = numericKeys.includes(key) ? -1 : 1; }
    if ('page' in view) view.page = 0;
    render();
  });
}

// ---------------------------------------------------------------- events

function bindEvents() {
  form.addEventListener('change', (ev) => {
    if (ev.target.type === 'date') return; // handled below, so typing doesn't trigger runs
    if (ev.target === el.memberSearch || ev.target.closest('.picker')) return; // handled below
    scheduleRun();
  });

  // Pickers: choosing a datalist option (or typing an exact code) adds it.
  for (const key of PICKERS) {
    const input = form.querySelector(`.picker[data-picker="${key}"] input`);
    const add = () => {
      const v = pickerValue(key, input.value);
      if (v == null) return;
      if (!picked[key].includes(v)) picked[key].push(v);
      input.value = '';
      renderPickers();
      scheduleRun();
    };
    input.addEventListener('input', () => { if (pickerOptions[key].has(input.value.trim())) add(); });
    input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); add(); } });
  }
  form.addEventListener('click', (ev) => {
    const chip = ev.target.closest('.picked-chip');
    if (!chip) return;
    const key = chip.dataset.pickerKey;
    picked[key] = picked[key].filter((v) => v !== chip.dataset.value);
    renderPickers();
    scheduleRun();
  });

  const openFromSearch = () => {
    const id = pickerValue('members', el.memberSearch.value);
    if (id) showMember(id);
  };
  el.memberSearch.addEventListener('input', () => { if (pickerOptions.members.has(el.memberSearch.value.trim())) openFromSearch(); });
  el.memberSearch.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); openFromSearch(); } });

  // Member names in the tables, and the "All members" button.
  document.addEventListener('click', (ev) => {
    const link = ev.target.closest('a.member-link');
    if (link && !(ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button === 1)) {
      ev.preventDefault();
      showMember(link.dataset.member);
      return;
    }
    if (ev.target.closest('[data-action="all-members"]')) showMember(null);
  });
  for (const input of [el.start, el.end]) input.addEventListener('change', () => scheduleRun(300));

  const redrawChart = () => {
    state = readControls();
    syncUrl();
    if (result) renderChart(el.canvas, result, chartOpts());
    el.resetZoom.hidden = !isZoomed();
  };
  for (const box of [el.showInvested, el.showNetFlow, el.logScale]) box.addEventListener('change', redrawChart);

  onZoomChange((zoomed) => { el.resetZoom.hidden = !zoomed; });
  el.resetZoom.addEventListener('click', resetZoom);

  $('#reset').addEventListener('click', () => {
    state = { ...DEFAULT_STATE };
    writeControls();
    scheduleRun(0);
  });

  $('#copy-link').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    try {
      await navigator.clipboard.writeText(location.href);
      btn.textContent = 'Copied';
    } catch {
      window.prompt('Copy this link:', location.href);
    }
    setTimeout(() => { btn.textContent = 'Copy link'; }, 1500);
  });

  bindSortable('#holdings', holdingsView, renderHoldings, ['shares', 'price', 'value', 'weight', 'members']);
  bindSortable('#tradelog', logView, renderTradeLog, ['date', 'amount', 'price', 'value']);

  let searchTimer = null;
  el.logSearch.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { logView.query = el.logSearch.value; logView.page = 0; renderTradeLog(); }, 150);
  });
  el.logStatus.addEventListener('change', () => { logView.status = el.logStatus.value; logView.page = 0; renderTradeLog(); });
  el.logPager.addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-page]');
    if (!b) return;
    logView.page += b.dataset.page === 'next' ? 1 : -1;
    renderTradeLog();
    document.querySelector('#tradelog').scrollIntoView({ block: 'nearest' });
  });

  // Back/forward or a pasted URL with a different query string.
  window.addEventListener('popstate', () => {
    state = decodeState(location.search);
    writeControls();
    scheduleRun(0);
  });
}

// ---------------------------------------------------------------- data freshness

/** Header line: "Data as of <latest close> · Disclosures through … · Updated … ago". */
function renderFreshness() {
  const f = freshness(meta, new Date());
  const parts = [];
  if (f.pricesThrough) parts.push(`Data as of <strong>${esc(formatDay(f.pricesThrough))}</strong> close`);
  if (f.disclosures.length) {
    parts.push(`Disclosures through ${f.disclosures
      .map((d) => `${esc(d.label)} <strong>${esc(formatDay(d.date))}</strong>`).join(', ')}`);
  }
  if (f.updated) {
    const when = esc(`Updated ${f.updated}`);
    parts.push(f.runUrl ? `<a href="${esc(f.runUrl)}" rel="noopener" target="_blank">${when}</a>` : when);
  }
  if (f.stale) parts.push('<span class="stale-badge">Out of date</span>');
  el.asof.innerHTML = parts.join(' · ');
  el.asof.classList.toggle('stale', f.stale);
  el.asof.title = [
    f.builtAt ? `Built ${formatStamp(f.builtAt)}` : '',
    ...f.staleReasons,
  ].filter(Boolean).join('\n');
}

// ---------------------------------------------------------------- start

async function init() {
  setStatus('Loading trades…', 'busy');
  try {
    [meta, trades] = await Promise.all([data.meta(), data.trades()]);
  } catch (err) {
    setStatus(err.message, 'error');
    return;
  }
  available = tickersWithPrices(meta);
  const bench = meta?.prices?.benchmark ?? 'SPY';
  const benchPrices = await data.prices([bench]);
  calendar = benchPrices[bench]?.dates ?? [];
  if (!calendar.length) {
    setStatus(`No ${bench} price file found. Run the price step of the pipeline (README section 5).`, 'error');
    return;
  }
  for (const input of [el.start, el.end]) {
    input.min = calendar[0];
    input.max = calendar.at(-1);
  }
  renderFreshness();
  setInterval(renderFreshness, 10 * 60 * 1000); // keep "updated … ago" current

  memberIndex = buildMemberIndex(trades);
  memberById = new Map(memberIndex.map((m) => [m.id, m]));

  buildChips();
  writeControls();
  bindEvents();
  await run();
}

init();
