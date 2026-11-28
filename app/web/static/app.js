/* Telec signal panel. Plain JS, Lightweight Charts v4 (global LightweightCharts). */
'use strict';

const C = { ink: '#151a2e', surface: '#1d2440', line: '#2c3556', text: '#ece8de', muted: '#9197ae',
            long: '#3bbf9a', short: '#ef6b6b', saffron: '#e9b44c' };
const TF_MS = { '15m': 9e5, '1h': 36e5, '4h': 144e5, '1d': 864e5 };
const TZ = -new Date().getTimezoneOffset() * 60; // chart shows the viewer's local time
const STYLE_FA = { htf_momentum: 'مومنتوم ۴ساعته', trend_pullback: 'پولبک روندی', breakout: 'شکست محدوده', mean_reversion: 'برگشت به میانگین',
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
  let data;
  try { data = await r.json(); } catch { throw new Error(`سرور پاسخ نداد (${r.status})`); }
  if (!r.ok) { const e = new Error(data.error || 'خطا'); e.data = data; e.status = r.status; throw e; }
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
const fmtPx = (v) => { const a = Math.abs(v); const d = a >= 1000 ? 1 : a >= 1 ? 4 : a >= 0.01 ? 5 : 8; return Number(v).toLocaleString('en-US', { maximumFractionDigits: d }); };
const fr = (v, d = 2) => v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(d)}`;
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
  return `<div class="ladder" aria-label="موقعیت قیمت نسبت به حد ضرر و اهداف" title="R یعنی فاصلهٔ ورود تا حد ضرر. ‎−1R‎ روی حد ضرر، ‎+1.5R‎ روی TP1 و ‎+3R‎ روی TP2 است.">
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

function signalOutcome(sig) {
  if (!sig.status || sig.status === 'new') return '<div class="t-result muted">منتظر ورود در کندل بعد</div>';
  if (sig.status === 'void') return '<div class="t-result muted">باطل شد: قیمت قبل از ورود از سطوح عبور کرد</div>';
  if (sig.status === 'active') return `<div class="t-result">فعال${sig.tp1_hit ? '؛ TP1 گرفته شد و حد ضرر روی ورود است' : ''}</div>`;
  const cls = sig.result_r > 0 ? 'pos' : sig.result_r < 0 ? 'neg' : 'muted';
  return `<div class="t-result">نتیجهٔ سیگنال: ${EXIT_FA[sig.exit_reason] || sig.exit_reason}، <b class="num ${cls}">${fr(sig.result_r)}R</b></div>`;
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
    ? sigs.map((s) => ticket(s, { price: state.lastPrice, extra: signalOutcome(s) + paperOutcome(s), clickable: true })).join('')
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
    ? sigs.map((s) => ticket(s, { extra: signalOutcome(s) + paperOutcome(s), clickable: true })).join('')
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
function rememberGlossary() {
  document.querySelectorAll('details.glossary').forEach((d, i) => {
    const k = `glossary-${i}`;
    try { const v = localStorage.getItem(k); if (v !== null) d.open = v === '1'; } catch {}
    d.addEventListener('toggle', () => { try { localStorage.setItem(k, d.open ? '1' : '0'); } catch {} });
  });
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
  $('#b-split').innerHTML = split('in_sample', 'داخل نمونه (اول دوره)') + split('out_of_sample', 'خارج نمونه (آخر دوره)')
    + (s.signal_r.unseen && s.signal_r.unseen.n ? split('unseen', `دیده‌نشده در طراحی (قبل از ${when(s.signal_r.unseen_before)})`) : '');
  $('#b-style').innerHTML = rows(s.signal_r.by_style, (k) => STYLE_FA[k] || k);
  $('#b-score').innerHTML = rows(s.signal_r.by_score);
  $('#b-regime').innerHTML = rows(s.signal_r.by_regime, (k) => REGIME_FA[k] || k);
  $('#b-side').innerHTML = rows(s.signal_r.by_side, (k) => SIDE_KEY_FA[k] || k);
  $('#b-count').textContent = b.signals.length ? `${b.signals.length} مورد آخر` : '';
  $('#b-signals').innerHTML = b.signals.map((x) => `<tr data-symbol="${esc(x.symbol)}"><td>${when(x.bar_time + TF_MS['15m'])}</td><td>${esc(x.symbol)}</td>
    <td class="side ${sideCls(x.side)}">${sideFa(x.side)}</td><td class="num">${Math.round(x.score)}</td>
    <td>${(x.styles || []).filter((v) => (v.role || (v.core === false ? 'confirm' : 'anchor')) !== 'info').map((v) => STYLE_FA[v.style] || v.style).join('، ')}</td>
    <td>${x.status === 'closed' ? (EXIT_FA[x.exit_reason] || x.exit_reason) : x.status === 'open' ? 'باز' : 'پر نشد'}</td>
    <td class="num ${x.r > 0 ? 'pos' : x.r < 0 ? 'neg' : ''}">${x.status === 'closed' ? fr(x.r) : '—'}</td></tr>`).join('');
  $('#b-caveats').textContent = 'محدودیت‌ها: فهرست ارزها همان فهرست امروز است (ارزهایی که از فهرست خارج شده‌اند در آزمون نیستند)؛ قیمت‌ها از بایننس است؛ اگر یک کندل ۱۵ دقیقه‌ای هم حد ضرر و هم هدف را لمس کند، ضرر فرض شده است.';
}

// ---- custom coins (admin) -----------------------------------------------
const CX_STATUS = { pending: 'در حال افزودن', active: 'فعال', invalid: 'نامعتبر' };
async function loadCustom() {
  const list = await api('/api/custom-symbols');
  $('#cx-list').innerHTML = list.length ? list.map((c) => `<li>
      <b class="num">${esc(c.symbol || c.input)}</b>
      <span class="st ${esc(c.status)}">${CX_STATUS[c.status] || c.status}</span>
      <small>${esc(c.note || (c.status === 'pending' ? 'تا ۲ دقیقه دیگر بررسی می‌شود' : ''))}</small>
      <button type="button" data-input="${esc(c.input)}" aria-label="حذف ${esc(c.input)}">×</button></li>`).join('')
    : '<li class="muted">هنوز ارزی اضافه نشده.</li>';
}
function bindCustom() {
  const dlg = $('#cx-dialog');
  $('#cx-open').addEventListener('click', () => { $('#cx-err').textContent = ''; dlg.showModal(); loadCustom().catch(showError); });
  $('#cx-close').addEventListener('click', () => { dlg.close(); refreshSymbols().catch(showError); });
  $('#cx-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = e.target.symbol;
    $('#cx-err').textContent = '';
    try {
      await api('/api/custom-symbols', { method: 'POST', body: JSON.stringify({ symbol: input.value }) });
      input.value = '';
      await loadCustom();
    } catch (x) { $('#cx-err').textContent = x.message; }
  });
  $('#cx-list').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-input]'); if (!b) return;
    await api(`/api/custom-symbols/${encodeURIComponent(b.dataset.input)}`, { method: 'DELETE' });
    await loadCustom();
  });
}
async function refreshSymbols() {
  state.symbols = await api('/api/symbols');
  renderChips();
}

