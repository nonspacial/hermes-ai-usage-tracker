import asyncio,contextvars,importlib.util,json,subprocess,sys,time,types
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import pytest
from fastapi import FastAPI,APIRouter
from fastapi.testclient import TestClient
from _hermes_ai_usage_ledger_v2 import accounting as a,recorder as r,adapters as ad
from _hermes_ai_usage_ledger_v2.storage import Store,summary
from _hermes_ai_usage_ledger_v2.api import add_routes
ROOT=Path(__file__).resolve().parents[1]
RAW={'input_tokens':1000,'output_tokens':100,'input_tokens_details':{'cached_tokens':800,'cache_write_tokens':50},'output_tokens_details':{'reasoning_tokens':40},'total_tokens':1100}

def request(key='r1',started=None):
    return {'id':key,'started':started or time.time(),'provider':'openai-codex','model':'test-model','session_id':'s1','source':'main_hook','task':'main','status':'pending'}
@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path));r._STORES.clear();r._LOOKUP.clear();r.CURRENT.set(None);r.AUX.set(None);r.COMPRESSION.set(None);r.ADAPTERS.clear();r.FAILURES=0
    monkeypatch.setattr(ad,'install',lambda:None)
    return tmp_path

def test_openai_decomposition():
    u=a.normalize(RAW);assert u['input_tokens']==150;assert u['cache_read_tokens']==800;assert u['cache_write_tokens']==50;assert u['total_tokens']==1100
    assert u['reasoning_tokens']==40 and sum(u[k] for k in a.BUCKETS)==1100

def test_missing_cache_write_not_zero():
    u=a.normalize({'input_tokens':1000,'output_tokens':100,'input_tokens_details':{'cached_tokens':800}})
    assert u['cache_write_tokens'] is None and u['input_tokens'] is None and u['total_tokens']==1100

def test_explicit_zero_is_zero():
    u=a.normalize({'prompt_tokens':1000,'completion_tokens':100,'prompt_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}})
    assert u['cache_write_tokens']==0 and u['input_tokens']==1000

def test_anthropic_input_exclusive():
    u=a.normalize({'input_tokens':100,'output_tokens':20,'cache_read_input_tokens':200,'cache_creation_input_tokens':30})
    assert u['prompt_tokens']==330 and u['total_tokens']==350

def test_zero_usage_valid():
    u=a.normalize({'input_tokens':0,'output_tokens':0,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}})
    assert u['total_tokens']==0 and u['usage_source']=='response_usage'

def test_invalid_overlapping_cache_flags():
    u=a.normalize({'input_tokens':10,'output_tokens':1,'input_tokens_details':{'cached_tokens':20,'cache_write_tokens':1}})
    assert u['input_tokens'] is None and 'cache_components_exceed_input' in u['warnings']

def test_numeric_allowlist_no_content_or_secret():
    raw={**RAW,'authorization':'secret','content':'secret','api_key':1234,'input_tokens_details':{'cached_tokens':800,'cache_write_tokens':50,'secret':999}}
    s=json.dumps(a.safe_usage(raw));assert 'secret' not in s and 'authorization' not in s

def test_decimal_component_costs():
    rate={k:v for k,v in zip(a.BUCKETS,['5','30','0.5','6.25'])}
    c=a.costs(a.normalize(RAW),rate)
    assert Decimal(c['total_usd'])==Decimal('0.0044625')
    assert Decimal(c['components']['cache_read_tokens'])==Decimal('0.0004')
    assert Decimal(c['components']['cache_write_tokens'])==Decimal('0.0003125')

@pytest.mark.parametrize('bad',['NaN','-1','Infinity'])
def test_invalid_prices_rejected(bad):
    with pytest.raises(ValueError):a.validate_rate({'provider':'p','model':'m','input_tokens':bad})

def test_rates_unknown_are_not_free():
    c=a.costs(a.normalize(RAW),None);assert c['total_usd'] is None and c['complete'] is False

def test_unknown_fast_tier_not_default_priced(monkeypatch):
    assert a.builtin_rate('openai-codex','anything','priority') is None

def test_persistence_dedup_and_profile_isolation(env):
    s=Store(env);rec=request();s.request(rec,'request_started');s.request(rec,'request_started')
    s.request({'id':rec['id'],'ended':time.time(),'status':'completed','usage':a.normalize(RAW)},'request_completed')
    s.request({'id':rec['id'],'ended':time.time(),'status':'completed','usage':a.normalize(RAW)},'request_completed')
    data=Store(env).read();assert data['request_count']==1 and data['summary']['known']['total_tokens']==1100
    assert Store(env/'another').read()['request_count']==0
    assert not (env/'state.db').exists()

def test_sqlite_write_concurrency(env):
    s=Store(env)
    def one(i):s.request({**request(str(i)),'usage':a.normalize(RAW),'status':'completed'},'request_completed')
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(one,range(40)))
    assert s.read()['summary']['known']['total_tokens']==44000

