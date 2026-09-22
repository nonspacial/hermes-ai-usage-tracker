"""Window-specific cache/cost rollups. Rates here are test fixtures, not prices."""
import copy
import json
from decimal import Decimal
import pytest
from _hermes_ai_usage_ledger_v2.storage import Store
from _hermes_ai_usage_ledger_v2.accounting import normalize

RATE={'model':'gpt-5.6-luna','provider':'openai-codex','service_tier':'standard','input_tokens':'.4','output_tokens':'2','cache_read_tokens':'.04','cache_write_tokens':'.5','source':'SYNTHETIC UNIT TEST','pricing_version':'fixture-1'}

def request(key='luna',started=100,model='gpt-5.6-luna',provider='openai-codex',session='s',inp=17224,out=50,**extra):
    u=normalize({'input_tokens':inp,'output_tokens':out,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}})
    return dict(id=key,started=started,ended=started+1,status='completed',provider=provider,model=model,session_id=session,usage=u,**extra)

@pytest.fixture
def store(tmp_path,monkeypatch):
    s=Store(tmp_path)
    monkeypatch.setattr(s,'latest_rate',lambda c,r:copy.deepcopy({**RATE,'provider':r['provider'],'model':r.get('response_model') or r['model']}))
    return s

def test_luna_groups_show_saved_rate_not_catalog(store):
    store.request(request(),'request_completed')
    store.request(request('title',inp=235,out=11,task='title_generation'),'request_completed')
    with store.db() as c:c.execute('DELETE FROM provider_catalog')
    d=store.read(start=90,end=110)
    assert d['price_catalogs'] and all(not c['rates'] for c in d['price_catalogs'])
    [g]=d['applied_rate_groups']
    assert g['model']=='gpt-5.6-luna' and g['attempts']==2
    assert g['known']['input_tokens']==17459 and g['known']['output_tokens']==61
    assert g['known']['total_tokens']==17520 and g['known']['cache_read_tokens']==g['known']['cache_write_tokens']==0
    assert g['rate']==RATE
    assert Decimal(g['known_cost_usd'])==Decimal('0.0071056')
    assert g['known_cost_usd']==d['summary']['known_cost_usd']
    assert Decimal(d['summary']['savings']['cache_savings_usd'])==0

def test_window_filters_entire_set_not_pagination(store):
    for i in range(205):store.request(request(str(i),started=100+i),'request_completed')
    store.request(request('old',started=1,model='old-model'),'request_completed')
    store.request(request('future',started=400,model='future-model'),'request_completed')
    d=store.read(start=100,end=305,offset=200,limit=2)
    assert len(d['requests'])==2 and d['summary']['attempts']==205
    assert len(d['applied_rate_groups'])==1
    assert d['applied_rate_groups'][0]['attempts']==205
    assert d['applied_rate_groups'][0]['known']==d['summary']['known']
    assert d['applied_rate_groups'][0]['known_cost_usd']==d['summary']['known_cost_usd']

@pytest.mark.parametrize('filter_args',[{'provider':'openai-codex'},{'session':'s'},{'agent':'subagent'},{'project':'repo-a'},{'subagent':'child-a'},{'session':'root','session_scope':'family'}])
def test_rollups_respect_every_scope(store,filter_args):
    store.request(request('wanted',agent_kind='subagent',subagent_id='child-a',session_lineage=['root'],project_id='repo-a'),'request_completed')
    store.request(request('other',provider='openrouter',session='other',agent_kind='primary',project_id='repo-b'),'request_completed')
    d=store.read(start=90,end=110,**filter_args)
    assert d['summary']['attempts']==1 and len(d['applied_rate_groups'])==1
    assert d['applied_rate_groups'][0]['known']['total_tokens']==17274

def test_rates_tiers_and_revisions_not_averaged_or_repriced(store,monkeypatch):
    store.request(request('one'),'request_completed')
    updated={**RATE,'input_tokens':'9','pricing_version':'fixture-2'}
    monkeypatch.setattr(store,'latest_rate',lambda c,r:copy.deepcopy(updated))
    store.request(request('two',started=102),'request_completed')
    updated_fast={**updated,'service_tier':'fast','input_tokens':'18'}
    monkeypatch.setattr(store,'latest_rate',lambda c,r:copy.deepcopy(updated_fast))
    store.request(request('three',started=104),'request_completed')
    store.request(request('one'),'request_completed') # Duplicate after a price refresh.
    d=store.read(start=90,end=110)
    gs=d['applied_rate_groups']
    assert len(gs)==3 and sum(g['attempts'] for g in gs)==3
    assert {g['rate']['input_tokens'] for g in gs}=={'.4','9','18'}
    assert sum(Decimal(g['known_cost_usd']) for g in gs)==Decimal(d['summary']['known_cost_usd'])

def test_unknown_pending_and_missing_usage_not_dropped(store,monkeypatch):
    monkeypatch.setattr(store,'latest_rate',lambda c,r:None)
    store.request(request('unpriced'),'request_completed')
    store.request({'id':'pending','started':103,'provider':'openai-codex','model':'pending-model','session_id':'s','status':'pending'},'request_started')
    d=store.read(start=90,end=110)
    assert d['summary']['attempts']==2
    assert len(d['applied_rate_groups'])==2
    pending=next(g for g in d['applied_rate_groups'] if g['model']=='pending-model')
    assert pending['rate'] is None
    assert pending['missing_usage']==pending['attempts']==1
    priced=next(g for g in d['applied_rate_groups'] if g['model']=='gpt-5.6-luna')
    assert priced['rate']['retrospective'] is True
    assert priced['cost_missing_fields']['input_tokens']==0

def test_empty_window_has_no_catalog_rows(store):
    store.request(request(),'request_completed')
    d=store.read(start=200,end=300)
    assert d['applied_rate_groups']==[]
    assert d['summary']['attempts']==0 and d['summary']['known']['total_tokens']==0

def test_returned_model_context_band_and_saved_provenance(store,monkeypatch):
    rate={**RATE,'model':'returned-model','context_band':'long','threshold_tokens':272000,'source_url':'https://example.invalid/test-only','observed_at':10}
    monkeypatch.setattr(store,'latest_rate',lambda c,r:rate)
    store.request(request(model='requested-alias',response_model='returned-model'),'request_completed')
    [g]=store.read(start=90,end=110)['applied_rate_groups']
    assert g['model']=='returned-model'
    assert g['rate']['context_band']=='long' and g['rate']['source_url']==rate['source_url']
