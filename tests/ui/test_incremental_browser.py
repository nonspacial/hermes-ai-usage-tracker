"""Paged pane uses complete GETs; signed POST remains supported by the backend."""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from ledger_runtime.api import add_routes
from ledger_runtime.storage import SCHEMA, Store
from ledger_runtime.skills import SCHEMA as SKILLS_SCHEMA


def put(store, key, started, reads):
    store.request(dict(id=key, started=started, ended=started+1, provider='fixture',
                       model='model', response_model='model', session_id='shared',
                       source='main_hook', task='main', status='completed',
                       usage={'cache_read_tokens':reads,'prompt_tokens':reads,'total_tokens':reads}),
                  'request_completed')


def freshness(page):
    page.get_by_test_id('connection-status').hover()
    return page.get_by_test_id('ledger-freshness')

def run():
    with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
        root=Path(directory)
        store=Store(root)
        put(store,'seed',time.time()-7200,10)
        store.test('start','Synthetic open marker')
        legacy=root/'legacy'/'usage-ledger'
        legacy.mkdir(parents=True)
        with sqlite3.connect(legacy/'events.sqlite3') as c:c.executescript(SCHEMA+SKILLS_SCHEMA)
        router=APIRouter()
        add_routes(router,lambda profile:(legacy.parent if profile=='default' else root,profile or 'infra',None),lambda:root)
        app=FastAPI();app.include_router(router)
        client=TestClient(app)
        revision=client.get('/ledger/analytics',params={'profile':'infra'}).json()['revision']
        calls=[]
        fault_get=False
        def backend(request):
            nonlocal fault_get
            url=urlsplit(request['path'])
            response=(client.post(url.path+'?'+url.query,json=request.get('body')) if request['method']=='POST'
                      else client.get(url.path+'?'+url.query))
            calls.append((request['method'],url.path,url.query,response.status_code))
            assert response.status_code==200,response.text
            dto=response.json()
            if fault_get and url.path=='/ledger':
                fault_get=False
                dto['project_options']=[{'id':None,'label':'Invalid'}]
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
            page.get_by_role('tab',name='All providers',exact=True).click()
            expect(freshness(page)).to_contain_text('Updated')
            assert len(calls)==1 and calls[-1][:2]==('GET','/ledger')
            assert 'list_mode=page' in calls[-1][2]
            initial=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
            assert initial['summary']['attempts']==1 and initial['list_count']==1
            assert 'resume_token' not in initial['incremental']
            # The signed backend route still performs a real delta for a legacy
            # non-paged Overview reader; the paged pane must not use that token.
            query={'profile':'infra','start':initial['window']['start'],
                   'end':initial['window']['end'],'view':'overview','group':'model'}
            baseline=client.get('/ledger',params=query)
            assert baseline.status_code==200,baseline.text
            token=baseline.json()['incremental']['resume_token']
            put(store,'added',initial['window']['start']+1800,25)
            delta=client.post('/ledger/refresh',params=query,json={'resume_token':token})
            assert delta.status_code==200,delta.text
            assert delta.json()['incremental']['mode']=='delta'
            fresh=client.get('/ledger',params=query).json()
            assert {k:v for k,v in delta.json().items() if k not in ('incremental','generated_at')}=={
                k:v for k,v in fresh.items() if k not in ('incremental','generated_at')}
            page.evaluate('''()=>{window.signalLedger=()=>{demoChange++;for(const fn of demoSubscribers)
              fn({type:'changed',mode:'native-events'})}}''')
            page.evaluate('signalLedger()')
            page.wait_for_function('''()=>JSON.parse(ledgerMemory.values().next().value.payload).summary.attempts===2''')
            assert calls[-1][:2]==('GET','/ledger') and 'list_mode=page' in calls[-1][2]
            assert not any(method=='POST' for method,_,_,_ in calls)
            before=page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')
            fault_get=True
            page.evaluate('signalLedger()')
            expect(freshness(page)).to_contain_text('Update failed')
            assert page.evaluate('''()=>JSON.parse(ledgerMemory.values().next().value.payload)''')==before
            page.evaluate('signalLedger()')
            page.wait_for_function('''()=>!document.querySelector('[data-testid=ledger-freshness]')?.textContent.includes('Update failed')''')
            page.get_by_role('group',name='Breakdown grouping').get_by_role('button',name='Hour').click()
            expect(freshness(page)).to_contain_text('Updated')
            assert calls[-1][:2]==('GET','/ledger') and 'group=time' in calls[-1][2]
            page.get_by_role('tab',name='Requests',exact=True).click()
            expect(page.get_by_test_id('request-list')).to_be_visible()
            page.evaluate('window.downloads=[];downloadFile=(name,body)=>downloads.push({name,body})')
            page.get_by_role('button',name='Export request CSV',exact=True).click()
            page.wait_for_function("downloads.some(d=>d.name==='hermes-request-ledger.csv')")
            assert any(method=='GET' and 'limit=2000' in query and 'view=' not in query for method,_,query,_ in calls)
            assert not any(method=='POST' for method,_,_,_ in calls)
            assert not errors,errors
            assert not outbound,outbound
            browser.close()
    print('PASS paged real-FastAPI GET refresh, failed GET retention, group/CSV isolation; direct signed non-paged POST delta parity')

if __name__=='__main__':run()
