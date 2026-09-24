"""Disposable-ledger identity filters; no host database or producer is opened."""
import json
import sqlite3

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from _hermes_ai_usage_ledger_v2 import aggregate, skills
from _hermes_ai_usage_ledger_v2.api import add_routes
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
from _hermes_ai_usage_ledger_v2.storage import SCHEMA


def fixture(root, requests, compressions, observations, marker=(10, 40)):
    folder = root / 'usage-ledger'
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / 'events.sqlite3') as conn:
        conn.executescript(SCHEMA)
        conn.executescript(skills.SCHEMA)
        for item in requests:
            conn.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (item['id'], item['started'], item['ended'], item['provider'], item['model'],
                          item['session_id'], item['task'], None, 'completed', json.dumps(item)))
        for item in compressions:
            conn.execute('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?)',
                         (item['id'], item['started'], item['ended'], item['provider'],
                          item['session_id'], item['session_id'], None, json.dumps(item)))
        for item in observations:
            conn.execute('INSERT INTO skill_events VALUES(?,?,?,?,?,?,?,?)',
                         (item['id'], item['ts'], item['kind'], item['session_id'], item['provider'],
                          item['model'], item.get('skill'), json.dumps(item)))
        conn.execute('INSERT INTO tests VALUES(?,?,?,?,?)',
                     ('window', *marker, 'window', json.dumps({'id': 'window', 'started': marker[0], 'ended': marker[1]})))


def request(key, stamp, provider='alpha', model='requested', response_model='', project='p',
            session='s', agent='primary', subagent=''):
    return dict(id=key, started=stamp, ended=stamp + .1, provider=provider, model=model,
                response_model=response_model, project_id=project, session_id=session,
                root_session_id=session, agent_kind=agent, subagent_id=subagent, task='task',
                status='completed', usage={'total_tokens': 12, 'prompt_tokens': 10,
                                           'input_tokens': 10, 'output_tokens': 2, 'cache_read_tokens': 0})


def event(key, stamp, provider='alpha', model='returned', **extra):
    return dict(id=key, ts=stamp, kind='skill_load', session_id='s', provider=provider,
                model=model, skill='example', success=True, is_reference=False, repeat=False,
                estimated_tokens=3, project_id='p', agent_kind='primary', **extra)


@pytest.fixture
def reports(tmp_path):
    primary = [request('left', 10, response_model='returned'),
               request('right', 20, provider='beta', model='returned'),
               request('requested-only', 25, model='returned', response_model='different'),
               request('second', 30, response_model='returned', session='child',
                       agent='subagent', subagent='child'),
               request('edge', 40, response_model='returned')]
    compressions = [dict(id='known', started=15, ended=16, provider='alpha', model='returned',
                         session_id='s'),
                    dict(id='unknown', started=16, ended=17, provider='alpha', session_id='s'),
                    dict(id='wrong-provider', started=17, ended=18, provider='beta', model='returned',
                         session_id='s')]
    observations = [event('known', 12), event('unknown', 13, model='unknown'),
                    event('wrong-provider', 14, provider='beta'), event('edge', 40)]
    fixture(tmp_path, primary, compressions, observations)
    other = tmp_path / 'profiles' / 'other'
    fixture(other, [request('duplicate', 18, response_model='returned'),
                    request('collision', 19, provider='beta', model='returned')],
            [dict(id='other-comp', started=18, ended=19, provider='alpha', model='returned', session_id='s')],
            [event('other-event', 18)])
    runtime = AnalyticsRuntime()
    try:
        yield tmp_path, other, runtime
    finally:
        runtime._discard(runtime.current)


def test_selected_model_provider_effective_response_and_own_event_models(reports):
    root, _, runtime = reports
    result = runtime.read(root, 0, 40, '', 0, 2, '', '', '', 'exact', '',
                          'returned', 'alpha')
    assert [r['id'] for r in result['requests']] == ['second', 'left']
    assert result['request_count'] == result['summary']['attempts'] == 2
    assert result['next_offset'] is None
    assert [(r['provider'], r['model']) for r in result['model_groups']] == [('alpha', 'returned')]
    assert {(g['provider'], g['model']) for g in result['groups']} == {('alpha', 'returned')}
    assert sum(g['attempts'] for g in result['groups']) == result['request_count']
    assert sum(b['attempts'] for b in result['trend']['buckets']) == 2
    assert result['provider_groups'][0]['attempts'] == 2
    assert [r['id'] for r in result['compressions']] == ['known']
    assert result['compression_count'] == 1
    filtered = skills.read(root, start=0, end=40, model='returned', model_provider='alpha')
    assert [e['id'] for e in filtered['events']] == ['known']
    assert filtered['summary']['loads'] == 1 and filtered['event_count'] == 1
    # A request's requested model is not a substitute for its returned model;
    # an unrecorded compression model is not inferred from its session.
    assert runtime.read(root, 0, 40, '', 0, 200, '', '', '', 'exact', '',
                        'different', 'alpha')['requests'][0]['id'] == 'requested-only'

