"""Analytics reload never replaces recorder modules or writes the real ledger."""
import json
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from _hermes_ai_usage_ledger_v2 import api, recorder, pricing
from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime, RestartRequired
from _hermes_ai_usage_ledger_v2.storage import Store


@pytest.fixture
def runtime(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'ledger_runtime'
    folder = tmp_path / 'plugin' / 'ledger_runtime'
    shutil.copytree(source, folder, ignore=shutil.ignore_patterns('__pycache__'))
    runtime = AnalyticsRuntime(folder)
    yield runtime
    runtime._discard(runtime.current)


def fixture_store(tmp_path):
    store = Store(tmp_path / 'home')
    for key, started, reads in [('first', 1, 0), ('second', 2, 100)]:
        store.request(dict(id=key, started=started, ended=started + .1,
            session_id='fixture', model='fixture', provider='fixture', task='main',
            source='main_hook', status='completed', usage={'cache_read_tokens': reads}), 'request_completed')
    return store


def change_basis(runtime):
    path = runtime.folder / 'session_cache_writes.py'
    path.write_text(path.read_text().replace('per session stream', 'per isolated session stream'))


def test_reload_changes_only_new_reads_and_preserves_recorder(runtime, tmp_path):
    store = fixture_store(tmp_path)
    before = store.path.read_bytes()
    original_pre, original_stores = recorder.pre, recorder._STORES
    workers = dict(pricing._WORKERS)
    old = runtime.read(store.root, 0, 3)
    change_basis(runtime)
    assert runtime.read(store.root, 0, 3)['analytics_revision'] == old['analytics_revision']
    reloaded = runtime.reload()
    new = runtime.read(store.root, 0, 3)
    assert new['analytics_revision'] == reloaded['revision'] != old['analytics_revision']
    assert 'isolated' in new['summary']['session_cache_writes']['basis']
    assert new['summary']['session_cache_writes']['tokens'] == 100
    assert recorder.pre is original_pre and recorder._STORES is original_stores
    assert pricing._WORKERS == workers
    assert store.path.read_bytes() == before


def test_inflight_generation_survives_then_retires(runtime, tmp_path):
    store = fixture_store(tmp_path)
    with runtime.lease() as previous:
        names = list(previous['modules'])
        change_basis(runtime)
        runtime.reload()
        assert all(name in sys.modules for name in names)
        result = runtime._reader(previous, store.root).read(0, 3)
        assert 'isolated' not in result['summary']['session_cache_writes']['basis']
    assert all(name not in sys.modules for name in names)


def test_bad_source_keeps_previous_generation_and_cleans_modules(runtime, tmp_path):
    store = fixture_store(tmp_path)
    revision = runtime.info()['revision']
    modules = {key for key in sys.modules if key.startswith('_ai_usage_analytics_')}
    (runtime.folder / 'storage.py').write_text('this is invalid python !!!')
    with pytest.raises(SyntaxError):
        runtime.reload()
    assert runtime.info()['revision'] == revision
    assert runtime.read(store.root, 0, 3)['request_count'] == 2
    assert {key for key in sys.modules if key.startswith('_ai_usage_analytics_')} == modules


def test_failed_behaviour_validation_keeps_old_reader(runtime, tmp_path):
    store = fixture_store(tmp_path)
    revision = runtime.info()['revision']
    path = runtime.folder / 'session_cache_writes.py'
    path.write_text(path.read_text().replace('tokens=max(0, current-prior)', 'tokens=999'))
    with pytest.raises(ValueError, match='calculation'):
        runtime.reload()
    assert runtime.info()['revision'] == revision
    assert runtime.read(store.root, 0, 3)['summary']['session_cache_writes']['tokens'] == 100


def test_recorder_changes_require_restart(runtime):
    revision = runtime.info()['revision']
    path = runtime.folder / 'recorder.py'
    path.write_text(path.read_text() + '\n# recorder changed\n')
    assert runtime.info()['restart_required']
    with pytest.raises(RestartRequired):
        runtime.reload()
    assert runtime.info()['revision'] == revision


def test_projection_contract_change_requires_restart_without_mixed_view(runtime, tmp_path):
    store = fixture_store(tmp_path)
    before = runtime.read(store.root, 0, 3, view='requests')
    assert 'requests' in before and 'price_catalogs' not in before
    contract = runtime.folder / 'projection.py'
    original = contract.read_text()
    assert "('requests', 'cache_read_progression')" in original
    contract.write_text(original.replace("('requests', 'cache_read_progression')",
                                         "('price_catalogs', 'cache_read_progression')"))
    assert runtime.info()['restart_required']
    with pytest.raises(RestartRequired):
        runtime.reload()
    unchanged = runtime.read(store.root, 0, 3, view='requests')
    assert unchanged['analytics_revision'] == before['analytics_revision']
    assert unchanged['projection'] == before['projection']
    assert 'requests' in unchanged and 'price_catalogs' not in unchanged
    contract.write_text(original)
    change_basis(runtime)
    assert not runtime.info()['restart_required']
    reloaded = runtime.reload()
    after = runtime.read(store.root, 0, 3, view='requests')
    assert after['analytics_revision'] == reloaded['revision'] != before['analytics_revision']
    assert after['projection'] == before['projection']
    assert 'requests' in after and 'price_catalogs' not in after


def test_schema_changes_require_restart(runtime):
    path = runtime.folder / 'storage.py'
    path.write_text(path.read_text() + '\nSCHEMA += "CREATE TABLE unexpected(x);"\n')
    with pytest.raises(RestartRequired, match='schema'):
        runtime.reload()


@pytest.mark.parametrize('filename,old,new', [
    ('storage.py', 'data.setdefault(\'status\',\'pending\')', 'data.setdefault(\'status\',\'changed\')'),
    ('accounting.py', 'from typing import Any', 'from typing import Any\n# changed accounting'),
])
def test_shared_writer_code_requires_restart(runtime, filename, old, new):
    path = runtime.folder / filename
    source = path.read_text()
    assert old in source
    path.write_text(source.replace(old, new))
    assert runtime.info()['restart_required']
    with pytest.raises(RestartRequired):
        runtime.reload()


def test_storage_read_only_change_is_reloadable(runtime, tmp_path):
    store = fixture_store(tmp_path)
    path = runtime.folder / 'storage.py'
    source = path.read_text()
    old = "'generated_at':time.time(),'seq':seq"
    assert old in source
    path.write_text(source.replace(old, "'reader_change':True,'generated_at':time.time(),'seq':seq"))
    assert not runtime.info()['restart_required']
    runtime.reload()
    assert runtime.read(store.root, 0, 3)['reader_change'] is True


def test_new_key_method_stays_inside_protected_store_contract(runtime):
    path = runtime.folder / 'storage.py'
    source = path.read_text()
    path.write_text(source.replace('SELECT started,id FROM requests WHERE ', 'SELECT started,id FROM main.requests WHERE '))
    assert runtime.info()['restart_required']
    with pytest.raises(RestartRequired):
        runtime.reload()


def test_reader_cannot_write_main_database(runtime, tmp_path):
    store = fixture_store(tmp_path)
    with runtime.lease() as generation:
        reader = runtime._reader(generation, store.root)
        with reader.db() as connection:
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                connection.execute('DELETE FROM requests')
    assert runtime.read(store.root, 0, 3)['request_count'] == 2


def test_reload_routes_profile_validation_readback_and_no_producer_start(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'AnalyticsRuntime', lambda: runtime)
    monkeypatch.setattr(api, 'start_worker', lambda *_a, **_k: pytest.fail('reload must not start pricing'))
    router = APIRouter()
    api.add_routes(router, lambda p: (tmp_path, p, None) if p == 'infra' else (None, None, 'Unknown profile'), lambda: tmp_path)
    app = FastAPI(); app.include_router(router)
    client = TestClient(app)
    assert client.post('/ledger/analytics/reload?profile=bad').status_code == 404
    result = client.post('/ledger/analytics/reload?profile=infra')
    assert result.status_code == 200
    assert client.get('/ledger/analytics?profile=infra').json()['revision'] == result.json()['revision']
    assert client.get('/ledger?profile=infra').json()['request_count'] == 0
    assert client.get('/ledger/status?profile=infra').json()['state'] == 'not_recording'
    with client.websocket_connect('/ledger/events?profile=infra') as ws:
        assert ws.receive_json()['mode'] == 'display-refresh-fallback'
    assert not (tmp_path / 'usage-ledger').exists()
    path = runtime.folder / 'recorder.py'; path.write_text(path.read_text() + '\n# changed\n')
    assert client.post('/ledger/analytics/reload?profile=infra').status_code == 409
    # Restore only the fixture file to exercise the validation failure response.
    shutil.copy2(Path(recorder.__file__), path)
    (runtime.folder / 'storage.py').write_text('invalid !!!')
    failed = client.post('/ledger/analytics/reload?profile=infra')
    assert failed.status_code == 503
    assert 'previous code remains active' in failed.json()['detail']
    assert str(tmp_path) not in json.dumps(failed.json())


def test_read_routes_do_not_construct_writer_or_start_pricing(runtime, tmp_path, monkeypatch):
    store = fixture_store(tmp_path)
    with store.db() as connection:
        connection.execute('INSERT INTO tests VALUES(?,?,?,?,?)', ('window', 1.5, 3, 'fixture', '{}'))
    before = store.path.read_bytes()
    monkeypatch.setattr(api, 'AnalyticsRuntime', lambda: runtime)
    monkeypatch.setattr(api, 'Store', lambda *_a, **_k: pytest.fail('read route constructed writer'))
    monkeypatch.setattr(api, 'start_worker', lambda *_a, **_k: pytest.fail('read route started pricing'))
    router = APIRouter()
    api.add_routes(router, lambda p: (store.root, p, None), lambda: store.root)
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/ledger?profile=infra').json()['request_count'] == 2
        result = client.get('/ledger?profile=infra&test_id=window').json()
        assert [row['id'] for row in result['requests']] == ['second']
        assert result['summary']['session_cache_writes']['tokens'] == 100
        assert client.get('/ledger/status?profile=infra').json()['state'] == 'not_recording'
        with client.websocket_connect('/ledger/events?profile=infra') as ws:
            assert ws.receive_json()['type'] == 'connected'
    assert store.path.read_bytes() == before