def test_filter_and_export_pagination(env):
    s=Store(env)
    for i in range(10):s.request({**request(str(i),started=100+i),'provider':'p' if i%2 else 'q'},'request_started')
    d=s.read(102,108,'p',limit=2);assert d['request_count']==3 and len(d['requests'])==2 and d['next_offset']==2

def test_next_call_correlation_and_no_double_count(env):
    s=Store(env);s.compression({'id':'c1','started':100,'ended':101,'session_id':'old','session_after':'s1','provider':'openai-codex','status':'committed'})
    rec=request(started=102);s.request(rec,'request_started')
    s.request({'id':rec['id'],'ended':103,'status':'completed','usage':a.normalize(RAW)},'request_completed')
    s.request({**request('aux',started=100.5),'compression_id':'c1','source':'auxiliary_adapter','task':'compression','usage':a.normalize(RAW),'status':'completed'},'request_completed')
    d=s.read(0,200);c=d['compressions'][0]
    assert c['next_request_id']=='r1' and c['next_request_prompt_tokens']==1000
    assert c['auxiliary']['known']['total_tokens']==1100 and d['summary']['known']['total_tokens']==2200

def test_legacy_manual_rate_not_used_for_new_requests(env):
    s=Store(env);s.rate({'provider':'openai-codex','model':'test-model','input_tokens':5,'output_tokens':30,'cache_read_tokens':.5,'cache_write_tokens':6.25})
    s.request({**request(),'ended':time.time(),'status':'completed','usage':a.normalize(RAW)},'request_completed')
    c=s.read()['requests'][0]['cost'];assert not c['complete'] and c['rate'] is None
    assert len(s.read()['rates'])==1 # historical metadata preserved, never applied automatically

def test_native_hooks_no_prompt_leak(env):
    t=time.time();r.pre(api_request_id='one',session_id='s1',provider='openai-codex',model='m',started_at=t,request_messages=[{'content':'SUPERSECRET'}],request={'body':{'reasoning':{'effort':'high'},'service_tier':'priority','input':'SUPERSECRET'}})
    r.capture_raw(types.SimpleNamespace(usage=RAW),types.SimpleNamespace(session_id='s1'))
    r.post(api_request_id='one',session_id='s1',provider='openai-codex',model='m',started_at=t,ended_at=t+1,usage={'input_tokens':1})
    d=Store(env).read(end=t+5);assert d['summary']['known']['total_tokens']==1100
    assert d['requests'][0]['service_tier']=='priority'
    with Store(env).db() as c:payload=''.join(x[0] for x in c.execute('SELECT data FROM events'))
    assert 'SUPERSECRET' not in payload

def test_raw_usage_survives_failed_normalization(env):
    t=time.time();r.pre(api_request_id='one',session_id='s1',started_at=t,provider='openai-codex')
    r.capture_raw(types.SimpleNamespace(usage=RAW),types.SimpleNamespace(session_id='s1'))
    r.error(api_request_id='one',session_id='s1',ended_at=t+1,error=RuntimeError('SECRET'))
    d=Store(env).read(end=t+5);assert d['requests'][0]['status']=='failed' and d['summary']['known']['total_tokens']==1100

def test_signature_guard(env):
    obj=types.SimpleNamespace(fn=lambda x:x)
    original=obj.fn;ad.patch(obj,'fn',lambda fn:None,['unsupported']);assert obj.fn is original

def test_aux_sync_pass_through_and_error(env):
    mod=types.SimpleNamespace(_RELAY_AUX_CALL_CONTEXT=contextvars.ContextVar('at',default={'task':'compression'}),_RUNTIME_MAIN_CONTEXT=contextvars.ContextVar('rt',default={'session_id':'s1'}))
    response=types.SimpleNamespace(usage=RAW,model='m');seen=[]
    def original(client,kwargs,**options):seen.append((client,kwargs,options));return response
    wrapped=ad.auxiliary_wrapper(mod)(original);args={'model':'m','messages':['SUPERSECRET']};client=object()
    assert wrapped(client,args,provider='openai-codex') is response
    assert seen[0][1] is args and Store(env).read()['summary']['known']['total_tokens']==1100
    def bad(client,kwargs,**options):raise TimeoutError('secret')
    with pytest.raises(TimeoutError):ad.auxiliary_wrapper(mod)(bad)(client,args,provider='openai-codex')
    assert Store(env).read()['request_count']==2

