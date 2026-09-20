"""Pre-SDK cache evidence: fake SDK boundary, real httpx request objects; no HTTP."""
import copy
import json
import time
import types
import weakref
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from _hermes_ai_usage_ledger_v2 import adapters as ad,recorder as r,cache_probe as cp
from _hermes_ai_usage_ledger_v2.storage import Store

URL='https://chatgpt.com/backend-api/codex/responses'
RAW={'input_tokens':136408,'input_tokens_details':{'cached_tokens':135936,'cache_write_tokens':0},'output_tokens':174,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':136582}

@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path));monkeypatch.setenv('HERMES_USAGE_PRICING_OFFLINE','1')
    r._STORES.clear();r._LOOKUP.clear();r.ADAPTERS.clear();r.FAILURES=0
    r.CURRENT.set(None);r.AUX.set(None);r.COMPRESSION.set(None);r.LAST_FAILURE_AT=0
    cp._REQUESTS=weakref.WeakKeyDictionary()
    monkeypatch.setattr(ad,'install',lambda:None)
    yield tmp_path
    r.FAILURES=0;r.LAST_FAILURE_AT=0;r.CURRENT.set(None);r.AUX.set(None);r.COMPRESSION.set(None)
    cp._REQUESTS=weakref.WeakKeyDictionary()

def pre(aid='a',sid='s',provider='openai-codex',api_mode='codex_responses'):
    r.pre(api_request_id=aid,session_id=sid,provider=provider,api_mode=api_mode,model='test-model',started_at=time.time()-1)
    return types.SimpleNamespace(session_id=sid,_current_api_request_id=aid,provider=provider,api_mode=api_mode)

def request(body=None,url=URL):
    req=httpx.Request('POST',url,json=body or {'model':'test-model','stream':True,'input':[{'role':'user','content':[{'type':'input_text','text':'private message'}]}]})
    cp.observe_request(req)
    return req

def event(writes=0):
    raw=copy.deepcopy(RAW)
    if writes=='absent':del raw['input_tokens_details']['cache_write_tokens']
    else:raw['input_tokens_details']['cache_write_tokens']=writes
    return {'type':'response.completed','response':{'id':'resp_test','model':'test-model','service_tier':'default','usage':raw}}

def emit(req,data):
    cp.observe_response(data,httpx.Response(200,request=req))

def record(root,aid=None):
    rows=Store(root).read()['requests']
    return next(x for x in rows if x.get('api_request_id')==aid) if aid else rows[0]

@pytest.mark.parametrize('writes,state',[(0,'explicit_zero'),(250,'positive'),('absent','field_absent'),(None,'invalid_or_null'),('250','invalid_or_null'),(-1,'invalid_or_null'),(True,'invalid_or_null')])
def test_pre_sdk_preserves_zero_positive_absent_and_invalid(env,writes,state):
    pre();req=request();data=event(writes);emit(req,data);row=record(env)
    assert row['cache_evidence']['boundary']=='decoded_http_json_before_sdk_models'
    assert row['cache_evidence']['cache_write_state']==state
    assert row['usage']['usage_source']=='wire_terminal_usage'
    assert row['usage']['cache_write_tokens']==(writes if state in ('positive','explicit_zero') else None)
    assert row['usage']['prompt_tokens']==136408 and row['usage']['total_tokens']==136582
    assert row['cache_evidence']['endpoint_kind']=='chatgpt_codex_subscription'
    assert r.FAILURES==0

def test_read_positive_write_zero_real_record_reconciles(env):
    pre();req=request();emit(req,event());row=record(env)
    assert row['usage']['input_tokens']==472 and row['usage']['cache_read_tokens']==135936
    assert row['usage']['total_tokens']==136582 and row['usage']['output_tokens']==174

@pytest.mark.parametrize('wire_value',[0,110,'absent'])
def test_original_sdk_models_cannot_invent_or_erase_writes(env,wire_value):
    agent=pre();seen=[]
    class FakeBaseClient:
        def _build_request(self,options,*,retries_taken=0):return options
        def _process_response_data(self,*,data,cast_to,response):
            seen.append(data)
            # Simulate a local SDK/converter that injects an erroneous zero.
            obj=copy.deepcopy(data['response']);obj['usage']['input_tokens_details']['cache_write_tokens']=0
            return obj
    cp.install(types.SimpleNamespace(BaseClient=FakeBaseClient))
    client=FakeBaseClient();req=httpx.Request('POST',URL,json={'stream':True});got=client._build_request(req)
    assert got is req
    data=event(wire_value);original=copy.deepcopy(data)
    modeled=client._process_response_data(data=data,cast_to=dict,response=httpx.Response(200,request=req))
    assert seen[0] is data and data==original
    ad.capture_native_response(modeled)
    r.capture_raw(modeled,agent)
    r.post(api_request_id='a',session_id='s',provider='openai-codex',api_mode='codex_responses',model='test-model',usage={'cache_write_tokens':0},ended_at=time.time())
    row=record(env)
    assert row['usage']['cache_write_tokens']==(wire_value if wire_value!='absent' else None)
    assert row['usage']['usage_source']=='wire_terminal_usage' and row['status']=='completed'
    assert len(Store(env).read()['requests'])==1

