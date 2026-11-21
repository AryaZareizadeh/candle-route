# Architecture

## Components

The application runs one Python process. FastAPI serves the plain JavaScript
dashboard in `static/index.html`, REST endpoints and a WebSocket update stream.
There is no Vue build, separate frontend server or trained AI model.

With `TRADER_MULTIUSER=true`, `multiuser.py` authenticates accounts and dispatches
each request to a separate dashboard instance. Account records and emailed code
hashes are in PostgreSQL when configured. Each user has scoped trading rows,
a private artifact workspace, bot registry, research jobs, WebSocket stream,
and exchange client. In SQLite mode each user has a separate trading file. The user ID
comes from the server-side session, never from a request path or body. Exchange
credentials are encrypted in the account database with `TRADER_MASTER_KEY`.

| Module | Responsibility |
| --- | --- |
| `main.py`, `settings.py` | Launcher, password setup, backup and configured paths |
| `auth.py` | Operator password verification, in-memory sessions, CSRF and origin checks |
| `server.py` | HTTP/WebSocket interface and application lifecycle |
| `bot_manager.py`, `bot.py` | Bot configuration, runtime state and live order lifecycle |
| `storage.py`, `postgres.py`, `migrations/` | State transactions, schema versions and deployment ownership |
| `data.py`, `indicators.py` | OHLCV retrieval, closed-candle filtering and cached TA-Lib indicators |
| `strategy.py` | AdvRSI, TrendMomentum and beta ExtremeReversal signal rules |
| `execution_model.py`, `accounting.py` | Historical fills, costs, reserved capital and equity |
| `backtester.py`, `optimizer.py` | Tester jobs and Differential Evolution parameter search |
| `nobitex_client.py` | Nobitex account, order and position API adapter |

## Data and execution

Nobitex provides candles and live margin execution. Binance is an optional
historical data source. A Binance research result does not represent an order
executed at Nobitex: exchange prices, liquidity and available markets can differ.

Historical research routes use the shared execution model. It consumes closed
candles and strategy decisions, simulates fills and produces trades, equity and
metadata. Optimizer workers receive data and serializable settings; the parent
process owns job persistence. See [backtesting](backtesting.md) for timing and
accounting assumptions. Existing real bots keep their exchange order policy.

## State and identity

The configured backend is authoritative. Versioned records hold
configuration, runtime snapshots, order intents and research results; a separate
ownership table prevents two bots claiming the same account/position ID. Writes
use transactions and database constraints. PostgreSQL scopes private records by
account and uses one database-held deployment lock. SQLite uses WAL, full
synchronization and a local file lock.

Run one active deployment per exchange account, including during migration and
restore. Model files and the standalone forecasting workspaces live under
`TRADER_DATA_DIR` and are included in coordinated backup bundles.

Legacy JSON files are migration inputs and remain intact. Restored bots start
stopped. Interrupted research jobs remain visible as interrupted; optimizer
populations are not resumed. An exchange submission with an uncertain outcome
must retain its saved identity and be reconciled before another submission.

## Authentication and secrets

A local setup command writes a salted scrypt password hash to the selected
environment file. The browser receives an opaque, HttpOnly, SameSite=Strict
session cookie and a session-bound CSRF token. Sessions expire, are revoked on
logout and disappear on application restart. REST APIs and WebSockets require a
session; browser origins and hostnames must match configuration.

That paragraph describes legacy single-user mode. In multi-user mode, invited
users register with a username, password and verified email. Recovery codes expire
after ten minutes and are single-use. Exchange credentials are encrypted at rest
in the accounts store and are never returned by API responses. Remote HTTPS
requires Secure cookies and a reverse proxy preserving the original Host.

## Tests

`tests/conftest.py` selects temporary data/environment paths before application
imports, removes exchange credentials from the test environment, rejects writes
to operator configuration and blocks requests HTTP calls. Synthetic candles and
scripted fake exchanges test calculation and recovery failures offline. CI runs
the suite on Python 3.13 on Windows and Linux; passing local Windows tests alone
does not establish that the Linux CI job passed.
