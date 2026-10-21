/* ==========================================================================
   xshark console -- the paper bot's live window, settled windows, fill
   quality, earnings and tuning. Built in Bo-BOTT's console UI: the same
   stylesheet, page shape and idioms (el / mount / legend / tipRows /
   niceTicks, hash router, theme toggle), so the consoles read as one product.

   The page only READS the bot's files through the server and POSTs tuning;
   it never drives the trading loop. Every clock on the page is GMT+9 with its
   date (the user's rule for every dashboard); "m:ss left" axes are durations.

   Charts are hand-rolled SVG with Bo-BOTT's mark specs: 2px lines, >= 8px
   markers with a 2px surface ring, hairline gridlines, <= 24px bars with a
   rounded data end. Colour is bound to the entity: Up = slot 1, Down = slot
   2, Chainlink = slot 3; maker bids and taker clips have their own validated
   pair (xshark.css).
   ========================================================================== */

'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const stored = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
const store = (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } };

const WINDOW_S = 300;
const MARKET_COINS = ['btc', 'eth', 'sol', 'xrp', 'doge', 'bnb'];

/* ═══════════════════════════════ time: GMT+9 ═══════════════════════════════
   Same rule as troll-poly-bot's console: shift the epoch by nine hours and read
   the UTC fields (GMT+9 has no daylight saving), so it is exact and cheap. */
const TZ_LABEL = 'GMT+9';
const TZ_SHIFT_MS = 9 * 3600 * 1000;
const pad2 = (n) => String(n).padStart(2, '0');
function tzParts(ms) {
  const d = new Date(Number(ms) + TZ_SHIFT_MS);
  return {
    y: d.getUTCFullYear(), mo: pad2(d.getUTCMonth() + 1), d: pad2(d.getUTCDate()),
    h: pad2(d.getUTCHours()), mi: pad2(d.getUTCMinutes()), s: pad2(d.getUTCSeconds()),
  };
}
const validTs = (ms) => ms != null && ms !== '' && Number.isFinite(Number(ms));

const fmt = {
  money: (v) => (v == null ? '—' : (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })),
  signed: (v) => (v == null ? '—' : (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })),
  pct: (v, d = 1) => (v == null ? '—' : (v * 100).toFixed(d) + '%'),
  spct: (v, d = 2) => (v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(d) + '%'),
  num: (v, d = 0) => (v == null ? '—' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })),
  px: (v) => (v == null ? '—' : Number(v).toFixed(2)),
  px3: (v) => (v == null ? '—' : Number(v).toFixed(3)),
  cents: (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}¢`),
  ms: (v) => (v == null ? '—' : Math.round(v) + ' ms'),
  // 2026-09-30 21:38:05
  clock: (ms) => { if (!validTs(ms)) return '—'; const p = tzParts(ms); return `${p.y}-${p.mo}-${p.d} ${p.h}:${p.mi}:${p.s}`; },
  // 2026-09-30 21:38
  stamp: (ms) => { if (!validTs(ms)) return '—'; const p = tzParts(ms); return `${p.y}-${p.mo}-${p.d} ${p.h}:${p.mi}`; },
  // 09-30 21:38:05
  short: (ms) => { if (!validTs(ms)) return '—'; const p = tzParts(ms); return `${p.mo}-${p.d} ${p.h}:${p.mi}:${p.s}`; },
  // 21:38
  hm: (ms) => { if (!validTs(ms)) return '—'; const p = tzParts(ms); return `${p.h}:${p.mi}`; },
  // "2026-09-30 00:45–00:50 GMT+9" for a window given its open (epoch s)
  window: (start) => {
    if (!validTs(start)) return '—';
    const a = tzParts(start * 1000), b = tzParts((start + WINDOW_S) * 1000);
    return `${a.y}-${a.mo}-${a.d} ${a.h}:${a.mi}–${b.h}:${b.mi} ${TZ_LABEL}`;
  },
  mmss: (s) => { const t = Math.max(0, Math.round(s)); return `${Math.floor(t / 60)}:${pad2(t % 60)}`; },
  age: (s) => (s == null ? '—' : s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`),
};
const cls = (v) => (v == null ? '' : v > 0 ? 'pos' : v < 0 ? 'neg' : '');
const sideTag = (o) => `<span class="side-tag" data-side="${o === 'Up' ? 'UP' : 'DOWN'}">${o === 'Up' ? 'UP' : 'DOWN'}</span>`;
const legTag = (k) => `<span class="leg-tag" data-leg="${k}">${k}</span>`;

const state = {
  live: null,
  page: 'live',
  markets: { data: null, updated: null, loading: false },
  hist: { slug: null, pts: [] },
  earn: { period: ['day', 'week', 'month'].includes(stored('xshark-earn', 'day')) ? stored('xshark-earn', 'day') : 'day', data: null },
  win: { page: 0, data: null, open: null },
  fills: null,
};

let MARKET_ASSETS = null;

/* ═════════════════════════════════ theme ═════════════════════════════════ */

function initTheme() {
  const q = new URLSearchParams(location.search).get('theme');
  const pick = (q === 'dark' || q === 'light') ? q : stored('xshark-theme', null);
  if (pick === 'dark' || pick === 'light') document.documentElement.dataset.theme = pick;
  themeLabel();
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    store('xshark-theme', next);
    themeLabel();
    renderPage(true);
  });
}
function themeLabel() {
  const dark = (document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
  $('[data-theme-label]').textContent = dark ? 'Light' : 'Dark';
}

function notice(msg, isError = true) {
  const box = $('#notice');
  if (!msg) { box.hidden = true; return; }
  box.textContent = msg;
  box.hidden = false;
  box.dataset.kind = isError ? 'error' : 'info';
}

/* which coin's bot this page shows (paper console: one bot per coin, ?asset=) */
let ASSET = '';
try { ASSET = (new URLSearchParams(location.search).get('asset') || localStorage.getItem('xshark-asset') || '').toLowerCase(); } catch { /* private mode */ }
const COIN = () => (ASSET || (state.live && state.live.asset) || 'btc').toUpperCase();
function withAsset(url) { return ASSET ? url + (url.includes('?') ? '&' : '?') + 'asset=' + ASSET : url; }

/* a coin price with the decimals its size needs: BTC $84,087.54, XRP $2.4512, DOGE $0.24871 */
function coinPx(v, ref = v) {                 // ref: size the decimals by another price (a gap by its strike)
  if (v == null || !Number.isFinite(v)) return '—';
  const a = Math.abs(ref), d = a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : 5;
  return '$' + v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

async function loadCoins() {
  let d;
  try { d = await (await fetch('/api/assets', { cache: 'no-store' })).json(); } catch { return; }
  if (!d || !d.assets || !d.assets.length) { ASSET = ''; MARKET_ASSETS = new Set(); return; }
  MARKET_ASSETS = new Set(d.assets.map((a) => (a.asset || a.coin || '').toLowerCase()).filter(Boolean));
  if (!d.multi) {
    ASSET = '';                                   // one bot folder: its coin as the only tab
    if (!d.assets[0].coin) return;
  } else {
    const names = d.assets.map((a) => a.asset);
    if (!names.includes(ASSET)) ASSET = names[0];
  }
  const box = $('#coin-switch');
  box.innerHTML = '';
  d.assets.forEach((a) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'coin-btn';
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(!d.multi || a.asset === ASSET));
    const pnl = a.pnl == null ? '' : `${a.pnl >= 0 ? '+' : '-'}$${Math.abs(a.pnl).toFixed(2)}`;
    const coin = (a.coin || a.asset).toUpperCase();
    const kind = /LIVE/.test(a.mode || '') ? 'real bot' : 'paper bot';
    b.innerHTML = `<span>${coin}</span><small class="${a.pnl == null ? '' : a.pnl >= 0 ? 'pos' : 'neg'}">${pnl}</small>`;
    b.title = `${coin} ${kind} · ${a.alive ? 'running' : 'not running'}${a.halted ? ' · HALTED: ' + a.halted : ''}`
      + ` · ${a.windows || 0} windows settled`;
    if (!a.alive) b.classList.add('down');
    b.addEventListener('click', () => {
      if (!d.multi || a.asset === ASSET) return;
      try { localStorage.setItem('xshark-asset', a.asset); } catch { /* private mode */ }
      const q = new URLSearchParams(location.search);
      q.set('asset', a.asset);
      q.delete('window');
      location.search = q.toString();
    });
    box.appendChild(b);
  });
  box.hidden = false;
  document.querySelectorAll('.coin-name').forEach((n) => { n.textContent = COIN(); });
  if (state.page === 'markets') pollMarkets();
}

