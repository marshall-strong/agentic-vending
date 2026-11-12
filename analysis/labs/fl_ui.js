(function () {
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function pd(iso) { const p = iso.slice(0, 10).split('-').map(Number); return new Date(Date.UTC(p[0], p[1] - 1, p[2])); }
const fd = (iso, dow) => { const t = pd(iso); return (dow ? WD[t.getUTCDay()] + ' ' : '') + MON[t.getUTCMonth()] + ' ' + t.getUTCDate(); };
const addDays = (iso, n) => { const t = pd(iso); t.setUTCDate(t.getUTCDate() + n); return t.toISOString().slice(0, 10); };
const ok = v => v !== null && v !== undefined && !isNaN(v);
const n1 = v => ok(v) ? (Math.round(v * 10) / 10).toFixed(1) : '–';
const n2 = v => ok(v) ? v.toFixed(2) : '–';
const n0 = v => ok(v) ? Math.round(v).toLocaleString('en-US') : '–';
const pct = v => ok(v) ? Math.round(v * 100) + '%' : '–';
const usd = d => '$' + d.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd0 = d => '$' + Math.round(d).toLocaleString('en-US');
const sgn = (v, f) => ok(v) ? (v >= 0 ? '+' : '−') + f(Math.abs(v)) : '–';
const AVG = DATA.bt.average;

/* ---------- models ---------- */
const MODELS = [
  { id: 'snaive', name: 'Seasonal naive', color: 'var(--s1)', desc: 'same weekday last week' },
  { id: 'hw', name: 'Holt-Winters additive', color: 'var(--s2)', desc: 'level + weekly season, fit by the script' },
  { id: 'sma', name: 'Simple moving average', color: 'var(--s3)', desc: 'equal weight on the last k days' },
  { id: 'wma', name: 'Weighted moving average', color: 'var(--s4)', desc: 'recent days count more' },
  { id: 'ses', name: 'Simple exponential smoothing', color: 'var(--s5)', desc: 'weights fade by (1 − α)' },
  { id: 'naive', name: 'Naive', color: 'var(--s6)', desc: 'repeat the last day' },
  { id: 'hwd', name: 'Holt-Winters damped', color: 'var(--s7)', desc: 'adds a trend that fades out' },
  { id: 'creg', name: 'Calendar regression', color: 'var(--s8)', desc: 'weekday + class flag + recent level' }
];
const MB = {}; MODELS.forEach(m => MB[m.id] = m);
const S = { sel: { snaive: true, hw: true, sma: true, wma: false, ses: false, naive: false, hwd: false, creg: false },
  k: 7, wma: 'linear', alpha: 0.3, sesFit: true, adjust: false, origin: DATA.bt.origins[DATA.bt.origins.length - 1] };
const cfg = () => ({ k: S.k, wma: S.wma, alpha: S.alpha, sesFit: S.sesFit, adjust: S.adjust });
function mLabel(id) {
  if (id === 'sma') return 'Moving average, k = ' + S.k;
  if (id === 'wma') return 'Weighted MA, ' + ({ equal: 'equal 1-1-1-1', linear: 'linear 4-3-2-1', halving: 'halving 8-4-2-1' })[S.wma];
  if (id === 'ses') return 'Exp. smoothing, ' + (S.sesFit ? 'α fit each date' : 'α = ' + S.alpha.toFixed(2));
  return MB[id].name;
}
const FWD = DATA.fc.dates[0];   // the first day after the last poll
// s3, s4 and s5 fall below 3:1 on the light surface, so their dashed forecast lines are drawn heavier.
const lineW = id => (id === 'sma' || id === 'wma' || id === 'ses') ? 2.5 : 2;
const MAX_SEL = 4;
const IHZ = DATA.fc.interval_horizon_days;   // the 80% range is backtested for this many days ahead

/* ---------- visits (door-open counter) ---------- */
// Shape and color both carry the grade, so the marks still read without color.
const VK = {
  'proven refill': { rank: 3, color: 'var(--good)', label: 'Proven refill (one of our slots sold more than a rail holds)' },
  'likely refill': { rank: 2, color: 'var(--good)', label: 'Likely refill (2+ of our empty-looking slots sold again)' },
  'possible top-up': { rank: 1, color: 'var(--warn)', label: 'Possible top-up (one of our empty-looking slots sold again)' },
  'no sign of refill': { rank: 0, color: 'var(--muted)', label: 'Door opened, no sign of refill' }
};
function markShape(kind, x, y) {
  const c = VK[kind].color, X1 = x.toFixed(1), r = 5;
  if (kind === 'proven refill') return `<path d="M${(x - 6).toFixed(1)},${(y - 5).toFixed(1)}H${(x + 6).toFixed(1)}L${X1},${(y + 6).toFixed(1)}Z" style="fill:${c};stroke:var(--surface)" stroke-width="1" pointer-events="none"/>`;
  if (kind === 'likely refill') return `<circle cx="${X1}" cy="${y}" r="${r}" style="fill:${c};stroke:var(--surface)" stroke-width="1" pointer-events="none"/>`;
  if (kind === 'possible top-up') return `<circle cx="${X1}" cy="${y}" r="${r - 0.6}" style="fill:var(--surface);stroke:${c}" stroke-width="2" pointer-events="none"/>`;
  return `<rect x="${(x - 3.5).toFixed(1)}" y="${y - 3.5}" width="7" height="7" transform="rotate(45 ${X1} ${y})" style="fill:var(--surface);stroke:${c}" stroke-width="1.6" pointer-events="none"/>`;
}
const markKey = kind => `<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" style="flex:none;overflow:visible">${markShape(kind, 7, 7)}</svg>`;
const VISITS = DATA.visits;
const VBYDAY = {};
VISITS.forEach(v => { (VBYDAY[v.day] = VBYDAY[v.day] || []).push(v); });
const topKind = list => list.reduce((a, v) => (VK[v.evidence].rank > VK[a].rank ? v.evidence : a), 'no sign of refill');
const hhmm = iso => iso.slice(11, 16);
function visitWhen(v) {
  return v.start.slice(0, 10) === v.end.slice(0, 10) ? `${fd(v.end, true)} ${hhmm(v.start)}–${hhmm(v.end)}` : `between ${fd(v.start, true)} ${hhmm(v.start)} and ${fd(v.end, true)} ${hhmm(v.end)}`;
}
function visitDetail(v) {
  const bits = [`${v.openings} door opening${v.openings === 1 ? '' : 's'}`];
  if (v.proven.length) bits.push('refill proven by slot ' + v.proven.join(', '));
  if (v.resumed.length) bits.push('empty-looking slots that sold again: ' + v.resumed.join(', '));
  if (PAIR && PAIR.visits.some(p => p.visit === v.id)) bits.push(PAIR_TEXT.replace(/\.$/, ''));
  if (v.note) bits.push(v.note.split(';')[0]);
  return bits.join('; ');
}
const RF = DATA.refill, LAST_RS = RF.last_restock;
const wkCount = (() => { const c = {}; VISITS.filter(v => VK[v.evidence].rank >= 1 && v.day >= DATA.meta.regular_from).forEach(v => { c[v.weekday] = (c[v.weekday] || 0) + 1; }); return c; })();
const wkText = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].filter(d => wkCount[d]).map(d => `${d} ${wkCount[d]}`).join(', ');
const nRefillVisits = Object.values(wkCount).reduce((a, b) => a + b, 0);
const SUP = DATA.supply, SUP_END = SUP[SUP.length - 1];
const PH = DATA.photo, PH_NEED = PH.rails - PH.stocked.length;
const PHOTO_TEXT = `The ${fd(PH.at)} photo shows only ${PH.stocked.length} of our ${PH.rails} rails visibly stocked; at that visit the counters had flagged ${PH.flagged.length} as suspected empty (${PH.conflict.length} of them visibly stocked: ${PH.conflict.join(', ')}) ${PH.missed.length ? `and missed ${PH.missed.length} that looked empty (${PH.missed.join(', ')})` : 'and missed none of the rails that looked empty'}. Product at the back of a rail can be hidden, so a rail that looks empty is not proof.`;
const PAIR = RF.later_pair, PAIR_TEXT = PAIR ? `Slots ${PAIR.slots.join(' and ')} prove a refill at ${PAIR.visits.map(v => fd(v.start)).join(' or ')} (the counters cannot say which).` : '';
const FULLW = DATA.weekly.filter(w => !w.partial && w.term_week >= 1);   // full weeks only: never compare with a partial week
const wk3Text = FULLW.slice(-3).map(w => n0(w.units)).join(' → ');
const lastFullSlots = FULLW[FULLW.length - 1].active_slots;

/* ---------- calendar helpers ---------- */
const calType = ci => CAL.cls[ci] ? 'class' : (CAL.dow[ci] < 5 ? 'break' : null);
const dayLabel = ci => { const d = CAL.dates[ci]; const i = DATES.indexOf(d); const lab = i >= 0 ? DATA.daily.label[i] : DATA.fc.label[DATA.fc.dates.indexOf(d)]; return lab || ''; };
const typeText = ci => CAL.cls[ci] ? 'class day' : (CAL.dow[ci] < 5 ? 'weekday, no classes' : 'weekend');

/* ---------- chart engine (hand-built SVG) ---------- */
function niceTicks(lo, hi, count) {
  if (!(hi > lo)) hi = lo + 1;
  const raw = (hi - lo) / count, p = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * p).find(s => s >= raw * 0.999);
  const a = Math.floor(lo / step + 1e-9) * step, b = Math.ceil(hi / step - 1e-9) * step, t = [];
  for (let v = a; v <= b + step * 1e-6; v += step) t.push(Math.round(v / step) * step);
  return t;
}
function barPath(x, yb, yv, w) {
  const h = Math.abs(yv - yb); if (h < 0.4) return '';
  const r = Math.min(4, w / 2, h);
  if (yv < yb) return `M${x},${yb}V${yv + r}Q${x},${yv} ${x + r},${yv}H${x + w - r}Q${x + w},${yv} ${x + w},${yv + r}V${yb}Z`;
  return `M${x},${yb}V${yv - r}Q${x},${yv} ${x + r},${yv}H${x + w - r}Q${x + w},${yv} ${x + w},${yv - r}V${yb}Z`;
}
function renderTip(tip, c) {
  let h = `<div class="tt">${esc(c.title)}</div>`;
  (c.rows || []).forEach(r => {
    const key = r.color ? `<i class="k${r.box ? ' box' : ''}${r.dash ? ' dash' : ''}" style="${r.box ? 'background' : 'border-top-color'}:${r.color}"></i>` : '';
    h += `<div class="tr">${key}<b>${esc(r.value)}</b><span>${esc(r.label)}</span></div>`;
  });
  if (c.note) h += `<div class="tn">${esc(c.note)}</div>`;
  tip.innerHTML = h;
}
function placeTip(tip, x, W, top) {
  tip.hidden = false;
  const tw = tip.offsetWidth;
  let left = x + 14; if (left + tw > W - 2) left = x - 14 - tw;
  tip.style.left = Math.max(2, left) + 'px'; tip.style.top = top + 'px';
}
function drawChart(box, c) {
  const svg = box.querySelector('svg'), tip = box.querySelector('.tip');
  const W = Math.max(250, Math.floor(box.clientWidth || 600));
  const Hh = c.h || (W < 560 ? 220 : 280);
  const L = c.left || 46, R = 10, T = c.top || (c.window ? 30 : 12), B = c.bottom || 24;
  const pw = W - L - R, ph = Hh - T - B, n = c.n, bw = pw / n;
  const X = i => L + (i + 0.5) * bw;
  const ticks = niceTicks(c.yMin || 0, Math.max(c.yMax, (c.yMin || 0) + 1e-9), c.yCount || 4);
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const Yp = v => T + ph - (v - y0) / (y1 - y0) * ph;
  const fmt = c.yFmt || (v => String(v));
  const yb = Yp(Math.max(y0, Math.min(0, y1)));
  let g = '';
  if (c.bands) {
    let i = 0;
    while (i < n) { const t = c.bands[i]; let j = i; while (j + 1 < n && c.bands[j + 1] === t) j++;
      if (t) g += `<rect class="band-${t}" x="${(L + i * bw).toFixed(1)}" y="${T}" width="${((j - i + 1) * bw).toFixed(1)}" height="${ph}"/>`;
      i = j + 1; }
  }
  if (c.shade) { const [a, b] = c.shade; g += `<rect x="${(L + a * bw).toFixed(1)}" y="${T}" width="${((b - a + 1) * bw).toFixed(1)}" height="${ph}" style="fill:var(--sunk);opacity:.85"/>`; }
  ticks.forEach(v => { const yy = Yp(v).toFixed(1); g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/><text class="ax num" x="${L - 6}" y="${(+yy + 4).toFixed(1)}" text-anchor="end">${esc(fmt(v))}</text>`; });
  if (c.window) {
    const [a, b] = c.window, x0 = L + a * bw;
    g += `<rect class="win" x="${x0.toFixed(1)}" y="${T - 8}" width="${((b - a + 1) * bw).toFixed(1)}" height="4" rx="2"/>`;
    g += `<text class="lab" x="${(L + (b + 1) * bw - 2).toFixed(1)}" y="${T - 14}" text-anchor="end">${esc(c.windowLabel || '')}</text>`;
    g += `<line class="vl" x1="${x0.toFixed(1)}" x2="${x0.toFixed(1)}" y1="${T - 8}" y2="${T + ph}"/>`;
  }
  (c.xTicks || []).forEach(([i, lab]) => { g += `<text class="ax" x="${X(i).toFixed(1)}" y="${Hh - 6}" text-anchor="middle">${esc(lab)}</text>`; });
  g += `<line class="base" x1="${L}" x2="${W - R}" y1="${yb.toFixed(1)}" y2="${yb.toFixed(1)}"/>`;
  (c.areas || []).forEach(a => {
    let i = 0;
    while (i < n) {
      if (!ok(a.lo[i]) || !ok(a.hi[i])) { i++; continue; }
      let j = i; while (j + 1 < n && ok(a.lo[j + 1]) && ok(a.hi[j + 1])) j++;
      let d = '';
      for (let t = i; t <= j; t++) d += (t === i ? 'M' : 'L') + X(t).toFixed(1) + ' ' + Yp(a.hi[t]).toFixed(1);
      for (let t = j; t >= i; t--) d += 'L' + X(t).toFixed(1) + ' ' + Yp(a.lo[t]).toFixed(1);
      g += `<path d="${d}Z" style="fill:${a.color};opacity:.16"/>`;
      i = j + 1;
    }
  });
  (c.bars || []).forEach(b => {
    const w = Math.max(1.5, Math.min(24, bw - 2));
    b.y.forEach((v, i) => { if (!ok(v)) return; const d = barPath(X(i) - w / 2, yb, Yp(v), w); if (!d) return;
      const op = b.opacity ? b.opacity(i) : 1, ol = b.outline && b.outline(i);
      g += `<path class="bar" data-i="${i}" d="${d}" style="fill:${b.color};${op < 1 ? 'opacity:' + op : ''}"/>`;
      if (ol) g += `<path d="${d}" fill="none" style="stroke:var(--ink)" stroke-width="1.2" stroke-dasharray="3 2" pointer-events="none"/>`; });
  });
  (c.lines || []).forEach(s => {
    let d = '', pen = false;
    s.y.forEach((v, i) => { if (!ok(v)) { pen = false; return; } d += (pen ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Yp(v).toFixed(1) + ' '; pen = true; });
    if (d) g += `<path d="${d}" fill="none" style="stroke:${s.color}" stroke-width="${s.w || 2}" ${s.dash ? 'stroke-dasharray="6 4"' : ''} stroke-linejoin="round" stroke-linecap="round"/>`;
  });
  (c.hlines || []).forEach(h => {
    const yy = Yp(h.v).toFixed(1);
    g += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" style="stroke:${h.color}" stroke-width="2" stroke-dasharray="6 4"/>`;
    g += `<text class="lab" x="${W - R - 4}" y="${(+yy + (h.below ? 14 : -6)).toFixed(1)}" text-anchor="end">${esc(h.label)}</text>`;
  });
  (c.dots || []).forEach(d => d.pts.forEach(p => {
    if (!ok(p.v)) return;
    g += d.hollow ? `<circle cx="${X(p.i).toFixed(1)}" cy="${Yp(p.v).toFixed(1)}" r="4.5" style="fill:var(--surface);stroke:${d.color}" stroke-width="2"/>`
      : `<circle cx="${X(p.i).toFixed(1)}" cy="${Yp(p.v).toFixed(1)}" r="4.5" style="fill:${d.color};stroke:var(--surface)" stroke-width="2"/>`;
  }));
  (c.labels || []).forEach(l => { g += `<text class="lab" x="${X(l.i).toFixed(1)}" y="${(Yp(l.v) + (l.dy || -8)).toFixed(1)}" text-anchor="${l.anchor || 'middle'}">${esc(l.text)}</text>`; });
  (c.marks || []).forEach(m => {
    const x = X(m.i);
    if (m.guide) g += `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${T}" y2="${T + ph}" style="stroke:${VK[m.kind].color}" stroke-width="1.2" stroke-dasharray="3 3" opacity=".75" pointer-events="none"/>`;
    g += markShape(m.kind, x, T - 10);
  });
  (c.vtext || []).forEach(t => {
    // one line to the right of the guide if it fits, else the words stacked to the right (t.wrap), else one line to the left
    const room = W - R - X(t.i) - 5, fits = t.text.length * 6.2 <= room;
    const words = t.wrap && !fits ? t.text.split(' ') : null, stack = words && Math.max(...words.map(w => w.length)) * 6.2 <= room;
    if (stack) g += `<text class="lab" x="${(X(t.i) + 5).toFixed(1)}" y="${T + 12}" text-anchor="start">${words.map((w, k) => `<tspan x="${(X(t.i) + 5).toFixed(1)}" dy="${k ? 12 : 0}">${esc(w)}</tspan>`).join('')}</text>`;
    else g += `<text class="lab" x="${(X(t.i) + (fits ? 5 : -5)).toFixed(1)}" y="${T + 12}" text-anchor="${fits ? 'start' : 'end'}">${esc(t.text)}</text>`;
  });
  g += `<line class="cursor" x1="0" x2="0" y1="${T}" y2="${T + ph}"/>`;
  if (c.mode === 'bars') for (let i = 0; i < n; i++) g += `<rect class="hitbar" data-i="${i}" x="${(L + i * bw).toFixed(1)}" y="${T}" width="${bw.toFixed(1)}" height="${ph}" fill="transparent" tabindex="0" aria-label="${esc(c.barLabel ? c.barLabel(i) : '')}"/>`;
  svg.setAttribute('viewBox', `0 0 ${W} ${Hh}`);
  svg.innerHTML = g;
  if (!c.hover || !tip) return;
  const cur = svg.querySelector('.cursor');
  let idx = -1;
  const hide = () => { tip.hidden = true; cur.style.opacity = 0; svg.querySelectorAll('.bar.on').forEach(b => b.classList.remove('on')); };
  const show = i => {
    idx = i; const content = c.hover(i); if (!content) { hide(); return; }
    renderTip(tip, content);
    if (c.mode === 'bars') { svg.querySelectorAll('.bar').forEach(b => b.classList.toggle('on', +b.dataset.i === i)); }
    else { cur.setAttribute('x1', X(i)); cur.setAttribute('x2', X(i)); cur.style.opacity = 0.5; }
    const r = svg.getBoundingClientRect(), scale = r.width ? r.width / W : 1;
    placeTip(tip, X(i) * scale, r.width || W, T * scale);
  };
  if (c.mode === 'bars') {
    svg.onpointermove = null; svg.onfocus = null; svg.onkeydown = null;
    svg.querySelectorAll('.hitbar').forEach(h => {
      const i = +h.dataset.i;
      h.addEventListener('pointerenter', () => show(i));
      h.addEventListener('focus', () => show(i));
      h.addEventListener('blur', hide);
    });
    svg.onpointerleave = hide;
    return;
  }
  svg.onpointermove = ev => {
    const r = svg.getBoundingClientRect(); const cx = (ev.clientX - r.left) * W / r.width;
    if (cx < L - 4 || cx > W - R + 4) return;
    show(Math.max(0, Math.min(n - 1, Math.floor((cx - L) / bw))));
  };
  svg.onpointerleave = hide;
  svg.onfocus = () => show(idx >= 0 ? idx : n - 1);
  svg.onblur = hide;
  svg.onkeydown = ev => {
    if (ev.key === 'ArrowLeft') { show(Math.max(0, idx - 1)); ev.preventDefault(); }
    else if (ev.key === 'ArrowRight') { show(Math.min(n - 1, idx + 1)); ev.preventDefault(); }
    else if (ev.key === 'Home') { show(0); ev.preventDefault(); }
    else if (ev.key === 'End') { show(n - 1); ev.preventDefault(); }
    else if (ev.key === 'Escape') hide();
  };
}
const box = id => document.getElementById(id).closest('.chartbox');
function dayTicks(dates, W, every) {
  const step = every || (W < 560 ? 21 : 14), t = [];
  let last = -999;
  dates.forEach((d, i) => { if (pd(d).getUTCDay() === 6 && i - last >= step) { t.push([i, fd(d)]); last = i; } });
  return t;
}
const widthOf = id => Math.floor(box(id).clientWidth || 600);

