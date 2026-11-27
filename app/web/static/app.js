/* Telec signal panel. Plain JS, Lightweight Charts v4 (global LightweightCharts). */
'use strict';

const C = { ink: '#151a2e', surface: '#1d2440', line: '#2c3556', text: '#ece8de', muted: '#9197ae',
            long: '#3bbf9a', short: '#ef6b6b', saffron: '#e9b44c' };
const TF_MS = { '15m': 9e5, '1h': 36e5, '4h': 144e5, '1d': 864e5 };
const TZ = -new Date().getTimezoneOffset() * 60; // chart shows the viewer's local time
const STYLE_FA = { trend_pullback: 'پولبک روندی', breakout: 'شکست محدوده', mean_reversion: 'برگشت به میانگین',
                   liquidity_sweep: 'شکار نقدینگی', divergence: 'واگرایی RSI', vwap_volume: 'VWAP و حجم' };
const REGIME_FA = { trend_up: 'روند صعودی', trend_down: 'روند نزولی', range: 'رنج', squeeze: 'فشرده', chaotic: 'آشفته' };
const EXIT_FA = { stop: 'حد ضرر خورد', breakeven: 'سربه‌سر بسته شد', tp1: 'TP1', tp2: 'به هدف TP2 رسید' };

const state = { symbols: [], symbol: null, tf: '1h', signals: [], lastPrice: null, selected: null, priceLines: [] };
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) } });
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || 'خطا');
  return data;
}

// ---- formatting --------------------------------------------------------
function decimals(p) {
  if (!p) return 2;
  if (p >= 1000) return 1;
  if (p >= 10) return 2;
  return Math.min(8, Math.max(3, 3 - Math.floor(Math.log10(p))));
}
const fp = (p, ref) => p == null ? '—' : Number(p).toLocaleString('en-US', { minimumFractionDigits: decimals(ref ?? p), maximumFractionDigits: decimals(ref ?? p) });
const money = (v, sign = false) => v == null ? '—' : (sign && v > 0 ? '+' : '') + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (v) => v == null ? '—' : `${v}%`;
const when = (ms) => new Date(ms).toLocaleString('fa-IR', { dateStyle: 'medium', timeStyle: 'short' });
const sideFa = (s) => (s > 0 ? 'خرید' : 'فروش');
const sideCls = (s) => (s > 0 ? 'long' : 'short');

// ---- risk ladder: SL at -1R, entry 0, TP1, TP2, live price marker ------------
function ladder(sig, price) {
  const risk = Math.abs(sig.entry - sig.stop);
  const r = (p) => (sig.side * (p - sig.entry)) / risk;
  const r1 = r(sig.tp1), r2 = r(sig.tp2), lo = -1.15, hi = r2 + 0.15;
  const x = (v) => (((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo)) * 100).toFixed(2);
  const tick = (v, label, p) => `<div class="tick" style="left:${x(v)}%"><span><b class="num">${fp(p, sig.entry)}</b>${label}</span></div>`;
  let now = '';
  if (price != null) {
    const rv = r(price);
    now = `<div class="now" style="left:${x(rv)}%" title="قیمت فعلی"><em class="num">${rv >= 0 ? '+' : ''}${rv.toFixed(2)}R</em></div>`;
  }
  return `<div class="ladder" aria-label="موقعیت قیمت نسبت به حد ضرر و اهداف">
    <div class="loss" style="left:${x(-1)}%;width:${x(0) - x(-1)}%"></div>
    <div class="gain" style="left:${x(0)}%;width:${x(r2) - x(0)}%"></div>
    ${tick(-1, 'حد ضرر', sig.stop)}${tick(0, 'ورود', sig.entry)}${tick(r1, 'TP1', sig.tp1)}${tick(r2, 'TP2', sig.tp2)}${now}
  </div>`;
}

