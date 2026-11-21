import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from accounts import ExchangeIdentityConflict, open_accounts
from bot import Bot, BotConfig
from exchange_execution import OrderExecutor
from portfolio import PortfolioHistory
from storage import open_store
from tests.test_recovery import Exchange


def test_postgres_preserves_scope_and_transaction_boundaries(postgres_database, tmp_path):
    first = open_store(tmp_path / "first" / "trader.sqlite3", account_id="first")
    second = open_store(tmp_path / "second" / "trader.sqlite3", account_id="second")
    first.put("bot_trades", "shared", {"bot_id": "runner", "note": "\0"})
    second.put("bot_trades", "shared", {"bot_id": "runner", "note": "private"})
    assert first.list_for_bots("bot_trades", ["runner"]) == {
        "shared": {"bot_id": "runner", "note": "\0"},
    }
    first.put("prefs", "null", None)
    assert first.get("prefs", "null", "default") is None

    with pytest.raises(RuntimeError, match="abort"):
        with first.write_transaction() as conn:
            first.write(conn, "runtime", "bot", {"balance": 10})
            first.append(conn, "audit_events", "event", {"bot_id": "bot"})
            raise RuntimeError("abort")
    assert first.list("runtime") == first.list("audit_events") == {}

    first.claim_position("exchange", 1, "one")
    second.claim_position("exchange", 1, "two")
    with pytest.raises(ValueError, match="owned"):
        first.claim_position("exchange", 1, "two")
    assert first.owner("exchange", 1) == "one"
    assert second.owner("exchange", 1) == "two"

    history = PortfolioHistory(first)
    when = "2026-01-01T00:00:00+00:00"
    history.save_snapshot(dict(captured_at=when, valuation_complete=True, equity_usdt=100))
    flow_id = history.add_flow(25, when, "deposit")
    assert history.performance()["period_start"] == when
    assert PortfolioHistory(second).snapshots() == []
    assert not PortfolioHistory(second).delete_flow(flow_id)
    assert history.flows()[0]["id"] == flow_id
    assert not first.path.exists() and not second.path.exists()


def test_postgres_exchange_credential_claim_is_atomic(postgres_database, tmp_path):
    accounts = open_accounts(tmp_path / "accounts.sqlite3", Fernet.generate_key().decode())
    password = "test-password-long-enough"
    users = [accounts.register(name, f"{name}@example.com", password, "invite", "invite")
             for name in ("Alice", "Bob")]
    for user in users:
        assert accounts.verify_email(user["email"], accounts.issue_verification(user["id"]))
    assert accounts.login("aLiCe", password) == users[0]
    assert accounts.login("alice\0", password) is None
    with pytest.raises(ValueError, match="already registered"):
        accounts.register("ALICE", "other@example.com", password, "invite", "invite")

    start = Barrier(2)

    def assign(user):
        start.wait(timeout=10)
        try:
            accounts.set_credentials(user["id"], {"token": "test-token"})
        except ExchangeIdentityConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(assign, users)) == [False, True]
    saved = [accounts.credentials(user["id"]) for user in users]
    assert [record["token"] for record in saved if record] == ["test-token"]
    with postgres_database.connection() as conn:
        assert b"test-token" not in conn.execute("SELECT encrypted FROM credentials").fetchone()[0]


