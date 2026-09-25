"""Read-only fan-in of independently derived local profile reports.

Never concatenate request histories: same-session calculations belong to one
physical ledger. Detail prefixes serve pagination only; totals are SQL rollups.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any

from .storage import summary
from .read_snapshot import snapshot_root, SnapshotBusy, SnapshotUnsupported


def profile_id(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]


def discover(server_home):
    """Enumerate standard local Hermes roots without auth/quota/core imports.

    HERMES_HOME may be a standalone custom home or <base>/profiles/<name>.
    Enumeration failure is evidence, not an empty successful inventory.
    """
    server = Path(server_home).resolve()
    base = server.parent.parent if server.parent.name == 'profiles' else server
    candidates = [('default', base)]
    errors = []
    try:
        folder = base / 'profiles'
        if folder.exists():
            candidates.extend((p.name, p) for p in sorted(folder.iterdir()) if p.is_dir())
    except OSError:
        errors.append({'status': 'unreadable', 'source': 'profile_directory'})
    if not any(p.resolve() == server for _, p in candidates):
        candidates.append((server.name, server))
    rows, roots, databases, physical_paths = [], {}, {}, {}
    for name, path in candidates:
        try:
            root = path.resolve()
            db = (root / 'usage-ledger' / 'events.sqlite3').resolve()
            stat = db.stat() if db.exists() else None
            physical = (stat.st_dev, stat.st_ino) if stat else None
            previous = roots.get(str(root)) or databases.get(db)
            if previous is not None:
                previous['aliases'].append(name)
                previous['is_server'] |= root == server
                continue
            row = {'name': name, 'path': str(root), 'profile_id': profile_id(root),
                   'profile_scope': 'selected', 'is_default': name == 'default',
                   'is_server': root == server, 'aliases': []}
            rows.append(row)
            roots[str(root)] = row
            databases[db] = row
            if physical:
                physical_paths.setdefault(physical, []).append(row)
        except OSError:
            errors.append({'status': 'unreadable', 'source': 'profile_root', 'name': name})
    # SQLite sidecars belong to pathnames, not DB inodes. Distinct hardlinks
    # cannot safely share a snapshot, even if no WAL happens to exist now.
    for linked in physical_paths.values():
        if len(linked) > 1:
            for row in linked:
                row['availability'] = {'status': 'unavailable', 'reason': 'ambiguous_hardlink'}
                errors.append({**row['availability'], 'source': 'profile_database',
                               'name': row['name'], 'profile_id': row['profile_id']})
    return {'profiles': rows, 'discovery': {'status': 'partial' if errors else 'complete', 'errors': errors},
            'scope_options': [{'label': 'All profiles', 'profile_scope': 'all'},
                              *[{'label': r['name'], 'profile': r['name'], 'profile_scope': 'selected',
                                 'profile_id': r['profile_id']} for r in rows]],
            'default_profile_scope': 'selected'}


def public_inventory(inventory):
    """Expose selection metadata, never the internal absolute filesystem roots."""
    fields = ('name', 'profile_id', 'profile_scope', 'is_default', 'is_server', 'aliases', 'availability')
    return {**inventory, 'profiles': [{key: row[key] for key in fields if key in row}
                                    for row in inventory['profiles']]}


def opaque(pid, kind, value):
    body = json.dumps([pid, kind, value], ensure_ascii=False, separators=(',', ':')).encode()
    return 'ap1.' + base64.urlsafe_b64encode(body).decode().rstrip('=')


def decode(value, kind, profiles):
    try:
        if not value.startswith('ap1.'):
            raise ValueError()
        token = value[4:]
        pid, actual_kind, original = json.loads(base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True))
        if actual_kind != kind or pid not in profiles or not isinstance(original, str) or not original:
            raise ValueError()
        if opaque(pid, kind, original) != value:
            raise ValueError()
        return pid, original
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('All-profiles filters require a qualified ' + kind + ' identity.') from None


IDENTITIES = {
    'session_id': 'session', 'root_session_id': 'session', 'parent_session_id': 'session',
    'session_after': 'session', 'subagent_id': 'subagent', 'project_id': 'project',
    'compression_id': 'compression', 'request_id': 'request', 'next_request_id': 'request',
    'previous_request_id': 'request', 'current_request_id': 'request',
    'turn_id': 'turn', 'process': 'process', 'task_id': 'task', 'task': 'task',
    'api_request_id': 'api_request', 'tool_call_id': 'tool_call',
    'skill': 'skill',
}
LIST_IDENTITIES = {'session_lineage': 'session', 'aux_request_ids': 'request', 'retained_skills': 'skill'}


def qualify(value, profile, kind=None, key_field='id', *, nested=False):
    """Retain original identifiers alongside opaque cross-profile identities."""
    if isinstance(value, list):
        return [qualify(v, profile, nested=True) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    originals = {}
    for key, item in value.items():
        identity = kind if key == key_field and kind else IDENTITIES.get(key)
        if identity and item is not None and item != '':
            originals[key] = item
            result[key] = opaque(profile['profile_id'], identity, item)
        elif key in LIST_IDENTITIES and isinstance(item, list):
            originals[key] = item
            result[key] = [opaque(profile['profile_id'], LIST_IDENTITIES[key], v) for v in item]
        else:
            result[key] = qualify(item, profile, nested=True)
    if originals:
        result['original_ids'] = originals
    if originals or not nested:
        result['profile_id'] = profile['profile_id']
        result['profile'] = profile['name']
    return result


def add_values(values: list[Any]) -> Any:
    """Add count/Decimal summary trees; keep descriptive constants unchanged."""
    first = values[0]
    if isinstance(first, dict):
        return {key: add_values([v[key] for v in values if key in v]) for key in dict.fromkeys(k for v in values for k in v)}
    if isinstance(first, bool):
        return first
    if isinstance(first, int):
        return sum(values)
    if isinstance(first, str):
        try:
            return str(sum((Decimal(v) for v in values), Decimal(0)))
        except ArithmeticError:
            return first
    return first


def merge_summaries(values):
    if not values:
        return summary([])
    result = add_values(values)
    known, missing = result['known'], result['missing_fields']
    result['cache_hit_rate'] = (known['cache_read_tokens'] / known['prompt_tokens']
        if known['prompt_tokens'] and not missing['cache_read_tokens'] and not missing['prompt_tokens'] else None)
    return result


def merge_groups(reports, field, dimensions):
    groups = {}
    for report in reports:
        for row in report[field]:
            key = tuple(json.dumps(row.get(d), sort_keys=True) for d in dimensions)
            groups.setdefault(key, []).append(row)
    result = []
    summary_keys = summary([]).keys()
    for key in sorted(groups):
        rows = groups[key]
        result.append({**{d: rows[0].get(d) for d in dimensions},
                       **merge_summaries([{k: r[k] for k in summary_keys} for r in rows])})
    return result


def ordered(rows, timestamp, limit=None, offset=0, *, numeric_id=False):
    # Original IDs must be used for tie-breaking: base64 is not order-preserving.
    # Match each source SQL comparator before merging bounded per-profile prefixes.
    convert = int if numeric_id else str
    rows.sort(key=lambda r: (r.get(timestamp) or 0, convert(r.get('original_ids', {}).get('id', 0 if numeric_id else '')),
                             r.get('profile_id', '')), reverse=True)
    return rows[offset:None if limit is None else offset + limit]


def prepare(inventory, filters):
    profiles = inventory['profiles']
    ids = {p['profile_id'] for p in profiles}
    decoded = {}
    for key, kind in [('session', 'session'), ('project', 'project'), ('subagent', 'subagent'),
                      ('test_id', 'test'), ('skill', 'skill')]:
        if filters.get(key):
            decoded[key] = decode(filters[key], kind, ids)
    target_ids = {pid for pid, _ in decoded.values()}
    if len(target_ids) > 1:
        raise ValueError('Qualified filters belong to different profiles.')
    local = dict(filters)
    local.update({key: raw for key, (_, raw) in decoded.items()})
    selected = [p for p in profiles if not target_ids or p['profile_id'] in target_ids]
    return selected, local


def coverage(inventory, statuses):
    read = sum(s['status'] == 'read' for s in statuses)
    complete = read == len(statuses) and inventory['discovery']['status'] == 'complete'
    return {'status': 'complete' if complete and statuses else ('partial' if read else 'unavailable'),
            'discovery': inventory['discovery'], 'profiles': statuses,
            'read_profiles': read, 'selected_profiles': len(statuses),
            'note': 'Totals cover readable recorded observations only, not unrecorded historical activity. '
                    'Profiles are independent read snapshots, not one atomic cross-profile snapshot.'}


def validate(start, end, offset, limit, maximum, filters):
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)) or start < 0 or end < start:
        raise ValueError('Invalid time window.')
    if offset < 0 or not 1 <= limit <= maximum:
        raise ValueError('Invalid pagination.')
    if filters.get('agent', '') not in ('', 'primary', 'subagent', 'unknown') or filters.get('session_scope', 'exact') not in ('exact', 'family'):
        raise ValueError('Invalid scope filter.')


def quota_unavailable():
    return {'available': False, 'reason': 'aggregate_quota_unavailable',
            'note': 'Subscription quotas may share accounts across profiles and cannot be summed. No quota probes were performed.'}


def ledger(runtime, inventory, *, start: float=0, end=None, offset=0, limit=200, view=None, group=None, **filters):
    from .projection import selected_fields, manifest
    fields = selected_fields(view, group)
    include = lambda name: fields is None or name in fields
    end = time.time() if end is None else end
    validate(start, end, offset, limit, 2000, filters)
    profiles, local = prepare(inventory, filters)
    reports, statuses, ready = [], [], []
    # One generation for the entire fan-in. No Store constructor or worker.
    with runtime.lease() as generation, ExitStack() as snapshots:
        anchor = max(start, filters['bucket_start']) if filters.get('bucket_start') is not None else (start if start else None)
        for profile in profiles:
            status = {k: profile[k] for k in ('profile_id', 'name', 'aliases')}
            statuses.append(status)
            if profile.get('availability', {}).get('status') == 'unavailable':
                status.update(profile['availability'])
                continue
            path = Path(profile['path']) / 'usage-ledger' / 'events.sqlite3'
            try:
                if not path.is_file():
                    status['status'] = 'missing'
                    continue
                copied_root = snapshots.enter_context(snapshot_root(profile['path']))
                reader = runtime._reader(generation, copied_root)
                with reader.db() as c:
                    first = c.execute('SELECT MIN(started) FROM requests WHERE started>=? AND started<?', (start, end)).fetchone()[0]
                if first is not None and start == 0 and filters.get('bucket_start') is None:
                    anchor = min(anchor, first) if anchor is not None else first
                ready.append((profile, status, reader))
            except (sqlite3.Error, OSError) as exc:
                status['status'] = 'unreadable'
                if isinstance(exc, (SnapshotBusy, SnapshotUnsupported)):
                    status['reason'] = exc.reason
        if anchor is None:
            anchor = max(0, end - 86400)
        candidates = []
        for profile, status, reader in ready:
            try:
                lo, hi = start, end
                selected_test = None
                if local.get('test_id'):
                    test_start, test_end, selected_test = reader.test_window(local['test_id'],include_data=True)
                    lo = max(lo, test_start)
                    hi = min(hi, test_end) if test_end is not None else hi
                if local.get('bucket_start') is not None:
                    lo = max(lo, local['bucket_start'])
                    hi = min(hi, local['bucket_end'])
                hi = max(lo, hi)
                scope = (lo, hi, local.get('provider', ''), local.get('session', ''),
                         local.get('agent', ''), local.get('project', ''),
                         local.get('session_scope', 'exact'), local.get('subagent', ''),
                         local.get('model', ''), local.get('model_provider', ''))
                keys = reader._request_keys(*scope, limit=offset + limit) if include('requests') else []
                candidates.append((profile, status, reader, scope, keys, selected_test))
            except (sqlite3.Error, OSError, json.JSONDecodeError):
                status['status'] = 'unreadable'
        # Merge scalar keys only. Never retain/qualify per-profile JSON prefixes.
        # A late read failure changes both totals and winners: retry survivors on
        # these same copied roots and generation, not newly sampled live sources.
        while candidates:
            winners = sorted(((stamp or 0, str(key), p['profile_id'])
                              for p, _, _, _, keys, _ in candidates for stamp, key in keys),
                             reverse=True)[offset:offset + limit]
            selected = {}
            for _, key, pid in winners:
                selected.setdefault(pid, []).append(key)
            reports = []
            failed = set()
            for profile, status, reader, scope, _, selected_test in candidates:
                try:
                    lo, hi, provider, session, agent, project, session_scope, subagent, model, model_provider = scope
                    report = reader.read(lo, hi, provider, 0, limit, session, agent, project,
                        session_scope, subagent, model, model_provider,
                        trend_start=lo if local.get('test_id') or local.get('bucket_start') is not None else anchor,
                        _detail_ids=selected.get(profile['profile_id'], ()) if include('requests') else None,
                        view=view,group=group,_selected_test=selected_test)
                    status['status'] = 'read'
                    reports.append((profile, report))
                except (sqlite3.Error, OSError, json.JSONDecodeError):
                    status['status'] = 'unreadable'
                    failed.add(profile['profile_id'])
            if not failed:
                break
            reports = []
            candidates = [entry for entry in candidates if entry[0]['profile_id'] not in failed]
        revision = generation['revision']
    data = [r for _, r in reports]
    out = {'profile_scope': 'all', 'read_only': True, 'refresh_mode': 'polling',
           'generated_at': time.time(), 'analytics_revision': revision,
           'window': data[0]['window'] if (local.get('test_id') or local.get('bucket_start') is not None) and data else {'start': start, 'end': end},
           'coverage': coverage(inventory, statuses), 'quota': quota_unavailable(), 'quota_observations': [],
           'summary': merge_summaries([r['summary'] for r in data]) if data else None,
           'seq': None, 'profile_sequences': {p['profile_id']: r['seq'] for p, r in reports}}
    for field in ('request_count', 'compression_count', 'crossing_start', 'crossing_end'):
        if include(field):out[field] = sum(r[field] for r in data) if data else None
    out['next_offset'] = offset + limit if out['request_count'] is not None and offset + limit < out['request_count'] else None
    for field, kind, stamp, cap in [('requests', 'request', 'started', limit),
            ('compressions', 'compression', 'started', 1000), ('tests', 'test', 'started', 100),
            ('rates', 'rate', 'created', 500), ('health', None, 'updated', None)]:
        if not include(field):continue
        rows = [qualify(v, p, kind) for p, r in reports for v in r[field]]
        out[field] = ordered(rows, stamp, cap, numeric_id=field == 'rates')
    if local.get('test_id') and include('tests'):
        # Global recency can exclude a marker even if it survived its own
        # profile's prefix. Only the decoded profile's factual marker qualifies.
        for profile, report in reports:
            marker_fact = next((t for t in report['tests'] if t['id'] == local['test_id']), None)
            if marker_fact is not None:
                fact = qualify(marker_fact, profile, 'test')
                if not any(t['id'] == fact['id'] for t in out['tests']):
                    out['tests'].append(fact)
                break
    if include('compression_truncated'):
        out['compression_truncated'] = out['compression_count'] is not None and out['compression_count'] > len(out['compressions'])
    for field, kind, key in [('project_groups', 'project', 'key'), ('session_groups', 'session', 'key'),
            ('subagent_groups', 'subagent', 'key'), ('project_options', 'project', 'id')]:
        if include(field):out[field] = sorted([qualify(v, p, kind, key) for p, r in reports for v in r[field]], key=lambda v: v[key])
    # Global categorical groups have shared semantics, unlike profile-local IDs.
    if include('groups'):
        out['groups'] = sorted([qualify(v, p) for p, r in reports for v in r['groups']],
                               key=lambda v: (v['provider'], v['model'], v['agent_kind'], v['task']))
    for field, dimensions in [('provider_groups', ('provider',)), ('model_groups', ('provider', 'model')),
            ('agent_groups', ('agent_kind',)), ('applied_rate_groups', ('provider', 'model', 'service_tier', 'rate'))]:
        if include(field):out[field] = merge_groups(data, field, dimensions)
    out['subagent_summary'] = merge_summaries([r['subagent_summary'] for r in data]) if data else None
    out['providers'] = sorted({v for r in data for v in r['providers']})
    if include('price_catalogs'):
        out['price_catalogs'] = [qualify(v, p) for p, r in reports for v in r['price_catalogs']]
    out['trend'] = {'unit': None, 'timezone': 'UTC', 'seconds': None, 'buckets': []}
    if data:
        out['trend'] = {k: v for k, v in data[0]['trend'].items() if k != 'buckets'}
        buckets = {}
        for report in data:
            for bucket in report['trend']['buckets']:
                key = (bucket['start'], bucket['end'])
                buckets.setdefault(key, []).append({k: v for k, v in bucket.items() if k not in ('start', 'end')})
        out['trend']['buckets'] = [dict(start=key[0], end=key[1], **merge_summaries(buckets[key])) for key in sorted(buckets)]
    if include('cache_read_progression'):
        progression = [r['cache_read_progression'] for r in data]
        out['cache_read_progression'] = None
        if progression:
            combined = deepcopy(progression[0])
            for field in ('eligible_pairs', 'excluded_pairs'):
                combined[field] = sum(r[field] for r in progression)
            for field in ('positive_read_growth_tokens', 'read_drop_tokens'):
                combined[field] = sum(r[field] or 0 for r in progression) if combined['eligible_pairs'] else None
            combined['excluded_reasons'] = add_values([r['excluded_reasons'] for r in progression])
            out['cache_read_progression'] = combined
    if fields is not None:
        out = {**{key:out[key] for key in fields if key in out},
               **{key:out[key] for key in ('profile_scope', 'read_only', 'refresh_mode', 'analytics_revision', 'coverage', 'quota', 'profile_sequences')},
               'projection':manifest(fields)}
    return out


def skills(inventory, *, start=0, end=None, offset=0, limit=200, aggregate_only=False, **filters):
    from .skills import read, SNAPSHOT_LIMIT
    end = time.time() if end is None else end
    validate(start, end, offset, limit, 200, filters)
    profiles, local = prepare(inventory, filters)
    reports, statuses = [], []
    for profile in profiles:
        status = {k: profile[k] for k in ('profile_id', 'name', 'aliases')}
        statuses.append(status)
        if profile.get('availability', {}).get('status') == 'unavailable':
            status.update(profile['availability'])
            continue
        try:
            if not (Path(profile['path']) / 'usage-ledger' / 'events.sqlite3').is_file():
                status['status'] = 'missing'
                continue
            with snapshot_root(profile['path']) as copied_root:
                report = read(copied_root, start=start, end=end, offset=0, limit=limit,
                              _detail_limit=offset + limit, aggregate_only=aggregate_only, **local)
            recorded = report['coverage']['status'] != 'not_recorded'
            status.update(status='read' if recorded else 'not_recorded', observations=report['coverage'])
            if recorded:
                reports.append((profile, report))
        except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            status['status'] = 'unreadable'
            if isinstance(exc, (SnapshotBusy, SnapshotUnsupported)):
                status['reason'] = exc.reason
    data = [r for _, r in reports]
    out = {'version': 1, 'profile_scope': 'all', 'read_only': True, 'refresh_mode': 'polling',
           'generated_at': time.time(), 'coverage': coverage(inventory, statuses),
           'window': data[0]['window'] if (local.get('test_id') or local.get('bucket_start') is not None) and data else {'start': start, 'end': end},
           'summary': add_values([r['summary'] for r in data]) if data else None,
           'model_options': sorted({v for r in data for v in r['model_options']})}
    for field in ('event_count', 'snapshot_count'):
        out[field] = sum(r[field] for r in data) if data and not aggregate_only else None
    out['next_offset'] = offset + limit if out['event_count'] is not None and offset + limit < out['event_count'] else None
    out['events'] = ordered([qualify(v, p, 'skill_event') for p, r in reports for v in r['events']], 'ts', limit, offset)
    out['snapshots'] = ordered([qualify(v, p, 'skill_event') for p, r in reports for v in r['snapshots']], 'ts', SNAPSHOT_LIMIT)
    out['snapshots_truncated'] = out['snapshot_count'] is not None and out['snapshot_count'] > len(out['snapshots'])
    out['skills'] = sorted([qualify(v, p, 'skill', 'name') for p, r in reports for v in r['skills']],
                            key=lambda v: (-v['loads'], v['original_ids']['name'], v['profile_id']))
    out['catalogue'] = sorted([qualify(v, p, 'skill', 'name') for p, r in reports for v in r['catalogue']],
                              key=lambda v: (-v['exposures'], v['original_ids']['name'], v['profile_id']))
    observed = [r['catalogue_coverage']['since'] for r in data if r['catalogue_coverage']['since'] is not None]
    out['catalogue_coverage'] = {'status': 'partial' if observed and end > min(observed) else 'unavailable',
                                 'since': min(observed) if observed else None}
    return out
