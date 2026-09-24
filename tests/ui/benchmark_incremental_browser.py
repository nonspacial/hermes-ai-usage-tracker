"""One-shot disposable in-process HTTP→preview comparison, not a live benchmark."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from ledger_runtime.api import add_routes
from ledger_runtime.storage import Store
from test_incremental_browser import put


def run():
    with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
        root=Path(directory)
        store=Store(root)
        now=time.time()
        for i in range(600):put(store,str(i),now-85000+i*130,i+1)
        router=APIRouter()
        add_routes(router,lambda profile:(root,'infra',None),lambda:root)
        app=FastAPI();app.include_router(router)
        client=TestClient(app)
        revision=client.get('/ledger/analytics').json()['revision']
        timings=[]
        def bridge(request):
            u=urlsplit(request['path']);started=time.perf_counter()
            result=(client.post(u.path+'?'+u.query,json=request['body']) if request['method']=='POST'
                    else client.get(u.path+'?'+u.query))
            payload=result.json()
            timings.append({'method':request['method'],'route':u.path,
                            'mode':payload.get('incremental',{}).get('mode'),
                            'api_ms':round((time.perf_counter()-started)*1000,2),
                            'bytes':len(result.content)})
            assert result.status_code==200,result.text
            return payload
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'],headless=True,
                                      args=['--no-sandbox','--disable-dev-shm-usage'])
            page=browser.new_page(viewport={'width':1700,'height':1100})
            page.route('**/*',lambda route:route.abort())
            page.expose_function('backendLedger',bridge)
            text=(ROOT/'preview.html').read_text().replace('plugin.register({',
                   'window.demoAnalyticsRevision='+json.dumps(revision)+';\nplugin.register({',1)
            page.set_content(text)
            start=time.perf_counter()
            page.get_by_role('tab',name='All providers',exact=True).click()
            page.wait_for_function('''()=>[...ledgerMemory.values()].some(x=>
              JSON.parse(x.payload).incremental?.mode==='snapshot')''')
            cold_ms=round((time.perf_counter()-start)*1000,2)
            page.evaluate('''()=>{window.signal=()=>{demoChange++;for(const fn of demoSubscribers)
               fn({type:'changed',mode:'native-events'})};}''')
            put(store,'correction',now-3600,10)
            start=time.perf_counter();page.evaluate('signal()')
            page.wait_for_function('''()=>[...ledgerMemory.values()].some(x=>
              JSON.parse(x.payload).incremental?.mode==='delta')''')
            delta_ms=round((time.perf_counter()-start)*1000,2)
            # Compare a genuine complete GET through the same bridge and
            # coordinator/renderer/cache path, not a held mock promise.
            page.evaluate('''()=>{window.beforeRevision=JSON.parse([...ledgerMemory.values()].at(-1).payload).incremental.revision;
              const original=rest;window.forceFull=true;
              rest=(path,options)=>forceFull&&path.startsWith('/ledger/refresh?')?
                original('/ledger?'+path.split('?')[1].replace(/&end=[^&]*/,''),
                         {timeoutMs:120000}):original(path,options);}''')
            put(store,'correction',now-3600,20)
            start=time.perf_counter();page.evaluate('signal()')
            page.wait_for_function('''()=>[...ledgerMemory.values()].some(x=>{
              const d=JSON.parse(x.payload);return d.incremental?.mode==='snapshot'&&
                 d.incremental.revision>beforeRevision;
            })''')
            full_ms=round((time.perf_counter()-start)*1000,2)
            # Exact parity is checked at identical response bounds, rather
            # than comparing naturally advancing rolling windows.
            entry=page.evaluate('''()=>JSON.parse([...ledgerMemory.values()].at(-1).payload)''')
            query=page.evaluate('''()=>scopeCalls.filter(c=>c.path.startsWith('/ledger?')&&
              new URL(c.path,'https://offline').searchParams.get('view')==='overview').at(-1).path''')
            oracle=client.get('/ledger',params={**dict(parse_qsl(urlsplit(query).query)),
                                                'start':entry['window']['start'],'end':entry['window']['end']}).json()
            assert {k:v for k,v in entry.items() if k not in ('incremental','generated_at')}=={
                   k:v for k,v in oracle.items() if k not in ('incremental','generated_at')}
            browser.close()
        print(json.dumps({'rows':600,'cold_get_to_paint_ms':cold_ms,
                          'delta_post_to_paint_ms':delta_ms,'forced_full_get_to_paint_ms':full_ms,
                          'bridge_requests':timings},indent=2))


if __name__=='__main__':run()
