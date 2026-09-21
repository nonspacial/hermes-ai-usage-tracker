"""Exercise packaged single-flight reads and notification options, offline only."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True,
                                   args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1500, 'height': 1000})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        expect(page.get_by_role('tab', name='All providers', exact=True)).to_be_visible()
        result = page.evaluate('''async()=>{
          const original=rest;let calls=0;const finish=[];
          rest=(path)=>{if(!path.startsWith('/synthetic-flight'))return original(path);
            calls++;return new Promise((resolve,reject)=>finish.push({resolve,reject}))};
          try{
            const batch=Array.from({length:20},()=>sharedLedgerRead('/synthetic-flight?profile=infra'));
            await Promise.resolve();const duplicateCalls=calls;
            const other=sharedLedgerRead('/synthetic-flight?profile=default');await Promise.resolve();
            finish[0].resolve({value:'infra'});finish[1].resolve({value:'default'});
            const values=await Promise.all(batch);const isolated=await other;
            const failed=sharedLedgerRead('/synthetic-flight?profile=infra').catch(()=>null);
            await Promise.resolve();finish[2].reject(new Error('synthetic'));await failed;
            const retry=sharedLedgerRead('/synthetic-flight?profile=infra');await Promise.resolve();finish[3].resolve({value:'retry'});await retry;
            return {duplicateCalls,calls,values:values.map(v=>v.value),isolated:isolated.value,remaining:pendingLedgerReads.size};
          }finally{rest=original}
        }''')
        assert result['duplicateCalls'] == 1, result
        assert result['calls'] == 4, result
        assert result['values'] == ['infra'] * 20 and result['isolated'] == 'default'
        assert result['remaining'] == 0, result
        page.evaluate('''()=>{window.refreshOptions=[];const original=queryClient.invalidateQueries.bind(queryClient);
          queryClient.invalidateQueries=(filters,options)=>{window.refreshOptions.push({filters,options});return original(filters,options)}}''')
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.wait_for_function('window.refreshOptions.some(x=>x.filters?.queryKey?.[1]==="ledger")')
        options = page.evaluate('window.refreshOptions.filter(x=>["ledger","skills","connection"].includes(x.filters?.queryKey?.[1]))')
        assert options and all(x.get('options', {}).get('cancelRefetch') is False for x in options), options
        assert not errors, errors
        browser.close()
    print('PASS 20 concurrent same-scope reads share one transport call; profiles isolate; failures release flight; retries work')
    print('PASS subscription invalidation never cancels/restarts an in-flight analytics/status/skills query')


if __name__ == '__main__':
    run()