def test_aux_async_pass_through(env):
    mod=types.SimpleNamespace();response=types.SimpleNamespace(usage=RAW)
    async def orig(client,kwargs,**opts):return response
    assert asyncio.run(ad.auxiliary_wrapper(mod,True)(orig)(None,{'model':'m'},provider='openai-codex')) is response
    assert Store(env).read()['summary']['known']['total_tokens']==1100

def test_codex_terminal_preserved_before_adapter_drops_cache(env):
    mod=types.SimpleNamespace();item=ad.aux_begin(mod,{'model':'m'},{'provider':'openai-codex','api_mode':'codex_responses'});token=r.AUX.set(item)
    terminal={'type':'response.completed','response':{'usage':RAW,'id':'resp123'}};seen=[]
    def consume(event_iter,*,model,on_event=None):
        for event in event_iter:on_event(event)
        return types.SimpleNamespace(usage=RAW)
    result=ad.codex_stream_wrapper(consume)(iter([terminal]),model='m',on_event=seen.append)
    r.AUX.reset(token);ad.aux_end(item,types.SimpleNamespace(usage={'prompt_tokens':1000,'completion_tokens':100}))
    d=Store(env).read();assert len(seen)==1 and d['requests'][0]['usage']['cache_read_tokens']==800 and d['request_count']==1

def test_compression_wrapper_never_modifies_original(env):
    cc=types.SimpleNamespace(_last_compression_telemetry={'current_estimated_tokens':800,'effective_threshold':750,'main_context_limit':1000,'focus_topic':'SECRET'},last_compression_rough_tokens=100)
    agent=types.SimpleNamespace(context_compressor=cc,session_id='s1',provider='openai-codex',model='m')
    messages=['SECRET'];result=(['summary'],'system')
    def original(agent,messages,system_message,**kw):
        ad.native_comp_event(agent,{'commit_status':'committed','split_status':'in_place'});return result
    assert ad.compression_wrapper(original)(agent,messages,'secret',approx_tokens=800) is result
    c=Store(env).read()['compressions'][0];assert c['before_estimated_tokens']==800 and 'SECRET' not in json.dumps(c)

def test_api_http_validation_and_tests(env):
    router=APIRouter();add_routes(router,lambda p:(env,None,None) if p!='bad' else (None,None,'unknown profile'),lambda:env)
    app=FastAPI();app.include_router(router);client=TestClient(app)
    assert client.get('/ledger').status_code==200
    assert client.get('/ledger?profile=bad').status_code==404
    assert client.get('/ledger?start=5&end=1').status_code==400
    t=client.post('/ledger/tests',json={'action':'start','label':'test'}).json()
    assert client.post('/ledger/tests',json={'action':'stop','id':t['id']}).status_code==200
    assert client.post('/ledger/rates',json={'provider':'p','model':'m','input_tokens':-1}).status_code==410
    assert client.post('/ledger/pricing/refresh').json()['status']=='queued'

def test_websocket_event_delivery(env):
    router=APIRouter();add_routes(router,lambda p:(env,None,None),lambda:env);app=FastAPI();app.include_router(router)
    with TestClient(app) as client:
        with client.websocket_connect('/ledger/events') as ws:
            hello=ws.receive_json();assert hello['type']=='connected'
            if hello['mode']=='native-events':
                Store(env).request(request(),'request_started');assert ws.receive_json()['type']=='changed'

def test_weighted_cache_rate():
    rows=[{'usage':a.normalize(RAW)} for _ in range(2)];s=summary(rows);assert s['cache_hit_rate']==.8
    rows.append({'usage':a.normalize({'input_tokens':100,'output_tokens':1})});assert summary(rows)['cache_hit_rate'] is None

def test_installer_dry_run_apply_rollback(tmp_path):
    home=tmp_path/'home';home.mkdir();old=home/'desktop-plugins/ai-usage-tracker/plugin.js';old.parent.mkdir(parents=True);old.write_text('OLD')
    cmd=[sys.executable,str(ROOT/'install.py'),'--home',str(home)]
    subprocess.run(cmd,check=True,capture_output=True);assert old.read_text()=='OLD' and not (home/'plugins').exists()
    subprocess.run(cmd+['--apply'],check=True,capture_output=True);assert old.read_text()!='OLD'
    receipt=next(home.glob('usage-ledger-backups/*/receipt.json'))
    subprocess.run([sys.executable,str(ROOT/'install.py'),'--rollback',str(receipt),'--apply'],check=True,capture_output=True)
    assert old.read_text()=='OLD' and not (home/'plugins/ai-usage-tracker/__init__.py').exists()