async function api(url) {
  const r = await fetch(withAsset(url), { cache: 'no-store' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/* ══════════════════════════════ svg helpers ═════════════════════════════ */

const NS = 'http://www.w3.org/2000/svg';
const el = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
/* a circle as a path (so it is drawn like the other markers) */
const circlePath = (cx, cy, r) => `M${cx - r},${cy} a${r},${r} 0 1,0 ${2 * r},0 a${r},${r} 0 1,0 ${-2 * r},0 Z`;
/* a taker buy's marker by its rule (user, 2026-10-13: "show balancing and entry buys separately") */
const BUY_KIND = { v6h: 'bal', v6r: 'mom', v6f: 'rec', v6n: 'rec', v5b: 'bal', v5m: 'mom', v4b: 'bal', v3x: 'bal', v2c: 'bal', v2e3: 'bal', bal: 'bal',
  v4m: 'mom', v3m: 'mom', v3cm: 'mom', v2b: 'mom', v2d4: 'mom', v4n: 'rec', v3r: 'rec', v3s: 'rec' };
const BUY_KIND_LABEL = { entry: 'Entry / other buy', bal: 'Balancing buy', mom: 'Momentum buy', rec: 'Both-negative / recovery buy' };
/* cross-market lag fill tags: crosspair:{leader|hold|gap|deep|maj|sweep|xlead|cset|pair}:{…} */
const CROSSPAIR_RULE = {
  leader: { n: 1, short: 'R1', label: 'Rule 1 · Leader', pairs: true },
  hold: { n: 2, short: 'R2', label: 'Rule 2 · Hold', pairs: false },
  gap: { n: 3, short: 'R3', label: 'Rule 3 · Gap', pairs: true },
  deep: { n: 4, short: 'R4', label: 'Rule 4 · Deep', pairs: true },
  maj: { n: 5, short: 'R5', label: 'Rule 5 · Majority', pairs: false },
  sweep: { n: 6, short: 'R6', label: 'Rule 6 · Sweep', pairs: false },
  xlead: { n: 7, short: 'R7', label: 'Rule 7 · Cross pair', pairs: false },
  cset: { n: 8, short: 'CS', label: 'Complete set · Secondary', pairs: false },
  pair: { n: 0, short: 'Pair', label: 'Pair leg', pairs: false },
};
function crosspairRule(tag) {
  const t = String(tag || '');
  if (!t.startsWith('crosspair:')) return null;
  return CROSSPAIR_RULE[t.split(':')[1]] || null;
}
function crosspairPairRise(rule, model) {
  if (!rule || !rule.pairs) return null;
  const m = model || {};
  if (rule.n === 4) return Number(m.deep_rise != null ? m.deep_rise : 0.10);
  return Number(m.rise != null ? m.rise : 0.30);
}
function buyPath(kind, cx, cy) {
  if (kind === 'bal') return `M${cx - 5},${cy - 5} h10 v10 h-10 Z`;                          // ■
  if (kind === 'mom') return `M${cx},${cy - 7} L${cx + 6.5},${cy + 4.5} L${cx - 6.5},${cy + 4.5} Z`;   // ▲
  if (kind === 'rec') {                                                                       // ★
    let d = '';
    for (let i = 0; i < 10; i += 1) {
      const r = i % 2 ? 3 : 7.5, a = -Math.PI / 2 + i * Math.PI / 5;
      d += `${i ? 'L' : 'M'}${(cx + r * Math.cos(a)).toFixed(1)},${(cy + r * Math.sin(a)).toFixed(1)} `;
    }
    return `${d}Z`;
  }
  return circlePath(cx, cy, 5.5);                                                             // ●
}
const crossPath = (cx, cy, r) => `M${cx - r},${cy - r} L${cx + r},${cy + r} M${cx - r},${cy + r} L${cx + r},${cy - r}`;

function mount(host, height) {
  host.innerHTML = '';
  const w = Math.max(host.clientWidth || 600, 260);
  const svg = el('svg', { width: w, height, viewBox: `0 0 ${w} ${height}`, role: 'img' });
  host.appendChild(svg);
  return { svg, w, h: height };
}
/* a column with its far end rounded and its baseline end square */
function colPath(x, y, w, h, r, up) {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  if (up) {
    return `M${x},${y + h} V${y + rr} A${rr},${rr} 0 0 1 ${x + rr},${y} H${x + w - rr}`
      + ` A${rr},${rr} 0 0 1 ${x + w},${y + rr} V${y + h} Z`;
  }
  return `M${x},${y} H${x + w} V${y + h - rr} A${rr},${rr} 0 0 1 ${x + w - rr},${y + h}`
    + ` H${x + rr} A${rr},${rr} 0 0 1 ${x},${y + h - rr} Z`;
}

const tip = $('#tooltip');
function showTip(html, ev) {
  tip.innerHTML = html;
  tip.hidden = false;
  const pad = 14;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
const hideTip = () => { tip.hidden = true; };
const tipRows = (title, rows) =>
  `<div class="tt-title">${title}</div>` + rows.map(([k, v, c]) => `
    <div class="tt-row"><span class="tt-key">${
      c ? `<span class="tt-swatch" style="background:${c}"></span>` : ''}${k}</span>
    <span class="tt-val">${v}</span></div>`).join('');

function legend(host, items) {
  host.innerHTML = '';
  items.forEach((it) => {
    const s = document.createElement('span');
    s.className = 'legend-item';
    if (it.shape === 'diamond-open') {
      s.innerHTML = `<span class="legend-key diamond open" style="border-color:${it.color}"></span>${it.label}`;
    } else if (it.shape === 'tri') {
      s.innerHTML = `<span class="legend-key tri" style="color:${it.color}"></span>${it.label}`;
    } else if (it.shape === 'tri-down') {
      s.innerHTML = `<svg width="11" height="10" viewBox="0 0 11 10" aria-hidden="true"><path d="M5.5,9 L10,1 L1,1 Z" fill="${it.color}" stroke="currentColor" stroke-width="1"/></svg>${it.label}`;
    } else if (it.shape === 'path') {
      s.innerHTML = `<svg width="12" height="12" viewBox="-1 -2 14 15" aria-hidden="true"><path d="${it.d}" fill="${it.color}"/></svg>${it.label}`;
    } else if (it.shape === 'cross') {
      s.innerHTML = `<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M1.5,1.5 L8.5,8.5 M1.5,8.5 L8.5,1.5" stroke="${it.color}" stroke-width="1.8" stroke-linecap="round"/></svg>${it.label}`;
    } else if (it.shape === 'tri-open') {
      s.innerHTML = `<svg width="11" height="10" viewBox="0 0 11 10" aria-hidden="true"><path d="M5.5,1 L10,9 L1,9 Z" fill="none" stroke="${it.color}" stroke-width="1.5"/></svg>${it.label}`;
    } else {
      s.innerHTML = `<span class="legend-key${it.shape ? ' ' + it.shape : ''}" style="background:${it.color}${
        it.dash ? `;background:repeating-linear-gradient(90deg,${it.color} 0 4px,transparent 4px 6px)` : ''}"></span>${it.label}`;
    }
    host.appendChild(s);
  });
}

/* Axis ticks on round values, with a gridline exactly on zero when the range
   crosses it (Bo-BOTT's niceTicks). */
function niceTicks(lo, hi, target = 5) {
  const span = (hi - lo) || 1;
  const mag = Math.pow(10, Math.floor(Math.log10(span / target)));
  const norm = span / target / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = start; v <= end + step * 1e-9; v += step) ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return { ticks, lo: start, hi: end === start ? start + step : end };
}
function axisMoney(v) {
  const a = Math.abs(v), sign = v < 0 ? '-' : '';
  if (a >= 1000) return sign + '$' + (a / 1000).toFixed(a >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k';
  const s = a >= 10 ? a.toFixed(0) : a.toFixed(2).replace(/\.?0+$/, '');
  return sign + '$' + (s || '0');
}

function emptyChart(host, msg, height = 200) {
  const { svg, w } = mount(host, height);
  const t = el('text', { class: 'axis-text', x: w / 2, y: height / 2, 'text-anchor': 'middle' });
  t.textContent = msg;
  svg.appendChild(t);
}

/* ═══════════════ the window price chart (live and archived) ═══════════════
   x = seconds into the window on a fixed 0..300 axis (labelled "m:ss left"),
   so the line grows rightward instead of recompressing on every sample.
   Both tokens are drawn (they mirror: Down = 1 - Up), each with its fair
   value (dashed), our resting bid (dotted steps) and our fills (diamond =
   maker bid filled, triangle = taker clip). */

const COLS = { t: 0, bid: 1, ask: 2, fair: 3, model: 4, spot: 5, qUp: 6, qDown: 7 };

/* A window's chart runs 6 minutes: the minute before the open (its book already trades, and the
   strike is the 60 s average of exactly that minute), then the 5-minute window. x = seconds since the open. */
const PRE_S = 60, SPAN_S = PRE_S + WINDOW_S;
const xSec = (m, pw, s) => m.l + (Math.max(-PRE_S, Math.min(WINDOW_S, s)) + PRE_S) / SPAN_S * pw;
const secAt = (px, pw) => px / (pw || 1) * SPAN_S - PRE_S;
const tipWhen = (s) => (s < 0 ? `opens in ${fmt.mmss(-s)}` : `${fmt.mmss(WINDOW_S - s)} left`);
/* a GMT+9 clock label every minute (10:54 ... 11:00) and a dashed line at the open */
function timeAxis(svg, m, pw, ph, y, start) {
  for (let at = -PRE_S; at <= WINDOW_S; at += 60) {
    const lab = el('text', { class: 'axis-text', x: xSec(m, pw, at), y,
      'text-anchor': at === -PRE_S ? 'start' : at === WINDOW_S ? 'end' : 'middle' });
    lab.textContent = start ? fmt.hm((start + at) * 1000) : fmt.mmss(WINDOW_S - at);
    svg.appendChild(lab);
  }
  const x0 = xSec(m, pw, 0);
  svg.appendChild(el('line', { x1: x0, x2: x0, y1: m.t, y2: m.t + ph, stroke: css('--text-muted'),
    'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0.8 }));
  const t = el('text', { class: 'axis-text', x: x0 + 4, y: m.t + 10 });
  t.textContent = 'open';
  svg.appendChild(t);
}

/* The Up price to draw: the mid when both sides exist. Once a window is all
   but decided one side empties (no Up bid under 0.01, only an ask at 0.01,
   later 0.001), and a mid-only line vanished for the last minutes; then the
   side that is left is the price. Down mirrors it (1 - Up). */
function upMid(p) {
  const b = p[COLS.bid], a = p[COLS.ask];
  if (b != null && a != null) return (a + b) / 2;
  return a != null ? a : b != null ? b : null;
}

function segments(pts, X, Y, valueOf) {
  // one path, broken wherever the value is missing (a gap, not a false line)
  let d = '', on = false;
  for (const p of pts) {
    const v = valueOf(p);
    if (v == null || !Number.isFinite(v)) { on = false; continue; }
    d += `${on ? 'L' : 'M'}${X(p).toFixed(1)},${Y(v).toFixed(1)} `;
    on = true;
  }
  return d;
}

function stepPath(pts, X, Y, valueOf) {
  let d = '', last = null;
  for (const p of pts) {
    const v = valueOf(p);
    if (v == null) { last = null; continue; }
    const x = X(p).toFixed(1), y = Y(v).toFixed(1);
    if (last == null) d += `M${x},${y} `;
    else if (v !== last) d += `H${x} V${y} `;
    else d += `H${x} `;
    last = v;
  }
  return d;
}

function renderPriceChart(host, legendHost, pts, start, fills, height = 320, clips = [], pairings = null, model = null) {
  if (!pts || pts.length < 2) { emptyChart(host, 'Waiting for the window’s first samples…', height); if (legendHost) legendHost.innerHTML = ''; return; }
  const cUp = css('--series-1'), cDown = css('--series-2'), ring = css('--surface-1');
  const crossPair = model && model.strategy === 'cross-market pairs';
  const m = { t: 14, r: 58, b: 34, l: 44 };
  // shares held and the average buy price per side (user, 2026-10-12), from the fills in time order
  const held = (fills || []).slice().sort((a, b) => a.ts - b.ts);
  const HS = held.length ? 78 : 0;                     // the shares-held strip under the chart
  const { svg, w } = mount(host, height + HS);
  const pw = w - m.l - m.r, ph = height - m.t - m.b;
  const el0 = (p) => (p[COLS.t] / 1000 - start);
  const X = (p) => xSec(m, pw, el0(p));
  const Xs = (s) => xSec(m, pw, s);
  const Y = (v) => m.t + ph - Math.max(0, Math.min(1, v)) * ph;

  [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(t), y2: Y(t) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 8, y: Y(t) + 4, 'text-anchor': 'end' });
    lab.textContent = t.toFixed(2);
    svg.appendChild(lab);
  });
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph }));
  timeAxis(svg, m, pw, ph, height - 12, start);

  const inW = pts.filter((p) => el0(p) >= -PRE_S - 1 && el0(p) <= WINDOW_S + 1);
  const series = [
    { key: 'Up', color: cUp, mid: (p) => upMid(p), fair: (p) => p[COLS.fair], bid: (p) => p[COLS.qUp] },
    { key: 'Down', color: cDown, mid: (p) => { const v = upMid(p); return v == null ? null : 1 - v; },
      fair: (p) => (p[COLS.fair] == null ? null : 1 - p[COLS.fair]), bid: (p) => p[COLS.qDown] },
  ];
  series.forEach((s) => {
    svg.appendChild(el('path', { class: 'series-line', d: stepPath(inW, X, Y, s.bid), stroke: s.color,
      'stroke-width': 1.5, 'stroke-dasharray': '1.5 3', opacity: 0.9 }));
    svg.appendChild(el('path', { class: 'series-line', d: segments(inW, X, Y, s.fair), stroke: s.color,
      'stroke-width': 1.5, 'stroke-dasharray': '5 4', opacity: 0.85 }));
    svg.appendChild(el('path', { class: 'series-line', d: segments(inW, X, Y, s.mid), stroke: s.color }));
  });
  // the average buy price of each side: a long-dashed line with its value
  const avgOf = (o) => {
    const b = held.filter((f) => f.outcome === o && !isSold(f));
    const n = b.reduce((a, f) => a + f.size, 0);
    return n > 0 ? { avg: b.reduce((a, f) => a + f.size * f.price, 0) / n, n } : null;
  };
  const avgs = { Up: avgOf('Up'), Down: avgOf('Down') };
  [['Up', cUp], ['Down', cDown]].forEach(([o, color]) => {
    const a = avgs[o];
    if (!a) return;
    svg.appendChild(el('line', { x1: m.l, x2: m.l + pw, y1: Y(a.avg), y2: Y(a.avg), stroke: color, 'stroke-width': 1.4,
      'stroke-dasharray': '10 5', opacity: 0.75 }));
    const rulesOnSide = [...new Set((fills || [])
      .filter((f) => f.outcome === o && !isSold(f) && crosspairRule(f.tag) && crosspairRule(f.tag).n)
      .map((f) => crosspairRule(f.tag).short))];
    const t = el('text', { class: 'axis-text', x: m.l + 6, y: Y(a.avg) - 4, fill: color, 'font-size': 11 });
    t.textContent = `${o} avg buy ${a.avg.toFixed(3)} · ${fmt.num(a.n, 0)} sh`
      + (rulesOnSide.length ? ` · ${rulesOnSide.join('/')}` : '');
    svg.appendChild(t);
  });
  // Cross-pair: pair-trigger level (entry + rise) for each leader/gap/deep fill
  if (crossPair) {
    (fills || []).forEach((f) => {
      if (isSold(f)) return;
      const rule = crosspairRule(f.tag);
      const rise = crosspairPairRise(rule, model);
      if (rise == null || !(f.price >= 0)) return;
      const trigger = Math.min(0.99, f.price + rise);
      const color = f.outcome === 'Up' ? cUp : cDown;
      const s = f.ts / 1000 - start;
      if (s < 0 || s > WINDOW_S + 1) return;
      const y = Y(trigger);
      svg.appendChild(el('line', { x1: Xs(s), x2: m.l + pw, y1: y, y2: y, stroke: color, 'stroke-width': 1.1,
        'stroke-dasharray': '3 4', opacity: 0.55 }));
      const lab = el('text', { class: 'axis-text', x: m.l + pw - 4, y: y - 4, fill: color, 'font-size': 10,
        'text-anchor': 'end' });
      lab.textContent = `${rule.short} pair @ ${trigger.toFixed(2)} (+$${rise.toFixed(2)})`;
      svg.appendChild(lab);
    });
  }

  // end markers and value labels; two labels closer than a line apart are pushed apart
  const ends = series.map((s) => {
    const last = [...inW].reverse().find((p) => s.mid(p) != null);
    return last ? { s, cx: X(last), cy: Y(s.mid(last)), v: s.mid(last) } : null;
  }).filter(Boolean);
  ends.forEach((e) => { e.ly = e.cy + 4; });
  if (ends.length === 2 && Math.abs(ends[0].ly - ends[1].ly) < 13) {
    const [hi, lo] = ends[0].cy <= ends[1].cy ? ends : [ends[1], ends[0]];
    const mid = (hi.ly + lo.ly) / 2;
    hi.ly = mid - 6.5; lo.ly = mid + 6.5;
  }
  ends.forEach((e) => {
    svg.appendChild(el('circle', { cx: e.cx, cy: e.cy, r: 5, fill: ring, stroke: e.s.color, 'stroke-width': 2.2 }));   // the price now: a ring (fills are solid dots)
    const t = el('text', { class: 'value-label-strong', x: e.cx + 9, y: e.ly });
    t.textContent = e.v.toFixed(3);
    svg.appendChild(t);
  });

  // the strip under the chart: shares held of each side over time (a sell takes shares off)
  if (HS) {
    const y0 = height + 6, y1 = height + HS - 8;
    let up = 0, dn = 0;
    const steps = [];
    held.forEach((f) => {
      if (f.outcome === 'Up') up += f.size; else dn += f.size;
      steps.push([f.ts / 1000 - start, up, dn]);
    });
    const top = Math.max(1, ...steps.map((x) => Math.max(x[1], x[2])));
    const Yh = (v) => y1 - Math.max(0, v) / top * (y1 - y0 - 14);
    const tEnd = Math.min(WINDOW_S, Math.max(...inW.map((p) => el0(p)), steps[steps.length - 1][0]));
    svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1, y2: y1 }));
    [[1, cUp], [2, cDown]].forEach(([k, color]) => {
      let d = `M${Xs(steps[0][0]).toFixed(1)},${Yh(0).toFixed(1)} `;
      let prev = 0;
      steps.forEach((x) => {
        d += `H${Xs(x[0]).toFixed(1)} V${Yh(x[k]).toFixed(1)} `;
        prev = x[k];
      });
      d += `H${Xs(tEnd).toFixed(1)}`;
      svg.appendChild(el('path', { d, fill: 'none', stroke: color, 'stroke-width': 1.6 }));
      const last = el('text', { class: 'value-label', x: m.l + pw + 6, y: Yh(prev) + 4, fill: color, 'font-size': 10 });
      last.textContent = fmt.num(prev, 0);
      svg.appendChild(last);
    });
    const lab = el('text', { class: 'axis-text', x: m.l + 6, y: y0 + 8 });
    lab.textContent = `Shares held · Up ${fmt.num(up, 1)} · Down ${fmt.num(dn, 1)} (max ${fmt.num(top, 0)})`;
    svg.appendChild(lab);
  }

  // fills, drawn over the lines; their hover targets go on top of the crosshair's
  const hits = [];
  const inOrder = (fills || []).slice().sort((a, b) => a.ts - b.ts);
  const after = new Map(runningPnl(inOrder).map((r, i) => [inOrder[i], r]));    // the window's P&L after each fill
  const pnlRows = (f) => {
    const r = after.get(f);
    return r ? [
      ['Up / Down held', `${fmt.num(r.up, 2)} / ${fmt.num(r.dn, 2)}`],
      ['If Up wins', `<span class="${cls(r.ifUp)}">${fmt.signed(r.ifUp)}</span>`, cUp],
      ['If Down wins', `<span class="${cls(r.ifDown)}">${fmt.signed(r.ifDown)}</span>`, cDown],
    ] : [];
  };
  const pairRowsForFill = (f) => {
    const tag = String(f.tag || '');
    const rule = crosspairRule(tag);
    const rows = [];
    if (rule) {
      rows.push(['Rule', rule.label]);
      const rise = crosspairPairRise(rule, model);
      if (rise != null) {
        rows.push(['Pair after', `leader ask rises +$${rise.toFixed(2)} → $${(f.price + rise).toFixed(2)}`]);
      } else if (rule.n === 2) {
        rows.push(['Pair', 'none · hold to settlement']);
      } else if (rule.n === 0) {
        rows.push(['Pair', 'opposite-token maker fill']);
      }
    }
    if (pairings == null || !tag.startsWith('crosspair:')) return rows;
    const parts = tag.split(':');
    const kind = parts[1], asset = parts[2], outcome = parts[3] || f.outcome;
    const own = pairings.find((p) => p.asset === String(asset || '').toUpperCase() && p.outcome === outcome);
    const partners = (own && own.partners) || [];
    const planId = kind && outcome ? `${kind}:${outcome}` : kind;
    const linked = partners.filter((p) => p.plan_id === planId || p.plan_id === kind);
    const paired = linked.length ? linked : partners;
    paired.forEach((p) => rows.push(['Paired with', `${String(asset).toUpperCase()} ${outcome} ↔ ${p.asset} ${p.outcome}`]));
    if (rule && rule.pairs && !paired.length) rows.push(['Paired with', 'waiting for +rise · then maker on opposite']);
    return rows;
  };
  // taker orders that got nothing (user, 2026-10-12: "show all orders"): an x at their limit, when sent;
  // drawn first, so the fills sit on top
  const killed = (clips || []).filter((c) => !(c.filled > 0));
  killed.forEach((c) => {
    const sIn = c.ts / 1000 - start;
    if (sIn < 0 || sIn > WINDOW_S + 1) return;
    const color = c.outcome === 'Up' ? cUp : cDown;
    const cx = Xs(sIn), cy = Y(c.limit);
    svg.appendChild(el('path', { d: crossPath(cx, cy, 3.6), fill: 'none', stroke: css('--text-primary'), 'stroke-width': 1.7,
      'stroke-linecap': 'round', opacity: 0.85 }));                               // black x (user, 2026-10-12)
    const h = el('circle', { class: 'hit', cx, cy, r: 8 });
    hits.push(h);
    const name = DEC_TAG[c.tag] || 'Clip';
    h.addEventListener('mousemove', (ev) => showTip(tipRows(`${name} ${c.result === 'SENT' ? 'on its way' : 'not filled'} · buy ${c.outcome}`, [
      ['Why', c.why || String(c.result || '').toLowerCase(), color],
      ['Limit', fmt.px(c.limit)],
      ['Ask when sent', fmt.px(c.seen_ask)],
      ['Our fair then', fmt.px3(c.fair)],
      ['Shares', fmt.num(c.size, 0)],
      ...(c.race_p == null ? [] : [['Race odds', fmt.pct(c.race_p, 0)]]),
      ['Latency', c.latency_ms == null ? '—' : `${c.latency_ms} ms`],
      ['Sent', fmt.clock(c.ts)],
    ]), ev));
    h.addEventListener('mouseleave', hideTip);
  });
  (fills || []).forEach((f) => {
    const s = (f.ts / 1000) - start;
    if (s < 0 || s > WINDOW_S + 1) return;
    const color = f.outcome === 'Up' ? cUp : cDown;
    const cx = Xs(s), cy = Y(f.price);
    const sold = isSold(f);
    const cpRule = crosspairRule(f.tag);
    const d = sold
      ? `M${cx},${cy + 7} L${cx + 6.5},${cy - 5} L${cx - 6.5},${cy - 5} Z`          // a sell: ▼, dark outline
      : f.kind === 'maker' || (cpRule && cpRule.n === 0)
        ? `M${cx},${cy - 6} L${cx + 6},${cy} L${cx},${cy + 6} L${cx - 6},${cy} Z`
        : buyPath(BUY_KIND[f.tag] || 'entry', cx, cy);                              // a taker buy: by its rule
    svg.appendChild(el('path', { d, fill: color, stroke: sold ? css('--text-primary') : ring, 'stroke-width': sold ? 1.8 : 2 }));
    // On-chart rule tag next to the fill (cross-pair). Skip Rule 6 — it clips
    // every second and the stacked "Rule 6 · Sweep" labels bury the chart.
    if (!sold && cpRule && cpRule.n !== 6 && cpRule.n !== 7 && cpRule.n !== 8) {
      const above = cy > m.t + 18;
      const ty = above ? cy - 12 : cy + 16;
      const lab = el('text', { class: 'value-label-strong', x: cx + 8, y: ty, fill: color, 'font-size': 11 });
      lab.textContent = cpRule.n ? cpRule.label : 'Pair fill';
      svg.appendChild(lab);
    }
    const hit = el('circle', { class: 'hit', cx, cy, r: 11 });
    hits.push(hit);
    if (sold) {
      const n = Math.abs(f.size);
      hit.addEventListener('mousemove', (ev) => showTip(tipRows(`Sold · ${f.outcome}`, [
        ...pairRowsForFill(f),
        ['Price', fmt.px3(f.price), color],
        ['Shares sold', fmt.num(n, 2)],
        ['Received', fmt.money(n * f.price - (f.fee || 0))],
        ['Fee', fmt.money(f.fee)],
        ['Time left', fmt.mmss(f.secs_left)],
        ['At', fmt.clock(f.ts)],
        ...pnlRows(f),
      ]), ev));
      hit.addEventListener('mouseleave', hideTip);
      return;
    }
    const what = cpRule ? `${cpRule.label} filled`
      : f.tag === 'pair' ? 'Pair completed' : f.tag === 'dog' ? 'Underdog bid filled'
      : f.tag === 'fav' ? 'Favourite bid filled' : f.tag === 'late' ? 'Last-minute favourite filled'
      : f.tag && (f.tag.startsWith('scalp') || ['rise', 'btc', 'bal', 'top', 'ladder', 'wpair', 'v2a', 'v2b', 'v2c', 'v2d1', 'v2d2', 'v2d4', 'v2e1', 'v2e2', 'v2e3', 'v3e', 'v3m', 'v3r', 'v3s', 'v3b', 'v3x', 'v3la', 'v3lc', 'v3cm', 'v4e', 'v4m', 'v4n', 'v4b', 'v5m', 'v5b', 'v6s', 'v6r', 'v6h', 'v6f', 'v6n'].includes(f.tag)) ? `${DEC_TAG[f.tag]} filled`
      : f.kind === 'maker' ? 'Bid filled' : 'Clip filled';
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(`${what} · ${f.outcome}`, [
      ...pairRowsForFill(f),
      ['Price', fmt.px3(f.price), color],
      ['Shares', fmt.num(f.size, 2)],
      ['How', HOW[f.how] || f.how],
      ['Fair then', fmt.px3(f.fair)],
      ['Edge at fill', f.fair == null ? '—' : fmt.cents(f.fair - f.price)],
      ['Fee', fmt.money(f.fee)],
      ['Time left', fmt.mmss(f.secs_left)],
      ['At', fmt.clock(f.ts)],
      ...pnlRows(f),
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });

  const cross = el('line', { class: 'crosshair', y1: m.t, y2: m.t + ph, opacity: 0 });
  svg.appendChild(cross);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const bx = svg.getBoundingClientRect();
    const at = secAt(ev.clientX - bx.left - m.l, pw);
    let best = null, bd = Infinity;
    for (const p of inW) { const d = Math.abs(el0(p) - at); if (d < bd) { bd = d; best = p; } }
    if (!best) return;
    cross.setAttribute('x1', X(best)); cross.setAttribute('x2', X(best)); cross.setAttribute('opacity', 1);
    const b = best[COLS.bid], a = best[COLS.ask];
    const pairRows = pairings == null ? [] : pairings.flatMap((pair) => {
      const rule = CROSSPAIR_RULE[pair.kind] || null;
      const head = rule ? `${rule.label} · ${pair.asset} ${pair.outcome}` : `${pair.asset} ${pair.outcome}`;
      const partners = pair.partners || [];
      if (!partners.length) {
        return rule && !rule.pairs
          ? [['Active rule', `${head} · hold to close`]]
          : [['Active rule', `${head} · waiting to pair`]];
      }
      return partners.map((partner) => [
        'Paired tokens', `${head} ↔ ${partner.asset} ${partner.outcome}`,
      ]);
    });
    if (pairings != null && pairRows.length === 0) pairRows.push(['Active rule', 'No cross-pair entry in this window']);
    showTip(tipRows(`${tipWhen(el0(best))} · ${fmt.short(best[COLS.t])}`, [
      ['Up bid / ask', `${fmt.px(b)} / ${fmt.px(a)}`, cUp],
      ['Down bid / ask', `${a == null ? '—' : fmt.px(1 - a)} / ${b == null ? '—' : fmt.px(1 - b)}`, cDown],
      ...pairRows,
      ['Fair Up', fmt.px3(best[COLS.fair])],
      ['TWAP model Up', fmt.px3(best[COLS.model])],
      ['Our Up bid', fmt.px(best[COLS.qUp])],
      ['Our Down bid', fmt.px(best[COLS.qDown])],
    ]), ev);
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });
  hits.forEach((h) => svg.appendChild(h));

  if (legendHost) {
    const any = (k) => (fills || []).some((f) => f.kind === k && !isSold(f));
    const items = [
      { label: 'Up mid', color: cUp }, { label: 'Down mid', color: cDown },
      { label: 'Fair value (dashed)', color: css('--text-secondary'), dash: true },
      { label: 'Our bid (dotted)', color: css('--text-secondary'), dash: true },
    ];
    if (avgs.Up || avgs.Down) items.push({ label: 'Avg buy price (long dash) · shares held below', color: css('--text-secondary'), dash: true });
    if (crossPair) {
      const seen = new Set();
      (fills || []).forEach((f) => {
        const r = crosspairRule(f.tag);
        if (!r || isSold(f) || seen.has(r.short)) return;
        seen.add(r.short);
        items.push({ label: r.label + (r.pairs ? ` · pair after +$${Number(crosspairPairRise(r, model)).toFixed(2)}` : ' · no pair'),
          color: css('--text-secondary'), shape: r.n === 0 ? 'diamond' : 'dot' });
      });
      if ([...seen].some((s) => CROSSPAIR_RULE.leader.short === s || CROSSPAIR_RULE.gap.short === s || CROSSPAIR_RULE.deep.short === s)
          || (fills || []).some((f) => { const r = crosspairRule(f.tag); return r && r.pairs; })) {
        items.push({ label: 'Pair trigger (short dash) · entry + rise', color: css('--text-secondary'), dash: true });
      }
    }
    if (any('maker')) items.push({ label: 'Bid filled', color: css('--text-secondary'), shape: 'diamond' });
    const kinds = new Set((fills || []).filter((f) => f.kind === 'taker' && !isSold(f) && !crosspairRule(f.tag))
      .map((f) => BUY_KIND[f.tag] || 'entry'));
    ['entry', 'bal', 'mom', 'rec'].filter((k) => kinds.has(k)).forEach((k) => items.push(
      { label: BUY_KIND_LABEL[k], color: css('--text-secondary'), shape: 'path', d: buyPath(k, 6, 6) }));
    if ((fills || []).some(isSold)) items.push({ label: 'Sold', color: css('--text-secondary'), shape: 'tri-down' });
    if (killed.length) {
      const lost = killed.filter((c) => c.why === 'lost the race').length;
      items.push({ label: `Taker not filled (${killed.length}${lost ? `: ${lost} lost the race` : ''}) · at its limit`,
        color: css('--text-primary'), shape: 'cross' });
    }
    legend(legendHost, items);
  }
}

