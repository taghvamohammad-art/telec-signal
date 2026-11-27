-- telec-signal schema. Safe to run more than once.
-- Works on plain PostgreSQL; uses TimescaleDB features when the extension is available.

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'timescaledb not available, using plain tables';
END $$;

CREATE TABLE IF NOT EXISTS candles (
    symbol        text             NOT NULL,
    tf            text             NOT NULL,
    open_time     timestamptz      NOT NULL,
    open          double precision NOT NULL,
    high          double precision NOT NULL,
    low           double precision NOT NULL,
    close         double precision NOT NULL,
    volume        double precision NOT NULL,
    quote_volume  double precision NOT NULL,
    trades        integer          NOT NULL,
    received_at   timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, tf, open_time)
);

-- Daily snapshot of the universe. Backtests rebuild "top 100" from this table
-- for each past day, so coins that later dropped out are not silently ignored.
CREATE TABLE IF NOT EXISTS universe_snapshots (
    day               date             NOT NULL,
    rank              integer          NOT NULL,
    symbol            text             NOT NULL,
    base              text             NOT NULL,
    multiplier        integer          NOT NULL,
    quote_volume_24h  double precision NOT NULL,
    range_pct_24h     double precision NOT NULL,
    on_lbank          boolean          NOT NULL,
    lbank_symbol      text,
    in_analysis       boolean          NOT NULL DEFAULT false,
    PRIMARY KEY (day, symbol)
);