def test_models_tasks_uses_response_identity_across_providers_and_requested_collisions(reports):
    root, _, runtime = reports
    result = runtime.read(root, 0, 40)
    groups = {}
    for g in result['groups']:
        identity = g['provider'], g['model']
        groups[identity] = groups.get(identity, 0) + g['attempts']
    assert groups == {(g['provider'], g['model']): g['attempts'] for g in result['model_groups']}
    assert groups == {('alpha', 'returned'): 2, ('alpha', 'different'): 1,
                      ('beta', 'returned'): 1}
    for provider, model in groups:
        narrowed = runtime.read(root, 0, 40, '', 0, 200, '', '', '', 'exact', '',
                                model, provider)
        assert narrowed['request_count'] == groups[(provider, model)]
        assert {(g['provider'], g['model']) for g in narrowed['groups']} == {(provider, model)}


def test_marker_bucket_intersection_half_open_and_all_profiles(reports):
    root, _, runtime = reports
    route = APIRouter()
    add_routes(route, lambda _: (root, 'default', None), lambda: root)
    app = FastAPI()
    app.include_router(route)
    with TestClient(app) as client:
        params = dict(start=0, end=50, test_id='window', bucket_start=10, bucket_end=20,
                      model='returned', model_provider='alpha')
        selected = client.get('/ledger', params=params)
        assert selected.status_code == 200, selected.text
        report = selected.json()
        assert report['window']['start'] == 10 and report['window']['end'] == 20
        assert [r['id'] for r in report['requests']] == ['left']
        assert report['summary']['attempts'] == 1
        assert client.get('/ledger/skills', params=params).json()['event_count'] == 1
        assert client.get('/ledger', params={**params, 'bucket_start': 40,
                                             'bucket_end': 41}).json()['request_count'] == 0
        assert client.get('/ledger', params={**params, 'bucket_end': 9}).status_code == 400
        assert client.get('/ledger', params={key: value for key, value in params.items() if key != 'bucket_end'}).status_code == 400
        # Saved marker limits cannot be widened by a later bucket or custom bound.
        empty = client.get('/ledger', params={**params, 'bucket_start': 40,
                                               'bucket_end': 41}).json()
        assert empty['window']['start'] == empty['window']['end'] == 40
    inventory = aggregate.discover(root)
    results = aggregate.ledger(runtime, inventory, start=0, end=50,
                               bucket_start=10, bucket_end=20,
                               model='returned', model_provider='alpha', limit=1)
    assert results['request_count'] == 2 and results['next_offset'] == 1
    assert {r['profile'] for r in results['requests']} == {'other'}
    assert results['summary']['attempts'] == 2
    assert results['compression_count'] == 2
    assert sum(b['attempts'] for b in results['trend']['buckets']) == 2
    page2 = aggregate.ledger(runtime, inventory, start=0, end=50,
                             bucket_start=10, bucket_end=20,
                             model='returned', model_provider='alpha', limit=1, offset=1)
    assert {r['profile'] for r in page2['requests']} == {'default'}
    assert page2['next_offset'] is None
    observations = aggregate.skills(inventory, start=0, end=50, bucket_start=10,
                                    bucket_end=20, model='returned', model_provider='alpha')
    assert observations['event_count'] == 2
    assert {e['profile'] for e in observations['events']} == {'default', 'other'}
    # Qualified aggregate project/session/subagent IDs preserve source ownership.
    for kind, key in [('project', results['project_groups'][0]['key']),
                      ('session', results['session_groups'][0]['key'])]:
        narrowed = aggregate.ledger(runtime, inventory, start=0, end=50,
                                    bucket_start=10, bucket_end=20, model='returned',
                                    model_provider='alpha', **{kind: key})
        assert narrowed['request_count'] == 1
    assert aggregate.ledger(runtime, inventory, start=0, end=50,
                            bucket_start=20, bucket_end=30, model='returned',
                            model_provider='alpha')['request_count'] == 0

def test_marker_fractional_boundaries_survive_aligned_zoom_and_manual_window(tmp_path):
    marker = (1800 + 73, 5400 - 71)
    fixture(tmp_path, [request('before', 1800 + 1),
                       request('inside', 3600), request('after', 5400 - 50)],
            [], [], marker=marker)
    route = APIRouter()
    add_routes(route, lambda _: (tmp_path, 'default', None), lambda: tmp_path)
    app = FastAPI()
    app.include_router(route)
    with TestClient(app) as client:
        aligned = dict(start=1800, end=5400)
        scoped = client.get('/ledger', params={**aligned, 'test_id': 'window'})
        assert scoped.status_code == 200, scoped.text
        assert (scoped.json()['window']['start'], scoped.json()['window']['end']) == marker
        assert [r['id'] for r in scoped.json()['requests']] == ['inside']
        zoomed = client.get('/ledger', params={**aligned, 'test_id': 'window',
                                               'bucket_start': 3600, 'bucket_end': 5400})
        assert zoomed.status_code == 200, zoomed.text
        assert zoomed.json()['window']['end'] == marker[1]
        assert [r['id'] for r in zoomed.json()['requests']] == ['inside']
        assert {r['id'] for r in client.get('/ledger', params=aligned).json()['requests']} == {
            'before', 'inside', 'after'}