function initMarketCards() {
  const grid = $('#market-grid');
  if (!grid || grid.children.length) return;
  grid.innerHTML = MARKET_COINS.map((coin) => {
    const name = coin.toUpperCase();
    return `<section class="card market-chart-card" data-market="${coin}">
      <div class="market-chart-head">
        <div>
          <p class="eyebrow">Polymarket · 5 minute</p>
          <h3>${name} Up/Down</h3>
          <p class="card-sub" id="market-window-${coin}">Waiting for market data</p>
        </div>
        <div class="market-card-actions">
          <span class="badge market-status" id="market-status-${coin}" data-state="off"><span class="dot" aria-hidden="true"></span><span class="market-status-text">Offline</span></span>
          <a class="market-open-link" id="market-open-${coin}" target="_blank" rel="noopener noreferrer" hidden>Open ↗</a>
        </div>
      </div>
      <div class="legend market-legend" id="market-legend-${coin}"></div>
      <div class="chart-host market-chart-host" id="market-chart-${coin}"></div>
    </section>`;
  }).join('');
}

async function ensureMarketAssets() {
  if (MARKET_ASSETS !== null) return;
  try {
    const r = await fetch('/api/assets', { cache: 'no-store' });
    const d = await r.json();
    MARKET_ASSETS = new Set((d.assets || []).map((a) => (a.asset || a.coin || '').toLowerCase()).filter(Boolean));
  } catch {
    MARKET_ASSETS = new Set();
  }
}

function renderMarkets() {
  if (state.page !== 'markets') return;
  initMarketCards();
  const data = state.markets.data || {};
  MARKET_COINS.forEach((coin) => {
    const host = $(`#market-chart-${coin}`), legendHost = $(`#market-legend-${coin}`);
    const badge = $(`#market-status-${coin}`), status = badge.querySelector('.market-status-text');
    const sub = $(`#market-window-${coin}`), link = $(`#market-open-${coin}`);
    const d = data[coin];
    const w = d && d.window;
    const configured = MARKET_ASSETS && MARKET_ASSETS.has(coin);
    if (!configured) {
      badge.dataset.state = 'off'; status.textContent = 'Not connected';
      sub.textContent = 'No bot feed for this market';
      link.hidden = true;
      emptyChart(host, 'This asset is not connected to the current console.', 250);
      legendHost.innerHTML = '';
      return;
    }
    if (!d || d.error) {
      badge.dataset.state = 'off'; status.textContent = 'Feed unavailable';
      sub.textContent = 'Could not read bot state';
      link.hidden = true;
      emptyChart(host, 'Waiting for this market’s feed…', 250);
      legendHost.innerHTML = '';
      return;
    }
    if (d.alive) {
      badge.dataset.state = 'live';
      status.textContent = w && Number.isFinite(w.secs_left) ? `Live · ${fmt.mmss(w.secs_left)} left` : 'Live';
    } else {
      badge.dataset.state = 'stale';
      status.textContent = d.ts ? `Stale · ${fmt.age(d.age_s)} old` : 'Offline';
    }
    sub.textContent = w ? fmt.window(w.start) : 'Waiting for the next market window';
    if (w && w.slug) {
      link.href = `https://polymarket.com/event/${encodeURIComponent(w.slug)}`;
      link.hidden = false;
    } else link.hidden = true;
    if (!w) {
      emptyChart(host, 'Waiting for the window’s first samples…', 250);
      legendHost.innerHTML = '';
      return;
    }
    renderPriceChart(host, legendHost, d.hist || [], w.start, w.fills || [], 250, w.clips || [],
      d.model && d.model.strategy === 'cross-market pairs' ? (d.model.pairings || []) : null,
      d.model && d.model.strategy === 'cross-market pairs' ? d.model : null);
  });
  const updated = state.markets.updated;
  $('#markets-updated').textContent = updated ? `Updated ${fmt.hm(updated)} ${TZ_LABEL}` : 'Waiting for feeds';
}

let marketsInFlight = false;
async function pollMarkets() {
  if (marketsInFlight || state.page !== 'markets') return;
  marketsInFlight = true;
  try {
    await ensureMarketAssets();
    const pairs = await Promise.all(MARKET_COINS.map(async (coin) => {
      if (!MARKET_ASSETS.has(coin)) return [coin, null];
      try {
        const r = await fetch(`/api/live?asset=${encodeURIComponent(coin)}`, { cache: 'no-store' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return [coin, await r.json()];
      } catch (e) {
        return [coin, { error: e.message }];
      }
    }));
    state.markets.data = Object.fromEntries(pairs);
    state.markets.updated = Date.now();
    renderMarkets();
  } finally {
    marketsInFlight = false;
  }
}

const HOW = {
  take: 'Taker clip: bought the ask',
  queue: 'Bid: reached through the queue',
  sweep: 'Bid: a trade went through its price',
  cross: 'Bid: the ask fell through it',
  venue: 'Bid: filled on the venue',
  sell: 'Sold: hit the bid',
};

/* The coin against the strike, both on Chainlink's level. One axis (dollars). */
function renderSpotChart(host, legendHost, pts, start, strike, height = 220, decisions = []) {
  const sp = (pts || []).filter((p) => p[COLS.spot] != null && (p[COLS.t] / 1000 - start) >= -PRE_S - 1);
  if (sp.length < 2) { emptyChart(host, 'No spot samples yet', height); if (legendHost) legendHost.innerHTML = ''; return; }
  const cStrike = css('--series-3'), cSpot = css('--text-primary'), ring = css('--surface-1');
  const m = { t: 12, r: 78, b: 30, l: 70 };
  const { svg, w } = mount(host, height);
  const pw = w - m.l - m.r, ph = height - m.t - m.b;
  const vals = sp.map((p) => p[COLS.spot]).concat(strike ? [strike] : []);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const padv = Math.max((hi - lo) * 0.12, Math.abs(hi) * 6e-5);
  const nt = niceTicks(lo - padv, hi + padv, 4);
  lo = nt.lo; hi = nt.hi;
  const el0 = (p) => p[COLS.t] / 1000 - start;
  const X = (p) => xSec(m, pw, el0(p));
  const Y = (v) => m.t + ph - (v - lo) / (hi - lo) * ph;
  nt.ticks.forEach((t) => {
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(t), y2: Y(t) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 8, y: Y(t) + 4, 'text-anchor': 'end' });
    lab.textContent = coinPx(t);
    svg.appendChild(lab);
  });
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph }));
  timeAxis(svg, m, pw, ph, height - 10, start);
  if (strike) {
    svg.appendChild(el('line', { x1: m.l, x2: m.l + pw, y1: Y(strike), y2: Y(strike), stroke: cStrike,
      'stroke-width': 2, 'stroke-dasharray': '6 4' }));
    const t = el('text', { class: 'value-label', x: m.l + pw + 6, y: Y(strike) + 4 });
    t.textContent = `strike ${coinPx(strike)}`;
    svg.appendChild(t);
  }
  svg.appendChild(el('path', { class: 'series-line', d: segments(sp, X, Y, (p) => p[COLS.spot]), stroke: cSpot }));
  const last = sp[sp.length - 1];
  svg.appendChild(el('circle', { cx: X(last), cy: Y(last[COLS.spot]), r: 4.5, fill: cSpot, stroke: ring, 'stroke-width': 2 }));
  const cross = el('line', { class: 'crosshair', y1: m.t, y2: m.t + ph, opacity: 0 });
  svg.appendChild(cross);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const bx = svg.getBoundingClientRect();
    const at = secAt(ev.clientX - bx.left - m.l, pw);
    let best = null, bd = Infinity;
    for (const p of sp) { const d = Math.abs(el0(p) - at); if (d < bd) { bd = d; best = p; } }
    if (!best) return;
    cross.setAttribute('x1', X(best)); cross.setAttribute('x2', X(best)); cross.setAttribute('opacity', 1);
    const v = best[COLS.spot];
    const rows = [[`${COIN()} (Chainlink level)`, coinPx(v), cSpot]];
    if (strike) {
      rows.push(['Strike', coinPx(strike), cStrike],
        ['Distance', `${v - strike >= 0 ? '+' : '-'}${coinPx(Math.abs(v - strike))} (${((Math.log(v / strike)) * 1e4).toFixed(1)} bps)`]);
    }
    showTip(tipRows(`${tipWhen(el0(best))} · ${fmt.short(best[COLS.t])}`, rows), ev);
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });
  const marks = drawDecisions(svg, decisions, sp, start, X, Y, el0);
  if (legendHost) {
    const src = state.live && state.live.model && state.live.model.price_source;
    const items = [{ label: src === 'chainlink' ? `${COIN()}, Chainlink price (the settlement feed)` : `${COIN()} composite on Chainlink’s level`, color: cSpot },
      { label: 'Strike: Chainlink 60 s TWAP at the open', color: cStrike, dash: true }];
    if (marks.clips) items.push({ label: `Taker filled (${marks.clips})${marks.trigMs ? ` · ○→● the ${marks.trigMs} ms move that triggered it` : ''}`, color: css('--text-secondary'), shape: 'circle' });
    if (marks.bids) items.push({ label: `Bid filled (${marks.bids})`, color: css('--text-secondary'), shape: 'diamond' });
    if (marks.missed) items.push({ label: `Taker not filled (${marks.missed})`, color: css('--text-primary'), shape: 'cross' });
    if (marks.missedBids) items.push({ label: `Bid not filled (${marks.missedBids})`, color: css('--text-secondary'), shape: 'diamond-open' });
    if (marks.sells) items.push({ label: `Sell (${marks.sells})`, color: css('--text-secondary'), shape: 'square' });
    legend(legendHost, items);
  }
}

/* Why each order was sent, on the coin's price line: a circle for a taker order (above the line for
   an Up buy, below for Down; an x when it got nothing), a diamond for a resting bid (hollow = not filled).
   Requotes of the same bid within 3 s are drawn once (the latest), so the line stays readable. */
const DEC_TAG = { rise: 'Rising buy', wpair: 'Pair buy', v2a: 'V2 entry (A)', v2b: 'V2 UP add-on (B)', v2c: 'V2 balance (C)', v2d1: 'V2 ladder 0.32 (D1)', v2d2: 'V2 ladder 0.42 (D2)', v2d4: 'V2 ladder add-on (D4)', v2e1: 'V2 low token at 0.40 (E1)', v2e2: 'V2 low token at 0.50 (E2)', v2e3: 'V2 balance after E (E3)', v3e: 'V3 entry', v3m: 'V3 momentum add-on', v3r: 'V3 recovery', v3s: 'V3 recovery step', v3b: 'V3 level B (0.42)', v3x: 'V3 balance', v3la: 'V3 level A (0.32)', v3lc: 'V3 level C (0.45)', v3cm: 'V3 level C momentum', v4e: 'V4 entry', v4m: 'V4 momentum', v4n: 'V4 both-negative', v4b: 'V4 balance', v5m: 'V5 momentum', v5b: 'V5 balance', v6s: 'V6 start', v6r: 'V6 run buy', v6h: 'V6 hedge', v6f: 'V6 favourite', v6n: 'V6 both-negative fix', ladder: 'Ladder bid', btc: 'BTC move buy', bal: 'Balancing buy', top: 'Profit top-up', clip: 'Clip', pair: 'Pair bid', fav: 'Favourite bid', late: 'Last-minute favourite',
  dog: 'Underdog bid', bid: 'Bid', crosspair: 'Cross-market pair', scalp: 'Scalp first buy', scalp_pair: 'Scalp pair bid', scalp_hedge: 'Scalp hedge' };
/* a sell: the console's or the strategy's (tag "sell"), booked as a fill of negative size */
function isSold(f) { return f.tag === 'sell' || f.size < 0; }
function decisionTitle(dec) {
  if (dec.tag === 'sell') return `Sell · ${fmt.num(Math.abs(dec.size), 2)} ${dec.outcome} ≥ ${fmt.px3(dec.price)}`;
  const name = dec.tag && (dec.tag.startsWith('scalp') || ['rise', 'btc', 'bal', 'top', 'ladder', 'wpair', 'crosspair', 'v2a', 'v2b', 'v2c', 'v2d1', 'v2d2', 'v2d4', 'v2e1', 'v2e2', 'v2e3', 'v3e', 'v3m', 'v3r', 'v3s', 'v3b', 'v3x', 'v3la', 'v3lc', 'v3cm', 'v4e', 'v4m', 'v4n', 'v4b', 'v5m', 'v5b', 'v6s', 'v6r', 'v6h', 'v6f', 'v6n'].includes(dec.tag)) ? DEC_TAG[dec.tag]
    : dec.kind === 'taker' ? (dec.tag === 'pair' ? 'Pair clip' : 'Clip') : (DEC_TAG[dec.tag] || 'Bid');
  return `${name} · buy ${fmt.num(dec.size, 0)} ${dec.outcome} ${dec.kind === 'taker' ? '≤ ' : '@ '}${fmt.px(dec.price)}`;
}
function decisionResult(dec) {
  const r = String(dec.result || '');
  if (dec.filled > 0) return r === 'PARTIAL' ? `Partly filled: ${fmt.num(dec.filled, 2)} sh` : `Filled ${fmt.num(dec.filled, 2)} sh`;
  if (r === 'RESTING') return 'Resting on the book';
  if (r === 'SENT') return 'On its way';
  if (r === 'KILLED') return 'Not filled (killed)';
  if (r === 'CANCELLED') return 'Not filled (cancelled)';
  if (r === 'REJECTED') return 'Rejected (would have crossed)';
  if (r === 'EXPIRED') return 'Not filled (window closed)';
  return r || '—';
}
function decisionWhy(dec) {
  const rows = [['Reason', dec.why || '—']];
  if (dec.tag === 'btc' && dec.btc_bps != null) {
    rows.push(['Trigger', `${COIN()} moved ${dec.btc_bps >= 0 ? '+' : ''}${dec.btc_bps.toFixed(2)} bps in ${dec.btc_ms} ms (needs ${dec.btc_need} bps)`]);
  }
  if (dec.kind === 'taker' && dec.tag !== 'pair' && dec.z != null && !['rise', 'btc', 'bal', 'top', 'ladder', 'wpair', 'v2a', 'v2b', 'v2c', 'v2d1', 'v2d2', 'v2d4', 'v2e1', 'v2e2', 'v2e3', 'v3e', 'v3m', 'v3r', 'v3s', 'v3b', 'v3x', 'v3la', 'v3lc', 'v3cm', 'v4e', 'v4m', 'v4n', 'v4b', 'v5m', 'v5b', 'v6s', 'v6r', 'v6h', 'v6f', 'v6n'].includes(dec.tag)) {
    rows.push(['Trigger', `${COIN()} moved ${dec.z >= 0 ? '+' : ''}${dec.z.toFixed(1)}σ in ${dec.trigger_ms} ms (needs ${dec.trigger}σ)`]);
  }
  if (dec.trig_from_ts != null) {
    const dpx = dec.trig_to_px - dec.trig_from_px;
    rows.push(['Move start (○)', `${clockMs(dec.trig_from_ts)} · ${coinPx(dec.trig_from_px)}`]);
    rows.push(['Move end (●)', `${clockMs(dec.trig_to_ts)} · ${coinPx(dec.trig_to_px)}`]);
    rows.push(['Change', `${dpx >= 0 ? '+' : '-'}${coinPx(Math.abs(dpx))} (${(Math.log(dec.trig_to_px / dec.trig_from_px) * 1e4).toFixed(2)} bps) in ${dec.trig_to_ts - dec.trig_from_ts} ms`]);
    if (dec.ts > dec.trig_to_ts) rows.push(['Order sent', `${dec.ts - dec.trig_to_ts} ms after the move's end`]);
  }
  if (dec.dist_bps != null) rows.push(['From the strike', `${dec.dist_bps >= 0 ? '+' : ''}${dec.dist_bps.toFixed(1)} bps (${dec.dist_bps >= 0 ? 'Up' : 'Down'} side)`]);
  if (dec.fair != null) rows.push([`Our fair for ${dec.outcome}`, fmt.px3(dec.fair)]);
  if (dec.edge != null) rows.push(['Edge after fee', fmt.cents(dec.edge)]);
  if (dec.sigma_bps != null) rows.push(['σ (vol)', `${dec.sigma_bps.toFixed(2)} bps/s`]);
  rows.push(['Time left', fmt.mmss(dec.secs_left)]);
  rows.push(['Result', decisionResult(dec)]);
  return rows;
}
/* 01:23:45.678 in GMT+9 */
function clockMs(ms) {
  const p = tzParts(ms);
  return `${p.h}:${p.mi}:${p.s}.${String(Math.floor(ms % 1000)).padStart(3, '0')}`;
}

