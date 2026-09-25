"""Synthetic browser: append, stale refusal, and renderer-owned frozen list."""
import os
import csv
import io
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(os.environ['TMPDIR']) / 'usage-record-paging'


def run():
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.evaluate('''()=>{
          const first=events[0];events=[];
          for(let i=0;i<21;i++){
            const e=JSON.parse(JSON.stringify(first));
            e.id='page-'+i;e.started=Date.now()/1000-1000+i;e.ended=e.started+0.1;
            e.task='page-task-'+i;events.push(e);
          }
        }''')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Requests', exact=True).click()
        pager = page.get_by_test_id('record-pagination')
        expect(pager).to_contain_text('Showing 10 of 21 records')
        expect(page.get_by_test_id('request-list').locator('tbody tr')).to_have_count(10)
        page.get_by_role('button', name='Load more').click()
        expect(pager).to_contain_text('Showing 20 of 21 records')
        page.get_by_role('button', name='Load more').click()
        expect(pager).to_contain_text('Showing 21 of 21 records')
        assert page.evaluate('''()=>scopeCalls.filter(x=>x.path.startsWith('/ledger?') &&
          new URL(x.path,'https://offline').searchParams.get('view')==='requests').map(x=>{
            let p=new URL(x.path,'https://offline').searchParams;
            return [p.get('list_mode'),p.get('limit'),p.get('offset')]
          }).slice(-3)''') == [['page','10','0'],['page','10','10'],['page','10','20']]
        # A concurrent write invalidates the next page; current rows remain.
        page.get_by_role('tab', name='Overview', exact=True).click()
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(pager).to_contain_text('Showing 10 of 21 records')
        page.evaluate('''()=>{let e={...events[0],id:'late',started:Date.now()/1000-100};events.push(e)}''')
        page.get_by_role('button', name='Load more').click()
        expect(pager).to_contain_text('Showing 10 of 21 records')
        expect(pager.get_by_role('alert')).to_contain_text('Records changed while loading')
        page.get_by_role('button', name='View all records').click()
        expect(pager).to_contain_text('Showing 22 of 22 records')
        page.get_by_role('button', name='Exit frozen view').click()
        # Notify the ordinary refresh coordinator and then capture a complete one-read DTO.
        page.evaluate('''()=>{demoChange++;for(const fn of demoSubscribers)fn({type:'changed',mode:'native-events'})}''')
        expect(pager).to_contain_text('Showing 10 of 22 records')
        page.get_by_role('button', name='View all records').click()
        expect(pager).to_contain_text('Showing 22 of 22 records')
        expect(pager).to_contain_text('Frozen')
        pager.scroll_into_view_if_needed()
        page.mouse.move(0, 0)
        page.get_by_test_id('provider-subpage').screenshot(path=str(SHOTS / 'selected-frozen-requests.png'))
        page.evaluate('''()=>{let e={...events[0],id:'after-freeze',started:Date.now()/1000-10};events.push(e);
          demoChange++;for(const fn of demoSubscribers)fn({type:'changed',mode:'native-events'})}''')
        expect(pager).to_contain_text('Showing 22 of 22 records')
        page.get_by_role('button', name='Exit frozen view').click()
        expect(pager).to_contain_text('Showing 10 of 23 records')
        page.evaluate('''()=>{window.beforeFullFailure=rest;rest=async(path,options)=>{
          if(new URL(path,'https://offline').searchParams.get('list_mode')==='all')
            throw new Error('Full record report exceeds the 20,000-row safety limit; narrow the window.');
          return beforeFullFailure(path,options)
        }}''')
        page.get_by_role('button', name='View all records').click()
        expect(pager).to_contain_text('Showing 10 of 23 records')
        expect(pager.get_by_role('alert')).to_contain_text('20,000-row safety limit')
        page.evaluate('rest=beforeFullFailure')
        picker = page.get_by_role('combobox', name='Hermes profile', exact=True)
        picker.select_option('scope:all')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(pager).to_contain_text('Showing 10 of 46 records')
        page.get_by_role('button', name='View all records').click()
        expect(pager).to_contain_text('Showing 46 of 46 records')
        expect(pager.get_by_text('Frozen · per-profile snapshots')).to_have_attribute('title',
            'Per-profile copied snapshots; no simultaneous cross-profile instant.')
        pager.scroll_into_view_if_needed()
        page.mouse.move(0, 0)
        page.get_by_test_id('provider-subpage').screenshot(path=str(SHOTS / 'all-profiles-frozen-requests.png'))
        page.get_by_role('tab', name='Overview', exact=True).click()
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(pager).to_contain_text('Showing 10 of 46 records')
        page.evaluate('''()=>{events.forEach((e,i)=>{e.response_model='paging-model-'+i});
          demoChange++;for(const fn of demoSubscribers)fn({type:'changed',mode:'native-events'})}''')
        picker.select_option('profile:infra')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Overview', exact=True).click()
        expect(pager).to_contain_text('Showing 10 of 23 records')
        chart = page.get_by_test_id('usage-chart').inner_text()
        page.get_by_role('button', name='Load more').click()
        expect(pager).to_contain_text('Showing 20 of 23 records')
        page.get_by_role('button', name='View all records').click()
        expect(pager).to_contain_text('Showing 23 of 23 records')
        assert page.get_by_test_id('usage-chart').inner_text() == chart
        page.evaluate('''()=>{const first=compressions[0];compressions.splice(0,compressions.length,
          ...Array.from({length:21},(_,i)=>({...first,id:'comp-page-'+i,
            started:Date.now()/1000-300+i,kind:i%2?'micro_compaction':'compression'})));
          demoChange++;for(const fn of demoSubscribers)fn({type:'changed',mode:'native-events'})}''')
        page.get_by_role('tab', name='Compressions', exact=True).click()
        expect(pager).to_contain_text('Showing 10 of 21 records')
        page.get_by_role('combobox', name='Compression type').select_option('micro_compaction')
        expect(pager).to_contain_text('Showing 10 of 10 records')
        assert page.get_by_test_id('compression-table').locator('tbody tr').count() == 10
        page.get_by_role('combobox', name='Compression type').select_option('compression')
        expect(pager).to_contain_text('Showing 10 of 11 records')
        page.get_by_role('button', name='Load more').click()
        expect(pager).to_contain_text('Showing 11 of 11 records')
        page.evaluate('''()=>{window.downloads=[];downloadFile=(name,body)=>downloads.push({name,body});
          window.beforeCsv=rest;rest=(path,options)=>{
            const q=new URL(path,'https://offline').searchParams;
            if(q.get('limit')==='2000'&&q.has('compression_kind'))throw new Error('CSV legacy route rejects compression_kind');
            return beforeCsv(path,options)
          }}''')
        for kind in ('compression', 'micro_compaction'):
            page.get_by_role('combobox', name='Compression type').select_option(kind)
            expect(pager).to_contain_text('records')
            page.get_by_role('button', name='Export request CSV', exact=True).click()
            page.wait_for_function('downloads.length > 0')
            body = page.evaluate('downloads.shift().body')
            assert len(list(csv.DictReader(io.StringIO(body)))) == 23
        assert page.evaluate('''()=>scopeCalls.filter(c=>c.path?.startsWith('/ledger?')&&
          new URL(c.path,'https://offline').searchParams.get('limit')==='2000')
          .every(c=>!new URL(c.path,'https://offline').searchParams.has('compression_kind'))''')
        assert not errors, errors
        browser.close()
    print('PASS 10/21 append, stale refusal, selected/all-profile frozen completeness, grouped chart and compression type paging')


if __name__ == '__main__':
    run()