function ticket(sig, { price = null, extra = '', clickable = false } = {}) {
  const rr = (Math.abs(sig.tp2 - sig.entry) / Math.abs(sig.entry - sig.stop)).toFixed(1);
  const why = (sig.styles || []).map((v) =>
    `<li title="${esc(v.reason)}">${esc(STYLE_FA[v.style] || v.style)}<small class="num">${Number(v.s).toFixed(2)}</small></li>`).join('');
  return `<article class="ticket ${sideCls(sig.side)}${clickable ? ' clickable' : ''}" data-id="${sig.id}" data-symbol="${esc(sig.symbol)}">
    <div class="t-head">
      <h3>${esc(sig.symbol)}</h3><span class="side ${sideCls(sig.side)}">${sideFa(sig.side)}</span>
      <span class="t-time">${when(sig.bar_time + TF_MS['15m'])}</span>
      <div class="score" title="امتیاز هم‌پوشانی از ۱۰۰"><span class="bar"><i style="width:${Math.min(100, sig.score)}%"></i></span><b class="num">${Math.round(sig.score)}</b></div>
    </div>
    ${ladder(sig, price)}
    <div class="t-facts">
      <span>نسبت سود به ضرر <b class="num">1:${rr}</b></span>
      ${sig.regime_1h ? `<span>بازار ۱ساعته <b>${REGIME_FA[sig.regime_1h] || sig.regime_1h}</b></span>` : ''}
      ${(sig.notes || []).map((n) => `<span>${esc(n)}</span>`).join('')}
    </div>
    ${why ? `<ul class="t-why">${why}</ul>` : ''}
    ${sig.lbank_entry != null ? `<div class="t-lbank"><span>قیمت‌ها در ال‌بانک</span>
      <span>ورود <b class="num">${fp(sig.lbank_entry)}</b></span><span>حد ضرر <b class="num">${fp(sig.lbank_stop, sig.lbank_entry)}</b></span>
      <span>TP1 <b class="num">${fp(sig.lbank_tp1, sig.lbank_entry)}</b></span><span>TP2 <b class="num">${fp(sig.lbank_tp2, sig.lbank_entry)}</b></span>
      <span class="muted">اختلاف با بایننس <b class="num">${sig.lbank_diff_pct > 0 ? '+' : ''}${sig.lbank_diff_pct}%</b></span></div>` : ''}
    ${extra}
  </article>`;
}

function paperOutcome(sig) {
  if (!sig.paper_status) return '';
  if (sig.paper_status === 'skipped') return `<div class="t-result muted">در ترید مجازی ثبت نشد: ${esc(sig.paper_skip)}</div>`;
  if (sig.paper_status === 'open') return `<div class="t-result">در ترید مجازی باز است.</div>`;
  const cls = sig.paper_pnl > 0 ? 'pos' : 'neg';
  return `<div class="t-result">ترید مجازی: ${EXIT_FA[sig.paper_exit] || sig.paper_exit}، <span class="num ${cls}">${money(sig.paper_pnl, true)} USDT</span></div>`;
}

