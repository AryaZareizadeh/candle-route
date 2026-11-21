from contextlib import closing
import shutil
import sqlite3

from cryptography.fernet import Fernet
import pytest

from accounts import Accounts
from backup import backup_bundle, restore_bundle, verify_bundle
from storage import Store


OWNER = 'a' * 32
MODEL = 'b' * 32


def _runtime(root):
    accounts = Accounts(root / 'accounts.sqlite3', Fernet.generate_key().decode())
    with accounts.connect() as conn:
        conn.execute('INSERT INTO users(id,username,email,password_hash,verified) VALUES (?,?,?,?,1)',
                     (OWNER, 'Alice', 'alice@example.com', 'preserve-source-hash'))
        conn.execute('INSERT INTO credentials VALUES (?,?)', (OWNER, b'opaque-ciphertext'))
    account = root / 'users' / OWNER
    store = Store(account / 'trader.sqlite3')
    store.put('trained_models', MODEL, {'status': 'ready', 'architecture': 'alexnet1d-v1'})
    with store.connection() as conn:
        conn.execute('INSERT INTO portfolio_cashflows VALUES (42,?,?,?)',
                     ('2026-10-03T09:00:00+00:00', 25.5, 'deposit'))
        conn.execute("INSERT INTO portfolio_cashflows VALUES (200,'2026-10-04T09:00:00+00:00',1,'removed')")
        conn.execute('DELETE FROM portfolio_cashflows WHERE id=200')
    checkpoint = account / 'models' / f'{MODEL}.pt'
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b'checkpoint')
    forecast = account / 'forecasting' / 'data' / '1h' / 'BTC.csv'
    forecast.parent.mkdir(parents=True)
    forecast.write_bytes(b'local dataset')
    return store.path


def test_postgres_migration_dry_run_validates_registered_checkpoint(tmp_path):
    from postgres_transfer import migrate_runtime

    source = tmp_path / 'sqlite'
    legacy_path = _runtime(source)
    (legacy_path.parent / 'models' / f'{MODEL}.pt').unlink()
    with pytest.raises(ValueError, match='missing'):
        migrate_runtime(None, source, tmp_path / 'target', dry_run=True)
    assert not (tmp_path / 'target').exists()


def test_postgres_migration_copies_committed_wal_and_artifacts(postgres_database, tmp_path):
    from postgres_transfer import migrate_runtime

    source, target = tmp_path / 'sqlite', tmp_path / 'postgres'
    legacy_path = _runtime(source)
    with closing(sqlite3.connect(legacy_path)) as sqlite:
        sqlite.execute("INSERT INTO records(namespace,key,value) VALUES ('order_intents','accepted','{\"id\":19}')")
        sqlite.commit()
        assert legacy_path.with_suffix('.sqlite3-wal').is_file()
        assert migrate_runtime(postgres_database, source, target, dry_run=True)['rows']['records'] == 2
        assert not target.exists()
        assert migrate_runtime(postgres_database, source, target)['artifacts'] == 2
        assert sqlite.execute('SELECT COUNT(*) FROM portfolio_cashflows').fetchone() == (1,)

    assert (target / 'users' / OWNER / 'models' / f'{MODEL}.pt').read_bytes() == b'checkpoint'
    assert (target / 'users' / OWNER / 'forecasting' / 'data' / '1h' / 'BTC.csv').read_bytes() == b'local dataset'
    assert not list(target.rglob('*.sqlite3'))
    with postgres_database.connection() as conn:
        assert conn.execute('SELECT user_id,encrypted FROM candle_route.credentials').fetchone() == (
            OWNER, b'opaque-ciphertext')
        assert conn.execute("SELECT scope_id,value FROM candle_route.records WHERE namespace='order_intents'").fetchone() == (
            OWNER, '{"id":19}')
        row = conn.execute("INSERT INTO candle_route.portfolio_cashflows(scope_id,occurred_at,amount_usdt) "
                           "VALUES (%s,'2026-10-05T00:00:00+00:00',1) RETURNING id", (OWNER,)).fetchone()
        assert row[0] > 200
    with pytest.raises(ValueError, match='already contains'):
        migrate_runtime(postgres_database, source, tmp_path / 'other')
    assert not (tmp_path / 'other').exists()