def test_cache_request_content_is_not_persisted_or_modified(env):
    pre();body={'model':'test-model','stream':True,'store':False,'service_tier':'priority','reasoning':{'effort':'xhigh'},
        'prompt_cache_options':{'mode':'implicit','ttl':'30m','comparison_response_id':'secret-comparison-id'},'prompt_cache_retention':'24h',
        'prompt_cache_key':'private-secret-key','instructions':'PRIVATE SYSTEM TEXT','tools':[{'name':'secret_tool','parameters':{'secret':'schema'}}],
        'input':[{'role':'user','content':[{'type':'input_text','text':'PRIVATE USER TEXT','prompt_cache_breakpoint':{'mode':'explicit'}}]}]}
    original=copy.deepcopy(body);req=request(body);assert json.loads(req.content)==original
    data=event();data['response']['usage']['extra_prompt']='never-save';data['response']['authorization']='never-save'
    emit(req,data);row=record(env);meta=row['cache_request']
    assert meta['mode_sent']=='implicit' and meta['ttl_sent']=='30m' and meta['explicit_breakpoint_count']==1
    assert meta['comparison_requested'] is True and meta['retention_sent']=='24h' and meta['service_tier_sent']=='priority'
    assert meta['reasoning_effort_sent']=='xhigh' and meta['input_item_count']==1
    assert len(meta['cache_key_fingerprint'])==64 and meta['tools_fingerprint']
    with Store(env).db() as c:
        strings=json.dumps([x[0] for x in c.execute('SELECT data FROM requests')])+json.dumps([x[0] for x in c.execute('SELECT data FROM events')])
    for text in ('PRIVATE','private-secret-key','secret-comparison-id','never-save','secret_tool','schema','Bearer'):
        assert text not in strings
    assert cp.request_metadata(body)['cache_key_fingerprint']==meta['cache_key_fingerprint']
    assert cp.request_metadata({**body,'prompt_cache_key':'different'})['cache_key_fingerprint']!=meta['cache_key_fingerprint']