// ---- chart -------------------------------------------------------------
let chart, candles;
function initChart() {
  chart = LightweightCharts.createChart($('#chart'), {
    layout: { background: { color: C.surface }, textColor: C.muted, fontFamily: 'Vazirmatn, Tahoma, sans-serif' },
    grid: { vertLines: { color: 'rgba(44,53,86,.45)' }, horzLines: { color: 'rgba(44,53,86,.45)' } },
    rightPriceScale: { borderColor: C.line }, timeScale: { borderColor: C.line, timeVisible: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  candles = chart.addCandlestickSeries({ upColor: C.long, downColor: C.short, wickUpColor: C.long,
    wickDownColor: C.short, borderVisible: false });
  new ResizeObserver(() => chart.applyOptions({ width: $('#chart').clientWidth, height: $('#chart').clientHeight })).observe($('#chart'));
}

function clearLines() { state.priceLines.forEach((l) => candles.removePriceLine(l)); state.priceLines = []; }

function drawLines(sig) {
  clearLines();
  if (!sig) return;
  const L = LightweightCharts.LineStyle;
  const add = (price, color, title, style = L.Dashed) =>
    state.priceLines.push(candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  add(sig.entry, C.saffron, 'ورود', L.Solid);
  add(sig.stop, C.short, 'حد ضرر');
  add(sig.tp1, C.long, 'TP1');
  add(sig.tp2, C.long, 'TP2');
}

function markers() {
  const step = TF_MS[state.tf];
  const out = [];
  for (const s of state.signals) {
    const t = Math.floor(s.bar_time / step) * step / 1000 + TZ;
    out.push({ time: t, position: s.side > 0 ? 'belowBar' : 'aboveBar', color: s.side > 0 ? C.long : C.short,
               shape: s.side > 0 ? 'arrowUp' : 'arrowDown', text: `${Math.round(s.score)}`, size: s.id === state.selected ? 2 : 1 });
    if (s.paper_closed_at) {
      // closed_at is the END of the exit bar; place the dot on that bar
      out.push({ time: Math.floor((s.paper_closed_at - 1) / step) * step / 1000 + TZ, position: 'inBar', color: s.paper_pnl > 0 ? C.long : C.short, shape: 'circle', text: '' });
    }
  }
  return out.sort((a, b) => a.time - b.time);
}

async function loadChart(fit = true) {
  if (!state.symbol) return;
  const [rows, sigs] = await Promise.all([
    api(`/api/candles?symbol=${state.symbol}&tf=${state.tf}&limit=600`),
    api(`/api/signals?symbol=${state.symbol}&limit=50`),
  ]);
  state.signals = sigs;
  const d = decimals(rows.length ? rows[rows.length - 1][4] : 1);
  candles.applyOptions({ priceFormat: { type: 'price', precision: d, minMove: Math.pow(10, -d) } });
  candles.setData(rows.map((r) => ({ time: r[0] / 1000 + TZ, open: r[1], high: r[2], low: r[3], close: r[4] })));
  state.lastPrice = rows.length ? rows[rows.length - 1][4] : null;
  if (state.selected && !sigs.some((s) => s.id === state.selected)) state.selected = null;
  if (!state.selected && sigs.length) state.selected = sigs[0].id;
  candles.setMarkers(markers());
  drawLines(sigs.find((s) => s.id === state.selected));
  if (fit) chart.timeScale().scrollToRealTime();

  const sym = state.symbols.find((s) => s.symbol === state.symbol);
  $('#c-symbol').textContent = state.symbol;
  $('#c-regime').textContent = sym && sym.regime_1h ? `بازار ۱ساعته: ${REGIME_FA[sym.regime_1h] || sym.regime_1h}` : '';
  $('#c-count').textContent = sigs.length ? `${sigs.length} سیگنال` : '';
  $('#c-tickets').innerHTML = sigs.length
    ? sigs.map((s) => ticket(s, { price: state.lastPrice, extra: paperOutcome(s), clickable: true })).join('')
    : `<div class="empty"><b>برای ${esc(state.symbol)} هنوز سیگنالی ثبت نشده.</b>هر وقت چند سبک هم‌زمان فرصتی ببینند، اینجا و روی نمودار ظاهر می‌شود.</div>`;
}

function renderChips() {
  $('#chips').innerHTML = state.symbols.map((s) =>
    `<button class="chip" data-symbol="${esc(s.symbol)}" aria-pressed="${s.symbol === state.symbol}" title="${REGIME_FA[s.regime_1h] || ''}">
      <span class="dot ${esc(s.regime_1h || '')}"></span>${esc(s.symbol.replace(/USDT$/, ''))}</button>`).join('');
}

function selectSymbol(sym, signalId = null) {
  state.symbol = sym;
  state.selected = signalId;
  renderChips();
  const chip = document.querySelector(`.chip[data-symbol="${sym}"]`);
  if (chip) chip.scrollIntoView({ inline: 'center', block: 'nearest' });
  loadChart().catch(showError);
}

// ---- signals view ------------------------------------------------------
async function loadSignals() {
  const sigs = await api('/api/signals?limit=100');
  $('#s-tickets').innerHTML = sigs.length
    ? sigs.map((s) => ticket(s, { extra: paperOutcome(s), clickable: true })).join('')
    : `<div class="empty"><b>هنوز سیگنالی صادر نشده.</b>موتور هر ۱۵ دقیقه ارزها را بررسی می‌کند و فقط وقتی دو سبک یا بیشتر هم‌نظر باشند سیگنال می‌دهد؛ ممکن است چند ساعت طول بکشد.</div>`;
}

// ---- paper view ----------------------------------------------------------
let eqChart, eqSeries;
function initEquity() {
  eqChart = LightweightCharts.createChart($('#equity'), {
    layout: { background: { color: C.surface }, textColor: C.muted, fontFamily: 'Vazirmatn, Tahoma, sans-serif' },
    grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(44,53,86,.45)' } },
    rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false, timeVisible: true },
  });
  eqSeries = eqChart.addAreaSeries({ lineColor: C.saffron, topColor: 'rgba(233,180,76,.28)', bottomColor: 'rgba(233,180,76,0)', lineWidth: 2 });
  new ResizeObserver(() => eqChart.applyOptions({ width: $('#equity').clientWidth, height: $('#equity').clientHeight })).observe($('#equity'));
}

async function loadPaper() {
  const p = await api('/api/paper');
  $('#p-start').classList.toggle('hidden', !!p.account);
  $('#p-main').classList.toggle('hidden', !p.account);
  if (!p.account) return;
  const init = p.account.initial_capital, diff = p.equity - init;
  $('#p-equity').textContent = `${money(p.equity)} USDT`;
  $('#p-delta').innerHTML = `<span class="${diff >= 0 ? 'pos' : 'neg'}">${money(diff, true)} (${((diff / init) * 100).toFixed(2)}%)</span>`;
  $('#p-since').textContent = `سرمایهٔ اولیه ${money(init)} از ${when(p.account.started_at)}`;
  const s = p.stats;
  $('#p-stats').innerHTML = [
    ['معاملات بسته', s.trades], ['وین‌ریت', pct(s.win_rate)], ['سود خالص', money(s.net_pnl, true)],
    ['امید ریاضی هر معامله', money(s.expectancy, true)], ['Profit Factor', s.profit_factor ?? '—'],
    ['حداکثر افت سرمایه', pct(s.max_drawdown_pct)], ['مارجین درگیر', money(p.margin_used)],
  ].map(([k, v]) => `<div><span>${k}</span><b class="num">${v}</b></div>`).join('');

  const pts = new Map();
  for (const c of p.curve) pts.set(Math.floor(c.t / 1000) + TZ, c.equity);
  const now = Math.floor(Date.now() / 1000) + TZ;
  if (now > Math.max(...pts.keys())) pts.set(now, p.equity);
  eqSeries.setData([...pts].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })));
  eqChart.timeScale().fitContent();

  $('#p-open-count').textContent = p.open.length ? `${p.open.length} از ${p.account.max_open}` : '';
  $('#p-open').innerHTML = p.open.length ? p.open.map((t) => {
    const u = t.unrealized;
    const extra = `<div class="t-facts" style="margin-top:10px">
        <span>ورود واقعی <b class="num">${fp(t.entry)}</b></span><span>اهرم <b class="num">${t.leverage}x</b></span>
        <span>مارجین <b class="num">${money(t.margin)}</b></span><span>ریسک <b class="num">${t.risk_pct}%</b></span>
        ${t.tp1_hit ? '<span><b>TP1 گرفته شد، حد ضرر روی ورود</b></span>' : ''}
        <span>سود / زیان باز <b class="num ${u >= 0 ? 'pos' : 'neg'}">${money(u, true)}</b></span></div>`;
    return ticket({ ...t, id: t.candidate_id, bar_time: t.opened_at - TF_MS['15m'], entry: t.entry, stop: t.stop_initial },
                  { price: t.mark, extra, clickable: true });
  }).join('') : '<div class="empty">الان پوزیشن بازی ندارید. سیگنال بعدی خودکار وارد می‌شود.</div>';

  $('#p-hist-count').textContent = p.history.length ? `${p.history.length}` : '';
  $('#p-hist').innerHTML = p.history.map((t) => {
    const res = t.status === 'skipped' ? `<span class="muted">ثبت نشد: ${esc(t.skip_reason)}</span>` : (EXIT_FA[t.exit_reason] || t.exit_reason);
    const pnl = t.status === 'skipped' ? '—' : `<span class="${t.realized > 0 ? 'pos' : 'neg'}">${money(t.realized, true)}</span>`;
    return `<tr data-symbol="${esc(t.symbol)}" data-id="${t.candidate_id}"><td>${t.opened_at ? when(t.opened_at) : '—'}</td><td>${esc(t.symbol)}</td>
      <td class="side ${sideCls(t.side)}">${sideFa(t.side)}</td><td class="num">${Math.round(t.score)}</td>
      <td class="num">${fp(t.entry ?? t.signal_entry)}</td><td class="num">${t.leverage ? t.leverage + 'x' : '—'}</td>
      <td>${res}</td><td class="num">${pnl}</td></tr>`;
  }).join('') || '<tr><td colspan="8" class="muted">هنوز معامله‌ای بسته نشده.</td></tr>';
}

