"""Projected ledger fields are exactly full-response fields on disposable readers."""
import json
import sqlite3

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from _hermes_ai_usage_ledger_v2 import aggregate, storage
from _hermes_ai_usage_ledger_v2.api import add_routes
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
from _hermes_ai_usage_ledger_v2.projection import selected_fields, ALL_FIELDS

VIEWS = [('overview', g) for g in ('model', 'time', 'project', 'session', 'subagent')] + [
    ('requests', None), ('cache', None), ('compressions', None), ('models', None), ('skills', None)]


def create(root, *, empty=False):
    folder = root / 'usage-ledger'
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / 'events.sqlite3') as conn:
        conn.executescript(storage.SCHEMA)
        if empty:
            return
        for key, stamp, provider, model, agent, project in [
            ('one', 10, 'alpha', 'returned', 'primary', 'p'),
            ('two', 20, 'alpha', 'returned', 'subagent', 'p'),
            ('unknown', 30, 'beta', '', 'unknown', ''),
        ]:
            row = dict(id=key, started=stamp, ended=stamp + 1, provider=provider,
                model='requested', response_model=model, session_id=key,
                root_session_id='root', subagent_id=key if agent == 'subagent' else '',
                agent_kind=agent, project_id=project or 'unattributed', task='task',
                status='completed', usage={'total_tokens': 12 if key != 'unknown' else None,
                                          'input_tokens': 10, 'cache_read_tokens': 0})
            conn.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key, stamp, stamp + 1, provider, 'requested', key, 'task', None,
                 'completed', json.dumps(row)))
        comp = dict(id='comp', started=21, ended=22, provider='alpha', model='returned',
                    session_id='two')
        conn.execute('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?)',
            ('comp', 21, 22, 'alpha', 'two', 'two', None, json.dumps(comp)))
        conn.execute('INSERT INTO tests VALUES(?,?,?,?,?)',
            ('marker', 11, 29, 'marker', json.dumps({'id': 'marker', 'started': 11, 'ended': 29})))


@pytest.fixture
def sources(tmp_path):
    create(tmp_path)
    create(tmp_path / 'profiles' / 'other')
    (tmp_path / 'profiles' / 'missing').mkdir()
    return tmp_path


def assert_projection(projected, full):
    shape = projected['projection']
    assert shape['version'] == 1
    assert set(shape['included']) | set(shape['omitted']) == ALL_FIELDS
    assert not set(shape['included']) & set(shape['omitted'])
    assert set(projected) - set(full) == {'projection'}
    assert set(projected) == (set(full) - set(shape['omitted'])) | {'projection'}
    for field in full.keys() & projected.keys() - {'generated_at', 'projection'}:
        assert projected[field] == full[field], field


@pytest.mark.parametrize('view,group', VIEWS)
@pytest.mark.parametrize('filters', [{}, {'test_id': 'marker', 'model': 'returned',
                                          'model_provider': 'alpha', 'bucket_start': 12,
                                          'bucket_end': 25}])
def test_selected_contract_and_parity(sources, view, group, filters):
    runtime = AnalyticsRuntime()
    try:
        args = (sources, 0, 40, '', 0, 1, '', '', '', 'exact', '',
                filters.get('model', ''), filters.get('model_provider', ''))
        bounds = dict(test_id=filters.get('test_id', ''),
                      bucket_start=filters.get('bucket_start'), bucket_end=filters.get('bucket_end'))
        full = runtime.read(*args, **bounds)
        projected = runtime.read(*args, **bounds, view=view, group=group)
        assert_projection(projected, full)
        assert projected['projection']['included'] == sorted(selected_fields(view, group))
    finally:
        runtime._discard(runtime.current)


@pytest.mark.parametrize('view,group', VIEWS)
def test_all_profiles_partial_coverage_parity(sources, view, group):
    runtime = AnalyticsRuntime()
    try:
        inventory = aggregate.discover(sources)
        full = aggregate.ledger(runtime, inventory, start=0, end=40, offset=1, limit=1)
        projected = aggregate.ledger(runtime, inventory, start=0, end=40, offset=1,
                                     limit=1, view=view, group=group)
        assert full['coverage']['status'] == projected['coverage']['status'] == 'partial'
        assert_projection(projected, full)
        assert projected['summary']['attempts'] == 6
    finally:
        runtime._discard(runtime.current)


@pytest.mark.parametrize('empty', [True, False])
def test_empty_and_unknown_are_not_omitted_or_zeroed(tmp_path, empty):
    create(tmp_path, empty=empty)
    runtime = AnalyticsRuntime()
    try:
        full = runtime.read(tmp_path, 0, 40)
        projected = runtime.read(tmp_path, 0, 40, view='overview', group='model')
        assert_projection(projected, full)
        assert 'requests' not in projected and 'requests' in projected['projection']['omitted']
        if not empty:
            assert projected['summary']['missing_usage'] > 0
    finally:
        runtime._discard(runtime.current)


def test_no_readable_all_profiles_preserves_unknown_coverage(tmp_path):
    (tmp_path / 'profiles' / 'missing').mkdir(parents=True)
    runtime = AnalyticsRuntime()
    try:
        inventory = aggregate.discover(tmp_path)
        full = aggregate.ledger(runtime, inventory, start=0, end=40)
        projected = aggregate.ledger(runtime, inventory, start=0, end=40,
                                     view='overview', group='session')
        assert full['coverage']['status'] == projected['coverage']['status'] == 'unavailable'
        assert projected['summary'] is None and projected['request_count'] is None
        assert_projection(projected, full)
    finally:
        runtime._discard(runtime.current)


def test_route_validation_and_legacy(sources):
    router = APIRouter()
    add_routes(router, lambda _: (sources, 'default', None), lambda: sources)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for params in ({'view': 'invalid'}, {'group': 'model'}, {'view': 'overview'},
                       {'view': 'requests', 'group': 'model'}, {'view': 'overview', 'group': 'invalid'}):
            for scope in ('selected', 'all'):
                result = client.get('/ledger', params={**params, 'profile_scope': scope, 'end': 40})
                assert result.status_code == 400, result.text
        legacy = client.get('/ledger', params={'end': 40}).json()
        assert 'projection' not in legacy and 'requests' in legacy and 'price_catalogs' in legacy
        projected = client.get('/ledger', params={'end': 40, 'view': 'cache'}).json()
        assert 'applied_rate_groups' in projected and 'price_catalogs' in projected
        assert 'requests' not in projected


def test_hidden_expensive_sections_not_executed(sources, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Hidden section executed')
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'sql_applied_rate_groups', forbidden)
        store = storage.Store(sources)
        report = store.read(0, 40, view='overview', group='time')
        assert report['trend']['buckets']
        assert 'applied_rate_groups' not in report
    # Observe SQL as well: no hidden model/task, attribution groups or details.
    statements = []
    store = storage.Store(sources)
    original = store.db
    from contextlib import contextmanager
    @contextmanager
    def traced():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(store, 'db', traced)
    store.read(0, 40, view='overview', group='time')
    sql = '\n'.join(statements).lower()
    assert 'group by provider,coalesce(nullif(json_extract(data,\'$.response_model\')' not in sql
    assert 'group by provider,coalesce(json_extract(data,\'$.agent_kind\')' not in sql
    assert 'compression_id=?' not in sql
    assert 'read_detail_ids' not in sql
