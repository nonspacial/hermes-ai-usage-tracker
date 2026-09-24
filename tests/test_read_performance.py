"""Synthetic read benchmark and full-response parity; never opens a live ledger."""
import cProfile
import io
import json
import pstats
import shutil
import statistics
import sys
import time

import pytest
from _hermes_ai_usage_ledger_v2 import storage
from _hermes_ai_usage_ledger_v2.accounting import normalize, costs
from _hermes_ai_usage_ledger_v2 import ownership


@pytest.fixture
def ledger(tmp_path):
    store = storage.Store(tmp_path)
    records = []
    rate = dict(provider='synthetic', model='fixture', service_tier='standard',
                input_tokens='1.23456789', output_tokens='9.87654321',
                cache_read_tokens='0.123456789', cache_write_tokens='1.5432098625')
    for i in range(2000):
        sid = 'session-' + str(i % 20)
        usage = normalize({'input_tokens': 30000, 'output_tokens': 20,
                           'input_tokens_details': {'cached_tokens': (i // 20 % 15) * 1000,
                                                    'cache_write_tokens': 0}})
        if i % 31 == 0:
            usage = {}
        elif i % 53 == 0:
            usage = {}  # terminal request without usage
        elif i % 37 == 0:
            usage = dict(usage_source='hermes_normalized', prompt_tokens=30000,
                         input_tokens=30000, output_tokens=20, total_tokens=30020,
                         cache_read_tokens=0, cache_write_tokens=0)
        elif i % 41 == 0:
            usage['request_count'] = 3
        r = dict(id=f'r{i:04}', started=1000 + (i // 2) * 10, ended=1001 + (i // 2) * 10,
                 session_id=sid, provider='synthetic' if i % 3 else 'other', model='fixture',
                 response_model='fixture', api_mode='responses', account_id='fixture-account',
                 task='main' if i % 17 else 'helper', source='main_hook' if i % 17 else 'aux_hook',
                 process='fixture', status='pending' if i % 31 == 0 else 'completed',
                 agent_kind='subagent' if i % 2 else 'primary', subagent_id=sid if i % 2 else '',
                 root_session_id='root', session_lineage=['root'],
                 project_id='p' + str(i % 3), project_label='Project ' + str(i % 3),
                 usage=usage, cost=costs(usage, rate),
                 cache_request={'tools_fingerprint': 'fixture', 'mode_sent': 'automatic'})
        if r['status'] == 'pending':
            r['ended'] = None
            if i % 4 == 0:
                r['owner'] = ownership.identity()  # live, even without usage
            elif i % 4 == 1:
                r['owner'] = {'version': 1}  # uninspectable, never presumed dead
            elif i % 4 == 2:
                r['status'] = 'usage_received'
                r['usage'] = {'total_tokens': 100, 'input_tokens': 50}
            else:
                r['status'] = 'abandoned_without_usage'
        records.append(r)
    with store.db() as c:
        c.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      [(r['id'], r['started'], r['ended'], r['provider'], r['model'], r['session_id'],
                        r['task'], None, r['status'], storage.jd(r)) for r in reversed(records)])
        c.execute('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?)',
                  ('comp', 3000, 3001, 'synthetic', 'session-1', 'session-1', None,
                   storage.jd(dict(id='comp', started=3000, ended=3001, provider='synthetic',
                                   session_id='session-1', session_after='session-1'))))
        c.execute('INSERT INTO tests VALUES(?,?,?,?,?)',
                  ('marker', 3003, 3257, 'fixture', storage.jd(dict(id='marker', started=3003, ended=3257))))
    return store


@pytest.mark.parametrize('filters', [
    {}, {'start': 5000, 'offset': 13, 'limit': 19},
    {'provider': 'synthetic'}, {'session': 'session-1'},
    {'session': 'root', 'session_scope': 'family', 'agent': 'subagent'},
    {'project': 'p1', 'subagent': 'session-1', 'start': 3000},
    {'agent': 'unknown'}, {'provider': 'absent'}, {'start': 11000},
    {'model': 'fixture', 'model_provider': 'synthetic', 'offset': 130, 'limit': 17},
    {'bucket_start': 3000, 'bucket_end': 3300, 'offset': 3, 'limit': 7},
])
def test_full_response_parity(ledger, monkeypatch, filters):
    from _hermes_ai_usage_ledger_v2 import session_cache_writes
    monkeypatch.setattr(storage.time, 'time', lambda: 12000)
    with ledger.db() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM main.requests ORDER BY id')]
    args = dict(start=0, end=12000, **{k: v for k, v in filters.items() if k != 'start'})
    args['start'] = filters.get('start', 0)
    bucket_start = args.pop('bucket_start', None)
    bucket_end = args.pop('bucket_end', None)
    if bucket_start is not None:
        args['start'], args['end'] = max(args['start'], bucket_start), min(args['end'], bucket_end)
    actual = ledger.read(**args)
    with monkeypatch.context() as original:
        original.setattr(session_cache_writes, 'materialise_summary', lambda c, sql: sql)
        expected = ledger.read(**args)
    assert actual == expected  # Every field, decimal string and list order, not just totals.
    with ledger.db() as c:
        assert before == [tuple(r) for r in c.execute('SELECT * FROM main.requests ORDER BY id')]
        assert not c.execute("SELECT 1 FROM sqlite_temp_master WHERE name='read_summary_values'").fetchone()


@pytest.mark.parametrize('filters', [{}, {'start': 5000, 'provider': 'synthetic', 'model': 'fixture'}])
def test_owner_execution_is_evaluated_once_per_selected_open_row(ledger, monkeypatch, filters):
    monkeypatch.setattr(storage.time, 'time', lambda: 12000)
    calls = 0
    original = ownership.execution_state

    def counted(record, cache=None):
        nonlocal calls
        calls += 1
        return original(record, cache)

    monkeypatch.setattr(ownership, 'execution_state', counted)
    args = {**dict(start=0, end=12000, limit=19), **filters}
    result = ledger.read(**args)
    # Open owners are evaluated freshly on EVERY read; page details are
    # separately decorated, while full-set rollups reuse one per-read value.
    assert result['request_count'] > 0
    with ledger.db() as c:
        open_rows = c.execute("SELECT COUNT(*) FROM requests WHERE status IN ('pending','usage_received') "
            "AND ended IS NULL AND started>=? AND started<? AND (?='' OR provider=?)",
            (args['start'], args['end'], args.get('provider', ''), args.get('provider', ''))).fetchone()[0]
    assert calls == open_rows + len(result['requests'])
    assert result['summary']['pending'] > 0
    assert result['summary']['unresolved'] > 0
    assert result['summary']['abandoned'] > 0
    calls = 0
    ledger.read(**args)
    assert 0 < calls <= open_rows + len(result['requests'])  # no cross-read cache


def test_owner_liveness_changes_between_reads(ledger, monkeypatch):
    monkeypatch.setattr(storage.time, 'time', lambda: 12000)
    first = ledger.read(0, 12000, limit=5)
    original = ownership.inspect_owner
    monkeypatch.setattr(ownership, 'inspect_owner', lambda owner: ('unknown', 'inspection_unavailable'))
    second = ledger.read(0, 12000, limit=5)
    assert second['summary']['pending'] < first['summary']['pending']
    assert second['summary']['unresolved'] > first['summary']['unresolved']
    monkeypatch.setattr(ownership, 'inspect_owner', original)


@pytest.mark.parametrize('filters', [
    {'offset': 137, 'limit': 17},
    {'provider': 'synthetic', 'model': 'fixture', 'model_provider': 'synthetic',
     'bucket_start': 3100, 'bucket_end': 3200, 'offset': 3, 'limit': 7},
])
@pytest.mark.parametrize('partial', [False, True])
def test_all_profiles_full_response_parity(ledger, tmp_path, monkeypatch, filters, partial):
    from _hermes_ai_usage_ledger_v2 import aggregate
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    monkeypatch.setattr(storage.time, 'time', lambda: 12000)
    other = tmp_path / 'profiles' / 'other' / 'usage-ledger'
    other.mkdir(parents=True)
    shutil.copyfile(ledger.path, other / 'events.sqlite3')
    if partial:
        (tmp_path / 'profiles' / 'missing').mkdir()
        unavailable = tmp_path / 'profiles' / 'unreadable' / 'usage-ledger'
        unavailable.mkdir(parents=True)
        (unavailable / 'events.sqlite3').touch()  # no schema, not counted as zero
    inventory = aggregate.discover(tmp_path)
    runtime = AnalyticsRuntime()
    try:
        args = dict(start=0, end=12000, **filters)
        actual = aggregate.ledger(runtime, inventory, **args)
        assert actual['coverage']['status'] == ('partial' if partial else 'complete')
        if partial:
            assert {'missing', 'unreadable'} <= {p['status'] for p in actual['coverage']['profiles']}
        assert actual['request_count'] > 0
        writes = sys.modules[runtime.current['store_type'].__module__.rsplit('.', 1)[0] + '.session_cache_writes']
        with monkeypatch.context() as original:
            original.setattr(writes, 'materialise_summary', lambda c, sql: sql)
            expected = aggregate.ledger(runtime, inventory, **args)
        assert actual == expected
        assert len({r['id'] for r in actual['requests']}) == len(actual['requests'])
    finally:
        runtime._discard(runtime.current)


def test_marker_and_bucket_full_response_parity(ledger, monkeypatch):
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    monkeypatch.setattr(storage.time, 'time', lambda: 12000)
    runtime = AnalyticsRuntime()
    try:
        args = (0, 12000, 'synthetic', 11, 9, '', '', '', 'exact', '', 'fixture', 'synthetic')
        kwargs = dict(test_id='marker', bucket_start=3070, bucket_end=3100)
        actual = runtime.read(ledger.root, *args, **kwargs)
        writes = sys.modules[runtime.current['store_type'].__module__.rsplit('.', 1)[0] + '.session_cache_writes']
        with monkeypatch.context() as original:
            original.setattr(writes, 'materialise_summary', lambda c, sql: sql)
            expected = runtime.read(ledger.root, *args, **kwargs)
        assert actual == expected
        assert actual['window']['start'] == 3070
        assert actual['window']['end'] == 3100
    finally:
        runtime._discard(runtime.current)


def test_new_snapshot_sees_completed_usage(ledger, monkeypatch):
    from _hermes_ai_usage_ledger_v2 import session_cache_writes
    monkeypatch.setattr(storage.time, 'time', lambda: 12000)
    before = ledger.read(start=5000, end=12000, project='p1')
    # A late completion outside the selected project/time window can change the
    # predecessor. Refresh must consult the ledger again, not reuse a cached read.
    with ledger.db() as c:
        row = json.loads(c.execute("SELECT data FROM requests WHERE id='r0821'").fetchone()[0])
        row['usage']['cache_read_tokens'] = 123
        c.execute('UPDATE requests SET data=? WHERE id=?', (storage.jd(row), row['id']))
    after = ledger.read(start=5000, end=12000, project='p1')
    assert after != before
    with monkeypatch.context() as original:
        original.setattr(session_cache_writes, 'materialise_summary', lambda c, sql: sql)
        assert after == ledger.read(start=5000, end=12000, project='p1')


def test_synthetic_read_benchmark(ledger, capsys):
    samples = []
    for _ in range(3):
        began = time.perf_counter()
        result = ledger.read(0, 12000)
        samples.append(time.perf_counter() - began)
    assert result['request_count'] == 2000
    profile = cProfile.Profile()
    profile.runcall(ledger.read, 0, 12000)
    report = io.StringIO()
    pstats.Stats(profile, stream=report).sort_stats('cumulative').print_stats(12)
    with capsys.disabled():
        print('\nSYNTHETIC 2000 read seconds:', samples, 'median:', statistics.median(samples))
        print(report.getvalue())
