"""Offline pane integration against real in-process FastAPI ledger responses."""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from ledger_runtime.api import add_routes
from ledger_runtime.storage import SCHEMA, Store
from ledger_runtime.skills import SCHEMA as SKILLS_SCHEMA


def put(store, key, started, reads, *, model='model'):
    store.request(dict(id=key, started=started, ended=started+1, provider='fixture',
                       model=model, response_model=model, session_id='shared', source='main_hook',
                       task='main', status='completed',
                       usage={'cache_read_tokens':reads,'prompt_tokens':reads,'total_tokens':reads}),
                  'request_completed')


def run():
    with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as root:
        root=Path(root)
        store=Store(root)
        put(store,'seed',time.time()-7200,10)
        store.test('start','Synthetic open marker')
        legacy=root/'legacy'/'usage-ledger'
        legacy.mkdir(parents=True)
        with sqlite3.connect(legacy/'events.sqlite3') as c:c.executescript(SCHEMA+SKILLS_SCHEMA)
        router=APIRouter()
        add_routes(router, lambda profile:(legacy.parent if profile=='default' else root,profile or 'infra',None), lambda:root)
        app=FastAPI()
        app.include_router(router)
        client=TestClient(app)
        revision=client.get('/ledger/analytics', params={'profile':'infra'}).json()['revision']
        calls=[]
        reject=False
        fault=None
        fault_get=None
        fail_get=False
        def corrupt(dto,kind):
            if kind=='provider_groups':dto.pop('provider_groups')
            elif kind=='trend':dto.pop('trend')
            elif kind=='manifest':dto.pop('projection')
            elif kind=='wrong_group':
                dto['projection']['included'].remove('model_groups')
                dto['projection']['omitted'].append('model_groups')
                dto.pop('model_groups')
            elif kind=='wrong_type':dto['provider_groups']='not an array'
            elif kind=='header_type':dto['summary']='not a summary'
            elif kind=='manifest_partition':dto['projection']['omitted'].pop()
            elif kind=='wrong_window':dto['window']['end']+=60
            elif kind in ('project_options','providers','tests'):
                dto[kind]=[None]
            elif kind=='project_identity':dto['project_options']=[{'id':None,'label':'Bad','path':None,'basis':None}]
            elif kind=='provider_identity':dto['providers']=[{'id':'fixture'}]
            elif kind=='test_identity':dto['tests']=[{'id':None,'started':1,'label':'Bad'}]
            elif kind=='project_provenance':dto['project_options']=[{'id':'p','label':'P','path':None,'basis':[]}]
            elif kind=='test_bounds':dto['tests']=[{'id':'t','started':'now','label':'T'}]
            elif kind=='null_provider_group':dto['provider_groups']=[None]
            elif kind=='null_trend_bucket':dto['trend']['buckets']=[None]
            elif kind=='null_model_group':dto['model_groups']=[None]
            elif kind=='empty_providers':dto['provider_groups']=[]
            elif kind=='empty_trend':dto['trend']['buckets']=[]
            elif kind=='empty_models':dto['model_groups']=[]
            elif kind=='partial_providers':dto['provider_groups'][0]['attempts']-=1
            elif kind=='partial_trend':next(row for row in dto['trend']['buckets'] if row['attempts'])['attempts']-=1
            elif kind=='partial_models':dto['model_groups'][0]['attempts']-=1
            else:raise AssertionError(kind)
        def backend(request):
            nonlocal reject,fault,fault_get,fail_get
            u=urlsplit(request['path'])
            body=request.get('body') or {}
            if reject and u.path=='/ledger/refresh':
                reject=False
                body={'resume_token':body['resume_token'][:-4]+'XXXX'}
            result=(client.post(u.path+'?'+u.query,json=body) if request['method']=='POST'
                    else client.get(u.path+'?'+u.query))
            calls.append((request['method'], u.path, u.query, result.status_code,
                          result.json().get('incremental',{}).get('mode')))
            if fail_get and u.path=='/ledger':
                raise RuntimeError('HTTP 503 fixture fallback failure')
            if result.status_code!=200:
                raise RuntimeError('HTTP '+str(result.status_code))
            dto=result.json()
            if fault and u.path=='/ledger/refresh':
                kind=fault;fault=None
                corrupt(dto,kind)
            if fault_get and u.path=='/ledger':
                kind=fault_get;fault_get=None
                corrupt(dto,kind)
            return dto
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'],headless=True,
                                      args=['--no-sandbox','--disable-dev-shm-usage'])
            page=browser.new_page(viewport={'width':1700,'height':1100})
            errors=[];outbound=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:outbound.append(request.url))
            page.route('**/*',lambda route:route.abort())
            page.expose_function('backendLedger',backend)
            text=(ROOT/'preview.html').read_text().replace('plugin.register({',
                    'window.demoAnalyticsRevision='+json.dumps(revision)+';\nplugin.register({',1)
            page.set_content(text)
            page.get_by_role('tab', name='All providers', exact=True).click()
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            assert len(calls)==1 and calls[0][0:2]==('GET','/ledger') and calls[0][4]=='snapshot',calls
            first=page.evaluate('''()=>({token:ledgerMemory.values().next().value&&
             JSON.parse(ledgerMemory.values().next().value.payload).incremental?.resume_token,
             window:JSON.parse(ledgerMemory.values().next().value.payload).window})''')
            assert first['token'] and first['window']['end']>first['window']['start']
            initial=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
            assert initial['tests'][0].get('ended') is None
            assert initial['project_options'][0]['path'] is None
            assert initial['summary']['missing_fields']['output_tokens']>0
            put(store,'initial',first['window']['start']+1800,25)
            page.evaluate('''()=>{window.heldIncremental=[];window.unavailableAttempts=0;const prior=rest;
              window.heldFallback=[];window.holdFallback=false;
              rest=(path,options)=>path.startsWith('/ledger?')&&window.holdFallback?
                new Promise((resolve,reject)=>heldFallback.push(async()=>{
                  try{resolve(await prior(path,options))}catch(error){reject(error)}})):
                path.startsWith('/ledger/refresh?')?
                new Promise((resolve,reject)=>heldIncremental.push(async()=>{
                  try{
                    if(window.routeUnavailable){window.unavailableAttempts++;
                      throw Object.assign(new Error('Route unavailable'),{status:404})}
                    resolve(await prior(path,options))
                  }catch(error){reject(error)}})):
                prior(path,options);
              window.signalLedger=()=>{demoChange++;for(const fn of demoSubscribers)
                fn({type:'changed',mode:'native-events'})};}''')
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Updating')
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('''()=>scopeCalls.some(c=>c.method==='POST'&&c.path.startsWith('/ledger/refresh?'))''')
            page.wait_for_function('''()=>document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Snapshot")&&
              !document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Updating")''')
            assert len(calls)==2 and calls[-1][4]=='delta',calls
            post=calls[-1]
            post_query='?'+post[2]
            oracle=client.get('/ledger'+post_query).json()
            current=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
            assert {k:v for k,v in current.items() if k not in ('incremental','generated_at')}=={
                k:v for k,v in oracle.items() if k not in ('incremental','generated_at')}
            assert current['incremental']['resume_token']!=first['token']
            assert 'end=' in post[2] and float(current['window']['end'])>first['window']['end']
            # Correct a previously selected row. Only one POST, not a full GET.
            put(store,'initial',first['window']['start']+1800,80)
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('''()=>scopeCalls.filter(c=>c.method==='POST').length===2''')
            page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Updating")''')
            assert len(calls)==3 and calls[-1][4]=='delta',calls
            # A real zero-activity POST remains valid: absence is not inferred
            # from empty arrays, zero counters, or unknown accounting values.
            with store.db() as c:
                c.execute('DELETE FROM requests')
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Updating")''')
            empty=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
            assert calls[-1][:2]==('POST','/ledger/refresh') and empty['summary']['attempts']==0
            assert empty['request_count']==0 and empty['provider_groups']==[] and empty['model_groups']==[]
            assert empty['project_options']==[] and empty['tests'][0].get('ended') is None
            put(store,'seed',first['window']['start']+1800,10)
            put(store,'initial',first['window']['start']+1800,80)
            # Each malformed 200 is still a real FastAPI POST with one field
            # corrupted at the bridge; no candidate reaches paint or RAM cache.
            for kind in ('provider_groups','trend','manifest','wrong_group','wrong_type',
                         'header_type','manifest_partition','wrong_window',
                         'project_options','providers','tests','project_identity',
                         'provider_identity','test_identity','project_provenance','test_bounds',
                         'null_provider_group','null_trend_bucket','null_model_group',
                         'empty_providers','empty_trend','empty_models','partial_providers','partial_trend','partial_models'):
                fault=kind
                page.evaluate('window.holdFallback=true;signalLedger()')
                page.wait_for_function('heldIncremental.length===1')
                prior=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
                prior_text=page.get_by_test_id('ledger-freshness').inner_text()
                prior_totals=page.get_by_test_id('usage-totals').inner_text()
                before=len(calls)
                page.evaluate('heldIncremental.shift()()')
                page.wait_for_function('heldFallback.length===1')
                assert len(calls)==before+1 and calls[-1][:2]==('POST','/ledger/refresh'), (kind,calls)
                assert page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')==prior,kind
                expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Updating')
                assert page.get_by_test_id('usage-totals').inner_text()==prior_totals,kind
                assert 'Cached snapshot' in page.get_by_test_id('ledger-freshness').inner_text() or 'Snapshot' in prior_text
                page.evaluate('window.holdFallback=false;heldFallback.shift()()')
                page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Updating")''')
                assert [c[:2] for c in calls[before:]]==[('POST','/ledger/refresh'),('GET','/ledger')],(kind,calls[before:])
                current=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
                bounds=dict(parse_qsl(calls[-1][2]))
                bounds.update(start=current['window']['start'],end=current['window']['end'])
                oracle=client.get('/ledger?'+urlencode(bounds)).json()
                assert {k:v for k,v in current.items() if k not in ('generated_at','incremental')}=={
                    k:v for k,v in oracle.items() if k not in ('generated_at','incremental')},kind
            # Failed fallback preserves the previous labelled snapshot, not the
            # malformed POST. A later retry is a separate coordinator cycle.
            fault='project_options';fail_get=True
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            previous=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
            before=len(calls)
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('''()=>document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Update failed")''')
            assert [c[:2] for c in calls[before:]]==[('POST','/ledger/refresh'),('GET','/ledger')],calls[before:]
            assert page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')==previous
            fail_get=False
            retry_gets=sum(c[0]=='GET' and c[1]=='/ledger' for c in calls)
            page.evaluate('signalLedger()')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.method==='GET'&&c.path.startsWith('/ledger?')).length>n''',arg=retry_gets)
            page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Update failed")''')
            # A successful but malformed fallback GET is a terminal read error,
            # not a second fallback, and cannot replace the aged snapshot.
            fault='tests';fault_get='project_options'
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            previous=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
            before=len(calls)
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('''()=>document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Update failed")''')
            assert [c[:2] for c in calls[before:]]==[('POST','/ledger/refresh'),('GET','/ledger')],calls[before:]
            assert page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')==previous
            retry_gets=sum(c[0]=='GET' and c[1]=='/ledger' for c in calls)
            page.evaluate('signalLedger()')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.method==='GET'&&c.path.startsWith('/ledger?')).length>n''',arg=retry_gets)
            page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Update failed")''')
            # A rejected signed receipt attempts exactly one full GET, then
            # leaves the view in a complete replacement state.
            reject=True
            before=len(calls)
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.method==='GET'&&c.path.startsWith('/ledger?')).length>n''',arg=sum(c[0]=='GET' and c[1]=='/ledger' for c in calls[:before]))
            page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Updating")''')
            assert [c[0:2] for c in calls[-2:]]==[('POST','/ledger/refresh'),('GET','/ledger')],calls
            # Older backend route missing: one failed POST → GET; thereafter
            # this owner avoids the missing route rather than retrying forever.
            page.evaluate('window.routeUnavailable=true;signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            before=len(calls)
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('unavailableAttempts===1')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.path.startsWith('/ledger?')).length>n''',arg=sum(c[1]=='/ledger' for c in calls[:before]))
            page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Updating")''')
            assert calls[-1][0:2]==('GET','/ledger')
            before=len(calls)
            page.evaluate('signalLedger()')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.path.startsWith('/ledger?')).length>n''',arg=sum(c[1]=='/ledger' for c in calls[:before]))
            assert page.evaluate('unavailableAttempts')==1 and not page.evaluate('heldIncremental.length')
            page.evaluate('window.routeUnavailable=false')
            # A different group must start with GET, never borrow a time token.
            # A cold GET with the same incomplete DTO must fail safely before
            # paint/cache, without recursion or borrowing model rows as time.
            fault_get='project_options'
            before=len(calls)
            page.get_by_role('group',name='Breakdown grouping').get_by_role('button',name='Hour').click()
            page.wait_for_function('''()=>document.querySelector('[data-testid=usage-stale]')?.textContent.includes('Invalid Overview')''')
            assert [c[:2] for c in calls[before:]]==[('GET','/ledger')],calls[before:]
            assert not page.get_by_test_id('ledger-freshness').count()
            assert not page.evaluate('''()=>[...ledgerMemory.values()].some(item=>{
              const dto=JSON.parse(item.payload);return dto.projection?.included.includes('model_groups')===false})''')
            assert not errors,errors
            page.evaluate('signalLedger()')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.path.startsWith('/ledger?')&&
               new URL(c.path,'https://offline').searchParams.get('group')==='time').length>n''',arg=1)
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            assert calls[-1][0:2]==('GET','/ledger') and 'group=time' in calls[-1][2],calls[-1]
            put(store,'seed',time.time()-7200,35)
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            posts_before=sum(c[0]=='POST' for c in calls)
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.method==='POST').length>n''',arg=posts_before)
            page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Updating")''')
            assert calls[-1][4]=='delta' and 'group=time' in calls[-1][2],calls
            # A held old-owner POST cannot publish after connection identity
            # changes; the new owner starts with a GET, not the old receipt.
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            page.evaluate("host.state.connectionId.set('fixture-reconnected')")
            page.wait_for_function('''()=>scopeCalls.filter(c=>c.path.startsWith('/ledger?')&&
               new URL(c.path,'https://offline').searchParams.get('group')==='time').length>=2''')
            page.evaluate('heldIncremental.shift()()')
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            assert calls[-2][0:2]==('GET','/ledger') and calls[-1][0:2]==('POST','/ledger/refresh'),calls[-2:]
            assert page.evaluate("JSON.parse(ledgerMemory.values().next().value.payload).incremental.mode")=='snapshot'
            # Legacy DB with no installed journal returns full DTO and never POSTs.
            with store.db() as c:
                c.execute('DROP TRIGGER analytics_requests_update')
            before=len([c for c in calls if c[1]=='/ledger/refresh'])
            page.evaluate('signalLedger()')
            page.wait_for_function('heldIncremental.length===1')
            posts_before=sum(c[0]=='POST' for c in calls)
            page.evaluate('heldIncremental.shift()()')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.method==='POST').length>n''',arg=posts_before)
            page.wait_for_function('''()=>!document.querySelector("[data-testid=ledger-freshness]")?.textContent.includes("Updating")''')
            assert calls[-1][4] is None
            before_gets=sum(c[0]=='GET' and c[1]=='/ledger' and 'group=time' in c[2] for c in calls)
            page.evaluate('signalLedger()')
            page.wait_for_function('''(n)=>scopeCalls.filter(c=>c.path.startsWith('/ledger?')&&
                new URL(c.path,'https://offline').searchParams.get('group')==='time').length>n''',arg=before_gets)
            assert len([c for c in calls if c[1]=='/ledger/refresh'])==before+1
            # Other tab and CSV continue to use GET (unprojected CSV), no POST.
            page.get_by_role('tab',name='Requests',exact=True).click()
            expect(page.get_by_test_id('request-list')).to_be_visible()
            page.evaluate('window.downloads=[];downloadFile=(name,body)=>downloads.push({name,body})')
            page.get_by_role('button',name='Export request CSV',exact=True).click()
            page.wait_for_function('''()=>downloads.some(d=>d.name==='hermes-request-ledger.csv')''')
            csv_paths=page.evaluate("scopeCalls.filter(c=>c.path.startsWith('/ledger?')&&new URL(c.path,'https://offline').searchParams.get('limit')==='2000').map(c=>c.path)")
            assert csv_paths and all('view=' not in v and 'group=' not in v for v in csv_paths)
            # Actual old-schema profile (no changefeed objects at all).
            page.get_by_role('combobox',name='Hermes profile').select_option(label='default · default')
            page.get_by_role('tab',name='All providers',exact=True).click()
            page.get_by_role('tab',name='Overview',exact=True).click()
            page.wait_for_function('''()=>scopeCalls.some(c=>c.path.startsWith('/ledger?')&&
               new URL(c.path,'https://offline').searchParams.get('profile')==='default')''')
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            assert calls[-1][0:2]==('GET','/ledger') and calls[-1][4] is None,calls[-1]
            posts_before=len([c for c in calls if c[1]=='/ledger/refresh'])
            page.evaluate('signalLedger()')
            page.wait_for_function('''()=>scopeCalls.filter(c=>c.path.startsWith('/ledger?')&&
               new URL(c.path,'https://offline').searchParams.get('profile')==='default').length>=2''')
            assert len([c for c in calls if c[1]=='/ledger/refresh'])==posts_before
            page.get_by_role('combobox',name='Hermes profile').select_option(label='All profiles')
            page.get_by_role('tab',name='All providers',exact=True).click()
            expect(page.get_by_test_id('profile-coverage')).to_be_visible()
            page.evaluate('signalLedger()')
            page.wait_for_function('''()=>scopeCalls.some(c=>c.path.startsWith('/ledger?')&&
               new URL(c.path,'https://offline').searchParams.get('profile_scope')==='all')''')
            assert len([c for c in calls if c[1]=='/ledger/refresh'])==posts_before
            assert not errors,errors
            assert not outbound,outbound
            browser.close()
    print('PASS real FastAPI GET/token/held POST, zero/unknown/model/time parity, nested/count POST faults and one GET, invalid GET/failed fallback, reject/404/legacy, connection and CSV')


if __name__=='__main__':run()
