# Research log — every strategy tested, with results and conclusions

All tests use Binance USDⓈ-M perpetual data (prices are later translated to LBank for execution),
real funding history, fees of 0.06–0.10% per side, and only information available at decision time
(each engine has a causality test in `tests/`). Results are after costs unless stated.

Dates below are 2026-11-27/28 (the whole project was built in about two days).

---

## Summary of what was learned

| # | Approach | Verdict |
|---|---|---|
| 1 | v2 / v2.1 multi-style 15m signal system (pullback, breakout, 4h momentum + confirmers) | **No edge.** Average ≈ 0R per signal. Switched off. |
| 2 | v3 Donchian ensemble trend, daily, long-only (Zarattini et al. 2025) | Small edge, Sharpe ≈ BTC buy-and-hold, flat in 2025–26. Short side loses. Long-only rejected by the user. |
| 3 | Intraday momentum (noise-area breakout, half-hour momentum) | **Gross edge exists (Sharpe 0.93 without costs) but fees destroy it.** Dead at retail fees. |
| 4 | Funding crowding: cross-sectional carry and time-series contrarian | Looked good with today's coins only; **with dead coins included the edge disappears after 2021.** |
| 5 | Strategy lab: 29 daily styles, survivorship-free, discovery/holdout split, deflated Sharpe | No single style passes the luck test. Best: `btc_ma50`, `mom_cond`, `fund_diverge`, `tsmom_ens`. |
| 6 | **Equal-risk combinations of lab styles** | **All three combinations stayed profitable on the untouched 2025–26 holdout (Sharpe 0.87–1.22).** This is what runs live now (paper). |
| 7 | Portfolio engine backtest ($3000, LBank-only coins, real minimum order sizes) | `combo_all_pos` executable holdout Sharpe 1.17 vs 1.29 ideal → $3000 is enough. |
| 8 | Emergency per-position stop (25/40/60% vs entry) | **Hurts every variant** (holdout Sharpe 1.17 → 0.82–0.85, deeper drawdowns). Not used. |

The key methodological lesson: price-only signals that look good on recent data usually come from
luck or survivorship bias. What survived was a *diversified mix* of weak, independent daily styles.

---

## 1. v2 → v2.1 signal system (15m, multi-timeframe)

Code: `app/analysis/*`, backtest `app/backtest/*` (`python -m app.backtest.run --days N`).

Design: regime on 1h (trend / range / squeeze / chaotic), bias from 4h; styles vote; roles:
anchors (trend_pullback, breakout, htf_momentum), confirmers (liquidity_sweep, vwap_volume),
info-only (divergence, mean_reversion). Rules: one anchor with strength ≥ 0.6 and at least two agreeing
styles, no opposing anchor, score ≥ 70, stop 0.5–3 ATR(1h), TP1 1.5R (half off, stop to entry), TP2 3R,
no trades in RANGE/CHAOTIC, fixed 1% risk. LBank check before publishing (price diff ≤ 0.5%, spread ≤ 0.2%,
depth ≥ $5000 within 0.5%).

Results:
- v2 (120 days): −0.044R per signal.
- v2.1 (400 days, 40 coins, 6922 signals): −0.003R per signal overall; in-sample ≈ 0, out-of-sample −0.075R,
  unseen period +0.016R. Paper portfolio $1000 → $895, max drawdown 41%. Breakdowns by style/regime
  flipped sign between runs → noise.

Conclusion: no edge. Publishing is now **off** (`SIGNALS_V2=0` default; `SIGNALS_V2=1` turns it back on).
The analysis service still computes regimes for the chart.

## 2. v3 trend (Donchian ensemble, daily)

Code: `app/trend/ensemble.py`, `app/trend/backtest.py`. Reference: Zarattini, Pagani & Barbon (2025),
"Catching Crypto Trends", SSRN 5209907. Lookbacks 5–360 days, ratcheting midpoint stop, 25% vol target,
top-N liquid coins.