/* ---------- step navigation ---------- */
const KEY = 'vending-forecast-lab-step';
let current = 1;
function go(n, focusTab) {
  n = Math.max(1, Math.min(6, +n || 1));
  current = n;
  $$('[role=tab]').forEach(b => { const on = +b.dataset.step === n; b.setAttribute('aria-selected', on ? 'true' : 'false'); b.tabIndex = on ? 0 : -1; if (on && focusTab) b.focus(); });
  $$('section.panel').forEach(p => { p.hidden = +p.dataset.panel !== n; });
  try { localStorage.setItem(KEY, String(n)); } catch (e) { /* storage blocked */ }
  try { if (history.replaceState) history.replaceState(null, '', '#step' + n); } catch (e) { /* sandboxed */ }
  render(n);
}
$$('[role=tab]').forEach(b => {
  b.addEventListener('click', () => { go(b.dataset.step); window.scrollTo({ top: 0 }); });
  b.addEventListener('keydown', ev => {
    let n = null;
    if (ev.key === 'ArrowRight') n = current % 6 + 1;
    else if (ev.key === 'ArrowLeft') n = (current + 4) % 6 + 1;
    else if (ev.key === 'Home') n = 1; else if (ev.key === 'End') n = 6;
    if (n) { ev.preventDefault(); go(n, true); }
  });
});
$$('[data-go]').forEach(b => b.addEventListener('click', () => { go(b.dataset.go); window.scrollTo({ top: 0 }); }));
function render(n) { [null, drawStep1, drawStep2, drawStep3, drawStep4, drawStep5, drawStep6][n](); }