// ---- telegram ------------------------------------------------------------
async function openTelegram() {
  const dlg = $('#tg-dialog'), body = $('#tg-body');
  dlg.showModal();
  body.innerHTML = '<p class="muted">در حال بارگذاری…</p>';
  const t = await api('/api/telegram');
  if (t.linked) {
    body.innerHTML = `<p class="linked"><i></i>به تلگرام ${t.telegram_name ? '<b>@' + esc(t.telegram_name) + '</b>' : ''} وصل است.</p>
      <p class="muted">سیگنال‌ها و به‌روزرسانی‌ها (TP1، حد ضرر، هدف نهایی) در تلگرام می‌آیند. ${t.notify ? '' : 'اعلان‌ها خاموش است؛ در بات /on را بفرستید.'}</p>
      <button type="button" class="btn quiet" id="tg-unlink">قطع اتصال</button>`;
    $('#tg-unlink').onclick = async () => { await api('/api/telegram/unlink', { method: 'POST' }); openTelegram(); };
    return;
  }
  if (!t.bot_username) {
    body.innerHTML = '<p>بات تلگرام هنوز روی سرور راه‌اندازی نشده است.</p><p class="muted">مدیر سیستم باید توکن بات را در تنظیمات سرور وارد کند.</p>';
    return;
  }
  body.innerHTML = `<ol><li>دکمهٔ زیر را بزنید تا تلگرام باز شود.</li><li>در بات، دکمهٔ Start را بزنید.</li><li>پیام «اتصال انجام شد» می‌آید.</li></ol>
    <button type="button" class="btn" id="tg-make">باز کردن بات و اتصال</button>
    <p class="muted small">لینک اتصال ۳۰ دقیقه اعتبار دارد و فقط برای حساب شماست.</p>`;
  $('#tg-make').onclick = async () => {
    try { const r = await api('/api/telegram/link', { method: 'POST' }); window.open(r.link, '_blank', 'noopener'); }
    catch (e) { body.insertAdjacentHTML('beforeend', `<p class="form-error">${esc(e.message)}</p>`); }
  };
}



// ---- live portfolio test -------------------------------------------------
const COMBO_FA = { combo_all_pos: 'ترکیب کامل (۱۸ سبک)', combo_top: 'ترکیب منتخب (۶ سبک)' };
const pf = { data: [], combo: 'combo_all_pos', chart: null, exec: null, ideal: null, bt: null, btChart: null, btExec: null, btIdeal: null };

