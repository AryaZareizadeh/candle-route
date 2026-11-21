"""Verified recovery bundles of stopped runtimes; secrets remain separate."""
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import zipfile

from forecast_storage import forecast_artifacts, forecast_workspace, is_forecast_artifact
from runtime_lease import FileLease

FORMAT_VERSION = 1
POSTGRES_FORMAT_VERSION = 2
POSTGRES_DUMP = 'postgres.dump'
MAX_BUNDLE_BYTES = 32 * 1024 ** 3


def _safe_member(name):
    if not isinstance(name, str):
        raise ValueError('Unsafe archive member path')
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute()
            or any(part in {'', '.', '..'} for part in name.split('/'))
            or any(ord(character) < 32 for character in name)):
        raise ValueError('Unsafe archive member path')
    for part in path.parts:
        if (part.endswith(('.', ' ')) or any(char in '<>"|?*' for char in part)
                or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', part.split('.')[0], re.I)):
            raise ValueError('Unsafe archive member path')
    return path


def _digest(stream):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _registered_artifacts(records):
    from trainer_catalog import artifact_suffix

    for namespace, key, raw in records:
        record = json.loads(raw)
        if namespace == 'trained_models':
            if record.get('status') != 'ready':
                continue
            if not re.fullmatch('[0-9a-f]{32}', key):
                raise ValueError('Invalid registered model artifact identity')
            suffix = artifact_suffix(record.get('architecture', 'alexnet1d-v1'))
            yield PurePosixPath('models', key + suffix), None
        elif isinstance(record, dict):
            model = record.get('model_artifact')
            references = [(model, record.get('model_file_sha256')),
                          ((record.get('data_snapshot') or {}).get('file'), None)]
            if namespace == 'spot_manager' and record.get('paper_model') and not model:
                references.append(('spot_research_model.joblib', None))
            for reference, checksum in references:
                if reference:
                    member = _safe_member(reference)
                    if len(member.parts) != 1 or not reference.startswith('spot_'):
                        raise ValueError('Invalid registered spot artifact path')
                    yield member, checksum


def _check_artifact(root, relative, checksum):
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.resolve() != path:
        raise ValueError('A registered model or research artifact is missing or linked')
    if checksum:
        with path.open('rb') as stream:
            if _digest(stream)[0] != checksum:
                raise ValueError('Registered spot model artifact hash mismatch')


def _required_artifacts(root, *, artifacts_root=None):
    artifacts_root = root if artifacts_root is None else artifacts_root
    for database in root.rglob('trader.sqlite3'):
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='records'").fetchone():
                continue
            records = conn.execute("SELECT namespace,key,value FROM records WHERE "
                                   "namespace IN ('trained_models','spot_experiments') "
                                   "OR (namespace='spot_manager' AND key='research')")
            scope = artifacts_root / database.parent.relative_to(root)
            for relative, checksum in _registered_artifacts(records):
                _check_artifact(scope, relative, checksum)


def _is_runtime_artifact(parts):
    if len(parts) > 2 and parts[0] == 'users' and re.fullmatch('[0-9a-f]{32}', parts[1]):
        parts = parts[2:]
    if not parts or any(part in ('.env', 'docker.env') for part in parts):
        return False
    if parts[0] == 'forecasting':
        return is_forecast_artifact(parts[1:])
    if len(parts) == 2 and parts[0] == 'models':
        return Path(parts[-1]).suffix in ('.pt', '.joblib')
    return (len(parts) == 1 and parts[0].startswith('spot_')
            and parts[0].endswith(('.joblib', '.json', '.json.gz')))


def _runtime_artifacts(root):
    directories = [root]
    directories.extend(path for path in (root / 'users').glob('*')
                       if path.is_dir() and re.fullmatch('[0-9a-f]{32}', path.name))
    found = set()
    for directory in directories:
        if directory.resolve() != directory:
            raise ValueError('Runtime contains a linked account directory')
        workspace = forecast_workspace(directory)
        if workspace.is_dir():
            found.update(path for path in forecast_artifacts(workspace)
                         if _is_runtime_artifact(path.relative_to(root).parts))
        for path in (*directory.glob('spot_*'), *(directory / 'models').glob('*')):
            if path.is_file() and _is_runtime_artifact(path.relative_to(root).parts):
                found.add(path)
    for path in found:
        _safe_member(path.relative_to(root).as_posix())
        if path.resolve() != path:
            raise ValueError('Runtime contains a linked artifact')
    return sorted(found)


def _postgres_references(conn):
    rows = conn.execute("SELECT scope_id,namespace,key,value FROM candle_route.records WHERE "
                        "namespace IN ('trained_models','spot_experiments') "
                        "OR (namespace='spot_manager' AND key='research')")
    for scope, namespace, key, value in rows:
        if scope == 'single':
            parent = PurePosixPath()
        elif re.fullmatch('[0-9a-f]{32}', scope):
            parent = PurePosixPath('users', scope)
        else:
            raise ValueError('Invalid PostgreSQL artifact scope')
        for relative, checksum in _registered_artifacts([(namespace, key, value)]):
            yield parent / relative, checksum


def _publish_file(source, output):
    # Exclusive creation prevents overwriting a concurrently created backup.
    with output.open('xb') as destination, source.open('rb') as stream:
        try:
            shutil.copyfileobj(stream, destination)
            destination.flush()
            os.fsync(destination.fileno())
        except BaseException:
            destination.close()
            output.unlink(missing_ok=True)
            raise


@contextmanager
def _publish_artifacts(staging, destination, *, workspace=None):
    # An empty Docker volume mount cannot be renamed. Publish its children from
    # a temporary sibling inside the same mount, and track them for rollback.
    if not destination.exists():
        staging.rename(destination)
        try:
            yield
        except BaseException:
            destination.rename(staging)
            raise
        return
    if any(path != workspace for path in destination.iterdir()):
        raise ValueError('Artifact destination must be empty')
    published = []
    try:
        for path in sorted(staging.iterdir()):
            target = destination / path.name
            if target.exists() or target.is_symlink():
                raise ValueError('Artifact destination changed during publication')
            path.rename(target)
            published.append((target, path))
        yield
    except BaseException:
        for target, original in reversed(published):
            target.rename(original)
        raise


def _archive_bundle(data, archive_path, output, *, postgres=False, key_required=False, references=()):
    files = []
    for path in sorted(data.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                digest, size = _digest(stream)
            files.append(dict(path=path.relative_to(data).as_posix(), sha256=digest, size=size))
    manifest = dict(format_version=POSTGRES_FORMAT_VERSION if postgres else FORMAT_VERSION,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    requires_separate_master_key=key_required, files=files)
    if postgres:
        manifest.update(database='postgres', registered_artifacts=sorted(references))
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, indent=2))
        for item in files:
            archive.write(data / item['path'], item['path'])
    verify_bundle(archive_path)
    _publish_file(archive_path, output)
    return manifest


