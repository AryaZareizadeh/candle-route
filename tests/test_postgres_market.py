from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from candle_store import CandleStore
from market_quotes import MarketQuotes, shared_market_quotes
from tests.test_market_quotes import Response


def _candle(open_time, close_time, *, volume=1.23456789012345, marker='original\x00'):
    return {
        'open_time': open_time, 'close_time': close_time,
        'open': 10.0, 'high': 12.0, 'low': 9.0, 'close': 11.0,
        'volume': volume, 'metadata': {'marker': marker},
    }


def test_default_candle_store_keeps_closed_bars_and_gap_expiry_in_postgres(postgres_database, tmp_path):
    first_open = 2_200_000_000
    rows = [_candle(first_open, first_open + 60), _candle(first_open + 120, first_open + 180)]
    store = CandleStore()
    assert store.save('Binance', 'btcusdt', '1', rows, as_of=first_open + 180) == 2
    assert store.save('binance', 'BTCUSDT', '1m', [
        _candle(first_open, first_open + 60, volume=50),
        _candle(first_open + 180, first_open + 240),
    ], as_of=first_open + 180) == 0

    store.remember_empty('binance', 'BTCUSDT', '1m', first_open + 60.5, first_open + 120,
                         now=first_open + 180, ttl=60)
    reopened = CandleStore()
    assert reopened.read('binance', 'BTCUSDT', '1m', start=first_open + 0.5) == [rows[1]]
    assert reopened.read('binance', 'BTCUSDT', '1m', end=first_open + 0.5) == [rows[0]]
    assert reopened.read('binance', 'BTCUSDT', '1m', as_of=first_open + 59.5) == []
    assert reopened.read('binance', 'BTCUSDT', '1m', limit=2) == rows
    assert reopened.missing_ranges('binance', 'BTCUSDT', '1m', first_open, first_open + 180,
                                  now=first_open + 239.5) == [(first_open + 60, first_open + 60.5)]
    assert reopened.missing_ranges('binance', 'BTCUSDT', '1m', first_open, first_open + 180,
                                  now=first_open + 240) == [(first_open + 60, first_open + 120)]
    assert not (tmp_path / 'candles.sqlite3').exists()


def test_postgres_candle_series_lock_allows_one_download_and_nested_access(postgres_database):
    candle = _candle(0, 60)
    stores = [CandleStore(), CandleStore()]
    ready = Barrier(2)
    downloads = []

    def fetch(store):
        ready.wait(timeout=5)
        with store.series_lock('BINANCE', 'btcusdt', '1'):
            with stores[0].series_lock('binance', 'BTCUSDT', '1m'):
                if not store.read('binance', 'BTCUSDT', '1m'):
                    downloads.append(candle)
                    store.save('binance', 'BTCUSDT', '1m', [candle], as_of=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(fetch, stores))

    assert downloads == [candle]


def test_postgres_quotes_share_polling_reservation_and_cooldown(postgres_database, tmp_path):
    clock = [1000]
    polling = Event()
    finish = Event()

    def success(*args, **kwargs):
        polling.set()
        assert finish.wait(timeout=5)
        return Response()

    def rate_limited(*args, **kwargs):
        return Response(status=429, headers={'Retry-After': '120'})

    first = MarketQuotes(clock=lambda: clock[0], get=success)
    second = MarketQuotes(clock=lambda: clock[0], get=rate_limited)
    with ThreadPoolExecutor(max_workers=1) as pool:
        refresh = pool.submit(first.refresh_due)
        try:
            assert polling.wait(timeout=5)
            clock[0] = 1011
            assert second.refresh_due() is False
            assert shared_market_quotes().snapshot()['next_poll'] == 1010
        finally:
            finish.set()
        assert refresh.result(timeout=5) is True

    clock[0] = 1021
    assert second.refresh_due() is True
    clock[0] = 1121
    reopened = MarketQuotes(clock=lambda: clock[0])
    assert reopened.snapshot()['next_poll'] == 1141
    assert reopened.refresh_due() is False
    assert reopened.quote('BTCUSDT')['live_price'] == 101.5
    assert reopened.quote('BTCUSDT')['live_price_stale'] is True
    assert not (tmp_path / 'market-quotes.sqlite3').exists()


def test_postgres_quotes_discard_response_after_losing_poll_lock(postgres_database):
    from psycopg import Error

    clock = [1000]
    polling = Event()
    finish = Event()

    def delayed_response(*args, **kwargs):
        polling.set()
        assert finish.wait(timeout=5)
        return Response({'status': 'ok', 'stats': {'btc-usdt': {'latest': '1'}}})

    first = MarketQuotes(clock=lambda: clock[0], get=delayed_response)
    second = MarketQuotes(clock=lambda: clock[0], get=lambda *args, **kwargs: Response())
    with ThreadPoolExecutor(max_workers=1) as pool:
        refresh = pool.submit(first.refresh_due)
        try:
            assert polling.wait(timeout=5)
            with postgres_database.connection() as conn:
                pid, = conn.execute("""SELECT pid FROM pg_locks WHERE locktype = 'advisory'
                    AND database = (SELECT oid FROM pg_database WHERE datname = current_database())""").fetchone()
                assert conn.execute('SELECT pg_terminate_backend(%s, 5000)', (pid,)).fetchone() == (True,)
            clock[0] = 1011
            assert second.refresh_due() is True
        finally:
            finish.set()
        with pytest.raises(Error):
            refresh.result(timeout=5)

    assert second.quote('BTCUSDT')['live_price'] == 101.5