function drawDecisions(svg, decisions, sp, start, X, Y, el0) {
  const out = { clips: 0, bids: 0, sells: 0, missed: 0, missedBids: 0, trigMs: null };
  if (!decisions || !decisions.length) return out;
  const cUp = css('--series-1'), cDown = css('--series-2'), ring = css('--surface-1');
  // one marker per bid side and tag in any 3 s run of requotes (keep the latest), every clip
  const shown = [];
  decisions.forEach((dec) => {
    const prev = shown[shown.length - 1];
    if (dec.kind === 'maker' && prev && prev.kind === 'maker' && prev.tag === dec.tag && prev.outcome === dec.outcome
        && dec.ts - prev.ts < 3000 && !(prev.filled > 0)) shown[shown.length - 1] = dec;
    else shown.push(dec);
  });
  const ts = sp.map((p) => p[COLS.t]);
  const hits = [];
  const boxes = [];                                   // labels already placed: keep new ones clear of them
  const place = (x, y, w) => {
    for (const dy of [0, 12, -12, 24, -24, 36, -36]) {
      const yy = y + dy;
      if (!boxes.some((b) => x < b.x + b.w && b.x < x + w && Math.abs(yy - b.y) < 11)) {
        boxes.push({ x, y: yy, w });
        return yy;
      }
    }
    return null;                                      // too crowded: the tooltip still has it
  };
  shown.forEach((dec) => {                            // every order, filled or not (user, 2026-10-12)
    const sIn = dec.ts / 1000 - start;
    if (sIn < 0 || sIn > WINDOW_S + 1) return;
    let v = dec.spot;
    if (v == null) {
      let i = 0;
      while (i + 1 < ts.length && ts[i + 1] <= dec.ts) i++;
      v = sp[i][COLS.spot];
    }
    if (v == null) return;
    const cx = X([dec.ts]), cy = Y(v);
    const color = dec.outcome === 'Up' ? cUp : cDown;
    const filled = dec.filled > 0;
    let d;
    const sale = dec.tag === 'sell';
    if (sale) {
      out.sells += 1;                                 // a sell: a square, dark outline
      const oy = dec.outcome === 'Up' ? cy - 9 : cy + 9;
      d = `M${cx - 5},${oy - 5} L${cx + 5},${oy - 5} L${cx + 5},${oy + 5} L${cx - 5},${oy + 5} Z`;
    } else if (dec.kind === 'taker') {
      out[filled ? 'clips' : 'missed'] += 1;          // a taker: a circle (filled) or an x (not), above (Up) / below (Down)
      d = filled ? circlePath(cx, dec.outcome === 'Up' ? cy - 8 : cy + 8, 5) : crossPath(cx, dec.outcome === 'Up' ? cy - 8 : cy + 8, 3.6);
    } else {
      out[filled ? 'bids' : 'missedBids'] += 1;
      const oy = dec.outcome === 'Up' ? cy - 9 : cy + 9;
      d = `M${cx},${oy - 5} L${cx + 5},${oy} L${cx},${oy + 5} L${cx - 5},${oy} Z`;
    }
    if (dec.trig_from_ts != null && dec.trig_from_px != null) {
      // the move that triggered the clip: start (hollow) -> end (solid), over trigger_ms
      const x0 = X([dec.trig_from_ts]), y0 = Y(dec.trig_from_px);
      const x1 = X([dec.trig_to_ts]), y1 = Y(dec.trig_to_px);
      svg.appendChild(el('line', { x1: x0 - 6, x2: x0, y1: y0, y2: y0, stroke: color, 'stroke-width': 1.5 }));
      svg.appendChild(el('line', { x1, x2: x1 + 6, y1, y2: y1, stroke: color, 'stroke-width': 1.5 }));
      svg.appendChild(el('line', { x1: x0, x2: x1, y1: y0, y2: y1, stroke: color, 'stroke-width': 2.5,
        'stroke-linecap': 'round' }));
      svg.appendChild(el('circle', { cx: x0, cy: y0, r: 3.2, fill: ring, stroke: color, 'stroke-width': 1.6 }));
      svg.appendChild(el('circle', { cx: x1, cy: y1, r: 3.2, fill: color, stroke: ring, 'stroke-width': 1 }));
      const text = dec.tag === 'btc' && dec.btc_bps != null ? `${dec.btc_bps >= 0 ? '+' : ''}${dec.btc_bps.toFixed(1)} bps`
        : dec.z != null ? `${dec.z >= 0 ? '+' : ''}${dec.z.toFixed(1)}σ` : '';
      out.trigMs = dec.trig_to_ts - dec.trig_from_ts;
      const ly = place(x1 + 9, (y0 + y1) / 2 + 3, text.length * 6);
      if (ly != null) {
        const lab = el('text', { class: 'axis-text', x: x1 + 9, y: ly, 'font-size': 10, fill: color });
        lab.textContent = text;
        svg.appendChild(lab);
      }
    }
    const x = dec.kind === 'taker' && !filled && !sale;
    svg.appendChild(el('path', { d, fill: x ? 'none' : filled ? color : ring, 'stroke-linecap': 'round',
      stroke: sale || x ? css('--text-primary') : (filled ? ring : color), 'stroke-width': sale ? 1.8 : (filled ? 1.5 : x ? 1.7 : 1.6) }));
    const h = el('circle', { class: 'hit', cx, cy: dec.outcome === 'Up' ? cy - 8 : cy + 8, r: 10 });
    hits.push(h);
    h.addEventListener('mousemove', (ev) => showTip(tipRows(`${decisionTitle(dec)} · ${fmt.clock(dec.ts)}`,
      decisionWhy(dec).map(([k, val], i) => (i === 0 ? [k, val, color] : [k, val]))), ev));
    h.addEventListener('mouseleave', hideTip);
  });
  hits.forEach((h) => svg.appendChild(h));
  return out;
}

/* ═════════════════════════════════ live page ═════════════════════════════ */

let inFlight = false;

async function pollLive() {
  if (inFlight) return;
  inFlight = true;
  try {
    const h = state.hist;
    let q = '';
    if (h.slug && h.pts.length) q = `?slug=${encodeURIComponent(h.slug)}&since=${h.pts[h.pts.length - 1][0]}`;
    const d = await api('/api/live' + q);
    const w = d.window;
    if (w && d.history_partial && h.slug === w.slug) {
      h.pts = h.pts.concat(d.hist || []);
    } else {
      state.hist = { slug: w ? w.slug : null, pts: d.hist || [] };
    }
    d.hist = state.hist.pts;
    state.live = d;
    renderBadge(d);
    if (state.page === 'live') renderLive();
  } catch (e) {
    renderBadge(null);
  } finally {
    inFlight = false;
  }
}

const MODE_INFO = {
  paper: ['PAPER', 'Fills are simulated against the live books. No key, no orders.', 'xshark · paper console',
    'real books, simulated fills', 'Paper equity', 'xshark paper'],
  SHADOW: ['SHADOW', 'Logged in to the real account: every order is signed but NOT sent. Fills are simulated.',
    'xshark · real account (shadow)', 'orders signed, not sent · simulated fills', 'Paper equity (shadow)', 'xshark shadow'],
  'LIVE-ARMED': ['LIVE', 'REAL ORDERS on the real account. Fills are the venue\'s.', 'xshark · LIVE',
    'real orders on the real account · fills from the venue', 'Equity · real account', 'xshark live'],
};
let MODE = 'paper';
let DDIR = 'data/bot';

function renderMode(d) {
  const mode = (d && d.mode) || 'paper';
  const [label, title, brand, sub, equity] = MODE_INFO[mode] || MODE_INFO.paper;
  MODE = mode;
  const crossPair = d && d.model && d.model.strategy === 'cross-market pairs';
  const subText = crossPair
    ? `Polymarket 5m crypto · six markets · shared ${fmt.money(d.balance0)} paper bankroll · cross-market lag held to close`
    : `Polymarket ${COIN()} 5m · bids on both sides + taker clips, held to settlement · ${sub}`;
  if ($('#brand-sub').textContent !== subText) $('#brand-sub').textContent = subText;
  const equityLabel = crossPair ? 'Shared paper equity' : equity;
  if ($('#hero-label').textContent !== equityLabel) $('#hero-label').textContent = equityLabel;
  if (d && d.data_dir && d.data_dir !== DDIR) {
    DDIR = d.data_dir;
    document.querySelectorAll('.ddir').forEach((n) => { n.textContent = DDIR; });
    $('#ctrl-key').title = `Enter the write key (${DDIR}/console_key)`;
  }
  const b = $('#mode-badge');
  const lv = d && d.live;
  let tip = title;
  if (lv) {
    const st = lv.stats || {};
    tip += `\nVenue balance ${lv.venue_balance != null ? '$' + lv.venue_balance.toFixed(2) : '—'}`
      + ` · signed ${st.signed || 0} · posted ${st.posted || 0} · refused ${st.refused || 0}`
      + (lv.sign_p50_ms != null ? ` · sign p50 ${lv.sign_p50_ms} ms` : '')
      + (lv.rtt_p50_ms != null ? ` · post p50 ${lv.rtt_p50_ms} ms` : '')
      + `\nCaps: $${lv.caps.max_order_usdc}/order, $${lv.caps.max_open_usdc} open, $${lv.caps.max_daily_loss_usdc} daily loss`
      + (lv.user_ws ? `\nFill feed (account websocket): ${lv.user_ws}` : '')
      + (lv.halted ? `\nHALTED: ${lv.halted}` : '') + (lv.last_error ? `\nLast error: ${lv.last_error}` : '');
  }
  if (b.textContent !== label) b.textContent = label;
  b.title = tip;
  b.dataset.mode = mode;
  const h = $('#brand-title');
  if (h.textContent !== brand) h.textContent = brand;
}

function renderBadge(d) {
  if (d) $('#ctrl-restart-wrap').hidden = ((d.mode || 'paper') !== 'paper');
  const b = $('#bot-badge');
  if (!d || !d.running) {
    b.dataset.state = 'stopped';
    $('#bot-state').textContent = d && d.ts ? `stopped · last seen ${fmt.age((Date.now() - d.ts) / 1000)} ago` : 'stopped';
  } else if (!d.alive) {
    b.dataset.state = 'external';
    $('#bot-state').textContent = `not responding · ${fmt.age(d.age_s)}`;
  } else {
    b.dataset.state = 'running';
    $('#bot-state').textContent = `${d.halted ? 'HALTED' : 'running'} · up ${fmt.age(d.uptime_s)}`;
  }
  renderMode(d);
  const n = d && d.stats ? d.stats.windows : null;
  $('#tab-win-count').textContent = n ? String(n) : '';
}

const sigs = {};
function changed(key, sig) {
  if (sigs[key] === sig) return false;
  sigs[key] = sig;
  return true;
}

function renderLive(force = false) {
  const d = state.live;
  const usable = d && d.alive;
  $('#idle').hidden = !!usable;
  $('#live-body').hidden = !usable;
  if (!usable) {
    $('#idle-hint').textContent = d && d.ts
      ? `Last state written ${fmt.clock(d.ts)} ${TZ_LABEL}. Fills are simulated; this bot holds no key and sends no order.`
      : 'Fills are simulated. This bot holds no key and sends no order.';
    return;
  }
  renderHero(d);
  const w = d.window;
  renderWindowHead(d);
  renderNext(d);
  renderPriceChart($('#price-chart'), $('#price-legend'), d.hist, w ? w.start : 0, w ? w.fills : [], 320, w ? w.clips : [],
    d.model && d.model.strategy === 'cross-market pairs' ? (d.model.pairings || []) : null,
    d.model && d.model.strategy === 'cross-market pairs' ? d.model : null);
  renderSpotChart($('#spot-chart'), $('#spot-legend'), d.hist, w ? w.start : 0, w ? w.strike : null, 220,
    w ? w.decisions : []);
  if (force || changed('pos', JSON.stringify(w && [w.position, w.books, w.secs_left > 0, d.sells]))) renderPosition(w, d);
  if (force || changed('legs', JSON.stringify([d.model, w && w.why, w && w.orders, d.halted, d.rule_pnl]))) renderLegs(d);
  renderTrigger(d);
  if (force || changed('fills', JSON.stringify(w && [w.slug, (w.fills || []).length]))) renderFills(w);
  if (force || changed('pending', JSON.stringify(d.pending))) renderPending(d.pending || []);
  if (force || changed('feeds', JSON.stringify(d.feeds))) renderFeeds(d.feeds || {});
}