/* ---------- STEP 1 ---------- */
const lastFullWeek = FULLW[FULLW.length - 1];   // 'current' numbers quote the last full Saturday-to-Friday week
const peakWeek = FULLW.reduce((a, w) => (w.units > a.units ? w : a), FULLW[0]);
const peakSlots = FULLW.reduce((a, w) => Math.max(a, w.active_slots), 0);
const RANK = Object.keys(AVG).sort((a, b) => AVG[a].mae - AVG[b].mae);
const WIN = RANK[0];
const SHORT = { [HW_NAME]: 'Holt-Winters additive', [HWD_NAME]: 'Holt-Winters damped', [SN_NAME]: 'Seasonal naive', [CREG_NAME]: 'Calendar regression' };
const shortName = m => SHORT[m] || m;
const AFTER_RS = VISITS.filter(v => v.end > LAST_RS.end);
const afterText = AFTER_RS.map(v => `${fd(v.end, true)} (${/^student/i.test(v.note) ? 'the student team, not a refill' : v.evidence})`).join(', ');
function drawStep1() {
  const t = DATA.meta.totals;
  $('#p1Lede').textContent = `The team will restock the machine and its agent sets the plan for each week. To do that well it needs a number for each day, plus a sense of how far off that number usually is. This lab builds that forecast from the machine's own counters and tests it the honest way. One warning first: the operators stopped regular refilling to prepare for the project. The last refill proven at a single visit was ${fd(LAST_RS.start, true)}; later visits put product back in only some rails, so the last weeks show an emptying machine, not fading demand. Charts here are whole-machine totals (the pre-competition history, when course staff stocked every slot); slot counts and the restocked scenario cover {{OUR}} only.`;
  $('#p1Plan').innerHTML = `<b>Every test here stands at a past Saturday</b> and hides every day after it. The hidden days are used only to score the forecast, never to make it. Saturday is the fixed weekly plan date, the day a ledger week starts: the door counter shows the ${nRefillVisits} visits with refill evidence since hourly polling began (Sep 21) fell on ${esc(wkText)}.`;
  $('#p1Zero').innerHTML = `<b>A zero is not always a lack of demand.</b> A slot that sold out also sells zero, and after ${esc(fd(LAST_RS.start))} refills were partial at most. By the last poll ${SUP_END.slots_sold_out_not_refilled + SUP_END.slots_silent_since_before_last_restock} of {{OUR}} were suspected empty. ${esc(PHOTO_TEXT)} When the whole machine slows, ask how many slots still had stock before you blame the drinks.`;
  const items = [
    ['Drinks sold, whole machine', n0(t.units), fd(DATA.meta.first_poll) + ' to ' + fd(DATA.meta.last_poll)],
    ['Takings, tax included', usd(t.cents / 100), 'whole machine ({{OUR}}: ' + usd(DATA.meta.ours.cents / 100) + ')'],
    ['Polls of the counters', n0(DATA.meta.polls), 'hourly from ' + fd(DATA.meta.regular_from)],
    ['Lowest of the script\'s models, average miss', n2(AVG[WIN].mae) + '/day', shortName(WIN), true],
    ['Seasonal naive, average miss', n2(AVG[SN_NAME].mae) + '/day', 'the bar every model must beat'],
    ['Last full week, whole machine', n0(lastFullWeek.units) + ' drinks', 'Sat ' + fd(lastFullWeek.week) + ' to Fri ' + fd(addDays(lastFullWeek.week, 6)) + '; peak ' + n0(peakWeek.units)]
  ];
  $('#p1Kpis').innerHTML = items.map(([l, v, s, hl]) => `<div class="kpi${hl ? ' hl' : ''}"><div class="v">${esc(v)}</div><div class="l">${esc(l)} · ${esc(s)}</div></div>`).join('');
  const iv = DATA.bt.intervals[WIN], nOr = DATA.bt.origins.length;
  const adj = { k: 7, wma: 'linear', alpha: 0.3, sesFit: true, adjust: true };
  const smaAdj = backtest('sma', adj, {}).mae, hwAdj = backtest('hw', adj, {}).mae;
  $('#p1Head').innerHTML = `Over ${nOr} Saturday plan dates (${esc(fd(DATA.bt.origins[0]))} to ${esc(fd(DATA.bt.origins[nOr - 1]))}), each forecasting 7 days ahead, the lowest miss among the analysis script's models was <b>${esc(shortName(WIN))}, ${n2(AVG[WIN].mae)} drinks a day</b> on average. Seasonal naive, which just copies last week, missed by ${n2(AVG[SN_NAME].mae)}. That is a near tie. With the calendar adjustment from step 4, a 7-day moving average scores ${n2(smaAdj)} and Holt-Winters ${n2(hwAdj)}: with ${nOr} windows, the leaders are within noise of each other. ${Object.keys(AVG).filter(m => AVG[m].bias < 0).length} of ${Object.keys(AVG).length} script models ran high on average (negative bias), which fits a machine that was emptying out. <b>How sure:</b> on a class day the actual landed between ${n1(Math.abs(iv.q10_class))} below and ${n1(iv.q90_class)} above the ${esc(shortName(WIN))} forecast in 8 of 10 backtest days (in-sample coverage ${pct(iv.in_sample_coverage)}).`;
  const sn = AVG[SN_NAME].mae, mx = Math.max(...RANK.map(m => AVG[m].mae)) * 1.05;
  const host = $('#p1Bars'), tip = $('#p1BarsTip');
  host.innerHTML = RANK.map((m, i) => {
    const a = AVG[m], beats = a.mae < sn, isSn = m === SN_NAME;
    return `<div class="hbar" role="listitem" tabindex="0" data-m="${i}"><span class="nm">${esc(m)}</span><span class="tk"><i class="${beats || isSn ? '' : 'other'}" style="width:${(a.mae / mx * 100).toFixed(1)}%;${isSn ? 'background:var(--ink)' : ''}"></i><span class="ref" style="left:${(sn / mx * 100).toFixed(1)}%"></span></span><span class="v">${n2(a.mae)}</span></div>`;
  }).join('');
  host.querySelectorAll('.hbar').forEach(el => {
    const m = RANK[+el.dataset.m], a = AVG[m];
    const show = () => {
      renderTip(tip, { title: m, rows: [{ value: n2(a.mae), label: 'MAE, drinks/day' }, { value: n2(a.rmse), label: 'RMSE' }, { value: pct(a.wape), label: 'WAPE' }, { value: sgn(a.bias, n2), label: a.bias < 0 ? 'bias: ran high' : 'bias: ran low' }] });
      const r = host.getBoundingClientRect(), er = el.getBoundingClientRect();
      tip.hidden = false; tip.style.left = Math.max(2, r.width - tip.offsetWidth - 60) + 'px'; tip.style.top = (er.bottom - r.top + 4) + 'px';
    };
    el.addEventListener('pointerenter', show); el.addEventListener('focus', show);
    el.addEventListener('pointerleave', () => tip.hidden = true); el.addEventListener('blur', () => tip.hidden = true);
  });
  const beat = RANK.filter(m => AVG[m].mae < sn).length;
  $('#p1BarsNote').textContent = `MAE in drinks per day, averaged over the ${nOr} plan dates. ${beat} of ${RANK.length - 1} models beat seasonal naive. Holt-Winters and calendar regression come from the analysis script; step 5 reruns the rest live.`;
  const wk = DATA.weekly.filter(w => w.days > 1 || w.units > 0);
  const W = widthOf('p1Week');
  const isEst = w => w.gap_hours >= 24; // same 24-hour gap threshold as build_forecast_data.py
  drawChart(box('p1Week'), {
    n: wk.length, yMax: Math.max(...wk.map(w => w.units)), mode: 'bars', h: W < 560 ? 210 : 250,
    bars: [{ y: wk.map(w => w.units), color: 'var(--s1)', opacity: i => wk[i].partial ? 0.4 : 1, outline: i => isEst(wk[i]) }],
    xTicks: wk.map((w, i) => [i, fd(w.week)]).filter((_, i) => W >= 560 || i % 2 === 0),
    barLabel: i => `Week of ${fd(wk[i].week)}: ${n1(wk[i].units)} drinks${isEst(wk[i]) ? ', includes a spread estimate' : ''}`,
    hover: i => { const w = wk[i]; return { title: 'Week of ' + fd(w.week, true) + (w.partial ? ' (partial, ' + w.days + ' days)' : ''),
      rows: [{ value: n1(w.units), label: 'drinks (gaps spread)', color: 'var(--s1)', box: true }, { value: usd0(w.cents_spread / 100), label: 'takings (gaps spread)' }, { value: n0(w.units_raw), label: 'drinks as polled' }, { value: usd0(w.cents / 100), label: 'takings as polled' }, { value: String(w.class_days), label: 'class days' }, { value: String(w.active_slots), label: 'of {{OUR}} sold' }],
      note: isEst(w) ? `${w.gap_hours} hour${w.gap_hours === 1 ? '' : 's'} of this week were not polled; the spread figures are estimates.` : '' }; }
  });
  const estW = wk.filter(isEst);
  const cp = DATA.bt.partial;
  $('#p1WeekNote').textContent = (estW.length ? `Outlined bars (${fd(estW[0].week)} to ${fd(estW[estW.length - 1].week)}) hold polling gaps: their drinks are spread back over the gap by an assumed profile, so treat them as estimates. ` : '') + `Bars are whole-machine drinks per Saturday-to-Friday week (the ledger week). Our slots that sold fell from ${peakSlots} in the best full week to ${lastFullSlots} in the last full week (week of ${fd(lastFullWeek.week)}). The faded bar is the partial week from ${fd(cp.week)}: ${cp.days_observed} days so far, ${n0(cp.units_observed)} drinks, about ${n1(cp.scaled_full_week)} scaled to a full class week; compare full weeks only.`;
}

