"""Regression for the user's normalized-only screenshot; all values are fixtures.
No credentials, prompts, actual live account traffic or pricing requests.
"""
import contextvars, json, time, types
from concurrent.futures import ThreadPoolExecutor
import pytest
from _hermes_ai_usage_ledger_v2 import accounting as a,recorder as r,adapters as ad
from _hermes_ai_usage_ledger_v2.storage import Store

RAW={'input_tokens':143778,'output_tokens':34,'input_tokens_details':{'cached_tokens':137344,'cache_write_tokens':50},'output_tokens_details':{'reasoning_tokens':0},'total_tokens':143812}
CANONICAL={'input_tokens':6434,'output_tokens':34,'cache_read_tokens':137344,'cache_write_tokens':0,'reasoning_tokens':0,'prompt_tokens':143778,'total_tokens':143812}
RATE={'provider':'openai-codex','model':'test-model','service_tier':'standard','input_tokens':'10','output_tokens':'50','cache_read_tokens':'1','cache_write_tokens':'12.5'}
@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path));r._STORES.clear();r._LOOKUP.clear();r.CURRENT.set(None);r.AUX.set(None);r.COMPRESSION.set(None);r.ADAPTERS.clear();r.FAILURES=0
    monkeypatch.setattr(ad,'install',lambda:None)
    return tmp_path

def pre(aid='a1',sid='s1'):
    r.pre(api_request_id=aid,session_id=sid,provider='openai-codex',api_mode='codex_responses',model='test-model',started_at=time.time()-1)
    return types.SimpleNamespace(session_id=sid,_current_api_request_id=aid,provider='openai-codex',api_mode='codex_responses')
def post(aid='a1',sid='s1',usage=None):
    r.post(api_request_id=aid,session_id=sid,provider='openai-codex',api_mode='codex_responses',model='test-model',ended_at=time.time(),usage=usage or CANONICAL)

def test_screenshot_normalized_zero_is_unverified_not_true_zero():
    u=a.normalize(canonical=CANONICAL)
    assert u['cache_write_tokens'] is None and u['input_tokens'] is None
    assert u['total_tokens']==143812 and u['prompt_tokens']==143778 and u['cache_read_tokens']==137344
    assert u['normalized_usage']['input_tokens']==6434 and u['normalized_usage']['cache_write_tokens']==0
    assert u['field_provenance']['cache_write_tokens']=='unverified_normalized_zero'
    assert 'normalized_cache_zero_not_provider_verified' in u['warnings']

def test_captured_explicit_zero_stays_zero():
    raw={**RAW,'input_tokens_details':{'cached_tokens':137344,'cache_write_tokens':0}}
    u=a.normalize(raw)
    assert u['cache_write_tokens']==0 and u['input_tokens']==6434
    assert u['field_provenance']['cache_write_tokens']=='response_field'

def test_absent_write_does_not_infer_uncached_from_residual():
    u=a.normalize({**RAW,'input_tokens_details':{'cached_tokens':137344}})
    assert u['cache_write_tokens'] is None and u['input_tokens'] is None and u['total_tokens']==143812

@pytest.mark.parametrize('name',['cache_write_tokens','cache_creation_tokens'])
def test_top_level_write_aliases(name):
    u=a.normalize({**RAW,'input_tokens_details':{'cached_tokens':137344},name:50})
    assert u['cache_write_tokens']==50 and u['input_tokens']==6384

def test_main_agent_capture_after_context_loss(env):
    agent=pre();r.CURRENT.set(None)
    r.capture_raw(types.SimpleNamespace(usage=RAW),agent);post()
    rec=Store(env).read()['requests'][0]
    assert rec['usage']['cache_write_tokens']==50 and rec['usage']['raw_usage']==RAW
    assert rec['usage']['input_tokens']==6384

