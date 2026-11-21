CREATE TABLE candles (
    exchange TEXT NOT NULL,
    instrument TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time BIGINT NOT NULL,
    close_time BIGINT NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY (exchange, instrument, interval, open_time)
);

CREATE INDEX candles_coverage ON candles (exchange, instrument, interval, close_time, open_time);

CREATE TABLE checked_empty (
    exchange TEXT NOT NULL,
    instrument TEXT NOT NULL,
    interval TEXT NOT NULL,
    start_time DOUBLE PRECISION NOT NULL,
    end_time DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (exchange, instrument, interval, start_time, end_time)
);

CREATE INDEX checked_empty_expiry ON checked_empty (expires_at);

CREATE TABLE market_quotes (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL
);
