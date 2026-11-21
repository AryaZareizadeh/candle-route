"""Shared PostgreSQL connections, schema upgrades and deployment ownership."""

import hashlib
import os
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

SCHEMA = "candle_route"
MIGRATIONS = Path(__file__).resolve().parent / "migrations" / "postgres"
_RUNTIME_LOCK = "deployment:runtime"


class DatabaseUnavailable(RuntimeError):
    """The configured database could not provide a usable connection."""


def _lock_key(name):
    return int.from_bytes(hashlib.sha256(f"{SCHEMA}:{name}".encode()).digest()[:8],
                          byteorder="big", signed=True)


class PostgresDatabase:
    def __init__(self, dsn):
        import psycopg
        from psycopg_pool import ConnectionPool, PoolTimeout

        self.dsn = dsn
        self._driver = psycopg
        self._pool_timeout = PoolTimeout
        self._options = {
            "connect_timeout": 5,
            "options": f"-c search_path={SCHEMA},pg_catalog -c timezone=UTC",
        }
        try:
            self._pool = ConnectionPool(dsn, kwargs=self._options, open=False, min_size=0,
                                        max_size=12, timeout=5, check=ConnectionPool.check_connection,
                                        name=SCHEMA)
        except (ValueError, psycopg.Error):
            raise RuntimeError("Invalid Candle Route PostgreSQL connection settings") from None
        self._pool_lock = threading.Lock()
        self._runtime_lock = threading.Lock()
        self._migration_lock = threading.Lock()
        self._migrated = set()
        self._runtime = None

    @contextmanager
    def connection(self):
        with self._pool_lock:
            if self._pool.closed:
                self._pool.open()
        try:
            with self._pool.connection() as conn:
                yield conn
        except self._pool_timeout:
            raise DatabaseUnavailable("Could not acquire a Candle Route PostgreSQL connection") from None

    def close(self):
        self._pool.close()

    def _connect_lock_session(self):
        try:
            return self._driver.connect(self.dsn, autocommit=True, **self._options)
        except (ValueError, self._driver.Error):
            raise DatabaseUnavailable("Could not connect to the Candle Route PostgreSQL database") from None

    @contextmanager
    def locked_connection(self, name, *, wait=False):
        with self._connect_lock_session() as conn:
            operation = "SELECT pg_advisory_lock(%s)" if wait else "SELECT pg_try_advisory_lock(%s)"
            acquired = conn.execute(operation, (_lock_key(name),)).fetchone()[0]
            yield conn if wait or acquired else None
            # The dedicated session releases its lock even on failure.

    @contextmanager
    def lock(self, name, *, wait=False):
        with self.locked_connection(name, wait=wait) as conn:
            yield conn is not None

    def transaction_lock(self, conn, name):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(name),))

    @contextmanager
    def runtime_lease(self):
        with self.locked_connection(_RUNTIME_LOCK) as conn:
            if conn is None:
                raise RuntimeError("The Candle Route PostgreSQL deployment is already in use")
            with self._runtime_lock:
                self._runtime = conn
            try:
                yield
            finally:
                with self._runtime_lock:
                    self._runtime = None

    def require_runtime(self):
        """Fail closed on a lost owner session. This connection is never replaced."""
        with self._runtime_lock:
            if self._runtime is not None:
                try:
                    self._runtime.execute("SELECT 1")
                    return
                except self._driver.Error:
                    self._runtime.close()
                    self._runtime = None
            raise RuntimeError("PostgreSQL runtime ownership is unavailable. Restart Candle Route before trading.")

    def check_ready(self):
        self.require_runtime()
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1")
        except self._driver.Error:
            raise DatabaseUnavailable("Candle Route PostgreSQL is not ready") from None

    def migrate(self, component, directory):
        with self._migration_lock:
            if component not in self._migrated:
                self._migrate(component, directory)
                self._migrated.add(component)

    def _migrate(self, component, directory):
        files = sorted(Path(directory).glob("*.sql"))
        if not files:
            raise RuntimeError(f"No PostgreSQL migrations found for {component}")
        with self.connection() as conn:
            self.transaction_lock(conn, "deployment:schema")
            conn.execute("CREATE SCHEMA IF NOT EXISTS candle_route")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    component TEXT NOT NULL,
                    migration TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    PRIMARY KEY (component, migration)
                )
            """)
            applied = dict(conn.execute(
                "SELECT migration, sha256 FROM schema_migrations WHERE component = %s",
                (component,)).fetchall())
            versions = [source.name for source in files]
            if sorted(applied) != versions[:len(applied)]:
                raise RuntimeError(f"Unrecognized PostgreSQL migration history for {component}")
            for source in files:
                sql = source.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if source.name in applied:
                    if applied[source.name] != checksum:
                        raise RuntimeError(f"PostgreSQL migration {component}/{source.name} was modified")
                    continue
                conn.execute(sql, prepare=False)
                conn.execute("INSERT INTO schema_migrations VALUES (%s, %s, %s)",
                             (component, source.name, checksum))


@lru_cache(maxsize=1)
def _database(dsn):
    return PostgresDatabase(dsn)


def get_database():
    dsn = os.getenv("TRADER_DATABASE_URL", "").strip()
    return _database(dsn) if dsn else None


def close_database():
    if _database.cache_info().currsize:
        database = get_database()
        if database is not None:
            database.close()
        _database.cache_clear()


def ensure_schema(database):
    for component in ("core", "market"):
        database.migrate(component, MIGRATIONS / component)


def require_imported_runtime(database, root, *, multiuser=False):
    """Refuse to start over an existing SQLite deployment by changing the DSN alone."""
    root = Path(root)
    if multiuser:
        existing = (root / "accounts.sqlite3").is_file() or any((root / "users").glob("*/trader.sqlite3"))
    else:
        existing = (root / "trader.sqlite3").is_file()
    if existing:
        with database.connection() as conn:
            migrated = conn.execute(
                "SELECT to_regclass('candle_route.sqlite_migration') IS NOT NULL"
            ).fetchone()[0]
        if not migrated:
            raise RuntimeError("This data directory contains SQLite state. Run migrate-postgres before starting.")
