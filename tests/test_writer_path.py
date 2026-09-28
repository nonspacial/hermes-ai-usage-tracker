"""Writer-path overhead reductions must not change what is recorded.

Synthetic homes only. The reference is the recorder at 93f5475 (before the
persistent writer, health cadence, running-total journal trim and notify
cache), extracted from git into tmp_path and loaded under a private name.
"""
import importlib
import importlib.util
import io
import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from _hermes_ai_usage_ledger_v2 import incremental as inc, recorder as r, storage
from _hermes_ai_usage_ledger_v2.storage import Store

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_COMMIT = '93f5475'
NEW = '_hermes_ai_usage_ledger_v2'
RAW = {'input_tokens': 100, 'output_tokens': 5, 'input_tokens_details': {'cached_tokens': 60, 'cache_write_tokens': 0}}
SYSTEM = ('You are a fixture.\n<available_skills>\n' +
          '\n'.join(f'  - skill-{i}: synthetic description {i}' for i in range(6)) + '\n</available_skills>')
MESSAGES = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': 'hello ' * 40}]
TABLES = ('requests', 'events', 'compressions', 'session_context', 'provider_catalog', 'pricing_status',
          'rates', 'tests', 'quota', 'skill_events', 'analytics_change_meta')


@pytest.fixture(scope='module')
def reference():
    """The 93f5475 ledger_runtime package, or skip when history is unavailable."""
    try:
        blob = subprocess.run(['git', '-C', str(ROOT), 'archive', REFERENCE_COMMIT, 'ledger_runtime'],
                              check=True, capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip('reference commit unavailable outside the repository')
    import tempfile
    folder = Path(tempfile.mkdtemp(prefix='writer-path-reference-'))
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        archive.extractall(folder, filter='data')
    name = '_writer_path_reference_ledger'
    package = folder / 'ledger_runtime'
    spec = importlib.util.spec_from_file_location(name, package / '__init__.py',
                                                  submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield name
    for key in [k for k in sys.modules if k == name or k.startswith(name + '.')]:
        sys.modules.pop(key, None)


BASE = float(int(time.time()) - 3600)


class Clock:
    """Deterministic wall clock: constant within a hook, advanced between hooks."""
    def __init__(self):
        self.now = BASE
    def __call__(self):
        return self.now
    def step(self, seconds=1.5):
        self.now += seconds


def profile(root):
    root.mkdir(parents=True)
    with sqlite3.connect(root / 'state.db') as c:
        c.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,parent_session_id TEXT,git_repo_root TEXT,cwd TEXT)')
        c.executemany('INSERT INTO sessions VALUES(?,?,?,?,?)', [
            ('sess', 'cli', None, None, '/synthetic/project/app'),
            ('child', 'subagent', 'sess', None, '/tmp/worktree')])
    with sqlite3.connect(root / 'projects.db') as c:
        c.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,primary_path TEXT,archived INTEGER)')
        c.execute('CREATE TABLE project_folders(project_id TEXT,path TEXT)')
        c.execute("INSERT INTO projects VALUES('p1','Fixture project','/synthetic/project',0)")
        c.execute("INSERT INTO project_folders VALUES('p1','/synthetic/project')")
    return root


