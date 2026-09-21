"""Synthetic roots only: exact process evidence and transactional race cuts."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest
from _hermes_ai_usage_ledger_v2 import ownership as own, recorder as r, adapters as ad
from _hermes_ai_usage_ledger_v2.storage import Store, summary
from _hermes_ai_usage_ledger_v2.accounting import normalize


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(ad, 'install', lambda: None)
    monkeypatch.setattr(r, 'health', lambda *a, **kw: None)
    r._STORES.clear(); r._LOOKUP.clear(); r._TURN_ROOTS.clear(); r.CURRENT.set(None)
    return r.store(tmp_path)


def put(s, key='one', owner=None, **kw):
    s.request(dict(id=key, started=1, session_id='session', provider='fixture',
                   model='fixture', status='pending', owner=owner or own.identity(), **kw), 'request_started')


def row(s, key='one'):
    with s.db() as c:
        return json.loads(c.execute('SELECT data FROM requests WHERE id=?', (key,)).fetchone()[0])


def test_real_child_exit_is_reconciled_at_successor_session_start(env):
    code = '''import runpy
runpy.run_path('bootstrap.py')
from _hermes_ai_usage_ledger_v2 import recorder as r, adapters as ad
ad.install=lambda:None
r.pre(api_request_id='child',session_id='session',started_at=1)
assert r.CURRENT.get()
'''
    child = subprocess.run([sys.executable, '-B', '-c', code], check=True, timeout=20)
    assert child.returncode == 0
    r.session_start(session_id='successor')
    report = env.read(0, 10)
    rec = report['requests'][0]
    assert rec['status'] == 'abandoned_without_usage'
    assert rec.get('ended') is None and rec['execution_outcome'] == 'abandoned'
    assert rec['reconciliation']['reason'] == 'process_absent'
    assert rec['reconciliation']['actual_end_known'] is False
    assert report['summary']['pending'] == report['summary']['unresolved'] == 0
    assert report['summary']['abandoned'] == report['summary']['attempts'] == 1
    assert report['summary']['missing_reasons']['total_tokens']['abandoned_execution'] == 1
    with env.db() as c:
        kinds = [x[0] for x in c.execute('SELECT kind FROM events WHERE item_id=?', (rec['id'],))]
    assert kinds == ['request_started', 'request_abandoned']
    own.reconcile(env)
    with env.db() as c:
        assert c.execute("SELECT count(*) FROM events WHERE kind='request_abandoned'").fetchone()[0] == 1


@pytest.mark.parametrize('state', ['Z', 'X', 'x'])
def test_terminal_leader_state_is_not_process_death(env, monkeypatch, state):
    owner = own.identity()
    put(env, owner=owner)
    monkeypatch.setattr(own, 'process', lambda pid: (owner['start'], state))
    assert own.inspect_owner(owner) == ('unknown', 'terminal_leader_state')
    own.reconcile(env)
    assert row(env)['status'] == 'pending'
    assert own.execution_state(row(env)) == 'unresolved'


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux pthread_exit/proc regression')
def test_pthread_exit_leader_keeps_worker_request_open(env):
    import time
    code = '''import ctypes, json, os, runpy, threading
runpy.run_path('bootstrap.py')
from _hermes_ai_usage_ledger_v2 import recorder as r, adapters as ad
ad.install=lambda:None
r.health=lambda *a, **kw:None
r.pre(api_request_id='threaded',session_id='session',started_at=1)
cur=r.CURRENT.get()
assert cur
libc=ctypes.CDLL(None)
libc.pthread_exit.argtypes=[ctypes.c_void_p]
libc.pthread_exit.restype=None
def worker():
    try:
        assert input() == 'complete'
        r.capture_raw({'usage':{'input_tokens':7,'output_tokens':3}},context=cur)
        r.post(api_request_id='threaded',session_id='session',ended_at=2)
        print('completed',flush=True)
    except BaseException:
        import traceback
        traceback.print_exc()
        os._exit(1)
    os._exit(0)
threading.Thread(target=worker).start()
print(json.dumps(cur),flush=True)
libc.pthread_exit(None)
'''
    child = subprocess.Popen([sys.executable, '-B', '-c', code], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import select
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 20)[0], 'worker startup timed out'
        cur = json.loads(child.stdout.readline())
        deadline = time.monotonic() + 10
        while own.process(child.pid)[1] != 'Z':
            assert time.monotonic() < deadline, 'leader did not exit'
            time.sleep(0.01)
        rec = row(env, cur['id'])
        assert own.inspect_owner(rec['owner']) == ('unknown', 'terminal_leader_state')
        assert any(own.process(int(task.name))[1] not in ('Z', 'X', 'x')
                   for task in Path(f'/proc/{child.pid}/task').iterdir())
        r.session_start(session_id='successor')
        assert row(env, cur['id']) == rec
        report = env.read(0, 10)
        assert report['summary']['unresolved'] == 1
        assert report['summary']['abandoned'] == 0
        stdout, stderr = child.communicate('complete\n', timeout=20)
        assert child.returncode == 0, stderr
        assert stdout.strip() == 'completed'
        rec = row(env, cur['id'])
        assert rec['status'] == 'completed' and rec['ended'] == 2
        assert rec['usage']['total_tokens'] == 10
        assert 'reconciliation' not in rec
        own.reconcile(env)
        assert row(env, cur['id']) == rec
        with env.db() as c:
            assert c.execute("SELECT count(*) FROM events WHERE kind='request_abandoned'").fetchone()[0] == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.parametrize('change,expected', [({}, ('live', 'matching_process_identity')),
    ({'start':'0'}, ('dead','pid_reused')),
    ({'boot':'00000000-0000-0000-0000-000000000000'}, ('dead','previous_boot')),
    ({'host':'foreign'}, ('unknown','foreign_host')),
    ({'namespace':'foreign'}, ('unknown','foreign_pid_namespace')),
    ({'generation':None}, ('unknown','incomplete_owner_identity')),
    ({'boot':'malformed'}, ('unknown','malformed_owner_identity')),
    ({'start':'malformed'}, ('unknown','malformed_owner_identity'))])
def test_identity_evidence(change, expected):
    owner = own.identity()
    assert own.inspect_owner(dict(owner, **change)) == expected


def test_access_platform_and_hidden_proc_are_unknown(monkeypatch):
    owner = own.identity()
    def denied(*a): raise PermissionError(errno.EACCES, 'fixture')
    monkeypatch.setattr(own, 'process', denied)
    assert own.inspect_owner(owner) == ('unknown','inspection_unavailable')
    def missing(*a): raise FileNotFoundError(errno.ENOENT, 'fixture')
    monkeypatch.setattr(own, 'process', missing)
    assert own.inspect_owner(owner) == ('unknown','process_uninspectable')
    monkeypatch.setattr(own, 'environment', denied)
    assert own.inspect_owner(owner) == ('unknown','inspection_unavailable')
    assert own.inspect_owner(None) == ('unknown','missing_owner_identity')


def test_live_overlapping_helpers_retries_and_stale_heartbeat(env):
    r.pre(api_request_id='first', session_id='session', turn_id='one', started_at=1)
    first = r.CURRENT.get()
    helper = ad.aux_begin(types.SimpleNamespace(), {}, {})
    r.pre(api_request_id='retry', session_id='session', turn_id='two', started_at=2)
    env.health(r.PROCESS, {'heartbeat_at':1, 'request_hooks_registered':True})
    r.capture_raw({'usage':{'input_tokens':4,'output_tokens':2}}, context=first)
    r.session_start(session_id='session')
    out = env.read()
    assert out['summary']['pending'] == 3  # includes usage_received
    assert out['summary']['unresolved'] == out['summary']['abandoned'] == 0
    assert all(x['owner']['generation'] == own.identity()['generation'] for x in out['requests'])
    r.session_end(session_id='session', turn_id='two')
    assert row(env, helper[1]['id'])['status'] == 'pending'
    assert row(env, first['id'])['status'] == 'usage_received'


def test_legacy_opens_unresolved_not_rewritten(env):
    env.request({'id':'legacy','started':1,'status':'pending','process':'old'},'fixture')
    before = row(env,'legacy')
    own.reconcile(env)
    assert row(env,'legacy') == before
    out=env.read(0,10)
    assert out['summary']['pending'] == 0 and out['summary']['unresolved'] == 1
    assert out['requests'][0]['execution_state'] == 'unresolved'
    assert out['summary']['missing_reasons']['total_tokens']['unresolved_execution'] == 1


@pytest.mark.parametrize('race', ['complete','usage','owner'])
def test_transaction_guard_and_late_capture(env, monkeypatch, race):
    owner=dict(own.identity(), start='0')
    put(env, owner=owner)
    original=env.request
    usage=dict(normalize({'input_tokens':7,'output_tokens':3}), usage_source='wire_terminal_usage')
    def interleave(rec, kind, **kw):
        if kind=='request_abandoned':
            if race=='complete': original({'id':'one','ended':2,'status':'completed','usage':usage},'request_completed')
            if race=='usage': original({'id':'one','status':'usage_received','usage':usage},'capture')
            if race=='owner':
                # Simulate a migrated/replaced generation after selection.
                with env.db() as c:
                    data=row(env);data['owner']=own.identity()
                    c.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(data),'one'))
        return original(rec,kind,**kw)
    monkeypatch.setattr(env,'request',interleave)
    own.reconcile(env)
    rec=row(env)
    assert rec['status']=={'complete':'completed','usage':'abandoned_with_usage','owner':'pending'}[race]
    if race=='owner':return
    original({'id':'one','status':'usage_received','usage':usage},'capture')
    original({'id':'one','status':'usage_received','usage':usage},'capture')
    assert row(env)['status']==rec['status']
    assert row(env).get('ended')==rec.get('ended')
    report=env.read(0,10)
    assert report['request_count']==1 and report['summary']['known']['total_tokens']==10


def test_capture_selected_before_reconciliation_cannot_reopen(env, monkeypatch):
    r.pre(api_request_id='one',session_id='session',started_at=1)
    cur=r.CURRENT.get()
    monkeypatch.setattr(own,'inspect_owner',lambda owner:('dead','process_absent'))
    original=env.request
    def interleave(rec, kind, **kw):
        if kind=='usage_received_before_normalization':own.reconcile(env)
        return original(rec,kind,**kw)
    monkeypatch.setattr(env,'request',interleave)
    for _ in range(2):r.capture_raw({'usage':{'input_tokens':7,'output_tokens':3}},context=cur)
    rec=row(env,cur['id'])
    assert rec['status']=='abandoned_with_usage' and rec.get('ended') is None
    assert rec['execution_outcome']=='abandoned'
    assert env.read(0,10)['summary']['known']['total_tokens']==10


def test_rotating_bounded_indexed_batches_do_not_starve(env):
    put(env,key='a-live')
    put(env,key='b-dead',owner=dict(own.identity(),start='0'))
    with env.db() as c:
        plan=c.execute("EXPLAIN QUERY PLAN SELECT id,data FROM requests WHERE status IN ('pending','usage_received') AND ended IS NULL AND id>? ORDER BY id LIMIT ?",('',1)).fetchall()
    assert any('request_open_identity' in x['detail'] for x in plan)
    own.reconcile(env,limit=1)
    assert row(env,'b-dead')['status']=='pending'
    own.reconcile(env,limit=1)
    assert row(env,'b-dead')['status']=='abandoned_without_usage'
    assert row(env,'a-live')['status']=='pending'


def test_python_sql_groups_and_all_profiles_parity(env, tmp_path):
    from _hermes_ai_usage_ledger_v2 import aggregate
    from _hermes_ai_usage_ledger_v2.analytics_reload import AnalyticsRuntime
    put(env,'live',usage={'total_tokens':10})
    put(env,'dead',owner=dict(own.identity(),start='0'),usage={'total_tokens':20})
    env.request({'id':'legacy','started':1,'status':'usage_received'},'fixture')
    own.reconcile(env)
    other=Store(tmp_path/'profiles'/'other')
    put(other,'live')
    out=env.read(0,10)
    with env.db() as c:records=[json.loads(x[0]) for x in c.execute('SELECT data FROM requests')]
    expected=summary(records)
    for key in ('pending','unresolved','abandoned','known','missing_fields','missing_reasons','attempts'):
        assert out['summary'][key]==expected[key]
    for group in ('provider_groups','model_groups','groups','session_groups','project_groups','agent_groups','applied_rate_groups'):
        assert sum(x['pending'] for x in out[group])==1
        assert sum(x['unresolved'] for x in out[group])==1
        assert sum(x['abandoned'] for x in out[group])==1
    runtime=AnalyticsRuntime()
    try:
        merged=aggregate.ledger(runtime,aggregate.discover(tmp_path),end=10)
        assert merged['summary']['pending']==2
        assert merged['summary']['unresolved']==merged['summary']['abandoned']==1
        assert merged['summary']['attempts']==4
        assert merged['summary']['known']['total_tokens']==30
    finally:runtime._discard(runtime.current)


def test_reconciliation_preserves_counters_prices_and_history(env, monkeypatch):
    put(env,owner=dict(own.identity(),start='0'))
    usage=dict(normalize({'input_tokens':7,'output_tokens':3}),usage_source='wire_terminal_usage')
    rate=dict(input_tokens='1',output_tokens='2',cache_read_tokens='1',cache_write_tokens='1')
    monkeypatch.setattr(env,'latest_rate',lambda c,rec:rate)
    env.request({'id':'one','status':'usage_received','usage':usage},'capture')
    before=row(env)
    with env.db() as c:events=[tuple(x) for x in c.execute('SELECT * FROM events')]
    monkeypatch.setattr(env,'latest_rate',lambda c,rec:dict(rate,output_tokens='999'))
    own.reconcile(env)
    for _ in range(2):env.request({'id':'one','status':'usage_received','usage':usage},'capture')
    env.request({'id':'one','status':'completed','ended':999,'usage':usage},'request_completed')
    after=row(env)
    assert after['usage']==before['usage'] and after['cost']==before['cost']
    assert after['status']=='abandoned_with_usage' and after.get('ended') is None
    with env.db() as c:
        assert [tuple(x) for x in c.execute('SELECT * FROM events ORDER BY seq LIMIT ?', (len(events),))]==events
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM events WHERE kind='request_abandoned'").fetchone()[0]==1


def test_ended_usage_received_is_not_open(env):
    put(env,ended=2)
    env.request({'id':'one','status':'usage_received'},'capture')
    result=env.read(0,10)
    assert result['summary']['pending']==result['summary']['unresolved']==0
    assert result['requests'][0]['execution_state']=='closed'


def test_reconciliation_observes_each_owner_once_per_batch(env, monkeypatch):
    for i in range(4):put(env,key=str(i))
    calls=[]
    def inspect(owner):calls.append(owner);return 'live','matching_process_identity'
    monkeypatch.setattr(own,'inspect_owner',inspect)
    own.reconcile(env)
    assert len(calls)==1


def test_unsupported_platform_never_proves_death(monkeypatch):
    owner=own.identity()
    monkeypatch.setattr(own.sys,'platform','unsupported')
    assert own.inspect_owner(owner)==('unknown','inspection_unavailable')
    assert 'boot' not in own.identity()


def test_fork_gets_distinct_generation(env):
    parent=own.identity()
    rd,wr=os.pipe()
    pid=os.fork()
    if pid==0:
        try:
            os.close(rd)
            os.write(wr,json.dumps(own.identity()).encode())
        finally:os._exit(0)
    os.close(wr)
    with os.fdopen(rd) as stream:child=json.loads(stream.read())
    _,status=os.waitpid(pid,0)
    assert status==0 and child['pid']==pid and child['generation']!=parent['generation']
    assert own.inspect_owner(child)==('dead','process_absent')