def test_installer_symlinks_refused(tmp_path):
    home=tmp_path/'home';home.mkdir();outside=tmp_path/'elsewhere';outside.mkdir();(home/'plugins').symlink_to(outside)
    res=subprocess.run([sys.executable,str(ROOT/'install.py'),'--home',str(home),'--apply'],capture_output=True,text=True)
    assert res.returncode==1 and not list(outside.iterdir())

def test_root_layout_installer_excludes_repository_and_private_files(tmp_path):
    spec=importlib.util.spec_from_file_location('root_layout_installer',ROOT/'install.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    home=tmp_path/'home';home.mkdir()
    _,entries=module.plan(home)
    source_paths={str(src.relative_to(ROOT)) for src,_ in entries}
    assert {'plugin.yaml','__init__.py','bootstrap.py','desktop/plugin.js',
            'dashboard/plugin_api.py','ledger_runtime/session_cache_writes.py'}<=source_paths
    assert all(not path.startswith(('.git/','.local-history/','tests/','.venv/','catalog/'))
               for path in source_paths)
    assert not {'install.py','preview.html','requirements-dev.txt'} & source_paths
    assert all(dest.is_relative_to(home) for _,dest in entries)

def test_original_backend_mounts(env):
    path=ROOT/'dashboard/plugin_api.py';spec=importlib.util.spec_from_file_location('test_api',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    paths={getattr(r,'path','') for r in m.router.routes};assert {'/usage','/profiles','/health','/ledger','/ledger/events'}<=paths

def test_anthropic_missing_cache_does_not_invent_total():
    u=a.normalize({'input_tokens':100,'output_tokens':10,'cache_read_input_tokens':200},api_mode='anthropic')
    assert u['prompt_tokens'] is None and u['total_tokens'] is None

def test_exact_saved_test_boundary(env):
    s=Store(env);t=s.test('start');s.request(request('after'),'request_started');stop=s.test('stop',key=t['id'])
    assert s.test_window(t['id'])==(t['started'],stop['ended'])
    router=APIRouter();add_routes(router,lambda p:(env,None,None),lambda:env);app=FastAPI();app.include_router(router)
    d=TestClient(app).get('/ledger',params={'test_id':t['id'],'start':0}).json()
    assert d['window']['start']==t['started'] and d['window']['end']==stop['ended']

def test_rollback_refuses_changed_file(tmp_path):
    home=tmp_path/'home';home.mkdir();cmd=[sys.executable,str(ROOT/'install.py'),'--home',str(home),'--apply'];subprocess.run(cmd,check=True,capture_output=True)
    js=home/'desktop-plugins/ai-usage-tracker/plugin.js';js.write_text('USER NEW CHANGES')
    receipt=next(home.glob('usage-ledger-backups/*/receipt.json'))
    result=subprocess.run([sys.executable,str(ROOT/'install.py'),'--rollback',str(receipt),'--apply'],capture_output=True)
    assert result.returncode==1 and js.read_text()=='USER NEW CHANGES'

def test_native_stream_callback_exception_is_preserved(env):
    r.pre(api_request_id='stream-fail',session_id='s1',provider='openai-codex')
    def original(event_iter,*,model,on_event=None):
        for event in event_iter:on_event(event)
    def callback(event):raise TimeoutError('timeout')
    terminal={'type':'response.completed','response':{'usage':RAW}}
    with pytest.raises(TimeoutError):ad.codex_stream_wrapper(original)([terminal],model='m',on_event=callback)
    assert Store(env).read()['summary']['known']['total_tokens']==1100


def test_returned_tier_preserved_and_priced(env):
    root=env
    r.pre(api_request_id='tier',session_id='s',provider='openai-codex',model='m',started_at=time.time())
    r.capture_raw({'id':'resp_tier','model':'m','service_tier':'priority','usage':RAW})
    r.post(api_request_id='tier',session_id='s',provider='openai-codex',model='m',ended_at=time.time())
    row=Store(root).read()['requests'][0]
    assert row['returned_service_tier']=='priority'
    assert row['provider_response_id']=='resp_tier'
    assert row['cost']['rate'] is None