def run_sequence(package, root, monkeypatch):
    """Drive one fixed hook sequence through ``package``'s recorder."""
    rec = importlib.import_module(package + '.recorder')
    adapters = importlib.import_module(package + '.adapters')
    skills = importlib.import_module(package + '.skills')
    ownership = importlib.import_module(package + '.ownership')
    clock = Clock()
    counter = iter(range(1, 10**6))
    with monkeypatch.context() as m:
        m.setattr(time, 'time', clock)
        m.setattr(uuid, 'uuid4', lambda: uuid.UUID(int=next(counter)))
        m.setattr(adapters, 'install', lambda: None)
        m.setattr(rec, 'PROCESS', 'fixture-process')
        m.setattr(ownership, '_GENERATION', (os.getpid(), 'a' * 32))
        m.setenv('HERMES_HOME', str(root))
        rec._STORES.clear(); rec._LOOKUP.clear(); rec._TURN_ROOTS.clear(); rec.ADAPTERS.clear()
        rec.CURRENT.set(None); rec.FAILURES = 0; rec.LAST_FAILURE_AT = 0
        getattr(rec, '_HEALTH', {}).clear()
        target = str(root.resolve())
        rec._REGISTERED_ROOTS.add(target)
        try:
            for i in range(7):
                clock.step()
                kw = dict(api_request_id=f'a{i}', session_id='sess', turn_id=f't{i // 3}', provider='openai-codex',
                          api_mode='codex_responses', model='gpt-5', platform='cli', started_at=clock.now,
                          approx_input_tokens=1000 + i, request_messages=MESSAGES, system_prompt=SYSTEM)
                rec.pre(**kw)
                clock.step()
                if i == 1:
                    skills.tool(tool_name='skill_view', tool_call_id='call-1', session_id='sess', turn_id='t0',
                                api_request_id='a1', args=json.dumps({'name': 'demo'}),
                                result=json.dumps({'success': True, 'content': 'x' * 400}))
                if i == 2:
                    rec.capture_raw({'usage': RAW}, source='native_terminal_usage')
                if i == 4:
                    rec.error(session_id='sess', api_request_id='a4', error={'type': 'TimeoutError'})
                    continue
                if i == 6:
                    break  # left open for the turn-end sweep
                rec.post(**kw, ended_at=clock.now, response_model='gpt-5-fixture',
                         usage={'input_tokens': 900, 'output_tokens': 50, 'cache_read_tokens': 600,
                                'cache_write_tokens': 0, 'reasoning_tokens': 0, 'prompt_tokens': 1500,
                                'total_tokens': 1550})
                if i == 2:
                    clock.step(20)  # beyond one heartbeat interval
                    rec.session_end(session_id='sess', turn_id='t0', completed=True, turn_exit_reason='completed')
            clock.step()
            rec.subagent_start(child_session_id='child', parent_session_id='sess', parent_turn_id='t2',
                               child_role='worker', child_subagent_id='sub-1')
            clock.step()
            rec.subagent_stop(child_session_id='child', parent_session_id='sess', child_status='ok', duration_ms=5)
            clock.step()
            rec.session_end(session_id='sess', turn_id='t2', interrupted=True, turn_exit_reason='interrupted')
            failures = rec.FAILURES
        finally:
            rec._REGISTERED_ROOTS.discard(target)
            for s in rec._STORES.values():
                getattr(s, 'close_writer', lambda: None)()
            rec._STORES.clear()
    assert failures == 0
    return root / 'usage-ledger' / 'events.sqlite3'


def dump(path):
    with sqlite3.connect(path) as c:
        out = {t: c.execute(f'SELECT * FROM {t} ORDER BY rowid').fetchall() for t in TABLES}
        health = []
        for process, updated, data in c.execute('SELECT process,updated,data FROM health ORDER BY process'):
            data = json.loads(data)
            health.append((process, data.pop('heartbeat_at') is None, data))
        out['health'] = health
        # Health journal entries are exactly what the cadence removes; every
        # other journal row must match in content and order.
        out['journal'] = c.execute("SELECT kind,item_id,old_row,new_row FROM analytics_changes "
                                   "WHERE kind!='health' ORDER BY revision").fetchall()
        health_journal = c.execute("SELECT COUNT(*) FROM analytics_changes WHERE kind='health'").fetchone()[0]
    return out, health_journal


def test_recorded_content_matches_reference_recorder(tmp_path, monkeypatch, reference):
    old_path = run_sequence(reference, profile(tmp_path / 'old'), monkeypatch)
    new_path = run_sequence(NEW, profile(tmp_path / 'new'), monkeypatch)
    old, old_health_writes = dump(old_path)
    new, new_health_writes = dump(new_path)
    assert old['requests'] and old['skill_events'] and old['events'] and old['journal']
    assert {json.loads(row[-1])['project_id'] for row in old['requests']} == {'p1'}
    for table in (*TABLES, 'health', 'journal'):
        assert new[table] == old[table], table
    # Fewer lease writes (the point of the cadence), never none.
    assert 0 < new_health_writes < old_health_writes


def test_usage_enum_metadata_differs_only_where_reference_sentinelled(reference):
    """The only permitted writer difference from 93f5475: usage-enum values the
    reference collapsed to 'unrecognised' are now kept verbatim when they are
    bounded content-free enum strings (fail-open pricing needs the value).
    Every documented value, and every non-string/oversized/odd value, is
    byte-identical."""
    old = importlib.import_module(reference + '.recorder')
    values = ['fast', 'standard', 'global', 'us', 'not_available', 'priority', 'batch', 'eu', 'turbo',
              'NOT_AVAILABLE', 'not-available', 'x' * 64, 'x' * 65, '', ' us', 'a/b', 'é', 7, True, None, {'k': 1}]
    changed = 0
    for key in ('speed', 'inference_geo', 'service_tier'):
        for value in values:
            usage = {'input_tokens': 1, key: value}
            before = old.response_metadata({'model': 'm', 'id': 'i', 'usage': usage})
            after = r.response_metadata({'model': 'm', 'id': 'i', 'usage': usage})
            if before == after:
                continue
            field = {'speed': 'returned_speed', 'inference_geo': 'returned_inference_geo',
                     'service_tier': 'returned_usage_service_tier'}[key]
            assert before.pop(field) == 'unrecognised' and after.pop(field) == value, (key, value)
            assert before == after
            changed += 1
    assert changed > 0


