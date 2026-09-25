"""Offline renderer cache: strict scope, labelled revisits and bounded RAM."""
import os
import re
import time
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def completed_selection_regressions(browser):
    """A hint changing during a successful read must not erase its sibling snapshot."""
    page = browser.new_page(viewport={'width': 1700, 'height': 1050})
    errors, outbound = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: outbound.append(request.url))
    page.route('**/*', lambda route: route.abort())
    page.set_content((ROOT / 'preview.html').read_text())
    page.evaluate('''()=>{
      window.held=[];window.reads=[];window.holdLedger=false;
      window.changeOnDelivery=false;
      const original=rest;
      rest=(path,options)=>{
        if(new URL(path,'https://offline').pathname==='/ledger'){
          reads.push(path);
          if(holdLedger)return new Promise((resolve,reject)=>held.push({path,
            deliver:()=>{if(changeOnDelivery){demoChange++;changeOnDelivery=false}
              original(path,options).then(resolve,reject)}}));
        }
        return original(path,options);
      };
    }''')
    period = page.get_by_role('group', name='Time window')
    page.get_by_role('tab', name='All providers', exact=True).click()
    expect(page.get_by_test_id('ledger-freshness')).to_be_visible()
    period.get_by_role('button', name='Custom', exact=True).click()
    page.get_by_label('Window start').fill('2026-09-24T00:00')
    page.get_by_label('Window end').fill('2026-09-25T00:00')
    expect(page.get_by_test_id('ledger-freshness')).to_be_visible()
    # Mutate the opaque hint while the first Custom GET is held, before the
    # post-read check. The completed response still belongs to these dates.
    page.evaluate('window.holdLedger=true;window.changeOnDelivery=true')
    page.get_by_role('tab', name='Requests', exact=True).click()
    page.wait_for_function('held.length===1')
    custom = page.evaluate('held[0].path')
    custom_query = parse_qs(urlsplit(custom).query)
    assert custom_query['view'] == ['requests'] and 'end' in custom_query, custom_query
    page.evaluate('held.shift().deliver()')
    expect(page.get_by_test_id('request-list')).to_be_visible()
    custom_key = page.evaluate('''path=>ledgerMemoryKey(host.state.connectionId.get(),
      'infra',path,0)''', custom)
    page.wait_for_function('key=>ledgerMemory.has(key)', arg=custom_key)
    page.wait_for_function('path=>held.some(r=>r.path===path)', arg=custom)
    # A dirty hint schedules a fresh read but must not remove the completed
    # Custom entry. Keep reads held so only deliberately delivered reads paint.
    period.get_by_role('button', name='Past 24h', exact=True).click()
    page.wait_for_function('''()=>held.some(r=>{const q=new URLSearchParams(r.path.split('?')[1]);
      return !q.has('end') && Math.abs(Date.now()/1000-Number(q.get('start'))-86400)<30})''')
    page.evaluate('''()=>{const i=held.findIndex(r=>{const q=new URLSearchParams(r.path.split('?')[1]);
      return !q.has('end') && Math.abs(Date.now()/1000-Number(q.get('start'))-86400)<30});
      held.splice(i,1)[0].deliver()}''')
    expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
    page.wait_for_function('''()=>[...ledgerMemory.keys()].some(k=>JSON.parse(k).at(-1)===86400)''')
    period.get_by_role('button', name='Custom', exact=True).click()
    page.wait_for_function('path=>held.filter(r=>r.path===path).length>=2', arg=custom)
    expect(page.get_by_test_id('request-list')).to_be_visible()
    expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Cached snapshot')
    expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Updating')
    assert page.evaluate('key=>ledgerMemory.has(key)', custom_key)
    prior_received = page.evaluate('key=>ledgerMemory.get(key).received', custom_key)
    page.evaluate('''path=>{const i=held.findLastIndex(r=>r.path===path);
      if(i<0)throw Error('missing Custom replacement');held.splice(i,1)[0].deliver()}''', custom)
    expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Snapshot')
    expect(page.get_by_test_id('ledger-freshness')).not_to_contain_text('Cached snapshot')
    expect(page.get_by_test_id('ledger-freshness')).not_to_contain_text('Updating')
    assert page.evaluate('([key,prior])=>ledgerMemory.get(key).received>=prior',
                         [custom_key, prior_received])
    # Another fixed Custom date is a cold selection, not the old date's body.
    page.get_by_label('Window end').fill('2026-09-26T00:00')
    page.wait_for_function('''()=>held.some(r=>new URLSearchParams(r.path.split('?')[1]).get('end')
      ===String(new Date('2026-09-26T00:00').getTime()/1000))''')
    expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Requests')
    assert page.evaluate('key=>ledgerMemory.has(key)', custom_key)
    changed_custom = page.evaluate('held.at(-1).path')
    assert page.evaluate('''([first,second])=>first!==ledgerMemoryKey(
      host.state.connectionId.get(),'infra',second,0)&&
      !ledgerMemory.has(ledgerMemoryKey(host.state.connectionId.get(),'infra',second,0))''',
      [custom_key, changed_custom])
    # The original rolling 24h remains available even after a full 7d read.
    period.get_by_role('button', name='7 days', exact=True).click()
    page.wait_for_function('''()=>held.some(r=>{const q=new URLSearchParams(r.path.split('?')[1]);
      return !q.has('end') && Math.abs(Date.now()/1000-Number(q.get('start'))-604800)<30})''')
    page.evaluate('''()=>{const i=held.findIndex(r=>{const q=new URLSearchParams(r.path.split('?')[1]);
      return !q.has('end') && Math.abs(Date.now()/1000-Number(q.get('start'))-604800)<30});
      held.splice(i,1)[0].deliver()}''')
    expect(page.get_by_test_id('request-list')).to_be_visible()
    page.wait_for_function('''()=>[...ledgerMemory.keys()].some(k=>JSON.parse(k).at(-1)===604800)''')
    period.get_by_role('button', name='Past 24h', exact=True).click()
    page.wait_for_function('''()=>held.some(r=>{const q=new URLSearchParams(r.path.split('?')[1]);
      return !q.has('end') && Math.abs(Date.now()/1000-Number(q.get('start'))-86400)<30})''')
    expect(page.get_by_test_id('request-list')).to_be_visible()
    expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Cached snapshot')
    expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Updating')
    assert page.evaluate('key=>ledgerMemory.has(key)', custom_key)
    assert not errors and not outbound, (errors, outbound)
    page.close()


