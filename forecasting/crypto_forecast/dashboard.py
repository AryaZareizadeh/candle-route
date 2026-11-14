"""Loopback browser dashboard with isolated, persistent training jobs."""

from __future__ import annotations

import argparse
import atexit
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
import os
from pathlib import Path
import pickle
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid

from .pipeline import TrainingConfig
from .data import _coins
from .model import MODEL_SPECS
from .dashboard_web import body_limit, static_response

ACTIVE = {"queued", "running", "awaiting_decision"}
TERMINAL = {"completed", "failed", "stopped", "interrupted"}
COIN_LIMIT = 100
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}\Z")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _open_snapshot(path, flags):
    """Open read-only snapshots without blocking atomic replacement on Windows."""
    if os.name != "nt":
        return os.open(path, flags)
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    # GENERIC_READ, FILE_SHARE_READ | WRITE | DELETE, OPEN_EXISTING,
    # FILE_ATTRIBUTE_NORMAL. Delete sharing permits rename/replacement while
    # the reader finishes reading the previous complete snapshot.
    handle = create_file(os.fsdecode(path), 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        error.filename = os.fsdecode(path)
        raise error
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
        raise


def read_json(path):
    for attempt in range(8):
        try:
            with io.open(path, "r", encoding="utf-8", opener=_open_snapshot) as snapshot:
                return json.loads(snapshot.read())
        except (PermissionError, FileNotFoundError, json.JSONDecodeError) as error:
            transient = (isinstance(error, (FileNotFoundError, json.JSONDecodeError)) or
                         getattr(error, "winerror", None) in {32, 33})
            if not transient or attempt == 7:
                raise
            time.sleep(min(0.01 * 2**attempt, 0.1))


def _replace_snapshot(source, target):
    if os.name != "nt":
        return os.replace(source, target)
    import ctypes
    from ctypes import wintypes

    replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
    replace_file.restype = wintypes.BOOL
    # Unlike MoveFileEx (os.replace), ReplaceFile supports replacing an
    # existing snapshot while readers hold handles with delete sharing.
    if replace_file(str(target), str(source), None, 0, None, None):
        return
    error = ctypes.get_last_error()
    if error == 2:  # ERROR_FILE_NOT_FOUND: first publication has no target yet.
        return os.replace(source, target)
    raise ctypes.WinError(error)


def write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        for attempt in range(8):
            try:
                _replace_snapshot(temporary, path)
                break
            except PermissionError:
                # Windows readers briefly hold files without delete sharing.
                # Keep the old complete snapshot until the replacement succeeds.
                if attempt == 7:
                    raise
                time.sleep(min(0.01 * 2**attempt, 0.1))
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_bytes(path, json.dumps(value, allow_nan=False).encode("utf-8"))


class DashboardError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def publish_download(stage, destination, coins):
    """Validate and replace one coin's four timeframes, rolling back write failures."""
    stage, destination = Path(stage), Path(destination)
    files = []
    for interval in ("15m", "1h", "4h", "1d"):
        for coin in coins:
            csv = stage / interval / f"{coin}.csv"
            metadata = csv.with_suffix(".metadata.json")
            values = read_json(metadata)
            if hashlib.sha256(csv.read_bytes()).hexdigest() != values["data_sha256"]:
                raise ValueError(f"{coin} {interval}: downloaded data does not match its metadata.")
            files.extend((csv.relative_to(stage), metadata.relative_to(stage)))
    staged_report = read_json(stage / "download_report.json")
    target_report = destination / "download_report.json"
    report = read_json(target_report) if target_report.exists() else {}
    report.update({coin: staged_report[coin] for coin in coins})
    report_payload = json.dumps(report).encode("utf-8")
    files.append(Path("download_report.json"))
    backup = stage / ".previous"
    originals = {}
    # Validate and back up everything before changing shared data.
    for relative in files:
        if not (stage / relative).is_file():
            raise ValueError(f"Missing downloaded file: {relative}")
        target = destination / relative
        originals[relative] = target.exists()
        if target.exists():
            previous = backup / relative
            previous.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, previous)
    applied = []
    try:
        for relative in files:
            applied.append(relative)
            payload = report_payload if relative == Path("download_report.json") else (stage / relative).read_bytes()
            write_bytes(destination / relative, payload)
    except (OSError, ValueError):
        for relative in reversed(applied):
            if originals[relative]:
                write_bytes(destination / relative, (backup / relative).read_bytes())
            else:
                (destination / relative).unlink(missing_ok=True)
        raise


