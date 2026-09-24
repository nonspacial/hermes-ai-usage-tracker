"""Opt-in transactional ledger change history for bounded analytics catch-up.

This is durable evidence, not a disposable response cache. Triggers cover writers
that bypass Store (notably project reconciliation). Existing ledgers are never
migrated implicitly; only a newly created Store installs this schema.
"""
from __future__ import annotations

import sqlite3
import uuid
from decimal import Decimal
from functools import lru_cache

CAPACITY = 4096
MAX_ROW_BYTES = 32768
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
TRACKED = ('requests', 'compressions', 'events', 'session_context', 'provider_catalog',
           'pricing_status', 'rates', 'tests', 'health', 'quota', 'skill_events')
COLUMNS = {
    'requests': ('id','started','ended','provider','model','session_id','task','compression_id','status','data'),
    'compressions': ('id','started','ended','provider','session_id','session_after','next_request_id','data'),
    'events': ('seq','ts','kind','item_id','data'),
    'session_context': ('session_id','updated','data'),
    'provider_catalog': ('id','source_id','observed','data'),
    'pricing_status': ('source_id','data'),
    'rates': ('id','created','data'),
    'tests': ('id','started','ended','label','data'),
    'health': ('process','updated','data'),
    'quota': ('seq','ts','data'),
    'skill_events': ('id','ts','kind','session_id','provider','model','skill','data'),
}


def safe_cost(record: dict) -> bool:
    """Conservative admission for reassociating legacy Decimal(28) SQL sums.

    At most 100,000 attempts are admitted by the caller: adding at most five
    integer digits to any value with <=22 integer+fractional digits stays below
    28 significant digits. The same guard applies to post-snapshot changes.
    """
    cost = record.get('cost') or {}
    if not isinstance(cost, dict):
        return False
    components = cost.get('components') or {}
    if not isinstance(components, dict):
        return False
    values = [cost.get(k) for k in ('known_components_usd', 'cache_read_savings_usd',
                                    'cache_write_premium_usd', 'cache_savings_usd')]
    values.extend(components.values())
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str):
            return False
        try:
            decimal = Decimal(value)
            if not decimal.is_finite():
                return False
            digits = len(decimal.as_tuple().digits)
            exponent = decimal.as_tuple().exponent
            if not isinstance(exponent, int) or max(0, digits + exponent) + max(0, -exponent) > 22:
                return False
        except (ValueError, TypeError):
            return False
    return True


META_SQL = 'CREATE TABLE analytics_change_meta(version INTEGER NOT NULL, identity TEXT NOT NULL)'
CHANGES_SQL = '''CREATE TABLE analytics_changes(
        revision INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, item_id TEXT,
        old_row TEXT, new_row TEXT)'''

def _triggers():
    definitions = {}
    for table in TRACKED:
        # Store exact SQL column values as a JSON object; data is a JSON string,
        # not parsed/reformatted by SQLite (which would lose integer precision).
        columns = COLUMNS[table]
        identity = 'id' if 'id' in columns else ('session_id' if table == 'session_context' else
                   'source_id' if table == 'pricing_status' else
                   'process' if table == 'health' else 'seq')
        for operation, old, new in (('INSERT', 'NULL', 'NEW'),
                                    ('UPDATE', 'OLD', 'NEW'),
                                    ('DELETE', 'OLD', 'NULL')):
            def record(alias):
                if alias == 'NULL':
                    return 'NULL'
                # json_set() can mark NEW.data with SQLite's JSON subtype;
                # substr() strips it so journal data remains the exact TEXT
                # value, not a parsed/re-serialized JSON object.
                return 'json_object(' + ','.join(
                    f"'{column}',"+(f'substr({alias}.{column},1)' if column == 'data'
                                    else f'{alias}.{column}') for column in columns) + ')'
            subject = new if new != 'NULL' else old
            old_value = record(old) if table == 'requests' else 'NULL'
            new_value = record(new) if table == 'requests' else 'NULL'
            oversized = (f'(length(CAST({old_value} AS BLOB))>{MAX_ROW_BYTES} OR '
                         f'length(CAST({new_value} AS BLOB))>{MAX_ROW_BYTES})') if table == 'requests' else '0'
            name = f'analytics_{table}_{operation.lower()}'
            definitions[name] = f'''CREATE TRIGGER {name}
                AFTER {operation} ON {table} BEGIN
                INSERT INTO analytics_changes(kind,item_id,old_row,new_row)
                    VALUES(CASE WHEN {oversized} THEN 'oversize' ELSE '{table}' END,
                           substr(CAST({subject}.{identity} AS TEXT),1,256),
                           CASE WHEN {oversized} THEN NULL ELSE {old_value} END,
                           CASE WHEN {oversized} THEN NULL ELSE {new_value} END);
                DELETE FROM analytics_changes WHERE revision <=
                    (SELECT MAX(revision)-{CAPACITY} FROM analytics_changes);
                DELETE FROM analytics_changes WHERE revision < COALESCE((
                    SELECT MIN(revision) FROM (
                        SELECT revision, SUM(length(CAST(COALESCE(old_row,'') AS BLOB)) +
                            length(CAST(COALESCE(new_row,'') AS BLOB)) +
                            length(CAST(COALESCE(item_id,'') AS BLOB)) +
                            length(CAST(kind AS BLOB)) + 64)
                            OVER (ORDER BY revision DESC) AS bytes
                        FROM analytics_changes)
                    WHERE bytes <= {MAX_JOURNAL_BYTES}),
                    (SELECT MAX(revision) FROM analytics_changes));
                END'''
    return definitions