def refresh_failure_regressions(browser):
    """A manual read completes once, even with an endless dirty background stream."""
    page = browser.new_page()
    errors, outbound = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: outbound.append(request.url))
    page.route('**/*', lambda route: route.abort())
    page.set_content((ROOT / 'preview.html').read_text())
    page.evaluate('''()=>{
      window.flights=[];window.patches=[];
      const original=rest;
      rest=(path,options)=>new URL(path,'https://offline').pathname==='/ledger'
        ?new Promise((resolve,reject)=>flights.push({
           deliver:()=>original(path,options).then(resolve,reject),
           fail:()=>reject(new Error('fixture read failed'))}))
        :original(path,options);
      window.coordinator=makeLedgerCoordinator({profile:'infra',
        path:'/ledger?profile=infra&start=0&view=requests',windowSeconds:0,
        publish:patch=>{patches.push(patch);window.last={...window.last,...patch}}});
      coordinator.start();
    }''')
    page.wait_for_function('flights.length===1')
    page.evaluate('flights[0].deliver()')
    page.wait_for_function('last.data && !last.fetching')
    page.evaluate('coordinator.hint()')
    page.wait_for_function('flights.length===2')
    page.evaluate('coordinator.refresh(); coordinator.refresh(); coordinator.hint()')
    assert page.evaluate('last.manual && flights.length===2')
    page.evaluate('flights[1].deliver()')
    page.wait_for_function('flights.length===3')
    assert page.evaluate('last.manual && flights.length===3')
    page.evaluate("coordinator.hint(); flights[2].fail()")
    page.wait_for_function('last.error && last.manual===false')
    state = page.evaluate('''()=>({same:!!last.data,failed:last.error.message,
      manualPatches:patches.filter(p=>p.manual===false).length,
      flights:flights.length})''')
    assert state['same'] and state['failed'] == 'fixture read failed' and state['flights'] == 3, state
    # A dirty retry may continue, but it never reclaims the manual control.
    page.wait_for_function('flights.length===4')
    assert page.evaluate('last.manual===false && !!last.data')
    page.evaluate('coordinator.dispose(); flights[3].fail()')
    page.evaluate('''()=>{
      window.flights=[];window.patches=[];window.last={};
      window.coordinator=makeLedgerCoordinator({profile:'infra',
        path:'/ledger?profile=infra&start=0&view=requests',windowSeconds:0,
        publish:patch=>{patches.push(patch);window.last={...window.last,...patch}}});
      coordinator.start();
    }''')
    page.wait_for_function('flights.length===1')
    page.evaluate('coordinator.refresh(); coordinator.hint(); flights[0].deliver()')
    page.wait_for_function('flights.length===2')
    assert page.evaluate('last.manual && flights.length===2')
    page.evaluate('coordinator.hint(); flights[1].deliver()')
    page.wait_for_function('last.manual===false && !!last.data')
    page.wait_for_function('flights.length===3')
    assert page.evaluate('last.manual===false && !last.error')
    page.evaluate('coordinator.dispose(); flights[2].fail()')
    assert not errors and not outbound, (errors, outbound)
    page.close()


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1050})
        errors, outbound = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: outbound.append(request.url))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.evaluate('''()=>{
          window.held=[];window.holdLedger=false;window.reads=[];
          const original=rest;
          rest=(path,options)=>{
            const route=new URL(path,'https://offline').pathname;
            if(route==='/ledger/change-token'&&window.failIdentity)
              return original(path,options).then(value=>({...value,coverage:{...value.coverage,discovery:{status:'partial',errors:['fixture']}}}));
            if(route==='/ledger'){
              reads.push(path);
              if(holdLedger)return new Promise((resolve,reject)=>held.push({path,
                deliver:()=>original(path,options).then(resolve,reject)}));
            }
            return original(path,options);
          };
        }''')
        page.evaluate('''()=>{
          window.ageTimers=new Set();const start=window.setInterval,stop=window.clearInterval;
          window.setInterval=function(fn,ms,...args){const id=start(fn,ms,...args);
            if(ms===5000)ageTimers.add(id);return id};
          window.clearInterval=function(id){ageTimers.delete(id);return stop(id)};
        }''')
        page.get_by_role('tab', name='All providers', exact=True).click()
        expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
        # Bounded allocation and identity/TTL are exercised via actual source functions.
        stats = page.evaluate('''()=>{
          clearLedgerMemory();const generation=ledgerMemoryGeneration;
          const sample=n=>({analytics_revision:'r',projection:{version:1,included:['window']},
            window:{start:1,end:2},payload:'x'.repeat(n)});
          for(let i=0;i<30;i++)rememberLedger('entry'+i,sample(3000),'r','hint',10000+i,generation);
          const count=ledgerMemory.size,bytes=ledgerMemoryBytes;
          const oversized=rememberLedger('huge',sample(2*1024*1024),'r','hint',11000,generation);
          const mismatch=cachedLedger('entry28','r','different',11000);
          const expired=cachedLedger('entry29','r','hint',11000+LEDGER_CACHE_TTL+1);
          clearLedgerMemory();const late=rememberLedger('late',sample(3),'r','hint',12000,generation);
          const freshGeneration=ledgerMemoryGeneration;
          for(let i=0;i<8;i++)rememberLedger('large'+i,sample(600000),'r','hint',12000+i,freshGeneration);
          const byteBound={count:ledgerMemory.size,bytes:ledgerMemoryBytes};
          clearLedgerMemory();
          const heavy={analytics_revision:'r',projection:{version:1,included:['requests']},
            window:{start:1,end:2},requests:Array.from({length:9000},(_,i)=>({id:i,
              usage:{input:i,output:i+1},metadata:{provider:'fixture',task:'memory'}}))};
          const heavySize=JSON.stringify(heavy).length*2;
          const heavyStored=rememberLedger('object-heavy',heavy,'r','inventory',13000,ledgerMemoryGeneration);
          const heavyEntry=ledgerMemory.get('object-heavy');
          const serializedOnly=heavyStored&&typeof heavyEntry.payload==='string'&&
            !Object.hasOwn(heavyEntry,'data')&&heavyEntry.size===heavySize&&
            cachedLedger('object-heavy','r','inventory',13001).data.requests.length===9000;
          const heapSample=performance.memory?.usedJSHeapSize??null;
          clearLedgerMemory();
          const sorted=ledgerMemoryKey('conn','infra','/ledger?view=overview&group=model&start=1&project=p',86400)
            ===ledgerMemoryKey('conn','infra','/ledger?project=p&start=500&group=model&view=overview',86400);
          const distinct=ledgerMemoryKey('other','infra','/ledger?view=overview&group=model&start=1&project=p',86400)
            !==ledgerMemoryKey('conn','infra','/ledger?view=overview&group=model&start=1&project=p',86400);
          const invalidSelected=ledgerInventoryKey({coverage:{status:'complete',
            discovery:{status:'complete'},profiles:[{profile_id:'p-else',name:'else',aliases:[],status:'available'}]}},'infra')===null;
          const invalidAll=ledgerInventoryKey({coverage:{status:'partial',
            discovery:{status:'partial'},profiles:[{profile_id:'p-else',name:'else',aliases:[],status:'available'}]}},ALL_PROFILES)===null;
          return {count,bytes,byteBound,oversized,mismatch,expired,late,sorted,distinct,
            serializedOnly,heavySize,heapSample,invalidSelected,invalidAll,remaining:ledgerMemory.size};
        }''')
        assert stats['count'] == 12 and stats['oversized'] is False and stats['mismatch'] is None
        assert stats['expired'] is None and stats['late'] is False and stats['sorted'] and stats['distinct']
        assert stats['remaining'] == 0 and 0 < stats['bytes'] <= 8*1024*1024
        assert stats['serializedOnly'] and stats['heavySize'] < 2*1024*1024, stats
        assert stats['invalidSelected'] and stats['invalidAll'], stats
        assert 0 < stats['byteBound']['count'] < 8 and 0 < stats['byteBound']['bytes'] <= 8*1024*1024, stats
        # First use of Requests has no cached body; revisit is immediate after the
        # revision check, while the new full read remains unresolved.
        page.evaluate('window.holdLedger=true')
        cold_start=time.perf_counter()
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Requests')
        page.wait_for_function('held.length===1')
        page.evaluate('held.shift().deliver()')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        cold_ms=round((time.perf_counter()-cold_start)*1000,1)
        age_before=page.get_by_test_id('ledger-freshness').inner_text()
        expect(page.get_by_test_id('ledger-freshness')).to_contain_text(re.compile(r'([5-9]|1[0-2])s since received'),timeout=13000)
        assert age_before != page.get_by_test_id('ledger-freshness').inner_text()
        assert page.evaluate('ageTimers.size') == 1
        hidden=page.evaluate('''()=>{
          Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});
          document.dispatchEvent(new Event('visibilitychange'));
          const count=ageTimers.size;
          delete document.visibilityState;
          document.dispatchEvent(new Event('visibilitychange'));
          return count;
        }''')
        assert hidden == 0 and page.evaluate('ageTimers.size') == 1
        page.get_by_role('tab', name='Overview', exact=True).click()
        page.wait_for_function('held.length===1')
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Overview')
        page.evaluate('held.shift().deliver()')
        expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
        # The hint can change without accounting changing. The aged snapshot
        # remains visibly old while the replacement read is held.
        page.evaluate('window.demoChange++')
        revisit_start=time.perf_counter()
        page.get_by_role('tab', name='Requests', exact=True).click()
        page.wait_for_function('held.length===1')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Cached snapshot')
        revisit_ms=round((time.perf_counter()-revisit_start)*1000,1)
        expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Updating')
        # The first Requests flight belongs to the disposed owner. A quick
        # Overview/Requests return must start a separate read, not adopt it.
        page.get_by_role('tab', name='Overview', exact=True).click()
        page.wait_for_function('held.length===2')
        page.get_by_role('tab', name='Requests', exact=True).click()
        page.wait_for_function('held.length===3')
        expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Cached snapshot')
        page.evaluate('held[0].deliver()')
        expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Cached snapshot')
        page.evaluate('window.failLedger=true;held[2].deliver()')
        expect(page.get_by_test_id('usage-stale')).to_contain_text('refresh failed')
        expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Update failed')
        expect(page.get_by_test_id('connection-status')).to_have_attribute('data-state', 'limited')
        expect(page.get_by_test_id('connection-status')).to_have_attribute('title', re.compile('Usage refresh failed'))
        page.evaluate('window.failLedger=false;held[1].deliver();held.length=0')
        # Failed identity verification must not replay a cached body.
        page.get_by_role('tab', name='Overview', exact=True).click()
        page.wait_for_function('held.length===1')
        page.evaluate('held.shift().deliver();window.failIdentity=true')
        expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Requests')
        page.wait_for_function('held.length===1')
        expect(page.get_by_test_id('usage-totals')).to_have_count(0)
        page.evaluate('held.shift().deliver();window.failIdentity=false')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        # A different connection cannot display the previous server's response.
        page.evaluate("host.state.connectionId.set('fixture-remote')")
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Requests')
        page.wait_for_function('held.length===1')
        assert page.evaluate('ledgerMemory.size') == 0
        page.evaluate('held.shift().deliver()')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        # Backend generation changes invalidate prior cached reports.
        page.evaluate("window.demoAnalyticsRevision='new-generation'")
        page.get_by_role('tab', name='Overview', exact=True).click()
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Overview')
        page.wait_for_function('held.length===1')
        page.evaluate('held.shift().deliver()')
        expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
        page.evaluate('''()=>{window.demoAnalyticsRevision='newer-generation';window.demoChange++;
          for(const fn of demoSubscribers)fn({type:'changed',mode:'native-events'})}''')
        page.wait_for_function('held.length===1')
        expect(page.get_by_test_id('ledger-view-pending')).to_contain_text('Overview')
        expect(page.locator('.au-breakdown tbody tr')).to_have_count(0)
        page.evaluate('held.shift().deliver()')
        expect(page.locator('.au-breakdown tbody tr').first).to_be_visible()
        # Pane unmount keeps module RAM; full plugin disposal revokes it and
        # outstanding reads cannot repopulate the discarded generation.
        page.evaluate("ReactDOM.unmountComponentAtNode(document.getElementById('root'))")
        assert page.evaluate('ageTimers.size') == 0
        page.evaluate("ReactDOM.render(h(()=>window.page(),{}),document.getElementById('root'))")
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.wait_for_function('held.length===1')
        expect(page.get_by_test_id('ledger-freshness')).to_contain_text('Cached snapshot')
        page.evaluate('window.disposePlugin();held.shift().deliver()')
        page.wait_for_timeout(100)
        assert page.evaluate('ledgerMemory.size') == 0
        page.evaluate('window.holdLedger=false')
        assert not errors, errors
        assert not outbound, outbound
        completed_selection_regressions(browser)
        refresh_failure_regressions(browser)
        browser.close()
    print(f"PASS completed Custom changed-hint storage/follow-up, held cached revisit and replacement, distinct Custom dates, 24h/7d/24h sibling retention; synthetic cold delivered {cold_ms} ms / labelled changed-hint revisit held {revisit_ms} ms; RAM count {stats['count']}, byte-limit retained {stats['byteBound']['count']} entries / {stats['byteBound']['bytes']} serialized UTF-16 payload bytes; object-heavy {stats['heavySize']} payload bytes; browser heap sample {stats['heapSample']} bytes (includes unrelated active/parsed state; not a cache heap cap); TTL/disposal, rapid-return flight, live age, failure, connection, identity and generation isolation; manual dirty-follow-up success/failure release and healthy-recorder/read-error separation")


if __name__ == '__main__':
    run()