def test_endpoint_class_is_identical_to_reference(reference):
    """No writer difference outside usage-enum values: the endpoint class the
    recorder stores is byte-identical to 93f5475 for every provider and host,
    including loopback/private hosts (no endpoint host class is recorded)."""
    old = importlib.import_module(reference + '.recorder')
    urls = [None, '', 'http://127.0.0.1:11434/v1', 'http://localhost:1234/v1', 'http://192.168.1.20:8000',
            'http://gpu-box.local:8080', 'http://host.docker.internal:11434', 'https://api.anthropic.com',
            'https://api.anthropic.com.', 'https://gateway.example/anthropic', 'https://api.groq.com/openai/v1']
    for provider in ('anthropic', 'custom', 'ollama', 'openrouter', 'copilot', 'xai', 'openai-codex', ''):
        for url in urls:
            kw = {'provider': provider, 'base_url': url}
            assert r.endpoint_class(kw) == old.endpoint_class(kw), kw


def test_health_cadence_keeps_lease_and_writes_state_changes(tmp_path, monkeypatch):
    from _hermes_ai_usage_ledger_v2 import adapters, connection
    clock = Clock()
    monkeypatch.setattr(time, 'time', clock)
    monkeypatch.setattr(adapters, 'install', lambda: None)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    r._STORES.clear(); r._HEALTH.clear(); r.ADAPTERS.clear(); r.FAILURES = 0; r.LAST_FAILURE_AT = 0
    target = str(tmp_path.resolve())
    s = r.store(target)
    writes = []
    original = s.health
    monkeypatch.setattr(s, 'health', lambda process, data: (writes.append((clock.now, data)), original(process, data)))
    r._REGISTERED_ROOTS.add(target)
    try:
        r.health()                        # first hook after start: written
        clock.step(1); r.health()         # unchanged, fresh: skipped
        clock.step(1); r.health(force=True)  # heartbeat thread: always written
        clock.step(1); r.ADAPTERS['x'] = 'registered'; r.health()   # state change: written
        clock.step(1); r.FAILURES += 1; r.LAST_FAILURE_AT = clock.now; r.health()  # failure: written
        clock.step(connection.HEARTBEAT_SECONDS); r.health()        # interval elapsed: written
        assert [w[0] - writes[0][0] for w in writes] == [0, 2, 3, 4, 4 + connection.HEARTBEAT_SECONDS]
        keys = set(writes[0][1])
        assert all(set(data) == keys and data['heartbeat_at'] == at for at, data in writes)
        # Between writes the lease never ages past a heartbeat interval.
        assert connection.HEARTBEAT_SECONDS < connection.LEASE_SECONDS
        assert connection.status(s, clock.now)['state'] == 'limited'   # recent failure, still live
    finally:
        r._REGISTERED_ROOTS.discard(target); r._STORES.clear(); r._HEALTH.clear(); r.FAILURES = 0; r.ADAPTERS.clear()
        s.close_writer()


