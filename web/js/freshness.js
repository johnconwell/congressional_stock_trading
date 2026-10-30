// freshness.js: how current the published data is, for the "Data as of …" line in the
// header (README phase 6). Pure functions of meta.json and the current time, so they are
// unit-tested under Node.

/** The nightly refresh runs once a day; a build older than this means it has stopped. */
export const STALE_BUILD_HOURS = 48;
/** Calendar days between the latest close and now before prices count as behind
 *  (covers weekends and holiday weekends). */
export const STALE_PRICE_DAYS = 5;

const CHAMBER_LABEL = { house: 'House', senate: 'Senate' };
const DAY_MS = 86_400_000;

const dateFmt = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
const stampFmt = new Intl.DateTimeFormat('en-US', {
  month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
});

/** "2026-10-29" -> "Oct 29, 2026" (date-only strings are read as UTC days). */
export function formatDay(iso) {
  if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso ?? '';
  return dateFmt.format(new Date(`${iso}T00:00:00Z`));
}

/** "just now", "3 hours ago", "2 days ago". */
export function relativeAge(hours) {
  if (hours == null || !Number.isFinite(hours)) return '';
  if (hours < 1) return 'just now';
  if (hours < 36) {
    const h = Math.round(hours);
    return `${h} hour${h === 1 ? '' : 's'} ago`;
  }
  const d = Math.round(hours / 24);
  return `${d} day${d === 1 ? '' : 's'} ago`;
}

function parseStamp(s) {
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : new Date(t);
}

/**
 * Freshness of the data described by `meta` at time `now`:
 *   { pricesThrough, disclosures: [{ chamber, label, date }], builtAt, ageHours,
 *     updated, stale, staleReasons: [], runUrl }
 * `updated` is a relative age ("6 hours ago"). `stale` is true when the build is
 * older than STALE_BUILD_HOURS or the latest close is more than STALE_PRICE_DAYS
 * calendar days old.
 */
export function freshness(meta, now = new Date()) {
  const builtAt = parseStamp(meta?.builtAt);
  const ageHours = builtAt ? Math.max(0, (now - builtAt) / 3_600_000) : null;
  const pricesThrough = meta?.prices?.lastDate ?? null;
  const disclosures = Object.entries(meta?.coverage ?? {})
    .filter(([, c]) => c?.lastDiscDate)
    .map(([chamber, c]) => ({ chamber, label: CHAMBER_LABEL[chamber] ?? chamber, date: c.lastDiscDate }));

  const staleReasons = [];
  if (ageHours != null && ageHours > STALE_BUILD_HOURS) {
    staleReasons.push(`The data hasn't been refreshed for ${relativeAge(ageHours).replace(' ago', '')}.`);
  }
  if (pricesThrough) {
    const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
    const behind = Math.round((today - Date.parse(`${pricesThrough}T00:00:00Z`)) / DAY_MS);
    if (behind > STALE_PRICE_DAYS) {
      staleReasons.push(`The latest closing price is from ${formatDay(pricesThrough)}, ${behind} days ago.`);
    }
  }
  return {
    pricesThrough,
    disclosures,
    builtAt,
    ageHours,
    updated: relativeAge(ageHours),
    stale: staleReasons.length > 0,
    staleReasons,
    runUrl: typeof meta?.buildRun?.url === 'string' && /^https:\/\//.test(meta.buildRun.url) ? meta.buildRun.url : null,
  };
}

/** Exact build time for a tooltip, in the viewer's time zone. */
export function formatStamp(date) {
  return date ? stampFmt.format(date) : '';
}
