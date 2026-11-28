# Handover — read this first

This file lets anyone (a person or another AI) continue the project without the original chat.
Companion files: `docs/RESEARCH_LOG.md` (every experiment with numbers), `AGENTS.md` (working rules),
`README.md` (Persian overview for the owner).

State as of **2026-11-28** (commit history on `main` is complete and readable, ~40 commits).

---

## 1. The owner and what he wants

- Owner: Persian-speaking, runs the server himself; GitHub `taghvamohammad-art/telec-signal`.
  **Always answer him in Persian (Farsi).** He has asked again whenever replies slipped into English.
- He asked to **discuss designs fully before coding** at the start of the project; later he says "بساز"
  (build it) when he agrees. Keep explanations plain; he is not a quant. Explain metrics (R, Sharpe, drawdown)
  in simple words.
- Product: web system **«تلک»** at https://signal.telec.shop that analyses crypto markets and tells him what to
  trade, mostly **futures**, **both directions** (he explicitly rejected long-only), multi-user with
  username/password, Telegram delivery, a beautiful and easy Persian (RTL) UI.
- Prices/analysis from **Binance**; he trades on **LBank** (levels and availability are translated to LBank).
- Program decides risk; paper trading with user-set capital; backtests; admin can add custom coins.
- Later: automatic trading on LBank; news (crypto + macro + politics/war, verified, no Twitter, minimal AI —
  free for now, no AI summaries); USDT subscription payments (not now).
- He wants **real profitability**, and pushes to "find the treasure". Be honest: tell him when something has
  no edge, show why, and guard against overfitting (see §5). That honesty is why the current result is trusted.
- Paper capital agreed: **$3000** per book.

## 2. Server and deployment

- Ubuntu 24.04, 2 vCPU / 2 GB RAM (+swap), hostname `sg1`, Romania (Binance blocks US IPs).
- App directory `/opt/telec-signal` (owned by root), Python venv `.venv`, services run as user `telec`.
- PostgreSQL 16 (PGDG) + TimescaleDB, Nginx + Let's Encrypt, systemd units in `deploy/`.
- Fresh install: `deploy/install.sh` (idempotent; history of install problems is in git log).
- `.env` holds secrets and settings (template `.env.example`). Extra env flags: `SIGNALS_V2` (default 0 = off),
  `WHALES_ENABLED`, `LBANK_CHECK`, `TELEGRAM_BOT_TOKEN`, `ADMIN_BOT_TOKEN`, `ADMIN_CHAT_ID`, `DOMAIN`.

### Services (systemd)

| unit | module | what it does |
|---|---|---|
| telec-data | `app.data.runner` | universe (top-100 Binance perps that exist on LBank), candles 15m/1h/4h/1d via REST + WebSocket `/market`, funding, OI, health alerts |
| telec-analysis | `app.analysis.engine` | v2.1 regime analysis every 15m; **signal publishing off** unless `SIGNALS_V2=1` |
| telec-web | `app.web.main` (Starlette/uvicorn on 127.0.0.1:8000) | panel + JSON API |
| telec-bot | `app.bot.main` | Telegram bot: account linking, signal events, `bot_outbox` messages (portfolio daily summary, news alerts) |
| telec-whales | `app.whales.collector` | Hyperliquid whale wallets/positions/flow |
| telec-portfolio | `app.portfolio.service` | **the live paper test**, daily after 00:10 UTC |
| telec-news | `app.news.service` | RSS + Binance delistings every 5 min, economic calendar every 6 h |
| telec-ops | `app.ops.runner` (root) | runs whitelisted jobs queued from the panel's «سرور» tab |

### Operating without SSH

The admin «سرور» tab queues whitelisted jobs (`app/ops/jobs.py`): update (git pull, pip, install units,
restart), restart, status, logs, history download, every backtest, lab runs, portfolio run, news/whale
snapshots. The web process only queues *job kind + validated params*; the root runner rebuilds the commands
itself. A password re-check (15 min) is required, max 3 pending jobs, each job logged with user/IP and
announced to the admin Telegram chat.

Note: the runner imports `jobs.py` at start, so after a release that adds a new service or job,
**press "update" twice** (the second run uses the new list).

### Security measures in the web app

Same-origin + JSON content-type required for all changing API calls (CSRF), CSP (`script-src 'self'`),
HSTS, X-Frame-Options DENY, nosniff, no-store on API, sessions stored as SHA-256 hashes, scrypt passwords,
login rate limiting, admin-only routes, defusedxml for untrusted feeds.

## 3. Code map