/* ---------- STEP 2 ---------- */
const REG_I = DATES.indexOf(DATA.meta.regular_from);
function p2Series() {
  const cash = $('#p2mCash').checked, spread = $('#p2Spread').checked;
  const dd = DATA.daily;
  const y = cash ? (spread ? dd.cents : dd.cents_raw).map(v => v / 100) : (spread ? dd.units : dd.units_raw).slice();
  return { y, cash, spread };
}
function drawStep2() {
  const { y, cash, spread } = p2Series();
  const stlOk = !cash && spread;
  const winSel = $('#p2Win');
  winSel.querySelector('option[value=stl]').disabled = !stlOk;
  if (!stlOk && winSel.value === 'stl') winSel.value = '7';
  const win = winSel.value;
  let dec;
  if (win === 'stl') {
    const D = DATA.decomp, map = i => { const j = D.date.indexOf(DATES[i]); return j; };
    dec = { trend: y.map((_, i) => { const j = map(i); return j >= 0 ? D.trend[j] : null; }),
      seasonal: y.map((_, i) => { const j = map(i); return j >= 0 ? D.seasonal[j] : null; }),
      remainder: y.map((_, i) => { const j = map(i); return j >= 0 ? D.remainder[j] : null; }) };
  } else dec = decompose(y, +win);
  const fmtV = cash ? (v => usd0(v)) : (v => n1(v));
  const fmtAx = cash ? (v => '$' + v) : (v => String(v));
  const on = { trend: $('#p2Trend').checked, season: $('#p2Season').checked, rem: $('#p2Rem').checked, empty: $('#p2Empty').checked, cal: $('#p2Cal').checked, visits: $('#p2Visits').checked };
  const W = widthOf('p2Main'), n = y.length;
  const bands = on.cal ? DATES.map((_, i) => calType(i)) : null;
  const ticks = dayTicks(DATES, W);
  const dd = DATA.daily;
  const gapOp = i => dd.gap_day[i] && spread ? 0.45 : 1;
  const meas = cash ? 'takings' : 'drinks';
  const hoverAll = i => {
    const rows = [{ value: fmtV(y[i]), label: meas, color: 'var(--s1)', box: true }];
    if (on.trend) rows.push({ value: ok(dec.trend[i]) ? fmtV(dec.trend[i]) : 'n/a', label: 'trend', color: 'var(--s2)' });
    if (on.season) rows.push({ value: ok(dec.seasonal[i]) ? sgn(dec.seasonal[i], fmtV) : 'n/a', label: 'weekly season', color: 'var(--s3)', box: true });
    if (on.rem) rows.push({ value: ok(dec.remainder[i]) ? sgn(dec.remainder[i], fmtV) : 'n/a', label: 'remainder', color: 'var(--s5)', box: true });
    if (on.empty) rows.push({ value: String(dd.empty_slots[i]), label: 'of our slots suspected empty', color: 'var(--s7)' });
    if (on.visits) (VBYDAY[DATES[i]] || []).forEach(v => rows.push({ value: hhmm(v.start) + '–' + hhmm(v.end), label: VK[v.evidence].label.split(' (')[0].toLowerCase() }));
    const notes = [typeText(i)]; if (dayLabel(i)) notes.push(dayLabel(i));
    if (on.visits) (VBYDAY[DATES[i]] || []).forEach(v => notes.push('Visit ' + visitWhen(v) + ': ' + visitDetail(v)));
    if (dd.gap_day[i]) notes.push(spread ? 'inside a polling gap: spread estimate' : 'inside a polling gap: sales land on the poll that ended it');
    return { title: fd(DATES[i], true), rows, note: notes.join(' · ') };
  };
  const marks = on.visits ? Object.keys(VBYDAY).map(d => { const i = DATES.indexOf(d), k = topKind(VBYDAY[d]); return i < 0 ? null : { i, kind: k, guide: VK[k].rank >= 2 }; }).filter(Boolean) : [];
  const lrI = DATES.indexOf(LAST_RS.start.slice(0, 10));
  drawChart(box('p2Main'), {
    n, yMax: Math.max(...y), bands, xTicks: ticks, yFmt: fmtAx, mode: 'cross', top: on.visits ? 24 : 12,
    marks, vtext: on.visits && lrI >= 0 ? [{ i: lrI, text: W < 520 ? 'Last proven refill' : 'Last proven refill, ' + fd(LAST_RS.start), wrap: W < 520 }] : [],
    bars: [{ y, color: 'var(--s1)', opacity: gapOp }],
    lines: on.trend ? [{ y: dec.trend, color: 'var(--s2)' }] : [],
    hover: hoverAll
  });
  const leg = [`<span><i class="k box" style="background:var(--s1)"></i>Daily ${meas}</span>`];
  if (spread) leg.push(`<span><i class="k box" style="background:var(--s1);opacity:.45"></i>Inside a polling gap (spread estimate)</span>`);
  if (on.trend) leg.push(`<span><i class="k" style="border-top-color:var(--s2)"></i>Trend, ${win === 'stl' ? 'STL' : win + '-day centered average'}</span>`);
  if (on.cal) leg.push(`<span><i class="k box" style="background:var(--class)"></i>Class day</span><span><i class="k box" style="background:var(--break)"></i>Weekday, no classes</span>`);
  $('#p2Legend').innerHTML = leg.join('');
  $('#p2VisitLegend').hidden = !on.visits;
  if (on.visits) $('#p2VisitLegend').innerHTML = Object.keys(VK).map(k => `<span>${markKey(k)}${esc(VK[k].label)}</span>`).join('') + `<span><i class="k dash" style="border-top-color:var(--good)"></i>Day of a proven or likely refill</span>`;
  const sub = (id, boxId, show, conf) => { $(boxId).hidden = !show; if (show) drawChart(box(id), Object.assign({ n, bands, xTicks: ticks, yFmt: fmtAx, h: W < 560 ? 130 : 150, mode: 'cross', hover: hoverAll }, conf)); };
  const sv = dec.seasonal.filter(ok), rv = dec.remainder.filter(ok);
  sub('p2SeasonC', '#p2SeasonBox', on.season, { yMin: Math.min(0, ...sv), yMax: Math.max(0, ...sv), bars: [{ y: dec.seasonal, color: 'var(--s3)' }] });
  sub('p2RemC', '#p2RemBox', on.rem, { yMin: Math.min(0, ...rv), yMax: Math.max(0, ...rv), bars: [{ y: dec.remainder, color: 'var(--s5)' }] });
  sub('p2EmptyC', '#p2EmptyBox', on.empty, { yMax: Math.max(...dd.empty_slots), lines: [{ y: dd.empty_slots, color: 'var(--s7)' }] });
  const gapDays = dd.gap_day.reduce((s, v) => s + v, 0), lump = dd.lump_units.reduce((s, v) => s + v, 0);
  $('#p2Note').textContent = `${gapDays} days before ${fd(DATA.meta.regular_from)} sit inside polling gaps of a day or more. ${n0(lump)} drinks arrived as lumps on the poll that closed a gap (${pct(lump / DATA.meta.totals.units)} of all drinks). A gap is missing data, not zero sales.` + (on.visits ? ` Visits come from the machine's door-open counter: ${VISITS.length} visits (${sum(VISITS.map(v => v.openings))} openings; rises within 3 hours are merged). Each mark sits on the day of the visit's last door rise; polls before ${fd(DATA.meta.regular_from)} were sparse, so early visits are only known to fall between two polls. The grade comes from sales after the visit: a slot that sells more than 11, the most any rail holds, proves a refill; empty-looking slots that start selling again suggest one.` : '');
  // KPIs
  const tr = dec.trend.map((v, i) => [v, i]).filter(p => ok(p[0]));
  const regTr = tr.filter(p => p[1] >= REG_I);
  const st = strengths(dec.trend, dec.seasonal, dec.remainder);
  const seasVals = []; for (let d = 0; d < 7; d++) { const i = dd.dow.indexOf(d); if (ok(dec.seasonal[i])) seasVals.push(dec.seasonal[i]); }
  const swing = Math.max(...seasVals) - Math.min(...seasVals);
  $('#p2Kpis').innerHTML = [
    ['Trend, ' + fd(DATES[regTr[0][1]]) + ' → ' + fd(DATES[regTr[regTr.length - 1][1]]), fmtV(regTr[0][0]) + ' → ' + fmtV(regTr[regTr.length - 1][0])],
    ['Weekly swing, top day to bottom day', fmtV(swing)],
    ['Strength of trend (0 to 1)', st.trend.toFixed(2)],
    ['Strength of season (0 to 1)', st.seasonal.toFixed(2)]
  ].map(([l, v]) => `<div class="kpi"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join('');
  // Day-of-week index
  const di = dowIndex(y, REG_I), idx = di.index;
  const per = [0, 1, 2, 3, 4, 5, 6].map(d => { const v = []; for (let i = REG_I; i < n; i++) if (dd.dow[i] === d && dd.class_day[i]) v.push(y[i]); return v.length ? mean(v) : null; });
  const cnt = [0, 1, 2, 3, 4, 5, 6].map(d => { let c = 0; for (let i = REG_I; i < n; i++) if (dd.dow[i] === d && dd.class_day[i]) c++; return c; });
  $('#p2DowLede').textContent = `For each weekday: its total ${meas} divided by the total of the centered 7-day average on those days, scaled so an average day = 1.00. Regular-polling days only (${fd(DATA.meta.regular_from)} on), class days and weekends; weekdays with no classes are left out. The centered average needs 3 days on each side, so the first and last 3 days drop out. The regression in the callout finds no weekday clearly different from Monday, so read small gaps between bars as noise.`;
  drawChart(box('p2Dow'), {
    n: 7, yMax: Math.max(...idx), mode: 'bars', h: 210, yFmt: v => v.toFixed(1),
    bars: [{ y: idx, color: 'var(--s3)' }], xTicks: DOW.map((d, i) => [i, d]),
    hlines: [{ v: 1, label: 'average day = 1.00', color: 'var(--muted)' }],
    barLabel: i => `${DOW[i]}: index ${idx[i].toFixed(2)}`,
    hover: i => ({ title: DOW[i], rows: [{ value: idx[i].toFixed(2), label: 'index', color: 'var(--s3)', box: true }, { value: ok(per[i]) ? fmtV(per[i]) : 'n/a', label: 'mean per class day' }, { value: String(di.n[i]), label: 'days in the index' }, { value: String(cnt[i]), label: 'class days in the mean' }] })
  });
  $('#p2DowHead').textContent = 'Mean per class day' + (cash ? ' ($)' : '');
  $('#p2DowTable').innerHTML = DOW.map((d, i) => `<tr><td class="l">${d}</td><td>${idx[i].toFixed(2)}</td><td>${ok(per[i]) ? esc(fmtV(per[i])) : '–'}</td><td>${di.n[i]}</td><td>${cnt[i]}</td></tr>`).join('');
  const tt = DATA.reg.trend, nc = DATA.reg.no_class;
  $('#p2Trendcall').innerHTML = `<b>What the regression says.</b> Holding the weekday fixed, daily drinks fell by ${n1(Math.abs(tt.slope_per_week))} a week through the term (95% CI ${n1(tt.ci_lo)} to ${n1(tt.ci_hi)}). A weekday with no classes sold ${n1(Math.abs(nc.coef))} fewer drinks (CI ${n1(nc.ci_lo)} to ${n1(nc.ci_hi)}), but that rests on only two such days. <br><br><b>Read the late fall as supply, not fading demand.</b> The operators stopped regular refilling to prepare for the project. The last refill proven at a single visit is ${esc(fd(LAST_RS.start, true))} ${hhmm(LAST_RS.start)}–${hhmm(LAST_RS.end)} (${esc(LAST_RS.evidence)}); later visits put product back in only some rails. Weekly drinks went ${wk3Text} (the last three full weeks) and our slots that sold went from ${peakSlots} in the best full week to ${lastFullSlots} in the last full week. Turn on suspected-empty slots and visits: the empty count climbs after the last single-visit proven refill.`;
}
['#p2mUnits', '#p2mCash', '#p2Spread', '#p2Trend', '#p2Season', '#p2Rem', '#p2Empty', '#p2Visits', '#p2Cal', '#p2Win'].forEach(s => $(s).addEventListener('change', drawStep2));

/* ---------- STEP 3 ---------- */
let p3Last = null; const p3Rows = [];
$('#p3Origin').innerHTML = DATA.bt.origins.map((o, i) => `<option value="${o}"${i === 0 ? ' selected' : ''}>${esc(fd(o, true))}</option>`).join('');
$('#p3Pool').textContent = `Test days come from ${fd(DATES[SPLIT_FIRST])} to ${fd(DATES[SPLIT_LAST])}, the same days the step 5 backtest scores. Training days reach back to term start, ${fd(TERM_START)}.`;
function p3Step() { return +$('#p3Model').value; }
function p3ModelNote() {
  $('#p3ModelNote').textContent = p3Step() === 7
    ? 'Predicts a test day as the average of the same weekday one week before and one week after (skipping other test days). On a time split there is no "after", so it becomes seasonal naive.'
    : 'Predicts a test day as the average of the training days just before and just after it. On a time split it becomes naive: repeat the last day.';
}
function drawStep3() {
  p3ModelNote();
  if (!p3Last) { const r = randomSplit(1, p3Step()); p3Last = { kind: 'random', r }; addP3Row('Random split, seed 1 (' + modelWord() + ')', r.test.length + ' days', r.m); $('#p3Explain').textContent = 'Seed 1 is drawn for you. Black dots are the 7 test days; rings are the predictions. Now press Time split and compare.'; }
  const i0 = I_TERM, n = N_DAYS - i0, ys = Y.slice(i0);
  const W = widthOf('p3Chart');
  const fcMap = {}, tests = [];
  const add = r => r.test.forEach((t, j) => { fcMap[t] = r.fc[j]; tests.push(t); });
  let shade = null;
  if (p3Last.kind === 'all') p3Last.rs.forEach(add); else add(p3Last.r);
  if (p3Last.kind === 'time') shade = [p3Last.r.test[0] - i0, p3Last.r.test[p3Last.r.test.length - 1] - i0];
  drawChart(box('p3Chart'), {
    n, yMax: Math.max(...ys), xTicks: dayTicks(DATES.slice(i0), W), shade,
    bands: null,
    lines: [{ y: ys, color: 'var(--ink)' }],
    dots: [{ pts: tests.map(t => ({ i: t - i0, v: Y[t] })), color: 'var(--ink)' }, { pts: tests.map(t => ({ i: t - i0, v: fcMap[t] })), color: 'var(--s1)', hollow: true }],
    hover: i => { const t = i + i0; const rows = [{ value: n1(Y[t]), label: 'actual drinks', color: 'var(--ink)' }];
      if (fcMap[t] !== undefined) { rows.push({ value: n1(fcMap[t]), label: 'predicted', color: 'var(--s1)' }); rows.push({ value: n1(Math.abs(Y[t] - fcMap[t])), label: 'miss' }); }
      return { title: fd(DATES[t], true) + (fcMap[t] !== undefined ? ' · test day' : ''), rows, note: typeText(t) }; }
  });
}
function addP3Row(name, days, m, rangeTxt) {
  p3Rows.unshift(`<tr><td class="l">${esc(name)}</td><td class="t">${esc(days)}</td><td>${rangeTxt ? esc(rangeTxt.mae) : n2(m.mae)}</td><td>${rangeTxt ? esc(rangeTxt.wape) : pct(m.wape)}</td></tr>`);
  $('#p3Table').innerHTML = p3Rows.slice(0, 8).join('');
}
const modelWord = () => p3Step() === 7 ? 'same-weekday fill-in' : 'next-door fill-in';
$('#p3Random').addEventListener('click', () => {
  const s = Math.max(1, Math.min(9999, Math.floor(+$('#p3Seed').value || 1))); $('#p3Seed').value = s;
  const r = randomSplit(s, p3Step()); p3Last = { kind: 'random', r };
  addP3Row(`Random split, seed ${s} (${modelWord()})`, r.test.length + ' days', r.m);
  const both = r.test.filter(t => t + p3Step() <= SPLIT_LAST).length;
  $('#p3Explain').innerHTML = `Black dots are the 7 test days, scattered across the term. Rings are the model's predictions. ${both} of the 7 have a training day <b>after</b> them, so the model peeks at the future.`;
  drawStep3();
});
$('#p3Time').addEventListener('click', () => {
  const o = $('#p3Origin').value, r = timeSplit(o, p3Step()); p3Last = { kind: 'time', r };
  addP3Row(`Time split at ${fd(o, true)} (${modelWord()})`, r.test.length + ' days', r.m);
  $('#p3Explain').innerHTML = `Train on everything before ${esc(fd(o, true))}; test the ${r.test.length} days from it on (shaded). There is no "after" side, so the model can only look back.`;
  drawStep3();
});
$('#p3Seeds').addEventListener('click', () => {
  const ms = [], ws = []; for (let s = 1; s <= 50; s++) { const r = randomSplit(s, p3Step()); ms.push(r.m.mae); ws.push(r.m.wape); }
  ms.sort((a, b) => a - b); ws.sort((a, b) => a - b);
  const med = a => (a[24] + a[25]) / 2;
  addP3Row(`Random seeds 1–50 (${modelWord()}): median (lowest – highest)`, '7 days each', null, { mae: `${n2(med(ms))} (${n2(ms[0])} – ${n2(ms[49])})`, wape: `${pct(med(ws))} (${pct(ws[0])} – ${pct(ws[49])})` });
  $('#p3Explain').textContent = 'Fifty random splits. The range shows how much one lucky or unlucky draw can move the score.';
});
$('#p3TimeAll').addEventListener('click', () => {
  const a = timeSplitAll(p3Step()); p3Last = { kind: 'all', rs: a.rs };
  addP3Row(`Time split at all ${a.rs.length} plan dates (${modelWord()}): average`, a.rs.length + ' windows', { mae: a.mae, wape: a.wape });
  const same = p3Step() === 7 ? SN_NAME : 'Naive (last day)';
  $('#p3Explain').innerHTML = `Every Saturday from ${esc(fd(DATA.bt.origins[0]))} on, forecasting the next 7 days. Average MAE ${n2(a.mae)}. The script's backtest of ${esc(same.toLowerCase())} scored ${n2(AVG[same].mae)}: it is the same test.`;
  drawStep3();
});
$('#p3Model').addEventListener('change', () => {
  if (!p3Last || p3Last.kind === 'random') $('#p3Random').click();
  else if (p3Last.kind === 'time') $('#p3Time').click();
  else if (p3Last.kind === 'all') $('#p3TimeAll').click();
  else drawStep3();
});