@pytest.fixture
def short_root():
    """AF_UNIX paths are limited to ~107 bytes; pytest's tmp_path is often longer."""
    import shutil
    import tempfile
    root = Path(tempfile.mkdtemp(prefix='w'))
    if len(str(root / 'usage-ledger' / f'notify-{2**22}-abcdef12.sock').encode()) >= 100:
        shutil.rmtree(root)
        pytest.skip('temporary directory path too long for AF_UNIX sockets')
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _bind(folder, pid=None):
    path = folder / f'notify-{pid or os.getpid()}-{uuid.uuid4().hex[:8]}.sock'
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path)); sock.settimeout(2)
    return sock, path


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='unix sockets unavailable')
def test_notify_reaches_every_live_listener_after_each_write(short_root):
    s = Store(short_root)
    first, first_path = _bind(s.folder)
    try:
        s.request({'id': 'one', 'started': 1, 'session_id': 's'}, 'request_started')
        assert first.recv(64) == b'changed'
        # Let the listing become cacheable, then prove a new listener still hears.
        old = time.time() - 60
        os.utime(s.folder, (old, old))
        s.request({'id': 'two', 'started': 2, 'session_id': 's'}, 'request_started')
        assert first.recv(64) == b'changed'
        assert storage._NOTIFY[str(s.folder)][2] == [str(first_path)]
        second, second_path = _bind(s.folder)
        try:
            s.test('start', 'marker')
            assert first.recv(64) == b'changed' and second.recv(64) == b'changed'
        finally:
            second.close(); second_path.unlink()
        # A listener that vanished between writes drops the cache.
        s.request({'id': 'three', 'started': 3, 'session_id': 's'}, 'request_started')
        assert first.recv(64) == b'changed'
        s.request({'id': 'four', 'started': 4, 'session_id': 's'}, 'request_started')
        assert first.recv(64) == b'changed'
    finally:
        first.close(); first_path.unlink(missing_ok=True)


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='unix sockets unavailable')
def test_notify_unlinks_only_refused_sockets_of_dead_owners(short_root):
    s = Store(short_root)
    dead = subprocess.run([sys.executable, '-c', 'import os;print(os.getpid())'],
                          capture_output=True, text=True, check=True)
    dead_pid = int(dead.stdout)
    orphan, orphan_path = _bind(s.folder, dead_pid)
    orphan.close()                       # file left behind, nothing bound
    live_owner, live_owner_path = _bind(s.folder)
    live_owner.close()                   # refused, but its (live) owner cleans up
    listener, listener_path = _bind(s.folder)
    try:
        s.request({'id': 'x', 'started': 1, 'session_id': 's'}, 'request_started')
        assert listener.recv(64) == b'changed'
        assert not orphan_path.exists()
        assert live_owner_path.exists()
    finally:
        listener.close(); listener_path.unlink(); live_owner_path.unlink(missing_ok=True)


def _journal(triggers, current):
    c = sqlite3.connect(':memory:')
    c.executescript(storage.SCHEMA)
    from _hermes_ai_usage_ledger_v2.skills import SCHEMA as SKILLS
    c.executescript(SKILLS)
    c.execute(inc.META_SQL); c.execute("INSERT INTO analytics_change_meta VALUES(2,?)", ('0' * 32,))
    c.execute(inc.CHANGES_SQL)
    if current:
        c.execute(inc.BYTES_SQL); c.execute('INSERT INTO analytics_change_bytes VALUES(0)')
    for sql in triggers.values():
        c.execute(sql)
    return c


def test_journal_retention_identical_to_window_sum_trim():
    old, new = _journal(inc.LEGACY_TRIGGERS, False), _journal(inc.TRIGGERS, True)
    rnd = random.Random(20260927)
    size = f"SUM({inc._size('')})"
    bound = []
    for i in range(6000):
        # Phases where the row cap binds, then the byte cap, then both.
        pad = 20 if i < 4400 else rnd.choice([20, 20, 400, 3000, 16000, 31000])
        row = json.dumps({'id': f'r{i % 900}', 'pad': 'x' * pad})
        for c in (old, new):
            c.execute('INSERT INTO requests VALUES(?,1,NULL,"p","m","s","main",NULL,"completed",?) '
                      'ON CONFLICT(id) DO UPDATE SET data=excluded.data', (f'r{i % 900}', row))
            if i % 1000 == 999:  # non-request writer too
                c.execute("INSERT INTO health VALUES('p',1,'{}') ON CONFLICT(process) DO UPDATE SET updated=updated+1")
        if i % 500 == 499 or i == 5999:
            query = 'SELECT revision,kind,item_id,old_row,new_row FROM analytics_changes ORDER BY revision'
            assert new.execute(query).fetchall() == old.execute(query).fetchall(), i
            count, total = new.execute(f'SELECT COUNT(*),{size} FROM analytics_changes').fetchone()
            assert new.execute('SELECT total FROM analytics_change_bytes').fetchone()[0] == total
            assert count <= inc.CAPACITY and total <= inc.MAX_JOURNAL_BYTES
            bound.append((count, total))
    assert any(count == inc.CAPACITY for count, _ in bound)            # row bound exercised
    assert any(total > inc.MAX_JOURNAL_BYTES * .9 and count < inc.CAPACITY for count, total in bound)
    # Raw journal edits keep the running total exact.
    new.execute('DELETE FROM analytics_changes WHERE revision % 7 = 0')
    new.execute("UPDATE analytics_changes SET item_id='changed' WHERE revision % 5 = 0")
    assert new.execute('SELECT total FROM analytics_change_bytes').fetchone()[0] == \
        new.execute(f'SELECT {size} FROM analytics_changes').fetchone()[0]


