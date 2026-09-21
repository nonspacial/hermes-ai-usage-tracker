"""Explicit, project-only historical maintenance; no Store initialisation/seeding.

Run: python -m ledger_runtime.reconcile_projects --profile-root /absolute/profile
Dry-run is the default. Back up the ledger before opting into --apply.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .attribution import STATE_FIELDS, persist_state, resolve
from .projects import PROJECT_FIELDS, read_projects


def _target(root, relative):
    path = root / relative
    if path.resolve() != path or not path.is_file():
        raise ValueError(f'Expected existing, non-aliased profile file: {path}')
    return path


def reconcile_projects(profile_root, *, apply=False):
    """Reassign project fields only, atomically, using current profile metadata.

    Missing state sessions may use cached parent links, but never cached paths.
    No reachable current session means leave that historical record untouched.
    Missing/corrupt metadata databases fail closed (including in dry-run).
    Events, session context, usage, price snapshots and all SQL columns except
    requests/compressions/skill_events.data remain untouched. No live notifications are sent.
    """
    root = Path(profile_root)
    if not root.is_absolute():
        raise ValueError('profile_root must be absolute')
    root = root.resolve()
    state = _target(root, 'state.db')
    _target(root, 'projects.db')
    ledger = _target(root, 'usage-ledger/events.sqlite3')
    claims = read_projects(root, strict=True)
    with closing(sqlite3.connect(state.as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        # Required metadata columns: a partial schema must not erase attribution.
        rows = [dict(r) for r in source.execute('SELECT ' + ','.join(STATE_FIELDS) + ' FROM sessions')]
    current = {r['id'] for r in rows}
    mode = 'rw' if apply else 'ro'
    with closing(sqlite3.connect(ledger.as_uri() + '?mode=' + mode, uri=True, timeout=2)) as con, \
            closing(sqlite3.connect(':memory:')) as context:
        con.row_factory = sqlite3.Row
        con.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        context.execute('CREATE TABLE session_context(session_id TEXT PRIMARY KEY, updated REAL, data TEXT)')
        for row in con.execute('SELECT session_id,updated,data FROM session_context'):
            data = json.loads(row['data'])
            data.pop('cwd', None)
            data.pop('git_repo_root', None)
            context.execute('INSERT INTO session_context VALUES(?,?,?)',
                            (row['session_id'], row['updated'], json.dumps(data)))
        for row in rows:
            persist_state(context, row)
        tables: dict[str, dict[str, int]] = {}
        resolved = {}
        report = dict(profile_root=str(root), ledger=str(ledger), applied=bool(apply), tables=tables)
        try:
            targets: list[tuple[str, tuple[str, ...]]] = [('requests', PROJECT_FIELDS), ('compressions', PROJECT_FIELDS)]
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_events'").fetchone():
                targets.append(('skill_events', ('project_id',)))
            for table, fields in targets:
                counts = dict(scanned=0, changed=0, skipped=0)
                for row in con.execute(f'SELECT id,session_id,data FROM {table}').fetchall():
                    counts['scanned'] += 1
                    data = json.loads(row['data'])
                    sid = row['session_id']
                    key = (sid, data.get('platform', ''))
                    if key not in resolved:
                        resolved[key] = resolve(context, sid, key[1], claims)
                    ctx = resolved[key]
                    if not ({sid, *ctx['session_lineage']} & current):
                        counts['skipped'] += 1
                        continue
                    if all(data.get(k) == ctx[k] for k in fields):
                        continue
                    counts['changed'] += 1
                    if apply:
                        # SQLite edits just these JSON members, retaining large
                        # integers and decimal spellings in untouched usage data.
                        args = [v for k in fields for v in ('$.' + k, ctx[k])]
                        placeholders = ','.join('?' for _ in args)
                        con.execute(f'UPDATE {table} SET data=json_set(data,{placeholders}) WHERE id=?',
                                    args + [row['id']])
                tables[table] = counts
            if apply:
                con.commit()
            else:
                con.rollback()
        except BaseException:
            con.rollback()
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile-root', required=True, help='Exact absolute Hermes profile directory')
    parser.add_argument('--apply', action='store_true', help='Apply after an operator-created ledger backup')
    args = parser.parse_args()
    print(json.dumps(reconcile_projects(args.profile_root, apply=args.apply), indent=2))


if __name__ == '__main__':
    main()