CREATE TABLE IF NOT EXISTS funding_rates (
    symbol      text             NOT NULL,
    ts          timestamptz      NOT NULL,
    rate        double precision NOT NULL,
    mark_price  double precision NOT NULL,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS open_interest (
    symbol  text             NOT NULL,
    ts      timestamptz      NOT NULL,
    oi      double precision NOT NULL,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS data_issues (
    id      bigserial   PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    symbol  text,
    tf      text,
    kind    text        NOT NULL,
    detail  jsonb
);
CREATE INDEX IF NOT EXISTS data_issues_ts_idx ON data_issues (ts DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('candles', 'open_time', chunk_time_interval => interval '7 days', if_not_exists => true, migrate_data => true);
        PERFORM create_hypertable('funding_rates', 'ts', chunk_time_interval => interval '30 days', if_not_exists => true, migrate_data => true);
        PERFORM create_hypertable('open_interest', 'ts', chunk_time_interval => interval '7 days', if_not_exists => true, migrate_data => true);
        BEGIN
            ALTER TABLE candles SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol, tf');
            PERFORM add_compression_policy('candles', interval '14 days', if_not_exists => true);
            ALTER TABLE open_interest SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol');
            PERFORM add_compression_policy('open_interest', interval '14 days', if_not_exists => true);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'compression not enabled: %', SQLERRM;
        END;
    END IF;
END $$;

-- ---- step 2: analysis --------------------------------------------------
-- Every signal that passed the analysis engine. Step 3 (risk/LBank) and
-- step 4 (lifecycle/Telegram) take candidates from here.
CREATE TABLE IF NOT EXISTS signal_candidates (
    id          bigserial        PRIMARY KEY,
    created_at  timestamptz      NOT NULL DEFAULT now(),
    bar_time    timestamptz      NOT NULL,          -- open time of the 15m bar that triggered it
    symbol      text             NOT NULL,
    side        smallint         NOT NULL,          -- 1 long, -1 short
    score       double precision NOT NULL,
    regime_1h   text             NOT NULL,
    bias_4h     smallint         NOT NULL,
    entry       double precision NOT NULL,
    stop        double precision NOT NULL,
    tp1         double precision NOT NULL,
    tp2         double precision NOT NULL,
    risk        double precision NOT NULL,
    f_market    double precision NOT NULL,
    f_news      double precision NOT NULL,
    styles      jsonb            NOT NULL,
    notes       jsonb            NOT NULL DEFAULT '[]',
    version     text             NOT NULL,
    status      text             NOT NULL DEFAULT 'new',
    UNIQUE (symbol, bar_time)
);
CREATE INDEX IF NOT EXISTS signal_candidates_created_idx ON signal_candidates (created_at DESC);

-- Latest analysis per symbol (for the panel and for debugging "why no signal").
CREATE TABLE IF NOT EXISTS analysis_state (
    symbol      text        PRIMARY KEY,
    updated_at  timestamptz NOT NULL,
    bar_time    timestamptz NOT NULL,
    regime_1h   text        NOT NULL,
    bias_4h     smallint    NOT NULL,
    votes       jsonb       NOT NULL,
    result      text        NOT NULL
);

-- ---- users, sessions, paper trading -----------------------------------
CREATE TABLE IF NOT EXISTS users (
    id             serial      PRIMARY KEY,
    username       text        NOT NULL UNIQUE,
    password_hash  text        NOT NULL,
    is_admin       boolean     NOT NULL DEFAULT false,
    active         boolean     NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    token       text        PRIMARY KEY,
    user_id     integer     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_accounts (
    id               serial           PRIMARY KEY,
    user_id          integer          NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    initial_capital  double precision NOT NULL,
    started_at       timestamptz      NOT NULL DEFAULT now(),
    fee_rate         double precision NOT NULL DEFAULT 0.0006,
    max_open         integer          NOT NULL DEFAULT 5,
    max_same_side    integer          NOT NULL DEFAULT 3
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id            bigserial        PRIMARY KEY,
    account_id    integer          NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
    candidate_id  bigint           NOT NULL REFERENCES signal_candidates(id) ON DELETE CASCADE,
    symbol        text             NOT NULL,
    side          smallint         NOT NULL,
    status        text             NOT NULL,       -- open | closed | skipped
    skip_reason   text,
    score         double precision NOT NULL,
    signal_entry  double precision NOT NULL,
    entry         double precision,
    qty           double precision,
    qty_open      double precision,
    leverage      integer,
    margin        double precision,
    risk_pct      double precision,
    risk_amount   double precision,
    stop_initial  double precision NOT NULL,
    stop          double precision NOT NULL,
    tp1           double precision NOT NULL,
    tp2           double precision NOT NULL,
    tp1_hit       boolean          NOT NULL DEFAULT false,
    realized      double precision NOT NULL DEFAULT 0,
    fees          double precision NOT NULL DEFAULT 0,
    funding       double precision NOT NULL DEFAULT 0,
    opened_at     timestamptz,
    closed_at     timestamptz,
    exit_reason   text,
    last_bar      timestamptz,                      -- last processed 15m open time
    events        jsonb            NOT NULL DEFAULT '[]',
    note          text,
    UNIQUE (account_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS paper_trades_account_idx ON paper_trades (account_id, status);

-- ---- backtests ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_runs (
    id          serial      PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    params      jsonb       NOT NULL,
    summary     jsonb       NOT NULL,
    curve       jsonb       NOT NULL,
    signals     jsonb       NOT NULL,
    trades      jsonb       NOT NULL
);

-- ---- LBank translation of each signal ---------------------------------
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_symbol     text;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_price      double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_diff_pct   double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_spread_pct double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_depth_usdt double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_entry      double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_stop       double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_tp1        double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS lbank_tp2        double precision;

-- ---- signal lifecycle (global, independent of paper accounts) ----------
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS fill_price  double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS stop_now    double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS tp1_hit     boolean NOT NULL DEFAULT false;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS last_bar    timestamptz;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS exit_reason text;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS closed_at   timestamptz;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS result_r    double precision;
ALTER TABLE signal_candidates ADD COLUMN IF NOT EXISTS published   boolean;   -- sent to Telegram (false = over daily cap)

CREATE TABLE IF NOT EXISTS signal_events (
    id            bigserial   PRIMARY KEY,
    candidate_id  bigint      NOT NULL REFERENCES signal_candidates(id) ON DELETE CASCADE,
    kind          text        NOT NULL,   -- new | active | tp1 | tp2 | stop | breakeven | void
    created_at    timestamptz NOT NULL DEFAULT now(),
    payload       jsonb       NOT NULL DEFAULT '{}',
    sent_at       timestamptz,
    UNIQUE (candidate_id, kind)
);
CREATE INDEX IF NOT EXISTS signal_events_unsent_idx ON signal_events (id) WHERE sent_at IS NULL;

-- ---- Telegram linking ----------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id  bigint UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_name     text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_link_code      text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_link_expires   timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify            boolean NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS app_settings (
    key    text PRIMARY KEY,
    value  text NOT NULL
);

-- ---- coins added by the admin ------------------------------------------
CREATE TABLE IF NOT EXISTS custom_symbols (
    input       text        PRIMARY KEY,         -- what the admin typed, upper case
    symbol      text,                            -- resolved Binance symbol
    status      text        NOT NULL DEFAULT 'pending',   -- pending | active | invalid
    note        text,
    added_by    integer     REFERENCES users(id) ON DELETE SET NULL,
    added_at    timestamptz NOT NULL DEFAULT now(),
    checked_at  timestamptz
);
