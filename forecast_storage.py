"""Locate and import durable forecasting data in the CandleRoute runtime."""

from contextlib import closing, ExitStack
from pathlib import Path
import os
import re
import shutil
import sqlite3
import tempfile

from runtime_lease import FileLease
from postgres import get_database


def forecast_workspace(account_dir):
    return Path(account_dir) / "forecasting"


def account_dir(root, username):
    root = Path(root).resolve()
    if username is None:
        return root
    database = get_database()
    if database is not None:
        with database.connection() as connection:
            row = connection.execute(
                'SELECT id FROM users WHERE lower(username COLLATE "C")=lower(%s COLLATE "C") AND verified=1',
                (username,)).fetchone()
    else:
        path = root / "accounts.sqlite3"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT id FROM users WHERE username=? AND verified=1", (username,)
            ).fetchone()
    if not row or not re.fullmatch("[0-9a-f]{32}", row[0]):
        raise ValueError("Choose a verified CandleRoute account.")
    return root / "users" / row[0]


def is_forecast_artifact(parts):
    """Select working data, models, reports, and job history without worker caches."""
    if len(parts) < 2 or any(part.startswith(".") for part in parts[1:]):
        return False
    if parts[0] in {"data", "models", "runs"}:
        return parts[1] != "_archives" and not parts[-1].endswith((".tmp", ".lock"))
    if parts[0] == ".dashboard" and parts[1] == "jobs":
        return len(parts) == 3 and parts[-1].endswith((".json", ".log"))
    return parts[:2] == (".dashboard", "charts") and parts[-1].endswith(".json")


def forecast_artifacts(source):
    source = Path(source).resolve()
    for directory in ("data", "models", "runs", ".dashboard"):
        for folder, subdirs, files in os.walk(source / directory):
            folder = Path(folder)
            if folder.resolve() != folder:
                raise ValueError("Forecasting workspace contains a linked directory.")
            subdirs[:] = [name for name in subdirs if not name.startswith(".") and name != "_archives"]
            if folder.relative_to(source).parts == (".dashboard", "jobs"):
                subdirs.clear()
            for name in files:
                path = folder / name
                if is_forecast_artifact(path.relative_to(source).parts):
                    if path.is_symlink() or path.resolve() != path:
                        raise ValueError("Forecasting workspace contains a linked artifact.")
                    yield path


def import_forecasting(source, root, username=None, *, exclude_runs=()):
    source, root = Path(source).resolve(), Path(root).resolve()
    if not source.is_dir():
        raise ValueError("Forecasting source directory does not exist.")
    with ExitStack() as stack:
        database = get_database()
        if database is not None:
            stack.enter_context(database.runtime_lease())
        stack.enter_context(FileLease(root / ".dashboard.lock"))
        target = forecast_workspace(account_dir(root, username))
        if target.resolve() != target:
            raise ValueError("Forecasting destination contains a linked directory.")
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError("Forecasting source and destination must be distinct workspaces.")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError("Forecasting destination already contains data.")
        source_lock = source / ".dashboard" / "server.lock"
        if source_lock.is_file():
            stack.enter_context(FileLease(source_lock))
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".forecast-import-", dir=target.parent) as temporary:
            staging = Path(temporary) / "workspace"
            staging.mkdir()
            count = 0
            for artifact in forecast_artifacts(source):
                relative = artifact.relative_to(source)
                if relative.parts[0] == "runs" and relative.parts[1] in exclude_runs:
                    continue
                output = staging / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact, output)
                count += 1
            if not count:
                raise ValueError("No forecasting data, models, or history found.")
            if target.is_dir():
                target.rmdir()
            staging.rename(target)
    return target, count
