"""Selected SQLite backup versus strict aggregate copy on disposable WAL ledgers."""
import json
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from ledger_runtime import aggregate, read_snapshot
from ledger_runtime.api import add_routes
from ledger_runtime.storage import SCHEMA, Store

QUERY = {'start': 100, 'end': 50000, 'view': 'overview', 'group': 'time'}


def client_for(root):
    router = APIRouter()
    add_routes(router, lambda profile: (root, 'fixture', None), lambda: root)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def insert(connection, number):
    started = 200 + number
    body = {'id': f'w{number}', 'started': started, 'ended': started + .1,
            'provider': 'fixture', 'model': 'model', 'session_id': 'fixture',
            'source': 'main_hook', 'task': 'main', 'status': 'completed',
            'usage': {'total_tokens': 1}}
    connection.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (body['id'], started, started + .1, 'fixture', 'model',
                        'fixture', 'main', None, 'completed', json.dumps(body)))
    connection.commit()


def test_active_wal_checkpoint_old_busy_selected_get_and_post_coherent(tmp_path, monkeypatch):
    store = Store(tmp_path)
    writer = sqlite3.connect(store.path, check_same_thread=False, timeout=2)
    writer.execute('PRAGMA journal_mode=WAL')
    insert(writer, 0)  # committed WAL, not necessarily in the main file
    assert Path(str(store.path) + '-wal').stat().st_size > 0
    real_copy = read_snapshot.shutil.copyfile
    copied = [0]
    def raced_copy(source, target):
        result = real_copy(source, target)
        # A checkpoint/reset and subsequent commit on every raw-copy attempt
        # changes the signed files between before/after, deterministically.
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        copied[0] += 1
        insert(writer, copied[0])
        return result
    monkeypatch.setattr(read_snapshot.shutil, 'copyfile', raced_copy)
    with pytest.raises(read_snapshot.SnapshotBusy):
        with read_snapshot.snapshot_root(tmp_path):
            pass
    monkeypatch.setattr(read_snapshot.shutil, 'copyfile', real_copy)
    assert copied[0] >= 3
    client = client_for(tmp_path)
    baseline = client.get('/ledger', params=QUERY)
    assert baseline.status_code == 200
    token = baseline.json()['incremental']['resume_token']
    initial_count = writer.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
    stop = threading.Event()
    running = threading.Event()
    errors = []
    written = [0]
    def writes():
        try:
            with closing(sqlite3.connect(store.path, timeout=2)) as connection:
                n = 10
                while not stop.is_set():
                    insert(connection, n)
                    written[0] += 1
                    connection.execute('PRAGMA wal_checkpoint(PASSIVE)')
                    n += 1
                    running.set()
                    time.sleep(.002)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=writes)
    thread.start()
    try:
        assert running.wait(2)
        before = writer.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
        response = client.get('/ledger', params=QUERY)
        assert response.status_code == 200, response.text
        report = response.json()
        assert report['request_count'] == report['summary']['attempts']
        assert report['request_count'] >= before
        # A moving source can intentionally suppress a signed GET receipt.
        # POST uses the prior stable signed baseline and remains coherent.
        post = client.post('/ledger/refresh', params=QUERY, json={'resume_token': token})
        assert post.status_code == 200, post.text
        updated = post.json()
        assert updated['request_count'] == updated['summary']['attempts']
        assert updated['request_count'] >= report['request_count']
        assert writer.execute('SELECT COUNT(*) FROM requests').fetchone()[0] >= updated['request_count']
        assert thread.is_alive() and not errors
    finally:
        stop.set()
        thread.join(timeout=2)
        assert writer.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == initial_count + written[0]
        writer.close()
    assert not thread.is_alive() and not errors


def test_selected_missing_db_and_backup_failure_cleanup(tmp_path, monkeypatch):
    empty = tmp_path / 'absent'
    with pytest.raises(read_snapshot.SnapshotUnavailable):
        with read_snapshot.selected_snapshot_root(empty):
            pass
    assert not (empty / 'usage-ledger' / 'events.sqlite3').exists()
    store = Store(tmp_path)
    original_connect = sqlite3.connect
    original_temp = read_snapshot.tempfile.TemporaryDirectory
    roots, closed = [], []
    class Connection:
        def __init__(self, real, source):
            self.real, self.source = real, source
        def backup(self, target, **kwargs):
            assert self.source
            kwargs['progress'](sqlite3.SQLITE_BUSY, 1, 2)
            pytest.fail('busy backup should have been cancelled at deadline')
        def close(self):
            closed.append(self.source)
            self.real.close()
    def connect(path, *args, **kwargs):
        is_source = isinstance(path, str) and 'mode=ro' in path
        return Connection(original_connect(path, *args, **kwargs), is_source)
    def temp(*args, **kwargs):
        kwargs['dir'] = tmp_path
        folder = original_temp(*args, **kwargs)
        roots.append(Path(folder.name))
        return folder
    monkeypatch.setattr(read_snapshot.sqlite3, 'connect', connect)
    monkeypatch.setattr(read_snapshot.tempfile, 'TemporaryDirectory', temp)
    clock = iter((0, 2))
    monkeypatch.setattr(read_snapshot.time, 'monotonic', lambda: next(clock))
    with pytest.raises(read_snapshot.SnapshotBusy):
        with read_snapshot.selected_snapshot_root(tmp_path):
            pass
    assert closed == [False, True]
    assert roots and not roots[0].exists()
    assert store.path.is_file()