def test_postgres_serializes_reservations_and_checks_live_lease(postgres_database, tmp_path):
    store = open_store(tmp_path / "trader.sqlite3")
    exchange = Exchange()
    start = Barrier(2)

    def reserve(name):
        bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100, mode="real", id=name))
        bot._store = store
        order = OrderExecutor(bot, exchange)
        start.wait(timeout=10)
        try:
            intent = order._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])
        except RuntimeError as exc:
            assert "reserved" in str(exc)
            return None
        return order, intent

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [result for result in executor.map(reserve, ("one", "two")) if result]
    [(order, intent)] = winners
    stale_intent = dict(intent)
    with pytest.raises(RuntimeError, match="runtime ownership"):
        asyncio.run(order.submit(intent))
    assert not exchange.sent
    assert store.get("orders", intent["client_order_id"])["state"] == "prepared"

    async def submit_twice():
        return await asyncio.gather(order.submit(dict(intent)), order.submit(dict(intent)),
                                    return_exceptions=True)

    with postgres_database.runtime_lease():
        results = asyncio.run(submit_twice())
    assert results.count(None) == 1
    assert any(isinstance(result, RuntimeError) and "already submitted" in str(result) for result in results)
    assert len(exchange.sent) == 1
    assert store.get("orders", intent["client_order_id"])["state"] == "acknowledged"
    asyncio.run(order.refresh(stale_intent))
    assert store.get("orders", intent["client_order_id"])["state"] == "acknowledged"


def test_postgres_rolls_back_order_state_failure_before_exchange(postgres_database, tmp_path, monkeypatch):
    store = open_store(tmp_path / "trader.sqlite3")
    bot = Bot(BotConfig("btcusdt", "AdvRSI", "60", 100, mode="real", id="one"))
    bot._store = store
    exchange = Exchange()
    order = OrderExecutor(bot, exchange)
    intent = order._new("open", "btc", "usdt", "buy", {"amount": "1", "price": "100"}, [])

    def failed_snapshot():
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(bot, "runtime_record", failed_snapshot)
    with postgres_database.runtime_lease(), pytest.raises(RuntimeError, match="snapshot failed"):
        asyncio.run(order.submit(intent))
    assert not exchange.sent
    assert store.get("orders", intent["client_order_id"])["state"] == "prepared"
    assert len(store.recent_audit(100)) == 1


def test_postgres_dashboard_keeps_accounts_and_bots_private_after_restart(postgres_database, tmp_path, monkeypatch):
    from multiuser import create_multiuser_app

    monkeypatch.setenv("TRADER_INVITE_CODE", "test-only-invite-code")
    monkeypatch.setenv("TRADER_MASTER_KEY", Fernet.generate_key().decode())
    sent = {}

    def send_code(email, code, purpose):
        sent[email] = code

    def create_dashboard():
        return create_multiuser_app(data_root=tmp_path, offline=True, allowed_hosts={"testserver"},
                                    send_code=send_code)

    def login(client, username):
        response = client.post("/api/auth/login", json={"username": username, "password": "test-password-long-enough"})
        assert response.status_code == 200
        return {"X-CSRF-Token": response.json()["csrf_token"]}

    with TestClient(create_dashboard()) as client:
        for name in ("Alice", "Bob"):
            email = f"{name.lower()}@example.test"
            registered = client.post("/api/auth/register", json={
                "username": name, "email": email, "password": "test-password-long-enough",
                "invitation_code": "test-only-invite-code",
            })
            assert registered.status_code == 201
            assert client.post("/api/auth/register/verify", json={"email": email, "code": sent[email]}).status_code == 200
        headers = login(client, "alice")
        created = client.post("/api/bots", headers=headers, json={
            "symbol": "btcusdt", "strategy_name": "AdvRSI", "timeframe": "60", "credit": 100,
        })
        assert created.status_code == 201
        bot_id = created.json()["id"]
        assert client.get("/api/runtime/status").json()["audit"][0]["bot_id"] == bot_id
        login(client, "bob")
        assert client.get("/api/bots").json() == []
        assert client.get(f"/api/bots/{bot_id}").status_code == 404
        assert client.get("/api/runtime/status").json()["audit"] == []

    with TestClient(create_dashboard()) as restarted:
        assert restarted.get("/api/bots").status_code == 401
        login(restarted, "ALICE")
        assert [bot["id"] for bot in restarted.get("/api/bots").json()] == [bot_id]
    assert not (tmp_path / "accounts.sqlite3").exists()
    assert not list(tmp_path.rglob("trader.sqlite3"))