function initPortfolioChart() {
  pf.chart = LightweightCharts.createChart($('#pf-chart'), {
    layout: { background: { color: C.surface }, textColor: C.muted, fontFamily: 'Vazirmatn, Tahoma, sans-serif' },
    grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(44,53,86,.45)' } },
    rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false },
  });
  pf.ideal = pf.chart.addLineSeries({ color: C.muted, lineWidth: 1, lineStyle: 2 });
  pf.exec = pf.chart.addAreaSeries({ lineColor: C.saffron, topColor: 'rgba(233,180,76,.25)', bottomColor: 'rgba(233,180,76,0)', lineWidth: 2 });
  new ResizeObserver(() => pf.chart.applyOptions({ width: $('#pf-chart').clientWidth, height: $('#pf-chart').clientHeight })).observe($('#pf-chart'));
  pf.btChart = LightweightCharts.createChart($('#pf-bt-chart'), {
    layout: { background: { color: C.surface }, textColor: C.muted, fontFamily: 'Vazirmatn, Tahoma, sans-serif' },
    grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(44,53,86,.45)' } },
    rightPriceScale: { borderVisible: false, mode: 1 }, timeScale: { borderVisible: false },
  });
  pf.btIdeal = pf.btChart.addLineSeries({ color: C.muted, lineWidth: 1, lineStyle: 2 });
  pf.btExec = pf.btChart.addLineSeries({ color: C.saffron, lineWidth: 2 });
  new ResizeObserver(() => pf.btChart.applyOptions({ width: $('#pf-bt-chart').clientWidth, height: $('#pf-bt-chart').clientHeight })).observe($('#pf-bt-chart'));
}

async function loadPortfolio() {
  [pf.data, pf.bt] = await Promise.all([api('/api/portfolio'), api('/api/portfolio/backtest').catch(() => null)]);
  $('#pf-empty').classList.toggle('hidden', pf.data.length > 0);
  $('#pf-main').classList.toggle('hidden', pf.data.length === 0);
  const combos = [...new Set(pf.data.map((b) => b.combo))];
  if (!combos.includes(pf.combo) && combos.length) pf.combo = combos[0];
  $('#pf-combo').innerHTML = combos.map((c) => `<button type="button" data-combo="${esc(c)}" aria-pressed="${c === pf.combo}">${esc(COMBO_FA[c] || c)}</button>`).join('');
  renderPortfolio();
}

function bookCard(b, label) {
  if (!b) return `<div class="pf-book ideal"><h2>${label}</h2><p class="muted">هنوز اجرا نشده</p></div>`;
  const diff = b.equity - b.capital, t = b.today || {};
  return `<div class="pf-book ${b.mode}"><h2>${label}</h2>
    <div><span class="big num">${money(b.equity)}</span> <span class="muted">USDT</span></div>
    <div class="pf-facts">
      <span class="${diff >= 0 ? 'pos' : 'neg'} num">${money(diff, true)} (${fr(b.return_pct)}%)</span>
      <span>بیشترین افت <b class="num">${Number(b.max_dd_pct).toFixed(2)}%</b></span>
      <span><b class="num">${b.days}</b> روز</span>
      ${b.mode === 'exec' ? `<span><b class="num">${t.n_long ?? 0}</b> خرید، <b class="num">${t.n_short ?? 0}</b> فروش</span>
      <span>اهرم کل <b class="num">${Number(t.gross ?? 0).toFixed(2)}x</b></span>` : ''}
    </div></div>`;
}