async function startPaper(form, errEl) {
  const capital = Number(String(new FormData(form).get('capital')).replace(/,/g, ''));
  if (!(capital >= 10)) { if (errEl) errEl.textContent = 'سرمایه را به عدد و حداقل ۱۰ دلار وارد کنید.'; return; }
  await api('/api/paper/reset', { method: 'POST', body: JSON.stringify({ capital }) });
  await loadPaper();
}

// ---- backtest view -------------------------------------------------------
let btChart, btSeries;
const SIDE_KEY_FA = { long: 'خرید', short: 'فروش' };
const fr = (v, d = 2) => v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(d)}`;
function initBacktestChart() {
  btChart = LightweightCharts.createChart($('#b-equity'), {
    layout: { background: { color: C.surface }, textColor: C.muted, fontFamily: 'Vazirmatn, Tahoma, sans-serif' },
    grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(44,53,86,.45)' } },
    rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false },
  });
  btSeries = btChart.addAreaSeries({ lineColor: C.saffron, topColor: 'rgba(233,180,76,.28)', bottomColor: 'rgba(233,180,76,0)', lineWidth: 2 });
  new ResizeObserver(() => btChart.applyOptions({ width: $('#b-equity').clientWidth, height: $('#b-equity').clientHeight })).observe($('#b-equity'));
}
function rows(obj, label = (k) => k) {
  const e = Object.entries(obj || {});
  if (!e.length) return '<tr><td colspan="5" class="muted">بدون معامله</td></tr>';
  return e.map(([k, v]) => `<tr><td><bdi>${esc(label(k))}</bdi></td><td class="num">${v.n}</td><td class="num">${v.win_rate}%</td>
    <td class="num ${v.avg_r > 0 ? 'pos' : 'neg'}">${fr(v.avg_r, 3)}</td><td class="num ${v.total_r > 0 ? 'pos' : 'neg'}">${fr(v.total_r)}</td></tr>`).join('');
}
async function loadBacktests() {
  const runs = await api('/api/backtests');
  $('#b-empty').classList.toggle('hidden', runs.length > 0);
  $('#b-main').classList.toggle('hidden', runs.length === 0);
  $('#b-runs').classList.toggle('hidden', runs.length === 0);
  if (!runs.length) return;
  const sel = $('#b-runs');
  const cur = Number(sel.value) || runs[0].id;
  sel.innerHTML = runs.map((r) => `<option value="${r.id}" ${r.id === cur ? 'selected' : ''}>#${r.id} · ${when(r.created_at)} · ${r.days} روز</option>`).join('');
  await loadBacktest(cur);
}
async function loadBacktest(id) {
  const b = await api(`/api/backtests/${id}`);
  const s = b.summary, a = s.signal_r.all, p = s.portfolio;
  const days = Math.round((s.period.end - s.period.start) / 864e5);
  $('#b-lead').innerHTML = a.n
    ? `در ${days} روز روی ${s.symbols} ارز، <b class="num">${a.n}</b> سیگنال بسته شد. میانگین هر سیگنال <b class="num ${a.avg_r > 0 ? 'pos' : 'neg'}">${fr(a.avg_r, 2)}R</b> بود؛ یعنی با ریسک ثابت در هر معامله، جمع نتیجه <b class="num ${a.total_r > 0 ? 'pos' : 'neg'}">${fr(a.total_r, 1)}R</b> می‌شود.`
    : `در ${days} روز هیچ سیگنالی با قواعد فعلی صادر نشد.`;
  const ps = p.stats;
  $('#b-stats').innerHTML = [
    ['وین‌ریت سیگنال‌ها', a.n ? `${a.win_rate}%` : '—'], ['Profit Factor', a.profit_factor ?? '—'],
    [`حساب آزمایشی با ${money(p.capital)} دلار`, `${money(p.final_equity)}`], ['معاملات حساب', ps.trades],
    ['حداکثر افت سرمایه', pct(ps.max_drawdown_pct)],
  ].map(([k, v]) => `<div><span>${k}</span><b class="num">${v}</b></div>`).join('');
  const pts = new Map(b.curve.map((c) => [Math.floor(c.t / 1000) + TZ, c.equity]));
  btSeries.setData([...pts].sort((x, y) => x[0] - y[0]).map(([time, value]) => ({ time, value })));
  btChart.timeScale().fitContent();
  const split = (k, label) => { const v = s.signal_r[k]; return `<tr><td>${label}</td><td class="num">${v.n}</td><td class="num">${v.n ? v.win_rate + '%' : '—'}</td>
    <td class="num ${v.avg_r > 0 ? 'pos' : 'neg'}">${fr(v.avg_r, 3)}</td><td class="num">${fr(v.total_r)}</td><td class="num">${v.profit_factor ?? '—'}</td></tr>`; };
  $('#b-split').innerHTML = split('in_sample', 'داخل نمونه (اول دوره)') + split('out_of_sample', 'خارج نمونه (آخر دوره)');
  $('#b-style').innerHTML = rows(s.signal_r.by_style, (k) => STYLE_FA[k] || k);
  $('#b-score').innerHTML = rows(s.signal_r.by_score);
  $('#b-regime').innerHTML = rows(s.signal_r.by_regime, (k) => REGIME_FA[k] || k);
  $('#b-side').innerHTML = rows(s.signal_r.by_side, (k) => SIDE_KEY_FA[k] || k);
  $('#b-count').textContent = b.signals.length ? `${b.signals.length} مورد آخر` : '';
  $('#b-signals').innerHTML = b.signals.map((x) => `<tr data-symbol="${esc(x.symbol)}"><td>${when(x.bar_time + TF_MS['15m'])}</td><td>${esc(x.symbol)}</td>
    <td class="side ${sideCls(x.side)}">${sideFa(x.side)}</td><td class="num">${Math.round(x.score)}</td>
    <td>${(x.styles || []).filter((v) => v.core !== false).map((v) => STYLE_FA[v.style] || v.style).join('، ')}</td>
    <td>${x.status === 'closed' ? (EXIT_FA[x.exit_reason] || x.exit_reason) : x.status === 'open' ? 'باز' : 'پر نشد'}</td>
    <td class="num ${x.r > 0 ? 'pos' : x.r < 0 ? 'neg' : ''}">${x.status === 'closed' ? fr(x.r) : '—'}</td></tr>`).join('');
  $('#b-caveats').textContent = 'محدودیت‌ها: فهرست ارزها همان فهرست امروز است (ارزهایی که از فهرست خارج شده‌اند در آزمون نیستند)؛ قیمت‌ها از بایننس است؛ اگر یک کندل ۱۵ دقیقه‌ای هم حد ضرر و هم هدف را لمس کند، ضرر فرض شده است.';
}

