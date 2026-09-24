"""Real FastAPI/browser admission for selected bucket and factual marker windows."""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from ledger_runtime.api import add_routes
from ledger_runtime.storage import Store
from test_incremental_browser import put


def run():
    with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
        home=Path(home)
        store=Store(home)
        put(store,'hour-seed',time.time()-7200,25)
        marker=store.test('start','Factual marker')
        put(store,'marker-seed',time.time(),40)
        router=APIRouter()
        add_routes(router,lambda profile:(home,profile or 'infra',None),lambda:home)
        app=FastAPI();app.include_router(router)
        client=TestClient(app)
        revision=client.get('/ledger/analytics',params={'profile':'infra'}).json()['revision']
        calls=[];fault=None
        def backend(request):
            nonlocal fault
            u=urlsplit(request['path'])
            result=client.post(request['path'],json=request.get('body') or {}) if request['method']=='POST' else client.get(request['path'])
            calls.append((request['method'],u.path,parse_qs(u.query),result.status_code))
            if result.status_code!=200:raise RuntimeError('HTTP '+str(result.status_code))
            dto=result.json()
            if fault and u.path=='/ledger':
                kind=fault;fault=None
                if kind=='window':dto['window']['start']+=1
                elif kind=='nested':dto['provider_groups']=[None]
                elif kind=='count':dto['provider_groups'][0]['attempts']+=1
                else:raise AssertionError(kind)
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
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            group=page.get_by_role('group',name='Breakdown grouping')
            group.get_by_role('button',name='Hour').click()
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            # The populated Hour row is an actual backend trend bucket, not a
            # guessed wall-clock grid cell. Activate its first-column link.
            dto=page.evaluate('''()=>JSON.parse([...ledgerMemory.values()].at(-1).payload)''')
            populated=next(i for i,row in enumerate(reversed(dto['trend']['buckets'])) if row['attempts'])
            bucket=list(reversed(dto['trend']['buckets']))[populated]
            page.locator('.au-breakdown tbody tr').nth(populated).locator('td').first.locator('button.au-drill').click()
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            expect(page.get_by_test_id('recorded-summary')).to_be_visible()
            expect(page.get_by_test_id('usage-chart')).to_be_visible()
            expect(page.get_by_test_id('usage-totals')).to_be_visible()
            method,route,q,status=calls[-1]
            assert (method,route,status)==('GET','/ledger',200)
            assert float(q['bucket_start'][0])==bucket['start'] and float(q['bucket_end'][0])==bucket['end']
            clipped=client.get('/ledger',params={k:v[0] for k,v in q.items()}).json()
            assert clipped['window']['start']==max(float(q['start'][0]),bucket['start'])
            assert clipped['window']['end']==min(bucket['end'],clipped['window']['end'])
            assert clipped['summary']['attempts']>0
            assert not page.get_by_test_id('usage-stale').count()
            assert not any(c[0]=='POST' for c in calls)
            page.get_by_role('button',name='Remove bucket filter').click()
            expect(page.get_by_test_id('request-navigation')).to_have_count(0)
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            assert not any(c[0]=='POST' for c in calls if 'bucket_start' in c[2])
            # The displayed Custom start is rounded; the marker's actual start
            # must survive in the backend window, including with a bucket.
            page.get_by_role('combobox',name='Saved tests').select_option(marker['id'])
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            method,route,q,status=calls[-1]
            assert q['test_id']==[marker['id']] and 'end' not in q
            marker_dto=client.get('/ledger',params={k:v[0] for k,v in q.items()}).json()
            assert marker_dto['window']['start']==marker['started']
            assert marker_dto['window']['end']>=marker['started']
            assert not page.evaluate('''({dto,query,revision})=>{
              dto.tests=[];return validOverviewDto(dto,'time',{
               start:Number(query.start),end:null,bucketStart:null,bucketEnd:null,
               testId:query.test_id},revision)}''',
              {'dto':marker_dto,'query':{k:v[0] for k,v in q.items()},'revision':revision})
            assert page.get_by_test_id('usage-chart').is_visible()
            # The marker view is minute-resolution. Select its first actual
            # bucket and require the backend's intersection to paint.
            rows=page.locator('.au-breakdown tbody tr')
            assert rows.count()
            rows.first.locator('td').first.locator('button.au-drill').click()
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            q=calls[-1][2]
            assert q['test_id']==[marker['id']] and 'bucket_start' in q
            marker_bucket=client.get('/ledger',params={k:v[0] for k,v in q.items()}).json()
            assert marker_bucket['window']['start']==max(float(q['start'][0]),float(q['bucket_start'][0]),marker['started'])
            assert marker_bucket['window']['end']>=marker_bucket['window']['start']
            assert page.get_by_test_id('usage-chart').is_visible() and not page.get_by_test_id('usage-stale').count()
            # A disjoint marker/bucket is a supported zero-width backend
            # response (not malformed just because end equals start).
            empty_query={k:v[0] for k,v in q.items()}
            empty_query.update(bucket_start=str(marker['started']-7200),
                               bucket_end=str(marker['started']-3600))
            empty=client.get('/ledger',params=empty_query)
            assert empty.status_code==200
            empty=empty.json()
            assert empty['window']['start']==empty['window']['end']==marker['started']
            assert empty['summary']['attempts']==0
            assert len(empty['trend']['buckets'])==1
            assert empty['trend']['buckets'][0]['start']==empty['trend']['buckets'][0]['end']==marker['started']
            assert empty['trend']['buckets'][0]['attempts']==0
            assert page.evaluate('''({dto,query,revision})=>validOverviewDto(dto,'time',{
              start:Number(query.start),end:null,bucketStart:Number(query.bucket_start),
              bucketEnd:Number(query.bucket_end),testId:query.test_id},revision)''',
              {'dto':empty,'query':empty_query,'revision':revision})
            page.get_by_role('button',name='Remove saved test marker filter').click()
            page.get_by_role('button',name='Remove bucket filter').click()
            page.get_by_role('group',name='Time window').get_by_role('button',name='Past 24h').click()
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            assert not any(c[0]=='POST' for c in calls if 'test_id' in c[2] or 'bucket_start' in c[2])
            # A malformed successful GET, whether its bound, nested DTO or
            # exact partition count, must not paint the selected view.
            for kind in ('window','nested','count'):
                group.get_by_role('button',name='Hour').click()
                expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
                fault=kind
                page.locator('.au-breakdown tbody tr').first.locator('td').first.locator('button.au-drill').click()
                page.wait_for_function("()=>document.querySelector('[data-testid=usage-stale]')?.textContent.includes('Invalid Overview')")
                assert not page.get_by_test_id('usage-chart').count(),kind
                assert calls[-1][0:2]==('GET','/ledger') and fault is None,kind
                page.get_by_role('button',name='Remove bucket filter').click()
                expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            for label in ('Project','Session','Subagents'):
                group.get_by_role('button',name=label).click()
                expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
                assert not page.get_by_test_id('usage-stale').count(),label
                assert calls[-1][0:2]==('GET','/ledger'),(label,calls[-1])
            page.get_by_role('group',name='Time window').get_by_role('button',name='Past hour').click()
            expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            assert calls[-1][0:2]==('GET','/ledger') and 'end' not in calls[-1][2]
            assert page.get_by_test_id('usage-chart').is_visible()
            assert not page.get_by_test_id('usage-stale').count()
            # Reopen a saved selection after it falls outside the recent 100.
            # The only marker authority is the persisted row, not Custom grid
            # rounding or a value manufactured in the browser.
            with sqlite3.connect(store.path) as conn:
                for n in range(101):
                    key=f'later-{n:03}'
                    started=marker['started']+n+1
                    fact={'id':key,'started':started,'ended':started+0.25,'label':key}
                    conn.execute('INSERT INTO tests VALUES(?,?,?,?,?)',
                                 (key,started,fact['ended'],key,json.dumps(fact)))
            bare=client.get('/ledger',params={'profile':'infra','view':'overview','group':'time'}).json()
            assert len(bare['tests'])==100 and marker['id'] not in {t['id'] for t in bare['tests']}
            persisted=browser.new_page(viewport={'width':1700,'height':1100})
            persisted_errors=[]
            persisted.on('pageerror',lambda error:persisted_errors.append(str(error)))
            persisted.route('**/*',lambda route:route.abort())
            persisted.expose_function('backendLedger',backend)
            injection='''window.demoStored['usage-view-v1:profile%%3Ainfra']={version:1,profile:'profile:infra',
              provider:'',tab:'Overview',period:'custom',group:'time',
              customStart:new Date(Math.floor(%s/1800)*1800*1000).toISOString().slice(0,16),
              customEnd:'',filterTest:%s};\nplugin.register({''' % (marker['started'],json.dumps(marker['id']))
            persisted.set_content(text.replace('plugin.register({',injection,1))
            persisted.get_by_role('tab',name='All providers',exact=True).click()
            expect(persisted.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
            assert persisted.get_by_test_id('usage-chart').is_visible()
            assert persisted.get_by_test_id('recorded-summary').is_visible()
            assert persisted.get_by_test_id('usage-totals').is_visible()
            assert not persisted.get_by_test_id('usage-stale').count()
            assert calls[-1][0:2]==('GET','/ledger') and calls[-1][2].get('test_id')==[marker['id']], (calls[-3:],persisted.evaluate('window.demoStored'))
            assert calls[-1][3]==200 and not persisted_errors,persisted_errors
            selected=client.get('/ledger',params={k:v[0] for k,v in calls[-1][2].items()}).json()
            assert selected['window']['start']==marker['started']
            assert selected['tests'][-1]==marker
            assert len(selected['tests'])==101
            assert selected['summary']['attempts']>0
            assert not any(c[0]=='POST' for c in calls if 'test_id' in c[2])
            assert not errors,errors
            assert not outbound,outbound
            browser.close()
    print('PASS real FastAPI selected Hour, factual marker/bucket, persisted old marker beyond 100 renders chart/data, malformed GET rejection; no filtered POST')

if __name__=='__main__':run()
