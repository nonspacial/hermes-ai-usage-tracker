"""Bound full request hydration, not complete-window accounting."""
import sqlite3

import pytest
from _hermes_ai_usage_ledger_v2 import aggregate
from test_all_profiles import database, inventory, request, runtime


@pytest.mark.parametrize('offset', [0, 3, 65, 79, 80, 1000])
@pytest.mark.parametrize('provider', ['', 'fixture', 'absent'])
def test_page_keys_match_full_report_and_bound_hydration(tmp_path, runtime, monkeypatch, offset, provider):
    roots = [tmp_path, tmp_path / 'profiles' / 'other']
    expected = []
    for root in roots:
        database(root, [request(str(i), ts=i // 3 + 1, padding='x' * 8192) for i in range(40)])
        profile = next(p for p in inventory(tmp_path)['profiles'] if p['profile_id'] == aggregate.profile_id(root))
        report = runtime.read(root, 0, 100, provider, 0, 200)
        expected.extend(aggregate.qualify(r, profile, 'request') for r in report['requests'])
    expected = aggregate.ordered(expected, 'started', 7, offset)
    store = runtime.current['store_type']
    original = store.read
    hydrated = []
    def read(self, *args, **kwargs):
        assert kwargs['_detail_ids'] is not None
        result = original(self, *args, **kwargs)
        assert {r['id'] for r in result['requests']} == set(kwargs['_detail_ids'])
        hydrated.extend(result['requests'])
        return result
    monkeypatch.setattr(store, 'read', read)
    out = aggregate.ledger(runtime, inventory(tmp_path), end=100, offset=offset, limit=7, provider=provider)
    assert out['requests'] == expected
    assert len(hydrated) == len(expected) <= 7
    assert out['request_count'] == (0 if provider == 'absent' else 80)
    assert out['summary']['attempts'] == out['request_count']


def test_late_failure_refills_from_same_snapshots(tmp_path, runtime, monkeypatch):
    for root in [tmp_path, tmp_path / 'profiles' / 'other']:
        database(root, [request(str(i), ts=i + 1) for i in range(10)])
    inv = inventory(tmp_path)
    store = runtime.current['store_type']
    original_keys, original_read = store._request_keys, store.read
    roots = []
    failed_root = None
    def keys(self, *args, **kwargs):
        roots.append(self.root)
        return original_keys(self, *args, **kwargs)
    def read(self, *args, **kwargs):
        nonlocal failed_root
        assert self.root in roots
        if failed_root is None:
            failed_root = self.root
            raise sqlite3.OperationalError('late fixture failure')
        assert self.root != failed_root
        return original_read(self, *args, **kwargs)
    monkeypatch.setattr(store, '_request_keys', keys)
    monkeypatch.setattr(store, 'read', read)
    out = aggregate.ledger(runtime, inv, end=100, offset=5, limit=4)
    assert len(roots) == 2
    assert out['coverage']['status'] == 'partial'
    assert out['request_count'] == out['summary']['attempts'] == 10
    assert [r['original_ids']['id'] for r in out['requests']] == ['4', '3', '2', '1']
    assert out['next_offset'] == 9


def test_private_detail_membership_cannot_escape_scope(tmp_path, runtime):
    database(tmp_path, [request('outside', ts=1), request('inside', ts=3)])
    with runtime.lease() as generation:
        reader = runtime._reader(generation, tmp_path)
        complete = reader.read(2, 5)
        empty = reader.read(2, 5, _detail_ids=[])
        selected = reader.read(2, 5, _detail_ids=['outside', 'inside'])
    assert empty['requests'] == []
    assert selected['requests'] == complete['requests']
    for field in ('summary', 'trend', 'groups', 'cache_read_progression', 'request_count'):
        assert empty[field] == selected[field] == complete[field]


@pytest.mark.parametrize('filters', [
    {'session': 'child'}, {'session': 'root', 'session_scope': 'family'},
    {'project': 'p1'}, {'agent': 'subagent'}, {'subagent': 'child'},
    {'provider': 'other', 'project': 'p0'}, {'agent': 'unknown'},
    {'session': 'root', 'session_scope': 'family', 'project': 'p1', 'subagent': 'child'},
])
def test_filtered_keys_and_full_rollups_match_selected(tmp_path, runtime, filters):
    rows = []
    for i in range(12):
        row = request(str(i), ts=i + 1)
        row.update(session_id='child' if i % 2 else 'root', session_lineage=['root'],
                   subagent_id='child' if i % 2 else '', agent_kind='subagent' if i % 2 else 'primary',
                   project_id='p' + str(i % 3), provider='other' if i % 3 else 'fixture')
        rows.append(row)
    database(tmp_path, rows)
    inv = inventory(tmp_path)
    profile = inv['profiles'][0]
    qualified = {key: aggregate.opaque(profile['profile_id'], key, value)
                 if key in ('session', 'project', 'subagent') else value for key, value in filters.items()}
    selected = runtime._reader(runtime.current, tmp_path).read(3, 20, offset=1, limit=2, trend_start=3, **filters)
    out = aggregate.ledger(runtime, inv, start=3, end=20, offset=1, limit=2, **qualified)
    assert out['requests'] == [aggregate.qualify(r, profile, 'request') for r in selected['requests']]
    for field in ('summary', 'trend', 'request_count', 'crossing_start', 'crossing_end', 'cache_read_progression'):
        assert out[field] == selected[field]


def test_keys_and_hydration_share_frozen_source(tmp_path, runtime, monkeypatch):
    path = database(tmp_path, [request('old')])
    store = runtime.current['store_type']
    original = store._request_keys
    def keys(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        # Authorised synthetic source changes after selection; hydration must not
        # reopen it. This also exercises a refresh racing with an incoming write.
        with sqlite3.connect(path) as c:
            c.execute('DELETE FROM requests')
        return result
    monkeypatch.setattr(store, '_request_keys', keys)
    out = aggregate.ledger(runtime, inventory(tmp_path), end=10)
    assert out['request_count'] == 1
    assert out['requests'][0]['original_ids']['id'] == 'old'


def test_maximum_detail_membership_has_no_sql_variable_limit(tmp_path, runtime):
    database(tmp_path, [request(str(i), ts=i + 1) for i in range(2001)])
    out = aggregate.ledger(runtime, inventory(tmp_path), end=3000, offset=1, limit=2000)
    assert len(out['requests']) == 2000
    assert out['request_count'] == 2001
    assert out['requests'][0]['original_ids']['id'] == '1999'
    assert out['requests'][-1]['original_ids']['id'] == '0'