function renderPortfolio() {
  const exec = pf.data.find((b) => b.combo === pf.combo && b.mode === 'exec');
  const ideal = pf.data.find((b) => b.combo === pf.combo && b.mode === 'ideal');
  $('#pf-books').innerHTML = bookCard(exec, 'اجرایی روی ال‌بانک (۳٬۰۰۰ دلار)') + bookCard(ideal, 'ایده‌آل، مثل بک‌تست');
  const pts = (b) => (b ? b.curve.map((p) => ({ time: Math.floor(p.t / 1000), value: p.equity })) : []);
  pf.exec.setData(pts(exec)); pf.ideal.setData(pts(ideal)); pf.chart.timeScale().fitContent();
  renderPortfolioBacktest();
  const book = exec || ideal;
  const eq = book ? book.equity : 1;
  $('#pf-count').textContent = book ? book.positions.length : '';
  const t = exec && exec.today;
  $('#pf-note').textContent = book ? [
    `آخرین به‌روزرسانی: ${when(book.last_day + 86400000)}`,
    t && t.dropped ? `${t.dropped} پوزیشن پیشنهادی به خاطر نبودن در ال‌بانک یا کوچک‌تر بودن از حداقل سفارش حذف شد` : '',
    book.closed.length ? `امروز بسته شد: ${book.closed.map((s) => s.replace('USDT', '')).join('، ')}` : '',
  ].filter(Boolean).join(' · ') : '';
  const lastDay = book ? book.last_day : 0;
  $('#pf-pos').innerHTML = book && book.positions.length ? book.positions.map((p) => {
    const side = p.notional > 0 ? 1 : -1;
    let chg = '<span class="muted">—</span>';
    if (p.prev == null) chg = '<span class="tag new">جدید</span>';
    else if ((p.prev > 0) !== (p.notional > 0)) chg = '<span class="tag new">تغییر جهت</span>';
    else chg = `<span class="num">${fr(Math.abs(p.notional) - Math.abs(p.prev))}$</span>`;
    const held = p.since != null ? Math.round((lastDay - p.since) / 86400000) : null;
    const since = p.since != null ? `${new Date(p.since + 86400000).toLocaleDateString('fa-IR', { month: 'short', day: 'numeric' })}<span class="muted small"> · ${held === 0 ? 'امروز' : `${held} روز`}</span>` : '—';
    const pnlCls = p.pnl_pct > 0 ? 'pos' : p.pnl_pct < 0 ? 'neg' : '';
    const st = p.styles || [];
    const styles = st.length ? `<details class="pf-src"><summary>${esc(st[0][0])}<span class="muted small"> · ${esc(st[0][1])}${st.length > 1 ? ` · +${st.length - 1}` : ''}</span></summary>
      <ul>${st.map(([label, kind, sd, pct]) => `<li><span class="${sideCls(sd)}">${sideFa(sd)}</span> ${esc(label)} <span class="muted">(${esc(kind)})</span> <b class="num">${pct}%</b></li>`).join('')}</ul></details>`
      : '<span class="muted small">از اجرای امشب</span>';
    return `<tr><td><bdi>${esc(p.symbol.replace('USDT', ''))}</bdi></td>
      <td class="${sideCls(side)}">${sideFa(side)}</td><td>${styles}</td>
      <td class="num">${money(Math.abs(p.notional))}<span class="muted small"> · ${((Math.abs(p.notional) / eq) * 100).toFixed(1)}%</span></td>
      <td class="num">${p.entry != null ? fmtPx(p.entry) : '—'}</td><td>${since}</td>
      <td class="num">${p.price != null ? fmtPx(p.price) : '—'}</td>
      <td class="num ${pnlCls}">${p.pnl_pct != null ? fr(p.pnl_pct) + '%' : '—'}</td><td>${chg}</td></tr>`;
  }).join('') : '<tr><td colspan="9" class="muted">پوزیشن بازی نیست.</td></tr>';
}

function renderPortfolioBacktest() {
  const bt = pf.bt;
  $('#pf-bt').classList.toggle('hidden', !bt);
  if (!bt) return;
  const pts = (k) => (bt.curves[k] || []).map(([t, v]) => ({ time: Math.floor(t / 1000), value: v }));
  pf.btExec.setData(pts(`${pf.combo}:exec`)); pf.btIdeal.setData(pts(`${pf.combo}:ideal`)); pf.btChart.timeScale().fitContent();
  const s = bt.summary;
  $('#pf-bt-note').textContent = `اجرا شده ${when(bt.created_at)}. محور قیمت لگاریتمی است. «دورهٔ قفل» از ۲۰۲۵ به بعد است که موقع انتخاب سبک‌ها دیده نشده بود. `
    + `از ${s.coins} ارز بایننس، ${s.lbank_listed} ارز امروز در ال‌بانک هست؛ همین فهرست امروز برای کل گذشته به کار رفته.`;
  const PER = { discovery: 'تا ۲۰۲۴', holdout: 'دورهٔ قفل' };
  const MODES = [['exec', 'اجرایی'], ['ideal', 'ایده‌آل'], ['exec:stop25', 'اجرایی + استاپ ۲۵٪'], ['exec:stop40', 'اجرایی + استاپ ۴۰٪'], ['exec:stop60', 'اجرایی + استاپ ۶۰٪']]
    .filter(([k]) => s.books[`${pf.combo}:${k}`]);
  $('#pf-bt-rows').innerHTML = MODES.flatMap(([mode, label]) => ['discovery', 'holdout'].map((per) => {
    const book = s.books[`${pf.combo}:${mode}`] || {};
    const r = book[per] || {};
    const cls = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');
    const stops = per === 'discovery' && book.stops != null && mode.includes('stop') ? `<span class="muted small"> · ${book.stops} بار استاپ</span>` : '';
    return `<tr><td>${label}${stops}</td><td>${PER[per]}</td>
      <td class="num">${r.sharpe ?? '—'}</td><td class="num ${cls(r.cagr)}">${r.cagr ?? '—'}%</td>
      <td class="num neg">${r.max_dd ?? '—'}%</td><td class="num ${cls(r.total)}">${r.total ?? '—'}%</td></tr>`;
  })).join('');
}


// ---- Hyperliquid whales ---------------------------------------------------
const WH_SORTS = [['pnl_all', 'سود کل'], ['roi_all', 'بازده کل'], ['pnl_month', 'سود ماه'], ['roi_month', 'بازده ماه'],
  ['pnl_week', 'سود هفته'], ['pnl_day', 'سود امروز'], ['account_value', 'موجودی'], ['upnl', 'سود باز فعلی']];