@contextmanager
def _client_connection(database):
    # Effective libpq parameters include the caller's connection service settings.
    # The child argv contains only a service label.
    with database.connection() as conn:
        options = {item.keyword: item.val for item in conn.pgconn.info
                   if item.val is not None and item.keyword != b'service'}
    from psycopg.conninfo import conninfo_to_dict

    # Let the client choose defaults for settings the operator did not supply; this
    # avoids passing new libpq options to a compatible client from an older release.
    option_keys = {key.encode('ascii') for key in conninfo_to_dict(database.dsn)}
    option_keys.update((b'user', b'dbname', b'host', b'port', b'hostaddr', b'connect_timeout', b'options'))
    if b'service' in option_keys:
        from psycopg.pq import Conninfo

        defaults = {item.keyword: item.val for item in Conninfo.get_defaults()}
        option_keys.update(name for name, value in options.items() if defaults.get(name) != value)
    with tempfile.TemporaryDirectory(prefix='candleroute-pg-') as temporary:
        service = Path(temporary) / 'pg_service.conf'
        fd = os.open(service, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(b'[candleroute_operator]\n')
            password = options.pop(b'password', None)
            for name, value in options.items():
                if name not in option_keys:
                    continue
                if value != value.rstrip() or b'\r' in value or b'\n' in value:
                    raise ValueError('PostgreSQL backup client cannot represent a connection option')
                stream.write(name + b'=' + value + b'\n')
        environment = os.environ.copy()
        environment.pop('TRADER_DATABASE_URL', None)
        environment['PGSERVICEFILE'] = str(service)
        if password is not None:
            environment['PGPASSWORD'] = os.fsdecode(password)
        yield environment


def _pg_tool(name, *arguments, database=None, capture=False):
    executable = shutil.which(name)
    if executable is None:
        raise ValueError(f'Install the PostgreSQL client tool {name} and add it to PATH')
    command = [executable, *map(str, arguments)]
    if database is None:
        completed = subprocess.run(command, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False)
    else:
        with _client_connection(database) as environment:
            command[1:1] = ['--no-password', '--dbname=service=candleroute_operator']
            completed = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, check=False)
    if completed.returncode:
        raise ValueError(f'{name} failed. Check access, target permissions and PostgreSQL client compatibility.')
    return completed.stdout


