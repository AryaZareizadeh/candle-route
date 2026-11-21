"""Local dashboard launcher and operator password setup."""

import argparse
from contextlib import nullcontext
import getpass
import os
from pathlib import Path


def main(argv=None):
    from postgres import get_database
    from settings import load_environment, update_environment

    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=(
        "serve", "setup-password", "backup", "backup-bundle", "verify-bundle",
        "restore-bundle", "migrate-postgres", "import-legacy", "import-forecasting",
        "setup-kronos"), default="serve")
    parser.add_argument("--output", type=Path, help="New database or bundle backup file")
    parser.add_argument("--username", help="Verified destination account for a data import or model setup")
    parser.add_argument("--source", type=Path, help="Source bundle, SQLite database, or SQLite runtime directory")
    parser.add_argument("--destination", type=Path, help="Artifact directory for restore-bundle or migrate-postgres")
    parser.add_argument("--dry-run", action="store_true", help="Validate migrate-postgres inputs without writing")
    parser.add_argument("--model", choices=("kronos-small", "kronos-base"), help="Original weights for setup-kronos")
    parser.add_argument("--exclude-run", action="append", default=[], help="Saved run directory to omit from import-forecasting")
    parser.add_argument("--host", default=os.getenv("TRADER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("TRADER_PORT", "8011")))
    args = parser.parse_args(argv)
    if args.dry_run and args.command != "migrate-postgres":
        parser.error("--dry-run is only valid with migrate-postgres")
    if args.model is not None and args.command != "setup-kronos":
        parser.error("--model is only valid with setup-kronos")
    if args.exclude_run and args.command != "import-forecasting":
        parser.error("--exclude-run is only valid with import-forecasting")
    if args.command == "migrate-postgres":
        from postgres_transfer import migrate_runtime
        from settings import data_dir

        if args.source is None or args.username is not None or args.output is not None:
            parser.error("migrate-postgres requires --source SQLITE_DIRECTORY_OR_BUNDLE")
        try:
            database = get_database()
            if database is None:
                parser.error("Set TRADER_DATABASE_URL to an unused PostgreSQL database")
            result = migrate_runtime(database, args.source, args.destination or data_dir(),
                                     dry_run=args.dry_run)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        summary = ", ".join(f"{table}={count}" for table, count in result["rows"].items())
        action = "Validated" if result["dry_run"] else "Migrated and verified"
        print(f"{action} {sum(result['rows'].values())} rows ({summary}) "
              f"and {result['artifacts']} artifact files.")
        return
    if args.command in ("import-forecasting", "setup-kronos"):
        from forecast_storage import account_dir, forecast_workspace, import_forecasting
        from runtime_lease import FileLease
        from settings import data_dir

        if args.output is not None or args.destination is not None:
            parser.error("Forecasting setup uses the configured CandleRoute data directory")
        root = data_dir()
        database = get_database()
        multiuser = os.getenv("TRADER_MULTIUSER", "false").lower() == "true"
        if (multiuser or (database is None and (root / "accounts.sqlite3").exists())) and not args.username:
            parser.error("--username is required for a multi-user runtime")
        try:
            if args.command == "import-forecasting":
                if args.source is None:
                    parser.error("import-forecasting requires --source PATH")
                target, count = import_forecasting(args.source, root, args.username, exclude_runs=args.exclude_run)
                print(f"Imported {count} forecasting files to {target}.")
            else:
                if args.source is not None:
                    parser.error("--source is only valid with import-forecasting")
                from forecasting.crypto_forecast.kronos_setup import download_pretrained

                model = args.model or "kronos-small"
                with (database.runtime_lease() if database is not None else nullcontext()), FileLease(root / ".dashboard.lock"):
                    workspace = forecast_workspace(account_dir(root, args.username))
                    download_pretrained(workspace / "models" / model, model=model)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        return
    if args.command in ("backup-bundle", "verify-bundle", "restore-bundle"):
        from backup import backup_bundle, restore_bundle, verify_bundle
        from settings import data_dir
        if args.username:
            parser.error("--username is only valid with import-legacy")
        try:
            if args.command == "backup-bundle":
                if args.output is None or args.source is not None or args.destination is not None:
                    parser.error("backup-bundle requires --output PATH and uses TRADER_DATA_DIR")
                manifest = backup_bundle(data_dir(), args.output, database=get_database())
                print(f"Verified bundle saved to {args.output.resolve()} ({len(manifest['files'])} files)")
            elif args.command == "verify-bundle":
                if args.source is None or args.output is not None or args.destination is not None:
                    parser.error("verify-bundle requires --source PATH")
                manifest = verify_bundle(args.source)
                print(f"Bundle integrity verified ({len(manifest['files'])} files)")
            else:
                if args.source is None or args.destination is None or args.output is not None:
                    parser.error("restore-bundle requires --source PATH --destination EMPTY_DIRECTORY")
                restore_bundle(args.source, args.destination, database=get_database())
                print(f"Bundle restored to {args.destination.resolve()}; bots load stopped. Supply the original master key separately.")
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        return
    if args.destination is not None:
        parser.error("--destination is only valid with restore-bundle")
    if args.command == "import-legacy":
        from accounts import Accounts
        from settings import data_dir
        import sqlite3

        if not args.username:
            parser.error("import-legacy requires --username")
        if args.output is not None:
            parser.error("--output is only valid with backup")
        master_key = os.getenv("TRADER_MASTER_KEY", "")
        if not master_key:
            parser.error("TRADER_MASTER_KEY must match the multi-user deployment")
        root = data_dir()
        source = args.source.resolve() if args.source is not None else root / "trader.sqlite3"
        database = get_database()
        if database is not None:
            from postgres_transfer import import_legacy_account

            credentials = {
                "token": os.getenv("NOBITEX_TOKEN", ""),
                "public_key": os.getenv("NOBITEX_API_PUBLIC_KEY", ""),
                "private_key": os.getenv("NOBITEX_API_PRIVATE_KEY", ""),
                "testnet": os.getenv("NOBITEX_TESTNET", "false").lower() == "true",
                "account_id": os.getenv("TRADER_ACCOUNT_ID", "default"),
            }
            try:
                user = import_legacy_account(database, source, root, username=args.username,
                                             master_key=master_key, credentials=credentials)
            except (OSError, ValueError, RuntimeError) as exc:
                parser.error(str(exc))
            print(f"Legacy data copied to account {user['username']}; original database retained.")
            return
        accounts = Accounts(root / "accounts.sqlite3", master_key)
        user = accounts.by_username(args.username)
        if not user or not user["verified"]:
            parser.error("Destination account must exist and have a confirmed email")
        destination = root / "users" / user["id"] / "trader.sqlite3"
        if not source.is_file():
            parser.error("No legacy database exists in TRADER_DATA_DIR")
        if destination.exists():
            parser.error("Destination already has trading data; import refused")
        legacy = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            has_records = legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone()
            if not has_records or legacy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                parser.error("Legacy database is missing its records or failed integrity check")
            import json
            saved_accounts = {state.get("_account") for (raw,) in legacy.execute(
                "SELECT value FROM records WHERE namespace='runtime'")
                if (state := json.loads(raw)).get("_account")}
            if len(saved_accounts) > 1:
                parser.error("Legacy bots contain multiple exchange identities; reconcile them before import")
            saved_account = next(iter(saved_accounts), None)
            account_id = saved_account.split(":", 1)[1] if saved_account else os.getenv("TRADER_ACCOUNT_ID", "default")
            testnet = saved_account.startswith("testnet:") if saved_account else (
                os.getenv("NOBITEX_TESTNET", "false").lower() == "true")
            destination.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(destination)
            try:
                legacy.backup(target)
            finally:
                target.close()
            token = os.getenv("NOBITEX_TOKEN", "")
            public_key = os.getenv("NOBITEX_API_PUBLIC_KEY", "")
            private_key = os.getenv("NOBITEX_API_PRIVATE_KEY", "")
            accounts.set_credentials(user["id"], {
                "token": token, "public_key": public_key, "private_key": private_key,
                "testnet": testnet, "account_id": account_id})
        finally:
            legacy.close()
        print(f"Legacy data copied to account {user['username']}; original database retained.")
        return
    if args.username is not None:
        parser.error("--username is only valid with import-legacy")
    if args.source is not None:
        parser.error("--source is only valid with import-legacy")
    if args.command == "backup":
        from settings import data_dir
        from storage import Store

        source = data_dir() / "trader.sqlite3"
        if args.output is None:
            parser.error("backup requires --output PATH")
        database = get_database()
        if database is not None:
            from backup import postgres_backup

            try:
                postgres_backup(database, args.output)
            except (OSError, ValueError, RuntimeError) as exc:
                parser.error(str(exc))
            print(f"PostgreSQL backup saved to {args.output.resolve()}")
            return
        if not source.is_file():
            parser.error("No database exists in the configured TRADER_DATA_DIR")
        if args.output.exists():
            parser.error("Backup destination already exists; choose a new file")
        Store(source).backup(args.output)
        print(f"Database backup saved to {args.output.resolve()}")
        return
    if args.output is not None:
        parser.error("--output is only valid with backup")
    if args.command == "setup-password":
        from auth import hash_password

        password = getpass.getpass("New dashboard password (at least 12 characters): ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match; nothing was saved.")
        try:
            encoded = hash_password(password)
        except ValueError as exc:
            parser.error(str(exc))
        update_environment({"DASHBOARD_PASSWORD_HASH": encoded})
        print("Dashboard password saved. Restart the dashboard to apply it.")
        return

    import uvicorn

    target = "multiuser:app" if os.getenv("TRADER_MULTIUSER", "false").lower() == "true" else "server:app"
    uvicorn.run(target, host=args.host, port=args.port, workers=1, reload=False,
                proxy_headers=False)


if __name__ == "__main__":
    from postgres import close_database

    try:
        main()
    finally:
        close_database()
