"""The combined app keeps forecast requests inside the active trading account."""

import json

from fastapi.testclient import TestClient
import pytest

from auth import COOKIE, hash_password
from forecast_routes import ForecastWorkspace
from multiuser import create_multiuser_app
from server import create_app


PASSWORD = "long password for testing"
KEY = "T8uO8gnZoHUruIXYMHau_cCn-3mV6D0bMLDRLgaCSf8="


def test_forecast_uses_dashboard_session_and_handles_missing_dependencies(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "testserver")

    def unavailable(*args):
        raise ImportError("missing PyTorch")

    monkeypatch.setattr(ForecastWorkspace, "read", unavailable)
    with TestClient(create_app(offline=True)) as client:
        assert client.get("/forecasting/").status_code == 401
        assert client.get("/forecasting/api/bootstrap").status_code == 401
        login = client.post("/api/auth/login", json={"password": PASSWORD})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]

        page = client.get("/forecasting/")
        assert page.status_code == 200
        assert page.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
        assert client.get("/").headers["x-frame-options"] == "DENY"
        assert client.get("/forecasting/app.js").headers["x-frame-options"] == "DENY"
        assert client.get("/forecasting/../server.py").status_code == 404
        assert client.get("/forecasting/api/bootstrap").status_code == 503
        stale = client.get("/forecasting/api/bootstrap", headers={"X-CSRF-Token": "stale"})
        assert stale.status_code == 401
        assert client.post("/forecasting/api/jobs", json={}).status_code == 403
        cross_origin = client.post("/forecasting/api/jobs", json={},
                                   headers={"Origin": "https://other.example", "X-CSRF-Token": csrf})
        assert cross_origin.status_code == 403
        too_large = client.post("/forecasting/api/jobs", content=b" " * 65537,
                                headers={"X-CSRF-Token": csrf})
        assert too_large.status_code == 400


def test_forecast_api_isolates_saved_jobs_by_account(tmp_path, monkeypatch):
    pytest.importorskip("crypto_forecast.dashboard")
    monkeypatch.setenv("TRADER_MASTER_KEY", KEY)
    monkeypatch.setenv("TRADER_INVITE_CODE", "invite-secret-for-testing")
    app = create_multiuser_app(data_root=tmp_path, offline=True, allowed_hosts={"testserver"},
                               send_code=lambda *args: None)
    accounts = app.state.accounts
    sessions = {}
    with TestClient(app) as client:
        for username in ("alice", "bob"):
            email = f"{username}@example.test"
            user = accounts.register(username, email, PASSWORD, "invite", "invite")
            assert accounts.verify_email(email, accounts.issue_verification(user["id"]))
            login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
            assert login.status_code == 200
            sessions[username] = (client.cookies.get(COOKIE), login.json()["csrf_token"])
            bootstrap = client.get("/forecasting/api/bootstrap")
            assert bootstrap.status_code == 200, bootstrap.text
            assert bootstrap.json()["csrf_token"] == sessions[username][1]
            assert bootstrap.json()["settings_scope"] == user["id"]
            assert bootstrap.json()["embedded"] is True
            assert bootstrap.json()["jobs"] == []
            assert client.get("/forecasting/").headers["x-frame-options"] == "SAMEORIGIN"
            jobs = tmp_path / "users" / user["id"] / "forecasting" / ".dashboard" / "jobs"
            assert jobs.is_dir()
            (jobs / f"{username}-job.json").write_text(json.dumps({
                "id": f"{username}-job",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:01+00:00",
                "status": "completed",
            }), encoding="utf-8")
            client.cookies.clear()

        cookie, token = sessions["alice"]
        client.cookies.set(COOKIE, cookie)
        headers = {"X-CSRF-Token": token}
        jobs = client.get("/forecasting/api/jobs", headers=headers).json()["jobs"]
        assert [job["id"] for job in jobs] == ["alice-job"]
        assert client.get("/forecasting/api/jobs/bob-job", headers=headers).status_code == 404
        assert client.get("/forecasting/api/jobs", headers={
            "X-CSRF-Token": sessions["bob"][1],
        }).status_code == 401
        response = client.post("/forecasting/api/jobs", headers=headers,
                               json={"operation": "download"})
        assert response.status_code == 503
        assert "offline" in response.json()["error"]
        prohibited = ("data_dir", "run_dir", "kronos_model_path", "kronos_tokenizer_path",
                      "kronos_tokenizer_run_dir")
        for field in prohibited:
            result = client.post("/forecasting/api/jobs", headers=headers, json={
                "operation": "train", "config": {field: str(tmp_path)},
            })
            assert result.status_code == 400
            assert result.json()["error"] == "Unknown training setting. Data and runs stay inside this project."
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        assert client.get("/forecasting/api/jobs").status_code == 401
