from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def test_postgres_runtime_lease_does_not_reconnect_after_session_loss(postgres_database):
    database = postgres_database
    from postgres import PostgresDatabase

    contender = PostgresDatabase(database.dsn)
    with database.runtime_lease():
        database.require_runtime()
        with pytest.raises(RuntimeError, match="already in use"), contender.runtime_lease():
            pass
        pid = database._runtime.info.backend_pid
        with database.connection() as conn:
            assert conn.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,)).fetchone()[0]
        with pytest.raises(RuntimeError, match="ownership is unavailable"):
            database.require_runtime()
        with contender.runtime_lease():
            contender.require_runtime()
            with pytest.raises(RuntimeError, match="ownership is unavailable"):
                database.require_runtime()
    contender.close()


def test_postgres_dashboard_starts_ready_and_rejects_an_unmigrated_directory(postgres_database, tmp_path):
    from server import create_app
    from storage import Store

    with TestClient(create_app(offline=True, security=False)) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/readyz").json() == {"ok": True}
        assert not list(tmp_path.glob("*.sqlite3"))

    Store(Path(tmp_path) / "trader.sqlite3")
    with pytest.raises(RuntimeError, match="Run migrate-postgres"), TestClient(
            create_app(offline=True, security=False)):
        pass
