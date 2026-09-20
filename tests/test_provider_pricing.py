"""Provider catalog tests use source-derived schema fixtures and mock transport.
They do not establish live HTTP compatibility or connect to user accounts.
"""
import json,time
from decimal import Decimal
from pathlib import Path
import pytest
from _hermes_ai_usage_ledger_v2 import pricing as p,accounting as a
from _hermes_ai_usage_ledger_v2.storage import Store,summary

BASE={'input_tokens':150,'output_tokens':100,'cache_read_tokens':800,'cache_write_tokens':50,'prompt_tokens':1000,'total_tokens':1100,'reasoning_tokens':40}

def make_catalog(store,source,entries,stamp=None):
    stamp=time.time()-1 if stamp is None else stamp
    data=p._snapshot(source,entries,stamp,'fixture_provider_catalog')
    with store.db() as c:
        c.execute('INSERT INTO provider_catalog(source_id,observed,data) VALUES(?,?,?)',(source,stamp,json.dumps(data)))
    return data

def sample_rate(model='test-model',tier='standard',**kwargs):
    return p.rate(model,tier,['5','30','.5','6.25'],**kwargs)

def req(key='r',**kw):
    return {'id':key,'provider':'openai-codex','model':'test-model','session_id':'s','started':time.time()-1,'ended':time.time(),'status':'completed','usage':dict(BASE),**kw}

def openai_fixture():
    # Minimal semantic HTML representation of the reviewed official table values.
    seed=json.loads(Path(p.__file__).with_name('provider_rates_seed.json').read_text())['snapshots'][0]['rates']
    head='<div>Standard Batch Flex Fast mode</div>'
    for tier in ('standard','batch','flex','fast'):
        head+='<table><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Cache writes</th><th>Output</th><th>Input</th><th>Cached input</th><th>Cache writes</th><th>Output</th></tr>'
        for model in p.LONG_CONTEXT:
            vals=[]
            for band in ('short','long'):
                r=next(r for r in seed if r['model']==model and r['service_tier']==tier and r['context_band']==band)
                vals += [r[k] for k in ('input_tokens','cache_read_tokens','cache_write_tokens','output_tokens')]
            head+='<tr>'+''.join('<td>'+str(x)+'</td>' for x in [model]+vals)+'</tr>'
        head+='</table>'
    return head

def test_seed_from_provider_no_manual_setup(tmp_path):
    s=Store(tmp_path);r=req(model='gpt-6-astra');s.request(r,'request_completed');d=s.read()['requests'][0]
    assert d['cost']['complete'] and Decimal(d['cost']['rate']['input_tokens'])==10
    assert d['cost']['rate']['origin'].startswith('bundled_provider_snapshot')
    assert d['cost']['rate']['source_url']=='https://developers.openai.com/api/docs/pricing'

def test_parse_four_tiers_and_context_bands():
    rows=p.parse_openai(openai_fixture());assert len(rows)==32
    r=next(r for r in rows if r['model']=='gpt-6-astra' and r['service_tier']=='fast' and r['context_band']=='long')
    assert Decimal(r['input_tokens'])==40 and Decimal(r['output_tokens'])==150

@pytest.mark.parametrize('html',['<html>login required</html>',openai_fixture().replace('Fast mode','Other')])
def test_openai_schema_change_fails_closed(html):
    with pytest.raises(ValueError):p.parse_openai(html)

def test_catalog_per_token_conversion_optional_writes_and_long_override():
    body=json.dumps({'data':[{'id':'org/model','pricing':{'prompt':'0.000002','completion':'0.00001','input_cache_read':'0.0000002','overrides':[{'min_prompt_tokens':32000,'prompt':'0.000004'}]}}]})
    rows=p.parse_catalog(body);assert len(rows)==2
    assert Decimal(rows[0]['input_tokens'])==2 and rows[0]['cache_write_tokens'] is None
    assert Decimal(rows[1]['input_tokens'])==4