@pytest.mark.parametrize('failure', ['allocation', 'mkdir'])
@pytest.mark.parametrize('route', ['get', 'post'])
def test_selected_scratch_failure_routes_are_sanitized_503(tmp_path, monkeypatch, failure, route):
    store = Store(tmp_path)
    original_temp = read_snapshot.tempfile.TemporaryDirectory
    original_mkdir = Path.mkdir
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    client = client_for(tmp_path)
    def temp(*args, **kwargs):
        if kwargs.get('prefix') != 'usage-selected-snapshot-':
            return original_temp(*args, **kwargs)
        if failure == 'allocation':
            raise OSError(28, 'private scratch path must not leak')
        kwargs['dir'] = scratch
        return original_temp(*args, **kwargs)
    def mkdir(path, *args, **kwargs):
        if path.name == 'usage-ledger' and path.parent.name.startswith('usage-selected-snapshot-'):
            raise PermissionError('private ledger path must not leak')
        return original_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(read_snapshot.tempfile, 'TemporaryDirectory', temp)
    if failure == 'mkdir':
        monkeypatch.setattr(Path, 'mkdir', mkdir)
    response = (client.get('/ledger', params=QUERY) if route == 'get' else
                client.post('/ledger/refresh', params=QUERY, json={}))
    assert response.status_code == 503
    assert response.json() == {'detail': 'Selected analytics snapshot unavailable; retry shortly.'}
    assert list(scratch.iterdir()) == []
    assert store.path.is_file()


def test_selected_snapshot_does_not_translate_reader_oserror(tmp_path):
    Store(tmp_path)
    with pytest.raises(OSError, match='reader failed'):
        with read_snapshot.selected_snapshot_root(tmp_path) as copied:
            assert (copied / 'usage-ledger' / 'events.sqlite3').is_file()
            raise OSError('reader failed')


def test_real_exclusive_lock_has_bounded_busy_failure(tmp_path):
    store = Store(tmp_path)
    with closing(sqlite3.connect(store.path)) as writer:
        writer.execute('PRAGMA journal_mode=DELETE')
        writer.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        try:
            with pytest.raises(read_snapshot.SnapshotBusy):
                with read_snapshot.selected_snapshot_root(tmp_path):
                    pass
            assert time.monotonic() - started < 4
        finally:
            writer.rollback()


@pytest.mark.parametrize('error,detail', [
    (read_snapshot.SnapshotBusy, 'Selected analytics snapshot busy; retry shortly.'),
    (read_snapshot.SnapshotUnavailable, 'Selected analytics snapshot unavailable; retry shortly.'),
])
def test_selected_busy_routes_retryable_and_aggregate_uses_strict_copy(tmp_path, monkeypatch, error, detail):
    Store(tmp_path)
    client = client_for(tmp_path)
    @contextmanager
    def blocked(_):
        raise error('fixture blocked')
        yield
    from ledger_runtime import analytics_reload
    monkeypatch.setattr(analytics_reload, 'selected_snapshot_root', blocked)
    get = client.get('/ledger', params=QUERY)
    post = client.post('/ledger/refresh', params=QUERY, json={})
    for response in (get, post):
        assert response.status_code == 503
        assert response.json()['detail'] == detail
    # All-profile must continue using aggregate.snapshot_root, never selected.
    assert aggregate.snapshot_root is read_snapshot.snapshot_root
    all_profiles = client.get('/ledger', params={**QUERY, 'profile_scope': 'all'})
    assert all_profiles.status_code == 200, all_profiles.text
    assert all_profiles.json()['coverage']['read_profiles'] == 1
    with read_snapshot.snapshot_root(tmp_path) as copied:
        with closing(sqlite3.connect(copied / 'usage-ledger' / 'events.sqlite3')) as connection:
            assert connection.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 0


def test_old_selected_ledger_stays_full_get_without_migration(tmp_path):
    folder = tmp_path / 'usage-ledger'
    folder.mkdir()
    source = folder / 'events.sqlite3'
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(SCHEMA)
        insert(connection, 0)
    client = client_for(tmp_path)
    result = client.get('/ledger', params=QUERY)
    assert result.status_code == 200, result.text
    assert result.json()['request_count'] == 1
    assert 'incremental' not in result.json()
    assert client.post('/ledger/refresh', params=QUERY, json={}).json()['request_count'] == 1
    with closing(sqlite3.connect(source)) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='analytics_changes'").fetchone()