function renderHero(d) {
  const st = d.stats || {};
  const paired = d.model && d.model.strategy === 'cross-market pairs';
  const pnl = d.equity - d.balance0;
  const h = $('#hero-equity');
  h.textContent = fmt.money(d.equity);
  h.className = 'hero-figure ' + (pnl >= 0 ? 'pos' : 'neg');
  const settled = d.shared_bankroll
    ? `settled across ${d.shared_markets || 7} markets ${fmt.signed(d.shared_pnl)}`
    : `settled ${fmt.signed(st.pnl)}`;
  $('#hero-sub').textContent = `${fmt.signed(pnl)} from ${fmt.money(d.balance0)} · ${settled}`
    + ` · est. maker rebates ${fmt.money(st.rebate_est)} (not in PnL) · cash ${fmt.money(d.cash)}`
    + (d.reserved ? ` · ${fmt.money(d.reserved)} in open orders` : '')
    + (d.outside ? ` · ${fmt.signed(d.outside)} moved outside the bot (deposits, trades by hand)` : '')
    + (d.halted ? ` · HALTED: ${d.halted}` : '');
  $('#hero-sub').classList.toggle('neg', !!d.halted);
  const paper = ((d && d.mode) || 'paper') === 'paper';
  $('#hero-restart').hidden = !(paper && d.halted);
  const traded = st.traded || 0;
  const rp = d.rule_pnl || {};
  const kpis = paired ? [
    { l: 'Today (GMT+9)', v: fmt.signed(st.today), n: `${st.today_windows || 0} windows settled`, c: cls(st.today) },
    { l: 'Windows traded', v: fmt.num(traded), n: `${st.wins || 0} won · ${st.losses || 0} lost` },
    { l: 'Rule 1 · Leader', v: fmt.signed(rp.rule1 || 0), n: 'settled · incl. pair leg', c: cls(rp.rule1 || 0) },
    { l: 'Rule 2 · Hold', v: fmt.signed(rp.rule2 || 0), n: 'settled · hold to close', c: cls(rp.rule2 || 0) },
    { l: 'Rule 3 · Gap', v: fmt.signed(rp.rule3 || 0), n: 'settled · incl. pair leg', c: cls(rp.rule3 || 0) },
    { l: 'Rule 4 · Deep', v: fmt.signed(rp.rule4 || 0), n: 'settled · incl. pair leg', c: cls(rp.rule4 || 0) },
    { l: 'Rule 5 · Majority', v: fmt.signed(rp.rule5 || 0), n: 'settled · hold to close', c: cls(rp.rule5 || 0) },
    { l: 'Rule 6 · Sweep', v: fmt.signed(rp.rule6 || 0), n: 'settled · repeat while peers high', c: cls(rp.rule6 || 0) },
    { l: 'Rule 7 · Cross pair', v: fmt.signed(rp.rule7 || 0), n: 'settled · ≤10 tok / cycle', c: cls(rp.rule7 || 0) },
    { l: 'Complete set', v: fmt.signed(rp.cset || 0), n: 'settled · same-market lock', c: cls(rp.cset || 0) },
  ] : [
    { l: 'Today (GMT+9)', v: fmt.signed(st.today), n: `${st.today_windows || 0} windows settled`, c: cls(st.today) },
    { l: 'Windows traded', v: fmt.num(traded), n: `${st.wins || 0} won · ${st.losses || 0} lost` },
    { l: 'Maker bids PnL', v: fmt.signed(st.maker_pnl), n: 'resting bids, no fee', c: cls(st.maker_pnl) },
    { l: 'Taker clips PnL', v: fmt.signed(st.taker_pnl), n: `after ${fmt.money(st.fees)} fees`, c: cls(st.taker_pnl) },
    { l: 'Return on cost', v: st.cost ? fmt.spct(st.pnl / st.cost) : '—', n: `on ${fmt.money(st.cost)} bought` },
    { l: 'Sigma',
      v: d.model && d.model.sigma_bps != null ? `${d.model.sigma_bps.toFixed(2)} bps/s` : '—',
      n: d.feeds && d.feeds.vol_ready ? `per-second ${COIN()} vol` : 'warming up' },
  ];
  $('#kpi-row').innerHTML = kpis.map((k) => `
    <div class="kpi"><span class="kpi-label">${k.l}</span>
    <span class="kpi-value ${k.c || ''}">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');
}

function renderWindowHead(d) {
  const w = d.window;
  if (!w) {
    $('#window-title').textContent = 'No open window';
    $('#window-clock').textContent = '—';
    $('#chart-sub').textContent = 'Waiting for the next window’s market to be listed.';
    return;
  }
  $('#window-title').textContent = fmt.window(w.start);
  $('#window-clock').textContent = `${fmt.mmss(w.secs_left)} left`;
  const m = d.model || {};
  const bits = [w.question];
  if (m.strategy === 'cross-market pairs') {
    bits.push(`cross-market lag · leader $${Number(m.leader_min || 0.3).toFixed(2)}-${Number(m.leader_max || 0.6).toFixed(2)} when peers > $${Number(m.peer_avg || 0.88).toFixed(2)} (first ${(m.buy_until_s || 240) / 60} min) · gap $${Number(m.gap_min || 0.1).toFixed(2)}-${Number(m.gap_max || 0.35).toFixed(2)} when ≥$${Number(m.gap || 0.35).toFixed(2)} under peers (first ${(m.gap_until_s || 240) / 60} min) · deep $${Number(m.deep_min || 0.01).toFixed(2)}-${Number(m.deep_max || 0.15).toFixed(2)} when ≥$${Number(m.deep || 0.35).toFixed(2)} under peers, pair +$${Number(m.deep_rise || 0.1).toFixed(2)} (first ${(m.buy_until_s || 240) / 60} min) · opposite maker after +$${Number(m.rise || 0.3).toFixed(2)} (or +$${Number(m.pair_fallback_rise || 0.1).toFixed(2)} after ${Number(m.pair_wait_s || 40)}s) · hold $${Number(m.hold_min || 0.5).toFixed(2)}-${Number(m.hold_max || 0.6).toFixed(2)} when peers > $${Number(m.hold_peer_avg || 0.9).toFixed(2)} (from ${Math.floor((m.hold_from_s || 90) / 60)}:${String((m.hold_from_s || 90) % 60).padStart(2, '0')} to close) · maj ask < $${Number(m.maj_max || 0.7).toFixed(2)} when ≥${Number(m.maj_min_high || 5)} peers > $${Number(m.maj_high || 0.85).toFixed(2)} (from ${Math.floor((m.maj_from_s || 0) / 60)}:${String((m.maj_from_s || 0) % 60).padStart(2, '0')} to ${Math.floor((m.maj_until_s || 300) / 60)}:${String((m.maj_until_s || 300) % 60).padStart(2, '0')}) · sweep $${Number(m.sweep_min || 0.7).toFixed(2)}-${Number(m.sweep_max || 0.9).toFixed(2)} when ≥${Number(m.sweep_min_high || 5)} peers > $${Number(m.sweep_peer || 0.9).toFixed(2)} every ${Number(m.sweep_every_ms || 5000) / 1000}s (from ${Math.floor((m.sweep_from_s || 140) / 60)}:${String((m.sweep_from_s || 140) % 60).padStart(2, '0')} to ${Math.floor((m.sweep_until_s || 300) / 60)}:${String((m.sweep_until_s || 300) % 60).padStart(2, '0')}) · xlead ≤${Number(m.xlead_max_tokens || 10)} tok every ${Number(m.xlead_every_ms || 7000) / 1000}s sum≤$${Number(m.xlead_max_sum || 1).toFixed(2)} · cset secondary ≥$${Number(m.cset_min_profit || 0.01).toFixed(3)}/sh`);
    const used = [...new Set((w.fills || []).map((f) => crosspairRule(f.tag)).filter((r) => r && r.n).map((r) => r.label))];
    if (used.length) bits.push(`this window filled: ${used.join(', ')}`);
    else bits.push('this window: no rule fill yet');
  } else {
    bits.push(w.strike ? `strike ${coinPx(w.strike)} (${w.strike_src === 'twap60' ? 'Chainlink TWAP-60' : w.strike_src})`
      : 'strike not printed yet');
    bits.push(m.fair_up != null ? `fair Up ${m.fair_up.toFixed(3)}` : `no fair value (${m.fair_src || '—'})`);
    if (m.p_model != null) bits.push(`TWAP model ${m.p_model.toFixed(3)}`);
    if (m.trigger_z != null) bits.push(`spot move ${m.trigger_z >= 0 ? '+' : ''}${m.trigger_z.toFixed(1)}σ`);
    if (m.price_source) bits.push(`price: ${m.price_source === 'chainlink' ? 'Chainlink' : 'exchange composite'}${m.spot_age_ms != null ? ` (${m.spot_age_ms} ms old)` : ''}`);
  }
  $('#chart-sub').textContent = bits.join(' · ');
}

/* the next window: listed ahead, its books already trade; the strike prints at its open */
function renderNext(d) {
  const n = d.next, w = d.window;
  $('#next-card').hidden = !n;
  if (!n) return;
  $('#next-title').textContent = fmt.window(n.start);
  $('#next-clock').textContent = `opens in ${fmt.mmss(n.secs_to_open)}`;
  const b = n.books || {};
  const mid = (x) => (x && x.bid != null && x.ask != null ? (x.bid + x.ask) / 2 : null);
  if (changed('next', JSON.stringify([n.slug, b]))) {
    const row = (o) => {
      const x = b[o] || {};
      return `<tr><td>${sideTag(o)}</td><td>${fmt.px(x.bid)} / ${fmt.px(x.ask)}</td><td>${fmt.px3(mid(x))}</td>
        <td>${fmt.num(x.bid_size, 0)} / ${fmt.num(x.ask_size, 0)}</td></tr>`;
    };
    $('#next-table').innerHTML = '<thead><tr><th scope="col">Side</th><th scope="col">Book bid / ask</th>'
      + '<th scope="col">Mid</th><th scope="col">Size bid / ask (sh.)</th></tr></thead>'
      + `<tbody>${row('Up')}${row('Down')}</tbody>`;
  }
  const up = b.Up || {}, dn = b.Down || {};
  const asks = up.ask != null && dn.ask != null ? up.ask + dn.ask : null;
  const gap = n.strike_now != null && w && w.strike != null ? n.strike_now - w.strike : null;
  const k = [
    { l: 'Up + Down asks', v: asks == null ? '—' : asks.toFixed(2), n: 'what a pair costs now' },
    { l: 'Strike if it opened now', v: coinPx(n.strike_now), n: 'Chainlink TWAP-60 now; set at the open' },
    { l: 'Vs this window\u2019s strike', v: gap == null ? '—' : (gap >= 0 ? '+' : '-') + coinPx(Math.abs(gap), w.strike),
      n: w && w.strike != null ? `this strike ${coinPx(w.strike)}` : 'this strike not printed' },
  ];
  $('#next-kpis').innerHTML = k.map((x) => `
    <div class="kpi"><span class="kpi-label">${x.l}</span>
    <span class="kpi-value">${x.v}</span><span class="kpi-note">${x.n}</span></div>`).join('');
  $('#next-sub').textContent = `${n.question} · the bot starts on it at the open; nothing is ordered before.`;
}

function renderPosition(w, d) {
  if (!w) { $('#pos-table').innerHTML = ''; $('#pos-kpis').innerHTML = ''; return; }
  const p = w.position, b = w.books || {};
  const heldToClose = d.model && d.model.strategy === 'cross-market pairs';
  const open = w.secs_left == null || w.secs_left > 0;
  const row = (o) => {
    const x = p[o] || {}, bk = b[o] || {};
    const can = !heldToClose && open && (x.shares || 0) >= 0.01 && bk.bid != null;
    const btn = can ? `<button type="button" class="btn btn-sm btn-danger sell-btn" data-sell="${o}"
      data-shares="${x.shares}" data-bid="${bk.bid}" title="Sell all ${o} shares now at the best bid">Sell</button>` : '';
    return `<tr><td>${sideTag(o)}</td><td>${fmt.num(x.shares, 1)}</td><td>${fmt.px3(x.avg)}</td>
      <td>${fmt.money(x.cost)}</td><td>${fmt.num(x.maker_shares, 1)} / ${fmt.num(x.taker_shares, 1)}</td>
      <td>${fmt.px(bk.bid)} / ${fmt.px(bk.ask)}</td><td>${btn}</td></tr>`;
  };
  $('#pos-table').innerHTML = '<thead><tr><th scope="col">Side</th><th scope="col">Shares</th><th scope="col">Avg</th>'
    + '<th scope="col">Cost</th><th scope="col">Maker / taker sh.</th><th scope="col">Book bid / ask</th>'
    + '<th scope="col"><span class="sr-only">Sell</span></th></tr></thead>'
    + `<tbody>${row('Up')}${row('Down')}</tbody>`;
  $('#pos-table').dataset.slug = w.slug || '';
  const k = [
    { l: 'Up + Down avg', v: p.pair_cost == null ? '—' : p.pair_cost.toFixed(3), n: 'per pair; a pair pays $1' },
    { l: 'Hedged', v: p.hedge == null ? '—' : fmt.pct(p.hedge, 0), n: 'smaller side / larger side' },
    { l: 'If Up / if Down', v: `${fmt.signed(p.if_up)} / ${fmt.signed(p.if_down)}`, n: `fees ${fmt.money(p.fees)}` },
  ];
  $('#pos-kpis').innerHTML = k.map((x) => `
    <div class="kpi"><span class="kpi-label">${x.l}</span>
    <span class="kpi-value">${x.v}</span><span class="kpi-note">${x.n}</span></div>`).join('');
  const sells = (d && d.sells) || [];
  const last = sells.length ? sells[sells.length - 1] : null;
  const lastTxt = last ? ` Last sell from the console (${fmt.clock(last.ts)}): ${last.outcome} `
    + (last.order ? `${fmt.num(last.shares, 2)} sh at <= ${fmt.px(last.limit)} -> ${last.result || last.status}`
      + (last.filled ? `, ${fmt.num(last.filled, 2)} sold` : '') : last.status) + '.' : '';
  $('#pos-sub').textContent = heldToClose
    ? (p.fills ? `${p.fills} fills so far this window. The cross-market strategy holds positions to close.`
      : 'Nothing bought yet this window. Cross-market positions are held to close.')
    : (p.fills ? `${p.fills} fills so far this window. Held to settlement unless you press Sell.`
      : 'Nothing bought yet this window.') + lastTxt;
}

/* Sell: everything held on that side of the current window, at once, at the best bid */
async function sellSide(btn) {
  const o = btn.dataset.sell, sh = Number(btn.dataset.shares), bid = Number(btn.dataset.bid);
  const real = MODE === 'LIVE-ARMED';
  const est = Math.max(0.01, bid - 0.02);
  const msg = `${real ? 'REAL MONEY. ' : ''}Sell all ${sh.toFixed(2)} ${o} shares now?\n\n`
    + `Best bid ${bid.toFixed(2)}; the order sells down to ${est.toFixed(2)} (2 cents under, so it fills at once).\n`
    + `About $${(sh * bid).toFixed(2)} back, less the taker fee.\n\n`
    + 'After a sell the bot places no more orders in this window.';
  if (!confirm(msg)) return;
  btn.disabled = true;
  try {
    const r = await postKeyed('/api/sell', { outcome: o, slug: $('#pos-table').dataset.slug || '' });
    if (r) notice(`Sell ${o} sent to the bot; it acts within a second. The result shows under the position.`);
  } catch (e) {
    notice(`Sell not sent: ${e.message}`, true);
  }
  btn.disabled = false;
}

/* the spot-move trigger, editable here as well as on Bot variables */
function renderTrigger(d) {
  const paired = d.model && d.model.strategy === 'cross-market pairs';
  $('#trigger-ctl').hidden = !!paired;
  if (paired) return;
  const v = (d.controls && d.controls.values) || {};
  const z = d.model && d.model.trigger_z;
  const busy = $('#trigger-ctl').contains(document.activeElement);
  if (!busy) {
    if (v.trigger_sigma != null) $('#trig-sigma').value = v.trigger_sigma;
    if (v.trigger_ms != null) $('#trig-ms').value = v.trigger_ms;
  }
  $('#trig-now').textContent = z == null ? 'move now: —'
    : `move now: ${z >= 0 ? '+' : ''}${z.toFixed(1)}σ ${Math.abs(z) >= (v.trigger_sigma || 0) ? '· over the threshold' : ''}`;
}

async function applyTrigger() {
  const sig = Number($('#trig-sigma').value), ms = Number($('#trig-ms').value);
  if (!Number.isFinite(sig) || !Number.isFinite(ms)) { $('#trig-status').textContent = 'Enter numbers.'; return; }
  try {
    const d = await postKeyed('/api/controls', { values: { trigger_sigma: sig, trigger_ms: ms } });
    if (!d) { $('#trig-status').textContent = 'Not saved: no write key.'; return; }
    const notes = d.notes_saved || [];
    $('#trig-status').textContent = `Saved: ${sig}σ within ${ms} ms — the bot applies it within ~2 s.`
      + (notes.length ? ` Adjusted: ${notes.join('; ')}` : '');
    $('#trig-status').dataset.kind = notes.length ? 'warn' : 'ok';
  } catch (e) {
    $('#trig-status').textContent = `Failed: ${e.message}`;
    $('#trig-status').dataset.kind = 'error';
  }
}

function renderLegs(d) {
  const w = d.window || {}, why = w.why || {}, m = d.model || {}, t = m.targets || {};
  const c = w.counts || {};
  if (m.strategy === 'cross-market pairs') {
    const sent = c.taker_sent || 0;
    const filled = (c.taker_fills || 0) + (c.taker_partial || 0);
    const onOff = (flag) => (flag === false ? 'off' : 'on');
    const rp = d.rule_pnl || {};
    const rows = [
      ['Rule 1 · Leader', `ask $${Number(m.leader_min || 0.3).toFixed(2)}-${Number(m.leader_max || 0.6).toFixed(2)} while other markets' same-side avg > $${Number(m.peer_avg || 0.88).toFixed(2)}`, `${onOff(m.rule1)} · first ${(m.buy_until_s || 240) / 60} min · repeat every ${Number(m.leader_every_ms || 5000) / 1000}s while eligible`, rp.rule1],
      ['Rule 1 · Pair', `rest a maker bid on the opposite token after the leader ask rises by $${Number(m.rise || 0.3).toFixed(2)} from entry; if still unpaired after ${Number(m.pair_wait_s || 40)}s, pair on +$${Number(m.pair_fallback_rise || 0.1).toFixed(2)}`, `${onOff(m.rule1)} · pair size follows filled entry clips · maker fill`, null],
      ['Rule 2 · Hold', `ask $${Number(m.hold_min || 0.5).toFixed(2)}-${Number(m.hold_max || 0.6).toFixed(2)} while peers > $${Number(m.hold_peer_avg || 0.9).toFixed(2)}`, `${onOff(m.rule2)} · from ${Math.floor((m.hold_from_s || 90) / 60)}:${String((m.hold_from_s || 90) % 60).padStart(2, '0')} to close · repeat every ${Number(m.hold_every_ms || 5000) / 1000}s while eligible`, rp.rule2],
      ['Rule 3 · Gap', `ask $${Number(m.gap_min || 0.1).toFixed(2)}-${Number(m.gap_max || 0.35).toFixed(2)} and ≥$${Number(m.gap || 0.35).toFixed(2)} under peer avg`, `${onOff(m.rule3)} · first ${(m.gap_until_s || 240) / 60} min · repeat every ${Number(m.gap_every_ms || 5000) / 1000}s while eligible · maker pair after +$${Number(m.rise || 0.3).toFixed(2)} (or +$${Number(m.pair_fallback_rise || 0.1).toFixed(2)} after ${Number(m.pair_wait_s || 40)}s)`, rp.rule3],
      ['Rule 4 · Deep', `ask $${Number(m.deep_min || 0.01).toFixed(2)}-${Number(m.deep_max || 0.15).toFixed(2)} and ≥$${Number(m.deep || 0.35).toFixed(2)} under peer avg`, `${onOff(m.rule4)} · first ${(m.buy_until_s || 240) / 60} min · repeat every ${Number(m.deep_every_ms || 5000) / 1000}s while eligible · maker pair after +$${Number(m.deep_rise || 0.1).toFixed(2)}`, rp.rule4],
      ['Rule 5 · Majority', `ask under $${Number(m.maj_max || 0.7).toFixed(2)} while ≥${Number(m.maj_min_high || 5)} other markets' same-side ask > $${Number(m.maj_high || 0.85).toFixed(2)}`, `${onOff(m.rule5)} · from ${Math.floor((m.maj_from_s || 0) / 60)}:${String((m.maj_from_s || 0) % 60).padStart(2, '0')} to ${Math.floor((m.maj_until_s || 300) / 60)}:${String((m.maj_until_s || 300) % 60).padStart(2, '0')} · repeat every ${Number(m.maj_every_ms || 5000) / 1000}s while eligible · hold to close · no pair`, rp.rule5],
      ['Rule 6 · Sweep', `ask $${Number(m.sweep_min || 0.7).toFixed(2)}-${Number(m.sweep_max || 0.9).toFixed(2)} while ≥${Number(m.sweep_min_high || 5)} other markets' same-side ask > $${Number(m.sweep_peer || 0.9).toFixed(2)}`, `${onOff(m.rule6)} · from ${Math.floor((m.sweep_from_s || 140) / 60)}:${String((m.sweep_from_s || 140) % 60).padStart(2, '0')} to ${Math.floor((m.sweep_until_s || 300) / 60)}:${String((m.sweep_until_s || 300) % 60).padStart(2, '0')} · 5 shares every ${Number(m.sweep_every_ms || 5000) / 1000}s while condition holds · no pair`, rp.rule6],
      ['Rule 7 · Cross pair', `15-pair distributed (BTC×ETH, SOL×XRP, DOGE×BNB, …) · one every ${Number(m.xlead_every_ms || 7000) / 1000}s at ${Number(m.xlead_max_tokens || 10)} tok/pair · next cycle after 15 · sum ≤ $${Number(m.xlead_max_sum || 1).toFixed(2)}`, `${onOff(m.rule7)} · from ${Math.floor((m.xlead_from_s || 0) / 60)}:${String((m.xlead_from_s || 0) % 60).padStart(2, '0')} to ${Math.floor((m.xlead_until_s || 290) / 60)}:${String((m.xlead_until_s || 290) % 60).padStart(2, '0')} · PRIMARY · paper`, rp.rule7],
      ['Complete set · Secondary', `buy Up+Down in the same market when all-in (asks + fees + $${Number(m.cset_reserve || 0.005).toFixed(3)}/leg) locks ≥$${Number(m.cset_min_profit || 0.01).toFixed(3)}/sh and ROI ≥${(Number(m.cset_min_roi || 0.01) * 100).toFixed(1)}%`, `${onOff(m.cset)} · from ${Math.floor((m.cset_from_s || 0) / 60)}:${String((m.cset_from_s || 0) % 60).padStart(2, '0')} to ${Math.floor((m.cset_until_s || 290) / 60)}:${String((m.cset_until_s || 290) % 60).padStart(2, '0')} · after Rule 7 and rules 1–6 · same-market only`, rp.cset],
      ['No chase', `rules 1, 3 & 4 stop retrying once the ask has risen $${Number(m.chase || 0.05).toFixed(2)} from the first signal`, 'do not buy again', null],
      ['This window', `${sent} orders sent · ${filled} filled or partial`, 'hold to settlement', null],
    ];
    $('#legs-table').innerHTML = '<thead><tr><th scope="col">Rule</th><th scope="col">Current strategy</th><th scope="col">Target</th><th scope="col">Settled P/L</th></tr></thead><tbody>'
      + rows.map(([k, v, tg, pnl]) => `<tr><td>${k}</td><td class="wrap">${esc(v)}</td><td>${esc(tg)}</td>`
        + `<td class="${pnl == null ? 'dim' : cls(pnl)}">${pnl == null ? '—' : fmt.signed(pnl)}</td></tr>`).join('')
      + '</tbody>';
    const orders = w.orders || [];
    $('#orders-table').innerHTML = orders.length
      ? '<thead><tr><th scope="col">Pair order</th><th scope="col">Limit</th><th scope="col">Filled</th><th scope="col">State</th></tr></thead><tbody>'
        + orders.map((o) => `<tr><td>${sideTag(o.outcome)}</td><td>${fmt.px(o.price)}</td><td>${fmt.num(o.filled, 1)} / ${fmt.num(o.size, 0)}</td>`
          + `<td class="dim">${o.state === 'sent' ? 'in flight' : 'working'}</td></tr>`).join('') + '</tbody>'
      : '<tbody><tr><td class="live-empty">No pair orders in flight.</td></tr></tbody>';
    return;
  }
  const lost = c.taker_lost_race || 0;
  const rows = [
    ['Maker bid · Up', why.maker_Up, t.Up],
    ['Maker bid · Down', why.maker_Down, t.Down],
    ['Taker clips', why.taker, null],
    ['Clips this window', c.taker_sent ? `${c.taker_sent} sent · ${(c.taker_filled || 0) + (c.taker_partial || 0)} filled · `
      + `${lost} lost the race · ${Math.max(0, (c.taker_killed || 0) - lost)} nothing at the limit` : 'none sent', null],
  ];
  $('#legs-table').innerHTML = '<thead><tr><th scope="col">Leg</th><th scope="col">Now</th><th scope="col">Target</th></tr></thead><tbody>'
    + rows.map(([k, v, tg]) => `<tr><td>${k}</td><td class="wrap">${esc(v || '—')}</td><td>${tg == null ? '—' : fmt.px(tg)}</td></tr>`).join('')
    + '</tbody>';
  const orders = w.orders || [];
  $('#orders-table').innerHTML = orders.length
    ? '<thead><tr><th scope="col">Open order</th><th scope="col">Price</th><th scope="col">Filled</th><th scope="col">Queue ahead</th><th scope="col">State</th></tr></thead><tbody>'
      + orders.map((o) => `<tr><td>${legTag(o.kind)} ${sideTag(o.outcome)}</td><td>${fmt.px(o.price)}</td>
          <td>${fmt.num(o.filled, 1)} / ${fmt.num(o.size, 0)}</td><td>${o.kind === 'maker' ? fmt.num(o.queue_ahead, 0) : '—'}</td>
          <td class="dim">${o.cancelling ? 'cancelling' : o.state === 'sent' ? 'in flight' : 'resting'}</td></tr>`).join('')
      + '</tbody>'
    : '<tbody><tr><td class="live-empty">No open orders.</td></tr></tbody>';
}