```
app/
  config.py, alerts.py          settings from .env; admin Telegram alerts
  data/                         Binance REST/WS, LBank (ccxt), universe, candle validation, Store (asyncpg)
  analysis/                     v2.1 indicators, regime, styles, scoring, LBank check (signals off)
  backtest/                     replay of v2.1 over history (equivalence-tested with live evaluate())
  paper/, signals/              v2.1 paper trading and signal lifecycle (kept; idle while signals are off)
  bot/                          Telegram bot
  trend/                        v3 Donchian ensemble + backtest (§2 of research log)
  intraday/                     intraday momentum tests
  crowding/                     funding crowding tests
  lab/styles.py, lab/run.py     29 daily styles, discovery/holdout, deflated Sharpe, combinations
  portfolio/targets.py          FROZEN combo members + live target weights (+ which styles make each position)
  portfolio/book.py             paper book: mark, funding, rebalance, fees, entry tracking, emergency stop (tests only)
  portfolio/service.py          daily live run, LBank availability/minimums (cached), Telegram summary
  portfolio/backtest.py         same engine over history; exec vs ideal; stop variants
  whales/collector.py           Hyperliquid data
  news/classify.py, service.py  rule-based news
  ops/jobs.py, runner.py        server operations from the panel
  web/                          main.py (routes, hardening), repo.py (SQL), auth.py, static/ (index.html, app.js, app.css)
db/schema.sql                   whole schema, idempotent (applied by every service at start)
scripts/                        extend_history.py (--days --tf --universe analysis|all|binance-all --symbols), etc.
tests/                          124 tests; run `python -m pytest -q`
```

Panel tabs: نمودار (chart), پرتفوی (live paper test + its backtest), نهنگ‌ها (whales), اخبار (news),
بک‌تست (old v2.1 backtests), سرور (admin ops). The old «سیگنال‌ها» and «ترید مجازی» tabs are hidden.

## 4. What runs now (the strategy)

Two equal-risk combinations of daily styles (`app/portfolio/targets.py`), chosen by the lab from data up to
2024 and confirmed on 2025–26 (see research log §6–7):

- `combo_all_pos` (18 styles) — main candidate; holdout Sharpe 1.22 ideal / 1.17 on LBank with $3000.
- `combo_top` (6 styles) — holdout 1.16 ideal / 0.90 executable.

Each combination has two paper books of $3000: `exec` (LBank-listed coins only, LBank minimum order
sizes) and `ideal` (as in the backtest). Every night after the UTC daily close: refresh data for all
trading Binance perps → compute weights → mark to market with real funding → rebalance with 0.1% fees →
store day, positions (entry price, opening day, contributing styles) → Telegram summary.
No fixed TP/SL: exits happen when the nightly rebalance changes the target. Expected (from backtests):
~8–10%/yr with drawdowns < 10% at the current 15% vol target; gross exposure ~0.3–1x, net ~0.

Live paper test started on **2026-11-26** (first daily close processed). Decision about real money after
2–3 months of live results compared with the backtest.

## 5. Rules that keep the results honest (do not break)

1. Every engine must be causal; tests prove it (decisions at a close, fills at the next bar/day).
2. Use the survivorship-free universe (all Binance perps incl. delisted) for daily research.
3. The 2025–26 holdout has been used. Do **not** tune parameters, members, or rules on it.
   New ideas: declare them, then test on a *fresh* holdout (forward data from 2026-11-28).
4. Combination members are frozen in `targets.py`. Changing them = a new strategy = a new test.
5. Report results after fees and funding, by year, discovery vs holdout. Show negative results too.
6. Things already tested and rejected (don't re-propose without new evidence): v2.1 15m signals, intraday
   momentum at retail fees, funding carry/contrarian, per-position emergency stops, the 23 failing lab styles.

## 6. Roadmap / open items

- Wait 2–3 months of live paper data; compare with `portfolio_backtest` numbers.
- Then: LBank auto-trading for `combo_all_pos` (nightly rebalance via API, start small, run beside the paper
  book), with an **account-level brake** (pause + alert at −15% from peak). Exchange leverage 2–3x cross is
  enough (system gross exposure < 1x).
- Possibly a second paper book at 2x risk to show the risk/return trade-off.
- After ~3 months of data: test whale positioning and news as new styles/filters on a fresh holdout.
- Optional: AI Persian news summaries (owner chose free/no-AI for now); USDT subscriptions; multi-user signal
  product built on the portfolio.

## 7. Known caveats

- Executable-book backtest uses today's LBank list for the past (slightly optimistic).
- LBank's ccxt spot endpoint times out sometimes; the code skips spot markets, retries, and falls back to the
  last saved market list (`app_settings.lbank_markets_cache`).
- Binance WebSocket must use `wss://fstream.binance.com/market`; Binance blocks US IPs (HTTP 451).
- Feeds that stop answering are listed as «بدون پاسخ» at the bottom of the news tab.