def test_native_run_scope_rebinds_in_worker(env):
    agent=pre();r.CURRENT.set(None)
    def consume(events,*,model,on_event=None):
        for event in events:on_event(event)
        return types.SimpleNamespace(usage={'input_tokens':143778,'output_tokens':34})
    consume=ad.codex_stream_wrapper(consume)
    def original(agent,api_kwargs):
        assert r.CURRENT.get()['api_request_id']=='a1'
        return consume([{'type':'response.completed','response':{'usage':RAW,'id':'response-exact'}}],model='test-model')
    wrapped=ad.codex_run_wrapper(original)
    with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(wrapped,agent,{'model':'test-model'}).result()
    post();rec=Store(env).read()['requests'][0]
    assert rec['usage']['cache_write_tokens']==50 and rec['provider_response_id']=='response-exact'
    assert rec['usage']['usage_source']=='native_terminal_usage'

def test_summary_envelope_survives_lost_context(env):
    # Isolate envelope transport: deliberately don't populate registry/CURRENT.
    agent=types.SimpleNamespace(session_id='s1',provider='openai-codex',api_mode='codex_responses')
    def original(self,response):return dict(CANONICAL)
    result=ad.raw_wrapper(original)(agent,types.SimpleNamespace(usage={**RAW,'authorization':'secret'}))
    assert result['_ai_usage_raw_usage']==RAW and 'secret' not in json.dumps(result)
    r.post(session_id='s1',api_request_id='orphan',usage=result)
    u=Store(env).read()['requests'][0]['usage']
    assert u['cache_write_tokens']==50 and u['usage_source']=='response_usage'

def test_exact_pair_no_misattribution(env):
    agent1=pre('one','s1');agent2=pre('two','s1')
    r.CURRENT.set(None)
    r.capture_raw(types.SimpleNamespace(usage=RAW),agent1)
    post('one');post('two')
    rows={x['api_request_id']:x for x in Store(env).read()['requests']}
    assert rows['one']['usage']['cache_write_tokens']==50
    assert rows['two']['usage']['cache_write_tokens'] is None

def test_mismatched_pair_does_not_use_current_session(env):
    agent=pre();bad=types.SimpleNamespace(session_id='s1',_current_api_request_id='wrong',provider='openai-codex',api_mode='codex_responses')
    r.capture_raw(types.SimpleNamespace(usage=RAW),bad)
    assert Store(env).read()['requests'][0].get('usage') is None

def test_terminal_evidence_not_overwritten_by_later_adapter(env):
    agent=pre();r.capture_raw(types.SimpleNamespace(usage=RAW),agent,source='native_terminal_usage')
    reduced={'prompt_tokens':143778,'completion_tokens':34,'prompt_tokens_details':{'cached_tokens':137344,'cache_write_tokens':0}}
    r.capture_raw(types.SimpleNamespace(usage=reduced),agent)
    post();row=Store(env).read()['requests'][0]
    assert row['usage']['cache_write_tokens']==50 and row['usage']['raw_usage']==RAW

def test_late_capture_does_not_reopen_completed_request(env):
    agent=pre();post();r.capture_raw(types.SimpleNamespace(usage=RAW),agent,source='native_terminal_usage')
    row=Store(env).read()['requests'][0]
    assert row['status']=='completed' and row['ended'] and row['usage']['cache_write_tokens']==50

def test_legacy_projection_does_not_modify_db_or_prices(env):
    s=Store(env)
    usage={**CANONICAL,'usage_source':'hermes_normalized','raw_usage':None,'warnings':[],'request_count':1}
    oldcost=a.costs(usage,RATE)
    record={'id':'old','started':10,'ended':11,'provider':'openai-codex','model':'test-model','session_id':'old-s','status':'completed','usage':usage,'cost':oldcost}
    with s.db() as c:
        c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)',('old',10,11,'openai-codex','test-model','old-s','main',None,'completed',json.dumps(record)))
        before=c.execute('SELECT data FROM requests WHERE id="old"').fetchone()[0]
    result=s.read(0,20);view=result['requests'][0]
    assert view['usage']['cache_write_tokens'] is None and view['usage']['input_tokens'] is None
    assert view['stored_accounting']['usage']==usage and view['stored_accounting']['cost']==oldcost
    assert view['cost']['rate']==RATE and view['cost']['complete'] is False
    assert result['summary']['known']['total_tokens']==143812 and result['summary']['missing_fields']['cache_write_tokens']==1
    assert result['summary']['missing_fields']['input_tokens']==1
    assert result['summary']['savings_missing']['cache_savings_usd']==1
    assert result['applied_rate_groups'][0]['known_cost_usd']==view['cost']['known_components_usd']
    assert result['model_groups'][0]['missing_fields']['cache_write_tokens']==1
    with s.db() as c:assert c.execute('SELECT data FROM requests WHERE id="old"').fetchone()[0]==before