const wh = { data: null, sort: 'pnl_all', open: null };
const usd = (v) => {
  if (v == null) return '—';
  const a = Math.abs(v), sgn = v < 0 ? '-' : '';
  if (a >= 1e9) return `${sgn}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sgn}$${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${sgn}$${(a / 1e3).toFixed(1)}K`;
  return `${sgn}$${a.toFixed(0)}`;
};
const roiPct = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`);
const tone = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');

async function loadWhales() {
  wh.data = await api('/api/whales');
  const has = wh.data.wallets.length > 0;
  $('#wh-empty').classList.toggle('hidden', has);
  $('#wh-main').classList.toggle('hidden', !has);
  if (!has) return;
  $('#wh-flow-at').textContent = wh.data.flow_at ? when(wh.data.flow_at) : '';
  const max = Math.max(1, ...wh.data.flow.map((f) => Math.max(f.long_notional, f.short_notional)));
  $('#wh-flow').innerHTML = wh.data.flow.slice(0, 20).map((f) => {
    const net = f.long_notional - f.short_notional;
    return `<tr><td><bdi>${esc(f.coin)}</bdi></td><td class="num pos">${f.n_long}</td><td class="num neg">${f.n_short}</td>
      <td class="num">${usd(f.long_notional)}</td><td class="num">${usd(f.short_notional)}</td>
      <td><div class="bar" title="${net >= 0 ? 'غلبهٔ خرید' : 'غلبهٔ فروش'} ${usd(Math.abs(net))}">${net >= 0
        ? `<i class="l" style="width:${(50 * net / max).toFixed(1)}%"></i>` : `<i class="s" style="width:${(50 * -net / max).toFixed(1)}%"></i>`}</div></td></tr>`;
  }).join('');
  renderWhales();
}

function renderWhales() {
  $('#wh-sort').innerHTML = WH_SORTS.map(([k, l]) => `<button type="button" data-k="${k}" aria-pressed="${k === wh.sort}">${l}</button>`).join('');
  document.querySelectorAll('.wh-table th[data-k]').forEach((th) => th.classList.toggle('sorted', th.dataset.k === wh.sort));
  const rows = [...wh.data.wallets].sort((a, b) => (b[wh.sort] ?? -Infinity) - (a[wh.sort] ?? -Infinity));
  $('#wh-rows').innerHTML = rows.map((w, i) => {
    const short = `${w.address.slice(0, 6)}…${w.address.slice(-4)}`;
    const dir = w.n_pos ? (w.net > 0 ? `<span class="pos">خالص خرید</span>` : w.net < 0 ? `<span class="neg">خالص فروش</span>` : '') : '';
    const row = `<tr class="wh-row" data-addr="${esc(w.address)}"><td class="num">${i + 1}</td>
      <td><a class="wh-addr" href="https://hypurrscan.io/address/${esc(w.address)}" target="_blank" rel="noopener noreferrer" dir="ltr">${esc(w.name || short)}</a></td>
      <td class="num">${usd(w.account_value)}</td><td class="num ${tone(w.pnl_all)}">${usd(w.pnl_all)}</td><td class="num ${tone(w.roi_all)}">${roiPct(w.roi_all)}</td>
      <td class="num ${tone(w.pnl_month)}">${usd(w.pnl_month)}</td><td class="num ${tone(w.roi_month)}">${roiPct(w.roi_month)}</td>
      <td class="num ${tone(w.pnl_week)}">${usd(w.pnl_week)}</td><td class="num ${tone(w.pnl_day)}">${usd(w.pnl_day)}</td>
      <td><span class="num">${w.n_pos}</span> <span class="small">${dir}</span></td><td class="num ${tone(w.upnl)}">${w.n_pos ? usd(w.upnl) : '—'}</td></tr>`;
    return row + (wh.open === w.address ? `<tr class="wh-detail"><td colspan="11" id="wh-detail">در حال بارگذاری…</td></tr>` : '');
  }).join('');
  if (wh.open) loadWhale(wh.open);
}

async function loadWhale(addr) {
  const pos = await api(`/api/whales/${addr}`);
  const cell = $('#wh-detail'); if (!cell) return;
  cell.innerHTML = pos.length ? `<table><thead><tr><th>ارز</th><th>جهت</th><th>اندازه</th><th>قیمت ورود</th><th>اهرم</th><th>سود باز</th></tr></thead><tbody>${
    pos.map((p) => `<tr><td><bdi>${esc(p.coin)}</bdi></td><td class="${sideCls(p.size > 0 ? 1 : -1)}">${sideFa(p.size > 0 ? 1 : -1)}</td>
      <td class="num">${usd(Math.abs(p.notional))}</td><td class="num">${p.entry_px != null ? fmtPx(p.entry_px) : '—'}</td>
      <td class="num">${p.leverage != null ? `${p.leverage}x` : '—'}</td><td class="num ${tone(p.upnl)}">${usd(p.upnl)}</td></tr>`).join('')}</tbody></table>`
    : '<span class="muted">در آخرین نمونه پوزیشن بازی نداشت.</span>';
}

function bindWhales() {
  const setSort = (k) => { if (!k) return; wh.sort = k; renderWhales(); };
  $('#wh-sort').addEventListener('click', (e) => setSort(e.target.closest('[data-k]')?.dataset.k));
  document.querySelector('.wh-table thead').addEventListener('click', (e) => setSort(e.target.closest('th[data-k]')?.dataset.k));
  $('#wh-rows').addEventListener('click', (e) => {
    if (e.target.closest('a')) return;
    const tr = e.target.closest('tr.wh-row'); if (!tr) return;
    wh.open = wh.open === tr.dataset.addr ? null : tr.dataset.addr;
    renderWhales();
  });
}


// ---- news --------------------------------------------------------------
const CAT_FA = { hack: 'هک و سرقت', delist: 'حذف از صرافی', war: 'سیاست و جنگ', macro: 'اقتصاد کلان', regulation: 'قانون‌گذاری',
  etf: 'ای‌تی‌اف و نهادی', listing: 'لیست شدن', market: 'بازار کریپتو', world: 'جهان' };
const NW_FILTERS = [['all', 'همه'], ['important', 'مهم'], ['verified', 'تأییدشده'], ['held', 'ارزهای پرتفوی'],
  ['hack', 'هک'], ['delist', 'حذف'], ['macro', 'اقتصاد کلان'], ['war', 'سیاست و جنگ'], ['regulation', 'قانون‌گذاری']];
const nw = { data: null, filter: 'all' };
const ago = (ms) => {
  const m = Math.max(0, Math.round((Date.now() - ms) / 60000));
  if (m < 60) return `${m} دقیقه پیش`;
  if (m < 1440) return `${Math.round(m / 60)} ساعت پیش`;
  return `${Math.round(m / 1440)} روز پیش`;
};

async function loadNews() {
  nw.data = await api('/api/news');
  const has = nw.data.stories.length > 0 || nw.data.events.length > 0;
  $('#nw-empty').classList.toggle('hidden', has);
  $('#nw-main').classList.toggle('hidden', !has);
  if (!has) return;
  const now = Date.now();
  $('#nw-events').innerHTML = nw.data.events.length ? nw.data.events.map((e) => {
    const d = new Date(e.ts);
    const when = d.toLocaleString('fa-IR', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tehran' });
    const nums = [e.actual ? `واقعی <b class="num">${esc(e.actual)}</b>` : '', e.forecast ? `پیش‌بینی <span class="num">${esc(e.forecast)}</span>` : '',
      e.previous ? `قبلی <span class="num">${esc(e.previous)}</span>` : ''].filter(Boolean).join(' · ');
    return `<div class="nw-ev ${esc(e.impact)}${e.ts < now ? ' past' : ''}"><span class="muted">${when} · ${esc(e.country_fa)}</span>
      <b>${esc(e.title_fa || e.title)}</b>${e.title_fa ? `<span class="muted small" dir="ltr">${esc(e.title)}</span>` : ''}${nums ? `<span class="small">${nums}</span>` : ''}</div>`;
  }).join('') : '<p class="muted small">رویداد مهمی ثبت نشده.</p>';
  const okSrc = nw.data.sources.filter((x) => x.ok).length;
  const bad = nw.data.sources.filter((x) => !x.ok).map((x) => x.name);
  $('#nw-sources').textContent = `منابع فعال: ${okSrc} از ${nw.data.sources.length}` + (bad.length ? ` · بدون پاسخ: ${bad.join('، ')}` : '');
  renderNews();
}

function renderNews() {
  $('#nw-filters').innerHTML = NW_FILTERS.map(([k, l]) => `<button type="button" data-k="${k}" aria-pressed="${k === nw.filter}">${l}</button>`).join('');
  const f = nw.filter;
  const list = nw.data.stories.filter((s) => f === 'all' || (f === 'important' && s.importance >= 4) || (f === 'verified' && s.verified)
    || (f === 'held' && s.held.length) || s.category === f);
  $('#nw-count').textContent = list.length;
  $('#nw-list').innerHTML = list.length ? list.slice(0, 150).map((s) => `<li class="nw-item i${s.importance}">
      <a class="t" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" dir="ltr">${esc(s.title)}</a>
      <div class="nw-meta"><span class="tag cat-${esc(s.category)}">${CAT_FA[s.category] || esc(s.category)}</span>
        ${s.verified ? `<span class="tag ok">تأییدشده</span>` : ''}
        ${s.coins.map((c) => `<span class="tag${s.held.includes(c) ? ' held' : ''}">${esc(c)}</span>`).join('')}
        <span>${ago(s.published)}</span>
        <span>${s.sources.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.name)}</a>`).join('، ')}</span>
        <span title="اهمیت از ۱ تا ۵">اهمیت <b class="num">${s.importance}</b></span></div></li>`).join('')
    : '<li class="muted">خبری با این فیلتر نیست.</li>';
}

