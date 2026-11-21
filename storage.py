"""Transactional local state. Importing this module never opens a database."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from postgres import MIGRATIONS, get_database
from settings import data_dir


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._runtime_file = None
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("Database was created by a newer application version")
            conn.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                conn.executescript((Path(__file__).parent / "migrations/001_initial.sql").read_text())
            if version < 2:
                conn.executescript((Path(__file__).parent / "migrations/002_portfolio.sql").read_text())

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @property
    def artifact_dir(self):
        return self.path.parent

    @property
    def workspace_id(self):
        return str(self.path)

    @contextmanager
    def write_transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @contextmanager
    def read_snapshot(self):
        with self.connection() as conn:
            conn.execute("BEGIN")
            yield conn

    def require_runtime(self):
        """Local runtime ownership is enforced by the filesystem lease."""

    @staticmethod
    def write(conn, namespace, key, value):
        conn.execute("INSERT INTO records(namespace,key,value) VALUES(?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP",
                     (namespace, str(key), json.dumps(value, allow_nan=False)))

    @staticmethod
    def append(conn, namespace, key, value):
        conn.execute("INSERT INTO records(namespace,key,value) VALUES(?,?,?)",
                     (namespace, str(key), json.dumps(value, allow_nan=False)))

    def put(self, namespace, key, value):
        with self.write_transaction() as conn:
            self.write(conn, namespace, key, value)

    @staticmethod
    def read(conn, namespace, key, default=None):
        row = conn.execute("SELECT value FROM records WHERE namespace=? AND key=?",
                           (namespace, str(key))).fetchone()
        return json.loads(row[0]) if row else default

    def get(self, namespace, key, default=None):
        with self.connection() as conn:
            return self.read(conn, namespace, key, default)

    @staticmethod
    def records(conn, namespace):
        rows = conn.execute("SELECT key,value FROM records WHERE namespace=? ORDER BY key", (namespace,))
        return {key: json.loads(value) for key, value in rows}

    def list(self, namespace):
        with self.connection() as conn:
            return self.records(conn, namespace)

    @staticmethod
    def erase(conn, namespace, key):
        conn.execute("DELETE FROM records WHERE namespace=? AND key=?", (namespace, str(key)))

    def delete(self, namespace, key):
        with self.write_transaction() as conn:
            self.erase(conn, namespace, key)

    def list_for_bots(self, namespace, bot_ids):
        """Read only the selected bot's records, including its independent runners."""
        if not bot_ids:
            return {}
        placeholders = ','.join('?' for _ in bot_ids)
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT key,value FROM records WHERE namespace=? AND json_extract(value,'$.bot_id') IN ({placeholders}) ORDER BY key",
                (namespace, *bot_ids)).fetchall()
        return {key: json.loads(value) for key, value in rows}

    def recent_audit(self, limit):
        with self.connection() as conn:
            rows = conn.execute("SELECT value FROM records WHERE namespace='audit_events' "
                                "ORDER BY json_extract(value,'$.time') DESC LIMIT ?", (limit,))
            return [json.loads(raw) for (raw,) in rows]

    def paper_history(self, prefix, limit):
        with self.connection() as conn:
            rows = conn.execute("SELECT value FROM records WHERE namespace='spot_paper_history' "
                                "AND key>=? AND key<? ORDER BY key DESC LIMIT ?",
                                (prefix, prefix + "\uffff", -1 if limit is None else limit)).fetchall()
        return [json.loads(raw) for (raw,) in reversed(rows)]

    @staticmethod
    def position_owner(conn, account, position_id):
        row = conn.execute("SELECT bot_id FROM position_owners WHERE account=? AND position_id=?",
                           (account, str(position_id))).fetchone()
        return row[0] if row else None

    def owner(self, account, position_id):
        with self.connection() as conn:
            return self.position_owner(conn, account, position_id)

    @staticmethod
    def _claim(conn, account, position_id, bot_id):
        conn.execute("INSERT OR IGNORE INTO position_owners VALUES(?,?,?)",
                     (account, str(position_id), bot_id))

    def claim_position(self, account, position_id, bot_id):
        with self.write_transaction() as conn:
            self._claim(conn, account, position_id, bot_id)
            if self.position_owner(conn, account, position_id) != bot_id:
                raise ValueError("Exchange position is already owned by another bot")

    @staticmethod
    def upsert_snapshot(conn, captured_at, equity, complete, data):
        conn.execute("INSERT OR REPLACE INTO portfolio_snapshots VALUES(?,?,?,?)",
                     (captured_at, equity, complete, data))

    @staticmethod
    def latest_snapshots(conn, limit):
        return conn.execute("SELECT data FROM portfolio_snapshots ORDER BY captured_at DESC LIMIT ?", (limit,))

    @staticmethod
    def insert_cashflow(conn, occurred_at, amount, note):
        return conn.execute("INSERT INTO portfolio_cashflows(occurred_at,amount_usdt,note) VALUES(?,?,?)",
                            (occurred_at, amount, note)).lastrowid

    @staticmethod
    def remove_cashflow(conn, flow_id):
        return conn.execute("DELETE FROM portfolio_cashflows WHERE id=?", (flow_id,)).rowcount > 0

    @staticmethod
    def cashflows(conn):
        return conn.execute("SELECT id,occurred_at,amount_usdt,note FROM portfolio_cashflows "
                            "ORDER BY occurred_at,id")

    @staticmethod
    def complete_snapshot_count(conn):
        return conn.execute("SELECT COUNT(*) FROM portfolio_snapshots WHERE valuation_complete=1").fetchone()[0]

    @staticmethod
    def complete_snapshots(conn):
        yield from conn.execute("SELECT data FROM portfolio_snapshots WHERE valuation_complete=1 ORDER BY captured_at")

    def acquire_runtime(self):
        if self._runtime_file is not None:
            raise RuntimeError("Trading runtime already active")
        stream = open(str(self.path) + ".lock", "a+b")
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("Trading runtime already active for this database") from exc
        self._runtime_file = stream

    def release_runtime(self):
        if self._runtime_file is not None:
            stream, self._runtime_file = self._runtime_file, None
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path:
            raise ValueError("Backup must use a different path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()


def record_projections(namespace, value):
    """Derive searchable fields without changing legacy JSON serialization."""
    if not isinstance(value, dict):
        return None, None
    bot_id = value.get("bot_id")
    audit_time = value.get("time") if namespace == "audit_events" else None

    def compatible_text(field):
        return field if isinstance(field, str) and "\0" not in field and all(
            not "\ud800" <= character <= "\udfff" for character in field) else None

    return compatible_text(bot_id), compatible_text(audit_time)


class PgStore(Store):
    def __init__(self, database, path, *, scope_id="single"):
        self.database = database
        self.scope_id = scope_id
        # The former SQLite path still identifies the local artifact workspace.
        self.path = Path(path).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_file = None
        self.database.migrate("core", MIGRATIONS / "core")

    def connection(self):
        return self.database.connection()

    @contextmanager
    def write_transaction(self):
        with self.connection() as conn:
            # Protect predicate-based checks such as reservations with no existing row.
            self.database.transaction_lock(conn, f"trading:{self.scope_id}")
            yield conn

    @contextmanager
    def read_snapshot(self):
        with self.connection() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield conn

    def require_runtime(self):
        self.database.require_runtime()

    def write(self, conn, namespace, key, value):
        conn.execute("""INSERT INTO records(scope_id,namespace,key,value,bot_id,audit_time)
                        VALUES(%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(scope_id,namespace,key) DO UPDATE SET value=excluded.value,
                            bot_id=excluded.bot_id,audit_time=excluded.audit_time,updated_at=excluded.updated_at""",
                     (self.scope_id, namespace, str(key), json.dumps(value, allow_nan=False),
                      *record_projections(namespace, value)))

    def append(self, conn, namespace, key, value):
        conn.execute("""INSERT INTO records(scope_id,namespace,key,value,bot_id,audit_time)
                        VALUES(%s,%s,%s,%s,%s,%s)""",
                     (self.scope_id, namespace, str(key), json.dumps(value, allow_nan=False),
                      *record_projections(namespace, value)))

    def read(self, conn, namespace, key, default=None):
        row = conn.execute("SELECT value FROM records WHERE scope_id=%s AND namespace=%s AND key=%s",
                           (self.scope_id, namespace, str(key))).fetchone()
        return json.loads(row[0]) if row else default

    def records(self, conn, namespace):
        rows = conn.execute("SELECT key,value FROM records WHERE scope_id=%s AND namespace=%s ORDER BY key",
                            (self.scope_id, namespace))
        return {key: json.loads(value) for key, value in rows}

    def erase(self, conn, namespace, key):
        conn.execute("DELETE FROM records WHERE scope_id=%s AND namespace=%s AND key=%s",
                     (self.scope_id, namespace, str(key)))

    def list_for_bots(self, namespace, bot_ids):
        if not bot_ids:
            return {}
        with self.connection() as conn:
            rows = conn.execute("SELECT key,value FROM records WHERE scope_id=%s AND namespace=%s "
                                "AND bot_id=ANY(%s) ORDER BY key", (self.scope_id, namespace, list(bot_ids)))
            return {key: json.loads(value) for key, value in rows}

    def recent_audit(self, limit):
        with self.connection() as conn:
            rows = conn.execute("SELECT value FROM records WHERE scope_id=%s AND namespace='audit_events' "
                                "ORDER BY audit_time DESC NULLS LAST, key LIMIT %s", (self.scope_id, limit))
            return [json.loads(raw) for (raw,) in rows]

    def paper_history(self, prefix, limit):
        with self.connection() as conn:
            rows = conn.execute("SELECT value FROM records WHERE scope_id=%s AND namespace='spot_paper_history' "
                                "AND key>=%s AND key<%s ORDER BY key DESC LIMIT %s",
                                (self.scope_id, prefix, prefix + "\uffff", limit)).fetchall()
        return [json.loads(raw) for (raw,) in reversed(rows)]

    def position_owner(self, conn, account, position_id):
        row = conn.execute("SELECT bot_id FROM position_owners WHERE scope_id=%s AND account=%s AND position_id=%s",
                           (self.scope_id, account, str(position_id))).fetchone()
        return row[0] if row else None

    def _claim(self, conn, account, position_id, bot_id):
        conn.execute("INSERT INTO position_owners(scope_id,account,position_id,bot_id) VALUES(%s,%s,%s,%s) "
                     "ON CONFLICT(scope_id,account,position_id) DO NOTHING",
                     (self.scope_id, account, str(position_id), bot_id))

    def upsert_snapshot(self, conn, captured_at, equity, complete, data):
        conn.execute("""INSERT INTO portfolio_snapshots(scope_id,captured_at,equity_usdt,valuation_complete,data)
                        VALUES(%s,%s,%s,%s,%s) ON CONFLICT(scope_id,captured_at) DO UPDATE SET
                            equity_usdt=excluded.equity_usdt,valuation_complete=excluded.valuation_complete,data=excluded.data""",
                     (self.scope_id, captured_at, equity, complete, data))

    def latest_snapshots(self, conn, limit):
        return conn.execute("SELECT data FROM portfolio_snapshots WHERE scope_id=%s ORDER BY captured_at DESC LIMIT %s",
                            (self.scope_id, limit))

    def insert_cashflow(self, conn, occurred_at, amount, note):
        row = conn.execute("INSERT INTO portfolio_cashflows(scope_id,occurred_at,amount_usdt,note) "
                           "VALUES(%s,%s,%s,%s) RETURNING id", (self.scope_id, occurred_at, amount, note)).fetchone()
        return row[0]

    def remove_cashflow(self, conn, flow_id):
        return conn.execute("DELETE FROM portfolio_cashflows WHERE scope_id=%s AND id=%s",
                            (self.scope_id, flow_id)).rowcount > 0

    def cashflows(self, conn):
        return conn.execute("SELECT id,occurred_at,amount_usdt,note FROM portfolio_cashflows "
                            "WHERE scope_id=%s ORDER BY occurred_at,id", (self.scope_id,))

    def complete_snapshot_count(self, conn):
        return conn.execute("SELECT COUNT(*) FROM portfolio_snapshots WHERE scope_id=%s AND valuation_complete=1",
                            (self.scope_id,)).fetchone()[0]

    def complete_snapshots(self, conn):
        with conn.cursor("portfolio_observations") as cursor:
            cursor.execute("SELECT data FROM portfolio_snapshots WHERE scope_id=%s AND valuation_complete=1 "
                           "ORDER BY captured_at", (self.scope_id,))
            yield from cursor

    def backup(self, destination):
        raise ValueError("Use backup-bundle for a PostgreSQL runtime")


_stores = {}
_lock = threading.Lock()


def open_store(path, *, account_id=None):
    database = get_database()
    return Store(path) if database is None else PgStore(database, path, scope_id=account_id or "single")


def get_store():
    path = data_dir() / "trader.sqlite3"
    key = (get_database(), path)
    with _lock:
        if key not in _stores:
            _stores[key] = open_store(path)
        return _stores[key]
