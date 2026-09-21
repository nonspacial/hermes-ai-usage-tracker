"""Deterministic interleavings; synthetic ledgers only, no provider traffic."""
import json
import time
import types

import pytest
from _hermes_ai_usage_ledger_v2 import recorder as r, adapters as ad
from _hermes_ai_usage_ledger_v2.storage import Store

RAW = {'input_tokens':100,'output_tokens':5,'input_tokens_details':{'cached_tokens':60,'cache_write_tokens':0}}

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(ad, 'install', lambda: None)
    monkeypatch.setattr(r, 'health', lambda *a, **kw: None)
    r._STORES.clear(); r._LOOKUP.clear(); r.CURRENT.set(None)
    if hasattr(r, '_TURN_ROOTS'): r._TURN_ROOTS.clear()
    return tmp_path

def start(aid='one', turn='turn-one'):
    r.pre(api_request_id=aid, session_id='session', turn_id=turn, provider='openai-codex', api_mode='codex_responses', model='fixture', started_at=time.time()-1)
    return dict(r.CURRENT.get())

def rows(env):
    with r.store(env).db() as c:
        return {x['api_request_id']: x for x in (json.loads(row[0]) for row in c.execute('SELECT data FROM requests'))}

def finish(**kw):
    r.session_end(session_id='session', turn_id='turn-one', interrupted=True, turn_exit_reason='interrupted', **kw)

def test_cleanup_is_turn_scoped_and_records_safe_reason(env):
    start(); start('two', 'turn-two')
    finish()
    data=rows(env)
    assert data['one']['status']=='ended_without_usage'
    assert data['two']['status']=='pending'
    assert data['one']['turn_outcome']=={'interrupted':True,'exit_reason':'interrupted'}

def test_cleanup_without_turn_identity_does_not_sweep_session(env):
    start()
    r.session_end(session_id='session')
    assert rows(env)['one']['status']=='pending'

def test_cleanup_cannot_overwrite_concurrent_completion(env, monkeypatch):
    start()
    store=r.store(env); original=store.request
    def interleave(rec, kind, **kw):
        if kind=='request_ended_without_usage':
            original({'id':rec['id'],'status':'completed','ended':time.time(),'usage':{'total_tokens':105}}, 'request_completed')
        return original(rec,kind,**kw)
    monkeypatch.setattr(store,'request',interleave)
    finish()
    assert rows(env)['one']['status']=='completed'

def test_cleanup_closes_received_usage_without_claiming_success(env):
    start(); r.capture_raw({'usage':RAW}, source='native_terminal_usage')
    finish()
    rec=rows(env)['one']
    assert rec['status']=='ended_with_usage' and rec['ended']
    assert rec['usage']['total_tokens']==105

def test_late_terminal_capture_repairs_availability_not_outcome(env):
    start(); finish()
    r.capture_raw({'usage':RAW}, source='native_terminal_usage')
    rec=rows(env)['one']
    assert rec['status']=='ended_with_usage'
    assert rec['turn_outcome']['interrupted'] is True
    assert rec['usage']['total_tokens']==105

def test_capture_cannot_reopen_request_after_cleanup_race(env, monkeypatch):
    start(); store=r.store(env); original=store.request
    def interleave(rec, kind, **kw):
        if kind=='usage_received_before_normalization':finish()
        return original(rec,kind,**kw)
    monkeypatch.setattr(store,'request',interleave)
    r.capture_raw({'usage':RAW}, source='native_terminal_usage')
    rec=rows(env)['one']
    assert rec['status']=='ended_with_usage' and rec['ended']

def test_late_capture_preserves_failed_outcome(env):
    cur=start(); r.error(session_id='session',api_request_id='one',error={'type':'TimeoutError','message':'PRIVATE'})
    r.capture_raw({'usage':RAW},source='native_terminal_usage',context=cur)
    rec=rows(env)['one']
    assert rec['status']=='failed' and rec['error_class']=='TimeoutError'
    assert rec['usage']['total_tokens']==105 and rec['usage']['raw_usage']==RAW
    assert 'PRIVATE' not in json.dumps(rec)