def test_old_selected_marker_fact_survives_recent_cap_without_cross_profile_leak(tmp_path):
    fixture(tmp_path, [request('within', 20)], [], [], marker=(10.25, 40.75))
    other = tmp_path / 'profiles' / 'other'
    fixture(other, [request('other-only', 75)], [], [], marker=(70.25, 90.75))
    for root in (tmp_path, other):
        with sqlite3.connect(root / 'usage-ledger' / 'events.sqlite3') as conn:
            for n in range(101):
                key = f'new-{n:03}'
                data = {'id': key, 'started': 100 + n, 'ended': 101 + n, 'label': key}
                conn.execute('INSERT INTO tests VALUES(?,?,?,?,?)',
                             (key, data['started'], data['ended'], key, json.dumps(data)))
    def resolve(name):
        return (other if name == 'other' else tmp_path, name or 'default', None)
    router = APIRouter()
    add_routes(router, resolve, lambda: tmp_path)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        base = dict(start=0, end=250, view='overview', group='time')
        plain = client.get('/ledger', params=base)
        assert plain.status_code == 200
        assert [t['id'] for t in plain.json()['tests']] == [f'new-{n:03}' for n in range(100, 0, -1)]
        legacy = client.get('/ledger', params={'start': 0, 'end': 250})
        assert legacy.status_code == 200
        assert legacy.json()['tests'] == plain.json()['tests']
        scoped = client.get('/ledger', params={**base, 'test_id': 'window'})
        assert scoped.status_code == 200, scoped.text
        data = scoped.json()
        assert (data['window']['start'], data['window']['end']) == (10.25, 40.75)
        assert data['summary']['attempts'] == 1
        assert data['tests'][:-1] == plain.json()['tests']
        assert data['tests'][-1] == {'id': 'window', 'started': 10.25, 'ended': 40.75}
        assert len(data['tests']) == len({t['id'] for t in data['tests']}) == 101
        # A recent marker is not appended twice; an unknown marker is refused.
        recent = client.get('/ledger', params={**base, 'test_id': 'new-100'}).json()
        assert recent['tests'] == plain.json()['tests']
        assert client.get('/ledger', params={**base, 'test_id': 'missing'}).status_code == 400

        inventory = aggregate.discover(tmp_path)
        pid = next(p['profile_id'] for p in inventory['profiles'] if p['name'] == 'default')
        qualified = aggregate.opaque(pid, 'test', 'window')
        all_scoped = client.get('/ledger', params={**base, 'profile_scope': 'all', 'test_id': qualified})
        assert all_scoped.status_code == 200, all_scoped.text
        all_data = all_scoped.json()
        assert all_data['coverage']['selected_profiles'] == 1
        assert (all_data['window']['start'], all_data['window']['end']) == (10.25, 40.75)
        assert len(all_data['tests']) == len({t['id'] for t in all_data['tests']}) == 101
        assert all_data['tests'][-1]['id'] == qualified
        assert all_data['tests'][-1]['profile_id'] == pid
        assert all_data['tests'][-1]['original_ids']['id'] == 'window'
        assert all(t['profile_id'] == pid for t in all_data['tests'])
        # Qualified filters select just this profile, even when another
        # profile contains the same raw marker ID.
        recent_qualified = aggregate.opaque(pid, 'test', 'new-100')
        global_recent = client.get('/ledger', params={**base, 'profile_scope': 'all',
                                                     'test_id': recent_qualified}).json()
        assert len(global_recent['tests']) == len({t['id'] for t in global_recent['tests']}) == 100
        assert global_recent['tests'][0]['id'] == recent_qualified
        assert all(t['profile_id'] == pid for t in global_recent['tests'])
        assert (global_recent['window']['start'], global_recent['window']['end']) == (200, 201)
        assert client.get('/ledger', params={**base, 'profile_scope': 'all', 'test_id': 'window'}).status_code == 400


@pytest.mark.parametrize(('duration', 'step', 'unit'), [
    (1799, 60, 'minute'), (1800, 60, 'minute'),
    (1800.1, 120, '2 minutes'), (3600, 120, '2 minutes'),
    (86400, 3600, 'hour'), (604800, 86400, 'day'),
])
def test_zoom_resolution_and_clipped_half_open_edges(reports, duration, step, unit):
    root, _, runtime = reports
    result = runtime.read(root, 10.25, 10.25 + duration)
    trend = result['trend']
    assert trend['seconds'] == step and trend['unit'] == unit
    assert trend['buckets'][0]['start'] == 10.25
    assert trend['buckets'][-1]['end'] == 10.25 + duration
    assert all(a['end'] == b['start'] for a, b in zip(trend['buckets'], trend['buckets'][1:]))