def test_new_and_reference_journals_are_supported_but_never_mixed(tmp_path, reference):
    old_storage = importlib.import_module(reference + '.storage')
    legacy = old_storage.Store(tmp_path / 'legacy')          # journalled by 93f5475
    with sqlite3.connect(legacy.path) as c:
        assert inc.supported(c)
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_change_bytes'").fetchone()
    Store(tmp_path / 'legacy')                              # reopening never rewrites it
    with sqlite3.connect(legacy.path) as c:
        assert inc.supported(c)
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_change_bytes'").fetchone()
        c.execute(inc.BYTES_SQL); c.execute('INSERT INTO analytics_change_bytes VALUES(0)')
        assert not inc.supported(c)                         # legacy triggers + running total
    current = Store(tmp_path / 'current')
    with sqlite3.connect(current.path) as c:
        assert inc.supported(c)
        c.execute('DROP TRIGGER analytics_journal_bytes_delete')
        assert not inc.supported(c)
    with sqlite3.connect(current.path) as c:
        c.execute(inc.TRIGGERS['analytics_journal_bytes_delete'])
        c.execute('INSERT INTO analytics_change_bytes VALUES(0)')
        assert not inc.supported(c)                         # two total rows


def test_persistent_writer_reuse_reconnect_and_threads(tmp_path, monkeypatch):
    s = Store(tmp_path)
    s.persistent = True
    opened = []
    original = s._connect
    monkeypatch.setattr(s, '_connect', lambda **kw: (opened.append(kw), original(**kw))[1])
    try:
        for i in range(5):
            s.request({'id': f'r{i}', 'started': i, 'session_id': 's'}, 'request_started')
        assert opened == [{'shared': True}]
        with s.db() as c:
            assert c.execute('PRAGMA synchronous').fetchone()[0] == 2   # FULL by default
        # A read's TEMP projection must not leak into later writes.
        assert s.read(0, 10)['request_count'] == 5
        s.request({'id': 'after-read', 'started': 6, 'session_id': 's'}, 'request_started')
        assert len([k for k in opened if k.get('shared')]) == 2
        # Errors roll back and reconnect.
        with pytest.raises(sqlite3.OperationalError):
            with s.db() as c:
                c.execute("INSERT INTO requests VALUES('lost',1,NULL,'p','m','s','main',NULL,'pending','{}')")
                c.execute('SELECT * FROM missing_table')
        with s.db() as c:
            assert c.execute("SELECT COUNT(*) FROM requests WHERE id='lost'").fetchone()[0] == 0
        # Concurrent threads: all writes land; a busy handle falls back to a fresh one.
        errors = []
        def write(n):
            try:
                for j in range(20):
                    s.request({'id': f't{n}-{j}', 'started': j, 'session_id': 's'}, 'request_started')
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        [t.start() for t in threads]; [t.join() for t in threads]
        assert not errors
        with sqlite3.connect(s.path) as c:
            assert c.execute("SELECT COUNT(*) FROM requests WHERE id LIKE 't%-%'").fetchone()[0] == 80
        # A replaced database file is never written through a stale handle
        # (no close_writer() first; see also the rollback test below).
        replacement = s.path.with_name('replacement.sqlite3')
        with sqlite3.connect(s.path) as source, sqlite3.connect(replacement) as target:
            source.backup(target)
        os.replace(replacement, s.path)
        s.request({'id': 'on-replacement', 'started': 1, 'session_id': 's'}, 'request_started')
        with sqlite3.connect(s.path) as c:
            assert c.execute("SELECT COUNT(*) FROM requests WHERE id='on-replacement'").fetchone()[0] == 1
    finally:
        s.close_writer()


def _wal(s):
    wal = Path(str(s.path) + '-wal')
    return wal.stat().st_size if wal.exists() else None


def _integrity_and_count(path):
    with sqlite3.connect(path) as c:
        return (c.execute('PRAGMA integrity_check').fetchone()[0],
                c.execute('SELECT COUNT(*) FROM requests').fetchone()[0])