def test_codex_wrapper_pins_entry_identity(env):
    start()
    agent=types.SimpleNamespace(session_id='session',_current_api_request_id='one',provider='openai-codex',api_mode='codex_responses')
    def original(agent, kwargs):
        start('two','turn-two'); agent._current_api_request_id='two'
        return {'usage':RAW}
    ad.codex_run_wrapper(original)(agent,{})
    data=rows(env)
    assert data['one']['usage']['total_tokens']==105
    assert not data['two'].get('usage')

def test_turn_cleanup_recovers_registered_home(env, monkeypatch, tmp_path):
    start(); monkeypatch.setenv('HERMES_HOME',str(tmp_path/'other'))
    finish()
    assert rows(env)['one']['status']=='ended_without_usage'
    assert not (tmp_path/'other').exists()


def test_missing_field_reasons_are_full_window_and_preserve_totals(env):
    from _hermes_ai_usage_ledger_v2.storage import summary
    s=r.store(env)
    records=[
        {'id':'legacy','started':1,'ended':2,'status':'completed','usage':{'input_tokens':40,'output_tokens':5,'cache_read_tokens':60,'cache_write_tokens':0,'total_tokens':105,'prompt_tokens':100,'usage_source':'hermes_normalized'}},
        {'id':'active','started':3,'status':'pending'},
        {'id':'stopped','started':4,'ended':5,'status':'ended_without_usage'},
        {'id':'partial','started':6,'ended':7,'status':'completed','usage':{'total_tokens':50}},
    ]
    for rec in records:s.request(dict(rec,provider='fixture',session_id='fixture'),'fixture')
    with s.db() as c:before=c.execute('SELECT data FROM requests ORDER BY id').fetchall()
    result=s.read(0,10,limit=1)
    assert result['request_count']==4 and len(result['requests'])==1
    assert result['summary']['known']['total_tokens']==155
    reasons=result['summary']['missing_reasons']['input_tokens']
    assert reasons==dict(awaiting_usage=0,unresolved_execution=1,abandoned_execution=0,unverified_accounting=1,ended_without_usage=1,unreported_field=1)
    assert result['summary']['missing_reasons']==summary(records)['missing_reasons']
    for key,reason in result['summary']['missing_reasons'].items():
        assert sum(reason.values())==result['summary']['missing_fields'][key]
    with s.db() as c:assert c.execute('SELECT data FROM requests ORDER BY id').fetchall()==before


def test_storage_rechecks_usage_priority_at_atomic_write(env):
    from _hermes_ai_usage_ledger_v2.accounting import normalize
    cur=start();s=r.store(env)
    strong=dict(normalize(RAW),usage_source='wire_terminal_usage')
    s.request({'id':cur['id'],'usage':strong,'status':'completed','ended':time.time()},'request_completed')
    price=rows(env)['one']['cost']
    weak=dict(normalize({'input_tokens':1,'output_tokens':1}),usage_source='native_assembled_usage')
    s.request({'id':cur['id'],'usage':weak,'status':'usage_received'},'usage_received_before_normalization')
    assert rows(env)['one']['usage']==strong
    assert rows(env)['one']['status']=='completed'
    assert rows(env)['one']['cost']==price
    s.request({'id':cur['id'],'usage':{'usage_source':'wire_terminal_usage'}},'cache_wire_evidence')
    assert rows(env)['one']['usage']==strong


def test_weaker_capture_cannot_replace_response_identity_or_pricing(env, monkeypatch):
    from _hermes_ai_usage_ledger_v2.accounting import normalize
    cur=start();s=r.store(env)
    def rate(connection, record):
        value='1' if record.get('response_model')=='strong-model' else '99'
        return dict(input_tokens=value,output_tokens=value,cache_read_tokens=value,cache_write_tokens=value)
    monkeypatch.setattr(s,'latest_rate',rate)
    strong=dict(normalize(RAW),usage_source='wire_terminal_usage')
    s.request({'id':cur['id'],'usage':strong,'status':'usage_received','response_model':'strong-model','returned_service_tier':'standard','provider_response_id':'strong-id'},'cache_wire_evidence')
    before=rows(env)['one']
    weak=dict(normalize(RAW),usage_source='native_assembled_usage')
    s.request({'id':cur['id'],'usage':weak,'response_model':'weak-model','returned_service_tier':'priority','provider_response_id':'weak-id'},'usage_received_before_normalization')
    after=rows(env)['one']
    for key in ('usage','response_model','returned_service_tier','provider_response_id','cost'):
        assert after[key]==before[key]
