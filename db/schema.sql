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