def test_main_file_rollback_under_live_producer_is_not_undone(tmp_path):
    """Between writes the main file holds every commit and the WAL is empty,
    so restoring an older main file under a live persistent handle is a clean
    rollback: nothing stale is replayed over it (as with per-write close)."""
    s = Store(tmp_path)
    s.persistent = True
    try:
        for i in range(60):
            s.request({'id': f'r{i}', 'started': i, 'session_id': 's', 'pad': 'x' * 3000}, 'request_started')
        backup = s.path.with_name('backup.sqlite3')
        with sqlite3.connect(s.path) as a, sqlite3.connect(backup) as b:
            a.backup(b)
        for i in range(60, 80):
            s.request({'id': f'r{i}', 'started': i, 'session_id': 's', 'pad': 'y' * 3000}, 'request_started')
        assert s.__dict__['_writer'] is not None           # handle still open: no close_writer()
        assert _wal(s) in (None, 0)
        # A main-file-only copy taken now is complete.
        copy = s.path.with_name('copy.sqlite3')
        import shutil
        shutil.copyfile(s.path, copy)
        assert _integrity_and_count(copy) == ('ok', 80)
        os.replace(backup, s.path)
        assert _integrity_and_count(s.path) == ('ok', 60)   # fresh reader, producer still live
        s.request({'id': 'after', 'started': 1, 'session_id': 's'}, 'request_started')
        assert _integrity_and_count(s.path) == ('ok', 61)
    finally:
        s.close_writer()
    assert _integrity_and_count(s.path) == ('ok', 61)


def test_hook_settles_wal_once_at_its_end(tmp_path, monkeypatch):
    s = Store(tmp_path)
    s.persistent = True
    settled = []
    original = storage._checkpoint
    monkeypatch.setattr(storage, '_checkpoint', lambda c, *a: (settled.append(1), original(c, *a))[1])
    try:
        with storage.hook_scope():
            s.request({'id': 'a', 'started': 1, 'session_id': 's'}, 'request_started')
            with storage.hook_scope():                     # nested hook: same unit
                s.request({'id': 'b', 'started': 2, 'session_id': 's'}, 'request_started')
            assert _wal(s) > 0 and not settled             # one burst shares the handle
        assert settled == [1] and _wal(s) == 0
        s.request({'id': 'c', 'started': 3, 'session_id': 's'}, 'request_started')  # outside a hook
        assert settled == [1, 1] and _wal(s) == 0
        assert s.__dict__['_writer'] is not None
    finally:
        s.close_writer()


def test_failing_context_reset_still_ends_the_hook_unit(tmp_path, monkeypatch):
    """A hook exited in another Context (ContextVar.reset raises ValueError)
    must still end its unit; otherwise _units sticks >0 and the WAL is never
    truncated again."""
    import contextvars
    s = Store(tmp_path)
    s.persistent = True
    settled = []
    original = storage._checkpoint
    monkeypatch.setattr(storage, '_checkpoint', lambda c, *a: (settled.append(1), original(c, *a))[1])

    def scenario():                                        # confine the leaked vars
        scope = storage.hook_scope()
        scope.__enter__()
        s.request({'id': 'a', 'started': 1, 'session_id': 's'}, 'request_started')
        assert s.__dict__['_units'] == 1 and _wal(s) > 0
        with pytest.raises(ValueError):
            contextvars.copy_context().run(scope.__exit__, None, None, None)
        assert s.__dict__['_units'] == 0 and settled == [1] and _wal(s) == 0
        # The leaked, closed unit set no longer captures later writes.
        s.request({'id': 'b', 'started': 2, 'session_id': 's'}, 'request_started')
        assert s.__dict__['_units'] == 0 and settled == [1, 1] and _wal(s) == 0
    try:
        contextvars.copy_context().run(scenario)
    finally:
        s.close_writer()
    assert _integrity_and_count(s.path) == ('ok', 2)


def test_only_the_last_concurrent_unit_settles(tmp_path, monkeypatch):
    """Like the former 'last close checkpoints': a hook ending while another
    thread's hook is still in progress does not checkpoint under it."""
    import contextvars
    s = Store(tmp_path)
    s.persistent = True
    settled = []
    original = storage._checkpoint
    monkeypatch.setattr(storage, '_checkpoint', lambda c, *a: (settled.append(1), original(c, *a))[1])
    inside, release = threading.Event(), threading.Event()
    def long_hook():
        with storage.hook_scope():
            s.request({'id': 'long', 'started': 1, 'session_id': 's'}, 'request_started')
            inside.set(); release.wait(10)
    t = threading.Thread(target=long_hook); t.start()
    try:
        assert inside.wait(5)
        with storage.hook_scope():
            s.request({'id': 'short', 'started': 2, 'session_id': 's'}, 'request_started')
        assert not settled and _wal(s) > 0                 # the long hook is still a live unit
    finally:
        release.set(); t.join()
    assert settled == [1] and _wal(s) == 0                  # settled by the last unit to end
    # A copied hook context used after its hook returned is its own unit.
    with storage.hook_scope():
        ctx = contextvars.copy_context()
    ctx.run(lambda: s.request({'id': 'late', 'started': 3, 'session_id': 's'}, 'request_started'))
    assert settled == [1, 1] and _wal(s) == 0 and s.__dict__['_units'] == 0
    s.close_writer()
    assert _integrity_and_count(s.path) == ('ok', 3)


