"""Copy a stopped SQLite runtime into an unused CandleRoute PostgreSQL schema."""

from collections import Counter
from contextlib import ExitStack, closing, nullcontext
from functools import partial
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from backup import _digest, _publish_artifacts, _required_artifacts, _runtime_artifacts, restore_bundle
from runtime_lease import FileLease


# Names are internal SQL constants. Explicit column order makes row checks portable.
ACCOUNT_TABLES = (
    ('users', 'id username email password_hash verified created_at', 'id'),
    ('verification', 'user_id code_hash expires_at attempts', 'user_id'),
    ('recovery', 'user_id code_hash expires_at attempts', 'user_id'),
    ('credentials', 'user_id encrypted', 'user_id'),
    ('exchange_ownership', 'testnet exchange_user user_id', 'testnet exchange_user'),
)
TRADING_TABLES = (
    ('records', 'namespace key value updated_at', 'namespace key'),
    ('position_owners', 'account position_id bot_id', 'account position_id'),
    ('portfolio_snapshots', 'captured_at equity_usdt valuation_complete data', 'captured_at'),
    ('portfolio_cashflows', 'id occurred_at amount_usdt note', 'id'),
)
CANDLE_TABLES = (
    ('candles', 'exchange instrument interval open_time close_time open high low close volume metadata',
     'exchange instrument interval open_time'),
    ('checked_empty', 'exchange instrument interval start_time end_time expires_at',
     'exchange instrument interval start_time end_time'),
)
QUOTE_TABLES = (('market_quotes', 'id payload', 'id'),)
ALL_TABLES = ACCOUNT_TABLES + TRADING_TABLES + CANDLE_TABLES + QUOTE_TABLES
USER_ID = re.compile(r'[0-9a-f]{32}\Z')


def _columns(names):
    return ', '.join(f'"{name}"' for name in names.split())


def _target_empty(conn):
    if conn.execute("SELECT to_regclass('candle_route.sqlite_migration') IS NOT NULL").fetchone()[0]:
        raise ValueError('The PostgreSQL target already contains a verified SQLite migration')
    for table, _, _ in ALL_TABLES:
        qualified = f'candle_route.{table}'
        exists = conn.execute('SELECT to_regclass(%s)', (qualified,)).fetchone()[0]
        if exists and conn.execute(f'SELECT 1 FROM {qualified} LIMIT 1').fetchone():
            raise ValueError('The PostgreSQL target already contains CandleRoute data')


def _copy_sqlite(source, snapshot):
    if source.resolve() != source or source.is_symlink():
        raise ValueError('SQLite source contains a linked database')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(snapshot)) as copied:
            original.backup(copied)
            if copied.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('SQLite source failed its integrity check')
            if copied.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('SQLite source contains an invalid foreign key')


def _fingerprint(digest, row):
    payload = json.dumps(row, ensure_ascii=True, separators=(',', ':'),
                         default=lambda value: {'bytes_hex': bytes(value).hex()})
    digest.update(payload.encode('ascii') + b'\n')


def _copy_table(source, target, table, fields, sort, scope):
    # SQLite's BINARY collation and PostgreSQL's C collation give the same key order.
    ordered = ', '.join(f'"{name}" COLLATE BINARY' for name in sort.split())
    rows = source.execute(f'SELECT {_columns(fields)} FROM {table} ORDER BY {ordered}')
    expected = hashlib.sha256()
    count = 0
    if target is None:
        for row in rows:
            _fingerprint(expected, row)
            count += 1
        return count
    prefix = 'scope_id ' if scope is not None else ''
    insert_fields = prefix + fields
    if table == 'records':
        from storage import record_projections
        insert_fields += ' bot_id audit_time'
    params = ','.join(['%s'] * len(insert_fields.split()))
    with target.cursor() as cursor:
        for batch in iter(partial(rows.fetchmany, 1000), []):
            values = []
            for row in batch:
                _fingerprint(expected, row)
                bound = (scope, *row) if scope is not None else row
                if table == 'records':
                    bound = (*bound, *record_projections(row[0], json.loads(row[2])))
                values.append(bound)
            cursor.executemany(f'INSERT INTO candle_route.{table} ({_columns(insert_fields)}) VALUES ({params})', values)
            count += len(batch)

    received = hashlib.sha256()
    key_types = {'testnet', 'id', 'open_time', 'start_time', 'end_time'}
    numeric_id = table in {'portfolio_cashflows', 'market_quotes'}
    ordered = ', '.join(
        f'"{key}"' if key in key_types and (key != 'id' or numeric_id) else f'"{key}" COLLATE "C"'
        for key in sort.split()
    )
    query = f'SELECT {_columns(fields)} FROM candle_route.{table}'
    params = ()
    if scope is not None:
        query += ' WHERE scope_id=%s'
        params = (scope,)
    # A server cursor avoids retaining a large candle database in memory.
    actual_count = 0
    with target.cursor(name='migration_verify') as cursor:
        cursor.execute(query + f' ORDER BY {ordered}', params)
        for row in cursor:
            _fingerprint(received, row)
            actual_count += 1
    if actual_count != count or expected.digest() != received.digest():
        raise ValueError(f'PostgreSQL row verification failed for {table}')
    return count


