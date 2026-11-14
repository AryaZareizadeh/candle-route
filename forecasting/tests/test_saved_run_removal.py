"""Saved artifact inventory and deletion stay inside managed run directories."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from crypto_forecast.dashboard import DashboardError, JobManager, create_server, utc_now, write_json


@pytest.fixture
def manager(tmp_path):
    value = JobManager(tmp_path)
    yield value
    value.close()


def saved_run(workspace, identifier="saved-run", artifact="model.pt"):
    directory = workspace / "runs" / identifier
    directory.mkdir(parents=True)
    (directory / artifact).write_bytes(b"saved weights")
    return directory


def job_record(manager, identifier, *, status="failed", operation="train", config=None):
    now = utc_now()
    value = {"id": identifier, "name": "Saved training", "operation": operation,
             "status": status, "stage": status, "started_at": now, "updated_at": now,
             "finished_at": None if status in {"queued", "running", "awaiting_decision"} else now,
             "config": config or {"run_dir": str(manager.workspace / "runs" / identifier)},
             "history": [{"epoch": 1, "validation_loss": 0.25}], "logs": [],
             "error": "Evaluation failed" if status == "failed" else None}
    write_json(manager._path(identifier), value)
    return value


def test_removal_preserves_job_history_and_unrelated_artifacts(manager):
    removed = saved_run(manager.workspace)
    retained = saved_run(manager.workspace, "retained-run")
    original = manager.workspace / "models" / "kronos-small" / "model" / "weights.bin"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original weights")
    candles = manager.workspace / "data" / "4h" / "BTC.csv"
    candles.parent.mkdir(parents=True)
    candles.write_text("date,close\n2026-01-01,100\n", encoding="utf-8")
    before = job_record(manager, removed.name)

    result = manager.remove_run(removed.name)

    assert result == {"id": "saved-run", "removed": True}
    assert not removed.exists()
    assert (retained / "model.pt").read_bytes() == b"saved weights"
    assert original.read_bytes() == b"original weights"
    assert candles.read_text(encoding="utf-8") == "date,close\n2026-01-01,100\n"
    after = manager.get_job(removed.name)
    assert after["status"] == "failed"
    assert after["history"] == before["history"]
    assert after["error"] == "Evaluation failed"
    assert after["artifacts_removed_at"]
    assert all(item["id"] != removed.name for item in manager.list_saved_runs())


def test_inventory_includes_failed_partial_tokenizer_and_job_owned_directories(manager):
    failed = saved_run(manager.workspace, "failed-predictor", "predictor.pt")
    tokenizer = saved_run(manager.workspace, "tokenizer-only", "tokenizer.pt")
    partial = saved_run(manager.workspace, "partial-model")
    summary = saved_run(manager.workspace, "summary-only", "summary.json")
    interrupted = manager.workspace / "runs" / "interrupted-before-save"
    interrupted.mkdir()
    (interrupted / "training.log").write_text("stopped before first epoch", encoding="utf-8")
    job_record(manager, failed.name)
    job_record(manager, tokenizer.name, status="stopped")
    job_record(manager, interrupted.name, status="interrupted")
    unrelated = manager.workspace / "runs" / "unrelated-folder"
    unrelated.mkdir()
    (unrelated / "personal.txt").write_text("keep", encoding="utf-8")

    items = {item["id"]: item for item in manager.list_saved_runs()}

    assert set(items) == {failed.name, tokenizer.name, partial.name, summary.name, interrupted.name}
    assert items[failed.name]["name"] == "Saved training"
    assert all(item["id"] != unrelated.name for item in items.values())
    manager.remove_run(interrupted.name)
    assert not interrupted.exists()
    assert (unrelated / "personal.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("identifier", ["original", "original-base", "../outside", "..\\outside",
    "/outside", "C:\\outside", "runs/saved-run", "", None, 7])
def test_removal_rejects_pretrained_identifiers_and_paths(manager, identifier):
    retained = saved_run(manager.workspace)
    with pytest.raises(DashboardError) as error:
        manager.remove_run(identifier)
    assert error.value.status in {400, 404}
    assert (retained / "model.pt").read_bytes() == b"saved weights"


@pytest.mark.parametrize("identifier", ["original", "original-base"])
def test_reserved_pretrained_name_is_protected_even_if_present_under_runs(manager, identifier):
    retained = saved_run(manager.workspace, identifier)
    with pytest.raises(DashboardError):
        manager.remove_run(identifier)
    assert retained.is_dir()


def test_unrecognized_directory_is_not_removable(manager):
    directory = manager.workspace / "runs" / "personal-data"
    directory.mkdir(parents=True)
    (directory / "notes.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(DashboardError) as error:
        manager.remove_run(directory.name)
    assert error.value.status in {400, 404}
    assert (directory / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_directory_with_checkpoint_name_does_not_claim_unmanaged_content(manager):
    directory = manager.workspace / "runs" / "personal-data"
    named_directory = directory / "model.pt"
    named_directory.mkdir(parents=True)
    (named_directory / "notes.txt").write_text("keep", encoding="utf-8")
    assert manager.list_saved_runs() == []
    with pytest.raises(DashboardError):
        manager.remove_run(directory.name)
    assert (named_directory / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_known_training_job_remains_listed_if_checkpoint_access_is_denied(manager, monkeypatch):
    inaccessible = saved_run(manager.workspace, "failed-predictor", "predictor.pt")
    retained = saved_run(manager.workspace, "retained-run")
    job_record(manager, inaccessible.name)
    actual_is_file = Path.is_file

    def is_file(path):
        if path == inaccessible / "predictor.pt":
            raise PermissionError("Checkpoint is temporarily inaccessible")
        return actual_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)

    items = {item["id"]: item for item in manager.list_saved_runs()}

    assert set(items) == {inaccessible.name, retained.name}
    assert items[inaccessible.name]["status"] == "failed"
    assert (retained / "model.pt").read_bytes() == b"saved weights"


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_decision"])
def test_active_training_run_is_not_removable(manager, status):
    directory = saved_run(manager.workspace)
    job_record(manager, directory.name, status=status)
    with pytest.raises(DashboardError) as error:
        manager.remove_run(directory.name)
    assert error.value.status == 409
    assert (directory / "model.pt").is_file()
    assert "artifacts_removed_at" not in manager.get_job(directory.name)


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_decision"])
@pytest.mark.parametrize("reference", ["forecast", "tokenizer"])
def test_active_job_using_saved_artifact_blocks_removal(manager, status, reference):
    directory = saved_run(manager.workspace)
    if reference == "forecast":
        job_record(manager, "active-forecast", status=status, operation="forecast",
                   config={"model": directory.name})
    else:
        job_record(manager, "active-training", status=status,
                   config={"kronos_tokenizer_run_dir": str(directory)})
    with pytest.raises(DashboardError) as error:
        manager.remove_run(directory.name)
    assert error.value.status == 409
    assert directory.is_dir()


def test_old_active_reference_outside_recent_job_window_blocks_removal(manager):
    directory = saved_run(manager.workspace)
    record = job_record(manager, "old-active-forecast", status="running", operation="forecast",
                        config={"model": directory.name})
    record["started_at"] = "2020-01-01T00:00:00+00:00"
    write_json(manager._path(record["id"]), record)
    for index in range(55):
        job_record(manager, f"newer-job-{index}", status="completed")
    with pytest.raises(DashboardError) as error:
        manager.remove_run(directory.name)
    assert error.value.status == 409
    assert directory.is_dir()


def test_finished_forecast_reference_allows_removal(manager):
    directory = saved_run(manager.workspace)
    job_record(manager, "completed-forecast", status="completed", operation="forecast",
               config={"model": directory.name})
    manager.remove_run(directory.name)
    assert not directory.exists()
    assert manager.get_job("completed-forecast")["status"] == "completed"


def test_live_worker_without_readable_job_record_still_blocks_removal(manager):
    directory = saved_run(manager.workspace)

    class RunningProcess:
        def poll(self):
            return None

    manager.processes[directory.name] = RunningProcess()
    try:
        with pytest.raises(DashboardError) as error:
            manager.remove_run(directory.name)
        assert error.value.status == 409
        assert (directory / "model.pt").read_bytes() == b"saved weights"
    finally:
        manager.processes.pop(directory.name, None)


@contextmanager
def directory_link(target, link, kind):
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("Windows junctions require Windows")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"Creating symlinks is unavailable: {error}")
    try:
        yield link
    finally:
        if kind == "junction":
            link.rmdir()
        else:
            link.unlink(missing_ok=True)


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_linked_runs_root_is_never_inventory_or_removal_target(manager, kind):
    outside = manager.workspace / "outside-runs"
    directory = outside / "saved-run"
    directory.mkdir(parents=True)
    (directory / "model.pt").write_bytes(b"outside weights")
    with directory_link(outside, manager.workspace / "runs", kind):
        assert manager.list_saved_runs() == []
        with pytest.raises(DashboardError):
            manager.remove_run("saved-run")
        assert (directory / "model.pt").read_bytes() == b"outside weights"


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_linked_run_directory_is_never_inventory_or_removal_target(manager, kind):
    outside = manager.workspace / "outside-run"
    outside.mkdir()
    (outside / "model.pt").write_bytes(b"outside weights")
    root = manager.workspace / "runs"
    root.mkdir()
    with directory_link(outside, root / "linked-run", kind):
        assert manager.list_saved_runs() == []
        with pytest.raises(DashboardError):
            manager.remove_run("linked-run")
        assert (outside / "model.pt").read_bytes() == b"outside weights"


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_nested_links_reject_whole_removal_before_any_file_is_removed(manager, kind):
    directory = saved_run(manager.workspace)
    outside = manager.workspace / "outside-content"
    outside.mkdir()
    (outside / "private.txt").write_text("keep", encoding="utf-8")
    with directory_link(outside, directory / "linked-content", kind):
        with pytest.raises(DashboardError):
            manager.remove_run(directory.name)
        assert (directory / "model.pt").read_bytes() == b"saved weights"
        assert (outside / "private.txt").read_text(encoding="utf-8") == "keep"


@pytest.fixture
def saved_runs_http(tmp_path):
    directory = saved_run(tmp_path)
    server = create_server(tmp_path, port=0)
    job_record(server.manager, directory.name)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, *, method="GET", value=None, authorized=True, raw=None):
        headers = {"Origin": address}
        if authorized:
            headers["X-CSRF-Token"] = server.csrf_token
        if raw is not None or value is not None:
            headers["Content-Type"] = "application/json"
        data = raw if raw is not None else json.dumps(value).encode() if value is not None else None
        req = urllib.request.Request(address + path, method=method, data=data, headers=headers)
        try:
            response = opener.open(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    try:
        yield server.manager, request, directory
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_inventory_and_authenticated_deletion_preserve_history(saved_runs_http):
    manager, request, directory = saved_runs_http
    status, body = request("/api/saved-runs")
    assert status == 200
    assert body["saved_runs"][0]["id"] == "saved-run"
    status, body = request("/api/runs/saved-run/delete", method="POST", value={})
    assert status == 200
    assert body == {"id": "saved-run", "removed": True}
    assert not directory.exists()
    assert manager.get_job("saved-run")["artifacts_removed_at"]
    assert request("/api/saved-runs") == (200, {"saved_runs": []})


def test_http_deletion_requires_csrf_and_leaves_artifacts(saved_runs_http):
    manager, request, directory = saved_runs_http
    status, _ = request("/api/runs/saved-run/delete", method="POST", value={}, authorized=False)
    assert status == 403
    assert directory.is_dir()
    assert "artifacts_removed_at" not in manager.get_job(directory.name)


@pytest.mark.parametrize("payload", [[], {"path": "../outside"}, None, "delete"])
def test_http_deletion_accepts_only_empty_object(saved_runs_http, payload):
    _, request, directory = saved_runs_http
    status, _ = request("/api/runs/saved-run/delete", method="POST", raw=json.dumps(payload).encode())
    assert status == 400
    assert (directory / "model.pt").is_file()


@pytest.mark.parametrize("status", ["failed", "stopped", "interrupted"])
def test_chart_inventory_can_select_saved_predictor_from_unfinished_evaluation(manager, status):
    import torch

    directory = saved_run(manager.workspace, "recovery-predictor", "predictor.pt")
    summary = {"artifact_type": "kronos-predictor", "created_at": utc_now(),
               "model_type": "kronos-small", "coins": ["BTC"], "training_complete": True,
               "config": {"model_type": "kronos-small", "kronos_timeframe": "4h"}}
    torch.save({"metadata": summary, "model_state_dict": {}, "tokenizer_state_dict": {}},
               directory / "predictor.pt")
    write_json(directory / "predictor_summary.json", summary)
    job_record(manager, directory.name, status=status)

    result = manager.chart_models()

    assert any(item["id"] == directory.name and item["model_type"] == "kronos-small"
               for item in result["runs"])
    assert {item["id"] for item in result["pretrained"]} == {"original", "original-base"}


@pytest.mark.parametrize("summary_state", ["missing", "stale", "corrupt", "malformed"])
def test_recovery_predictor_uses_checkpoint_metadata_after_incomplete_summary_publication(manager, summary_state):
    import torch

    directory = saved_run(manager.workspace, "recovery-predictor", "predictor.pt")
    authoritative = {"artifact_type": "kronos-predictor", "created_at": utc_now(),
                     "model_type": "kronos-small", "coins": ["BTC"], "best_epoch": 4,
                     "training_complete": True,
                     "config": {"model_type": "kronos-small", "kronos_timeframe": "1h"}}
    torch.save({"metadata": authoritative, "model_state_dict": {}, "tokenizer_state_dict": {}},
               directory / "predictor.pt")
    summary = directory / "predictor_summary.json"
    if summary_state == "stale":
        write_json(summary, {**authoritative, "best_epoch": 2,
                             "config": {"model_type": "kronos-small", "kronos_timeframe": "4h"}})
        checkpoint_time = (directory / "predictor.pt").stat().st_mtime_ns
        os.utime(summary, ns=(checkpoint_time - 1000000000, checkpoint_time - 1000000000))
    elif summary_state == "corrupt":
        summary.write_text("broken", encoding="utf-8")
    elif summary_state == "malformed":
        write_json(summary, {"artifact_type": "kronos-predictor"})
    job_record(manager, directory.name)

    selected = manager._saved_model_summary(directory.name)

    assert selected["checkpoint_file"] == "predictor.pt"
    assert selected["best_epoch"] == 4
    assert selected["config"]["kronos_timeframe"] == "1h"
    assert any(item["id"] == directory.name for item in manager.chart_models()["runs"])


def test_forecast_from_failed_run_selects_recovery_predictor_over_final_model(manager, monkeypatch):
    import torch
    from crypto_forecast import chart_forecast

    directory = saved_run(manager.workspace, "failed-predictor", "model.pt")
    metadata = {"artifact_type": "kronos-predictor", "model_type": "kronos-small",
                "config": {"model_type": "kronos-small", "kronos_timeframe": "1h"}}
    torch.save({"metadata": metadata}, directory / "predictor.pt")
    write_json(directory / "predictor_summary.json", metadata)
    write_json(directory / "summary.json", {"model_type": "kronos-small",
               "config": {"model_type": "kronos-small", "kronos_timeframe": "4h"}})
    job_record(manager, directory.name)
    launches = []
    monkeypatch.setattr(manager, "_launch", lambda job: launches.append(job))

    def prepare_chart(workspace, payload):
        assert workspace == manager.workspace
        assert payload == {"model": directory.name}
        return {"model": directory.name, "timeframe": "1h", "horizon": 2, "coin": "BTC"}, {}

    monkeypatch.setattr(chart_forecast, "prepare_chart", prepare_chart)

    forecast = manager.start_chart_forecast({"model": directory.name})

    assert forecast["config"]["checkpoint_file"] == "predictor.pt"
    assert launches[0]["config"]["checkpoint_file"] == "predictor.pt"
    assert (directory / "model.pt").read_bytes() == b"saved weights"


def test_forecast_rechecks_source_if_removal_finishes_after_initial_validation(manager, monkeypatch):
    from crypto_forecast import chart_forecast

    directory = saved_run(manager.workspace, "selected-model")
    write_json(directory / "summary.json", {"model_type": "kronos-small",
               "config": {"model_type": "kronos-small", "kronos_timeframe": "1h"}})
    real_summary = manager._saved_model_summary
    launches = []
    monkeypatch.setattr(manager, "_launch", lambda job: launches.append(job))
    monkeypatch.setattr(chart_forecast, "prepare_chart", lambda workspace, payload:
        ({"model": directory.name, "timeframe": "1h", "horizon": 2, "coin": "BTC"}, {}))

    def validate_then_remove(identifier):
        summary = real_summary(identifier)
        manager.remove_run(identifier)
        return summary

    monkeypatch.setattr(manager, "_saved_model_summary", validate_then_remove)

    with pytest.raises(DashboardError):
        manager.start_chart_forecast({"model": directory.name})

    assert not directory.exists()
    assert launches == []
    assert manager.list_jobs() == []


def test_training_rechecks_tokenizer_if_removal_finishes_after_initial_validation(manager, monkeypatch):
    directory = saved_run(manager.workspace, "selected-tokenizer", "tokenizer.pt")
    real_tokenizer_path = manager._tokenizer_path
    launches = []
    monkeypatch.setattr(manager, "_launch", lambda job: launches.append(job))

    def validate_then_remove(identifier):
        source = real_tokenizer_path(identifier)
        manager.remove_run(identifier)
        return source

    monkeypatch.setattr(manager, "_tokenizer_path", validate_then_remove)

    with pytest.raises(DashboardError):
        manager.start_job({"operation": "train", "config": {"model_type": "kronos-small",
            "kronos_training_stage": "predictor", "kronos_tokenizer_run_id": directory.name}})

    assert not directory.exists()
    assert launches == []
    assert manager.list_jobs() == []