def test_postgres_migration_accepts_v1_bundle(postgres_database, tmp_path):
    from postgres_transfer import migrate_runtime

    source, target = tmp_path / 'sqlite', tmp_path / 'postgres'
    _runtime(source)
    bundle = tmp_path / 'v1.zip'
    assert backup_bundle(source, bundle)['format_version'] == 1
    result = migrate_runtime(postgres_database, bundle, target)
    assert result['rows']['users'] == result['rows']['records'] == 1
    assert verify_bundle(bundle)['format_version'] == 1


def test_migration_can_retry_after_artifact_publication_fails(postgres_database, tmp_path, monkeypatch):
    from pathlib import Path

    from postgres_transfer import migrate_runtime

    source, target = tmp_path / 'sqlite', tmp_path / 'postgres'
    _runtime(source)
    real_rename = Path.rename

    def fail_to_publish(path, destination):
        if destination == target:
            raise OSError('Failed to publish artifacts')
        return real_rename(path, destination)

    with monkeypatch.context() as fault:
        fault.setattr(Path, 'rename', fail_to_publish)
        with pytest.raises(OSError, match='Failed to publish'):
            migrate_runtime(postgres_database, source, target)
    assert not target.exists()
    with postgres_database.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM candle_route.users').fetchone() == (0,)
        assert conn.execute("SELECT to_regclass('candle_route.sqlite_migration')").fetchone() == (None,)
    assert migrate_runtime(postgres_database, source, target)['rows']['users'] == 1


def test_postgres_legacy_import_preserves_saved_exchange_identity(postgres_database, tmp_path):
    from accounts import PgAccounts
    from postgres_transfer import import_legacy_account

    master_key = Fernet.generate_key().decode()
    accounts = PgAccounts(postgres_database, master_key)
    with accounts.connect() as conn:
        conn.execute("INSERT INTO candle_route.users(id,username,email,password_hash,verified) "
                     "VALUES (%s,'Alice','alice@example.com','hash',1)", (OWNER,))
    source = tmp_path / 'old' / 'trader.sqlite3'
    legacy = Store(source)
    legacy.put('runtime', 'bot', {'_account': 'testnet:legacy-owner', 'state': 'stopped'})
    root = tmp_path / 'target'
    user = import_legacy_account(postgres_database, source, root, username='alice', master_key=master_key,
                                 credentials={'token': 'credential', 'account_id': 'wrong'})
    assert user['id'] == OWNER
    assert accounts.credentials(OWNER) == {'token': 'credential', 'account_id': 'legacy-owner', 'testnet': True}
    with postgres_database.connection() as conn:
        assert conn.execute('SELECT scope_id,namespace,key FROM candle_route.records').fetchone() == (
            OWNER, 'runtime', 'bot')


def test_postgres_bundle_requires_a_fresh_target(postgres_database, tmp_path):
    from postgres_transfer import migrate_runtime
    from uuid import uuid4

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from postgres import PostgresDatabase

    if not all(shutil.which(tool) for tool in ('pg_dump', 'pg_restore')):
        pytest.skip('PostgreSQL clients are required for a PostgreSQL bundle')
    source, target = tmp_path / 'sqlite', tmp_path / 'postgres'
    _runtime(source)
    migrate_runtime(postgres_database, source, target)
    bundle = tmp_path / 'postgres.zip'
    manifest = backup_bundle(target, bundle, database=postgres_database)
    assert manifest['format_version'] == 2
    assert manifest['requires_separate_master_key'] is True
    assert {item['path'] for item in verify_bundle(bundle)['files']} == {
        'postgres.dump', f'users/{OWNER}/models/{MODEL}.pt', f'users/{OWNER}/forecasting/data/1h/BTC.csv'}
    destination = tmp_path / 'restore'
    with pytest.raises(ValueError, match='already contains'):
        restore_bundle(bundle, destination, database=postgres_database)
    assert not destination.exists()
    name = f'candle_test_restore_{uuid4().hex}'
    identifier = sql.Identifier(name)
    with psycopg.connect(postgres_database.dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(identifier))
        restored = PostgresDatabase(make_conninfo(postgres_database.dsn, dbname=name))
        try:
            destination.mkdir()
            restore_bundle(bundle, destination, database=restored)
            with restored.connection() as conn:
                assert conn.execute('SELECT user_id,encrypted FROM candle_route.credentials').fetchone() == (
                    OWNER, b'opaque-ciphertext')
            assert (destination / 'users' / OWNER / 'models' / f'{MODEL}.pt').read_bytes() == b'checkpoint'
        finally:
            restored.close()
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(identifier))