// ---- server operations (admin) --------------------------------------------
const OPS_STATUS = { queued: 'در صف', running: 'در حال اجرا', done: 'تمام شد', failed: 'خطا', cancelled: 'لغو شد' };
const ops = { kinds: [], kind: null, job: null, timer: null };
const kindLabel = (k) => (ops.kinds.find((x) => x.key === k) || {}).label || k;

async function loadOps() {
  try {
    const r = await api('/api/ops/kinds');
    ops.kinds = r.kinds;
    $('#ops-down').classList.toggle('hidden', r.runner_alive);
  } catch (e) { $('#ops-down').classList.remove('hidden'); throw e; }
  const groups = {};
  ops.kinds.forEach((k) => { (groups[k.group] = groups[k.group] || []).push(k); });
  $('#ops-kinds').innerHTML = Object.entries(groups).map(([g, ks]) => `<section class="ops-group"><h3>${esc(g)}</h3><div>${
    ks.map((k) => `<button type="button" class="ops-kind${k.dangerous ? ' danger' : ''}" data-kind="${esc(k.key)}" aria-pressed="${ops.kind === k.key}"
      ${k.dangerous ? 'title="سرویس‌ها را جابه‌جا یا ری‌استارت می‌کند"' : ''}>${esc(k.label)}</button>`).join('')}</div></section>`).join('');
  await loadJobs();
}

