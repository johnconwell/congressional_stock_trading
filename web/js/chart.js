// chart.js: Chart.js time-series rendering (README section 3.1, phase 4).
//
// Relies on the vendored UMD builds loaded by index.html, which set the globals
// Chart (with the date-fns adapter and zoom plugin registered) and Hammer.

const COLORS = {
  congress: '#c2410c',
  spy: '#1d4ed8',
  flow: '#6b7280',
};

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const usdCompact = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1,
});

let chart = null;
let zoomListener = null;

/** Call fn(isZoomed) whenever the user zooms or pans. */
export function onZoomChange(fn) {
  zoomListener = fn;
}
const notifyZoom = () => zoomListener?.(isZoomed());

function line(label, color, data, { dashed = false, width = 2, hidden = false } = {}) {
  return {
    label,
    data,
    borderColor: color,
    backgroundColor: color,
    borderWidth: width,
    borderDash: dashed ? [6, 4] : [],
    pointRadius: 0,
    pointHoverRadius: 3,
    tension: 0,
    spanGaps: false,
    hidden,
  };
}

/** Values for the chart; on a log axis, zero and negative values become gaps. */
function series(values, log) {
  return log ? values.map((v) => (v > 0 ? v : null)) : values;
}

function datasets(result, opts) {
  const { logScale: log, showInvested, showNetFlow } = opts;
  const name = opts.congressLabel ?? 'Congress pool';
  const congressInvested = result.congressValue.map((v, i) => v - result.congressCash[i]);
  const spyInvested = result.spyValue.map((v, i) => v - result.spyCash[i]);
  const sets = [
    line(name, COLORS.congress, series(result.congressValue, log), { width: 2.25 }),
    line('S&P 500 pool (SPY)', COLORS.spy, series(result.spyValue, log), { width: 2.25 }),
  ];
  if (showInvested) {
    sets.push(line(`${name} invested`, COLORS.congress, series(congressInvested, log), { dashed: true, width: 1.5 }));
    sets.push(line('SPY invested', COLORS.spy, series(spyInvested, log), { dashed: true, width: 1.5 }));
  }
  if (showNetFlow) {
    sets.push(line('Net contributions', COLORS.flow, series(result.congressNetFlow, log), { width: 1.5 }));
  }
  return sets;
}

/**
 * Draw or update the chart.
 * opts: { logScale, showInvested, showNetFlow, congressLabel }
 */
export function renderChart(canvas, result, opts) {
  const data = { labels: result.dates, datasets: datasets(result, opts) };
  const yType = opts.logScale ? 'logarithmic' : 'linear';

  if (chart) {
    chart.data = data;
    chart.options.scales.y.type = yType;
    chart.update('none');
    return chart;
  }

  const { Chart } = globalThis;
  if (!Chart) throw new Error('Chart.js failed to load (web/vendor/chart.umd.min.js).');

  chart = new Chart(canvas, {
    type: 'line',
    data,
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      normalized: true,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'month', tooltipFormat: 'EEE, MMM d, yyyy', displayFormats: { month: 'MMM yyyy' } },
          ticks: { maxRotation: 0, autoSkipPadding: 16 },
          grid: { display: false },
        },
        y: {
          type: yType,
          ticks: { callback: (v) => usdCompact.format(v), maxTicksLimit: 8 },
          grid: { color: 'rgba(0,0,0,0.06)' },
        },
      },
      plugins: {
        legend: { position: 'top', align: 'start', labels: { boxWidth: 14, boxHeight: 2, usePointStyle: false } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y == null ? 'n/a' : usd.format(ctx.parsed.y)}`,
          },
        },
        zoom: {
          limits: { x: { min: 'original', max: 'original' } },
          pan: { enabled: true, mode: 'x', modifierKey: 'shift', onPanComplete: notifyZoom },
          zoom: {
            mode: 'x',
            drag: { enabled: true, backgroundColor: 'rgba(29,78,216,0.08)', borderColor: 'rgba(29,78,216,0.4)', borderWidth: 1 },
            wheel: { enabled: true, modifierKey: 'ctrl' },
            pinch: { enabled: true },
            onZoomComplete: notifyZoom,
          },
        },
      },
    },
  });
  return chart;
}

export function resetZoom() {
  chart?.resetZoom?.();
  notifyZoom();
}

export function isZoomed() {
  return chart?.isZoomedOrPanned?.() ?? false;
}