Result (99 coins, 2019-12 → 2026-11, backtest #3):

| variant | CAGR% | vol% | Sharpe | maxDD% |
|---|---|---|---|---|
| long_top20 | 5.24 | 6.08 | 0.87 | −9.21 |
| long_top100 | 2.06 | 2.33 | 0.89 | −3.68 |
| short_top20 | −2.71 | 6.79 | −0.37 | −23.61 |
| both_top20 | 2.48 | 7.92 | 0.35 | −14.67 |
| BTC hold | 36.12 | 59.98 | 0.82 | −76.67 |

Per year long_top20: 2020 +7.9, 2021 +16.1, 2022 −3.9, 2023 +12.5, 2024 +7.3, 2025 −1.9, 2026 0.0.
Note: realised vol 6% vs 25% target (sizing divides by top_n) — Sharpe is unaffected.
Conclusion: Sharpe ≈ BTC; shorts lose; flat lately. User rejected long-only.

## 3. Intraday momentum (15m, 6 majors, 2020-11 → 2026-11, backtest #4)

Code: `app/intraday/noise.py` (Zarattini, Aziz & Barbon 2024 noise-area breakout, adapted to 24h UTC days)
and half-hour momentum.

- Noise-area breakout, default: CAGR −52%, Sharpe −2.49 (≈ 80 trades/month/coin). All 6 settings negative.
- **Same strategy without costs: CAGR +24.7%, Sharpe 0.93** → the pattern is real but small; 0.07%/side kills it.
- Half-hour momentum (first 30m → last 30m, or day-so-far → last 30m): Sharpe −8.7 / −6.7.

Conclusion: dead at retail fees.

## 4. Funding crowding (daily)

Code: `app/crowding/strategy.py`, `app/crowding/backtest.py`.

Cross-sectional: short the top 20% by recent funding, long the bottom 20% (market neutral, inverse-vol).
Time-series: fade a coin's own extreme funding z-score.

- With **today's 100 coins only** (backtest #5): XS Sharpe 0.94–1.26 in all 6 settings. Suspicious: profit came
  mostly from the long leg (lowest-funding coins), which are often dying coins — excluded by survivorship.
- With **all 647 Binance perps incl. delisted** (`--universe everything`, backtest #6): full period still
  Sharpe 0.71–1.23 but first half 1.84 vs second half −0.06. **Since 2022 (backtest #7): Sharpe 0.10–0.77,
  default 0.10**; years 2023 −5.7, 2025 −1.0, 2026 −24.0.
- Funding *carry* itself is large (+31–44%/yr since 2022) but adverse price moves eat it.
- Time-series contrarian: ≈ 0 in all settings.

Conclusion: no robust edge; survivorship bias was the illusion. Universe for all later work = all perps ever listed.

## 5. Strategy lab (daily, 647 coins incl. delisted)

Code: `app/lab/styles.py`, `app/lab/run.py` (`python -m app.lab.run [--reveal key ...]`).

**Discipline, fixed before any result was seen:**
- discovery = start … 2024-12-31; holdout = 2025-01-01 … now. Holdout shown only for styles that pass,
  or for candidates declared *before* the run with `--reveal`.
- pass = Sharpe ≥ 0.8 after costs, ≥ 60% of calendar years positive, Deflated Sharpe Ratio ≥ 0.90
  (Bailey & López de Prado 2014; spread of trial Sharpes estimated robustly with MAD from batch 2 on).
- fees 0.1% per unit traded + real funding; universe ranked daily by past 30-day volume (top 100).

Batch 1 (17 styles) and batch 2 (12 styles), discovery period (backtest #11/#12):

| style | Sharpe | CAGR% | maxDD% | years+ % |
|---|---|---|---|---|
| xs_mom_7 (7d cross-sectional momentum) | 0.59 | 12.5 | −48.9 | 40 |
| xs_mom_28 | 0.28 | 4.0 | −43.8 | 60 |
| xs_rev_1 / xs_rev_7 (short-term reversal) | −2.29 / −1.28 | | | 0 |
| lowvol | −0.31 | | | 40 |
| size (small minus big) | 0.67 | 12.0 | −36.3 | 40 |
| attention (abnormal volume) | 0.63 | 12.9 | −29.5 | 60 |
| bab (betting against beta) | −0.06 | | | 20 |
| max (lottery effect) | −0.44 | | | 20 |
| new_listing (short new listings vs BTC) | 0.37 | 7.3 | −65.8 | 80 |
| tsmom_14 (per-coin trend, both sides) | 0.71 | 8.0 | −14.5 | 100 |
| tsmom_60 | 0.48 | 5.2 | −19.1 | 80 |
| tsmom_regime (shorts only when BTC < 200d MA) | 0.89 | 10.3 | −16.7 | 80 |
| btc_ma50 (BTC long/short vs 50d MA) | 1.03 | 52.2 | −54.1 | 80 |
| btc_weekend | 0.01 | | | 40 |
| alt_rotation | −0.22 | | | 40 |
| carry_neutral (idealised funding arbitrage) | 4.15 | 12.3 | −10.8 | 60 |
| fund_change | 0.54 | 8.2 | −38.8 | 60 |
| fund_diverge (price up while funding falls) | 1.08 | 21.5 | −40.8 | 60 |
| high52 | 0.48 | 9.1 | −49.8 | 50 |
| resid_mom | 0.31 | 4.8 | −36.9 | 60 |
| ivol / skew | −0.18 / −0.55 | | | |
| mom_cond (7d momentum only while BTC > 50d MA) | 1.19 | 25.9 | −25.0 | 60 |
| tsmom_ens (7–120d trend ensemble, both sides) | 0.83 | 7.1 | −10.1 | 100 |
| squeeze_break | 0.70 | 10.4 | −17.6 | 80 |
| btc_volman | 0.56 | 17.3 | −67.8 | 60 |
| mkt_funding / ethbtc_revert | −0.40 / −0.95 | | | |

Only `carry_neutral` passed the rule, and it **failed the holdout** (Sharpe −2.61, 2025 −19.7%): funding fell
and weekly rotation fees (11%/yr) exceed the carry. Its discovery Sharpe was inflated by the perfect-hedge
assumption.

Pre-declared near-misses, holdout: btc_ma50 Sharpe 1.03 (CAGR 50%, maxDD −37%, 2025 −6%, 2026 +131%),
tsmom_14 0.50, tsmom_regime 0.13.

## 6. Combinations (the result that runs live)

`pick_combos` chooses members from discovery data only; each member is scaled to 15% vol using its own
past 60-day vol, then averaged (`app/lab/styles.combine`). Frozen members are in `app/portfolio/targets.py`.

- combo_top: mom_cond, btc_ma50, tsmom_14, size, attention, fund_change
- combo_trend: tsmom_14, tsmom_60, tsmom_regime, tsmom_ens, btc_ma50, btc_volman
- combo_all_pos: the 18 styles with positive discovery Sharpe (see targets.py)

| combo | discovery Sharpe | **holdout Sharpe** | holdout CAGR | holdout maxDD | 2025 | 2026 |
|---|---|---|---|---|---|---|
| combo_top | 1.37 | **1.16** | 9.7% | −6.3% | +3.9 | +14.7 |
| combo_trend | 1.06 | **0.87** | 11.5% | −10.6% | +0.3 | +22.7 |
| combo_all_pos | 1.22 | **1.22** | 9.1% | −4.1% | +8.8 | +8.6 |

BTC correlation ≈ 0; 2022 (BTC −64%) ≈ flat. DSR still below 0.9 (members were selected from 29), so the
holdout is the real evidence. **The holdout has now been used — do not tune anything on 2025–26 data.**
New ideas need a fresh holdout (e.g. live forward data from 2026-11-28 on).

## 7. Portfolio engine backtest (backtest #13/#14)

Code: `app/portfolio/backtest.py`. Same `Book` class as the live paper test, $3000, fees 0.1%, real funding.
Executable book = only coins listed on LBank **today** (433 of 647; slight look-ahead) and orders below
LBank's minimum dropped.

| book | discovery Sharpe | holdout Sharpe | holdout CAGR | holdout maxDD |
|---|---|---|---|---|
| combo_all_pos exec | 1.25 | 1.17 | 8.0% | −3.9% |
| combo_all_pos ideal | 1.27 | 1.29 | 9.7% | −4.0% |
| combo_top exec | 1.14 | 0.90 | 6.9% | −7.3% |
| combo_top ideal | 1.40 | 1.19 | 10.0% | −6.2% |

~48 positions/day held, ~38/day dropped (LBank or minimum size) for combo_all_pos; funding was net received.
Gross exposure is low (live day 2: 0.31x) because the target is 15% vol and the book is long/short.

## 8. Emergency stop (backtest #14)

Close a position when price moves 25/40/60% against its entry (checked with daily high/low; fill at stop
or gapped open; no re-entry in that direction for 7 days).

| combo_all_pos exec | holdout Sharpe | CAGR | maxDD | stops in 7 yrs |
|---|---|---|---|---|
| no stop | 1.17 | 8.0% | −3.9% | 0 |
| stop 60% | 0.85 | 5.9% | −5.6% | 169 |
| stop 40% | 0.85 | 5.8% | −5.5% | 473 |
| stop 25% | 0.82 | 5.5% | −5.6% | 1475 |

Positions are held for weeks and shrink at each nightly rebalance; slow drifts of 40–60% are not
disasters, and stopping them out at the worst point plus a cooldown loses the rebound.
Decision: no per-position stop. Planned instead for real money: an account-level brake (pause new trades and
alert if equity falls 15% from its peak — never triggered in 7 years of backtest).

## 9. Data collected for future tests (not used in trading yet)

- Hyperliquid whales (`app/whales/collector.py`, since 2026-11-27): 200 wallets/day chosen from the public
  leaderboard (all-time PnL ≥ $250k, profitable this month, account ≥ $100k, not market makers), per-coin
  long/short counts every 15 min, full positions hourly. First snapshot: whales heavily short BTC
  (56 short / 15 long, $405M vs $43M). Test after ~3 months with a fresh holdout.
- News (`app/news`, from 2026-11-28): categories, importance, cross-source verification. Only informational;
  could become a filter after months of data.