/* ---------- STEP 4 ---------- */
const p4Sel = $('#p4Origin');
p4Sel.innerHTML = DATA.bt.per_origin.map(po => `<option value="${po.origin}">${esc(fd(po.origin, true))}: forecast to ${esc(fd(po.test_dates[po.test_dates.length - 1]))}${po.test_dates.length < 7 ? ' (' + po.test_dates.length + ' days known)' : ''}</option>`).join('')
  + `<option value="${FWD}">${esc(fd(FWD, true))} (day after the last poll): next 7 days, not known yet</option>`;
p4Sel.value = S.origin;
$('#p4Models').insertAdjacentHTML('beforeend', MODELS.map(m => `<label class="opt"><input type="checkbox" id="p4m_${m.id}" data-model="${m.id}"${S.sel[m.id] ? ' checked' : ''}><i class="k key" style="border-top-color:${m.color}"></i><span>${esc(m.name)} <span class="muted small">· ${esc(m.desc)}</span></span></label>`).join(''));
$('#p4Models').insertAdjacentHTML('beforeend', '<span class="status" id="p4ModelsStatus" role="status"></span>');
function capModels() {
  const n = MODELS.filter(m => S.sel[m.id]).length, full = n >= MAX_SEL;
  $$('#p4Models input').forEach(inp => { inp.disabled = full && !inp.checked; });
  $('#p4ModelsStatus').textContent = full ? `Up to ${MAX_SEL} models at once. Untick a model to add another; the step 5 table scores all of them.` : '';
}
$$('#p4Models input').forEach(inp => inp.addEventListener('change', () => { S.sel[inp.dataset.model] = inp.checked; capModels(); drawStep4(); }));
capModels();
let p4DaysSet = false;
p4Sel.addEventListener('change', () => { S.origin = p4Sel.value; drawStep4(); });
$('#p4k').addEventListener('input', e => { S.k = +e.target.value; drawStep4(); });
$$('[data-wma]').forEach(b => b.addEventListener('click', () => { S.wma = b.dataset.wma; $$('[data-wma]').forEach(x => x.setAttribute('aria-pressed', x === b ? 'true' : 'false')); drawStep4(); }));
$('#p4alpha').addEventListener('input', e => { S.alpha = +e.target.value; drawStep4(); });
$('#p4sesFit').addEventListener('change', e => { S.sesFit = e.target.checked; drawStep4(); });
$('#p4adjust').addEventListener('change', e => { S.adjust = e.target.checked; drawStep4(); });
let p4Runs = [];
function runOrigin(origin) {
  const info = originInfo(origin);
  const runs = MODELS.filter(m => S.sel[m.id]).map(m => {
    const fc = forecastFor(m.id, info, cfg());
    return { id: m.id, fc, m: info.actual ? metrics(info.actual, fc.slice(0, info.actual.length)) : null };
  });
  return { info, runs };
}
function drawStep4() {
  $('#p4kVal').textContent = S.k;
  $('#p4alphaVal').textContent = S.alpha.toFixed(2);
  $('#p4alpha').disabled = S.sesFit;
  const { info, runs } = runOrigin(S.origin);
  p4Runs = runs;
  const fwd = !info.actual;
  // WMA formula and SES note for this plan date
  const w = WMA_PRESETS[S.wma], ws = sum(w), y = info.hist, L = y.length;
  const terms = w.map((v, j) => `${Math.round(100 * v / ws)}% × ${n1(y[L - 1 - j])}`);
  $('#p4wFormula').textContent = `From ${fd(S.origin)}: ${terms.join(' + ')} = ${n1(w.reduce((s, v, j) => s + v * y[L - 1 - j], 0) / ws)} drinks a day (most recent day first).`;
  const fit = sesFit(y);
  $('#p4sesNote').textContent = S.sesFit ? `Fitted on the days before ${fd(S.origin)}: α = ${fit.alpha.toFixed(2)}, level ${n1(fit.level)} drinks a day.` : `With α = ${S.alpha.toFixed(2)} the level is ${n1(sesLevel(y, S.alpha).level)}. The fitted α here would be ${fit.alpha.toFixed(2)}.`;
  // chart: 21 days before the plan date + 7 forecast days
  const back = 21, ci0 = info.oi - back, n = back + H;
  const act = []; for (let j = 0; j < n; j++) { const ci = ci0 + j; act.push(ci < N_DAYS ? Y[ci] : null); }
  const lines = [{ y: act, color: 'var(--ink)' }];
  runs.forEach(r => { const arr = new Array(back).fill(null).concat(r.fc); lines.push({ y: arr, color: MB[r.id].color, dash: true, w: lineW(r.id) }); });
  const W = widthOf('p4Chart');
  if (!p4DaysSet) { p4DaysSet = true; if (W < 560) $('#p4Days').open = true; }
  const allV = act.filter(ok).concat(...runs.map(r => r.fc));
  drawChart(box('p4Chart'), {
    n, yMax: Math.max(...allV, 5), lines, bands: Array.from({ length: n }, (_, j) => calType(ci0 + j)),
    window: [back, n - 1], windowLabel: fwd ? 'Next 7 days (not known yet)' : 'Hidden on the plan date',
    xTicks: Array.from({ length: n }, (_, j) => j).filter(j => pd(CAL.dates[ci0 + j]).getUTCDay() === 6 || (W >= 560 && pd(CAL.dates[ci0 + j]).getUTCDay() === 1)).map(j => [j, fd(CAL.dates[ci0 + j])]),
    hover: j => {
      const ci = ci0 + j, rows = [];
      if (ok(act[j])) rows.push({ value: n1(act[j]), label: j < back ? 'actual (known)' : 'actual', color: 'var(--ink)' });
      if (j >= back) runs.forEach(r => rows.push({ value: n1(r.fc[j - back]), label: mLabel(r.id), color: MB[r.id].color, dash: true }));
      return { title: fd(CAL.dates[ci], true), rows, note: [typeText(ci), dayLabel(ci)].filter(Boolean).join(' · ') };
    }
  });
  $('#p4Legend').innerHTML = `<span><i class="k" style="border-top-color:var(--ink)"></i>Actual drinks</span>` + runs.map(r => `<span><i class="k dash" style="border-top-color:${MB[r.id].color}"></i>${esc(mLabel(r.id))}</span>`).join('');
  // metrics table
  const best = runs.filter(r => r.m).reduce((a, r) => (!a || r.m.mae < a.m.mae ? r : a), null);
  $('#p4Metrics').innerHTML = runs.length ? runs.map(r => {
    const m = r.m, tot = sum(r.fc);
    return `<tr><td class="l"><i class="k" style="border-top-color:${MB[r.id].color};border-top-style:dashed;margin-right:6px"></i>${esc(mLabel(r.id))}</td><td class="${best === r ? 'best' : ''}">${m ? n2(m.mae) : '–'}</td><td>${m ? n2(m.rmse) : '–'}</td><td>${m ? pct(m.wape) : '–'}</td><td>${m ? sgn(m.bias, n2) : '–'}</td><td>${n1(tot)}${m ? ' vs ' + n1(m.totA) : ''}</td></tr>`;
  }).join('') : '<tr><td class="l muted" colspan="6">Tick at least one model.</td></tr>';
  // reading
  const ndays = info.test.length;
  if (fwd) {
    const nc = info.test.filter(ci => CAL.cls[ci]).length;
    $('#p4Read').innerHTML = `These ${ndays} days are not known yet, so there is nothing to score. ${nc} of them are class days; ${esc(fd(CAL.dates[info.test.find(ci => isNoClassWeekday(ci))] || FWD))} starts the quarter break. Holt-Winters here comes from the analysis script with its calendar overlay already applied.`;
  } else if (best) {
    $('#p4Read').innerHTML = `Plan date ${esc(fd(S.origin, true))}: ${ndays} days scored, ${n1(sum(info.actual))} drinks sold. Lowest miss: <b>${esc(mLabel(best.id))}</b>, ${n2(best.m.mae)} drinks a day, ${best.m.bias < 0 ? 'running high' : 'running low'} by ${n2(Math.abs(best.m.bias))} a day on average.`;
  } else $('#p4Read').textContent = '';
  // day table
  $('#p4DayHead').innerHTML = `<tr><th>Day</th><th class="l">Calendar</th><th>Actual</th>${runs.map(r => `<th>${esc(mLabel(r.id))}</th>`).join('')}</tr>`;
  $('#p4DayBody').innerHTML = info.test.map((ci, j) => `<tr><td class="l">${esc(fd(CAL.dates[ci], true))}</td><td class="l">${esc(typeText(ci))}</td><td>${info.actual ? n1(info.actual[j]) : '–'}</td>${runs.map(r => `<td>${n1(r.fc[j])}</td>`).join('')}</tr>`).join('');
}
const logRows = [];
$('#p4Log').addEventListener('click', () => {
  if (!p4Runs.length) { $('#p4LogStatus').textContent = 'Tick a model first.'; return; }
  const info = originInfo(S.origin);
  p4Runs.forEach(r => {
    const m = r.m;
    logRows.unshift(`<tr><td class="l">${esc(fd(S.origin, true))}</td><td class="l">${esc(mLabel(r.id))}</td><td class="l">${S.adjust ? 'adjusted' : 'off'}</td><td>${m ? n2(m.mae) : '–'}</td><td>${m ? n2(m.rmse) : '–'}</td><td>${m ? pct(m.wape) : '–'}</td><td>${m ? sgn(m.bias, n2) : '–'}</td><td>${n1(sum(r.fc))}</td><td>${info.actual ? n1(sum(info.actual)) : 'not known'}</td></tr>`);
  });
  $('#p4LogBody').innerHTML = logRows.join('');
  $('#p4LogStatus').textContent = `Logged ${p4Runs.length} row${p4Runs.length > 1 ? 's' : ''}.`;
});
$('#p4Clear').addEventListener('click', () => { logRows.length = 0; $('#p4LogBody').innerHTML = '<tr><td class="l muted" colspan="9">Press "Log this run" to keep a row here for each model you have ticked.</td></tr>'; $('#p4LogStatus').textContent = ''; });