class JobManager:
    def __init__(self, workspace, *, offline=False):
        self.workspace = Path(workspace).resolve()
        self.offline = offline
        self.jobs_dir = self.workspace / ".dashboard" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._ownership = (self.jobs_dir.parent / "server.lock").open("a+b")
        self._ownership.seek(0, os.SEEK_END)
        if self._ownership.tell() == 0:
            self._ownership.write(b"0")
            self._ownership.flush()
        self._ownership.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._ownership.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._ownership.close()
            raise ValueError("A dashboard already owns this project workspace. Use its existing browser address.") from None
        self.lock = threading.RLock()
        self.processes = {}
        self.monitors = {}
        self.stopping = set()
        self.sent_decisions = {}
        self.closed = False
        for path in self.jobs_dir.glob("*.json"):
            try:
                job = read_json(path)
                if job.get("status") in ACTIVE:
                    job.update(status="interrupted", stage="interrupted", pending_decision=None, finished_at=utc_now(),
                               updated_at=utc_now(), error="Dashboard stopped before this job finished. Start a new run.")
                    write_json(path, job)
            except (ValueError, OSError):
                continue

    def _path(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise DashboardError(404, "Job not found.")
        path = self.jobs_dir / f"{identifier}.json"
        if path.resolve() != path:
            raise DashboardError(404, "Job not found.")
        return path

    def get_job(self, identifier):
        try:
            job = read_json(self._path(identifier))
        except (FileNotFoundError, json.JSONDecodeError):
            raise DashboardError(404, "Job not found.") from None
        end = job.get("finished_at") or utc_now()
        job["elapsed_seconds"] = max(0, (datetime.fromisoformat(end) -
                                         datetime.fromisoformat(job["started_at"])).total_seconds())
        return job

    def _update_job(self, identifier, **fields):
        with self.lock:
            job = self.get_job(identifier)
            job.update(fields, updated_at=utc_now())
            write_json(self._path(identifier), job)
            return job

    def list_jobs(self):
        jobs = []
        for path in self.jobs_dir.glob("*.json"):
            try:
                jobs.append(self.get_job(path.stem))
            except (ValueError, OSError, KeyError):
                continue
        return sorted(jobs, key=lambda job: job["started_at"], reverse=True)[:50]

    def start_job(self, payload):
        if (not isinstance(payload, dict) or "operation" not in payload
                or set(payload) - {"operation", "name", "config"}):
            raise DashboardError(400, "Provide an operation, optional name, and training settings.")
        operation = payload.get("operation", "train")
        if not isinstance(operation, str) or operation not in {"train", "download"}:
            raise DashboardError(400, "Operation must be train or download.")
        if self.offline and operation == "download":
            raise DashboardError(503, "Candle downloads are disabled in offline mode.")
        name = payload.get("name", "")
        if not isinstance(name, str) or len(name) > 120:
            raise DashboardError(400, "Run name must be text of at most 120 characters.")
        settings = payload.get("config", {})
        allowed = {"coins", "start", "end", "lookback", "hidden_size", "epochs", "patience",
                   "batch_size", "learning_rate", "train_fraction", "validation_fraction", "seed", "device", "model_type",
                   "kronos_timeframe", "kronos_prediction_length", "kronos_sample_count", "kronos_eval_stride", "kronos_train_stride",
                   "kronos_finetune_tokenizer", "kronos_tokenizer_epochs", "kronos_tokenizer_patience",
                   "kronos_tokenizer_learning_rate", "kronos_training_stage", "kronos_tokenizer_run_id",
                   "kronos_tokenizer_batch_size"}
        allowed.add("kronos_preparation_workers")
        from .learning_rate import FIELDS
        allowed.update(FIELDS)
        allowed.update("kronos_tokenizer_" + field for field in FIELDS)
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise DashboardError(400, "Unknown training setting. Data and runs stay inside this project.")
        settings = dict(settings)
        if operation == "download":
            # Downloads use coin symbols and dates, never model or optimizer settings.
            settings = {key: value for key, value in settings.items() if key in {"coins", "start", "end"}}
        if "kronos_tokenizer_run_id" in settings:
            source = settings.pop("kronos_tokenizer_run_id")
            settings["kronos_tokenizer_run_dir"] = str(self._tokenizer_path(source))
        for key in ("learning_rate", "train_fraction", "validation_fraction", "kronos_tokenizer_learning_rate"):
            if key in settings and (isinstance(settings[key], bool) or not isinstance(settings[key], (int, float))):
                raise DashboardError(400, f"{key} must be a number.")
        identifier = f"{operation}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        try:
            config = TrainingConfig(**{"coins": ["BTC", "ETH", "SOL", "XRP", "ADA"], **settings},
                                    data_dir=str(self.workspace / "data"),
                                    run_dir=str(self.workspace / "runs" / identifier),
                                    input_mode="multiframe", origin_interval="4h")
            if config.model_type in {"kronos-small", "kronos-base"}:
                config.kronos_model_path, config.kronos_tokenizer_path = self._kronos_paths(config.model_type)
            config.validate()
            config.coins = _coins(config.coins)
            if not (0 < config.train_fraction < 1 and 0 < config.validation_fraction < 1
                    and config.train_fraction + config.validation_fraction < 1):
                raise ValueError("Training and validation fractions must be positive and leave a test split.")
            if len(config.coins) > COIN_LIMIT:
                raise ValueError(f"Selected {len(config.coins)} coins; the dashboard limit is {COIN_LIMIT} coins per job. "
                                 f"Select up to {COIN_LIMIT} coins for this job.")
            if operation == "train":
                for field, maximum in (("epochs", 5000), ("lookback", 2000), ("hidden_size", 2048),
                                       ("batch_size", 8192), ("kronos_tokenizer_epochs", 5000),
                                       ("kronos_tokenizer_batch_size", 8192)):
                    value = getattr(config, field)
                    if value is not None and value > maximum:
                        raise ValueError(f"{field} is {value}; the dashboard maximum is {maximum}.")
        except (ValueError, TypeError, OverflowError) as error:
            raise DashboardError(400, str(error)) from error
        with self.lock:
            if self.closed:
                raise DashboardError(409, "Dashboard is shutting down.")
            if any(job["status"] in ACTIVE for job in self.list_jobs()):
                raise DashboardError(409, "A job is already running. Wait for it to finish or stop it first.")
            if config.kronos_tokenizer_run_dir:
                # Deletion shares this lock. Recheck after validation so a
                # concurrent removal cannot leave a newly queued job orphaned.
                self._tokenizer_path(Path(config.kronos_tokenizer_run_dir).name)
            now = utc_now()
            job = {"id": identifier, "name": name.strip() or identifier, "operation": operation,
                   "status": "queued", "stage": "starting", "started_at": now, "updated_at": now,
                   "finished_at": None, "elapsed_seconds": 0, "config": asdict(config),
                   "history": [], "progress": {"epoch": 0, "max_epochs": config.kronos_tokenizer_epochs
                       if config.kronos_training_stage == "tokenizer" else config.epochs},
                   "logs": [], "error": None, "pending_decision": None}
            write_json(self._path(identifier), job)
            try:
                self._launch(job)
            except OSError as error:
                self._update_job(identifier, status="failed", stage="failed", error=str(error), finished_at=utc_now())
                raise DashboardError(500, "Could not start the training process: " + str(error)) from error
            return self.get_job(identifier)

    def _launch(self, job):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("TRADER_", "NOBITEX_", "DASHBOARD_", "SMTP_"))}
        package_root = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONPATH"] = package_root + os.pathsep + environment.get("PYTHONPATH", "")
        environment["PYTHONUNBUFFERED"] = "1"
        if self.offline:
            environment["HF_HUB_OFFLINE"] = "1"
            environment["TRANSFORMERS_OFFLINE"] = "1"
        log_path = self.jobs_dir / f"{job['id']}.log"
        with log_path.open("wb") as output:
            process = subprocess.Popen([sys.executable, "-u", "-m", "crypto_forecast.dashboard_worker",
                                        str(self._path(job["id"]))], cwd=self.workspace, env=environment,
                                       stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.processes[job["id"]] = process
        monitor = threading.Thread(target=self._monitor, args=(job["id"], process), daemon=True)
        self.monitors[job["id"]] = monitor
        monitor.start()

    def chart_preview(self, payload):
        from .chart_forecast import prepare_chart
        try:
            return prepare_chart(self.workspace, payload)[1]
        except (ValueError, TypeError, OSError) as error:
            raise DashboardError(400, str(error)) from error

    def chart_models(self):
        from .chart_forecast import ORIGINAL_MODELS, original_paths
        pretrained = []
        for identifier, variant in ORIGINAL_MODELS.items():
            try:
                original_paths(self.workspace, variant)
                ready = True
            except ValueError:
                ready = False
            pretrained.append({"id": identifier, "name": variant.capitalize(), "ready": ready})
        saved = []
        for path in (self.workspace / "runs").iterdir() if (self.workspace / "runs").is_dir() else []:
            try:
                summary = self._saved_model_summary(path.name)
                try:
                    name = self.get_job(path.name)["name"]
                except DashboardError:
                    name = path.name
                recovery = summary.get("checkpoint_file") == "predictor.pt"
                saved.append({"id": path.name, "name": name + (" · training weights · evaluation incomplete" if recovery else ""),
                              "created_at": summary.get("created_at"), "evaluation_complete": not recovery,
                              "coins": summary.get("coins", []), "model_type": summary["model_type"]})
            except (ValueError, OSError):
                continue
        return {"original_ready": pretrained[0]["ready"], "pretrained": pretrained,
                "runs": sorted(saved, key=lambda item: item["created_at"] or "", reverse=True)}

    def testing_chart(self, payload):
        from .chart_forecast import testing_chart
        try:
            return testing_chart(self.workspace, payload)
        except (ValueError, TypeError, OSError) as error:
            raise DashboardError(400, str(error)) from error

    def start_chart_forecast(self, payload, *, testing=False):
        from .chart_forecast import ORIGINAL_MODELS, original_paths, prepare_chart, prepare_testing
        try:
            value, chart = (prepare_testing if testing else prepare_chart)(self.workspace, payload)
            if value["model"] in ORIGINAL_MODELS:
                original_paths(self.workspace, ORIGINAL_MODELS[value["model"]])
            else:
                summary = self._saved_model_summary(value["model"])
                if summary["config"].get("kronos_timeframe") != value["timeframe"]:
                    raise ValueError("Choose a saved Kronos run trained on this candle interval.")
                value["checkpoint_file"] = summary["checkpoint_file"]
        except (ValueError, TypeError, OSError) as error:
            raise DashboardError(400, str(error)) from error
        with self.lock:
            if self.closed or any(job["status"] in ACTIVE for job in self.list_jobs()):
                raise DashboardError(409, "A job is already running or the dashboard is shutting down. Wait or stop it first.")
            if value["model"] not in ORIGINAL_MODELS:
                source = self._managed_run_path(value["model"])
                if not self.artifact(source, value["checkpoint_file"]).is_file():
                    raise DashboardError(404, "Saved predictor weights are unavailable.")
            identifier = f"forecast-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            now = utc_now()
            value.pop("csv", None)
            job = {"id": identifier, "name": f"{value['coin']} · {value['timeframe']} · {value['horizon']} candles",
                   "operation": "forecast", "config": value, "status": "queued", "stage": "starting",
                   "started_at": now, "updated_at": now, "finished_at": None, "elapsed_seconds": 0,
                   "history": [], "progress": {}, "logs": [], "error": None,
                   "pending_decision": None}
            if testing:
                job.update(mode="testing", anchor=chart["selected_candle"])
                job["name"] += " · " + chart["selected_candle"][:16].replace("T", " ") + " UTC"
            write_json(self.jobs_dir.parent / "charts" / identifier / "input.json", chart)
            write_json(self._path(identifier), job)
            try:
                self._launch(job)
            except OSError as error:
                self._update_job(identifier, status="failed", stage="failed", error=str(error), finished_at=utc_now())
                raise DashboardError(500, "Could not start the forecasting process: " + str(error)) from error
            return self.get_job(identifier)

    def chart_result(self, identifier):
        job = self.get_job(identifier)
        if job["operation"] != "forecast" or job["status"] != "completed":
            raise DashboardError(404, "Candle forecast is not available yet.")
        directory = self.jobs_dir.parent / "charts" / identifier
        path = self.artifact(directory, "result.json")
        if not path.resolve().is_relative_to(self.workspace):
            raise DashboardError(404, "Candle forecast is unavailable.")
        return json.loads(path.read_text(encoding="utf-8"))

    def _monitor(self, identifier, process):
        # A download worker pauses after preparing each coin. Publication is
        # owned by this parent under the same lock used by Stop.
        try:
            if self.get_job(identifier)["operation"] == "download" and hasattr(process, "poll"):
                while process.poll() is None:
                    self._publish_pending_coin(identifier, process)
                    time.sleep(0.1)
            code = process.wait()
            with self.lock:
                job = self.get_job(identifier)
                if identifier in self.stopping:
                    self._update_job(identifier, status="stopped", stage="stopped", pending_decision=None,
                                     finished_at=utc_now())
                elif code == 0 and job["operation"] == "download" and job["stage"] == "publishing":
                    try:
                        self._publish_download(job)
                        logs = job["logs"] + [{"timestamp": utc_now(), "message": "Candle history is ready. Start training with these settings."}]
                        self._update_job(identifier, status="completed", stage="completed", finished_at=utc_now(), logs=logs[-250:])
                    except (OSError, ValueError, KeyError) as error:
                        self._update_job(identifier, status="failed", stage="failed", finished_at=utc_now(),
                                         error=f"Could not publish downloaded candles: {error}")
                elif job["status"] not in TERMINAL:
                    log_path = self.jobs_dir / f"{identifier}.log"
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.exists() else ""
                    self._update_job(identifier, status="failed", stage="failed", pending_decision=None, finished_at=utc_now(),
                                     error=f"Worker exited ({code}) before finishing. {detail}")
        except (OSError, ValueError, KeyError) as error:
            # The polling loop is part of the monitor's lifetime too. Never
            # leave a child waiting for an acknowledgement from a dead monitor.
            with self.lock:
                if hasattr(process, "poll") and process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    except OSError:
                        pass
                message = f"Job monitor failed for {identifier}: {error.__context__ or error}"
                try:
                    stopped = identifier in self.stopping
                    state = "stopped" if stopped else "failed"
                    self._update_job(identifier, status=state, stage=state, pending_decision=None,
                                     finished_at=utc_now(), error=None if stopped else message)
                except (OSError, ValueError, KeyError):
                    # A genuinely removed/unreadable job cannot store an error.
                    print(message, file=sys.stderr, flush=True)
        finally:
            with self.lock:
                self.processes.pop(identifier, None)
                if getattr(process, "stdin", None) is not None:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass
                self.monitors.pop(identifier, None)

    def _publish_pending_coin(self, identifier, process):
        with self.lock:
            job = self.get_job(identifier)
            coin = job.get("publishing_coin")
            if (identifier in self.stopping or job["status"] != "running" or
                    job["stage"] != "publishing_coin" or coin not in job["config"]["coins"] or
                    job["progress"]["coin_progress"][coin]["status"] != "ready"):
                return
            response = {"coin": coin, "published": False}
            row = job["progress"]["coin_progress"][coin]
            try:
                self._publish_download(job, coins=[coin])
                row.update(status="saved", completed=4, total=4)
                response["published"] = True
            except (OSError, ValueError, KeyError) as error:
                row.update(status="failed", error=str(error))
                response["error"] = str(error)[:500]
            job["progress"]["completed_coins"] = sum(
                entry["status"] == "saved" for entry in job["progress"]["coin_progress"].values())
            self._update_job(identifier, progress=job["progress"])
            try:
                process.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
                process.stdin.flush()
            except OSError:
                # A stopped/crashed worker cannot undo a published coin.
                pass

    def _publish_download(self, job, coins=None):
        stage = self.jobs_dir / f"{job['id']}-data"
        destination = self.workspace / "data"
        publish_download(stage, destination, job["config"]["coins"] if coins is None else coins)

    def stop_job(self, identifier):
        with self.lock:
            job = self.get_job(identifier)
            if job["status"] not in ACTIVE:
                return job
            self.stopping.add(identifier)
            process = self.processes.get(identifier)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            result = self._update_job(identifier, status="stopped", stage="stopped", pending_decision=None, finished_at=utc_now(),
                                      error=None)
            return result

    def decide_training(self, identifier, payload):
        if (not isinstance(payload, dict) or set(payload) != {"decision_id", "action"}
                or isinstance(payload.get("decision_id"), bool)
                or not isinstance(payload.get("decision_id"), int) or payload["decision_id"] < 1
                or not isinstance(payload.get("action"), str)
                or payload["action"] not in {"continue", "finish"}):
            raise DashboardError(400, "Provide the current decision_id and action continue or finish.")
        with self.lock:
            job = self.get_job(identifier)
            pending = job.get("pending_decision")
            process = self.processes.get(identifier)
            decision_id = payload["decision_id"]
            if (self.closed or identifier in self.stopping or job.get("operation") != "train"
                    or job["status"] != "awaiting_decision" or not isinstance(pending, dict)
                    or pending.get("decision_id") != decision_id
                    or decision_id <= self.sent_decisions.get(identifier, 0)
                    or process is None or process.poll() is not None
                    or getattr(process, "stdin", None) is None):
                raise DashboardError(409, "This training decision is no longer available. Refresh the job progress.")
            # The child owns the job snapshot while it is alive. Send only a
            # bounded command; rewriting its JSON here could erase newer epochs.
            command = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(command) > 1024:
                raise DashboardError(400, "Training decision exceeds the command size limit.")
            try:
                process.stdin.write(command)
                process.stdin.flush()
            except (OSError, ValueError) as error:
                raise DashboardError(409, "The training worker is no longer accepting decisions.") from error
            self.sent_decisions[identifier] = decision_id
            return self.get_job(identifier)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            with self.lock:
                for job in self.list_jobs():
                    if job["status"] in ACTIVE:
                        self.stop_job(job["id"])
                # A worker can publish completed before it closes its files.
                # Keep the workspace lease until its process and monitor exit.
                monitors = tuple(self.monitors.values())
                for identifier in self.monitors:
                    process = self.processes[identifier]
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for monitor in monitors:
                monitor.join(timeout=5)
        finally:
            self._ownership.close()

    def _kronos_paths(self, variant="kronos-small"):
        from .kronos_setup import PRETRAINED_MODELS, TOKENIZER_ID
        root = self.workspace / "models" / variant
        model, tokenizer = root / "model", root / "tokenizer"
        if (model.is_dir() and tokenizer.is_dir()
                and model.resolve() == model and tokenizer.resolve() == tokenizer):
            return str(model), str(tokenizer)
        return PRETRAINED_MODELS[variant], TOKENIZER_ID

    def defaults(self):
        path = self.workspace / "config.example.json"
        defaults = asdict(TrainingConfig(coins=["BTC", "ETH", "SOL", "XRP", "ADA"]))
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    defaults.update({key: value for key, value in saved.items() if key in defaults})
            except (ValueError, OSError):
                pass
        defaults.update(input_mode="multiframe", origin_interval="4h", data_dir="data", run_dir="runs")
        defaults["kronos_model_path"], defaults["kronos_tokenizer_path"] = self._kronos_paths()
        return defaults

    def data_status(self):
        coins = []
        root = self.workspace / "data"
        names = {path.name for interval in ("15m", "1h", "4h", "1d")
                 for path in (root / interval).glob("*.csv")}
        for name in sorted(names):
            symbol = Path(name).stem
            if not IDENTIFIER.fullmatch(symbol):
                continue
            entry = {"coin": symbol, "timeframes": [], "rows": {}, "last_close": {}}
            for interval, seconds in (("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400)):
                csv_path = root / interval / name
                if not csv_path.exists():
                    continue
                entry["timeframes"].append(interval)
                metadata_path = csv_path.with_suffix(".metadata.json")
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    entry["rows"][interval] = metadata.get("rows")
                    last = metadata.get("last_date")
                    entry["last_close"][interval] = ((datetime.fromisoformat(last.replace("Z", "+00:00")) +
                                                      timedelta(seconds=seconds)).isoformat() if last else None)
                except (OSError, ValueError, TypeError):
                    entry["rows"][interval] = None
            coins.append(entry)
        return {"coins": coins}

    def _run_path(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise DashboardError(404, "Run not found.")
        path = self.workspace / "runs" / identifier
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != (self.workspace / "runs").resolve():
            raise DashboardError(404, "Run not found.")
        try:
            job = self.get_job(identifier)
        except DashboardError:
            job = None
        if job is not None and job["status"] != "completed":
            raise DashboardError(404, "This training run has not completed. Check its progress or start a new run.")
        return path

    @staticmethod
    def artifact(directory, filename):
        path = directory / filename
        if path.is_symlink() or path.resolve().parent != directory.resolve():
            raise DashboardError(404, "Run artifact is not available inside its run folder.")
        return path

    def _saved_model_summary(self, identifier):
        from .chart_forecast import ORIGINAL_MODELS
        path = self._managed_run_path(identifier)
        try:
            job = self.get_job(identifier)
        except DashboardError:
            job = None
        if job is not None and job["status"] in ACTIVE:
            raise DashboardError(404, "This training run is still active.")
        try:
            completed = job is None or job["status"] == "completed"
            if completed and self.artifact(path, "model.pt").is_file():
                summary = read_json(self.artifact(path, "summary.json"))
                filename = "model.pt"
            else:
                checkpoint = self.artifact(path, "predictor.pt")
                if not checkpoint.is_file():
                    raise DashboardError(404, "Saved predictor weights are unavailable.")
                # The checkpoint remains authoritative if its inventory view was
                # interrupted between atomic replacements.
                summary = None
                try:
                    view = self.artifact(path, "predictor_summary.json")
                    if view.stat().st_mtime_ns >= checkpoint.stat().st_mtime_ns:
                        summary = read_json(view)
                except (OSError, ValueError):
                    pass
                if (not isinstance(summary, dict) or summary.get("artifact_type") != "kronos-predictor"
                        or not isinstance(summary.get("config"), dict) or summary.get("model_type") not in ORIGINAL_MODELS.values()):
                    import torch
                    summary = torch.load(checkpoint, map_location="cpu", weights_only=True)["metadata"]
                if not isinstance(summary, dict) or summary.get("artifact_type") != "kronos-predictor":
                    raise ValueError("Unsupported predictor checkpoint.")
                filename = "predictor.pt"
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, EOFError, pickle.UnpicklingError):
            raise DashboardError(404, "Saved model settings are unavailable.") from None
        if (not isinstance(summary, dict) or summary.get("model_type") not in ORIGINAL_MODELS.values()
                or not isinstance(summary.get("config"), dict)):
            raise DashboardError(400, "Choose a saved Kronos run trained on this candle interval.")
        return {**summary, "checkpoint_file": filename}

    @staticmethod
    def _linked_path(path):
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                                 getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))

    def _managed_run_path(self, identifier):
        from .chart_forecast import ORIGINAL_MODELS
        if (not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier)
                or identifier in ORIGINAL_MODELS):
            raise DashboardError(404, "Saved run not found. Original models cannot be removed.")
        root = self.workspace / "runs"
        path = root / identifier
        try:
            if (self._linked_path(root) or root.resolve() != root or self._linked_path(path)
                    or not path.is_dir() or path.resolve().parent != root):
                raise DashboardError(400, "Saved run must be a local folder inside this project's runs folder.")
        except FileNotFoundError:
            raise DashboardError(404, "Saved run not found.") from None
        return path

    def list_saved_runs(self):
        entries = []
        root = self.workspace / "runs"
        if not root.is_dir() or self._linked_path(root) or root.resolve() != root:
            return entries
        for directory in root.iterdir():
            try:
                path = self._managed_run_path(directory.name)
                try:
                    job = self.get_job(path.name)
                except DashboardError:
                    job = None
                artifacts = []
                for name in ("model.pt", "tokenizer.pt", "predictor.pt", "summary.json",
                             "tokenizer_summary.json", "predictor_summary.json"):
                    try:
                        if (path / name).is_file():
                            artifacts.append(name)
                    except OSError:
                        # A known failed job can still be listed when its
                        # incomplete artifact is unreadable.
                        continue
                if not artifacts and (job is None or job.get("operation") != "train"):
                    continue
                entries.append({"id": path.name, "name": job.get("name", path.name) if job else path.name,
                                "status": job.get("status", "saved") if job else "saved",
                                "created_at": job.get("started_at") if job else None,
                                "artifacts": artifacts,
                                "removable": not job or job.get("status") not in ACTIVE})
            except (DashboardError, OSError, ValueError, KeyError):
                continue
        return sorted(entries, key=lambda item: item["created_at"] or item["id"], reverse=True)

    def remove_run(self, identifier):
        with self.lock:
            if self.closed:
                raise DashboardError(409, "The dashboard is shutting down.")
            path = self._managed_run_path(identifier)
            if identifier not in {entry["id"] for entry in self.list_saved_runs()}:
                raise DashboardError(404, "Managed saved run not found.")
            owner = self.processes.get(identifier)
            if identifier in self.stopping or (owner is not None and owner.poll() is None):
                raise DashboardError(409, "This saved run is in use. Stop its active job before removing it.")
            for snapshot in self.jobs_dir.glob("*.json"):
                try:
                    job = self.get_job(snapshot.stem)
                except (DashboardError, ValueError, OSError, KeyError):
                    continue
                config = job.get("config", {})
                process = self.processes.get(job["id"])
                running = job.get("status") in ACTIVE or (process is not None and process.poll() is None)
                if running and (job["id"] == identifier or config.get("model") == identifier
                                or any(config.get(field) and Path(config[field]).resolve() == path
                                       for field in ("run_dir", "kronos_tokenizer_run_dir"))):
                    raise DashboardError(409, "This saved run is in use. Stop its active job before removing it.")
            # Verify the complete tree before deleting anything. Reparse points
            # include Windows junctions, which is_symlink alone does not detect.
            try:
                pending = [path]
                while pending:
                    directory = pending.pop()
                    if self._linked_path(directory) or not directory.resolve().is_relative_to(path):
                        raise DashboardError(400, "Saved run contains a linked path. Remove the link before deleting this run.")
                    for child in directory.iterdir():
                        if self._linked_path(child) or not child.resolve().is_relative_to(path):
                            raise DashboardError(400, "Saved run contains a linked path. Remove the link before deleting this run.")
                        if child.is_dir():
                            pending.append(child)
                # rmtree also refuses directory symlinks and does not traverse
                # Windows junction targets if a link appears after validation.
                shutil.rmtree(path)
            except OSError as error:
                raise DashboardError(409, f"Could not remove the saved run: {error}") from error
            try:
                self._update_job(identifier, artifacts_removed_at=utc_now())
            except DashboardError:
                pass
            return {"id": identifier, "removed": True}

    def _tokenizer_path(self, identifier):
        """Select an independent artifact, including one from a stopped job."""
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise DashboardError(400, "Select an available tokenizer run from this project.")
        root = self.workspace / "runs"
        directory = root / identifier
        if (root.resolve() != root or directory.is_symlink() or not directory.is_dir()
                or directory.resolve().parent != root):
            raise DashboardError(400, "Selected tokenizer run is not available inside this project.")
        try:
            checkpoint = self.artifact(directory, "tokenizer.pt")
        except DashboardError:
            raise DashboardError(400, "Selected tokenizer artifact is not available inside its run folder.") from None
        if not checkpoint.is_file():
            raise DashboardError(400, "Selected run has no saved tokenizer checkpoint.")
        return directory.resolve()

    def list_tokenizers(self):
        tokenizers = []
        for path in (self.workspace / "runs").glob("*/tokenizer.pt"):
            try:
                directory = self._tokenizer_path(path.parent.name)
                summary = None
                try:
                    view = self.artifact(directory, "tokenizer_summary.json")
                    # Publishing the checkpoint precedes its lightweight view.
                    # A cancellation between replacements leaves the checkpoint authoritative.
                    if view.stat().st_mtime_ns >= path.stat().st_mtime_ns:
                        candidate = read_json(view)
                        if (isinstance(candidate, dict) and candidate.get("artifact_type") == "kronos-tokenizer"
                                and isinstance(candidate.get("config"), dict)):
                            summary = candidate
                except (OSError, ValueError):
                    pass
                complete = summary.get("training_complete") if summary is not None else None
                if summary is None or complete is None:
                    import torch
                    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
                    metadata = checkpoint.get("metadata", checkpoint)
                    if summary is None:
                        summary = metadata
                    if complete is None:
                        complete = metadata.get("training_complete", False)
                if (summary.get("artifact_type") != "kronos-tokenizer"
                        or not isinstance(summary.get("config"), dict)):
                    continue
                training = summary.get("tokenizer_training", {})
                try:
                    name = self.get_job(directory.name)["name"]
                except DashboardError:
                    name = directory.name
                tokenizers.append({"id": directory.name, "name": name,
                    "created_at": summary.get("created_at"), "best_epoch": summary.get("best_epoch", training.get("best_epoch")),
                    "best_validation_loss": training.get("best_validation_loss"),
                    "config": summary.get("config", {}), "complete": bool(complete)})
            except (ValueError, OSError, KeyError, TypeError, RuntimeError, AttributeError,
                    IndexError, EOFError, pickle.UnpicklingError):
                continue
        return sorted(tokenizers, key=lambda item: item["created_at"] or "", reverse=True)


