"""Cheap, read-only filesystem change hints for dashboard report consumers.

A token is deliberately not a SQLite revision or an atomic snapshot: stat
metadata can race writes or be masked by a filesystem. Consumers compare it to
avoid *unnecessary* full report reads, and still reconcile periodically.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

from . import aggregate


def _signature(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_mode)


def _source(row):
    """Only stat the same canonical SQLite files that snapshot_root reads."""
    try:
        db = (Path(row['path']) / 'usage-ledger' / 'events.sqlite3').resolve()
        parts = [_signature(path) for path in (db, Path(str(db) + '-wal'),
                                                Path(str(db) + '-journal'))]
    except OSError:
        return {'status': 'unavailable', 'reason': 'metadata_unreadable'}, ('unreadable',)
    if row.get('availability', {}).get('status') == 'unavailable':
        state = row['availability']
    elif parts[0] is None:
        state = {'status': 'unavailable', 'reason': 'missing_database'}
    elif any(part is not None and not stat.S_ISREG(part[5]) for part in parts):
        state = {'status': 'unavailable', 'reason': 'invalid_source_type'}
    elif parts[2] is not None and parts[2][2] > 0:
        state = {'status': 'unavailable', 'reason': 'unsupported_rollback_journal'}
    else:
        state = {'status': 'available'}
    # Canonical path is used only inside the digest: no filesystem paths in DTOs.
    return state, (str(db), parts)


def check(inventory, *, selected_root=None):
    """Return a filter-independent hint and explicit inventory/source coverage.

    Inventory is discovered afresh, not inferred from the last ledger report.
    No SQLite connection, snapshot copy, Store constructor or quota probe occurs.
    """
    if selected_root is None:
        rows = inventory['profiles']
        scope = 'all'
    else:
        scope = 'selected'
        root = Path(selected_root).resolve()
        # Discovery deduplicates by canonical database as well as by root.
        # Resolve a selected DB-file alias to that same inventory identity so
        # aliases and hardlink ambiguity are not lost to a synthetic profile.
        rows = [row for row in inventory['profiles'] if Path(row['path']) == root]
        if not rows:
            database = (root / 'usage-ledger' / 'events.sqlite3').resolve()
            rows = [row for row in inventory['profiles'] if
                    (Path(row['path']) / 'usage-ledger' / 'events.sqlite3').resolve() == database]
        if not rows:
            rows = [{'path': str(root), 'profile_id': aggregate.profile_id(root),
                     'name': root.name, 'aliases': []}]
    statuses, fingerprints = [], []
    for row in rows:
        state, signature = _source(row)
        public = {'profile_id': row['profile_id'], 'name': row['name'],
                  'aliases': row['aliases'], **state}
        statuses.append(public)
        fingerprints.append((row['profile_id'], row['name'], row['aliases'], state, signature))
    discovery = inventory['discovery'] if scope == 'all' else {'status': 'complete', 'errors': []}
    available = sum(row['status'] == 'available' for row in statuses)
    coverage = {'status': ('complete' if statuses and available == len(statuses) and
                           discovery['status'] == 'complete' else
                           'partial' if available else 'unavailable'),
                'discovery': discovery, 'profiles': statuses}
    # Include inventory/coverage even for unavailable sources so appearance,
    # disappearance, aliasing, and recovery change the hint.
    fingerprint = (scope, discovery, fingerprints)
    token = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'version': 1, 'profile_scope': scope, 'read_only': True,
            'change_token': token, 'token_kind': 'opaque-filesystem-hint',
            'coverage': coverage,
            'capabilities': {'change_check': True, 'aggregate_events': False,
                             'selected_events': 'negotiated-on-connect'}}
