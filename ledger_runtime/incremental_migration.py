"""Explicit offline-only upgrade of a known legacy usage-ledger schema.

An operator must stop/disable *all* writers and prevent new ones until return,
then take a consistent SQLite backup and independently record its SHA-256.
An exclusive SQLite transaction excludes cooperating concurrent writers during
this call, but does not prove they were stopped before or will stay stopped.
Never called by Store startup, dashboard reads, or plugin installation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
from decimal import Decimal, InvalidOperation
from contextlib import closing, contextmanager
from pathlib import Path

from .incremental import install, supported
from .storage import SCHEMA


def _exact_file(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_file():
        raise ValueError('Exact regular nonsymlink target/backup path required.')
    return path


def _schema(connection: sqlite3.Connection) -> dict:
    return dict(connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index','trigger') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"))


def _known_schema(connection: sqlite3.Connection) -> None:
    # Only the current version-0 legacy DDL is admitted. No inferred future
    # schema version, missing columns, relaxed constraints or altered indexes.
    if connection.execute('PRAGMA user_version').fetchone()[0] != 0:
        raise ValueError('Unknown ledger schema version; future migration needs explicit approval.')
    from .skills import SCHEMA as SKILLS_SCHEMA
    with closing(sqlite3.connect(':memory:')) as expected:
        expected.executescript(SCHEMA + SKILLS_SCHEMA)
        if _schema(connection) != _schema(expected):
            raise ValueError('Incompatible ledger schema, columns, constraints or indexes.')
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('Ledger integrity check failed.')


def _readable_data(connection: sqlite3.Connection) -> None:
    """Check existing analytics readers' JSON contract before installing any DDL.

    Do not require newer producer fields on legacy rows: absent/null optional
    objects are valid unknowns. Present values used as mappings, arrays or
    Decimal inputs must have the shape the read projection actually consumes.
    Errors identify only a fixed table and row ordinal, never stored payloads.
    """
    from .incremental import TRACKED
    from .accounting import BUCKETS, decimal_value

    def validate_rate_prices(rate):
        # Use the same price domain as costs(), including its optional 1h
        # cache-write rate. Amounts/savings below have different sign rules.
        for key in (*BUCKETS, 'cache_write_1h_tokens'):
            decimal_value(rate.get(key))

    def finite_decimal(value):
        if value is None or value == '':
            return True
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        try:
            return Decimal(str(value)).is_finite()
        except (InvalidOperation, ValueError):
            return False

    def object_fields(record, *fields):
        return all(record.get(key) is None or isinstance(record[key], dict) for key in fields)

    def sequence_fields(record, *fields):
        return all(record.get(key) is None or isinstance(record[key], list) for key in fields)

    for table in TRACKED:
        for ordinal, (raw,) in enumerate(connection.execute(f'SELECT data FROM {table}'), 1):
            try:
                if connection.execute('SELECT json_valid(?)', (raw,)).fetchone()[0] != 1:
                    raise ValueError()
                record = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(record, dict):
                    raise ValueError()
                if table == 'requests':
                    if not object_fields(record, 'usage', 'cost', 'cache_request', 'owner',
                                         'supplemental_valuation', 'calculated_cache_writes') or not sequence_fields(record, 'session_lineage'):
                        raise ValueError()
                    usage = record.get('usage') or {}
                    cost = record.get('cost') or {}
                    if not object_fields(usage, 'raw_usage', 'field_provenance', 'normalized_usage') or not object_fields(cost, 'components', 'rate'):
                        raise ValueError()
                    raw_usage = usage.get('raw_usage') or {}
                    if not object_fields(raw_usage, 'cache_creation'):
                        raise ValueError()
                    amounts = [cost.get('known_components_usd')]
                    amounts.extend((cost.get('components') or {}).get(k) for k in BUCKETS)
                    amounts.extend(cost.get(k) for k in ('cache_read_savings_usd',
                                   'cache_write_premium_usd', 'cache_savings_usd'))
                    if not all(finite_decimal(value) for value in amounts):
                        raise ValueError()
                    if cost.get('rate') is not None:
                        validate_rate_prices(cost['rate'])
                elif table == 'skill_events':
                    if not sequence_fields(record, 'session_lineage', 'retained_skills'):
                        raise ValueError()
                elif table == 'provider_catalog':
                    if not sequence_fields(record, 'rates') or any(
                            not isinstance(rate, dict) for rate in record.get('rates') or []):
                        raise ValueError()
                    # select_rate consumes these keys without defaults, even if
                    # today's rows never select this snapshot or model. Empty
                    # legacy catalogs remain valid unknown pricing.
                    for rate in record.get('rates') or []:
                        if (not isinstance(rate.get('model'), str) or
                                not isinstance(rate.get('service_tier'), str) or
                                (rate.get('context_band') is not None and not isinstance(rate['context_band'], str))):
                            raise ValueError()
                        validate_rate_prices(rate)
                        for key in ('min_prompt_tokens', 'threshold_tokens'):
                            if key in rate and rate[key] is not None and (type(rate[key]) is not int or rate[key] < 0):
                                raise ValueError()
                        if rate.get('context_band') and type(rate.get('threshold_tokens')) is not int:
                            raise ValueError()
                    if record.get('rates') and any(not isinstance(record.get(key), str) for key in
                                                    ('source_id', 'source', 'source_url', 'content_sha256', 'origin')):
                        raise ValueError()
                    observed = record.get('observed_at')
                    if record.get('rates') and observed is None:
                        raise ValueError()
                    if observed is not None and (isinstance(observed, bool) or
                            not isinstance(observed, (int, float)) or not math.isfinite(observed)):
                        raise ValueError()
            except (sqlite3.Error, UnicodeError, RecursionError, TypeError, ValueError,
                    ArithmeticError, OverflowError):
                raise ValueError(f'Unreadable analytics data in {table} (row {ordinal}).') from None

def _reader_preflight(backup: Path, backup_sha256: str) -> None:
    """Run the production Store.read on a private copy, never the locked target.

    Store construction seeds pricing and writes schema, so bypass it. The same
    read method/DB registration runs with read-only main and writable TEMP.
    Do not inspect host processes during an offline migration: open rows with
    owner identities need explicit reconciliation before this preflight.
    """
    from .storage import Store, DecimalSum
    from .ownership import register_sql
    with tempfile.TemporaryDirectory(prefix='ledger-migration-') as scratch:
        folder = Path(scratch) / 'usage-ledger'
        folder.mkdir(mode=0o700)
        path = folder / 'events.sqlite3'
        shutil.copyfile(backup, path)
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != backup_sha256:
                raise ValueError('Verified backup changed during reader preflight.')
        reader = object.__new__(Store)
        reader.root, reader.folder, reader.path = Path(scratch), folder, path

        @contextmanager
        def db():
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as c:
                c.row_factory = sqlite3.Row
                c.create_aggregate('decimal_sum', 1, DecimalSum)
                register_sql(c)
                yield c
        reader.db = db
        try:
            with db() as c:
                if c.execute("SELECT 1 FROM requests WHERE json_type(data,'$.owner')='object' AND "
                             "((status IN ('pending','usage_received') AND ended IS NULL) OR "
                             "(json_extract(data,'$.status') IN ('pending','usage_received') "
                             "AND json_extract(data,'$.ended') IS NULL)) LIMIT 1").fetchone():
                    raise ValueError('Unreadable analytics data in requests (liveness stage).')
                last = c.execute('SELECT MAX(started) FROM requests').fetchone()[0]
                count = c.execute('SELECT COUNT(*) FROM requests WHERE started>=0').fetchone()[0]
            end = max(float(last) + 86400 if last is not None else 0, 86400, time.time() + 86400)
            # Complete historical totals and pages of actual request detail;
            # a sparse old compression need not contain modern detail keys.
            offset = 0
            while True:
                result = reader.read(0, end, offset=offset, limit=2000, view='requests')
                if result['request_count'] != count:
                    raise ValueError('Incomplete nonnegative request history.')
                offset += len(result['requests'])
                if offset >= count:
                    break
                if not result['requests']:
                    raise ValueError('Incomplete request page.')
            for group in ('time', 'model'):
                reader.read(0, end, limit=0, view='overview', group=group)
            reader.read(0, end, limit=0, view='cache')
            reader.read(0, end, limit=0, view='models')
        except ValueError as exc:
            if str(exc).startswith('Unreadable analytics data in requests (liveness stage).'):
                raise
            raise ValueError('Unreadable analytics data in reader (full-history stage).') from None
        except (sqlite3.Error, ArithmeticError, TypeError, KeyError, IndexError, OverflowError, UnicodeError, OSError):
            raise ValueError('Unreadable analytics data in reader (full-history stage).') from None


def _contents(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for line in connection.iterdump():
        digest.update(line.encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def migrate_offline(database: Path, *, confirmed: bool = False,
                    offline_target: Path | None = None, backup: Path | None = None,
                    backup_sha256: str | None = None) -> None:
    """Upgrade an exact stopped-writer target backed by a verified, matching copy.

    `confirmed` and `offline_target` are operator intent, NOT evidence that
    writers stopped. The operator must establish external writer exclusion and
    preserve the backup independently; this function checks the backup digest,
    SQLite integrity/content and an exclusive lock but cannot enforce future
    process exclusion. Partial/incompatible schemas are never auto-repaired.
    """
    if not confirmed:
        raise ValueError('Offline migration requires explicit confirmation and a verified backup.')
    if offline_target is None or backup is None or backup_sha256 is None:
        raise ValueError('Exact offline target and verified backup proof required.')
    database = _exact_file(Path(database))
    if _exact_file(Path(offline_target)) != database:
        raise ValueError('Offline target does not match the exact database path.')
    backup = _exact_file(Path(backup))
    if os.path.samefile(database, backup):
        raise ValueError('Backup cannot alias migration target.')
    if any(Path(str(backup) + suffix).exists() for suffix in ('-wal', '-journal')):
        raise ValueError('Backup must be a standalone consistent SQLite backup.')
    if (not isinstance(backup_sha256, str) or len(backup_sha256) != 64 or
            any(c not in '0123456789abcdef' for c in backup_sha256)):
        raise ValueError('Verified backup SHA-256 required.')
    with backup.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != backup_sha256:
            raise ValueError('Verified backup SHA-256 mismatch.')
    target_stat = database.stat()
    with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=2)) as connection:
        connection.execute('PRAGMA locking_mode=EXCLUSIVE')
        connection.execute('BEGIN EXCLUSIVE')
        try:
            if (database.stat().st_dev, database.stat().st_ino) != (target_stat.st_dev, target_stat.st_ino):
                raise ValueError('Migration target changed during lock acquisition.')
            with closing(sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True)) as saved:
                if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Verified backup integrity check failed.')
                if supported(connection):
                    # Idempotent valid upgrade. Still require operator proof and
                    # a valid backup, never overwrite existing journal history.
                    return
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'analytics_%' LIMIT 1").fetchone():
                    raise ValueError('Partial or incompatible analytics schema; manual review required.')
                _known_schema(connection)
                if _schema(saved) != _schema(connection) or _contents(saved) != _contents(connection):
                    raise ValueError('Verified backup does not match locked ledger contents.')
            _readable_data(connection)
            _reader_preflight(backup, backup_sha256)
            install(connection)
            if not supported(connection):
                raise ValueError('Incremental journal validation failed.')
            if (database.stat().st_dev, database.stat().st_ino) != (target_stat.st_dev, target_stat.st_ino):
                raise ValueError('Migration target changed before commit.')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