def test_exact_http_request_correlation_after_context_loss(env):
    pre('one','s1');req1=request();pre('two','s2');req2=request();r.CURRENT.set(None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(emit,req1,event(123));b=pool.submit(emit,req2,event(456));a.result();b.result()
    assert record(env,'one')['usage']['cache_write_tokens']==123 and record(env,'two')['usage']['cache_write_tokens']==456
    assert r.FAILURES==0

def test_auxiliary_uses_its_own_request_not_parent(env):
    pre();parent_req=request()
    item=ad.aux_begin(types.SimpleNamespace(),{'model':'test-model'},{'provider':'openai-codex','api_mode':'codex_responses'})
    token=r.AUX.set(item)
    try:
        child_req=request();emit(child_req,event(123));ad.capture_native_response(event()['response']);ad.aux_end(item,event()['response'])
    finally:r.AUX.reset(token)
    emit(parent_req,event(321))
    rows=Store(env).read()['requests'];child=next(x for x in rows if x['source']=='auxiliary_adapter')
    assert child['usage']['cache_write_tokens']==123 and child['usage']['usage_source']=='wire_terminal_usage'
    assert record(env,'a')['usage']['cache_write_tokens']==321

def test_unobserved_client_context_route_or_read_request_ignored(env):
    req=request();emit(req,event());assert not Store(env).read()['requests']
    pre(provider='openrouter',api_mode='responses');req=request();emit(req,event());assert not record(env).get('cache_evidence')
    pre('b');req=request(url='https://api.openai.com/v1/chat/completions');emit(req,event());assert not record(env,'b').get('cache_evidence')
    req=httpx.Request('GET',URL);cp.observe_request(req);emit(req,event());assert not record(env,'b').get('cache_evidence')

def test_signature_drift_not_patched(env):
    class Changed:
        def _build_request(self,NEW_ARG):pass
        def _process_response_data(self,payload):pass
    before=Changed._build_request;cp.install(types.SimpleNamespace(BaseClient=Changed))
    assert Changed._build_request is before
    assert r.ADAPTERS['Changed._build_request']=='signature mismatch; NOT wrapped'

def test_original_builder_errors_and_conversion_errors_propagate(env):
    pre()
    def broken(self,options):raise ValueError('original builder failed')
    with pytest.raises(ValueError,match='original builder failed'):cp.request_wrapper(broken)(None,options={})
    req=request()
    def broken_response(self,*,data,cast_to,response):raise ValueError('original validation failed')
    with pytest.raises(ValueError,match='original validation failed'):
        cp.response_wrapper(broken_response)(None,data=event(120),cast_to=dict,response=httpx.Response(200,request=req))
    assert record(env)['usage']['cache_write_tokens']==120

def test_observer_storage_failure_does_not_break_original(env,monkeypatch):
    pre();req=request()
    monkeypatch.setattr(r,'store',lambda *a:(_ for _ in ()).throw(OSError('error with sensitive data')))
    def original(self,*,data,cast_to,response):return data
    data=event();assert cp.response_wrapper(original)(None,data=data,cast_to=dict,response=httpx.Response(200,request=req)) is data
    assert r.FAILURES==1

def test_terminal_without_usage_and_diagnostics_metadata(env):
    pre();req=request();data=event(120);data['response']['prompt_cache_diagnostics']={'type':'cache_miss','reason':'tools_changed','comparison_reusable_tokens':1234,'cache_missed_tokens':1234,'prompt':'never-store'}
    emit(req,data);row=record(env)
    assert row['cache_evidence']['diagnostics']['reason']=='tools_changed'
    assert 'prompt' not in row['cache_evidence']['diagnostics']
    missing={'type':'response.completed','response':{'id':'resp_test','prompt_cache_diagnostics':{'type':'unexpected-secret','reason':'private secret'}}}
    emit(req,missing);row=record(env)
    assert row['cache_evidence']['cache_write_state']=='field_absent' and row['usage']['cache_write_tokens']==120
    assert row['cache_evidence']['diagnostics']=={'type':'other','reason':'other'}

def test_multiple_distinct_responses_are_flagged_not_new_inferences_counted(env):
    pre();req=request();emit(req,event(10));data=event(20);data['response']['id']='resp_second';emit(req,data)
    row=record(env)
    assert row['cache_evidence']['multiple_terminal_responses']
    assert row['cache_evidence']['distinct_terminal_ids_seen']==['resp_test','resp_second']
    assert len(Store(env).read()['requests'])==1

def test_non_stream_responses_supported(env):
    pre(api_mode='responses',provider='openai');req=request({'stream':False},url='https://api.openai.com/v1/responses')
    emit(req,{'object':'response',**event(40)['response']})
    row=record(env)
    assert row['cache_evidence']['event_type']=='response.http_body' and row['usage']['cache_write_tokens']==40

def test_conflicting_cache_write_fields_are_not_silently_selected(env):
    pre();req=request();data=event(0);data['response']['usage']['cache_creation_tokens']=100
    emit(req,data);row=record(env)
    assert row['cache_evidence']['cache_write_state']=='conflicting_fields'
    assert row['usage']['cache_write_tokens'] is None and row['usage']['input_tokens'] is None
    assert 'conflicting_cache_write_fields' in row['usage']['warnings']

def test_request_lifetime_releases_mapping(env):
    import gc
    pre();req=request();assert len(cp._REQUESTS)==1
    ref=weakref.ref(req);del req;gc.collect()
    assert ref() is None and len(cp._REQUESTS)==0


def test_completely_missing_wire_usage_cannot_be_filled_by_sdk_defaults(env):
    agent=pre();req=request()
    emit(req,{'type':'response.completed','response':{'id':'resp_missing','usage':None}})
    synthesized={'input_tokens':0,'output_tokens':0,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'total_tokens':0}
    ad.capture_native_response({'usage':synthesized})
    r.post(api_request_id='a',session_id='s',provider='openai-codex',api_mode='codex_responses',model='test-model',usage={'_ai_usage_raw_usage':synthesized},ended_at=time.time())
    row=record(env)
    assert row['cache_evidence']['cache_write_state']=='field_absent'
    assert row['usage']['usage_source']=='wire_terminal_usage'
    assert row['usage']['cache_write_tokens'] is None and row['usage']['total_tokens'] is None
