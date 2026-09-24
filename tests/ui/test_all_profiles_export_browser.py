"""Full aggregate pagination/export and failure isolation against offline DTOs."""
import csv
import io
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                   args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        picker = page.get_by_role('combobox', name='Hermes profile', exact=True)
        expect(picker).to_have_value('profile:infra')
        page.evaluate('''()=>{
          const seed=events[0];events.splice(0,events.length,...Array.from({length:1200},(_,i)=>({...seed,id:'collision-'+String(i).padStart(4,'0'),started:now-i-1,ended:now-i-.5})));
          window.downloads=[];downloadFile=(name,body)=>downloads.push({name,body});
          const original=rest;window.failAggregate=false;window.movingExport=false;
          rest=async(path,options)=>{
            const u=new URL(path,'https://offline'),agg=u.searchParams.get('profile_scope')==='all';
            if(agg&&u.pathname==='/ledger'&&window.failAggregate)throw new Error('Synthetic aggregate transport failure');
            const d=await original(path,options);
            if(agg&&u.pathname==='/ledger'&&window.movingExport&&Number(u.searchParams.get('offset'))>0)d.profile_sequences={'p-infra':99999};
            if(window.holdIndividual&&u.searchParams.get('profile')==='default')return new Promise(resolve=>window.holdIndividual.push(()=>resolve(d)));
            if(agg&&window.oldAggregateBackend)return {...d,profile_scope:undefined,read_only:undefined};
            return d;
          };
        }''')
        picker.select_option('scope:all')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(page.get_by_test_id('request-list')).to_contain_text('of 2400 requests')
        first = page.locator('tbody tr').evaluate_all('(rs)=>rs.map(r=>r.dataset.rowId)')
        assert len(first) == len(set(first)) == 200
        page.get_by_role('button', name='Next requests', exact=True).click()
        expect(page.get_by_test_id('request-list')).to_contain_text('Showing 201–400 of 2400 requests')
        second = page.locator('tbody tr').evaluate_all('(rs)=>rs.map(r=>r.dataset.rowId)')
        assert not set(first).intersection(second)
        page.get_by_role('button', name='Previous requests', exact=True).click()
        expect(page.get_by_test_id('request-list')).to_contain_text('Showing 1–200 of 2400 requests')
        assert page.locator('tbody tr').evaluate_all('(rs)=>rs.map(r=>r.dataset.rowId)') == first
        page.get_by_role('button', name='Export request CSV', exact=True).click()
        page.wait_for_function('downloads.length===1')
        rows = list(csv.DictReader(io.StringIO(page.evaluate('downloads[0].body'))))
        assert len(rows) == len({r['id'] for r in rows}) == 2400
        assert {r['profile'] for r in rows} == {'infra', 'default'}
        assert [r['started_utc'] for r in rows] == sorted([r['started_utc'] for r in rows], reverse=True)
        calls = page.evaluate("scopeCalls.filter(c=>c.path.startsWith('/ledger?')&&new URL(c.path,'https://offline').searchParams.get('limit')==='2000').map(c=>Object.fromEntries(new URL(c.path,'https://offline').searchParams))")
        assert [c['offset'] for c in calls] == ['0', '2000']
        assert len({(c['start'], c['end']) for c in calls}) == 1
        assert all(c['profile_scope'] == 'all' and 'profile' not in c for c in calls)
        expect(page.locator('.au-reader')).to_contain_text('not an atomic export')
        page.evaluate('movingExport=true')
        page.get_by_role('button', name='Export request CSV', exact=True).click()
        expect(page.locator('.au-reader')).to_contain_text('Export stopped: profile coverage or ledger sequences changed')
        assert page.evaluate('downloads.length') == 1
        page.evaluate('movingExport=false;failAggregate=true')
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.get_by_test_id('usage-stale')).to_contain_text('last good recorded usage')
        expect(page.locator('tbody tr')).to_have_count(200)
        expect(picker).to_have_value('scope:all')
        page.evaluate('failAggregate=false')
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.locator('tbody tr')).to_have_count(200)
        # Held individual quota/ledger/status must not contaminate aggregate.
        page.evaluate('window.holdIndividual=[]')
        picker.select_option('profile:default')
        page.wait_for_function('holdIndividual.length>=2')
        expect(page.locator('tbody tr')).to_have_count(0)
        picker.select_option('scope:all')
        expect(page.locator('tbody tr')).to_have_count(200)
        page.evaluate('holdIndividual.splice(0).forEach(f=>f());window.holdIndividual=null')
        expect(picker).to_have_value('scope:all')
        assert page.locator('tbody tr').evaluate_all('(rs)=>rs.every(r=>r.dataset.rowId.startsWith("ap1."))')
        # Old backends accepting unknown query parameters cannot look aggregated.
        page.evaluate('window.oldAggregateBackend=true')
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.get_by_test_id('usage-stale')).to_contain_text('backend restart is required')
        expect(page.locator('tbody tr')).to_have_count(200)
        # Capability discovery gates old quota routes before they can probe accounts.
        page.evaluate('''()=>{
          window.oldAggregateBackend=false;discoveredProfiles=null;
          const original=rest;rest=(path,options)=>path==='/ledger/profiles'?Promise.reject(new Error('Synthetic missing inventory route')):original(path,options);
        }''')
        picker.select_option('profile:infra')
        # A new profile restores its own page, never all-profile rows.
        expect(page.locator('tbody tr')).to_have_count(0)
        page.evaluate('window.scopeCalls=[]')
        picker.select_option('scope:all')
        expect(page.get_by_test_id('usage-stale')).to_contain_text('Synthetic missing inventory route')
        assert not page.evaluate("scopeCalls.some(c=>new URL(c.path,'https://offline').pathname==='/ledger'&&new URL(c.path,'https://offline').searchParams.get('profile_scope')==='all')")
        assert not errors, errors
        assert not network, network
        browser.close()
    print('PASS 2400 globally ordered collision-safe rows, 200-row paging, two-page fixed-window CSV, mutation detection, transport failure retaining last good data, old-backend rejection and delayed individual replies')


if __name__ == '__main__':
    run()