// ---- shell ---------------------------------------------------------------
function showError(e) { if (e.message !== 'unauthorized') console.error(e); }

function showView(name) {
  document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', b.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => { v.hidden = v.id !== `view-${name}`; });
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  if (name === 'chart') loadChart(false).catch(showError);
  if (name === 'signals') loadSignals().catch(showError);
  if (name === 'paper') loadPaper().catch(showError);
  if (name === 'backtest') loadBacktests().catch(showError);
}

function bind() {
  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view)));
  $('#chips').addEventListener('click', (e) => { const c = e.target.closest('.chip'); if (c) selectSymbol(c.dataset.symbol); });
  $('#tf').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    state.tf = b.dataset.tf;
    document.querySelectorAll('#tf button').forEach((x) => x.setAttribute('aria-pressed', x === b));
    loadChart().catch(showError);
  });
  const openOnChart = (e) => {
    const el = e.target.closest('[data-symbol]'); if (!el || el.classList.contains('chip')) return;
    showView('chart'); selectSymbol(el.dataset.symbol, Number(el.dataset.id) || null);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  $('#c-tickets').addEventListener('click', (e) => {
    const t = e.target.closest('.ticket'); if (!t) return;
    state.selected = Number(t.dataset.id);
    candles.setMarkers(markers()); drawLines(state.signals.find((s) => s.id === state.selected));
    $('#chart').scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
  $('#s-tickets').addEventListener('click', openOnChart);
  $('#p-open').addEventListener('click', openOnChart);
  $('#p-hist').addEventListener('click', openOnChart);
  $('#b-signals').addEventListener('click', openOnChart);
  $('#b-runs').addEventListener('change', (e) => loadBacktest(Number(e.target.value)).catch(showError));
  $('#p-start-form').addEventListener('submit', (e) => { e.preventDefault(); startPaper(e.target, $('#p-start-err')).catch((x) => { $('#p-start-err').textContent = x.message; }); });
  $('#p-reset-form').addEventListener('submit', (e) => {
    e.preventDefault();
    if (confirm('تاریخچهٔ ترید مجازی پاک می‌شود و با سرمایهٔ جدید از نو شروع می‌شود. ادامه می‌دهید؟')) startPaper(e.target).catch((x) => alert(x.message));
  });
  window.addEventListener('hashchange', () => {
    const v = location.hash.slice(1);
    if (['chart', 'signals', 'paper', 'backtest'].includes(v)) showView(v);
  });
  $('#logout').addEventListener('click', async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; });
}

async function main() {
  bind();
  initChart();
  initEquity();
  initBacktestChart();
  const me = await api('/api/me');
  $('#who').textContent = me.username;
  state.symbols = await api('/api/symbols');
  state.symbol = state.symbols.length ? state.symbols[0].symbol : null;
  renderChips();
  const start = (location.hash || '#chart').slice(1);
  showView(['chart', 'signals', 'paper', 'backtest'].includes(start) ? start : 'chart');
  if (start !== 'chart') loadChart().catch(showError);
  // refresh the visible view every minute (candles close every 15 minutes, marks every 5)
  setInterval(() => {
    const v = document.querySelector('.tabs button[aria-selected="true"]').dataset.view;
    if (v === 'chart') loadChart(false).catch(showError);
    if (v === 'signals') loadSignals().catch(showError);
    if (v === 'paper') loadPaper().catch(showError);
  }, 60000);
}

main().catch(showError);