def _postgres_dump_inventory(path):
    inventory = _pg_tool('pg_restore', '--list', path, capture=True)
    entries = [line.split(b';', 1)[1].split()[2:] for line in inventory.splitlines()
               if line.partition(b';')[0].isdigit()]
    if not any(entry[:3] == [b'SCHEMA', b'-', b'candle_route'] for entry in entries):
        raise ValueError('PostgreSQL dump is missing the CandleRoute schema')
    required = {b'schema_migrations', b'users', b'records'}
    tables = {entry[2] for entry in entries if len(entry) > 3 and entry[0] == b'TABLE' and entry[1] == b'candle_route'}
    if not required <= tables:
        raise ValueError('PostgreSQL dump is missing required CandleRoute tables')
    foreign_schemas = [entry for entry in entries if entry[:2] == [b'SCHEMA', b'-'] and entry[2] != b'candle_route']
    foreign_tables = [entry for entry in entries if len(entry) > 3 and entry[0] == b'TABLE'
                      and entry[1] not in (b'candle_route', b'DATA')]
    if foreign_schemas or foreign_tables:
        raise ValueError('PostgreSQL dump contains unrelated schemas or tables')


def _dump_postgres(database, output, *, snapshot=None):
    snapshot_args = ('--snapshot', snapshot) if snapshot else ()
    _pg_tool('pg_dump', '--format=custom', '--schema=candle_route', '--strict-names', '--no-owner', '--no-acl',
             '--file', output, *snapshot_args, database=database)
    _postgres_dump_inventory(output)


def postgres_backup(database, output):
    """Publish an online database-only dump without overwriting an existing file."""
    output = Path(output).absolute()
    if output.exists():
        raise ValueError('Backup destination already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.pg-dump-', dir=output.parent) as temporary:
        dump = Path(temporary) / POSTGRES_DUMP
        _dump_postgres(database, dump)
        _publish_file(dump, output)
    return output


def _postgres_bundle(database, source, output):
    with ExitStack() as stack:
        stack.enter_context(database.runtime_lease())
        stack.enter_context(FileLease(source / '.dashboard.lock'))
        artifacts = _runtime_artifacts(source)
        staging = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.bundle-', dir=output.parent)))
        data = staging / 'data'
        data.mkdir()
        for path in artifacts:
            destination = data / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        with database.connection() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            snapshot = conn.execute('SELECT pg_export_snapshot()').fetchone()[0]
            key_required = bool(conn.execute('SELECT 1 FROM candle_route.users LIMIT 1').fetchone())
            references = set()
            for relative, checksum in _postgres_references(conn):
                _check_artifact(data, relative, checksum)
                references.add(relative.as_posix())
            _dump_postgres(database, data / POSTGRES_DUMP, snapshot=snapshot)
        return _archive_bundle(data, staging / 'bundle.zip', output, postgres=True,
                               key_required=key_required, references=references)