/* ---------- STEP 5 ---------- */
const breakOrigins = DATA.bt.per_origin.filter(p => p.contains_no_class_weekday).map(p => fd(p.origin));
$('#p5SkipLabel').textContent = `Leave out plan dates whose week holds a weekday with no classes (${breakOrigins.join(', ')})`;
$('#p5Skip').addEventListener('change', drawStep5);
$('#p5Script').addEventListener('change', drawStep5);
function leaderboard() {
  const skip = $('#p5Skip').checked, c = cfg();
  const rows = MODELS.map(m => ({ id: m.id, name: mLabel(m.id), color: m.color, live: !STORED[m.id], r: backtest(m.id, c, { skipBreak: skip }) }));
  return rows;
}
function drawStep5() {
  const nOr = DATA.bt.origins.length;
  $('#p5Lede').textContent = `A walk-forward backtest moves the plan date and does it again: stand at a past Saturday, forecast the next 7 days, score them, step forward a week. It is cross-validation that respects time. Here that means ${nOr} Saturdays, from ${fd(DATA.bt.origins[0])} to ${fd(DATA.bt.origins[nOr - 1])}, each trained on every day since term start.`;
  $('#p5Settings').innerHTML = `<b>Your step 4 settings:</b> moving average k = ${S.k}; weighted MA ${esc(({ equal: 'equal', linear: 'linear 4-3-2-1', halving: 'halving 8-4-2-1' })[S.wma])}; smoothing ${S.sesFit ? 'α fit at each plan date' : 'α = ' + S.alpha.toFixed(2)}; calendar adjustment <b>${S.adjust ? 'on' : 'off'}</b>.`;
  const rows = leaderboard();
  const skip = $('#p5Skip').checked;
  const scriptOff = S.adjust || skip;
  $('#p5Script').disabled = scriptOff;
  $('#p5ScriptWhy').textContent = scriptOff ? `Off while ${S.adjust ? 'the calendar adjustment is on' : ''}${S.adjust && skip ? ' and ' : ''}${skip ? 'break weeks are left out' : ''}: the script's rows use every window with the calendar off.` : '';
  if ($('#p5Script').checked && !scriptOff) {
    Object.keys(AVG).forEach(name => {
      if (/^(SMA|WMA|SES|Holt \(damped)/.test(name)) {
        const a = AVG[name];
        if (!skip) rows.push({ id: null, name: name + ' (script)', color: null, script: a });
      }
    });
  }
  rows.sort((a, b) => (a.r ? a.r.mae : a.script.mae) - (b.r ? b.r.mae : b.script.mae));
  const origins = rows.find(r => r.r).r.rows.map(x => x.origin);
  const col = (r, j) => r.r ? r.r.rows[j].m.mae : null;
  const bestCol = origins.map((_, j) => { let b = null; rows.forEach(r => { const v = col(r, j); if (v !== null && (b === null || v < b.v)) b = { v, r }; }); return b && b.r; });
  const wins = r => r.r ? origins.filter((_, j) => bestCol[j] === r).length : null;
  const mae = r => r.r ? r.r.mae : r.script.mae;
  const bestAvg = rows.reduce((a, r) => (!a || mae(r) < mae(a) ? r : a), null);
  $('#p5Head').innerHTML = `<tr><th>Model</th>${origins.map(o => `<th>${esc(fd(o))}</th>`).join('')}<th>Average</th><th>WAPE</th><th>Bias</th><th>Wins</th></tr>`;
  $('#p5Body').innerHTML = rows.map(r => {
    const key = r.color ? `<i class="k" style="border-top-color:${r.color};margin-right:6px"></i>` : '';
    const tag = r.r ? (r.live ? '' : ' <span class="muted small">(script fit)</span>') : '';
    const cells = origins.map((_, j) => r.r ? `<td class="${bestCol[j] === r ? 'best' : ''}">${n2(col(r, j))}</td>` : '<td class="muted">–</td>').join('');
    const a = r.r || r.script;
    return `<tr${r.id === 'snaive' ? ' class="hl"' : ''}><td class="l">${key}${esc(r.name)}${tag}</td>${cells}<td class="${bestAvg === r ? 'best' : ''}"><b>${n2(a.mae)}</b></td><td>${pct(a.wape)}</td><td>${sgn(a.bias, n2)}</td><td>${r.r ? wins(r) : '–'}</td></tr>`;
  }).join('');
  // live check against the script (only meaningful with calendar off and all windows)
  const chk = [['naive', {}, 'Naive (last day)'], ['snaive', {}, SN_NAME], ['sma', { k: 3 }, 'SMA k=3'], ['sma', { k: 7 }, 'SMA k=7'], ['sma', { k: 14 }, 'SMA k=14'], ['sma', { k: 28 }, 'SMA k=28'], ['wma', { wma: 'linear' }, 'WMA linear 4-3-2-1'], ['wma', { wma: 'halving' }, 'WMA halving 8-4-2-1'], ['ses', { sesFit: true }, 'SES (alpha fit by grid)']];
  let worst = 0;
  chk.forEach(([m, c2, name]) => { const r = backtest(m, Object.assign({ adjust: false }, c2), {}); worst = Math.max(worst, Math.abs(r.mae - AVG[name].mae)); });
  $('#p5Check').innerHTML = `<b>Live check.</b> With the calendar adjustment off, this page reruns the script's ${chk.length} baseline settings and matches its average MAE to within ${worst.toFixed(3)} drinks a day. Small gaps come from the script's daily totals being rounded to one decimal before they were saved.`;
  // small multiples
  const sel = MODELS.filter(m => S.sel[m.id]);
  $('#p5Legend').innerHTML = `<span><i class="k" style="border-top-color:var(--ink)"></i>Actual drinks</span>` + sel.map(m => `<span><i class="k dash" style="border-top-color:${m.color}"></i>${esc(mLabel(m.id))}</span>`).join('');
  const per = DATA.bt.per_origin.filter(p => !(skip && p.contains_no_class_weekday));
  const host = $('#p5Mult');
  host.innerHTML = per.map((p, j) => `<div class="card"><h3 id="p5mh${j}"></h3><div class="chartbox"><svg id="p5m${j}" tabindex="0" role="img" aria-label="Plan date ${esc(fd(p.origin))}"></svg><div class="tip" hidden></div></div></div>`).join('');
  const maxY = Math.max(...per.map(p => Math.max(...p.actual)), ...per.map(p => { const info = originInfo(p.origin); return Math.max(0, ...sel.map(m => Math.max(...forecastFor(m.id, info, cfg())))); }));
  per.forEach((p, j) => {
    const info = originInfo(p.origin);
    const fcs = sel.map(m => ({ m, fc: forecastFor(m.id, info, cfg()) }));
    const met = fcs.map(f => ({ m: f.m, x: metrics(info.actual, f.fc) }));
    const b = met.reduce((a, r) => (!a || r.x.mae < a.x.mae ? r : a), null);
    $('#p5mh' + j).textContent = fd(p.origin, true) + (b ? ' · best ' + mLabel(b.m.id) + ' ' + n2(b.x.mae) : '');
    drawChart(box('p5m' + j), {
      n: info.test.length, yMax: maxY, h: 150, left: 34, bands: info.test.map(calType),
      xTicks: info.test.map((ci, i) => [i, WD[pd(CAL.dates[ci]).getUTCDay()].slice(0, 2)]),
      lines: [{ y: info.actual, color: 'var(--ink)' }].concat(fcs.map(f => ({ y: f.fc, color: f.m.color, dash: true, w: lineW(f.m.id) }))),
      hover: i => ({ title: fd(CAL.dates[info.test[i]], true), rows: [{ value: n1(info.actual[i]), label: 'actual', color: 'var(--ink)' }].concat(fcs.map(f => ({ value: n1(f.fc[i]), label: mLabel(f.m.id), color: f.m.color, dash: true }))), note: typeText(info.test[i]) })
    });
  });
  // MAE by k
  const ks = maeByK(28, cfg());
  const sn = backtest('snaive', cfg(), {}).mae, hw = backtest('hw', cfg(), {}).mae;
  const kBest = ks.indexOf(Math.min(...ks));
  drawChart(box('p5K'), {
    n: 28, yMax: Math.max(...ks, sn, hw), h: 230, lines: [{ y: ks, color: 'var(--s3)' }],
    hlines: [{ v: sn, label: 'Seasonal naive ' + n2(sn), color: 'var(--s1)', below: sn < hw }, { v: hw, label: 'Holt-Winters ' + n2(hw), color: 'var(--s2)', below: hw <= sn }],
    dots: [{ pts: [{ i: S.k - 1, v: ks[S.k - 1] }], color: 'var(--s3)' }],
    labels: [{ i: S.k - 1, v: ks[S.k - 1], text: 'k = ' + S.k, dy: -10 }],
    xTicks: [1, 7, 14, 21, 28].map(k => [k - 1, 'k=' + k]),
    hover: i => ({ title: 'k = ' + (i + 1) + ' days', rows: [{ value: n2(ks[i]), label: 'moving average MAE', color: 'var(--s3)' }, { value: n2(sn), label: 'seasonal naive', color: 'var(--s1)', dash: true }, { value: n2(hw), label: 'Holt-Winters', color: 'var(--s2)', dash: true }] })
  });
  $('#p5KNote').textContent = `Best k on these windows: ${kBest + 1} days (MAE ${n2(ks[kBest])}). Your k = ${S.k}: ${n2(ks[S.k - 1])}. ${Math.min(...ks) < sn ? 'With this calendar setting, some k beats seasonal naive.' : 'No k beats seasonal naive with this calendar setting: the problem is the weekly season, not noise.'}`;
}

/* ---------- STEP 6 ---------- */
['p6mHw', 'p6mHwd', 'p6mSn', 'p6Band', 'p6Restock', 'p6Cal', 'p6aTeam', 'p6aAgent', 'p6Supply', 'p6Clock', 'p6Live'].forEach(id => $('#' + id).addEventListener('change', drawStep6));
const P6 = { hw: HW_NAME, hwd: HWD_NAME, snaive: SN_NAME };
function drawStep6() {
  const mid = document.querySelector('input[name=p6model]:checked').value, name = P6[mid], M = DATA.fc.models[name];
  const F = DATA.fc, nf = F.dates.length, back = 14, n = back + nf;
  const ci0 = N_DAYS - back;
  const act = [], fc = [], lo = [], hi = [], rs = [];
  for (let j = 0; j < n; j++) {
    const k = j - back;
    act.push(j < back ? Y[ci0 + j] : null);
    fc.push(k >= 0 ? M.forecast[k] : (k === -1 ? Y[N_DAYS - 1] : null));
    lo.push(k >= 0 && k < IHZ ? M.lo80[k] : null); hi.push(k >= 0 && k < IHZ ? M.hi80[k] : null);
    rs.push(k >= 0 ? F.restocked.rates[k] : (k === -1 ? Y[N_DAYS - 1] : null));
  }
  const showBand = $('#p6Band').checked, showR = $('#p6Restock').checked;
  const W = widthOf('p6Chart');
  const ymax = Math.max(...act.filter(ok), ...(showBand ? hi.filter(ok) : fc.filter(ok)), ...(showR ? rs.filter(ok) : [0]));
  drawChart(box('p6Chart'), {
    n, yMax: ymax, bands: $('#p6Cal').checked ? Array.from({ length: n }, (_, j) => calType(ci0 + j)) : null,
    window: [back, n - 1], windowLabel: 'Forecast from ' + fd(F.dates[0]),
    areas: showBand ? [{ lo, hi, color: MB[mid].color }] : [],
    lines: [{ y: act, color: 'var(--ink)' }, { y: fc, color: MB[mid].color }].concat(showR ? [{ y: rs, color: 'var(--s6)', dash: true }] : []),
    xTicks: dayTicks(CAL.dates.slice(ci0, ci0 + n), W, 7),
    hover: j => {
      const ci = ci0 + j, k = j - back, rows = [];
      if (j < back) rows.push({ value: n1(act[j]), label: 'actual drinks', color: 'var(--ink)' });
      else {
        rows.push({ value: n1(M.forecast[k]), label: MB[mid].name, color: MB[mid].color });
        if (showBand) rows.push(k < IHZ ? { value: n1(M.lo80[k]) + ' – ' + n1(M.hi80[k]), label: '80% range' } : { value: 'n/a', label: `80% range not backtested ${k + 1} days ahead` });
        if (showR) rows.push({ value: n1(F.restocked.rates[k]), label: 'restocked scenario (plan reference)', color: 'var(--s6)', dash: true });
      }
      const vs = j < back ? (VBYDAY[CAL.dates[ci]] || []) : [];
      return { title: fd(CAL.dates[ci], true), rows, note: [typeText(ci), dayLabel(ci)].concat(vs.map(v => 'Visit ' + hhmm(v.start) + '–' + hhmm(v.end) + ': ' + v.evidence)).filter(Boolean).join(' · ') };
    }
  });
  $('#p6Legend').innerHTML = `<span><i class="k" style="border-top-color:var(--ink)"></i>Actual drinks, whole machine</span><span><i class="k" style="border-top-color:${MB[mid].color}"></i>${esc(MB[mid].name)}</span>` + (showBand ? `<span><i class="k box" style="background:${MB[mid].color};opacity:.25"></i>80% range, first ${IHZ} days only</span>` : '') + (showR ? `<span><i class="k dash" style="border-top-color:var(--s6)"></i>Restocked scenario, {{OUR}} (planning reference)</span>` : '');
  const col = { hw: 2, hwd: 3, snaive: 4 }[mid];
  const heads = ['Week from (Sat)', 'Restocked, ours', 'Holt-Winters', 'HW damped', 'Seasonal naive', 'Days', 'Class days'];
  $('#p6WeeksHead').innerHTML = '<tr>' + heads.map((h, i) => `<th class="${i === col ? 'pick' : ''}">${esc(h)}</th>`).join('') + '</tr>';
  $('#p6Weeks').innerHTML = F.weekly.map(w => {
    const wkLab = w.week < F.dates[0] ? fd(F.dates[0], true) + ' (rest of week)' : fd(w.week, true);
    const cells = [wkLab, n1(w.restocked_recent), n1(w[HW_NAME]), n1(w[HWD_NAME]), n1(w[SN_NAME]), String(w.days), String(w.class_days)];
    return '<tr>' + cells.map((c, i) => `<td class="${i === 0 ? 'l' : ''}${i === 1 ? ' sel' : ''}${i === col ? ' pick' : ''}">${esc(c)}</td>`).join('') + '</tr>';
  }).join('');
  const last = F.dates[nf - 1];
  const ev = DATA.events.filter(e => e.end >= F.dates[0] && e.start <= last);
  $('#p6Events').innerHTML = ev.map(e => `<li><span class="pill${e.classes === 'none' ? ' br' : ''}">${esc(fd(e.start) + (e.end !== e.start ? ' – ' + fd(e.end) : ''))}</span> ${esc(e.label)}</li>`).join('');
  $('#p6Lede').textContent = `The models are refit on every day from term start to the last poll (${fd(DATA.meta.last_poll, true)}) and run ${nf} days ahead, through the quarter break and Thanksgiving into the Winter Quarter. The restocked scenario runs over the same days.`;
  const P = plan(mid);
  $('#p6Headline').innerHTML = `<b>The history ends in a machine that was not being regularly refilled.</b> The last refill proven at a single visit is ${esc(fd(LAST_RS.start, true))} ${hhmm(LAST_RS.start)}–${hhmm(LAST_RS.end)} (${esc(LAST_RS.evidence)}). ${esc(PAIR_TEXT)} Later door openings: ${esc(afterText)}. ${esc(PHOTO_TEXT)} Check every one of our rails at the visit; the photo suggests about ${PH_NEED} of our ${PH.rails} need product. Every model here learned from that emptying machine, so a forecast built on recent levels understates demand once the team restocks. <b>Plan our slots with the restocked scenario:</b> about ${n1(F.restocked.per_class_day)} drinks a class day from {{OUR}} (the model lines are whole-machine totals), the sum of each of our slots' own sales rate while it was in stock over the last 4 weeks (${n1(F.restocked.per_class_day_era)} on whole-era rates). It is a reference, not a forecast: it can run low because the counters miss some empty rails (${fd(PH.at)} photo), and high if the old product mix is not restocked. Read the models as a floor for a machine that stays unrefilled.`;
  $('#p6Kpis').innerHTML = [
    ['Restocked scenario, drinks per class day', n1(F.restocked.per_class_day), 'planning reference (whole era ' + n1(F.restocked.per_class_day_era) + ')', true],
    ['7 days from ' + fd(F.dates[P.wk[0]], true) + ', restocked', n0(P.rsTot) + ' drinks', P.clsJ.length + ' class days'],
    ['Same week, ' + MB[mid].name, n0(P.tot) + ' drinks', 'unrefilled machine: a floor'],
    ['Observed per weekday, last 4 weeks', n1(P.recentMean), 'an emptying machine']
  ].map(([l, v, sub, hl]) => `<div class="kpi${hl ? ' hl' : ''}"><div class="v">${esc(v)}</div><div class="l">${esc(l)} · ${esc(sub)}</div></div>`).join('');
  $('#p6Method').textContent = `80% range = the forecast plus the 10th to 90th percentile of this model's backtest misses, by day type, floored at 0. Those misses come from forecasts 1 to ${IHZ} days ahead, so the range is drawn for the first ${IHZ} days only; later weeks, after the break, are further ahead than any backtest. Weekdays with no classes are set to the average weekend day. In the weekly table weeks run Saturday to Friday, the shaded column is the restocked scenario for {{OUR}} (the planning reference), the model columns are whole-machine totals, and the underlined column is the model you picked. The restocked scenario adds up each of {{OUR}}' own in-stock rate on class days and uses the weekend rate on other days. It is a reference, not a forecast: it can run low because the counters miss some empty rails (${fd(PH.at)} photo), and high if the old product mix is not restocked; the whole-era figure is ${n1(F.restocked.per_class_day_era)}.`;
  writeNote(mid);
}
function plan(mid) {
  const F = DATA.fc, M = F.models[P6[mid]];
  // the next 7 days from the first forecast date (visits are not on a fixed weekday)
  const wk = [...Array(Math.min(7, F.dates.length)).keys()];
  const clsJ = wk.filter(j => F.classes[j] === 'normal' && F.dow[j] < 5);
  const recent = []; for (let i = N_DAYS - 28; i < N_DAYS; i++) if (DATA.daily.dow[i] < 5) recent.push(Y[i]);
  return { wk, clsJ, tot: sum(wk.map(j => M.forecast[j])), rsTot: sum(wk.map(j => F.restocked.rates[j])), recentMean: mean(recent) };
}
function writeNote(mid) {
  const F = DATA.fc, name = P6[mid], M = F.models[name], a = AVG[name], sn = AVG[SN_NAME];
  const agent = $('#p6aAgent').checked;
  const P = plan(mid), wk = P.wk, tot = P.tot, rsTot = P.rsTot, clsJ = P.clsJ;
  const brk = DATA.events.find(e => e.type === 'break' && e.start >= F.dates[0]);
  const brkJ = brk ? F.dates.map((d, j) => j).filter(j => F.dates[j] >= brk.start && F.dates[j] <= brk.end) : [];
  const brkTot = sum(brkJ.map(j => M.forecast[j])), brkRs = sum(brkJ.map(j => F.restocked.rates[j]));
  const winter = DATA.events.find(e => e.type === 'term_start' && e.start > F.dates[0]);
  const clsR = clsJ.filter(j => j < IHZ);
  const hiCls = clsR.length ? Math.max(...clsR.map(j => M.hi80[j])) : null, loCls = clsR.length ? Math.min(...clsR.map(j => M.lo80[j])) : null;
  const byDay = wk.map(j => `${fd(F.dates[j], true)} ${n1(M.forecast[j])}`).join(', ');
  const byDayRs = wk.map(j => `${fd(F.dates[j], true)} ${n1(F.restocked.rates[j])}`).join(', ');
  const curSlots = lastFullSlots;
  const nEmpty = SUP_END.slots_sold_out_not_refilled + SUP_END.slots_silent_since_before_last_restock;
  const board = leaderboard().filter(r => r.r), skip = $('#p5Skip').checked;
  const best = board.sort((x, y) => x.r.mae - y.r.mae)[0] || null;
  const btWhat = `calendar adjustment ${S.adjust ? 'on' : 'off'}${skip ? ', break weeks left out' : ''}`;
  const r7 = DATA.rule7, r5 = DATA.rule5;
  const rsWhen = `${fd(LAST_RS.start, true)} ${hhmm(LAST_RS.start)}–${hhmm(LAST_RS.end)}`;
  const span = `${fd(F.dates[wk[0]], true)} to ${fd(F.dates[wk[wk.length - 1]], true)}`;
  const L = [];
  if (!agent) {
    L.push(`Forecast for ${span} (machine ${DATA.meta.device}, whole machine)`);
    L.push('');
    L.push(`Supply first: the history ends in a machine that was not being regularly refilled. The operators stopped regular refilling to prepare for the project; the last refill proven at a single visit is ${rsWhen}, and later visits put product back in only some rails. Weekly drinks (whole machine) went ${wk3Text} over the last three full weeks. Forecasts built on these recent levels understate demand once we restock.`);
    L.push(`Plan our slots on the restocked scenario: about ${n1(F.restocked.per_class_day)} drinks a class day from {{OUR}}, so about ${n0(rsTot)} drinks from ${span} (${clsJ.length} class days). By day: ${byDayRs}. This adds up each of our slots' own in-stock rate over the last 4 weeks; it is a reference, not a forecast: it can run low because the counters miss some empty rails (${fd(PH.at)} photo), and high if the old product mix is not restocked; the whole-era figure is ${n1(F.restocked.per_class_day_era)}.`);
    L.push(`Floor if the machine stays unrefilled: ${MB[mid].name} says about ${n0(tot)} drinks for the same days (${byDay}). Over ${DATA.bt.origins.length} past weekly plan dates (Saturdays) it missed by ${n2(a.mae)} drinks a day on average, against ${n2(sn.mae)} for seasonal naive, a near tie; it ran ${a.bias < 0 ? 'high' : 'low'} by ${n2(Math.abs(a.bias))} a day. That measures how well it tracked the machine as it was, not demand.`);
    if (clsR.length) L.push(`How sure (model): on those class days its 80% range runs from ${n1(loCls)} to ${n1(hiCls)} drinks a day.`);
    if (brk) L.push(`${brk.label}: ${fd(brk.start)} to ${fd(brk.end)}. Expect very little: about ${n1(brkTot)} drinks in total over those ${brkJ.length} days on the model, ${n1(brkRs)} on the restocked scenario.${winter ? ' ' + winter.label + ' on ' + fd(winter.start, true) + '.' : ''}`);
    if ($('#p6Clock').checked) L.push(`Slow-seller rule: count class days, not calendar days, pause the clock ${brk ? 'from ' + fd(brk.start) + ' to ' + fd(brk.end) : 'over the break'}, and check stock before calling a product slow. In a machine that is not being refilled, a silent slot is usually empty, not slow: ${r7.flagged_while_suspected_empty} of all ${r7.flags} "zero for 7 calendar days" flags on our slots fell on slots already suspected empty. Of the ${r7.flags_with_outcome} with a known outcome, ${r7.sold_after_refill} sold again only after a refill, ${r7.silent_next_week} stayed silent, and only ${r7.contradicted} sold again without one.`);
    if ($('#p6Supply').checked) L.push(`Empty slots: our slots that sold fell from ${peakSlots} in the best full week to ${curSlots} in the last full week. By the last poll ${nEmpty} of our slots are suspected empty (${SUP_END.slots_sold_out_not_refilled} sold out after the last single-visit proven refill, ${SUP_END.slots_silent_since_before_last_restock} silent since before it). ${PHOTO_TEXT} Check every one of our rails at the visit; the photo suggests about ${PH_NEED} need product.`);
    if ($('#p6Live').checked && best) L.push(`In this lab's own backtest (${btWhat}), the lowest average miss was ${best.name} at ${n2(best.r.mae)} drinks a day.`);
  } else {
    L.push(`FORECAST GUIDANCE (from ${DATA.meta.device} counters through ${fd(DATA.meta.last_poll, true)})`);
    L.push(`- Supply: no refill proven at a single visit since ${rsWhen} (door counter + our slots); later visits refilled only some rails. Recent sales show an emptying machine, not demand; forecasts on recent levels understate demand after a restock.`);
    L.push(`- Size the stock for ${span} on the restocked scenario: about ${n0(rsTot)} drinks from {{OUR}} (${n1(F.restocked.per_class_day)} a class day). It is a reference, not a forecast: it can run low because the counters miss some empty rails (${fd(PH.at)} photo), and high if the old product mix is not restocked; the whole-era figure is ${n1(F.restocked.per_class_day_era)}.`);
    L.push(`- Daily plan (restocked): ${byDayRs}.`);
    L.push(`- Floor if not refilled: ${MB[mid].name} ${n0(tot)} drinks (${byDay}). Its backtest miss is ${n2(a.mae)} a day (seasonal naive ${n2(sn.mae)}); it ran ${a.bias < 0 ? 'high' : 'low'} by ${n2(Math.abs(a.bias))} a day.${clsR.length ? ` Model 80% range on class days: ${n1(loCls)} to ${n1(hiCls)} a day.` : ''}`);
    if (brk) L.push(`- ${fd(brk.start)} to ${fd(brk.end)} is ${brk.label.toLowerCase()}. Expect about ${n1(brkTot)} drinks in total (${n1(brkRs)} if fully stocked). A paid midweek visit in that stretch is unlikely to pay for itself.`);
    if ($('#p6Clock').checked) L.push(`- Slow-seller clock: count only observed class days; weekends, breaks, holidays and polling-gap days pause it. Do not call a slot slow while it may be empty: ${r7.flagged_while_suspected_empty} of ${r7.flags} past "zero for 7 days" flags fell on slots already suspected empty; of the ${r7.flags_with_outcome} with a known outcome, ${r7.sold_after_refill} sold again only after a refill, ${r7.silent_next_week} stayed silent, and only ${r7.contradicted} sold again without one. Requiring 5 class days and a slot not suspected empty cut the flags to ${r5.flags} (${r5.flags_with_outcome} with a known outcome): ${DATA.rule5_verdict}. Fewer flags is not fewer wrong flags.`);
    if ($('#p6Supply').checked) L.push(`- Empty slots: ${nEmpty} of our slots suspected empty at the last poll; only ${curSlots} of our slots sold in the last full week, against ${peakSlots} in the best full week. ${PHOTO_TEXT} Check every one of our rails at the visit; the photo suggests about ${PH_NEED} need product.`);
    if ($('#p6Live').checked && best) L.push(`- Lab backtest note: lowest miss was ${best.name} at ${n2(best.r.mae)} a day (${btWhat}).`);
  }
  $('#p6Note').textContent = L.join('\n');
}
$('#p6Copy').addEventListener('click', () => {
  const txt = $('#p6Note').textContent, st = $('#p6CopyStatus');
  try {
    navigator.clipboard.writeText(txt).then(() => { st.textContent = 'Copied.'; }, () => { st.textContent = 'Copy was blocked. Select the note and copy it by hand.'; });
  } catch (e) { st.textContent = 'Copy was blocked. Select the note and copy it by hand.'; }
});

/* ---------- footer ---------- */
$('#devId').textContent = DATA.meta.device;
$('#foot').textContent = `Data: machine ${DATA.meta.device}, lifetime counters polled ${n0(DATA.meta.polls)} times from ${fd(DATA.meta.first_poll, true)} to ${fd(DATA.meta.last_poll, true)}, Eastern time. A sale is a counter rise between two polls. Before ${fd(DATA.meta.regular_from)} polls were days apart; their lumps are spread over each gap with an hour-of-week profile from the regular era, weighted by the academic calendar (weekdays the Institute was closed get none, open weekdays without classes get the weekend rate), which is an assumption: the pre-term weeks are estimates whose shape uses later data. Live models in steps 3 to 5 train on that spread series; the script's own backtest re-spreads with data before each plan date. Stock is inferred, not observed, and the machine does not report product names. Visits come from the door-open counter (EA2*DO) in each poll; whether a visit refilled anything is inferred from the sales after it. Trend, calendar, weekday and forecast charts use whole-machine totals because they are the pre-competition history: course staff stocked every slot, so the totals reflect no rival strategy. Slot counts, refill evidence and the restocked scenario use {{OUR}} only; no other slot's figures are in this page. Holt-Winters, calendar regression, the 80% ranges and the restocked scenario come from the team's analysis script; every other model runs live in this page. Calendar dates are from the Institute calendar and may change.`;

/* ---------- start ---------- */
let rw = 0;
if (typeof ResizeObserver !== 'undefined') {
  new ResizeObserver(entries => { const w = Math.round(entries[0].contentRect.width); if (Math.abs(w - rw) > 8) { rw = w; requestAnimationFrame(() => render(current)); } }).observe($('.page'));
}
let start = 1;
const hm = (location.hash.match(/step([1-6])/) || [])[1];
if (hm) start = +hm; else { try { const v = +localStorage.getItem(KEY); if (v >= 1 && v <= 6) start = v; } catch (e) { /* storage blocked */ } }
go(start);
})();