def test_idle_handle_is_closed_and_sidecars_removed(tmp_path, monkeypatch):
    """At rest (IDLE_SECONDS after the last use) there is no open handle and
    no -wal/-shm, exactly as with per-write close; a burst shares one handle."""
    monkeypatch.setattr(storage, 'IDLE_SECONDS', 0.2)
    s = Store(tmp_path)
    s.persistent = True
    opened = []
    original = s._connect
    monkeypatch.setattr(s, '_connect', lambda **kw: (opened.append(kw), original(**kw))[1])
    try:
        for i in range(5):
            with storage.hook_scope():
                s.request({'id': f'r{i}', 'started': i, 'session_id': 's'}, 'request_started')
        assert opened == [{'shared': True}]                 # burst: one handle
        deadline = time.monotonic() + 3
        while s.__dict__.get('_writer') is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert s.__dict__.get('_writer') is None
        assert not Path(str(s.path) + '-wal').exists() and not Path(str(s.path) + '-shm').exists()
        # Sidecar housekeeping at rest (as before) cannot break the next write.
        s.request({'id': 'next', 'started': 9, 'session_id': 's'}, 'request_started')
        assert len(opened) == 2 and _integrity_and_count(s.path) == ('ok', 6)
    finally:
        s.close_writer()


def test_settle_never_recreates_a_removed_ledger(tmp_path):
    s = Store(tmp_path)
    s.persistent = True
    s.request({'id': 'a', 'started': 1, 'session_id': 's'}, 'request_started')
    s.close_writer()
    for suffix in ('', '-wal', '-shm'):
        Path(str(s.path) + suffix).unlink(missing_ok=True)
    assert s._settle() is True
    s.settle()
    assert not s.path.exists()


