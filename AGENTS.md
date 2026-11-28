# Instructions for AI assistants working on this repository

Start by reading `docs/HANDOVER.md` (context, architecture, current state) and `docs/RESEARCH_LOG.md`
(every experiment and its result). Then:

- **Talk to the owner in Persian (Farsi).** Keep explanations plain and honest.
- Discuss significant designs before writing code; build once the owner agrees («بساز»).
- Never present a strategy as profitable without a causal backtest after fees and funding, on the
  survivorship-free universe, with results by year and a holdout that was not used for choices.
  The 2025–26 holdout is already spent (see HANDOVER §5).
- Do not change the frozen combination members in `app/portfolio/targets.py`; a change is a new strategy.
- Run `python -m pytest -q` before every commit; add tests (including causality tests) for new engines.
- The owner operates the server from the panel's «سرور» tab: any new script he must run should become a
  whitelisted job in `app/ops/jobs.py` (validated params only, never free text). New services go into
  `SERVICES` there and in `deploy/install.sh`, with a unit file in `deploy/`.
- Schema changes go into `db/schema.sql` and must be idempotent (`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`).
- Web: keep CSP-safe (no inline scripts or `on*=` handlers), escape all data with `esc()`, Persian RTL UI.
- Keep the 2 GB RAM server in mind (MemoryMax in unit files; daily-data panels are fine, avoid loading
  15m history for hundreds of coins at once).