/* after each fill (in time order): shares held and the window's P&L if Up / if Down wins, cost and fees included */
function runningPnl(fills) {
  let up = 0, dn = 0, spent = 0;
  return fills.map((f) => {
    const n = Number(f.size) || 0;                      // a sell is a negative size
    if (f.outcome === 'Up') up += n; else dn += n;
    spent += (Number(f.price) || 0) * n + (Number(f.fee) || 0);
    return { up, dn, ifUp: up - spent, ifDown: dn - spent };
  });
}

function fillRow(f, withWindow = false, run = null) {
  const edge = f.fair == null ? null : f.fair - f.price;
  const rule = f.tag === 'pair' ? 'Pair completion · ' : ['rise', 'btc', 'bal', 'top', 'ladder', 'wpair', 'v2a', 'v2b', 'v2c', 'v2d1', 'v2d2', 'v2d4', 'v2e1', 'v2e2', 'v2e3', 'v3e', 'v3m', 'v3r', 'v3s', 'v3b', 'v3x', 'v3la', 'v3lc', 'v3cm', 'v4e', 'v4m', 'v4n', 'v4b', 'v5m', 'v5b', 'v6s', 'v6r', 'v6h', 'v6f', 'v6n'].includes(f.tag) ? `${DEC_TAG[f.tag]} · ` : '';
  return `<tr>
    <td>${fmt.short(f.ts)}</td>${withWindow ? `<td class="dim">${fmt.hm((Number(String(f.slug).split('-').pop()) || 0) * 1000)}</td>` : ''}
    <td>${legTag(isSold(f) ? 'sell' : f.kind)}</td><td>${sideTag(f.outcome)}</td><td>${fmt.px3(f.price)}</td><td>${fmt.num(f.size, 2)}</td>
    <td>${fmt.px3(f.fair)}</td><td class="${cls(edge)}">${edge == null ? '—' : fmt.cents(edge)}</td>
    <td>${f.fee ? fmt.money(f.fee) : '—'}</td><td class="dim">${rule}${esc(HOW[f.how] || f.how)}</td><td>${fmt.mmss(f.secs_left)}</td>${run ? `
    <td>${fmt.num(run.up, 2)} / ${fmt.num(run.dn, 2)}</td><td class="${cls(run.ifUp)}">${fmt.signed(run.ifUp)}</td>
    <td class="${cls(run.ifDown)}">${fmt.signed(run.ifDown)}</td>` : ''}</tr>`;
}

function renderFills(w) {
  const fills = (w && w.fills) || [];
  $('#fills-sub').textContent = fills.length
    ? `${fills.length} fills this window (latest first). Edge at fill = our fair for that side then, minus the price.`
    : 'No fills yet this window.';
  $('#fills-table').innerHTML = fills.length
    ? '<thead><tr><th scope="col">When</th><th scope="col">Leg</th><th scope="col">Side</th><th scope="col">Price</th><th scope="col">Shares</th>'
      + '<th scope="col">Fair then</th><th scope="col">Edge at fill</th><th scope="col">Fee</th><th scope="col">How</th><th scope="col">Left</th></tr></thead><tbody>'
      + fills.slice().reverse().slice(0, 80).map((f) => fillRow(f)).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">Nothing filled yet.</td></tr></tbody>';
}

function renderPending(rows) {
  $('#pending-table').innerHTML = rows.length
    ? '<thead><tr><th scope="col">Window</th><th scope="col">Closed</th><th scope="col">Up / Down shares</th><th scope="col">Cost</th></tr></thead><tbody>'
      + rows.map((r) => `<tr><td>${fmt.window(r.end - WINDOW_S)}</td><td>${fmt.age(Date.now() / 1000 - r.end)} ago</td>
          <td>${fmt.num(r.up, 1)} / ${fmt.num(r.down, 1)}</td><td>${fmt.money(r.cost)}</td></tr>`).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">Nothing awaiting a result.</td></tr></tbody>';
}

function renderFeeds(f) {
  const rows = [];
  for (const [name, s] of Object.entries(f.spot || {})) {
    rows.push([name, s.connected ? 'connected' : 'down', `${fmt.num(s.messages)} msgs · last ${s.last_msg_age_s == null ? '—' : s.last_msg_age_s + 's'} ago`
      + (f.composite && f.composite.lag_ms && f.composite.lag_ms[name] != null ? ` · trade lag ${f.composite.lag_ms[name]} ms` : '')
      + (s.reconnects ? ` · ${s.reconnects} reconnects` : '')]);
  }
  const c = f.composite;
  rows.push(['composite', c ? `${c.n} sources` : 'warming up',
    c ? `${coinPx(c.price)} · Chainlink level ${coinPx(c.cl_price)} · age ${c.age_ms} ms`
      + (c.lagging && c.lagging.length ? ` · lagging: ${c.lagging.join(', ')}` : '') : '—']);
  const cl = f.chainlink || {};
  rows.push(['chainlink', cl.connected ? 'connected' : 'down',
    `price ${cl.price ? coinPx(cl.price) : '—'} · TWAP-60 ${
      cl.twap60 ? coinPx(cl.twap60) : '—'} · ${cl.delay_ms == null ? '—' : cl.delay_ms + ' ms'} behind`
      + ` · composite basis ${cl.basis_bps == null ? '—' : cl.basis_bps + ' bps'}`]);
  const b = f.book || {};
  rows.push(['book channel', b.connected ? `${b.connections} connection${b.connections === 1 ? '' : 's'}` : 'down',
    `${fmt.num(b.messages)} msgs · lag p50 ${b.lag_p50_ms == null ? '—' : b.lag_p50_ms + ' ms'} / p90 ${
      b.lag_p90_ms == null ? '—' : b.lag_p90_ms + ' ms'} · ${b.tokens || 0} tokens` + (b.reconnects ? ` · ${b.reconnects} drops` : '')]);
  $('#feeds-table').innerHTML = '<thead><tr><th scope="col">Feed</th><th scope="col">State</th><th scope="col">Detail</th></tr></thead><tbody>'
    + rows.map(([a, s, dt]) => `<tr><td>${esc(a)}</td><td><span class="status" data-status="${/down|warming/.test(s) ? 'missed-open' : 'live'}">${esc(s)}</span></td><td class="dim wrap">${esc(dt)}</td></tr>`).join('')
    + '</tbody>';
}

/* ═════════════════════════════════ windows page ═════════════════════════════ */

async function loadWindows() {
  try {
    state.win.data = await api(`/api/windows?page=${state.win.page}&size=50`);
  } catch (e) {
    notice('Could not load the windows: ' + e.message);
    return;
  }
  renderWindows();
}

function renderWindows() {
  const d = state.win.data;
  if (!d) return;
  const s = d.summary;
  const k = [
    { l: 'Settled PnL', v: fmt.signed(s.pnl), n: `${s.windows} windows · ${s.traded} traded`, c: cls(s.pnl) },
    { l: 'Return on cost', v: fmt.spct(s.return_on_cost), n: `on ${fmt.money(s.cost)} bought` },
    { l: 'Maker bids', v: fmt.signed(s.maker_pnl), n: `${fmt.spct(s.maker_return)} on ${fmt.money(s.maker_cost)}`, c: cls(s.maker_pnl) },
    { l: 'Taker clips', v: fmt.signed(s.taker_pnl), n: `${fmt.spct(s.taker_return)} on ${fmt.money(s.taker_cost)} · fees ${fmt.money(s.fees)}`, c: cls(s.taker_pnl) },
    { l: 'Windows won', v: fmt.pct(s.window_win_rate), n: `median window ${fmt.signed(s.median_window_pnl)}` },
    { l: 'Est. maker rebates', v: fmt.money(s.rebate_est), n: '20% of the fee on our bid fills; not in PnL' },
  ];
  $('#win-kpis').innerHTML = k.map((x) => `
    <div class="kpi"><span class="kpi-label">${x.l}</span>
    <span class="kpi-value ${x.c || ''}">${x.v}</span><span class="kpi-note">${x.n}</span></div>`).join('');
  renderCurve(d.curve || []);
  renderBench(s, d.benchmark);
  renderWinTable(d);
}

/* cumulative settled PnL, total and per leg, on one money axis */
function renderCurve(curve) {
  const host = $('#win-chart');
  if (curve.length < 2) { emptyChart(host, curve.length ? 'One window settled so far' : 'No settled windows yet', 260); legend($('#win-legend'), []); return; }
  const cT = css('--text-primary'), cM = css('--leg-maker'), cK = css('--leg-taker'), ring = css('--surface-1');
  const H = 280, m = { t: 14, r: 92, b: 32, l: 60 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const xs = curve.map((c) => c[0]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const all = curve.flatMap((c) => [c[1], c[3], c[4], 0]);
  const nt = niceTicks(Math.min(...all), Math.max(...all), 5);
  const X = (t) => m.l + (x1 > x0 ? (t - x0) / (x1 - x0) : 0.5) * pw;
  const Y = (v) => m.t + ph - (v - nt.lo) / (nt.hi - nt.lo) * ph;
  nt.ticks.forEach((t) => {
    svg.appendChild(el('line', { class: t === 0 ? 'zero-line' : 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(t), y2: Y(t) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 8, y: Y(t) + 4, 'text-anchor': 'end' });
    lab.textContent = axisMoney(t);
    svg.appendChild(lab);
  });
  const nlab = Math.min(6, Math.max(2, Math.floor(pw / 130)));
  for (let i = 0; i < nlab; i++) {
    const t = x0 + (x1 - x0) * i / (nlab - 1);
    const lab = el('text', { class: 'axis-text', x: X(t), y: H - 10, 'text-anchor': i === 0 ? 'start' : i === nlab - 1 ? 'end' : 'middle' });
    lab.textContent = fmt.stamp(t * 1000).slice(5);
    svg.appendChild(lab);
  }
  const lines = [[3, cM, 'maker'], [4, cK, 'taker'], [1, cT, 'total']];
  lines.forEach(([i, color, name]) => {
    const d = curve.map((c, j) => `${j ? 'L' : 'M'}${X(c[0]).toFixed(1)},${Y(c[i]).toFixed(1)}`).join(' ');
    svg.appendChild(el('path', { class: 'series-line', d, stroke: color }));
    const last = curve[curve.length - 1];
    svg.appendChild(el('circle', { cx: X(last[0]), cy: Y(last[i]), r: 4.5, fill: color, stroke: ring, 'stroke-width': 2 }));
    const t = el('text', { class: name === 'total' ? 'value-label-strong' : 'value-label', x: X(last[0]) + 9, y: Y(last[i]) + 4 });
    t.textContent = `${name} ${fmt.signed(last[i])}`;
    svg.appendChild(t);
  });
  const cross = el('line', { class: 'crosshair', y1: m.t, y2: m.t + ph, opacity: 0 });
  svg.appendChild(cross);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const bx = svg.getBoundingClientRect();
    const t = x0 + ((ev.clientX - bx.left - m.l) / (pw || 1)) * (x1 - x0);
    let best = curve[0], bd = Infinity;
    for (const c of curve) { const dd = Math.abs(c[0] - t); if (dd < bd) { bd = dd; best = c; } }
    cross.setAttribute('x1', X(best[0])); cross.setAttribute('x2', X(best[0])); cross.setAttribute('opacity', 1);
    showTip(tipRows(`${fmt.window(best[0] - WINDOW_S)}`, [
      ['This window', fmt.signed(best[2])],
      ['Cumulative', fmt.signed(best[1]), cT],
      ['Maker bids', fmt.signed(best[3]), cM],
      ['Taker clips', fmt.signed(best[4]), cK],
    ]), ev);
  });
  hit.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });
  legend($('#win-legend'), [{ label: 'Cumulative settled PnL', color: cT },
    { label: 'Maker bids', color: cM }, { label: 'Taker clips', color: cK }]);
}

function renderBench(s, b) {
  if (!b) return;
  $('#bench-sub').textContent = `${b.name}: ${b.source}. Our column counts traded windows only.`;
  const rows = [
    ['Fills per window (median)', fmt.num(s.fills_per_window), fmt.num(b.fills_per_window)],
    ['Windows holding both sides', fmt.pct(s.both_sides, 1), fmt.pct(b.both_sides, 1)],
    ['Hedged (median smaller/larger side)', fmt.pct(s.hedge, 0), fmt.pct(b.hedge, 0)],
    ['Up + Down average cost per pair (median)', s.pair_cost == null ? '—' : s.pair_cost.toFixed(3), b.pair_cost.toFixed(3)],
    ['Share of fills that are resting bids', fmt.pct(s.maker_fill_share, 0), fmt.pct(b.maker_fill_share, 0)],
    ['Resting bids: return on cost', fmt.spct(s.maker_return), fmt.spct(b.maker_return)],
    ['Taker clips: return on cost (after fees)', fmt.spct(s.taker_return), fmt.spct(b.taker_return)],
    ['All fills: return on cost', fmt.spct(s.return_on_cost), fmt.spct(b.return_on_cost)],
    ['Windows won', fmt.pct(s.window_win_rate, 1), fmt.pct(b.window_win_rate, 1)],
    ['Median window PnL', fmt.signed(s.median_window_pnl), fmt.signed(b.median_window_pnl)],
    ['First fill (median s after the open)', s.first_fill_s == null ? '—' : `${Math.round(s.first_fill_s)} s`, `${b.first_fill_s} s`],
    ['Last fill (median s after the open)', s.last_fill_s == null ? '—' : `${Math.round(s.last_fill_s)} s`, `${b.last_fill_s} s`],
  ];
  $('#bench-table').innerHTML = '<thead><tr><th scope="col">Measure</th><th scope="col">'
    + esc((MODE_INFO[MODE] || MODE_INFO.paper)[5]) + '</th><th scope="col">'
    + esc(b.name) + '</th></tr></thead><tbody>'
    + rows.map(([a, x, y]) => `<tr><td>${a}</td><td>${x}</td><td>${y}</td></tr>`).join('') + '</tbody>';
}

function renderWinTable(d) {
  const rows = d.rows || [];
  $('#win-table').innerHTML = rows.length
    ? '<thead><tr><th scope="col">Window (GMT+9)</th><th scope="col">Result</th><th scope="col">PnL</th><th scope="col">Maker / taker</th>'
      + '<th scope="col">Up / Down shares</th><th scope="col">Pair cost</th><th scope="col">Hedged</th><th scope="col">Fills m / t</th>'
      + '<th scope="col">Cost</th><th scope="col">Fees</th><th scope="col">Settled by</th></tr></thead><tbody>'
      + rows.map((r) => `<tr data-slug="${esc(r.slug)}" tabindex="0"${state.win.open === r.slug ? ' class="is-open"' : ''}>
          <td>${fmt.window(r.start || (r.end - WINDOW_S))}</td><td>${r.winner ? sideTag(r.winner) : '—'}</td>
          <td class="${cls(r.pnl)}">${r.fills ? fmt.signed(r.pnl) : '—'}</td>
          <td>${r.fills ? `${fmt.signed(r.maker_pnl)} / ${fmt.signed(r.taker_pnl)}` : '—'}</td>
          <td>${fmt.num(r.up_shares, 0)} / ${fmt.num(r.down_shares, 0)}</td>
          <td>${r.pair_cost == null ? '—' : r.pair_cost.toFixed(3)}</td><td>${r.hedge == null ? '—' : fmt.pct(r.hedge, 0)}</td>
          <td>${r.maker_fills || 0} / ${r.taker_fills || 0}</td><td>${fmt.money(r.cost)}</td><td>${fmt.money(r.fees)}</td>
          <td class="dim">${esc(r.winner_src || '')}</td></tr>`).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">No settled windows yet. The first settles ~40 s after its close.</td></tr></tbody>';
  const pages = Math.max(1, Math.ceil(d.total / d.size));
  $('#win-pager').innerHTML = pages > 1
    ? `<button class="btn btn-sm" id="wp-prev" type="button"${d.page <= 0 ? ' disabled' : ''}>Newer</button>
       <span class="pager-info">page ${d.page + 1} of ${pages} · ${d.total} windows</span>
       <button class="btn btn-sm" id="wp-next" type="button"${d.page >= pages - 1 ? ' disabled' : ''}>Older</button>`
    : '';
  const prev = $('#wp-prev'), next = $('#wp-next');
  if (prev) prev.onclick = () => { state.win.page = Math.max(0, state.win.page - 1); loadWindows(); };
  if (next) next.onclick = () => { state.win.page += 1; loadWindows(); };
}

