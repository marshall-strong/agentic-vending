// Forecast core: pure functions shared by the lab page and the node check script.
const DD = DATA.daily;
const DATES = DD.date, Y = DD.units, N_DAYS = DATES.length;
const TERM_START = '2026-09-09';
const I_TERM = DATES.indexOf(TERM_START);
const H = 7;
const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const SN_NAME = 'Seasonal naive (same weekday last week)';
const HW_NAME = 'Holt-Winters additive (weekly)';
const HWD_NAME = 'Holt-Winters additive + damped trend';
const CREG_NAME = 'Calendar regression (weekday + class flag + level)';

// One calendar for history plus the 28 forecast days: class day = Mon-Fri with classes.
const CAL = (function () {
  const dates = DATES.slice(), dow = DD.dow.slice(), cls = DD.class_day.map(v => v === 1);
  DATA.fc.dates.forEach((d, j) => { dates.push(d); dow.push(DATA.fc.dow[j]); cls.push(DATA.fc.classes[j] === 'normal' && DATA.fc.dow[j] < 5); });
  return { dates, dow, cls };
})();
const calIndex = d => CAL.dates.indexOf(d);
const isNoClassWeekday = ci => CAL.dow[ci] < 5 && !CAL.cls[ci];

function mean(a) { return a.length ? a.reduce((s, v) => s + v, 0) / a.length : NaN; }
function sum(a) { return a.reduce((s, v) => s + v, 0); }

// Plan date (origin) -> history index range and test calendar indexes.
// Origin "2026-11-12" is the forward case: history runs to the last poll, no actuals yet.
// For a past plan date the history is the script's as-of series: gap lumps re-spread with only the data
// reported before that date, so no training day borrows its shape from later weeks.
function originInfo(origin) {
  const oi = calIndex(origin);
  const po = DATA.bt.per_origin.find(p => p.origin === origin);
  const hist = po && po.history_start === TERM_START ? po.history.slice() : Y.slice(I_TERM, Math.min(oi, N_DAYS));
  const test = [];
  for (let j = 0; j < H; j++) test.push(oi + j);
  const known = test.filter(ci => ci < N_DAYS);
  const actual = known.map(ci => Y[ci]);
  return { origin, oi, hist, histStart: I_TERM, test: oi < N_DAYS ? known : test, actual: oi < N_DAYS ? actual : null };
}

const WMA_PRESETS = { equal: [1, 1, 1, 1], linear: [4, 3, 2, 1], halving: [8, 4, 2, 1] };
const WMA_LABEL = { equal: 'equal 1-1-1-1', linear: 'linear 4-3-2-1', halving: 'halving 8-4-2-1' };

function sesLevel(y, a) {
  let lvl = y[0], sse = 0;
  for (let i = 1; i < y.length; i++) { sse += (y[i] - lvl) * (y[i] - lvl); lvl = a * y[i] + (1 - a) * lvl; }
  return { level: lvl, sse };
}
function sesFit(y) {
  let best = null;
  for (let s = 1; s <= 19; s++) {
    const a = Math.round(s * 5) / 100;
    const r = sesLevel(y, a);
    if (best === null || r.sse < best.sse) best = { alpha: a, level: r.level, sse: r.sse };
  }
  return best;
}

// Live models. Each returns a forecast array for the test days of `info`.
function liveForecast(model, info, cfg) {
  const y = info.hist, n = info.test.length, L = y.length;
  const flat = v => new Array(n).fill(Math.max(0, v));
  if (model === 'naive') return flat(y[L - 1]);
  if (model === 'snaive') return info.test.map((_, j) => Math.max(0, y[L - 7 + (j % 7)]));
  if (model === 'sma') { const k = Math.min(cfg.k, L); return flat(mean(y.slice(L - k))); }
  if (model === 'wma') {
    const w = WMA_PRESETS[cfg.wma]; let f = 0;
    for (let j = 0; j < w.length; j++) f += w[j] * y[L - 1 - j];
    return flat(f / sum(w));
  }
  if (model === 'ses') { const a = cfg.sesFit ? sesFit(y).alpha : cfg.alpha; return flat(sesLevel(y, a).level); }
  return null;
}

// Forward calendar regression from the analysis script's fitted coefficients.
function cregForward(info) {
  const c = DATA.fc.creg, ext = Y.slice(), out = [];
  info.test.forEach(ci => {
    const lvl = mean(ext.slice(ci - 13, ci - 6));
    let f = c[DOW[CAL.dow[ci]]] + (isNoClassWeekday(ci) ? c.no_class : 0) + c.level * lvl;
    f = Math.max(0, f);
    ext[ci] = f; out.push(f);
  });
  return out;
}