def test_settle_blocked_by_reader_is_retried_off_the_hook_path(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SETTLE_RETRY_SECONDS', 0.1)
    s = Store(tmp_path)
    s.persistent = True
    try:
        s.request({'id': 'a', 'started': 1, 'session_id': 's'}, 'request_started')
        # A reader holding an old snapshot blocks the write's non-waiting settle.
        reader = sqlite3.connect(s.path, isolation_level=None)
        reader.execute('BEGIN'); reader.execute('SELECT COUNT(*) FROM requests').fetchone()
        t = time.monotonic()
        s.request({'id': 'b', 'started': 2, 'session_id': 's'}, 'request_started')
        assert time.monotonic() - t < 0.5                  # the write never waited on the reader
        assert _wal(s) > 0
        reader.execute('COMMIT'); reader.close()
        deadline = time.monotonic() + 3
        while _wal(s) not in (None, 0) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _wal(s) in (None, 0)                         # truncated once the reader let go
        assert _integrity_and_count(s.path) == ('ok', 2)
    finally:
        s.close_writer()


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='fork unavailable')
def test_fork_child_does_not_inherit_held_module_locks(tmp_path):
    import warnings
    s = Store(tmp_path)
    s.persistent = True
    s.request({'id': 'parent', 'started': 1, 'session_id': 's'}, 'request_started')   # registers fork hook
    held, release = threading.Event(), threading.Event()
    def hold():
        with r._LOCK, storage.LOCK, storage._WRITERS_LOCK, storage._NOTIFY_LOCK, storage._IDLE_COND:
            held.set(); release.wait(10)
    holder = threading.Thread(target=hold); holder.start()
    assert held.wait(5)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            pid = os.fork()
        if pid == 0:                                        # pragma: no cover - child
            code = 1
            try:
                r.store(str(tmp_path))                      # recorder._LOCK
                s.request({'id': 'child', 'started': 2, 'session_id': 's'}, 'request_started')
                code = 0
            finally:
                os._exit(code)
        deadline = time.monotonic() + 30
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done: break
            if time.monotonic() > deadline:
                os.kill(pid, 9); os.waitpid(pid, 0)
                pytest.fail('forked child deadlocked on an inherited lock')
            time.sleep(0.05)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        release.set(); holder.join()
        s.close_writer()
    with sqlite3.connect(s.path) as c:
        assert {r[0] for r in c.execute('SELECT id FROM requests')} == {'parent', 'child'}
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_concurrent_hook_threads_with_heartbeat(tmp_path, monkeypatch):
    """Reviewer scenario, bounded: 4 threads x 40 hooks, heartbeat every 50 ms."""
    from _hermes_ai_usage_ledger_v2 import adapters, connection
    monkeypatch.setattr(adapters, 'install', lambda: None)
    monkeypatch.setattr(connection, 'HEARTBEAT_SECONDS', 0.05)
    monkeypatch.setattr(storage, 'SETTLE_RETRY_SECONDS', 0.1)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    r._STORES.clear(); r._HEALTH.clear(); r.ADAPTERS.clear(); r.FAILURES = 0; r.LAST_FAILURE_AT = 0
    threads_n, hooks = 4, 40
    errors = []
    def worker(n):
        try:
            for j in range(hooks):
                kw = dict(api_request_id=f'a{n}-{j}', session_id=f's{n}', turn_id=f't{n}-{j // 10}',
                          provider='openai-codex', api_mode='codex_responses', model='m', platform='cli',
                          started_at=time.time())
                r.pre(**kw)
                if j % 3 == 0:
                    r.capture_raw({'usage': {'input_tokens': 10, 'output_tokens': 2}}, source='native_terminal_usage')
                if j % 7 == 0:
                    r.error(session_id=f's{n}', api_request_id=f'a{n}-{j}', error={'type': 'TimeoutError'})
                else:
                    r.post(**kw, ended_at=time.time(), usage={'input_tokens': 9, 'output_tokens': 1, 'total_tokens': 10})
                if j % 10 == 9:
                    r.session_end(session_id=f's{n}', turn_id=f't{n}-{j // 10}', completed=True)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))
    r.start_heartbeat(str(tmp_path))
    try:
        workers = [threading.Thread(target=worker, args=(n,)) for n in range(threads_n)]
        [t.start() for t in workers]; [t.join() for t in workers]
    finally:
        r.stop_heartbeats()
    s = r.store(str(tmp_path.resolve()))
    try:
        assert not errors
        time.sleep(0.3)                                     # heartbeat thread observes stop
        deadline = time.monotonic() + 3                     # a contended settle is retried
        while _wal(s) not in (None, 0) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _wal(s) in (None, 0)                         # at rest after the burst
        failed = sum(1 for j in range(hooks) if j % 7 == 0) * threads_n
        with sqlite3.connect(s.path) as c:
            assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert dict(c.execute('SELECT status,COUNT(*) FROM requests GROUP BY status').fetchall()) == {
                'completed': threads_n * hooks - failed, 'failed': failed}
            assert inc.supported(c)
            size = f"COALESCE(SUM({inc._size('')}),0)"
            assert c.execute('SELECT total FROM analytics_change_bytes').fetchone()[0] == \
                c.execute(f'SELECT {size} FROM analytics_changes').fetchone()[0]
    finally:
        # Contended hooks may log 'database is locked' (as before this change);
        # never leak that failure state into later health/status tests.
        r._STORES.clear(); r._HEALTH.clear(); r.FAILURES = 0; r.LAST_FAILURE_AT = 0; r.ADAPTERS.clear()
        s.close_writer()


def test_synchronous_normal_is_explicit_opt_in(tmp_path, monkeypatch):
    s = Store(tmp_path)
    s.persistent = True
    for value, expected in (('', 2), ('normal', 1), ('OFF', 2), ('FULL', 2)):
        monkeypatch.setenv('HERMES_USAGE_LEDGER_SYNCHRONOUS', value)
        s.close_writer()
        with s.db() as c:
            assert c.execute('PRAGMA synchronous').fetchone()[0] == expected, value
    s.close_writer()


def test_producer_exit_checkpoints_and_keeps_every_commit(tmp_path):
    script = f'''
import os, sys, importlib.util
os.environ['HERMES_USAGE_PRICING_OFFLINE']='1'
os.environ['HERMES_HOME']={str(tmp_path)!r}
spec=importlib.util.spec_from_file_location('_boot',{str(ROOT / 'bootstrap.py')!r})
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from _hermes_ai_usage_ledger_v2 import recorder as r, adapters as ad
ad.install=lambda:None
for i in range(3):
    r.pre(api_request_id='a'+str(i),session_id='s',turn_id='t',provider='openai-codex',model='m',started_at=1000+i)
r.session_end(session_id='s',turn_id='t',completed=True)
assert r.store()._writer is not None
'''
    subprocess.run([sys.executable, '-c', script], check=True, timeout=60, cwd=tmp_path)
    ledger = tmp_path / 'usage-ledger' / 'events.sqlite3'
    wal = Path(str(ledger) + '-wal')
    assert not wal.exists() or wal.stat().st_size == 0       # closed at exit
    with sqlite3.connect(ledger) as c:
        rows = c.execute("SELECT status FROM requests").fetchall()
    assert rows == [('ended_without_usage',)] * 3