def backup_bundle(source, output, *, database=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError('Runtime source directory does not exist')
    if output.exists():
        raise ValueError('Backup destination already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    if database is not None:
        return _postgres_bundle(database, source, output)
    with ExitStack() as stack:
        stack.enter_context(FileLease(source / '.dashboard.lock'))
        databases = sorted(source.rglob('*.sqlite3'))
        if not databases:
            raise ValueError('No runtime databases found')
        for database in databases:
            stack.enter_context(FileLease(str(database) + '.lock'))
        # Freeze candidate paths before staging, which may be inside the source
        # volume when the operator stores its bundle under data/backups.
        candidates = list(source.rglob('*'))
        workspaces = {forecast_workspace(database.parent) for database in databases if database.name == 'trader.sqlite3'}
        workspaces.add(forecast_workspace(source))
        workspaces.update(path for path in (source / 'users').glob('*/forecasting') if path.is_dir())
        forecasts = {path for workspace in workspaces if workspace.is_dir() for path in forecast_artifacts(workspace)}
        staging = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.bundle-', dir=output.parent)))
        data = staging / 'data'
        data.mkdir()
        for path in sorted({*candidates, *forecasts}):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            _safe_member(relative.as_posix())
            database = path.suffix == '.sqlite3'
            artifact = ('models' in relative.parts and path.suffix in {'.pt', '.joblib'}) or (
                path.name.startswith('spot_') and (path.suffix in {'.joblib', '.json'} or path.name.endswith('.json.gz')))
            if any(path.is_relative_to(workspace) for workspace in workspaces):
                if path not in forecasts:
                    continue
                database = False
            elif not (database or artifact):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ValueError('Runtime artifact path escapes its directory')
            destination = data / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if database:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as original:
                    with closing(sqlite3.connect(destination)) as target:
                        original.backup(target)
            else:
                shutil.copyfile(path, destination)
        _required_artifacts(data)
        return _archive_bundle(data, staging / 'bundle.zip', output,
                               key_required=(data / 'accounts.sqlite3').exists())


def _manifest(archive):
    members = archive.infolist()
    names = [member.filename for member in members]
    if len(names) != len({name.casefold() for name in names}) or 'manifest.json' not in names:
        raise ValueError('Invalid archive member manifest')
    for member in members:
        _safe_member(member.filename)
        if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
            raise ValueError('Unsupported archive member type')
    if sum(member.file_size for member in members) > MAX_BUNDLE_BYTES:
        raise ValueError('Archive exceeds the supported size')
    if archive.getinfo('manifest.json').file_size > 16 * 1024 * 1024:
        raise ValueError('Oversized archive manifest')
    try:
        manifest = json.loads(archive.read('manifest.json'))
        if manifest['format_version'] not in (FORMAT_VERSION, POSTGRES_FORMAT_VERSION) or not isinstance(manifest['files'], list):
            raise ValueError('Unsupported archive manifest')
        paths = [item['path'] for item in manifest['files']]
        if len(paths) != len(set(paths)) or set(paths) | {'manifest.json'} != set(names):
            raise ValueError('Archive members do not match manifest')
        for item in manifest['files']:
            _safe_member(item['path'])
            if (type(item['size']) is not int or item['size'] < 0
                    or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
                raise ValueError('Invalid archive integrity manifest')
        if manifest['format_version'] == POSTGRES_FORMAT_VERSION:
            references = manifest['registered_artifacts']
            if (manifest['database'] != 'postgres' or POSTGRES_DUMP not in paths
                    or not isinstance(references, list) or not all(isinstance(path, str) for path in references)
                    or not set(references) <= set(paths) - {POSTGRES_DUMP}
                    or any(path != POSTGRES_DUMP and not _is_runtime_artifact(_safe_member(path).parts) for path in paths)):
                raise ValueError('Invalid PostgreSQL archive inventory')
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid archive manifest') from exc
    return manifest


def verify_bundle(source):
    try:
        with zipfile.ZipFile(source) as archive:
            manifest = _manifest(archive)
            for item in manifest['files']:
                if archive.getinfo(item['path']).file_size != item['size']:
                    raise ValueError('Archive size integrity check failed')
                with archive.open(item['path']) as stream:
                    digest, size = _digest(stream)
                if digest != item['sha256'] or size != item['size']:
                    raise ValueError('Archive hash integrity check failed')
            return manifest
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError('Archive integrity verification failed') from exc


def restore_bundle(source, destination, *, database=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Restore destination must be a new or empty directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        if database is not None:
            stack.enter_context(database.runtime_lease())
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix='.restore-', dir=destination if destination.exists() else destination.parent)))
        staging = temporary / 'data'
        staging.mkdir()
        with zipfile.ZipFile(source) as archive:
            manifest = _manifest(archive)
            postgres = manifest['format_version'] == POSTGRES_FORMAT_VERSION
            if postgres != (database is not None):
                message = ('Configure an empty PostgreSQL target to restore this bundle' if postgres
                           else 'Use migrate-postgres to transfer a SQLite bundle into PostgreSQL')
                raise ValueError(message)
            for item in manifest['files']:
                path = staging.joinpath(*_safe_member(item['path']).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item['path']) as original, path.open('xb') as target:
                    digest, size = hashlib.sha256(), 0
                    while chunk := original.read(1024 * 1024):
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    if size != item['size'] or digest.hexdigest() != item['sha256']:
                        raise ValueError('Archive hash integrity check failed during restore')
        if postgres:
            stack.enter_context(database.lock('deployment:schema', wait=True))
            with database.connection() as conn:
                if conn.execute("SELECT to_regnamespace('candle_route')").fetchone()[0] is not None:
                    raise ValueError('The destination PostgreSQL database already contains a CandleRoute schema')
            dump = temporary / POSTGRES_DUMP
            (staging / POSTGRES_DUMP).rename(dump)
            _postgres_dump_inventory(dump)
            with _publish_artifacts(staging, destination, workspace=temporary):
                # Dumps produced here contain only the application schema. A restore
                # --schema filter also drops the CREATE SCHEMA entry.
                _pg_tool('pg_restore', '--single-transaction', '--no-owner', '--no-acl', dump, database=database)
            return manifest
        for database in staging.rglob('*.sqlite3'):
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
                if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Restored database integrity check failed')
        _required_artifacts(staging)
        with _publish_artifacts(staging, destination, workspace=temporary):
            pass
    return manifest