def get_response(manager, path, csrf_token):
    """Read a dashboard resource after the transport has authenticated it."""
    try:
        if path == "/api/bootstrap":
            import torch
            return 200, {
                "csrf_token": csrf_token, "defaults": manager.defaults(), "data": manager.data_status(),
                "hardware": {"cuda_available": torch.cuda.is_available()}, "jobs": manager.list_jobs(),
                "saved_runs": manager.list_saved_runs(), "tokenizers": manager.list_tokenizers(),
                "chart_models": manager.chart_models(), "models": list(MODEL_SPECS.values()),
            }
        if path == "/api/jobs":
            jobs = manager.list_jobs()
            return 200, {
                "jobs": jobs, "active_job": next((job for job in jobs if job["status"] in ACTIVE), None),
            }
        if re.fullmatch(r"/api/chart/forecasts/[^/]+", path):
            return 200, manager.chart_result(path.split("/")[-1])
        if re.fullmatch(r"/api/jobs/[^/]+", path):
            return 200, manager.get_job(path.split("/")[-1])
        if path == "/api/tokenizers":
            return 200, {"tokenizers": manager.list_tokenizers()}
        if path == "/api/saved-runs":
            return 200, {"saved_runs": manager.list_saved_runs()}
        raise DashboardError(404, "Page not found.")
    except DashboardError as error:
        return error.status, {"error": str(error)}
    except (ValueError, OSError, KeyError):
        return 404, {"error": "Requested file or result is not available."}