TRIGGERS = _triggers()

@lru_cache(maxsize=1)
def _source_schema():
    from .storage import SCHEMA
    from .skills import SCHEMA as SKILLS_SCHEMA
    with sqlite3.connect(':memory:') as connection:
        connection.executescript(SCHEMA + SKILLS_SCHEMA)
        return {(kind, name): sql for kind, name, sql in connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index') "
            "AND name NOT LIKE 'sqlite_%'")}

def install(connection: sqlite3.Connection) -> None:
    for table, columns in COLUMNS.items():
        if tuple(row[1] for row in connection.execute(f'PRAGMA table_info({table})')) != columns:
            raise ValueError(f'Incompatible changefeed source {table}')
    connection.execute(META_SQL)
    connection.execute('INSERT INTO analytics_change_meta VALUES(2,?)', (uuid.uuid4().hex,))
    connection.execute(CHANGES_SQL)
    for sql in TRIGGERS.values():
        connection.execute(sql)


def supported(connection: sqlite3.Connection) -> bool:
    """A missing/damaged trigger is not a claim of complete history."""
    try:
        version = connection.execute('SELECT version,identity FROM analytics_change_meta').fetchall()
        if (len(version) != 1 or version[0][0] != 2 or not isinstance(version[0][1], str)
                or len(version[0][1]) != 32 or any(c not in '0123456789abcdef' for c in version[0][1])
                or connection.execute('PRAGMA user_version').fetchone()[0] != 0):
            return False
        for table, columns in COLUMNS.items():
            if tuple(row[1] for row in connection.execute(f'PRAGMA table_info({table})')) != columns:
                return False
        # A trigger's name is not its authority. SQLite can run an additional
        # AFTER trigger on a tracked table (or the journal itself) after our
        # writer and erase its evidence without touching any approved name.
        # Compare the *entire* main-schema inventory, including extra tables,
        # indexes and triggers; do not allow unknown names to opt out.
        actual = {(kind, name): sql for kind, name, sql in connection.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger') AND name NOT LIKE 'sqlite_%'")}
        expected = dict(_source_schema())
        expected.update({('table', 'analytics_changes'): CHANGES_SQL,
                         ('table', 'analytics_change_meta'): META_SQL})
        expected.update({('trigger', name): sql for name, sql in TRIGGERS.items()})
        return actual == expected
    except sqlite3.Error:
        return False


def identity(connection: sqlite3.Connection) -> str | None:
    if not supported(connection):
        return None
    return connection.execute('SELECT identity FROM analytics_change_meta').fetchone()[0]


def watermark(connection: sqlite3.Connection) -> int | None:
    if not supported(connection):
        return None
    return connection.execute('SELECT COALESCE(MAX(revision),0) FROM analytics_changes').fetchone()[0]


def changes(connection: sqlite3.Connection, since: int, *, maximum: int = 128):
    """Called inside a caller-owned read transaction. None means full fallback.

    The first retained revision must follow the client's revision. No skipped
    revision is inferred from events.seq, mtime or a stale renderer hint.
    """
    high = watermark(connection)
    if high is None or not isinstance(since, int) or isinstance(since, bool) or since < 0 or since > high:
        return None
    first = connection.execute('SELECT MIN(revision) FROM analytics_changes').fetchone()[0]
    if since < high and (first is None or since < first - 1):
        return None
    rows = connection.execute('SELECT revision,kind,item_id,old_row,new_row FROM analytics_changes '
                              'WHERE revision>? ORDER BY revision LIMIT ?', (since, maximum + 1)).fetchall()
    if len(rows) > maximum or len(rows) != high - since or any(r[1] == 'oversize' for r in rows):
        return None
    return high, rows