def _snapshot_tables(path, target, tables, scope=None):
    counts = Counter()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as source:
        present = {name for name, in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables[0][0] not in present:
            raise ValueError('SQLite source is missing its expected tables')
        if scope is not None and source.execute('PRAGMA user_version').fetchone()[0] > 2:
            raise ValueError('SQLite trading database was created by a newer application version')
        for table, fields, sort in tables:
            # Older versions can lack newer tables. The destination migrations create them empty.
            if table in present:
                counts[table] = _copy_table(source, target, table, fields, sort, scope)
    return counts


def _cashflow_watermark(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as source:
        if source.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            value = source.execute("SELECT seq FROM sqlite_sequence WHERE name='portfolio_cashflows'").fetchone()
            return value[0] if value else 0
    return 0


def _sqlite_sources(root, stack):
    sources = []
    for name, tables, scope in (
        ('accounts.sqlite3', ACCOUNT_TABLES, None), ('trader.sqlite3', TRADING_TABLES, 'single'),
        ('candles.sqlite3', CANDLE_TABLES, None), ('market-quotes.sqlite3', QUOTE_TABLES, None),
    ):
        path = root / name
        if path.exists():
            sources.append((path, tables, scope))
    for path in sorted((root / 'users').glob('*/trader.sqlite3')):
        user_id = path.parent.name
        if not USER_ID.fullmatch(user_id):
            raise ValueError('SQLite runtime contains an invalid account directory')
        sources.append((path, TRADING_TABLES, user_id))
    if not sources:
        raise ValueError('No CandleRoute SQLite databases found in the source')
    for path, _, _ in sources:
        if path.resolve() != path:
            raise ValueError('SQLite source contains a linked database')
        stack.enter_context(FileLease(str(path) + '.lock'))
    return sources


def _validate_accounts(root, sources):
    account_paths = {path for path, tables, _ in sources if tables is ACCOUNT_TABLES}
    valid = set()
    for path in account_paths:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
                raise ValueError('SQLite account database is missing its users table')
            valid = {user_id for (user_id,) in conn.execute('SELECT id FROM users')}
    if any(not isinstance(value, str) or not USER_ID.fullmatch(value) for value in valid):
        raise ValueError('SQLite runtime has an invalid user ID')
    for path in (root / 'users').glob('*'):
        if path.is_dir() and (path.resolve() != path or path.name not in valid):
            raise ValueError('SQLite runtime has an account directory without a matching user')


def _reserve_cashflow_ids(conn, source_watermark):
    largest = conn.execute('SELECT MAX(id) FROM candle_route.portfolio_cashflows').fetchone()[0]
    watermark = max(source_watermark, largest or 0)
    # Preserve IDs across scopes and avoid rewinding a sequence after deletions.
    last, used = conn.execute('SELECT last_value,is_called FROM candle_route.portfolio_cashflows_id_seq').fetchone()
    if watermark >= last + int(used):
        conn.execute(f'ALTER TABLE candle_route.portfolio_cashflows ALTER COLUMN id RESTART WITH {watermark + 1}')


def _stage_artifacts(stack, source, destination, artifacts):
    destination.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(stack.enter_context(tempfile.TemporaryDirectory(
        prefix='.pg-artifacts-', dir=destination if destination.exists() else destination.parent)))
    staging = workspace / 'data'
    staging.mkdir()
    for source_path in artifacts:
        relative = source_path.relative_to(source)
        output = staging / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, output)
        with output.open('rb') as checked, source_path.open('rb') as original:
            if _digest(checked) != _digest(original):
                raise ValueError(f'Source artifact changed during migration: {relative.as_posix()}')
    return staging, _publish_artifacts(staging, destination, workspace=workspace)


def migrate_runtime(database, source, destination, *, dry_run=False):
    """Validate/copy an SQLite runtime directory or a verified version-one bundle."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if database is None and not dry_run:
        raise ValueError('Configure a PostgreSQL target before migrating')
    if source != destination and (destination.is_relative_to(source) or source.is_relative_to(destination)):
        raise ValueError('Migration requires separate, non-overlapping source and destination directories')
    if source != destination and destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Migration destination must be new or empty, or the source runtime directory')
    if not source.exists():
        raise ValueError('SQLite migration source does not exist')

    with ExitStack() as stack:
        if database is not None:
            stack.enter_context(database.runtime_lease())
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='candleroute-sqlite-')))
        root = source
        if source.is_file():
            root = temporary / 'legacy'
            restore_bundle(source, root)
        stack.enter_context(FileLease(root / '.dashboard.lock'))
        sources = _sqlite_sources(root, stack)
        for path, _, _ in sources:
            copied = temporary / 'snapshot' / path.relative_to(root)
            copied.parent.mkdir(parents=True, exist_ok=True)
            _copy_sqlite(path, copied)
        snapshots = [(temporary / 'snapshot' / path.relative_to(root), tables, scope)
                     for path, tables, scope in sources]
        _validate_accounts(root, snapshots)
        artifacts = _runtime_artifacts(root)
        _required_artifacts(temporary / 'snapshot', artifacts_root=root)

        if dry_run:
            if database is not None:
                with database.connection() as conn:
                    _target_empty(conn)
            counts = Counter()
            for path, tables, scope in snapshots:
                counts.update(_snapshot_tables(path, None, tables, scope))
            return {'rows': dict(counts), 'artifacts': len(artifacts), 'dry_run': True}

        from postgres import ensure_schema
        ensure_schema(database)
        publication = nullcontext()
        if source != destination:
            stage, publication = _stage_artifacts(stack, root, destination, artifacts)
            _required_artifacts(temporary / 'snapshot', artifacts_root=stage)

        with ExitStack() as transfer:
            conn = transfer.enter_context(database.connection())
            _target_empty(conn)
            counts = Counter()
            for path, tables, scope in snapshots:
                counts.update(_snapshot_tables(path, conn, tables, scope))
            watermark = max((_cashflow_watermark(path) for path, _, scope in snapshots if scope is not None), default=0)
            _reserve_cashflow_ids(conn, watermark)
            conn.execute('CREATE TABLE candle_route.sqlite_migration (id INTEGER PRIMARY KEY CHECK (id=1), '
                         'completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            conn.execute('INSERT INTO candle_route.sqlite_migration(id) VALUES (1)')
            # Publish files while SQL is still uncommitted. Undo publication if commit fails.
            with publication:
                transfer.close()
        return {'rows': dict(counts), 'artifacts': len(artifacts), 'dry_run': False}


def import_legacy_account(database, source, root, *, username, master_key, credentials):
    """Import one legacy trading database to a verified PostgreSQL account."""
    from accounts import PgAccounts

    source, root = Path(source).resolve(), Path(root).resolve()
    if not source.is_file():
        raise ValueError('No legacy SQLite database exists at the selected path')
    with ExitStack() as stack:
        stack.enter_context(database.runtime_lease())
        for directory in sorted({source.parent, root}):
            stack.enter_context(FileLease(directory / '.dashboard.lock'))
        stack.enter_context(FileLease(str(source) + '.lock'))
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='candleroute-legacy-')))
        snapshot = temporary / 'trader.sqlite3'
        _copy_sqlite(source, snapshot)
        accounts = PgAccounts(database, master_key)
        user = accounts.by_username(username)
        if not user or not user['verified']:
            raise ValueError('Destination account must exist and have a confirmed email')
        scope = user['id']
        if not USER_ID.fullmatch(scope):
            raise ValueError('Destination account has an invalid ID')

        values = dict(credentials)
        with closing(sqlite3.connect(snapshot)) as sqlite:
            identities = {json.loads(raw).get('_account') for (raw,) in sqlite.execute(
                "SELECT value FROM records WHERE namespace='runtime'")}
        identities.discard(None)
        identities.discard('')
        if len(identities) > 1:
            raise ValueError('Legacy bots contain multiple exchange identities. Reconcile them before import.')
        if identities:
            identity = next(iter(identities))
            mode, separator, account_id = identity.partition(':')
            if not separator or not account_id or mode not in ('production', 'testnet'):
                raise ValueError('Legacy bots contain an invalid exchange identity')
            values.update(testnet=mode == 'testnet', account_id=account_id)

        destination = root / 'users' / scope
        artifacts = [path for path in _runtime_artifacts(source.parent)
                     if path.relative_to(source.parent).parts[0] != 'users']
        _required_artifacts(temporary, artifacts_root=source.parent)
        publication = nullcontext()
        if artifacts:
            stage, publication = _stage_artifacts(stack, source.parent, destination, artifacts)
            _required_artifacts(temporary, artifacts_root=stage)
        with ExitStack() as transfer:
            conn = transfer.enter_context(database.connection())
            database.transaction_lock(conn, 'accounts')
            if conn.execute('SELECT 1 FROM candle_route.credentials WHERE user_id=%s', (scope,)).fetchone():
                raise ValueError('Destination account already has exchange credentials')
            for table, _, _ in TRADING_TABLES:
                if conn.execute(f'SELECT 1 FROM candle_route.{table} WHERE scope_id=%s LIMIT 1', (scope,)).fetchone():
                    raise ValueError('Destination already has trading data. Import refused.')
            _snapshot_tables(snapshot, conn, TRADING_TABLES, scope)
            _reserve_cashflow_ids(conn, _cashflow_watermark(snapshot))
            accounts.set_credentials(scope, values, connection=conn)
            with publication:
                transfer.close()
    return user
