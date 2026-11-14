"""Trained predictor weights survive failures outside the optimizer loop."""

from dataclasses import replace
import hashlib
import json

import pytest
import torch

from crypto_forecast import kronos_backend as backend
from crypto_forecast import kronos_artifacts as artifacts
from crypto_forecast.pipeline import train
from test_kronos import config_for, tiny_modules, write_history


def setup_run(tmp_path, monkeypatch, **changes):
    write_history(tmp_path / "data")
    config = replace(config_for(tmp_path), kronos_finetune_tokenizer=False, **changes)
    model, tokenizer = tiny_modules()
    monkeypatch.setattr(backend, "load_pretrained", lambda *args: (model, tokenizer))
    return config, model, tokenizer


def test_best_predictor_survives_interrupted_epoch_callback(tmp_path, monkeypatch):
    config, model, tokenizer = setup_run(tmp_path, monkeypatch, epochs=3, patience=3)
    states = []

    def validation(*args):
        states.append({key: value.detach().clone() for key, value in model.state_dict().items()})
        return [0.2, 0.5][len(states) - 1]

    def interrupt(event):
        if event["kind"] == "epoch" and event["epoch"] == 2:
            raise RuntimeError("worker interrupted")

    monkeypatch.setattr(backend, "_validation_loss", validation)
    with pytest.raises(RuntimeError, match="worker interrupted"):
        train(config, progress=lambda _: None, on_event=interrupt)

    saved = torch.load(tmp_path / "run" / "predictor.pt", weights_only=True)
    assert saved["metadata"]["best_epoch"] == 1
    assert saved["metadata"]["training_complete"] is False
    assert saved["metadata"]["evaluation_complete"] is False
    assert any(not torch.equal(states[0][key], value) for key, value in states[1].items())
    assert all(torch.equal(states[0][key], value) for key, value in saved["state_dict"].items())
    assert all(torch.equal(tokenizer.state_dict()[key], value)
               for key, value in saved["tokenizer_state_dict"].items())
    restored, restored_tokenizer = tiny_modules()
    restored.load_state_dict(saved["state_dict"], strict=True)
    restored_tokenizer.load_state_dict(saved["tokenizer_state_dict"], strict=True)
    assert not (tmp_path / "run" / "model.pt").exists()


@pytest.mark.parametrize("failure", ["validation", "test", "latest", "report"])
def test_evaluation_and_report_failure_keep_trained_predictor(tmp_path, monkeypatch, failure):
    config, model, tokenizer = setup_run(tmp_path, monkeypatch)
    original_forecast = backend.forecast_samples
    calls = []

    def forecast(*args, **kwargs):
        calls.append(len(calls) + 1)
        if {"validation": 1, "test": 2, "latest": 3}.get(failure) == len(calls):
            raise ValueError("Kronos produced non-positive or non-finite predicted closes for FIL.")
        return original_forecast(*args, **kwargs)

    def broken_report(*args):
        raise OSError("report disk failure")

    monkeypatch.setattr(backend, "forecast_samples", forecast)
    if failure == "report":
        monkeypatch.setattr("crypto_forecast.report.write_report", broken_report)
    with pytest.raises((ValueError, OSError), match="FIL|report disk failure"):
        train(config, progress=lambda _: None)

    saved = torch.load(tmp_path / "run" / "predictor.pt", weights_only=True)
    summary = json.loads((tmp_path / "run" / "predictor_summary.json").read_text())
    assert saved["metadata"]["artifact_type"] == summary["artifact_type"] == "kronos-predictor"
    assert saved["metadata"]["training_complete"] is summary["training_complete"] is True
    assert saved["metadata"]["evaluation_complete"] is summary["evaluation_complete"] is False
    assert saved["metadata"]["best_epoch"] == summary["best_epoch"] == 1
    assert saved["metadata"]["config"] == summary["config"]
    assert all(torch.equal(model.state_dict()[key], value) for key, value in saved["state_dict"].items())
    assert all(torch.equal(tokenizer.state_dict()[key], value)
               for key, value in saved["tokenizer_state_dict"].items())
    assert not (tmp_path / "run" / "model.pt").exists()
    assert not (tmp_path / "run" / "summary.json").exists()
    assert not (tmp_path / "run" / "report.html").exists()


@pytest.mark.parametrize("old_completed_model", [False, True])
def test_recovered_predictor_supports_read_only_chart_inference(tmp_path, monkeypatch, old_completed_model):
    from crypto_forecast.chart_forecast import forecast_chart, prepare_chart
    from test_chart_forecast import payload

    config, _, _ = setup_run(tmp_path, monkeypatch)

    def broken_forecast(*args, **kwargs):
        raise ValueError("invalid evaluation candle")

    monkeypatch.setattr(backend, "forecast_samples", broken_forecast)
    with pytest.raises(ValueError, match="invalid evaluation candle"):
        train(config, progress=lambda _: None)
    run_dir = tmp_path / "runs" / "recovered"
    run_dir.parent.mkdir()
    (tmp_path / "run").rename(run_dir)
    checkpoint = run_dir / "predictor.pt"
    original = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    value, chart = prepare_chart(tmp_path, payload(model="recovered", device="cpu"))
    if old_completed_model:
        # The dashboard has selected recovery despite leftover final artifacts.
        (run_dir / "model.pt").write_bytes(b"stale checkpoint")
        value["checkpoint_file"] = "predictor.pt"
    result = forecast_chart(tmp_path, value, chart)
    assert len(result["forecast"]) == 3
    assert result["evaluation_complete"] is False
    assert "evaluation incomplete" in result["model_label"]
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == original
    if old_completed_model:
        assert (run_dir / "model.pt").read_bytes() == b"stale checkpoint"
    else:
        assert not (run_dir / "model.pt").exists()


@pytest.mark.parametrize("failure", ["write", "replace", "summary"])
def test_failed_checkpoint_publication_preserves_loadable_weights(tmp_path, monkeypatch, failure):
    directory = tmp_path / "run"
    summary = {"artifact_type": "kronos-predictor", "best_epoch": 1}
    artifacts.save_predictor(directory, {"weight": torch.tensor([1.0])}, {}, summary, summary)
    original = (directory / "predictor.pt").read_bytes()
    real_replace = artifacts.os.replace

    def broken_replace(source, destination):
        if str(destination).endswith("predictor.pt" if failure == "replace" else "predictor_summary.json"):
            raise OSError("interrupted publication")
        return real_replace(source, destination)

    if failure == "write":
        def broken_save(*args):
            raise OSError("disk full")
        monkeypatch.setattr(artifacts.torch, "save", broken_save)
    else:
        monkeypatch.setattr(artifacts.os, "replace", broken_replace)
    latest = {**summary, "best_epoch": 2}
    with pytest.raises(OSError, match="disk full|interrupted publication"):
        artifacts.save_predictor(directory, {"weight": torch.tensor([2.0])}, {}, latest, latest)
    saved = torch.load(directory / "predictor.pt", weights_only=True)
    if failure == "summary":
        assert saved["metadata"]["best_epoch"] == 2
        assert saved["state_dict"]["weight"].item() == 2
    else:
        assert (directory / "predictor.pt").read_bytes() == original
        assert saved["state_dict"]["weight"].item() == 1