// Precomputed models (Holt-Winters, calendar regression) for a backtest origin or the forward window.
function storedForecast(name, info) {
  const po = DATA.bt.per_origin.find(p => p.origin === info.origin);
  if (po) return po.forecasts[name].slice(0, info.test.length);
  if (name === CREG_NAME) return cregForward(info);
  const m = DATA.fc.models[name];
  if (!m) return null;
  return m.forecast.slice(0, info.test.length);
}

// "Adjust for the academic calendar": use only the history before the plan date.
// Flat models are scaled by (mean on that day type / mean of all days). Seasonal models only change
// where the target day's type differs from the day they copy; Holt-Winters gets no-class weekdays set to
// the mean of non-class days. Calendar regression already knows the calendar.
function calendarAdjust(model, fc, info) {
  const y = info.hist, st = info.histStart;
  const cls = [], off = [];
  y.forEach((v, i) => (CAL.cls[st + i] ? cls : off).push(v));
  const mAll = mean(y), mCls = mean(cls), mOff = mean(off);
  if (model === 'creg') return fc.slice();
  if (model === 'naive' || model === 'sma' || model === 'wma' || model === 'ses') {
    return fc.map((f, j) => mAll > 0 ? f * (CAL.cls[info.test[j]] ? mCls : mOff) / mAll : f);
  }
  if (model === 'snaive') {
    return fc.map((f, j) => {
      const ci = info.test[j], src = st + y.length - 7 + (j % 7);
      if (CAL.cls[ci] === CAL.cls[src]) return f;
      if (!CAL.cls[ci]) return mOff;
      const same = y.filter((v, i) => CAL.cls[st + i] && CAL.dow[st + i] === CAL.dow[ci]);
      return same.length ? mean(same) : mCls;
    });
  }
  return fc.map((f, j) => isNoClassWeekday(info.test[j]) ? mOff : f);   // hw, hwd
}

const STORED = { hw: HW_NAME, hwd: HWD_NAME, creg: CREG_NAME };
function forecastFor(model, info, cfg) {
  let fc = STORED[model] ? storedForecast(STORED[model], info) : liveForecast(model, info, cfg);
  if (cfg.adjust) fc = calendarAdjust(model, fc, info);
  return fc;
}

function metrics(actual, fc) {
  const e = actual.map((a, i) => a - fc[i]);
  const totA = sum(actual), totF = sum(fc);
  return {
    mae: mean(e.map(Math.abs)), rmse: Math.sqrt(mean(e.map(v => v * v))),
    wape: totA > 0 ? sum(e.map(Math.abs)) / totA : NaN, bias: mean(e),
    sumAbs: sum(e.map(Math.abs)), totA, totF, n: e.length
  };
}

// Rolling-origin backtest over the analysis plan dates.
function backtest(model, cfg, opts) {
  const rows = [];
  DATA.bt.per_origin.forEach(po => {
    if (opts && opts.skipBreak && po.contains_no_class_weekday) return;
    const info = originInfo(po.origin);
    const fc = forecastFor(model, info, cfg);
    rows.push({ origin: po.origin, fc, actual: info.actual, m: metrics(info.actual, fc) });
  });
  const absSum = sum(rows.map(r => r.m.sumAbs)), actSum = sum(rows.map(r => r.m.totA));
  return {
    rows,
    mae: mean(rows.map(r => r.m.mae)), rmse: mean(rows.map(r => r.m.rmse)),
    bias: mean(rows.map(r => r.m.bias)), wape: actSum > 0 ? absSum / actSum : NaN
  };
}

function maeByK(kMax, cfg) {
  const out = [];
  for (let k = 1; k <= kMax; k++) out.push(backtest('sma', Object.assign({}, cfg, { k }), {}).mae);
  return out;
}