async function openWindow(slug) {
  let doc;
  try { doc = await api(`/api/window?slug=${encodeURIComponent(slug)}`); } catch (e) {
    notice(`No archive for ${slug} (kept 7 days).`); return;
  }
  state.win.open = slug;
  $$('#win-table tbody tr').forEach((tr) => tr.classList.toggle('is-open', tr.dataset.slug === slug));
  const card = $('#win-detail');
  card.hidden = false;
  const r = doc.result || {};
  $('#wd-eyebrow').textContent = `Window · ${slug}`;
  $('#wd-title').textContent = fmt.window(doc.start);
  $('#wd-sub').textContent = `${doc.question || ''} · strike ${doc.strike ? coinPx(doc.strike) : '—'}`
    + ` (${doc.strike_src || '—'}) · result ${r.winner || '—'} (${r.winner_src || 'not settled'})`;
  $('#wd-json').href = withAsset(`/api/window?slug=${encodeURIComponent(slug)}`);
  $('#wd-json').setAttribute('download', `${slug}.json`);
  const k = [
    { l: 'PnL', v: r.fills ? fmt.signed(r.pnl) : '—', n: `maker ${fmt.signed(r.maker_pnl)} · taker ${fmt.signed(r.taker_pnl)}`, c: cls(r.pnl) },
    { l: 'Up / Down shares', v: `${fmt.num(r.up_shares, 0)} / ${fmt.num(r.down_shares, 0)}`, n: `pair cost ${r.pair_cost == null ? '—' : r.pair_cost.toFixed(3)}` },
    { l: 'Fills', v: fmt.num(r.fills || 0), n: `${r.maker_fills || 0} bids · ${r.taker_fills || 0} clips` },
    { l: 'Cost', v: fmt.money(r.cost), n: `fees ${fmt.money(r.fees)}` },
  ];
  $('#wd-kpis').innerHTML = k.map((x) => `
    <div class="kpi"><span class="kpi-label">${x.l}</span>
    <span class="kpi-value ${x.c || ''}">${x.v}</span><span class="kpi-note">${x.n}</span></div>`).join('');
  renderPriceChart($('#wd-chart'), $('#wd-legend'), doc.hist || [], doc.start, doc.fills || [], 320, doc.clips || [],
    doc.pairings == null ? null : doc.pairings,
    doc.pairings != null ? { strategy: 'cross-market pairs',
      rise: (doc.result && doc.result.rise) || 0.30,
      deep_rise: (doc.result && doc.result.deep_rise) || 0.10 } : null);
  renderSpotChart($('#wd-spot-chart'), $('#wd-spot-legend'), doc.hist || [], doc.start, doc.strike, 240, doc.decisions || []);
  const fills = (doc.fills || []).slice().sort((a, b) => a.ts - b.ts);
  const run = runningPnl(fills);
  $('#wd-fills').innerHTML = fills.length
    ? '<thead><tr><th scope="col">When</th><th scope="col">Leg</th><th scope="col">Side</th><th scope="col">Price</th><th scope="col">Shares</th>'
      + '<th scope="col">Fair then</th><th scope="col">Edge at fill</th><th scope="col">Fee</th><th scope="col">How</th><th scope="col">Left</th>'
      + '<th scope="col">Up / Down held</th><th scope="col">If Up wins</th><th scope="col">If Down wins</th></tr></thead><tbody>'
      + fills.map((f, i) => fillRow(f, false, run[i])).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">Watched only: nothing filled.</td></tr></tbody>';
  const why = doc.why || {};
  const top = (c) => Object.entries(c || {}).sort((a, b) => b[1] - a[1]).slice(0, 3)
    .map(([k2, n]) => `${k2} (${n}s)`).join(', ') || '—';
  $('#wd-foot').textContent = `Where the seconds went — Up bid: ${top(why.maker_Up)}. Down bid: ${top(why.maker_Down)}. Clips: ${top(why.taker)}.`;
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ═══════════════════════════════ fill quality ══════════════════════════════ */

async function loadFills() {
  try { state.fills = await api('/api/fills?limit=300'); } catch (e) { notice('Could not load fills: ' + e.message); return; }
  renderFq();
}

function renderFq() {
  const d = state.fills;
  if (!d) return;
  const s = d.summary || {};
  const mk = s.maker || {}, tk = s.taker || {};
  const k = [
    { l: 'Bid fills', v: fmt.num(mk.fills || 0), n: `${fmt.num(mk.shares || 0, 0)} shares · avg ${fmt.px3(mk.avg_price)}` },
    { l: 'Bid PnL per share', v: fmt.cents(mk.pnl_per_share), n: `win rate ${fmt.pct(mk.win_rate)}`, c: cls(mk.pnl_per_share) },
    { l: 'Clip fills', v: fmt.num(tk.fills || 0), n: `${fmt.num(tk.shares || 0, 0)} shares · avg ${fmt.px3(tk.avg_price)}` },
    { l: 'Clip PnL per share', v: fmt.cents(tk.pnl_per_share), n: `after fees · win rate ${fmt.pct(tk.win_rate)}`, c: cls(tk.pnl_per_share) },
  ];
  $('#fq-kpis').innerHTML = k.map((x) => `
    <div class="kpi"><span class="kpi-label">${x.l}</span>
    <span class="kpi-value ${x.c || ''}">${x.v}</span><span class="kpi-note">${x.n}</span></div>`).join('');
  const order = ['maker', 'maker:queue', 'maker:sweep', 'maker:cross', 'taker', 'taker:take'];
  const label = { maker: 'All bids', 'maker:queue': '· through the queue', 'maker:sweep': '· a trade went through',
    'maker:cross': '· the ask fell through', taker: 'All clips', 'taker:take': '· bought the ask' };
  const keys = order.filter((x) => s[x]);
  $('#fq-table').innerHTML = keys.length
    ? '<thead><tr><th scope="col">Leg</th><th scope="col">Fills</th><th scope="col">Shares</th><th scope="col">Avg price</th>'
      + '<th scope="col">Edge at fill</th><th scope="col">Markout 1 s</th><th scope="col">5 s</th><th scope="col">30 s</th>'
      + '<th scope="col">PnL / share</th><th scope="col">Won</th><th scope="col">Return</th></tr></thead><tbody>'
      + keys.map((x) => { const a = s[x]; return `<tr><td>${x.includes(':') ? `<span class="dim">${label[x]}</span>` : legTag(x) + ' ' + label[x]}</td>
          <td>${fmt.num(a.fills)}</td><td>${fmt.num(a.shares, 0)}</td><td>${fmt.px3(a.avg_price)}</td>
          <td class="${cls(a.edge_at_fill)}">${fmt.cents(a.edge_at_fill)}</td><td class="${cls(a.mo_1s)}">${fmt.cents(a.mo_1s)}</td>
          <td class="${cls(a.mo_5s)}">${fmt.cents(a.mo_5s)}</td><td class="${cls(a.mo_30s)}">${fmt.cents(a.mo_30s)}</td>
          <td class="${cls(a.pnl_per_share)}">${fmt.cents(a.pnl_per_share)}</td><td>${fmt.pct(a.win_rate)}</td>
          <td class="${cls(a.return)}">${fmt.spct(a.return)}</td></tr>`; }).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">No settled fills yet.</td></tr></tbody>';
  renderMarkoutChart(mk, tk);
  $('#fq-cal').textContent = calLine(d.calibration, d.clips);
  renderPerWindow(d.per_window || []);
  const rows = d.rows || [];
  $('#fr-sub').textContent = `${fmt.num(d.total)} settled fills on disk; the latest ${rows.length} below.`;
  $('#fr-table').innerHTML = rows.length
    ? '<thead><tr><th scope="col">When</th><th scope="col">Window</th><th scope="col">Leg</th><th scope="col">Side</th><th scope="col">Price</th><th scope="col">Shares</th>'
      + '<th scope="col">Fair then</th><th scope="col">Edge</th><th scope="col">1 s / 5 s / 30 s</th><th scope="col">Won</th><th scope="col">PnL</th></tr></thead><tbody>'
      + rows.map((f) => `<tr><td>${fmt.short(f.ts)}</td><td class="dim">${fmt.hm((Number(String(f.slug).split('-').pop()) || 0) * 1000)}</td>
          <td>${legTag(isSold(f) ? 'sell' : f.kind)}</td><td>${sideTag(f.outcome)}</td><td>${fmt.px3(f.price)}</td><td>${fmt.num(f.size, 2)}</td>
          <td>${fmt.px3(f.fair)}</td><td class="${cls(f.fair == null ? null : f.fair - f.price)}">${f.fair == null ? '—' : fmt.cents(f.fair - f.price)}</td>
          <td>${[f.mo_1s, f.mo_5s, f.mo_30s].map((v) => `<span class="${cls(v)}">${fmt.cents(v)}</span>`).join(' / ')}</td>
          <td>${f.won ? 'yes' : 'no'}</td><td class="${cls(f.pnl)}">${fmt.signed(f.pnl)}</td></tr>`).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">No settled fills yet.</td></tr></tbody>';
}

/* Overall profit / loss of every settled shared 5-minute window, aggregated
   across the six market ledgers. */
const PW = { n: Number(stored('xshark-pw', '96')) || 96 };

function renderPerWindow(all) {
  $$('#pw-range .seg-btn').forEach((b) => b.classList.toggle('is-on', Number(b.dataset.n) === PW.n));
  const rows = all.slice(-PW.n);
  const sum = (k) => rows.reduce((a, r) => a + (r[k] || 0), 0);
  const won = rows.filter((r) => r.pnl > 0).length;
  const best = rows.reduce((b, r) => (b == null || r.pnl > b.pnl ? r : b), null);
  const worst = rows.reduce((b, r) => (b == null || r.pnl < b.pnl ? r : b), null);
  const k = [
    { l: 'These windows', v: fmt.signed(sum('pnl')), n: `${rows.length} windows · ${won} won`, c: cls(sum('pnl')) },
    { l: 'Maker / taker', v: `${fmt.signed(sum('maker_pnl'))} / ${fmt.signed(sum('taker_pnl'))}`, n: `fees ${fmt.money(sum('fees'))}` },
    { l: 'Per window', v: rows.length ? fmt.signed(sum('pnl') / rows.length) : '—', n: 'average' },
    { l: 'Best / worst', v: best ? `${fmt.signed(best.pnl)} / ${fmt.signed(worst.pnl)}` : '—',
      n: best ? `${fmt.hm(best.start * 1000)} / ${fmt.hm(worst.start * 1000)} ${TZ_LABEL}` : '' },
  ];
  $('#pw-kpis').innerHTML = k.map((x) => `
    <div class="kpi"><span class="kpi-label">${x.l}</span>
    <span class="kpi-value ${x.c || ''}">${x.v}</span><span class="kpi-note">${x.n}</span></div>`).join('');
  const host = $('#pw-chart');
  const cPos = css('--series-1'), cNeg = css('--series-2');
  if (!rows.length) { emptyChart(host, 'No settled windows yet', 220); legend($('#pw-legend'), []); }
  else {
    const H = 260, m = { t: 14, r: 16, b: 34, l: 60 };
    const { svg, w } = mount(host, H);
    const pw = w - m.l - m.r, ph = H - m.t - m.b;
    const nt = niceTicks(Math.min(0, ...rows.map((r) => r.pnl)), Math.max(0, ...rows.map((r) => r.pnl)), 4);
    const Y = (v) => m.t + ph - (v - nt.lo) / (nt.hi - nt.lo) * ph;
    nt.ticks.forEach((t) => {
      svg.appendChild(el('line', { class: t === 0 ? 'zero-line' : 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(t), y2: Y(t) }));
      const lab = el('text', { class: 'axis-text', x: m.l - 8, y: Y(t) + 4, 'text-anchor': 'end' });
      lab.textContent = axisMoney(t);
      svg.appendChild(lab);
    });
    const slot = pw / Math.max(rows.length, 12);
    const bw = Math.max(2, Math.min(24, slot - 2));
    const every = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(pw / 64))));
    rows.forEach((r, i) => {
      const x = m.l + i * slot + (slot - bw) / 2, y0 = Y(0), y1 = Y(r.pnl), up = r.pnl >= 0;
      const hgt = Math.max(1, Math.abs(y1 - y0));
      svg.appendChild(el('path', { d: colPath(x, up ? y1 : y0, bw, hgt, Math.min(4, bw / 2), up), fill: up ? cPos : cNeg }));
      if (i % every === 0) {
        const lab = el('text', { class: 'axis-text', x: x + bw / 2, y: H - 12, 'text-anchor': 'middle' });
        lab.textContent = fmt.hm(r.start * 1000);
        svg.appendChild(lab);
      }
      const hit = el('rect', { class: 'hit', x: m.l + i * slot, y: m.t, width: slot, height: ph });
      hit.addEventListener('mousemove', (ev) => showTip(tipRows(fmt.window(r.start), [
        ['PnL', fmt.signed(r.pnl), up ? cPos : cNeg], ['Maker bids', fmt.signed(r.maker_pnl)],
        ['Taker clips', fmt.signed(r.taker_pnl)], ['Fees', fmt.money(r.fees)], ['Cost', fmt.money(r.cost)],
        ['Fills (bids / clips)', `${r.maker_fills} / ${r.taker_fills}`],
        ...Object.entries(r.markets || {}).sort((a, b) => a[0].localeCompare(b[0]))
          .map(([asset, pnl]) => [`${asset} PnL`, fmt.signed(pnl), cls(pnl)]),
      ]), ev));
      hit.addEventListener('mouseleave', hideTip);
      if (r.slug) hit.addEventListener('click', () => {
        const q = new URLSearchParams(location.search);
        q.set('window', r.slug);
        location.search = q.toString();
        location.hash = 'windows';
      });
      svg.appendChild(hit);
    });
    legend($('#pw-legend'), [{ label: 'Profit', color: cPos }, { label: 'Loss', color: cNeg }]);
  }
  $('#pw-sub').textContent = `Combined settled PnL across all six markets for each 5-minute window (${TZ_LABEL}), newest on the right. `
    + 'Hover a column for each market’s contribution.';
  $('#pw-table').innerHTML = '<thead><tr><th scope="col">Window</th><th scope="col">Overall PnL</th>'
    + '<th scope="col">BTC</th><th scope="col">ETH</th><th scope="col">SOL</th><th scope="col">XRP</th>'
    + '<th scope="col">DOGE</th><th scope="col">BNB</th>'
    + '<th scope="col">Maker</th><th scope="col">Taker</th><th scope="col">Fees</th><th scope="col">Fills b / c</th></tr></thead><tbody>'
    + rows.slice().reverse().map((r) => `<tr><td>${fmt.window(r.start)}</td>
        <td class="${cls(r.pnl)}">${fmt.signed(r.pnl)}</td>`
        + ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB'].map((a) => `<td class="${cls((r.markets || {})[a])}">${fmt.signed((r.markets || {})[a] || 0)}</td>`).join('')
        + `<td>${fmt.signed(r.maker_pnl)}</td><td>${fmt.signed(r.taker_pnl)}</td>
        <td>${fmt.money(r.fees)}</td><td>${r.maker_fills} / ${r.taker_fills}</td></tr>`).join('') + '</tbody>';
}

/* how the paper venue decides a clip: measured on this VPS's real orders */
function calLine(c, clips) {
  const bits = [];
  if (c) {
    const r = c.race || {};
    bits.push(`Execution is calibrated on ${fmt.num(c.orders)} real orders sent from this VPS (${fmt.pct(c.real_fill_rate)} of them filled): `
      + `every order waits a real latency (p50 ${c.latency_p50_ms} ms, p90 ${c.latency_p90_ms} ms), and a clip that finds its price `
      + `still has to win the race at the real odds — ${fmt.pct(r.depth_lt3, 0)} with under 3× our size at the ask, `
      + `${fmt.pct(r.depth_3_20, 0)} at 3–20×, ${fmt.pct(r.depth_ge20, 0)} at 20×+, ${fmt.pct(r.ask_lower, 0)} if the ask came down.`);
  }
  if (clips && clips.sent) {
    const lost = clips.lost_race || 0, killed = clips.killed || 0;
    bits.push(`Settled windows: ${clips.sent} clips sent, ${(clips.filled || 0) + (clips.partial || 0)} filled, `
      + `${lost} lost the race, ${Math.max(0, killed - lost)} found nothing at the limit.`);
  }
  bits.push('Resting bids have no real counterpart on this VPS (its real bots never rested an order): they get the real latency only.');
  return bits.join(' ');
}