def post_response(manager, path, content_type, body):
    """Process a bounded request body after the transport has checked CSRF."""
    try:
        if re.fullmatch(r"/api/jobs/[^/]+/stop", path):
            return 200, manager.stop_job(path.split("/")[3])
        actions = {
            "/api/jobs": (201, manager.start_job),
            "/api/chart/preview": (200, manager.chart_preview),
            "/api/chart/forecast": (201, manager.start_chart_forecast),
            "/api/testing/chart": (200, manager.testing_chart),
            "/api/testing/forecast": (201, lambda value: manager.start_chart_forecast(value, testing=True)),
        }
        delete = re.fullmatch(r"/api/runs/([^/]+)/delete", path)
        decision = re.fullmatch(r"/api/jobs/([^/]+)/decision", path)
        if path not in actions and not delete and not decision:
            raise DashboardError(404, "Action not found.")
        if content_type.split(";")[0] != "application/json":
            raise DashboardError(400, "Send settings as JSON.")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise DashboardError(400, "Invalid JSON settings.") from None
        if delete:
            if payload != {}:
                raise DashboardError(400, "Send an empty JSON object to remove a saved run.")
            return 200, manager.remove_run(delete[1])
        if decision:
            return 200, manager.decide_training(decision[1], payload)
        status, action = actions[path]
        return status, action(payload)
    except DashboardError as error:
        return error.status, {"error": str(error)}
    except (ValueError, TypeError) as error:
        return 400, {"error": str(error)}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CryptoForecastDashboard"

    def log_message(self, *_):
        pass

    def _trusted(self, mutation=False):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise DashboardError(403, "Use the local dashboard address.")
        if mutation:
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + host for host in hosts}:
                raise DashboardError(403, "Requests must come from this dashboard.")
            token = self.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token, self.server.csrf_token):
                raise DashboardError(403, "Reload the dashboard before starting a job.")

    def _send(self, status, content, content_type="application/json; charset=utf-8"):
        payload = json.dumps(content, allow_nan=False).encode() if isinstance(content, (dict, list)) else content
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            self._trusted()
            path = unquote(urlsplit(self.path).path)
            asset = static_response(path)
            if asset is not None:
                self._send(200, *asset)
            else:
                self._send(*get_response(self.server.manager, path, self.server.csrf_token))
        except DashboardError as error:
            self._send(error.status, {"error": str(error)})
        except OSError:
            self._send(404, {"error": "Requested file or result is not available."})

    def do_POST(self):
        try:
            path = unquote(urlsplit(self.path).path)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= body_limit(path):
                    raise ValueError
            except ValueError:
                raise DashboardError(400, "Invalid request size.") from None
            self.connection.settimeout(10)
            body = self.rfile.read(length)
            # Consume the bounded request body before rejecting its token/origin.
            # Closing with unread TCP bytes can reset a Windows client's socket.
            self._trusted(mutation=True)
            self._send(*post_response(self.server.manager, path, self.headers.get("Content-Type", ""), body))
        except DashboardError as error:
            self._send(error.status, {"error": str(error)})


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        if hasattr(self, "manager"):
            self.manager.close()
        super().server_close()


def create_server(workspace, host="127.0.0.1", port=8765):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The dashboard must bind to localhost.")
    manager = JobManager(workspace)
    try:
        server = DashboardServer((host, port), DashboardHandler)
    except Exception:
        manager.close()
        raise
    server.manager = manager
    server.csrf_token = secrets.token_urlsafe(32)
    return server


def serve_dashboard(workspace=".", port=8765):
    server = create_server(workspace, port=port)
    atexit.register(server.manager.close)
    print(f"Training dashboard: http://127.0.0.1:{server.server_address[1]}", flush=True)
    print("Leave this process running while training. Press Ctrl+C to close it.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        atexit.unregister(server.manager.close)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local cryptocurrency training dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", default=".")
    options = parser.parse_args()
    serve_dashboard(options.workspace, options.port)
