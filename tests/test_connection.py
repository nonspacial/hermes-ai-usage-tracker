"""Liveness is not inferred from idle token counters or a responding quota API."""
import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import APIRouter,FastAPI
from fastapi.testclient import TestClient
from _hermes_ai_usage_ledger_v2 import connection as c,recorder as r,adapters as ad
from _hermes_ai_usage_ledger_v2.api import add_routes
from _hermes_ai_usage_ledger_v2.storage import Store


def lease(at=100, **kw):
    return {'process':'p','updated':at,'data':{'heartbeat_at':at,'request_hooks_registered':True,'adapters':{'request hooks':'registered'},**kw}}


def test_live_lease_online_without_requests():
    d=c.summarize_health([lease()],101)
    assert d['state']=='online' and d['active_processes']==1


def test_old_lease_not_online():
    assert c.summarize_health([lease()],161)['state']=='not_recording'


def test_recent_recording_event_not_a_heartbeat():
    row={'updated':100,'data':{'adapters':{'request hooks':'registered'}}}
    assert c.summarize_health([row],101)['state']=='unverified'


def test_http_backend_alone_not_recording():
    assert c.summarize_health([],100)['state']=='not_recording'


def test_health_without_successful_hook_registration_not_online():
    assert c.summarize_health([lease(request_hooks_registered=False)],101)['state']=='not_recording'


def test_old_process_does_not_make_replacement_offline():
    d=c.summarize_health([lease(1),lease(100)],101)
    assert d['state']=='online' and d['stale_processes']==1


def test_unavailable_adapter_visible_as_limited():
    d=c.summarize_health([lease(adapters={'raw':'unavailable: changed signature'})],101)
    assert d['state']=='limited' and 'raw' in d['warnings'][0]


def test_recent_failure_limited_old_failure_not_permanent():
    row=lease(recorder_failures=2,last_failure_at=99)
    assert c.summarize_health([row],101)['state']=='limited'
    row['data']['heartbeat_at']=500
    assert c.summarize_health([row],500)['state']=='online'


def test_future_heartbeat_not_falsely_online():
    assert c.summarize_health([lease(110)],100)['state']!='online'


def test_malformed_status_never_online():
    for data in ['{bad json',[],{'heartbeat_at':float('nan')}]:
        assert c.summarize_health([{'data':data}],100)['state']!='online'


def test_profile_status_isolated_and_no_counter_scan(tmp_path):
    a=Store(tmp_path/'a');b=Store(tmp_path/'b')
    a.health('p',lease(time.time())['data'])
    assert c.status(a)['state']=='online'
    assert c.status(b)['state']=='not_recording'
    with a.db() as db:db.execute('DROP TABLE requests')
    assert c.status(a)['state']=='online'


def test_status_route_no_network_or_inference(tmp_path,monkeypatch):
    from _hermes_ai_usage_ledger_v2 import api
    def forbidden(*a,**k):raise AssertionError('No price fetch or worker from status')
    monkeypatch.setattr(api,'start_worker',forbidden)
    router=APIRouter();add_routes(router,lambda p:(tmp_path/p,p,None),lambda:tmp_path)
    app=FastAPI();app.include_router(router);client=TestClient(app)
    assert client.get('/ledger/status?profile=one').json()['state']=='not_recording'
    assert not (tmp_path/'one').exists()
    s=Store(tmp_path/'one');s.health('p',lease(time.time())['data'])
    assert client.get('/ledger/status?profile=one').json()['state']=='online'
    assert client.get('/ledger/status?profile=two').json()['state']=='not_recording'


def test_unknown_profile_not_silently_redirected(tmp_path):
    router=APIRouter();add_routes(router,lambda p:(None,None,'Unknown profile'),lambda:tmp_path)
    app=FastAPI();app.include_router(router)
    assert TestClient(app).get('/ledger/status?profile=absent').status_code==404


def test_status_failure_does_not_leak_exception(tmp_path,monkeypatch):
    Store(tmp_path)
    monkeypatch.setattr(c,'status',lambda *a,**kw:(_ for _ in ()).throw(OSError('PRIVATE_PATH')))
    router=APIRouter();add_routes(router,lambda p:(tmp_path,p,None),lambda:tmp_path)
    app=FastAPI();app.include_router(router);resp=TestClient(app).get('/ledger/status')
    assert resp.status_code==503 and 'PRIVATE_PATH' not in resp.text


def test_heartbeat_pins_profile_and_is_idempotent(tmp_path,monkeypatch):
    r.stop_heartbeats();r._STORES.clear();r.ADAPTERS.clear()
    monkeypatch.setattr(c,'HEARTBEAT_SECONDS',0.03)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'a'))
    thread=r.start_heartbeat();assert thread is r.start_heartbeat()
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'b'))
    try:
        time.sleep(.09)
        a=Store(tmp_path/'a')
        assert c.status(a)['state']=='online'
        assert not (tmp_path/'b').exists()
        with a.db() as db:
            assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    finally:
        r.stop_heartbeats();thread.join(timeout=1)
    assert not thread.is_alive()


def test_recorder_error_does_not_kill_heartbeat(tmp_path,monkeypatch):
    r.stop_heartbeats();r._STORES.clear()
    monkeypatch.setenv('HERMES_HOME',str(tmp_path));monkeypatch.setattr(c,'HEARTBEAT_SECONDS',0.02)
    s=r.store();original=s.health;calls=[]
    def flaky(*args):
        calls.append(1)
        if len(calls)==1:raise OSError('PRIVATE_PATH')
        return original(*args)
    monkeypatch.setattr(s,'health',flaky)
    old=r.FAILURES
    thread=r.start_heartbeat()
    try:
        time.sleep(.08)
        assert r.FAILURES>old and c.status(s)['state']=='limited'
    finally:r.stop_heartbeats();thread.join(timeout=1)


def test_registration_starts_lease_only_after_request_hooks(tmp_path,monkeypatch):
    import _hermes_ai_usage_ledger_v2.pricing as prices
    calls=[]
    monkeypatch.setattr(r,'start_heartbeat',lambda: calls.append('heartbeat'))
    monkeypatch.setattr(ad,'install',lambda:None)
    monkeypatch.setattr(prices,'start_worker',lambda *a:None)
    monkeypatch.setattr(r,'store',lambda:None)
    path=Path(__file__).resolve().parents[1]/'__init__.py'
    spec=importlib.util.spec_from_file_location('test_registration_health',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    class Context:
        def register_hook(self,name,callback):calls.append(name)
    mod.register(Context())
    assert calls.index('heartbeat')>calls.index('api_request_error')
    calls.clear()
    class Broken:
        def register_hook(self,name,callback):raise RuntimeError('failed registration')
    with pytest.raises(RuntimeError):mod.register(Broken())
    assert not calls