// Seeded PRNG so a random split can be reproduced.
function mulberry32(a) {
  return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    let t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
// Days eligible for testing: the same days the walk-forward backtest scores.
const SPLIT_FIRST = DATES.indexOf(DATA.bt.origins[0]);
const SPLIT_LAST = N_DAYS - 1;
// Fill-in model: average of the nearest training day on each side. step 7 = same weekday, step 1 = next door.
function fillIn(test, step) {
  const isTest = new Set(test);
  return test.map(t => {
    let b = t - step; while (b >= I_TERM && isTest.has(b)) b -= step;
    let a = t + step; while (a <= SPLIT_LAST && isTest.has(a)) a += step;
    const sides = [];
    if (b >= I_TERM) sides.push(Y[b]);
    if (a <= SPLIT_LAST) sides.push(Y[a]);
    return mean(sides);
  });
}
function randomSplit(seed, step) {
  const rnd = mulberry32(seed), pool = [];
  for (let i = SPLIT_FIRST; i <= SPLIT_LAST; i++) pool.push(i);
  for (let i = pool.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [pool[i], pool[j]] = [pool[j], pool[i]]; }
  const test = pool.slice(0, H).sort((a, b) => a - b);
  const fc = fillIn(test, step);
  return { test, fc, m: metrics(test.map(t => Y[t]), fc) };
}
// Time split at a plan Saturday: the fill-in model has no "after" side, so it can only look back.
function timeSplit(origin, step) {
  const oi = DATES.indexOf(origin), test = [], h = originInfo(origin).hist, L = h.length;
  for (let j = 0; j < H && oi + j <= SPLIT_LAST; j++) test.push(oi + j);
  const fc = test.map((t, j) => step === 7 ? h[L - 7 + (j % 7)] : h[L - 1]);
  return { test, fc, m: metrics(test.map(t => Y[t]), fc) };
}
function timeSplitAll(step) {
  const rs = DATA.bt.origins.map(o => timeSplit(o, step));
  const absSum = sum(rs.map(r => r.m.sumAbs)), actSum = sum(rs.map(r => r.m.totA));
  return { rs, mae: mean(rs.map(r => r.m.mae)), wape: absSum / actSum };
}

// Classical additive decomposition with a centered moving average of odd window w.
function decompose(y, w) {
  const h = (w - 1) / 2, n = y.length, trend = new Array(n).fill(null);
  for (let i = h; i < n - h; i++) trend[i] = mean(y.slice(i - h, i + h + 1));
  const byDow = [[], [], [], [], [], [], []];
  y.forEach((v, i) => { if (trend[i] !== null) byDow[DD.dow[i]].push(v - trend[i]); });
  let s = byDow.map(a => a.length ? mean(a) : 0);
  const sm = mean(s); s = s.map(v => v - sm);
  const seasonal = y.map((_, i) => s[DD.dow[i]]);
  const remainder = y.map((v, i) => trend[i] === null ? null : v - trend[i] - seasonal[i]);
  return { trend, seasonal, remainder, dowEffect: s };
}
function variance(a) { const m = mean(a); return mean(a.map(v => (v - m) * (v - m))); }
function strengths(trend, seasonal, remainder) {
  const T = [], S = [], R = [];
  remainder.forEach((r, i) => { if (r !== null && trend[i] !== null) { T.push(trend[i] + r); S.push(seasonal[i] + r); R.push(r); } });
  const vr = variance(R);
  return { trend: Math.max(0, 1 - vr / variance(T)), seasonal: Math.max(0, 1 - vr / variance(S)) };
}
// Multiplicative day-of-week index, same method as the analysis script: for each weekday, the sum of its
// values / the sum of the centered 7-day mean on those days (ratio of totals), scaled to average 1.
// Class days and weekends only: weekdays with no classes (break, closure) are left out.
// Returns the index and the number of days behind each weekday's figure.
function dowIndex(y, from) {
  const num = new Array(7).fill(0), den = new Array(7).fill(0), n = new Array(7).fill(0);
  for (let i = from + 3; i < y.length - 3; i++) {
    const c = mean(y.slice(i - 3, i + 4)), d = DD.dow[i];
    if (!(c > 0) || (d < 5 && !DD.class_day[i])) continue;
    num[d] += y[i]; den[d] += c; n[d]++;
  }
  const raw = num.map((v, d) => den[d] > 0 ? v / den[d] : 0), m = mean(raw);
  return { index: raw.map(v => v / m), n };
}

if (typeof module !== 'undefined') module.exports = {
  DATA, DATES, Y, I_TERM, CAL, H, DOW, SN_NAME, HW_NAME, HWD_NAME, CREG_NAME, WMA_PRESETS,
  originInfo, liveForecast, storedForecast, cregForward, calendarAdjust, forecastFor, metrics, backtest, maeByK,
  sesFit, sesLevel, mulberry32, randomSplit, timeSplit, timeSplitAll, decompose, strengths, dowIndex, mean, sum
};