def test_ttl_price_column_preserved():
    rows=p.parse_catalog(json.dumps({'data':[{'id':'a/b','pricing':{'prompt':'.000001','completion':'.000003','input_cache_read':'.0000001','input_cache_write':'.00000125','input_cache_write_1h':'.000002'}}]}))
    assert Decimal(rows[0]['cache_write_1h_tokens'])==2

def test_ollama_cache_write_unpublished_not_zero():
    rows=p.parse_ollama('<table><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Output</th></tr><tr><td>gpt-oss:120b</td><td>$0.15</td><td>$0.014</td><td>$0.60</td></tr></table>')
    assert Decimal(rows[0]['cache_read_tokens'])==Decimal('.014') and rows[0]['cache_write_tokens'] is None

def test_unknown_models_and_tiers_remain_unpriced(tmp_path):
    s=Store(tmp_path)
    for i,kwargs in enumerate([{'model':'gpt-6-astra-900k'},{'model':'gpt-6-astra','service_tier':'super-fast-unpublished'}]):
        s.request(req(str(i),**kwargs),'request_completed')
    assert all(not r['cost']['complete'] and r['cost']['rate'] is None for r in s.read()['requests'])

def test_returned_model_and_priority_fast_exact_match(tmp_path):
    s=Store(tmp_path);s.request(req(model='gpt-6-astra-900k',response_model='gpt-6-astra',service_tier='priority'),'request_completed')
    rate=s.read()['requests'][0]['cost']['rate'];assert rate['model']=='gpt-6-astra' and rate['service_tier']=='fast' and Decimal(rate['input_tokens'])==20

def test_context_threshold_from_prompt_not_context_capacity(tmp_path):
    s=Store(tmp_path)
    for key,n in [('short',272000),('long',272001)]:
        r=req(key,model='gpt-6-astra');r['usage']['prompt_tokens']=n;s.request(r,'request_completed')
    rates={r['id']:r['cost']['rate'] for r in s.read()['requests']}
    assert Decimal(rates['short']['input_tokens'])==10 and Decimal(rates['long']['input_tokens'])==20

def test_public_snapshot_revision_not_silent_repricing(tmp_path):
    s=Store(tmp_path);make_catalog(s,'openai',[sample_rate()]);r=req();s.request(r,'request_completed')
    before=s.read()['requests'][0]['cost']
    make_catalog(s,'openai',[p.rate('test-model','standard',['99','99','99','99'])],time.time())
    s.request({**r,'ended':time.time()},'request_completed')
    assert s.read()['requests'][0]['cost']==before
    s.request(req('new'),'request_completed')
    assert Decimal(next(r for r in s.read()['requests'] if r['id']=='new')['cost']['rate']['input_tokens'])==99

def test_historical_manual_rates_retained_but_not_selected(tmp_path):
    s=Store(tmp_path);s.rate({'provider':'openai-codex','model':'test-model','input_tokens':'999'})
    s.request(req(),'request_completed');assert s.read()['requests'][0]['cost']['rate'] is None
    assert len(s.read()['rates'])==1

def test_unknown_cached_values_do_not_give_false_savings():
    u=dict(BASE);u['cache_write_tokens']=None
    c=a.costs(u,sample_rate());assert c['cache_write_premium_usd'] is None and c['cache_savings_usd'] is None

def test_net_savings_includes_write_premium():
    c=a.costs(BASE,sample_rate());assert Decimal(c['cache_read_savings_usd'])==Decimal('.0036')
    assert Decimal(c['cache_write_premium_usd'])==Decimal('.0000625')
    assert Decimal(c['cache_savings_usd'])==Decimal('.0035375')

def test_savings_can_be_negative():
    u=dict(BASE,cache_read_tokens=0,cache_write_tokens=1000)
    assert Decimal(a.costs(u,sample_rate())['cache_savings_usd'])==Decimal('-.00125')

