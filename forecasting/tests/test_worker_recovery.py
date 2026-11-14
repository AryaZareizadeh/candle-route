"""Worker failures retain a truthful, confined record of saved predictor weights."""

from dataclasses import asdict
import os
from pathlib import Path

import pytest
import torch

from crypto_forecast import dashboard_worker
from crypto_forecast.dashboard import read_json, write_json
from crypto_forecast.pipeline import TrainingConfig


def _job(tmp_path, **config_changes):
    config = TrainingConfig(coins=["BTC", "FIL"], model_type="kronos-small",
                            run_dir=str(tmp_path / "runs" / "train-recovery"),
                            **config_changes)
    path = tmp_path / ".dashboard" / "jobs" / "train-recovery.json"
    write_json(path, {"id": "train-recovery", "operation": "train", "status": "queued",
                      "stage": "starting", "config": asdict(config), "logs": [],
                      "progress": {"epoch": 0, "max_epochs": 60}, "history": [],
                      "pending_decision": None})
    return path, config


def _checkpoint(config, *, complete=True):
    directory = Path(config.run_dir)
    directory.mkdir(parents=True)
    summary = {"artifact_type": "kronos-predictor", "best_epoch": 19,
               "training_complete": complete, "evaluation_complete": False,
               "config": asdict(config),
               "history": [{"epoch": 19, "validation_loss": 0.2}]}
    torch.save({"state_dict": {"weight": torch.tensor([1.0])}, "metadata": summary},
               directory / "predictor.pt")
    write_json(directory / "predictor_summary.json", summary)
    return directory


@pytest.mark.parametrize("complete", [True, False])
def test_failed_training_reports_saved_predictor_without_claiming_evaluation(tmp_path, monkeypatch, complete):
    path, config = _job(tmp_path)

    def fail_training(actual_config, **callbacks):
        _checkpoint(actual_config, complete=complete)
        raise ValueError("FIL: predicted close must be finite and positive")

    monkeypatch.setattr(dashboard_worker, "train", fail_training)
    assert dashboard_worker.run_job(path) == 1
    result = read_json(path)
    assert result["status"] == result["stage"] == "failed"
    assert result["error"] == "FIL: predicted close must be finite and positive"
    assert result["saved_checkpoint"] == {
        "checkpoint_path": str((Path(config.run_dir) / "predictor.pt").resolve()),
        "summary_path": str((Path(config.run_dir) / "predictor_summary.json").resolve()),
        "best_epoch": 19, "training_complete": complete, "evaluation_complete": False,
    }
    messages = "\n".join(row["message"] for row in result["logs"])
    assert "predictor.pt" in messages and "19" in messages
    assert "evaluation" in messages.lower() and "not complete" in messages.lower()
    assert "training completed" in messages.lower() if complete else "training interrupted" in messages.lower()


def test_checkpoint_event_persists_recovery_info_before_training_returns(tmp_path, monkeypatch):
    path, config = _job(tmp_path)

    def fail_training(actual_config, *, progress, on_event):
        directory = _checkpoint(actual_config, complete=False)
        on_event({"kind": "checkpoint", "checkpoint_path": str((directory / "predictor.pt").resolve()),
                  "best_epoch": 19, "training_complete": False, "evaluation_complete": False})
        running = read_json(path)
        assert running["status"] == "running"
        assert running["saved_checkpoint"]["best_epoch"] == 19
        assert running["saved_checkpoint"]["training_complete"] is False
        assert "saved" in running["logs"][-1]["message"].lower()
        raise ValueError("next validation failed")

    monkeypatch.setattr(dashboard_worker, "train", fail_training)
    assert dashboard_worker.run_job(path) == 1
    result = read_json(path)
    assert result["error"] == "next validation failed"
    assert result["saved_checkpoint"]["checkpoint_path"] == str((Path(config.run_dir) / "predictor.pt").resolve())


@pytest.mark.parametrize("invalid", ["missing_weights", "empty_weights", "corrupt_weights", "missing_summary", "bad_json",
                                     "stale_summary", "wrong_type", "wrong_config", "invalid_best_epoch"])
def test_invalid_checkpoint_is_not_offered_as_verified_recovery(tmp_path, monkeypatch, invalid):
    path, config = _job(tmp_path)

    def fail_training(actual_config, **callbacks):
        directory = _checkpoint(actual_config)
        if invalid == "missing_weights":
            (directory / "predictor.pt").unlink()
        elif invalid == "empty_weights":
            (directory / "predictor.pt").write_bytes(b"")
        elif invalid == "corrupt_weights":
            (directory / "predictor.pt").write_bytes(b"broken checkpoint")
        elif invalid == "missing_summary":
            (directory / "predictor_summary.json").unlink()
        elif invalid == "bad_json":
            (directory / "predictor_summary.json").write_text("broken", encoding="utf-8")
        elif invalid == "stale_summary":
            checkpoint_time = (directory / "predictor.pt").stat().st_mtime_ns
            os.utime(directory / "predictor_summary.json", ns=(checkpoint_time - 1_000_000_000,
                                                               checkpoint_time - 1_000_000_000))
        else:
            summary = read_json(directory / "predictor_summary.json")
            if invalid == "wrong_type":
                summary["artifact_type"] = "kronos-tokenizer"
            elif invalid == "wrong_config":
                summary["config"]["run_dir"] = str(tmp_path / "outside")
            else:
                summary["best_epoch"] = True
            write_json(directory / "predictor_summary.json", summary)
        raise ValueError("validation failed")

    monkeypatch.setattr(dashboard_worker, "train", fail_training)
    assert dashboard_worker.run_job(path) == 1
    result = read_json(path)
    assert result["status"] == "failed" and result["error"] == "validation failed"
    assert "saved_checkpoint" not in result
    if invalid not in {"missing_weights", "empty_weights"}:
        messages = "\n".join(row["message"] for row in result["logs"])
        assert "could not be verified" in messages and "predictor.pt" in messages


def test_checkpoint_event_cannot_publish_an_unrelated_path(tmp_path, monkeypatch):
    path, config = _job(tmp_path)
    outside = tmp_path / "other-run" / "predictor.pt"

    def fail_training(actual_config, *, progress, on_event):
        on_event({"kind": "checkpoint", "checkpoint_path": str(outside),
                  "best_epoch": 7, "training_complete": True, "evaluation_complete": False})
        running = read_json(path)
        assert "saved_checkpoint" not in running
        assert "checkpoint_path" not in running["progress"]
        raise ValueError("validation failed")

    monkeypatch.setattr(dashboard_worker, "train", fail_training)
    assert dashboard_worker.run_job(path) == 1
    result = read_json(path)
    assert result["error"] == "validation failed"
    assert str(outside) not in "\n".join(row["message"] for row in result["logs"])


def test_symlink_checkpoint_is_not_offered_as_recovery(tmp_path, monkeypatch):
    path, config = _job(tmp_path)
    checkpoint = Path(config.run_dir) / "predictor.pt"
    actual_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda value: value == checkpoint or actual_is_symlink(value))

    def fail_training(actual_config, **callbacks):
        _checkpoint(actual_config)
        raise ValueError("validation failed")

    monkeypatch.setattr(dashboard_worker, "train", fail_training)
    assert dashboard_worker.run_job(path) == 1
    result = read_json(path)
    assert "saved_checkpoint" not in result
    assert result["error"] == "validation failed"
