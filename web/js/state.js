// state.js: the view settings and their URL query-string form (README phases 4 and 5).
//
// Pure functions, no DOM, so they are unit-tested under `node --test`.
// Only values that differ from the defaults are written to the URL, which keeps
// shared links short:  ?start=2024-01-02&est=high&party=D,R&scale=log
// A per-member view is  ?member=P000197  (bioguide ID, or the name when there is none).

export const DEFAULT_STATE = Object.freeze({
  start: null,            // ISO date; null = first benchmark date
  end: null,              // ISO date; null = last benchmark date
  estimator: 'mid',       // low | mid | high
  dateBasis: 'transaction', // transaction | disclosure
  member: null,           // per-member view: one member key; group filters are then ignored
  chamber: [],            // house | senate
  party: [],
  gender: [],
  ageDecade: [],          // numbers: 40 = ages 40-49
  region: [],
  state: [],              // two-letter codes
  members: [],            // member keys (bioguide IDs)
  owner: [],              // self | spouse | joint | dependent | self_or_dependent | other
  logScale: false,
  showInvested: false,    // dashed invested-only lines
  showNetFlow: true,      // cumulative net contributions line
});

const ESTIMATORS = ['low', 'mid', 'high'];
const BASES = ['transaction', 'disclosure'];
const ISO = /^\d{4}-\d{2}-\d{2}$/;

// state key -> query parameter, in the order they appear in a link
const LIST_PARAMS = {
  chamber: 'chamber',
  party: 'party',
  gender: 'gender',
  ageDecade: 'age',
  region: 'region',
  state: 'state',
  members: 'members',
  owner: 'owner',
};

/** Filter keys that describe a group of members; a per-member view ignores them. */
export const GROUP_FILTERS = Object.freeze(['chamber', 'party', 'gender', 'ageDecade', 'region', 'state', 'members']);

function parseList(value) {
  if (!value) return [];
  return [...new Set(value.split(',').map((s) => s.trim()).filter(Boolean))];
}

/** Read a state object from a query string (with or without the leading '?'). */
export function decodeState(search = '') {
  const p = new URLSearchParams(search);
  const state = { ...DEFAULT_STATE };
  const start = p.get('start');
  const end = p.get('end');
  if (start && ISO.test(start)) state.start = start;
  if (end && ISO.test(end)) state.end = end;
  if (ESTIMATORS.includes(p.get('est'))) state.estimator = p.get('est');
  if (BASES.includes(p.get('basis'))) state.dateBasis = p.get('basis');
  const member = p.get('member')?.trim();
  if (member) state.member = member;
  for (const [key, param] of Object.entries(LIST_PARAMS)) {
    state[key] = parseList(p.get(param));
  }
  state.ageDecade = state.ageDecade.map(Number).filter((n) => Number.isInteger(n) && n >= 0);
  state.state = state.state.map((s) => s.toUpperCase());
  state.chamber = state.chamber.map((s) => s.toLowerCase()).filter((c) => c === 'house' || c === 'senate');
  if (p.get('scale') === 'log') state.logScale = true;
  if (p.has('show')) {
    const show = parseList(p.get('show'));
    state.showInvested = show.includes('invested');
    state.showNetFlow = show.includes('netflow');
  }
  return state;
}

/** Query string (without '?') holding only the settings that differ from the defaults. */
export function encodeState(state) {
  const s = { ...DEFAULT_STATE, ...state };
  const p = new URLSearchParams();
  if (s.member) p.set('member', s.member);
  if (s.start) p.set('start', s.start);
  if (s.end) p.set('end', s.end);
  if (s.estimator !== DEFAULT_STATE.estimator) p.set('est', s.estimator);
  if (s.dateBasis !== DEFAULT_STATE.dateBasis) p.set('basis', s.dateBasis);
  for (const [key, param] of Object.entries(LIST_PARAMS)) {
    if (s[key]?.length) p.set(param, s[key].join(','));
  }
  if (s.logScale) p.set('scale', 'log');
  if (s.showInvested !== DEFAULT_STATE.showInvested || s.showNetFlow !== DEFAULT_STATE.showNetFlow) {
    const show = [];
    if (s.showInvested) show.push('invested');
    if (s.showNetFlow) show.push('netflow');
    p.set('show', show.join(',') || 'none');
  }
  // URLSearchParams escapes commas; they are safe in a query string and easier to read.
  return p.toString().replace(/%2C/g, ',');
}

/**
 * Filters object in the shape sim.js expects. In a per-member view only that
 * member and the owner filter apply; the group filters stay in the state (and
 * the URL) so that going back to all members restores them.
 */
export function filtersOf(state) {
  if (state.member) return { members: [state.member], owner: state.owner ?? [] };
  return {
    chamber: state.chamber ?? [],
    party: state.party ?? [],
    gender: state.gender ?? [],
    ageDecade: state.ageDecade ?? [],
    region: state.region ?? [],
    state: state.state ?? [],
    members: state.members ?? [],
    owner: state.owner ?? [],
  };
}
