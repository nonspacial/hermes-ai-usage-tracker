"""Bounded read-cost paths versus their unbounded reference implementations.

Synthetic, isolated ledgers only. The reference mode reinstates the original
full-session cache-write replay, the original all-time project-option query,
per-row Python cost admission and a copy without the read-cost indexes; every
DTO must be identical. The ~50k-row scale case runs only when
HERMES_USAGE_READ_COST_SCALE=1 and reports timings (no wall-clock assertion).
"""
import contextlib
import json
import os
import sqlite3
import sys
import time

import pytest

from _hermes_ai_usage_ledger_v2 import storage, session_cache_writes, incremental
from _hermes_ai_usage_ledger_v2.accounting import normalize, costs

ORIGIN = 1_000_000.0
RATE = dict(provider='synthetic', model='fixture', service_tier='standard',
            input_tokens='1.23456789', output_tokens='9.87654321',
            cache_read_tokens='0.123456789', cache_write_tokens='1.5432098625')


def _record(i, n, pad):
    """Deterministic mix: long-lived and short sessions, several streams per
    session, pending/aggregate/missing reads, ties, projects and subagents."""
    # A quarter of rows belong to 6 sessions spanning the whole ledger; the rest
    # to short sessions of ~40 rows. Two rows share each timestamp (id tie-break).
    long_lived = i % 4 == 0
    sid = ('long-' + str(i % 6)) if long_lived else ('short-' + str(i // 40))
    if i % 211 == 0:
        sid = ''  # no session: never a cache stream
    started = ORIGIN + (i // 2) * 10
    helper = i % 7 == 3
    sub = i % 5 == 1
    reads = ((i // 3) % 17) * 1000 if i % 13 else 0
    usage = normalize({'input_tokens': 30000, 'output_tokens': 20,
                       'input_tokens_details': {'cached_tokens': reads, 'cache_write_tokens': 0}})
    status, ended = 'completed', started + 3
    if i % 97 == 0:
        usage = {}  # terminal without usage: missing reads, still a predecessor
    elif i % 89 == 0:
        status, ended, usage = 'pending', None, {}  # never a predecessor
    elif i % 83 == 0:
        usage['request_count'] = 3
    elif i % 79 == 0:
        usage = dict(usage_source='hermes_normalized', prompt_tokens=30000, input_tokens=30000,
                     output_tokens=20, total_tokens=30020, cache_read_tokens=0, cache_write_tokens=0)
    cost = costs(usage, RATE)
    if i % 101 == 0:
        cost['known_components_usd'] = '1' * 25  # too wide: must fail Decimal admission path
    elif i % 103 == 0:
        cost['known_components_usd'] = '1E+2'  # exponent text: Python decides
    record = dict(id=f'r{i:07}', started=started, ended=ended, session_id=sid,
                  provider='synthetic' if i % 3 else 'other', model='fixture',
                  response_model='fixture' if i % 11 else 'fixture-b',
                  api_mode='responses', account_id='acct-' + str(i % 2 if i % 9 == 0 else 0),
                  task='title_generation' if helper else 'main',
                  source='aux_hook' if helper else 'main_hook', status=status,
                  agent_kind='subagent' if sub else 'primary',
                  subagent_id=('child-' + str(i % 4)) if sub else '',
                  root_session_id=sid, project_id=None if i % 17 == 0 else 'p' + str(i % 5),
                  project_label=None if i % 17 == 0 else 'Project ' + str(i % 5),
                  project_path='/synthetic/p' + str(i % 5), usage=usage, cost=cost)
    if pad:
        record['cache_request'] = {'pad': 'x' * pad}
    return record


def build(root, n, *, pad=0):
    store = storage.Store(root)
    with store.db() as c:
        # Bulk fixture load only: the journal triggers are reinstalled verbatim
        # afterwards, so the canonical inventory (and signed reads) still apply.
        for name in incremental.TRIGGERS:
            c.execute('DROP TRIGGER ' + name)
        rows = (_record(i, n, pad) for i in range(n))
        c.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      ((r['id'], r['started'], r['ended'], r['provider'], r['model'], r['session_id'],
                        r['task'], None, r['status'], storage.jd(r)) for r in rows))
        events = []
        for k in range(n // 50):
            ts = ORIGIN + k * 250
            child = 'short-' + str((k * 25) // 40)
            events.append((ts, 'subagent_start', child, storage.jd({'subagent_id': 'child-' + str(k % 4)})))
            if k % 9:
                events.append((ts + 120, 'subagent_stop', child, '{}'))
            events.append((ts, 'request_completed', 'r' + str(k), '{"pad":"' + 'y' * min(pad, 200) + '"}'))
        c.executemany('INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)', events)
        for sql in incremental.TRIGGERS.values():
            c.execute(sql)
    with store.db() as c:
        assert incremental.supported(c)
    return store


def _reference_options(c):
    project = "COALESCE(json_extract(data,'$.project_id'),'unattributed')"
    return [dict(id=r[0], label=r[1] or 'Unattributed project', path=r[2], basis=r[3]) for r in c.execute(
        'SELECT ' + project + ",MAX(json_extract(data,'$.project_label')),MAX(json_extract(data,'$.project_path')),"
        "MAX(json_extract(data,'$.project_source')) FROM requests GROUP BY " + project)]


@contextlib.contextmanager
def reference(monkeypatch, modules):
    """Patch reloadable module objects (``modules`` = (storage, session_cache_writes))."""
    store_mod, writes_mod = modules

    def observations(c, sid, ids, lo, end):
        # The original full-history replay from ledger start.
        return _reference_project_session(c, sid, ids, end, writes_mod)

    with monkeypatch.context() as m:
        m.setattr(writes_mod, 'session_observations', observations)
        m.setattr(store_mod, 'project_options', _reference_options)
        m.setattr(store_mod, 'cost_admission', lambda projected, check: None)  # original ordered scan
        yield


def _reference_project_session(c, sid, ids, end, writes_mod):
    previous, changes = {}, {}
    for r in writes_mod._sequence_rows(c, sid, end):
        key = writes_mod.stream_key(r)
        result = writes_mod.observation(r, previous.get(key))
        if r['id'] in ids:
            changes[r['id']] = result
        if result['status'] != 'pending':
            previous[key] = r
    return changes


def unindexed_copy(store, target):
    folder = target / 'usage-ledger'
    folder.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(store.path)) as source, \
            contextlib.closing(sqlite3.connect(folder / 'events.sqlite3')) as copy:
        source.backup(copy)
        for name in storage.READ_INDEX_SQL:
            copy.execute('DROP INDEX ' + name)
        copy.commit()
    reader = object.__new__(storage.Store)
    reader.root, reader.folder, reader.path = target, folder, folder / 'events.sqlite3'
    return reader


def strip(result):
    return {k: v for k, v in result.items() if k != 'generated_at'}


def windows(n):
    end = ORIGIN + (n // 2) * 10
    width = end - ORIGIN
    return end, [
        dict(start=0, end=end),  # unbounded start
        dict(start=end - width * 0.2, end=end),  # tail window: long sessions have history
        dict(start=ORIGIN + width * 0.4, end=ORIGIN + width * 0.55),  # interior, later rows exist
        dict(start=end - width * 0.3, end=end, project='p1', agent='subagent'),
        dict(start=end - width * 0.3, end=end, session='long-2'),
        dict(start=end - width * 0.3, end=end, provider='other', model='fixture-b'),
    ]


N_SMALL = 3000


@pytest.fixture(scope='module')
def small(tmp_path_factory):
    return build(tmp_path_factory.mktemp('read-cost'), N_SMALL)


def test_session_observations_equal_full_replay(small):
    end, cases = windows(N_SMALL)
    with small.db() as c:
        sessions = [r[0] for r in c.execute("SELECT DISTINCT session_id FROM requests WHERE session_id<>''")]
        for case in cases[:3]:
            lo, hi = case['start'], case['end']
            for sid in sessions:
                ids = {r[0] for r in c.execute('SELECT id FROM requests WHERE session_id=? AND started>=? AND started<?',
                                              (sid, lo, hi))}
                if not ids:
                    continue
                first = c.execute('SELECT MIN(started) FROM requests WHERE session_id=? AND started>=? AND started<?',
                                  (sid, lo, hi)).fetchone()[0]
                # Also a sparse selection: only the last selected row.
                for chosen in (ids, {max(ids)}):
                    start = first if chosen is ids else c.execute(
                        'SELECT started FROM requests WHERE id=?', (max(ids),)).fetchone()[0]
                    assert (session_cache_writes.session_observations(c, sid, chosen, start, hi) ==
                            _reference_project_session(c, sid, chosen, hi, session_cache_writes))


@pytest.mark.parametrize('case', range(6))
@pytest.mark.parametrize('view,group', [(None, None), ('overview', 'time'), ('overview', 'model'),
                                        ('overview', 'project')])
def test_store_read_parity(small, tmp_path, monkeypatch, case, view, group):
    end, cases = windows(N_SMALL)
    args = cases[case]
    monkeypatch.setattr(storage.time, 'time', lambda: end + 5)
    actual = small.read(**args, view=view, group=group, _delta_seed=view is not None,
                        _validate_costs=view is not None)
    old = unindexed_copy(small, tmp_path / 'old')
    # New code on an existing (unindexed, never migrated) ledger.
    unindexed = old.read(**args, view=view, group=group, _delta_seed=view is not None,
                         _validate_costs=view is not None)
    with reference(monkeypatch, (storage, session_cache_writes)):
        expected = old.read(**args, view=view, group=group, _delta_seed=view is not None,
                            _validate_costs=view is not None)
    dump = lambda result: json.dumps(strip(result), sort_keys=True, default=str)
    assert dump(actual) == dump(expected)
    assert dump(unindexed) == dump(expected)
    assert actual['summary']['session_cache_writes']['compared_requests'] > 0


def test_runtime_selected_overview_parity(small, tmp_path, monkeypatch):
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    end, cases = windows(N_SMALL)
    old = unindexed_copy(small, tmp_path / 'old')
    runtime = AnalyticsRuntime()
    try:
        prefix = runtime.current['store_type'].__module__.rsplit('.', 1)[0]
        modules = (sys.modules[prefix + '.storage'], sys.modules[prefix + '.session_cache_writes'])
        for group in ('time', 'model'):
            for args in cases[1:3]:
                positional = (args['start'], args['end'], '', 0, 10, '', '', '', 'exact', '', '', '')
                actual = runtime.read(small.root, *positional, view='overview', group=group)
                with reference(monkeypatch, modules):
                    expected = runtime.read(old.root, *positional, view='overview', group=group)
                for result in (actual, expected):
                    result.pop('generated_at')
                    result['incremental'].pop('resume_token', None)
                assert actual == expected
    finally:
        runtime._discard(runtime.current)


def test_cost_admission_matches_ordered_python_scan():
    from _hermes_ai_usage_ledger_v2.incremental import safe_cost
    ok, wide, bad = {'known_components_usd': '0.1'}, {'known_components_usd': '1' * 23}, {'components': {'a': 'x'}}
    for costs_ in ([], [None], [ok, None, {}], [ok, wide], [wide, ok], [ok, bad], [wide, bad], [bad, wide]):
        projected = {str(i): v for i, v in enumerate(costs_)}
        try:
            expected = all(safe_cost({'cost': v}) for v in costs_)
        except Exception:
            expected = 'raises'
        got = storage.cost_admission(projected, safe_cost)
        if got is None:
            assert any(v is bad for v in costs_)  # defers to the original ordered scan
        else:
            assert got == expected


def _drop_read_indexes(store):
    with store.db() as c:
        for name in storage.READ_INDEX_SQL:
            c.execute('DROP INDEX IF EXISTS ' + name)


def _index_names(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _offline_options(path, tmp_path):
    import hashlib
    backup = tmp_path / 'verified.sqlite3'
    with contextlib.closing(sqlite3.connect(path)) as source, contextlib.closing(sqlite3.connect(backup)) as target:
        source.backup(target)
    proof = hashlib.sha256(backup.read_bytes()).hexdigest()
    return dict(confirmed=True, offline_target=path, backup=backup, backup_sha256=proof)


def test_read_indexes_fresh_only_and_offline_for_existing(tmp_path):
    # A fresh ledger gets the read indexes at creation (empty tables, no scan).
    # An existing ledger without them is never indexed by Store() (that is a
    # full scan under the writer lock); only the writer-excluded offline path
    # adds them, and presence stays optional-but-exact for signed reads.
    from _hermes_ai_usage_ledger_v2.incremental_migration import migrate_offline
    root = tmp_path / 'legacy'
    store = build(root, 200)
    with store.db() as c:
        assert set(storage.READ_INDEX_SQL) <= _index_names(c)
    _drop_read_indexes(store)
    storage.Store(root)
    with store.db() as c:
        assert not set(storage.READ_INDEX_SQL) & _index_names(c)
        assert incremental.supported(c)
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    options = _offline_options(store.path, tmp_path)
    migrate_offline(store.path, **options)
    with store.db() as c:
        names = _index_names(c)
        assert set(storage.READ_INDEX_SQL) <= names
        assert incremental.supported(c)
        plan = ' '.join(str(tuple(r)) for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT ts FROM events WHERE kind IN ('subagent_start','subagent_stop') ORDER BY seq"))
        assert 'event_kind' in plan
        # A different definition under an approved name is not admissible.
        c.execute('DROP INDEX event_kind')
        c.execute('CREATE INDEX event_kind ON events(kind,ts)')
        assert not incremental.supported(c)
        c.execute('DROP INDEX event_kind')
        c.execute(storage.READ_INDEX_SQL['event_kind'])
        assert incremental.supported(c)
        # Invalid JSON can still be written with the expression index present,
        # and project options fall back to the original query's semantics.
        c.execute("INSERT INTO requests VALUES('bad2',1,2,'p','m','s','main',NULL,'completed','{not-json')")
        c.execute("DELETE FROM requests WHERE id='bad2'")
        c.execute("INSERT INTO requests VALUES('bad3',1,2,'p','m','s','main',NULL,'completed','[1]')")
        assert storage.project_options(c) == _reference_options(c)
        # SQLite reads the first duplicate key, Python json the last: a TEMP
        # projection re-serialised by Python must defer to the original query.
        c.execute('''INSERT INTO requests VALUES('dup',1,2,'p','m','s','main',NULL,'completed','{"project_id":"first","project_id":"last"}')''')
        c.execute('CREATE TEMP TABLE session_write_projection(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        c.execute('''INSERT INTO session_write_projection VALUES('dup','{"project_id":"last"}')''')
        c.execute('CREATE TEMP VIEW requests AS SELECT r.id,r.started,r.ended,r.provider,r.model,r.session_id,r.task,'
                  'r.compression_id,r.status,COALESCE(p.data,r.data) AS data FROM main.requests r '
                  'LEFT JOIN session_write_projection p ON p.id=r.id')
        assert 'last' in {o['id'] for o in storage.project_options(c)}
        assert storage.project_options(c) == _reference_options(c)


def test_legacy_offline_migration_adds_read_indexes(tmp_path):
    from _hermes_ai_usage_ledger_v2.incremental_migration import migrate_offline
    from _hermes_ai_usage_ledger_v2.skills import SCHEMA as SKILLS_SCHEMA
    folder = tmp_path / 'usage-ledger'
    folder.mkdir()
    path = folder / 'events.sqlite3'
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.executescript(storage.SCHEMA + SKILLS_SCHEMA)
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(1,'subagent_start','s','{}')")
        c.commit()
    storage.Store(tmp_path)  # existing legacy ledger: neither journal nor read indexes
    with contextlib.closing(sqlite3.connect(path)) as c:
        assert not incremental.supported(c)
        assert not set(storage.READ_INDEX_SQL) & _index_names(c)
    migrate_offline(path, **_offline_options(path, tmp_path))
    with contextlib.closing(sqlite3.connect(path)) as c:
        assert incremental.supported(c)
        assert set(storage.READ_INDEX_SQL) <= _index_names(c)


def test_existing_ledger_open_never_blocks_cross_process_writer(tmp_path, monkeypatch):
    """Store() on an existing unindexed ledger (recorder/hook path) must not
    build read indexes under its writer lock: a second-process writer with the
    recorder's 2 s busy timeout must succeed promptly (else @safe drops a record).

    The index build is made deterministically slow (0.3 s per events row via a
    registered deterministic SQL function) so a regression holds the lock >2 s.
    """
    from _hermes_ai_usage_ledger_v2 import ownership
    import threading
    root = tmp_path / 'existing'
    store = build(root, 400)
    _drop_read_indexes(store)
    with store.db() as c:
        assert c.execute('SELECT COUNT(*) FROM events').fetchone()[0] * 0.3 > 2.5
    calls = []

    def slow(value):
        calls.append(value)
        time.sleep(0.3)
        return value
    register = ownership.register_sql

    def register_slow(c):
        register(c)
        c.create_function('slow_probe', 1, slow, deterministic=True)
    monkeypatch.setattr(ownership, 'register_sql', register_slow)
    monkeypatch.setitem(storage.READ_INDEX_SQL, 'event_kind',
                        'CREATE INDEX IF NOT EXISTS event_kind ON events(slow_probe(kind))')
    started, result = threading.Event(), {}

    def writer():
        started.wait(5)
        time.sleep(0.05)
        t = time.perf_counter()
        with contextlib.closing(sqlite3.connect(store.path, timeout=2)) as w:
            try:
                w.execute('BEGIN IMMEDIATE')
                w.execute("INSERT INTO health VALUES('probe',1,'{}') ON CONFLICT(process) DO UPDATE SET updated=2")
                w.commit()
                result['writer'] = ('ok', time.perf_counter() - t)
            except sqlite3.OperationalError as exc:
                result['writer'] = ('failed ' + str(exc), time.perf_counter() - t)
    real_db = storage.Store.db

    @contextlib.contextmanager
    def signalling_db(self):
        # Release the writer as soon as Store() opens its connection.
        with real_db(self) as c:
            started.set()
            yield c
    monkeypatch.setattr(storage.Store, 'db', signalling_db)
    thread = threading.Thread(target=writer)
    thread.start()
    storage.Store(root)
    thread.join(10)
    assert result['writer'][0] == 'ok', result
    assert result['writer'][1] < 1.0, result
    assert not calls
    with contextlib.closing(sqlite3.connect(store.path)) as c:
        assert 'event_kind' not in _index_names(c)
        assert c.execute("SELECT updated FROM health WHERE process='probe'").fetchone() is not None
    # A fresh ledger indexes empty tables: the slow expression never runs.
    storage.Store(tmp_path / 'fresh')
    assert not calls


def test_info_digest_cache_waits_for_settled_ctime(tmp_path, monkeypatch):
    from _hermes_ai_usage_ledger_v2 import analytics_reload
    runtime = object.__new__(analytics_reload.AnalyticsRuntime)
    path = tmp_path / 'module.py'
    path.write_text('a = 1\n')
    first = runtime._digest(path)
    assert path not in runtime._digests  # ctime within DIGEST_SETTLE: never cached
    now = time.time()
    monkeypatch.setattr(analytics_reload.time, 'time', lambda: now + analytics_reload.DIGEST_SETTLE + 1)
    assert runtime._digest(path) == first
    assert path in runtime._digests
    path.write_text('b = 2\n')  # same size, new ctime: key differs, rehashed
    assert runtime._digest(path) != first


# Reviewer's adversarial predecessor fixture (ported from an out-of-tree
# old-tree/new-tree comparison onto the in-repo reference mode).
ADV_ORIGIN = 2_000_000.0
ADV_RATE = dict(provider='synthetic', model='fixture', service_tier='standard', input_tokens='1.2',
                output_tokens='9.8', cache_read_tokens='0.12', cache_write_tokens='1.5')


def _adv_rows():
    import random
    rows = []

    def add(sid, t, *, task='main', source='main_hook', reads=None, status='completed', sub='', model='m',
            acct='a', rc=None, project='p1', agent='primary', provider='synthetic'):
        usage = {} if reads is None else normalize(
            {'input_tokens': 1000, 'output_tokens': 5,
             'input_tokens_details': {'cached_tokens': reads, 'cache_write_tokens': 0}})
        if rc is not None:
            usage['request_count'] = rc
        rows.append(dict(id=f'x{len(rows) + 1:05}', started=t, ended=None if status == 'pending' else t + 1,
                         session_id=sid, provider=provider, model=model, response_model=model,
                         api_mode='responses', account_id=acct, task=task, source=source, status=status,
                         agent_kind=agent, subagent_id=sub, root_session_id=sid, project_id=project,
                         project_label='L' + str(project), usage=usage, cost=costs(usage, ADV_RATE)))
    O, W = ADV_ORIGIN, ADV_ORIGIN + 1000
    # S1: several streams spanning W; pending rows (with and without reads) just before W.
    add('S1', O + 10, reads=100)
    add('S1', O + 20, reads=500, task='title', source='aux_hook')
    add('S1', O + 30, reads=200, sub='c1')
    add('S1', O + 900, status='pending')
    add('S1', O + 950, reads=900, status='pending')
    add('S1', O + 960, status='pending', task='title', source='aux_hook')
    add('S1', O + 970, reads=50, rc=3)  # aggregate predecessor
    add('S1', W, reads=400)  # ties at W
    add('S1', W, reads=410, sub='c1')
    add('S1', W + 5, reads=600, task='title', source='aux_hook')
    add('S1', W + 6, reads=700, project='p2')  # filtered-out neighbour
    add('S1', W + 7, reads=800)
    # S2: a stream whose only prior rows are pending; a never-seen stream.
    add('S2', O + 100, status='pending', model='m2')
    add('S2', O + 200, status='pending', model='m2')
    add('S2', O + 300, reads=77)
    add('S2', W + 1, reads=90, model='m2')
    add('S2', W + 2, reads=95, model='m3')
    add('S2', W + 3, reads=10)
    add('S3', O + 5, reads=1)
    for k in range(40):
        add('S3', O + 5 + k * 50, reads=(k * 37) % 300, task='h' if k % 3 == 0 else 'main',
            source='aux_hook' if k % 3 == 0 else 'main_hook',
            status=('pending' if k % 7 == 0 else 'completed') if k % 5 else 'failed',
            acct='a' if k % 4 else 'b', agent='subagent' if k % 6 == 0 else 'primary', project='p' + str(k % 3))
    for k in range(6):  # identical timestamps either side of W: id tie-break
        add('S4', W - 1 if k < 3 else W, reads=k * 100 if k != 2 else None,
            status='pending' if k == 2 else 'completed')
    add('S5', O + 50, reads=300)
    add('S5', O + 60)  # completed without usage: still a predecessor
    add('S5', W + 50, reads=310)
    rnd = random.Random(7)
    for _ in range(600):
        add('R' + str(rnd.randrange(12)), O + rnd.randrange(3000), reads=rnd.choice([None, 0, 100, 250, 1000, 5000]),
            status=rnd.choice(['completed'] * 5 + ['pending', 'failed']), task=rnd.choice(['main', 'main', 'h1', 'h2']),
            source=rnd.choice(['main_hook', 'aux_hook']), sub=rnd.choice(['', '', 'c1', 'c2']),
            model=rnd.choice(['m', 'm2']), acct=rnd.choice(['a', 'b']), rc=rnd.choice([None] * 8 + [2]),
            project=rnd.choice(['p1', 'p2', None]), agent=rnd.choice(['primary', 'subagent']),
            provider=rnd.choice(['synthetic', 'other']))
    return rows


@pytest.fixture(scope='module')
def adversarial(tmp_path_factory):
    root = tmp_path_factory.mktemp('adversarial')
    store = storage.Store(root)
    with store.db() as c:
        c.executemany('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                      [(r['id'], r['started'], r['ended'], r['provider'], r['model'], r['session_id'], r['task'],
                        None, r['status'], storage.jd(r)) for r in _adv_rows()])
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)", (ADV_ORIGIN + 1010, 'subagent_start', 'S1', '{}'))
        c.execute("INSERT INTO events(ts,kind,item_id,data) VALUES(?,?,?,?)", (ADV_ORIGIN + 1100, 'subagent_stop', 'S1', '{}'))
    return store


def _adv_read(store, start, end, filt, view, group):
    try:
        r = store.read(start=start, end=end, **filt, view=view, group=group,
                       _delta_seed=view is not None, _validate_costs=view is not None)
    except Exception as exc:  # errors must match too
        return 'ERR ' + type(exc).__name__ + ' ' + str(exc)
    r.pop('generated_at', None)
    return json.dumps(r, sort_keys=True, default=str)


@pytest.mark.parametrize('bounds', [(1000, 3100), (999, 3100), (500, 1500), (1990, 2010), (None, 3100), (1006, 1007)])
def test_adversarial_predecessor_parity(adversarial, tmp_path, monkeypatch, bounds):
    start = 0 if bounds[0] is None else ADV_ORIGIN + bounds[0]
    end = ADV_ORIGIN + bounds[1]
    monkeypatch.setattr(storage.time, 'time', lambda: ADV_ORIGIN + 5000)
    old = unindexed_copy(adversarial, tmp_path / 'old')
    filters = [{}, dict(project='p1'), dict(agent='subagent'), dict(session='S1'), dict(provider='other'), dict(model='m2')]
    views = [(None, None), ('overview', 'time'), ('overview', 'model'), ('overview', 'project')]
    actual = [_adv_read(adversarial, start, end, f, v, g) for f in filters for v, g in views]
    unindexed = [_adv_read(old, start, end, f, v, g) for f in filters for v, g in views]
    with reference(monkeypatch, (storage, session_cache_writes)):
        expected = [_adv_read(old, start, end, f, v, g) for f in filters for v, g in views]
    assert actual == expected
    assert unindexed == expected
    assert not any(r.startswith('ERR') for r in actual)


@pytest.mark.skipif(os.environ.get('HERMES_USAGE_READ_COST_SCALE') != '1',
                    reason='set HERMES_USAGE_READ_COST_SCALE=1 for the ~50k-row synthetic benchmark')
def test_scale_overview_time_parity_and_timing(tmp_path, monkeypatch, capsys):
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    n = int(os.environ.get('HERMES_USAGE_READ_COST_ROWS', '50000'))
    began = time.perf_counter()
    store = build(tmp_path / 'scale', n, pad=int(os.environ.get('HERMES_USAGE_READ_COST_PAD', '1200')))
    built = time.perf_counter() - began
    old = unindexed_copy(store, tmp_path / 'old')
    end = ORIGIN + (n // 2) * 10
    window = int(os.environ.get('HERMES_USAGE_READ_COST_WINDOW', str(n // 6)))
    runtime = AnalyticsRuntime()
    timings = {}
    try:
        prefix = runtime.current['store_type'].__module__.rsplit('.', 1)[0]
        modules = (sys.modules[prefix + '.storage'], sys.modules[prefix + '.session_cache_writes'])
        for label, (start, stop) in {'bounded': (end - (window // 2) * 10, end), 'unbounded': (0, end)}.items():
            positional = (start, stop, '', 0, 10, '', '', '', 'exact', '', '', '')
            t = time.perf_counter()
            actual = runtime.read(store.root, *positional, view='overview', group='time')
            timings[label + ' new'] = time.perf_counter() - t
            t = time.perf_counter()
            unindexed = runtime.read(old.root, *positional, view='overview', group='time')
            timings[label + ' new-unindexed'] = time.perf_counter() - t
            with reference(monkeypatch, modules):
                t = time.perf_counter()
                expected = runtime.read(old.root, *positional, view='overview', group='time')
                timings[label + ' reference'] = time.perf_counter() - t
            for result in (actual, unindexed, expected):
                result.pop('generated_at')
                result['incremental'].pop('resume_token', None)
            assert actual == expected
            assert unindexed == expected
            timings[label + ' rows'] = actual['request_count']
    finally:
        runtime._discard(runtime.current)
    with capsys.disabled():
        print(f'\nREAD-COST SCALE rows={n} window={window} build={built:.1f}s db={os.path.getsize(store.path)/1e6:.0f}MB',
              {k: round(v, 3) if isinstance(v, float) else v for k, v in timings.items()})