def test_one_hour_write_needs_specific_rate():
    u=dict(BASE,cache_write_tokens=50,raw_usage={'cache_creation':{'ephemeral_1h_input_tokens':40,'ephemeral_5m_input_tokens':10}})
    assert a.costs(u,sample_rate())['components']['cache_write_tokens'] is None
    c=a.costs(u,{**sample_rate(),'cache_write_1h_tokens':'10'})
    assert Decimal(c['components']['cache_write_tokens'])==Decimal('.0004625')

def test_refresh_uses_only_public_urls_keeps_last_good(tmp_path):
    s=Store(tmp_path);calls=[]
    def fetch(url):
        calls.append(url)
        if url==p.SOURCES['openai']['url']:return openai_fixture()
        raise ConnectionError('fake secret error must not be saved')
    assert p.refresh(s,force=True,fetcher=fetch)
    d=s.read();status={c['source_id']:c for c in d['price_catalogs']}
    assert status['openai']['status']=='ok' and status['openai']['origin']=='live_public_provider'
    assert status['ollama']['status']=='unavailable' and status['ollama']['rates']
    assert 'fake secret' not in json.dumps(d) and len(calls)==4
    assert set(calls)=={r['url'] for r in p.SOURCES.values()}
    n=len(calls);p.refresh(s,force=True,fetcher=fetch);assert len(calls)==n # button rate limiting

def test_trend_includes_all_pages_and_zero_buckets(tmp_path):
    s=Store(tmp_path);t=int(time.time()//86400)*86400
    for i in range(205):s.request(req(str(i),started=t+10+i,ended=t+20+i),'request_completed')
    s.request(req('next',started=t+86410,ended=t+86420),'request_completed')
    d=s.read(t,t+3*86400,limit=5)
    assert len(d['requests'])==5 and d['request_count']==206
    assert sum(b['known']['total_tokens'] for b in d['trend']['buckets'])==d['summary']['known']['total_tokens']
    assert len(d['trend']['buckets'])==3 and d['trend']['buckets'][-1]['attempts']==0
    assert d['model_groups'][0]['attempts']==206 and d['provider_groups'][0]['attempts']==206
    assert d['summary']['sessions']==1

def test_trend_clip_window_unknown_is_not_zero(tmp_path):
    s=Store(tmp_path);t=int(time.time()//3600)*3600
    s.request(req(started=t+10,usage={}), 'request_completed')
    d=s.read(t+5,t+2000);b=d['trend']['buckets'][0]
    assert b['start']==t+5 and b['end']==t+2000 and b['missing_usage']==1 and b['attempts']==1

def test_python_and_sql_summaries_match(tmp_path):
    s=Store(tmp_path);make_catalog(s,'openai',[sample_rate()])
    for i in range(3):s.request(req(str(i)),'request_completed')
    d=s.read();assert d['summary']==summary(d['requests'])

def test_lookups_do_not_make_network_calls(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'fetch',lambda *_:pytest.fail('Observer attempted network'))
    s=Store(tmp_path);s.request(req(model='gpt-6-astra'),'request_completed');assert s.read()['request_count']==1

def test_provider_endpoint_is_not_caller_controlled():
    with pytest.raises(ValueError):p.fetch('https://attacker.invalid/prices')


def test_variable_router_alias_does_not_poison_catalog():
    rows=p.parse_catalog(json.dumps({'data':[
      {'id':'openrouter/auto','pricing':{'prompt':'-1','completion':'-1'}},
      {'id':'valid/model','pricing':{'prompt':'0.000001','completion':'0.000005'}}]}))
    assert len(rows)==1 and rows[0]['model']=='valid/model'

def test_invalid_context_override_does_not_fall_back_to_short_price():
    rows=p.parse_catalog(json.dumps({'data':[
      {'id':'bad/model','pricing':{'prompt':'.000001','completion':'.000001','overrides':[{'min_prompt_tokens':123,'prompt':'-1'}]}},
      {'id':'good/model','pricing':{'prompt':'0','completion':'0'}}]}))
    assert [r['model'] for r in rows]==['good/model']
