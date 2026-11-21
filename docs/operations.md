# Operations and recovery

## PostgreSQL deployment

Follow the [Docker setup](../README.md#docker-setup) for a fresh multi-user
deployment. The PostgreSQL service uses a private Compose network and its own
`postgres_data` volume. The `trader_data` volume mounts at `/data` and holds models,
forecasting workspaces and other artifacts. Keep the original `TRADER_MASTER_KEY`
in private configuration across rebuilds and restores.

For a native Windows installation, create a dedicated PostgreSQL 17 database
owned by a Candle Route role. Install the PostgreSQL 17 client utilities
(`pg_dump` and `pg_restore`) on PATH for backups and restores. Configure the
following in the private environment file, with the password URL-encoded.

```dotenv
TRADER_DATABASE_URL=postgresql://candle_route:PASSWORD@127.0.0.1:5432/candle_route
```

Use one dashboard process per database and artifact directory. PostgreSQL
enforces a deployment lease while the dashboard runs. The liveness endpoint
`/healthz` reports process health, and `/readyz` checks the PostgreSQL lease.

### Existing SQLite deployment

1. Stop the dashboard so all database and file writers are idle. Preserve a
   verified SQLite bundle and the private configuration, including the original
   master key for multi-user deployments. Keep single-user and multi-user mode
   consistent when moving an entire runtime.
2. Create a new PostgreSQL database. Set `TRADER_DATABASE_URL` and keep
   `TRADER_DATA_DIR` pointed at the existing artifact directory. Check the import
   source and target using `python main.py migrate-postgres --source PATH --dry-run`.
   `PATH` can be a stopped SQLite runtime directory or a verified SQLite bundle.
3. Run the same command without `--dry-run`. The importer verifies copied rows
   and artifact references. An import can also populate a new artifact directory
   using `--destination`. Use that same directory as `TRADER_DATA_DIR` when serving.
4. Start the dashboard and check `/readyz`, login and representative account or
   portfolio history. Review real bots with saved positions or unresolved orders
   against the exchange before starting them.

For Compose, use the same `--env-file` and `-f` flags as in the README. Stop
`dashboard`, build the updated image, then start just `postgres` with
`up -d --wait postgres`. Run the importer in a one-off dashboard container
with `run --rm --no-deps dashboard python main.py migrate-postgres --source /data`.
Add `--dry-run` for the first invocation. Start the dashboard after verification.

The source remains available for rollback until PostgreSQL accepts application
writes. After that, stop the dashboard and preserve the PostgreSQL state before
considering a rollback. Reconcile changes against the exchange before starting
any real bot on the old database.

### PostgreSQL backup and restore

Stop the dashboard, then run `python main.py backup-bundle --output PATH`
using the same database URL and artifact directory. Run
`python main.py verify-bundle --source PATH` to verify the archive manifest.
For a restore drill or recovery, set `TRADER_DATABASE_URL` to a new empty database
and run `python main.py restore-bundle --source PATH --destination NEW_DIRECTORY`.
Restore bundles from a trusted Candle Route deployment.
Keep the matching master key separately and point the dashboard at the restored
database and directory together. The `backup` command produces a database-only
dump.

SMTP must be configured before inviting users. Signup emails confirm ownership of
the address, and password reset codes go only to confirmed addresses. If email
delivery fails, fix SMTP before asking a user to retry; the server never exposes
the recovery code in its response. Invite codes can be rotated by updating
`docker.env` and restarting Compose; existing accounts remain usable.

The legacy import command copies a single-operator SQLite database into a
verified account and retains its source. Verify the new account's email without
signing in, then stop the multi-user container before importing. First stop the
old runtime and make a consistent backup with `main.py backup`.
Mount the backup read-only into a one-off Compose container and use it as
`--source`. If the old `.env` holds Nobitex credentials, mount that file
read-only and set `TRADER_ENV_FILE` to its mount path while running the import
command so the credentials can be encrypted for the user. The Compose
`TRADER_MASTER_KEY` must match the running multi-user deployment.
After import, inspect every real bot and reconcile saved order or position state
against Nobitex before starting it. Never start the old and new runtimes against
the same exchange account at the same time.

## Local Windows dashboard

Follow the [README](../README.md) for the verified Python 3.13 installation.
`python main.py` binds to `127.0.0.1:8011`, runs one worker and disables reload.
Paths resolve from the source location, independently of the current directory.
Use absolute `TRADER_DATA_DIR` and `TRADER_ENV_FILE` values when moving state.

With the application stopped, run `python main.py setup-password` to set or reset
the operator password. The command prompts without echoing it and writes only a
salted password hash. Restart to apply a changed hash. Missing configuration
leaves APIs and WebSockets locked. Sessions last 12 hours by default and are
revoked on logout or application restart.

Configure exchange credentials after login. The API returns credential-presence
metadata, never secret values. Credential changes are rejected while a real bot
is running, owns a saved position, or has an unresolved order. Resolve those
states before switching accounts. Keep environment files private to the Windows
operator account and outside Git.

## Startup, stopping and uncertain orders

Startup migrates legacy JSON configuration once, retains original files and
loads durable state. Bots remain stopped until explicitly started. A prior
running state is descriptive; restarting the application does not authorize
new orders. Research jobs that did not finish become `interrupted` and need a
new run.

Stopping the application is **not** an instruction to close exchange positions.
When manual closure is intended, close the position in Nobitex, keep the bot
stopped, and use **Recover → Reconcile recorded state** before restarting it.
Do not delete runtime state to clear a position or error.

An order timeout can mean the exchange accepted it. The saved order intent and
acknowledged exchange IDs must be reconciled before a new submission. If the
application cannot establish the identity/outcome, keep that bot stopped and
inspect the order and position in the exchange account. Retain the database and
events for diagnosis; blindly retrying or resetting the bot can create duplicate
exposure. A legacy/manual exchange position has no automatic owner simply
because its market matches a bot.

For an accepted order whose response was lost, use **Recover → Bind and reconcile
order** only after locating the exact exchange order ID in Nobitex. The dashboard
requires the local intent and exchange order to match before binding them. Use
**Adopt this position** for an existing open position that has no recorded owner.

## Legacy SQLite backup

The backup command uses SQLite's online backup facility, so committed WAL data
is included. It refuses to overwrite an existing destination:

```powershell
.\.venv\Scripts\python.exe main.py backup --output D:\TradingBackups\trader-2026-09-15.sqlite3
```

Run the command under the same `TRADER_DATA_DIR` and `TRADER_ENV_FILE` settings
as the dashboard. It does not start bots or call the exchange. Back up the
environment file separately through a secure channel; it is not in SQLite.
Store backups on another protected disk and periodically verify a copy using:

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('D:/TradingBackups/trader-2026-09-15.sqlite3'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

Expect `ok`. Automated tests verify backup data preservation and destination
protection with temporary databases.

## Legacy SQLite restore

1. Stop the application and ensure no process is using its database.
2. Preserve the entire current data directory, including WAL/SHM companions,
   for diagnosis. Select a **new empty directory** for the restored runtime.
3. Copy the verified backup into the new directory as `trader.sqlite3`. Do not
   mix an older database with WAL/SHM files from a newer runtime.
4. Set `TRADER_DATA_DIR` to that directory; keep the same exchange identity and
   supply its environment file securely. Start one dashboard process.
5. Review retained bot/order state and reconcile it against the exchange before
   explicitly restarting any real bot. A stale database may omit later orders;
   it is not a trading rollback. Keep unresolved bots stopped.

## Later private Linux server

Linux with Python 3.13 is the proposed private-server target. The repository
includes Linux CI alongside Windows CI. Require that job and the offline
workflow to pass on the selected server before treating it as supported; local
Windows verification does not prove the Linux installation.

Use a dedicated OS account, a Python virtual environment, a persistent local
data directory, and an environment file readable only by that account. Install
the same manifests/constraints and run `pip check` and `pytest`. Do not put
SQLite on a shared/network filesystem or run multiple workers.

Terminate HTTPS at a reverse proxy that supports WebSocket upgrades and
preserves the original `Host` header. Bind the application to server loopback
and restrict external access to a private network/VPN or firewall allowlist.
Configure:

```dotenv
TRADER_DATA_DIR=/var/lib/nobitex-dashboard
TRADER_ENV_FILE=/etc/nobitex-dashboard/operator.env
TRADER_HOST=127.0.0.1
TRADER_PORT=8011
DASHBOARD_ALLOWED_HOSTS=trading.example.internal
DASHBOARD_SECURE_COOKIE=true
```

Set `TRADER_ENV_FILE` in the service manager environment before launch so the
application can locate that file. `DASHBOARD_ALLOWED_HOSTS` contains hostnames,
without schemes or ports. Browser requests must use the exact HTTPS origin.
The launcher disables proxy-header trust; authentication uses its explicit
HTTPS setting and the preserved Host, rather than trusting forwarded headers.

A systemd service can use the following core settings, after replacing paths:

```ini
[Unit]
Description=Nobitex single-operator dashboard
After=network-online.target

[Service]
User=nobitex-dashboard
WorkingDirectory=/opt/nobitex-dashboard
Environment=TRADER_ENV_FILE=/etc/nobitex-dashboard/operator.env
ExecStart=/opt/nobitex-dashboard/.venv/bin/python /opt/nobitex-dashboard/main.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=90
UMask=0077

[Install]
WantedBy=multi-user.target
```

Use the service journal for process failures, verify graceful stop/restart, and
schedule backups under the same OS account. This example is a deployment
template, not a claim that a server has been provisioned or tested.

## Cutover rehearsal and live migration

First use synthetic data, a fake exchange and a temporary database to rehearse
login, retained history, interrupted jobs and restart recovery on the server.
The offline suite exercises these mechanisms without credentials. Verify the
HTTPS login and WebSocket connection manually in the target private network.

For eventual live cutover, stop the Windows runtime before taking the final
backup. Transfer it and secrets securely, start the server with bots stopped,
reconcile every saved or unresolved order/position, and resume explicitly. Do
not start both machines against the account: database locks cannot coordinate
separate copies. For rollback, stop the server first, transfer its newest state
back, reconcile again and only then resume the Windows runtime.
When starting CandleRoute from a new checkout with an existing Compose install,
pass `docker compose -p <existing-project-name>` so Compose opens the existing
`trader_data` volume. Use `docker compose ls` to check the project name. Back up
the old runtime before the switch. For an isolated trial, restore a backup to a
separate volume and bind the trial deployment to a different host port.

Each account's `forecasting/` folder is stored in its workspace under `TRADER_DATA_DIR`.
The stopped-runtime bundle includes durable forecasting files. Keep `docker.env`
and its master key backed up separately.