/* markouts per leg, in cents a share, on a zero baseline: grouped columns */
function renderMarkoutChart(mk, tk) {
  const host = $('#fq-chart');
  const groups = [['Edge at fill', 'edge_at_fill'], ['1 s later', 'mo_1s'], ['5 s later', 'mo_5s'], ['30 s later', 'mo_30s'], ['At settlement', 'pnl_per_share']];
  const legs = [['Bids', mk, css('--leg-maker')], ['Clips', tk, css('--leg-taker')]].filter(([, a]) => a && a.fills);
  if (!legs.length) { emptyChart(host, 'No settled fills yet', 200); legend($('#fq-legend'), []); return; }
  const H = 230, m = { t: 16, r: 16, b: 34, l: 52 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const vals = legs.flatMap(([, a]) => groups.map(([, key]) => (a[key] == null ? 0 : a[key] * 100))).concat([0]);
  const nt = niceTicks(Math.min(...vals), Math.max(...vals), 4);
  const Y = (v) => m.t + ph - (v - nt.lo) / (nt.hi - nt.lo) * ph;
  nt.ticks.forEach((t) => {
    svg.appendChild(el('line', { class: t === 0 ? 'zero-line' : 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(t), y2: Y(t) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 8, y: Y(t) + 4, 'text-anchor': 'end' });
    lab.textContent = `${t > 0 ? '+' : ''}${Number(t.toFixed(2))}¢`;
    svg.appendChild(lab);
  });
  const gw = pw / groups.length;
  const bw = Math.min(24, (gw - 16) / legs.length - 2);
  groups.forEach(([name, key], gi) => {
    const gx = m.l + gi * gw + gw / 2;
    const lab = el('text', { class: 'axis-text', x: gx, y: H - 12, 'text-anchor': 'middle' });
    lab.textContent = name;
    svg.appendChild(lab);
    legs.forEach(([lname, a, color], li) => {
      const v = a[key] == null ? null : a[key] * 100;
      if (v == null) return;
      const x = gx - (legs.length * (bw + 2)) / 2 + li * (bw + 2);
      const y0 = Y(0), y1 = Y(v);
      const up = v >= 0;
      const hgt = Math.max(1, Math.abs(y1 - y0));
      const bar = el('path', { d: colPath(x, up ? y1 : y0, bw, hgt, 4, up), fill: color });
      svg.appendChild(bar);
      const t = el('text', { class: 'bar-label', x: x + bw / 2, y: up ? y1 - 4 : y1 + 12, 'text-anchor': 'middle' });
      t.textContent = `${v >= 0 ? '+' : ''}${v.toFixed(1)}`;
      svg.appendChild(t);
      const hit = el('rect', { class: 'hit', x: x - 2, y: Math.min(y0, y1) - 6, width: bw + 4, height: hgt + 12 });
      hit.addEventListener('mousemove', (ev) => showTip(tipRows(`${lname} · ${name}`, [
        ['Cents a share', `${v >= 0 ? '+' : ''}${v.toFixed(2)}¢`, color], ['Fills', fmt.num(a.fills)]]), ev));
      hit.addEventListener('mouseleave', hideTip);
      svg.appendChild(hit);
    });
  });
  legend($('#fq-legend'), legs.map(([lname, , color]) => ({ label: lname === 'Bids' ? 'Maker bids' : 'Taker clips', color })));
}

/* ══════════════════════════════════ earnings ══════════════════════════════
   Polarity per calendar period (GMT+9): a diverging column chart on a zero
   baseline, profit in slot 1 and loss in slot 2 -- Bo-BOTT's rule. */

async function loadEarnings() {
  try { state.earn.data = await api(`/api/earnings?period=${state.earn.period}`); } catch (e) { notice('Could not load earnings: ' + e.message); return; }
  renderEarnings();
}

function renderEarnings() {
  const d = state.earn.data;
  if (!d) return;
  $$('#earn-period .seg-btn').forEach((b) => b.classList.toggle('is-on', b.dataset.period === state.earn.period));
  const rows = d.rows || [];
  const tot = rows.reduce((a, r) => ({ pnl: a.pnl + r.pnl, maker: a.maker + r.maker_pnl, taker: a.taker + r.taker_pnl,
    reb: a.reb + r.rebate_est, fees: a.fees + r.fees }), { pnl: 0, maker: 0, taker: 0, reb: 0, fees: 0 });
  const best = rows.reduce((b, r) => (b == null || r.pnl > b.pnl ? r : b), null);
  const worst = rows.reduce((b, r) => (b == null || r.pnl < b.pnl ? r : b), null);
  const k = [
    { l: 'Shown periods', v: fmt.signed(tot.pnl), n: `${rows.length} ${state.earn.period}${rows.length === 1 ? '' : 's'}`, c: cls(tot.pnl) },
    { l: 'Maker / taker', v: `${fmt.signed(tot.maker)} / ${fmt.signed(tot.taker)}`, n: `fees ${fmt.money(tot.fees)}` },
    { l: 'Est. maker rebates', v: fmt.money(tot.reb), n: 'not in PnL' },
    { l: 'Best / worst', v: best ? `${fmt.signed(best.pnl)} / ${fmt.signed(worst.pnl)}` : '—', n: best ? `${best.key} / ${worst.key}` : '' },
  ];
  $('#earnings-kpis').innerHTML = k.map((x) => `
    <div class="kpi"><span class="kpi-label">${x.l}</span>
    <span class="kpi-value ${x.c || ''}">${x.v}</span><span class="kpi-note">${x.n}</span></div>`).join('');
  const host = $('#earnings-chart');
  const cPos = css('--series-1'), cNeg = css('--series-2');
  if (!rows.length) { emptyChart(host, 'No settled windows yet', 220); legend($('#earnings-legend'), []); }
  else {
    const H = 260, m = { t: 14, r: 16, b: 34, l: 60 };
    const { svg, w } = mount(host, H);
    const pw = w - m.l - m.r, ph = H - m.t - m.b;
    const nt = niceTicks(Math.min(0, ...rows.map((r) => r.pnl)), Math.max(0, ...rows.map((r) => r.pnl)), 4);
    const Y = (v) => m.t + ph - (v - nt.lo) / (nt.hi - nt.lo) * ph;
    nt.ticks.forEach((t) => {
      svg.appendChild(el('line', { class: t === 0 ? 'zero-line' : 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(t), y2: Y(t) }));
      const lab = el('text', { class: 'axis-text', x: m.l - 8, y: Y(t) + 4, 'text-anchor': 'end' });
      lab.textContent = axisMoney(t);
      svg.appendChild(lab);
    });
    const slot = pw / rows.length, bw = Math.max(3, Math.min(24, slot - 4));
    const every = Math.ceil(rows.length / Math.max(1, Math.floor(pw / 70)));
    rows.forEach((r, i) => {
      const x = m.l + i * slot + (slot - bw) / 2, y0 = Y(0), y1 = Y(r.pnl), up = r.pnl >= 0;
      const hgt = Math.max(1, Math.abs(y1 - y0));
      svg.appendChild(el('path', { d: colPath(x, up ? y1 : y0, bw, hgt, 4, up), fill: up ? cPos : cNeg }));
      if (i % every === 0 || i === rows.length - 1) {
        const lab = el('text', { class: 'axis-text', x: x + bw / 2, y: H - 12, 'text-anchor': 'middle' });
        lab.textContent = state.earn.period === 'month' ? r.key : r.key.slice(5);
        svg.appendChild(lab);
      }
      const hit = el('rect', { class: 'hit', x: m.l + i * slot, y: m.t, width: slot, height: ph });
      hit.addEventListener('mousemove', (ev) => showTip(tipRows(`${state.earn.period === 'week' ? 'Week of ' : ''}${r.key} (${TZ_LABEL})`, [
        ['Settled PnL', fmt.signed(r.pnl), up ? cPos : cNeg], ['Maker bids', fmt.signed(r.maker_pnl)], ['Taker clips', fmt.signed(r.taker_pnl)],
        ['Fees paid', fmt.money(r.fees)], ['Est. maker rebates', fmt.money(r.rebate_est)], ['Windows (traded)', `${r.windows} (${r.traded})`],
      ]), ev));
      hit.addEventListener('mouseleave', hideTip);
      svg.appendChild(hit);
    });
    legend($('#earnings-legend'), [{ label: 'Profit', color: cPos }, { label: 'Loss', color: cNeg }]);
  }
  $('#earnings-table').innerHTML = '<thead><tr><th scope="col">Period</th><th scope="col">PnL</th><th scope="col">Maker</th><th scope="col">Taker</th>'
    + '<th scope="col">Fees</th><th scope="col">Est. rebates</th><th scope="col">Cost</th><th scope="col">Windows</th></tr></thead><tbody>'
    + rows.slice().reverse().map((r) => `<tr><td>${r.key}</td><td class="${cls(r.pnl)}">${fmt.signed(r.pnl)}</td><td>${fmt.signed(r.maker_pnl)}</td>
        <td>${fmt.signed(r.taker_pnl)}</td><td>${fmt.money(r.fees)}</td><td>${fmt.money(r.rebate_est)}</td><td>${fmt.money(r.cost)}</td>
        <td>${r.windows} (${r.traded} traded)</td></tr>`).join('') + '</tbody>';
}

/* ═════════════════════════════════ tuning ══════════════════════════════════
   Bo-BOTT's "Bot variables" pattern with compare_bot's write key: edit, see
   what the running bot applied, save. An empty number means "no override".
   The form is not rebuilt while you are editing it. */

const CTRL = { view: null, edits: {}, key: stored('xshark-key', '') };

async function loadControls(force = false) {
  const busy = Object.keys(CTRL.edits).length || $('#ctrl-groups').contains(document.activeElement);
  if (busy && !force) return;
  try { CTRL.view = await api('/api/controls'); } catch { return; }
  renderControls();
}

function ctrlStatus(msg, kind = 'ok') {
  const s = $('#ctrl-status');
  s.textContent = msg || '';
  s.dataset.kind = kind;
}

function renderControls() {
  const v = CTRL.view;
  if (!v) return;
  const paired = v.strategy === 'cross-market pairs';
  ['#ctrl-apply', '#ctrl-revert', '#ctrl-reset'].forEach((selector) => { $(selector).hidden = false; });
  $('#ctrl-loss-reset').hidden = paired;
  const k = $('#ctrl-kill');
  k.setAttribute('aria-pressed', v.kill ? 'true' : 'false');
  k.textContent = v.kill ? 'Orders halted — resume' : 'Halt orders';
  const st = v.stored || {}, run = v.running || {}, def = v.defaults || {};
  const groups = [];
  v.spec.forEach((row) => {
    let g = groups.find((x) => x.name === row.group);
    if (!g) { g = { name: row.group, rows: [] }; groups.push(g); }
    g.rows.push(row);
  });
  const intro = paired ? `<div class="card ctrl-strategy-summary">
      <p><b>Cross-market lag</b> · paper only · parameters grouped by rule</p>
      <p>Each rule has an <b>on/off</b> switch at the top of its group. Rules are <b>independent</b> — each can buy the same asset/side when its own conditions match. Off rules do not open new entries (open positions still retry/pair/hold).</p>
      <p><b>Rules 1–6</b> repeat a five-share clip on each rule's own interval while that rule's entry condition stays true.
         <b>Rule 1</b> — lagging token ($0.30–$0.60) when peers average high; pair after +$0.30, or +$0.10 if unpaired after 40s.
         <b>Rule 2</b> — hold mid-band from 1:30 when peers &gt; $0.90.
         <b>Rule 3</b> — ≥$0.35 under peers in $0.10–$0.35; same pair / 40s fallback as Rule 1.
         <b>Rule 4</b> — ≥$0.35 under peers in $0.01–$0.15; pair after +$0.10.
         <b>Rule 5</b> — if ≥5 other markets &gt; $0.85, buy when ask &lt; $0.70 (no pair).
         <b>Rule 6</b> — from 2:20, if all 5 other markets &gt; $0.90, buy when ask is $0.70–$0.90 (no pair).
         <b>Rule 7</b> — every 7s buy ≤10 tokens of OK Up(A)+Down(B) among the six markets (cheapest first). Same-market complete set is secondary.
         Chase stop +$0.05 on rules 1, 3 &amp; 4.
         <b>Other markets needed</b> (default 5) sets how many peers must quote before the average counts.</p>
      <p>Edit a value and click Apply (shared across all six markets within ~2 s). Use Restart with this cash above to archive the run and start again.</p>
    </div>` : '';
  $('#ctrl-groups').innerHTML = intro + groups.map((g, gi) => {
    const n = g.rows.filter((r) => r.key in st || r.key in CTRL.edits).length;
    const open = paired || gi < 3;
    return `<details class="ctrl-group"${open ? ' open' : ''}>
      <summary>${esc(g.name)}${n ? `<span class="ctrl-count">${n} set</span>` : ''}</summary>
      <div class="ctrl-grid">${g.rows.map((row) => {
        const key = row.key, dirty = key in CTRL.edits;
        const shown = dirty ? CTRL.edits[key] : (key in st ? st[key] : null);
        const effective = key in run ? run[key] : def[key];
        const input = row.kind === 'bool'
          ? `<label class="ctrl-switch"><input type="checkbox" data-key="${key}" data-kind="bool"${
              (shown == null ? effective : shown) ? ' checked' : ''}><span>${
              shown == null ? `default (${effective ? 'on' : 'off'})` : (shown ? 'on' : 'off')}</span></label>`
          : `<input type="number" data-key="${key}" data-kind="${row.kind}" min="${row.lo}" max="${row.hi}" step="${row.step}"
              value="${shown == null ? '' : shown}" placeholder="${effective == null ? 'default' : effective}" aria-label="${esc(row.label)}">`;
        const running = v.alive ? `running: <b>${row.kind === 'bool' ? (run[key] ? 'on' : 'off') : run[key]}</b>` : 'bot not running';
        return `<div class="ctrl-row${dirty ? ' is-dirty' : ''}" data-row="${key}">
          <div class="ctrl-row-head"><span class="ctrl-label">${esc(row.label)}</span>${row.unit ? `<span class="ctrl-unit">${esc(row.unit)}</span>` : ''}</div>
          ${input}
          <span class="ctrl-running">${running}${key in st ? ` · default ${row.kind === 'bool' ? (def[key] ? 'on' : 'off') : def[key]}` : ''}</span>
          <span class="ctrl-help">${esc(row.help)} Range ${row.lo}–${row.hi}.</span>
        </div>`;
      }).join('')}</div></details>`;
  }).join('');
  const dirty = Object.keys(CTRL.edits).length > 0;
  $('#ctrl-apply').disabled = !dirty;
  $('#ctrl-revert').disabled = !dirty;
  const bits = [];
  const lim = (v.running || v.defaults || {}).daily_loss_stop_usdc;
  if (v.today != null) {
    bits.push(`Today (${TZ_LABEL}): ${fmt.signed(v.today)} settled against a ${fmt.money(lim)} daily loss limit`
      + (v.loss_reset_at ? ` (count reset ${fmt.clock(v.loss_reset_at * 1000)})` : '') + '.');
  }
  if (v.halted) bits.push(`Bot halted: ${v.halted}.`);
  if (v.kill) bits.push(`Halted: bids are cancelled and nothing new is sent while ${DDIR}/KILL exists.`);
  if (v.applied_at) bits.push(`The bot last applied (${fmt.clock(v.applied_at * 1000)} ${TZ_LABEL}): ${(v.applied || []).join('; ')}.`);
  if (v.notes && v.notes.length) bits.push(`It reported: ${v.notes.join('; ')}.`);
  bits.push(paired ? 'Pair parameter changes are saved for all six markets and apply within about two seconds.'
    : 'Balance and spot exchanges are start-time arguments (ecosystem.config.js).');
  $('#ctrl-foot').textContent = bits.join(' ');
}

function askKey(message = '') {
  return new Promise((resolve) => {
    const dlg = $('#dlg-key');
    $('#key-msg').textContent = message;
    $('#key-msg').classList.toggle('neg', !!message);
    $('#key-input').value = '';
    const done = (ok) => { $('#f-key').onsubmit = null; $('#key-cancel').onclick = null; dlg.close(); resolve(ok); };
    $('#f-key').onsubmit = (ev) => {
      ev.preventDefault();
      CTRL.key = $('#key-input').value.trim();
      store('xshark-key', CTRL.key);
      done(true);
    };
    $('#key-cancel').onclick = () => done(false);
    dlg.showModal();
  });
}

/* POST with the write key; on 401 ask for it and try again */
async function postKeyed(url, body) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const r = await fetch(withAsset(url), { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Console-Key': CTRL.key || '' },
      body: JSON.stringify(body || {}) });
    if (r.status === 401) {
      const ok = await askKey(CTRL.key ? 'That key was not accepted.' : '');
      if (!ok) return null;
      continue;
    }
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }
  return null;
}

/* the paper bot: archive this run and start again with new cash (the bot does it and pm2 restarts it) */
async function restartWith(inputSel, statusFn) {
  const bal = Number($(inputSel).value);
  if (!Number.isFinite(bal) || bal < 1) { statusFn('Enter the cash, $1 or more.', 'warn'); return; }
  if (!confirm(`Restart the paper bot with $${bal.toFixed(2)}?\n\nThis run's fills, windows and charts are archived `
    + '(data/paper-archive), the settings stay, and a position open in this window is dropped. '
    + 'The bot is back within about 10 seconds.')) return;
  try {
    const d = await postKeyed('/api/restart', { balance: bal });
    if (!d) { statusFn('Not sent: no write key.', 'warn'); return; }
    statusFn(`Restarting with $${bal.toFixed(2)}: back within about 10 seconds.`, 'ok');
    setTimeout(() => { pollLive(); loadControls(true); }, 12000);
  } catch (e) {
    statusFn(`Failed: ${e.message}`, 'error');
  }
}

async function ctrlAction(url, body, okMsg) {
  try {
    const d = await postKeyed(url, body);
    if (!d) { ctrlStatus('Not saved: no write key.', 'warn'); return; }
    CTRL.edits = {};
    CTRL.view = d;
    renderControls();
    const notes = d.notes_saved || [];
    ctrlStatus(notes.length ? `${okMsg} Adjusted: ${notes.join('; ')}` : okMsg, notes.length ? 'warn' : 'ok');
    setTimeout(() => loadControls(true), 2500);
  } catch (e) {
    ctrlStatus(`Failed: ${e.message}`, 'error');
  }
}

function wireControls() {
  $('#ctrl-groups').addEventListener('input', (ev) => {
    const inp = ev.target.closest('input[data-key]');
    if (!inp) return;
    const key = inp.dataset.key;
    if (inp.dataset.kind === 'bool') CTRL.edits[key] = inp.checked;
    else if (inp.value === '') delete CTRL.edits[key];
    else CTRL.edits[key] = Number(inp.value);
    inp.closest('.ctrl-row').classList.toggle('is-dirty', key in CTRL.edits);
    $('#ctrl-apply').disabled = !Object.keys(CTRL.edits).length;
    $('#ctrl-revert').disabled = !Object.keys(CTRL.edits).length;
  });
  $('#ctrl-apply').addEventListener('click', () => ctrlAction('/api/controls', { values: CTRL.edits }, 'Saved. The bot applies it within about two seconds.'));
  $('#ctrl-revert').addEventListener('click', () => { CTRL.edits = {}; renderControls(); ctrlStatus('Edits discarded.'); });
  $('#ctrl-restart').addEventListener('click', () => restartWith('#ctrl-restart-cash', ctrlStatus));
  $('#hero-restart-btn').addEventListener('click', () => restartWith('#hero-restart-cash', (msg) => {
    $('#hero-restart-msg').textContent = msg;
  }));
  $('#ctrl-reset').addEventListener('click', () => {
    if (!confirm('Clear every override and go back to the defaults?')) return;
    ctrlAction('/api/controls/reset', {}, 'Back to the defaults.');
  });
  $('#ctrl-kill').addEventListener('click', () => {
    const on = !(CTRL.view && CTRL.view.kill);
    ctrlAction('/api/kill', { on }, on ? 'Halted: bids come off, nothing new is sent. Held shares still settle.' : 'Resumed.');
  });
  $('#ctrl-key').addEventListener('click', async () => { if (await askKey()) ctrlStatus('Key saved in this browser.'); });
  $('#ctrl-loss-reset').addEventListener('click', () => {
    if (!confirm("Start today's loss count from zero now? A daily loss stop that has halted the bot lifts within ~2 s.")) return;
    ctrlAction('/api/loss/reset', {}, "Today's loss count starts from zero.");
  });
}

/* ═══════════════════════════════ page router ══════════════════════════════
   Pages switch on the hash; only the open page does per-tick work, and a page
   refreshes on arrival so nothing is stale when it is opened. */

const PAGES = ['live', 'markets', 'windows', 'fills', 'earnings', 'variables'];

function pageFromHash() {
  const h = (location.hash || '').replace(/^#/, '').split('?')[0];
  return PAGES.includes(h) ? h : 'live';
}

function renderPage(force = false) {
  if (state.page === 'live') renderLive(force);
  if (state.page === 'markets') renderMarkets();
  if (state.page === 'windows' && state.win.data) renderWindows();
  if (state.page === 'fills' && state.fills) renderFq();
  if (state.page === 'earnings' && state.earn.data) renderEarnings();
}

function showPage(name) {
  state.page = PAGES.includes(name) ? name : 'live';
  $$('.page').forEach((p) => { p.hidden = p.dataset.page !== state.page; });
  $$('.tab').forEach((a) => {
    const on = a.dataset.page === state.page;
    a.classList.toggle('is-on', on);
    a.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  store('xshark-page', state.page);
  notice('');
  if (state.page === 'live') { pollLive(); renderLive(true); }
  if (state.page === 'markets') { initMarketCards(); pollMarkets(); }
  if (state.page === 'windows') {
    loadWindows();
    const want = new URLSearchParams(location.search).get('window');
    if (want && state.win.open !== want) openWindow(want);
  }
  if (state.page === 'fills') loadFills();
  if (state.page === 'earnings') loadEarnings();
  if (state.page === 'variables') loadControls(true);
}

/* ═══════════════════════════════ wire up ════════════════════════════════ */

let resizeTimer;
addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => renderPage(true), 140); });
addEventListener('hashchange', () => showPage(pageFromHash()));

$('#win-table').addEventListener('click', (ev) => {
  const tr = ev.target.closest('tr[data-slug]');
  if (tr) openWindow(tr.dataset.slug);
});
$('#win-table').addEventListener('keydown', (ev) => {
  const tr = ev.target.closest('tr[data-slug]');
  if (tr && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); openWindow(tr.dataset.slug); }
});
$('#trig-apply').addEventListener('click', applyTrigger);
$('#wd-close').addEventListener('click', () => { $('#win-detail').hidden = true; state.win.open = null; $$('#win-table tbody tr').forEach((tr) => tr.classList.remove('is-open')); });
$$('#pw-range .seg-btn').forEach((b) => b.addEventListener('click', () => {
  PW.n = Number(b.dataset.n);
  store('xshark-pw', String(PW.n));
  if (state.fills) renderPerWindow(state.fills.per_window || []);
}));
$$('#earn-period .seg-btn').forEach((b) => b.addEventListener('click', () => {
  state.earn.period = b.dataset.period;
  store('xshark-earn', state.earn.period);
  loadEarnings();
}));

/* ?print=<slug>: one window's card alone, nothing polled -- what scripts/save_charts.py photographs */
const PRINT = new URLSearchParams(location.search).get('print');
document.addEventListener('click', (ev) => {
  const b = ev.target.closest && ev.target.closest('.sell-btn');
  if (b) sellSide(b);
});

initTheme();
initMarketCards();
if (PRINT) {
  document.body.classList.add('print-mode');
  $$('.page').forEach((p) => { p.hidden = p.dataset.page !== 'windows'; });
  pollLive().finally(() => openWindow(PRINT)).finally(() => {
    setTimeout(() => { document.title = 'ready'; }, 300);
  });
} else {
  wireControls();
  loadCoins();
  setInterval(loadCoins, 30000);
  showPage(location.hash ? pageFromHash() : stored('xshark-page', 'live'));
  if (state.page !== 'live') pollLive();          // the header badge, on every page, right away

  /* cadence: the live page polls twice a second (the bot writes state at 2 Hz);
     other pages refresh their data slowly and keep the header badge current */
  setInterval(() => { if (state.page === 'live') pollLive(); }, 250);   // the bot writes its state every 250 ms
  setInterval(() => {
    if (state.page !== 'live') pollLive();
    if (state.page === 'windows') loadWindows();
    if (state.page === 'fills') loadFills();
    if (state.page === 'variables') loadControls();
  }, 10000);
  setInterval(() => { if (state.page === 'markets') pollMarkets(); }, 500);
}
