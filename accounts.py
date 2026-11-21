"""Account identities, password recovery, and encrypted exchange credentials."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager, nullcontext
from pathlib import Path

from cryptography.fernet import Fernet

from auth import hash_password, verify_password
from postgres import MIGRATIONS, get_database


def canonical_email(email):
    return email.strip().lower() if isinstance(email, str) else ""


def _lookup_text(value):
    return value.lower() if value.isascii() and "\0" not in value else None


class ExchangeIdentityConflict(ValueError):
    """The supplied exchange identity already belongs to another tenant."""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Accounts:
    registration_conflict = sqlite3.IntegrityError

    def __init__(self, path: Path, master_key: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._configure_cipher(master_key)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS verification (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    code_hash TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS recovery (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    code_hash TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    encrypted BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchange_ownership (
                    testnet INTEGER NOT NULL,
                    exchange_user TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    PRIMARY KEY(testnet, exchange_user)
                );
            """)

    def _configure_cipher(self, master_key):
        self.cipher = Fernet(master_key.encode())
        self._identity_key = hmac.digest(master_key.encode(), b"dashboard-exchange-identity-v1", "sha256")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, factory=_ClosingConnection)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def write_transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def _execute(conn, sql, params=()):
        return conn.execute(sql, params)

    def register(self, username: str, email: str, password: str, invitation_code: str, expected_code: str):
        if not all(isinstance(value, str) for value in (username, email, password, invitation_code, expected_code)):
            raise ValueError("Invalid registration request")
        if not expected_code or not hmac.compare_digest(invitation_code, expected_code):
            raise PermissionError("Invalid invitation code")
        username = username.strip()
        email = canonical_email(email)
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", username):
            raise ValueError("Username must be 3–40 letters, digits, underscores or hyphens")
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email) or len(email) > 254:
            raise ValueError("Invalid email address")
        encoded = hash_password(password)
        user_id = secrets.token_hex(16)
        try:
            with self.write_transaction() as conn:
                self._execute(conn, "DELETE FROM users WHERE verified=0 AND id IN "
                              "(SELECT user_id FROM verification WHERE expires_at < ?)", (time.time(),))
                self._execute(conn, "DELETE FROM users WHERE verified=0 AND lower(username)=? AND lower(email)=?",
                              (username.lower(), email))
                self._execute(conn, "INSERT INTO users(id,username,email,password_hash) VALUES(?,?,?,?)",
                              (user_id, username, email, encoded))
        except self.registration_conflict as exc:
            raise ValueError("Username or email already registered") from exc
        return {"id": user_id, "username": username, "email": email}

    def issue_verification(self, user_id: str):
        code = f"{secrets.randbelow(100000000):08d}"
        with self.write_transaction() as conn:
            self._execute(conn, "INSERT INTO verification(user_id,code_hash,expires_at,attempts) VALUES(?,?,?,0) "
                          "ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,"
                          "expires_at=excluded.expires_at,attempts=0",
                          (user_id, hashlib.sha256(code.encode()).hexdigest(), time.time() + 600))
        return code

    def discard_unverified(self, user_id: str):
        with self.write_transaction() as conn:
            self._execute(conn, "DELETE FROM users WHERE id=? AND verified=0", (user_id,))

    def verify_email(self, email: str, code: str):
        user = self.by_email(email)
        if not user or not isinstance(code, str) or not re.fullmatch(r"\d{8}", code):
            return False
        with self.write_transaction() as conn:
            row = self._execute(conn, "SELECT code_hash,expires_at,attempts FROM verification WHERE user_id=?",
                                (user["id"],)).fetchone()
            if not row or row[1] < time.time() or row[2] >= 5:
                return False
            self._execute(conn, "UPDATE verification SET attempts=attempts+1 WHERE user_id=?", (user["id"],))
            if not hmac.compare_digest(row[0], hashlib.sha256(code.encode()).hexdigest()):
                return False
            self._execute(conn, "UPDATE users SET verified=1 WHERE id=?", (user["id"],))
            self._execute(conn, "DELETE FROM verification WHERE user_id=?", (user["id"],))
        return True

    def login(self, username: str, password: str):
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        with self.connect() as conn:
            row = self._execute(conn, "SELECT id,username,email,password_hash,verified FROM users "
                                "WHERE lower(username)=?", (_lookup_text(username),)).fetchone()
        # Spend comparable work for unknown accounts.
        encoded = row[3] if row else _DUMMY_HASH
        valid = verify_password(password, encoded)
        if not row or not valid or not row[4]:
            return None
        return {"id": row[0], "username": row[1], "email": row[2]}

    def by_email(self, email: str):
        if not isinstance(email, str):
            return None
        with self.connect() as conn:
            row = self._execute(conn, "SELECT id,username,email,verified FROM users WHERE lower(email)=?",
                                (_lookup_text(canonical_email(email)),)).fetchone()
        return {"id": row[0], "username": row[1], "email": row[2], "verified": bool(row[3])} if row else None

    def by_username(self, username: str):
        if not isinstance(username, str):
            return None
        with self.connect() as conn:
            row = self._execute(conn, "SELECT id,username,email,verified FROM users WHERE lower(username)=?",
                                (_lookup_text(username),)).fetchone()
        return {"id": row[0], "username": row[1], "email": row[2], "verified": bool(row[3])} if row else None

    def issue_recovery(self, user_id: str):
        code = f"{secrets.randbelow(100000000):08d}"
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.write_transaction() as conn:
            self._execute(conn, "INSERT INTO recovery(user_id,code_hash,expires_at,attempts) VALUES(?,?,?,0) "
                          "ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,"
                          "expires_at=excluded.expires_at,attempts=0", (user_id, digest, time.time() + 600))
        return code

    def reset_password(self, email: str, code: str, password: str):
        user = self.by_email(email)
        if not user or not user["verified"] or not isinstance(code, str) or not re.fullmatch(r"\d{8}", code):
            return False
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.write_transaction() as conn:
            row = self._execute(conn, "SELECT code_hash,expires_at,attempts FROM recovery WHERE user_id=?",
                                (user["id"],)).fetchone()
            if not row or row[1] < time.time() or row[2] >= 5:
                return False
            self._execute(conn, "UPDATE recovery SET attempts=attempts+1 WHERE user_id=?", (user["id"],))
            if not hmac.compare_digest(row[0], digest):
                return False
            # Commit the attempt before expensive hashing so account writes can continue.
        encoded = hash_password(password)
        with self.write_transaction() as conn:
            current = self._execute(conn, "SELECT code_hash,expires_at FROM recovery WHERE user_id=?",
                                    (user["id"],)).fetchone()
            if not current or current[1] < time.time() or not hmac.compare_digest(current[0], digest):
                return False
            self._execute(conn, "UPDATE users SET password_hash=? WHERE id=?", (encoded, user["id"]))
            self._execute(conn, "DELETE FROM recovery WHERE user_id=?", (user["id"],))
        return True

    def credentials(self, user_id: str):
        with self.connect() as conn:
            row = self._execute(conn, "SELECT encrypted FROM credentials WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else {}

    def profile_identity(self, profile: dict):
        """Derive an opaque stable owner from a credential-verified profile."""
        if not isinstance(profile, dict):
            raise ValueError("Exchange profile identity unavailable")
        for field in ("id", "userId"):
            value = profile.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                return str(value).strip()
        verified = profile.get("verifications")
        national_code = profile.get("nationalCode")
        if not isinstance(verified, dict) or verified.get("identity") is not True or not isinstance(national_code, str):
            raise ValueError("Verified exchange profile identity unavailable")
        try:
            normalized = "".join(str(unicodedata.decimal(char)) for char in national_code.strip())
        except (ValueError, TypeError):
            raise ValueError("Verified exchange profile identity unavailable") from None
        if len(normalized) != 10:
            raise ValueError("Verified exchange profile identity unavailable")
        return "national:" + hmac.new(self._identity_key, ("national:" + normalized).encode(), "sha256").hexdigest()

    def set_credentials(self, user_id: str, values: dict, verified_exchange_id=None, *, connection=None):
        """Replace credentials and claim a verified exchange identity atomically.

        Existing account_id remains stable for durable bot ownership. Legacy
        unverified credentials are also checked by credential fingerprint; their
        owners are never silently reassigned during migration.

        A caller-provided connection must already hold the account write lock.
        """
        values = dict(values)
        if isinstance(verified_exchange_id, dict):
            verified_exchange_id = verified_exchange_id.get("id", verified_exchange_id.get("userId"))
        if verified_exchange_id is not None:
            if isinstance(verified_exchange_id, bool) or not isinstance(verified_exchange_id, (str, int)):
                raise ValueError("Invalid exchange identity")
            verified_exchange_id = str(verified_exchange_id).strip()
            if not verified_exchange_id or len(verified_exchange_id) > 254:
                raise ValueError("Invalid exchange identity")
        scope = int(bool(values.get("testnet")))
        fingerprint = self._credential_fingerprint(values)
        transaction = self.write_transaction() if connection is None else nullcontext(connection)
        with transaction as conn:
            previous = {}
            for owner, blob in conn.execute("SELECT user_id,encrypted FROM credentials"):
                saved = json.loads(self.cipher.decrypt(blob))
                if owner == user_id:
                    previous = saved
                elif int(bool(saved.get("testnet"))) == scope:
                    if fingerprint and self._credential_fingerprint(saved) == fingerprint:
                        raise ExchangeIdentityConflict("Exchange account already assigned")
                    if verified_exchange_id and str(saved.get("verified_exchange_id", "")) == verified_exchange_id:
                        raise ExchangeIdentityConflict("Exchange account already assigned")
            if verified_exchange_id is None and fingerprint == self._credential_fingerprint(previous) and scope == int(bool(previous.get("testnet"))):
                verified_exchange_id = previous.get("verified_exchange_id")
            if verified_exchange_id:
                owner = self._execute(conn, "SELECT user_id FROM exchange_ownership WHERE testnet=? AND exchange_user=?",
                                      (scope, verified_exchange_id)).fetchone()
                if owner and owner[0] != user_id:
                    raise ExchangeIdentityConflict("Exchange account already assigned")
            # A replacement releases the prior profile identity only in the same
            # transaction that successfully saves the new encrypted credentials.
            self._execute(conn, "DELETE FROM exchange_ownership WHERE user_id=?", (user_id,))
            if verified_exchange_id:
                self._execute(conn, "INSERT INTO exchange_ownership(testnet,exchange_user,user_id) VALUES(?,?,?)",
                              (scope, verified_exchange_id, user_id))
                values["verified_exchange_id"] = verified_exchange_id
            else:
                values.pop("verified_exchange_id", None)
            values["account_id"] = previous.get("account_id", values.get("account_id", user_id))
            encrypted = self.cipher.encrypt(json.dumps(values).encode())
            self._execute(conn, "INSERT INTO credentials(user_id,encrypted) VALUES(?,?) "
                          "ON CONFLICT(user_id) DO UPDATE SET encrypted=excluded.encrypted", (user_id, encrypted))

    @staticmethod
    def _credential_fingerprint(values):
        identity = values.get("public_key") or values.get("token")
        kind = "key" if values.get("public_key") else "token"
        return hashlib.sha256((kind + ":" + identity).encode()).hexdigest() if isinstance(identity, str) and identity else None


class PgAccounts(Accounts):
    def __init__(self, database, master_key):
        from psycopg.errors import UniqueViolation

        self.database = database
        self.registration_conflict = UniqueViolation
        self._configure_cipher(master_key)
        self.database.migrate("core", MIGRATIONS / "core")

    def connect(self):
        return self.database.connection()

    @contextmanager
    def write_transaction(self):
        with self.connect() as conn:
            # Account writes include cross-user checks on encrypted credentials.
            self.database.transaction_lock(conn, "accounts")
            yield conn

    @staticmethod
    def _execute(conn, sql, params=()):
        # Account queries use positional parameters and contain no literal question marks.
        return conn.execute(sql.replace("?", "%s"), params)


def open_accounts(path, master_key):
    database = get_database()
    return Accounts(path, master_key) if database is None else PgAccounts(database, master_key)


_DUMMY_HASH = "scrypt$16384$8$1$00000000000000000000000000000000$" + "0" * 64