function opsField(p) {
  const id = `ops-p-${p.name}`;
  let input;
  if (p.kind === 'choice') {
    input = `<select class="field" id="${id}" name="${esc(p.name)}">${p.choices.map(([v, l]) =>
      `<option value="${esc(v)}"${v === p.default ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
  } else if (p.kind === 'int') {
    input = `<input class="field num" id="${id}" name="${esc(p.name)}" type="number" inputmode="numeric" min="${p.lo}" max="${p.hi}" value="${p.default ?? ''}" required>`;
  } else if (p.kind === 'date') {
    input = `<input class="field num" id="${id}" name="${esc(p.name)}" type="date" ${p.optional ? '' : 'required'}>`;
  } else {
    input = `<input class="field num" id="${id}" name="${esc(p.name)}" dir="ltr" autocomplete="off" placeholder="BTCUSDT ETHUSDT">`;
  }
  const hint = p.kind === 'int' ? `<span class="small">از ${p.lo} تا ${p.hi}</span>` : '';
  return `<label for="${id}">${esc(p.label)}${input}${hint}</label>`;
}

function selectKind(key) {
  ops.kind = key;
  const k = ops.kinds.find((x) => x.key === key);
  document.querySelectorAll('.ops-kind').forEach((b) => b.setAttribute('aria-pressed', b.dataset.kind === key));
  $('#ops-form').hidden = false;
  $('#ops-f-title').textContent = k.label;
  $('#ops-f-help').textContent = k.help;
  $('#ops-fields').innerHTML = k.params.map(opsField).join('');
  $('#ops-err').textContent = '';
}

function confirmPassword() {
  const dlg = $('#ops-confirm'), form = $('#ops-confirm-form'), err = $('#ops-confirm-err');
  form.reset(); err.textContent = '';
  return new Promise((resolve) => {
    const done = (v) => { form.onsubmit = null; $('#ops-confirm-cancel').onclick = null; dlg.close(); resolve(v); };
    form.onsubmit = async (e) => {
      e.preventDefault();
      try { await api('/api/ops/confirm', { method: 'POST', body: JSON.stringify({ password: form.password.value }) }); done(true); }
      catch (x) { err.textContent = x.message; }
    };
    $('#ops-confirm-cancel').onclick = () => done(false);
    dlg.showModal();
  });
}

async function opsPost(path, body) {
  try { return await api(path, { method: 'POST', body: JSON.stringify(body) }); }
  catch (e) {
    if (e.data && e.data.need_confirm && await confirmPassword()) return api(path, { method: 'POST', body: JSON.stringify(body) });
    throw e;
  }
}

async function runKind(e) {
  e.preventDefault();
  const k = ops.kinds.find((x) => x.key === ops.kind); if (!k) return;
  const params = {};
  k.params.forEach((p) => {
    const v = e.target.elements[p.name].value.trim();
    if (v === '') return;
    params[p.name] = p.kind === 'int' ? Number(v) : p.kind === 'symbols' ? v.toUpperCase().split(/[\s,]+/).filter(Boolean) : v;
  });
  if (k.dangerous && !confirm(`«${k.label}» سرویس‌ها را ری‌استارت می‌کند و سایت چند ثانیه قطع می‌شود. ادامه می‌دهید؟`)) return;
  $('#ops-err').textContent = '';
  $('#ops-run').disabled = true;
  try {
    const r = await opsPost('/api/ops/jobs', { kind: k.key, params });
    await loadJobs();
    selectJob(r.id);
  } catch (x) { $('#ops-err').textContent = x.message; }
  finally { $('#ops-run').disabled = false; }
}

async function loadJobs() {
  const jobs = await api('/api/ops/jobs');
  $('#ops-list').innerHTML = jobs.length ? jobs.map((j) => `<li><button type="button" data-job="${j.id}" aria-current="${ops.job === j.id}">
      <span>#${j.id} · ${esc(kindLabel(j.kind))}</span><span class="ops-status ${esc(j.status)}">${OPS_STATUS[j.status] || esc(j.status)}</span>
      <small>${when(j.created_at)} · ${esc(j.requested_by || '—')}${Object.keys(j.params || {}).length ? ' · ' + esc(Object.entries(j.params).filter(([, v]) => v != null).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(' ') : v}`).join(' ')) : ''}</small>
    </button></li>`).join('') : '<li class="muted small">هنوز اجرایی نبوده است.</li>';
  if (!ops.job && jobs.length) selectJob(jobs[0].id);
}

async function selectJob(id) {
  ops.job = id;
  clearTimeout(ops.timer);
  document.querySelectorAll('#ops-list button').forEach((b) => b.setAttribute('aria-current', Number(b.dataset.job) === id));
  await pollJob();
}

async function pollJob() {
  const id = ops.job; if (!id) return;
  let j;
  try { j = await api(`/api/ops/jobs/${id}`); }
  catch (e) {  // the web service restarts during updates: keep trying
    $('#ops-c-status').textContent = 'در انتظار سرور…';
    ops.timer = setTimeout(pollJob, 3000); return;
  }
  if (ops.job !== id) return;
  const out = $('#ops-out');
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  $('#ops-c-title').textContent = `#${j.id} · ${kindLabel(j.kind)}`;
  const st = $('#ops-c-status'); st.className = `ops-status ${j.status}`; st.textContent = OPS_STATUS[j.status] || j.status;
  out.textContent = j.output || (j.status === 'queued' ? 'در صف اجرا…' : '');
  if (atBottom) out.scrollTop = out.scrollHeight;
  const live = j.status === 'queued' || j.status === 'running';
  $('#ops-cancel').classList.toggle('hidden', !live || j.cancel_requested);
  if (live) ops.timer = setTimeout(pollJob, 2000);
  else if (document.querySelector(`#ops-list button[data-job="${id}"] .ops-status.running, #ops-list button[data-job="${id}"] .ops-status.queued`)) loadJobs().catch(showError);
}

