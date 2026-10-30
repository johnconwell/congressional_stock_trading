// members.js: one record per filer, built from the trades (README phase 5).
//
// Pure, no DOM; unit-tested under `node --test`. The pipeline puts each member's
// metadata on every trade (party, state and district as of the trade date), so the
// profile shown in the per-member view uses the member's most recent trade.

import { ageOn } from './sim.js';

/** The key sim.js uses for a member: bioguide ID, or the name when there is none. */
export const memberKey = (t) => t.memberId || t.member;

/**
 * [{ id, name, chamber, chambers, party, state, district, gender, birthDate,
 *    trades, buys, sells, firstTx, lastTx, lastDisc, tickers, sourceUrls }]
 * sorted by name. `chamber`, `party`, `state` and `district` are from the latest trade.
 */
export function buildMemberIndex(trades) {
  const byId = new Map();
  for (const t of trades) {
    const id = memberKey(t);
    if (!id) continue;
    let m = byId.get(id);
    if (!m) {
      m = {
        id, name: t.member, chambers: [], trades: 0, buys: 0, sells: 0,
        firstTx: t.txDate, lastTx: t.txDate, lastDisc: null, tickers: new Set(), latest: t,
      };
      byId.set(id, m);
    }
    m.trades += 1;
    if (t.type === 'buy') m.buys += 1; else m.sells += 1;
    if (t.chamber && !m.chambers.includes(t.chamber)) m.chambers.push(t.chamber);
    if (t.txDate < m.firstTx) m.firstTx = t.txDate;
    if (t.txDate >= m.lastTx) { m.lastTx = t.txDate; m.latest = t; }
    if (t.discDate && (!m.lastDisc || t.discDate > m.lastDisc)) m.lastDisc = t.discDate;
    if (t.ticker) m.tickers.add(t.ticker);
  }
  const out = [];
  for (const m of byId.values()) {
    const l = m.latest;
    out.push({
      id: m.id,
      name: l.member ?? m.name,
      chamber: l.chamber ?? null,
      chambers: m.chambers.sort(),
      party: l.party ?? null,
      state: l.state ?? null,
      district: l.district ?? null,
      gender: l.gender ?? null,
      birthDate: l.birthDate ?? null,
      trades: m.trades,
      buys: m.buys,
      sells: m.sells,
      firstTx: m.firstTx,
      lastTx: m.lastTx,
      lastDisc: m.lastDisc,
      tickers: m.tickers.size,
    });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

/** "R-GA-12" for a House seat, "R-AL" for a Senate seat; at-large House seats (0) show no number. */
export function seatLabel(m) {
  const parts = [m.party, m.state].filter(Boolean);
  if (m.chamber === 'house' && m.district) parts.push(String(m.district));
  return parts.join('-');
}

/** "Tommy Tuberville (R-AL, Senate)", for search lists. */
export function memberLabel(m) {
  const seat = seatLabel(m);
  const chamber = m.chamber === 'senate' ? 'Senate' : m.chamber === 'house' ? 'House' : '';
  const extra = [seat, chamber].filter(Boolean).join(', ');
  return extra ? `${m.name} (${extra})` : m.name;
}

/** Age today (or on `onDate`), or null. */
export function memberAge(m, onDate = new Date().toISOString().slice(0, 10)) {
  return ageOn(m.birthDate, onDate);
}

/** Members whose name, state or seat contains every word of `query` (case-insensitive). */
export function searchMembers(index, query, limit = 20) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return index.slice(0, limit);
  const hits = [];
  for (const m of index) {
    const hay = `${m.name} ${seatLabel(m)} ${m.chamber ?? ''}`.toLowerCase();
    if (words.every((w) => hay.includes(w))) hits.push(m);
    if (hits.length >= limit) break;
  }
  return hits;
}