def test_legacy_and_current_known_zeros_are_distinct(env):
    s=Store(env)
    legacy={**CANONICAL,'usage_source':'hermes_normalized','raw_usage':None,'warnings':[]}
    rec={'id':'old','started':10,'ended':11,'provider':'openai-codex','model':'test-model','session_id':'s1','status':'completed','usage':legacy}
    s.request(rec,'request_completed')
    raw={**RAW,'input_tokens_details':{'cached_tokens':137344,'cache_write_tokens':0}}
    s.request({**rec,'id':'new','started':12,'ended':13,'usage':a.normalize(raw)},'request_completed')
    result=s.read(0,20,limit=1)
    assert result['request_count']==2 and result['summary']['known']['total_tokens']==2*143812
    assert result['summary']['known']['cache_write_tokens']==0 and result['summary']['missing_fields']['cache_write_tokens']==1

def test_empty_raw_not_invented_by_envelope(env):
    def original(self,response):return CANONICAL
    result=ad.raw_wrapper(original)(types.SimpleNamespace(session_id='absent'),types.SimpleNamespace(usage={}))
    assert '_ai_usage_raw_usage' not in result
    assert a.normalize(canonical=result)['cache_write_tokens'] is None

def test_aux_terminal_is_not_replaced_by_assembled_usage(env):
    module=types.SimpleNamespace()
    item=ad.aux_begin(module,{'model':'test-model'},{'provider':'openai-codex','api_mode':'codex_responses'})
    token=r.AUX.set(item)
    try:
        ad.capture_native_response({'usage':RAW})
        ad.capture_native_response({'usage':{'input_tokens':143778,'output_tokens':34}},'native_assembled_usage')
        ad.aux_end(item,{'usage':{'input_tokens':143778,'output_tokens':34}})
    finally:r.AUX.reset(token)
    rows=Store(env).read()['requests']
    assert len(rows)==1 and rows[0]['usage']['cache_write_tokens']==50
    assert rows[0]['status']=='completed' and rows[0]['usage']['usage_source']=='native_terminal_usage'

def test_raw_zero_replaces_normalized_uncertainty(env):
    agent=pre();post()
    raw={**RAW,'input_tokens_details':{'cached_tokens':137344,'cache_write_tokens':0}}
    r.capture_raw(types.SimpleNamespace(usage=raw),agent,source='native_terminal_usage')
    data=Store(env).read();rec=data['requests'][0]
    assert rec['usage']['cache_write_tokens']==0 and rec['usage']['input_tokens']==6434
    assert data['summary']['missing_fields']['cache_write_tokens']==0

def test_worker_exception_propagates_and_context_restores(env):
    agent=pre();r.CURRENT.set(None)
    def bad(agent,api_kwargs):raise ValueError('provider transport error')
    with pytest.raises(ValueError,match='provider transport error'):
        ad.codex_run_wrapper(bad)(agent,{})
    assert r.CURRENT.get() is None

def test_post_wrong_request_id_cannot_overwrite_current(env):
    agent=pre('one','s1');r.capture_raw(types.SimpleNamespace(usage=RAW),agent)
    r.post(api_request_id='other',session_id='s1',usage=CANONICAL)
    rows=Store(env).read()['requests']
    assert len(rows)==2
    valid=next(x for x in rows if x.get('api_request_id')=='one')
    assert valid['status']=='usage_received' and valid['usage']['cache_write_tokens']==50
    assert any(x.get('orphan_success') for x in rows)

def test_post_exact_pair_recovers_registered_profile(env,monkeypatch,tmp_path):
    pre('one','s1');r.CURRENT.set(None)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'different-home'))
    post('one','s1')
    rows=Store(env).read()['requests']
    assert len(rows)==1 and rows[0]['status']=='completed' and not rows[0]['orphan_success']