function bindOps() {
  $('#ops-kinds').addEventListener('click', (e) => { const b = e.target.closest('.ops-kind'); if (b) selectKind(b.dataset.kind); });
  $('#ops-form').addEventListener('submit', runKind);
  $('#ops-list').addEventListener('click', (e) => { const b = e.target.closest('[data-job]'); if (b) selectJob(Number(b.dataset.job)); });
  $('#ops-cancel').addEventListener('click', async () => {
    if (!ops.job || !confirm('این اجرا متوقف شود؟')) return;
    try { await opsPost(`/api/ops/jobs/${ops.job}/cancel`, {}); pollJob(); } catch (x) { alert(x.message); }
  });
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
  if (name === 'ops') loadOps().catch(showError);
  if (name === 'portfolio') loadPortfolio().catch(showError);
  if (name === 'whales') loadWhales().catch(showError);
  if (name === 'news') loadNews().catch(showError);
  if (name !== 'ops') clearTimeout(ops.timer);
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
    if (['chart', 'portfolio', 'whales', 'news', 'signals', 'paper', 'backtest', 'ops'].includes(v)) showView(v);
  });
  bindCustom();
  bindOps();
  bindWhales();
  $('#nw-filters').addEventListener('click', (e) => { const b = e.target.closest('[data-k]'); if (b) { nw.filter = b.dataset.k; renderNews(); } });
  $('#pf-combo').addEventListener('click', (e) => { const b = e.target.closest('[data-combo]'); if (!b) return; pf.combo = b.dataset.combo;
    document.querySelectorAll('#pf-combo button').forEach((x) => x.setAttribute('aria-pressed', x === b)); renderPortfolio(); });
  rememberGlossary();
  $('#tg-open').addEventListener('click', () => openTelegram().catch(showError));
  $('#logout').addEventListener('click', async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; });
}

async function main() {
  bind();
  initChart();
  initEquity();
  initBacktestChart();
  initPortfolioChart();
  const me = await api('/api/me');
  $('#who').textContent = me.username;
  $('#cx-open').classList.toggle('hidden', !me.is_admin);
  $('#ops-tab').classList.toggle('hidden', !me.is_admin);
  state.symbols = await api('/api/symbols');
  state.symbol = state.symbols.length ? state.symbols[0].symbol : null;
  renderChips();
  const start = (location.hash || '#portfolio').slice(1);
  showView(['chart', 'portfolio', 'whales', 'news', 'signals', 'paper', 'backtest', ...(me.is_admin ? ['ops'] : [])].includes(start) ? start : 'portfolio');
  if (start !== 'chart') loadChart().catch(showError);
  // refresh the visible view every minute (candles close every 15 minutes, marks every 5)
  setInterval(() => {
    const v = document.querySelector('.tabs button[aria-selected="true"]').dataset.view;
    if (v === 'chart') loadChart(false).catch(showError);
    if (v === 'signals') loadSignals().catch(showError);
    if (v === 'paper') loadPaper().catch(showError);
    if (v === 'whales') loadWhales().catch(showError);
    if (v === 'news') loadNews().catch(showError);
  }, 60000);
}

main().catch(showError);
