"""Synthetic read benchmark and full-response parity; never opens a live ledger."""
import cProfile
import io
import json
import pstats
import statistics
import time

import pytest
from _hermes_ai_usage_ledger_v2 import storage
from _hermes_ai_usage_ledger_v2.accounting import normalize, costs


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
        records.append(r)
    with store.db() as c:
        c.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      [(r['id'], r['started'], r['ended'], r['provider'], r['model'], r['session_id'],
                        r['task'], None, r['status'], storage.jd(r)) for r in reversed(records)])
        c.execute('INSERT INTO compressions VALUES(?,?,?,?,?,?,?,?)',
                  ('comp', 3000, 3001, 'synthetic', 'session-1', 'session-1', None,
                   storage.jd(dict(id='comp', started=3000, ended=3001, provider='synthetic',
                                   session_id='session-1', session_after='session-1'))))
    return store


@pytest.mark.parametrize('filters', [
    {}, {'start': 5000, 'offset': 13, 'limit': 19},
    {'provider': 'synthetic'}, {'session': 'session-1'},
    {'session': 'root', 'session_scope': 'family', 'agent': 'subagent'},
    {'project': 'p1', 'subagent': 'session-1', 'start': 3000},
    {'agent': 'unknown'}, {'provider': 'absent'}, {'start': 11000},
])
def test_full_response_parity(ledger, monkeypatch, filters):
    from _hermes_ai_usage_ledger_v2 import session_cache_writes
    monkeypatch.setattr(storage.time, 'time', lambda: 12000)
    with ledger.db() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM main.requests ORDER BY id')]
    args = dict(start=0, end=12000, **{k: v for k, v in filters.items() if k != 'start'})
    args['start'] = filters.get('start', 0)
    actual = ledger.read(**args)
    with monkeypatch.context() as original:
        original.setattr(session_cache_writes, 'materialise_summary', lambda c, sql: sql)
        expected = ledger.read(**args)
    assert actual == expected  # Every field, decimal string and list order, not just totals.
    with ledger.db() as c:
        assert before == [tuple(r) for r in c.execute('SELECT * FROM main.requests ORDER BY id')]
        assert not c.execute("SELECT 1 FROM sqlite_temp_master WHERE name='read_summary_values'").fetchone()


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
