"""All-profiles reads use disposable SQLite fixtures only; never a real home."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from _hermes_ai_usage_ledger_v2 import aggregate, api, skills
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
from _hermes_ai_usage_ledger_v2.storage import SCHEMA


@pytest.fixture
def runtime():
    value = AnalyticsRuntime()
    yield value
    value._discard(value.current)


def database(root, rows=(), events=()):
    folder = root / 'usage-ledger'
    folder.mkdir(parents=True)
    path = folder / 'events.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        c.executescript(skills.SCHEMA)
        for row in rows:
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (row['id'], row['started'], row['ended'], row['provider'], row['model'],
                       row['session_id'], row['task'], row.get('compression_id'), row['status'], json.dumps(row)))
        for row in events:
            c.execute('INSERT INTO skill_events VALUES(?,?,?,?,?,?,?,?)',
                      (row['id'], row['ts'], row['kind'], row['session_id'], row['provider'], row['model'], row.get('skill'), json.dumps(row)))
    return path


def request(key='same', ts=1, reads=10, prompt=100, cost='0.1', **kw):
    return dict(id=key, started=ts, ended=ts + .1, provider='fixture', model='fixture',
                session_id='same-session', project_id='same-project', root_session_id='same-session',
                task='same-task', process='same-process', source='main_hook', agent_kind='primary',
                status='completed', usage={'prompt_tokens': prompt, 'cache_read_tokens': reads,
                    'cache_write_tokens': 0, 'total_tokens': prompt, 'usage_source': 'native'},
                cost={'known_components_usd': cost, 'complete': False, 'components': {}}, **kw)


def event(key='same', ts=1, tokens=0, kind='skill_load'):
    return dict(id=key, ts=ts, kind=kind, session_id='same-session', provider='fixture', model='fixture',
                skill='same-skill', success=True, is_reference=False, repeat=False, estimated_tokens=tokens,
                project_id='same-project', retained_skills=['same-skill'], request_id='same', context_used=None)


def inventory(root):
    return aggregate.discover(root)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_collision_sums_weighted_ratio_global_pages_and_originals(tmp_path, runtime):
    database(tmp_path, [request(ts=1, reads=10, prompt=100)])
    database(tmp_path / 'profiles' / 'other', [request(ts=2, reads=90, prompt=300, cost='0.2')])
    before = snapshot(tmp_path)
    out = aggregate.ledger(runtime, inventory(tmp_path), end=5, limit=1)
    assert out['summary']['attempts'] == out['request_count'] == 2
    assert out['summary']['sessions'] == 2
    assert out['summary']['known_cost_usd'] == '0.3'
    assert out['summary']['cache_hit_rate'] == .25
    assert out['provider_groups'][0]['attempts'] == 2
    assert out['provider_groups'][0]['known_cost_usd'] == '0.3'
    assert out['next_offset'] == 1 and len(out['requests']) == 1
    second = aggregate.ledger(runtime, inventory(tmp_path), end=5, offset=1, limit=1)
    rows = out['requests'] + second['requests']
    assert [r['started'] for r in rows] == [2, 1]
    for field in ('id', 'session_id', 'project_id', 'task', 'process'):
        assert rows[0][field] != rows[1][field]
        assert rows[0]['original_ids'][field] == rows[1]['original_ids'][field]
    assert second['summary'] == out['summary'] and second['next_offset'] is None
    assert len(out['session_groups']) == len(out['project_groups']) == len(out['project_options']) == 2
    assert len(out['groups']) == 2  # Task IDs are profile-local, not shared labels.
    selected = aggregate.ledger(runtime, inventory(tmp_path), end=5, session=rows[0]['session_id'])
    assert selected['request_count'] == 1 and selected['requests'][0]['profile'] == 'other'
    assert snapshot(tmp_path) == before


def test_derivations_are_per_root_even_with_identical_ids(tmp_path, runtime):
    database(tmp_path, [request(ts=1, reads=10)])
    database(tmp_path / 'profiles' / 'other', [request(ts=2, reads=100)])
    out = aggregate.ledger(runtime, inventory(tmp_path), end=5)
    assert out['summary']['session_cache_writes']['tokens'] == 0
    assert out['summary']['session_cache_writes']['baseline_requests'] == 2
    assert out['cache_read_progression']['eligible_pairs'] == 0
    assert out['cache_read_progression']['positive_read_growth_tokens'] is None
    assert all(r['calculated_cache_writes']['previous_request_id'] is None for r in out['requests'])
    # Add a real within-root predecessor: only its 5-token delta is admitted.
    with sqlite3.connect(tmp_path / 'usage-ledger' / 'events.sqlite3') as c:
        row = request('later', ts=3, reads=15)
        c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (row['id'], 3, 3.1, 'fixture', 'fixture', 'same-session', 'same-task', None, 'completed', json.dumps(row)))
    out = aggregate.ledger(runtime, inventory(tmp_path), start=2, end=5)
    assert out['summary']['session_cache_writes']['tokens'] == 5
    assert out['cache_read_progression']['positive_read_growth_tokens'] == 5
    later = next(r for r in out['requests'] if r['original_ids']['id'] == 'later')
    assert later['calculated_cache_writes']['previous_request_id'] == aggregate.opaque(later['profile_id'], 'request', 'same')


def test_missing_unreadable_and_unknown_are_not_zero(tmp_path, runtime):
    database(tmp_path, [request(reads=None)])
    (tmp_path / 'profiles' / 'missing').mkdir(parents=True)
    broken = tmp_path / 'profiles' / 'broken' / 'usage-ledger'
    broken.mkdir(parents=True)
    (broken / 'events.sqlite3').write_bytes(b'not a database')
    before = snapshot(tmp_path)
    out = aggregate.ledger(runtime, inventory(tmp_path), end=5)
    assert out['coverage']['status'] == 'partial'
    assert {r['status'] for r in out['coverage']['profiles']} == {'read', 'missing', 'unreadable'}
    assert out['summary']['known']['cache_read_tokens'] == 0
    assert out['summary']['missing_fields']['cache_read_tokens'] == 1
    assert out['summary']['cache_hit_rate'] is None
    assert snapshot(tmp_path) == before
    empty = tmp_path / 'empty-root'
    absent = aggregate.ledger(runtime, inventory(empty), end=5)
    assert absent['summary'] is None and absent['request_count'] is None
    assert absent['coverage']['status'] == 'unavailable'
    assert not empty.exists()
    real_empty = tmp_path / 'real-empty'
    database(real_empty)
    known = aggregate.ledger(runtime, inventory(real_empty), end=5)
    assert known['summary']['attempts'] == 0 and known['coverage']['status'] == 'complete'


def test_all_time_trends_align_across_different_profile_history(tmp_path, runtime):
    database(tmp_path, [request(ts=1)])
    database(tmp_path / 'profiles' / 'recent', [request(ts=999999)])
    out = aggregate.ledger(runtime, inventory(tmp_path), end=1000000)
    assert out['trend']['seconds'] == 86400
    buckets = out['trend']['buckets']
    assert sum(b['attempts'] for b in buckets) == 2
    assert buckets[0]['start'] == 1 and buckets[-1]['end'] == 1000000
    assert all(a['end'] == b['start'] for a, b in zip(buckets, buckets[1:]))
    assert sum(b['known']['prompt_tokens'] for b in buckets) == out['summary']['known']['prompt_tokens']


def test_deduplicate_symlink_roots_but_reject_hardlinked_databases(tmp_path, runtime):
    path = database(tmp_path, [request()], [event()])
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    (profiles / 'alias').symlink_to(tmp_path, target_is_directory=True)
    inv = inventory(tmp_path)
    assert len(inv['profiles']) == 1
    assert aggregate.ledger(runtime, inv, end=5)['request_count'] == 1
    duplicate = profiles / 'hardlink' / 'usage-ledger'
    duplicate.mkdir(parents=True)
    os.link(path, duplicate / 'events.sqlite3')
    before = snapshot(tmp_path)
    inv = inventory(tmp_path)
    assert len(inv['profiles']) == 2
    assert inv['profiles'][0]['aliases'] == ['alias']
    assert inv['scope_options'][0]['profile_scope'] == 'all'
    assert inv['default_profile_scope'] == 'selected'
    for out in (aggregate.ledger(runtime, inv, end=5), aggregate.skills(inv, end=5)):
        assert out['coverage']['status'] == 'unavailable'
        assert out['summary'] is None
        assert all(p['reason'] == 'ambiguous_hardlink' for p in out['coverage']['profiles'])
    assert snapshot(tmp_path) == before


def test_skills_collisions_paging_snapshots_and_unknown(tmp_path, monkeypatch):
    database(tmp_path, events=[event(ts=1, tokens=None), event('snap', 5, kind='context_snapshot')])
    database(tmp_path / 'profiles' / 'other', events=[event(ts=2, tokens=0), event('snap', 4, kind='context_snapshot')])
    monkeypatch.setattr(skills, 'SNAPSHOT_LIMIT', 1)
    before = snapshot(tmp_path)
    out = aggregate.skills(inventory(tmp_path), end=10, limit=1)
    assert out['summary'] == {'loads': 2, 'references': 0, 'failures': 0, 'sessions': 2}
    assert out['event_count'] == 4 and out['next_offset'] == 1
    assert out['events'][0]['ts'] == 5
    assert out['snapshot_count'] == 2 and out['snapshots_truncated']
    assert len(out['snapshots']) == 1 and out['snapshots'][0]['ts'] == 5
    assert {g['estimated_tokens'] for g in out['skills']} == {None, 0}
    assert len({g['name'] for g in out['skills']}) == 2
    events = [aggregate.skills(inventory(tmp_path), end=10, limit=1, offset=i)['events'][0] for i in range(4)]
    assert [e['ts'] for e in events] == [5, 4, 2, 1]
    assert len({e['id'] for e in events}) == 4
    filtered = aggregate.skills(inventory(tmp_path), end=10, skill=out['skills'][0]['name'])
    assert filtered['event_count'] == 2
    assert snapshot(tmp_path) == before


def test_identity_validation_and_equal_timestamp_pages(tmp_path, runtime):
    database(tmp_path, [request('a'), request('z')])
    database(tmp_path / 'profiles' / 'other', [request('a'), request('z')])
    inv = inventory(tmp_path)
    rows = [aggregate.ledger(runtime, inv, end=5, limit=1, offset=i)['requests'][0] for i in range(4)]
    assert [r['original_ids']['id'] for r in rows] == ['z', 'z', 'a', 'a']
    assert len({r['id'] for r in rows}) == 4
    with pytest.raises(ValueError, match='qualified'):
        aggregate.ledger(runtime, inv, end=5, session='same-session')
    with pytest.raises(ValueError, match='qualified'):
        aggregate.ledger(runtime, inv, end=5, session=rows[0]['id'])
    with pytest.raises(ValueError, match='different profiles'):
        aggregate.ledger(runtime, inv, end=5, session=rows[0]['session_id'], project=rows[1]['project_id'])


def test_marker_filters_and_large_skills_offset(tmp_path, runtime):
    database(tmp_path, [request(ts=1), request('late', ts=3)],
             [event(str(i), i + 1) for i in range(250)])
    database(tmp_path / 'profiles' / 'other', [request(ts=2)],
             [event(str(i), i + 1) for i in range(250)])
    marker = {'id': 'test', 'started': 2, 'ended': 5, 'label': 'fixture'}
    with sqlite3.connect(tmp_path / 'usage-ledger' / 'events.sqlite3') as c:
        c.execute('INSERT INTO tests VALUES(?,?,?,?,?)', ('test', 2, 5, 'fixture', json.dumps(marker)))
    inv = inventory(tmp_path)
    out = aggregate.ledger(runtime, inv, end=10)
    selected = aggregate.ledger(runtime, inv, end=10, test_id=out['tests'][0]['id'])
    assert selected['request_count'] == 1
    assert selected['requests'][0]['original_ids']['id'] == 'late'
    assert selected['window']['start'] == 2 and selected['window']['end'] == 5
    paged = aggregate.skills(inv, end=1000, offset=400, limit=2)
    assert paged['event_count'] == paged['summary']['loads'] == 500
    assert [r['ts'] for r in paged['events']] == [50, 50]
    assert paged['next_offset'] == 402


def test_discovery_failure_is_explicit(tmp_path, monkeypatch):
    (tmp_path / 'profiles').mkdir()
    original = Path.iterdir
    def denied(path):
        if path == tmp_path / 'profiles':
            raise PermissionError('private path must not leak')
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', denied)
    inv = inventory(tmp_path)
    assert inv['discovery']['status'] == 'partial'
    assert inv['discovery']['errors'] == [{'status': 'unreadable', 'source': 'profile_directory'}]
    assert 'private path' not in json.dumps(inv)


def test_zero_time_and_empty_window(tmp_path, runtime):
    database(tmp_path, [request(ts=0)])
    out = aggregate.ledger(runtime, inventory(tmp_path), end=1)
    assert out['request_count'] == 1 and out['trend']['buckets'][0]['start'] == 0
    assert out['trend']['buckets'][0]['attempts'] == 1
    assert aggregate.ledger(runtime, inventory(tmp_path), end=0)['request_count'] == 0


def test_wal_reads_do_not_create_sidecars(tmp_path, runtime):
    path = database(tmp_path, [request()])
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA journal_mode=WAL')
    c.close()
    before = snapshot(tmp_path)
    out = aggregate.ledger(runtime, inventory(tmp_path), end=5)
    assert out['request_count'] == 1
    assert snapshot(tmp_path) == before
    # Uncheckpointed committed rows must not disappear (immutable=1 would lose them).
    writer = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        row = request('wal-row', ts=2)
        writer.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (row['id'], 2, 2.1, 'fixture', 'fixture', 'same-session', 'same-task', None, 'completed', json.dumps(row)))
        observed = event()
        writer.execute('INSERT INTO skill_events VALUES(?,?,?,?,?,?,?,?)',
                       ('same', 1, 'skill_load', 'same-session', 'fixture', 'fixture', 'same-skill', json.dumps(observed)))
        writer.commit()
        before = snapshot(tmp_path)
        assert aggregate.ledger(runtime, inventory(tmp_path), end=5)['request_count'] == 2
        assert aggregate.skills(inventory(tmp_path), end=5)['summary']['loads'] == 1
        assert snapshot(tmp_path) == before
    finally:
        writer.close()


def test_busy_snapshot_is_partial_not_a_false_zero(tmp_path, runtime, monkeypatch):
    from _hermes_ai_usage_ledger_v2 import read_snapshot
    database(tmp_path, [request()])
    database(tmp_path / 'profiles' / 'other', [request()])
    original = read_snapshot.signature
    counter = 0
    def changing(path):
        nonlocal counter
        value = original(path)
        if path == tmp_path / 'usage-ledger' / 'events.sqlite3':
            counter += 1
            return (*value[:-1], counter)
        return value
    monkeypatch.setattr(read_snapshot, 'signature', changing)
    out = aggregate.ledger(runtime, inventory(tmp_path), end=5)
    assert out['coverage']['status'] == 'partial'
    assert out['request_count'] == 1
    assert {r['status'] for r in out['coverage']['profiles']} == {'read', 'unreadable'}


def test_routes_no_writers_socket_or_quota_and_selected_default(tmp_path, runtime, monkeypatch):
    database(tmp_path, [request()])
    database(tmp_path / 'profiles' / 'other', [request(ts=2)])
    monkeypatch.setattr(api, 'AnalyticsRuntime', lambda: runtime)
    def prohibited(*args, **kwargs):
        pytest.fail('aggregate reached a writer, worker, quota probe or socket')
    monkeypatch.setattr(api, 'Store', prohibited)
    monkeypatch.setattr(api, 'start_worker', prohibited)
    router = APIRouter()
    api.add_routes(router, lambda p: (tmp_path, p, None), lambda: tmp_path)
    app = FastAPI()
    app.include_router(router)
    before = snapshot(tmp_path)
    with TestClient(app) as client:
        assert client.get('/ledger?end=5').json()['request_count'] == 1
        assert client.get('/ledger?profile_scope=all&end=5').json()['request_count'] == 2
        assert client.get('/ledger/skills?profile_scope=all&end=5').status_code == 200
        public = client.get('/ledger/profiles').json()
        assert public['scope_options'][0]['profile_scope'] == 'all'
        assert all('path' not in row for row in public['profiles'])
        assert str(tmp_path) not in json.dumps(public)
        assert all('path' in row for row in inventory(tmp_path)['profiles'])
        for path in ('/ledger/tests', '/ledger/rates', '/ledger/pricing/refresh', '/ledger/analytics/reload'):
            assert client.post(path + '?profile_scope=all', json={'action': 'start'}).status_code == 409
        # Patch socket creation only during the websocket handler, not TestClient setup.
        with monkeypatch.context() as patcher:
            patcher.setattr(api.socket, 'socket', prohibited)
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect('/ledger/events?profile_scope=all'):
                    pass
            assert exc.value.code == 1008
        for query in ('profile_scope=invalid', 'profile_scope=all&start=nan', 'profile_scope=all&end=inf',
                      'profile_scope=all&offset=-1', 'profile_scope=all&session=raw'):
            assert client.get('/ledger?' + query).status_code == 400
    path = Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py'
    spec = importlib.util.spec_from_file_location('fixture_aggregate_dashboard', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_build_payload', prohibited)
    monkeypatch.setattr(module, '_resolve_profile', prohibited)
    monkeypatch.setattr(module, '_ledger_store', prohibited)
    result = module.get_usage(profile_scope='all')
    assert result['quota']['available'] is False and result['providers'] == []
    assert snapshot(tmp_path) == before
