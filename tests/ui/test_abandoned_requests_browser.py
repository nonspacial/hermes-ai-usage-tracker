"""Rendered lifecycle labels with synthetic DTOs only; no host/provider access."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'],headless=True,
                                 args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1700,'height':1100})
        errors=[];network=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda req:network.append(req.url))
        page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
        page.evaluate('''() => {
          const original=demoRest;
          demoRest=async (...args)=>{
            const out=await original(...args);
            if(new URL(args[0],'https://offline').pathname==='/ledger'&&out.summary){
              out.summary={...out.summary,pending:1,unresolved:2,abandoned:3,
                missing_fields:{...out.summary.missing_fields,total_tokens:6},
                missing_reasons:{total_tokens:{awaiting_usage:1,unresolved_execution:2,abandoned_execution:3}}};
              out.requests=out.requests.map((r,i)=>({...r,status:i===2?'abandoned_without_usage':'pending',execution_state:i===0?'unresolved':i===1?'owner_live':'abandoned'}));
            }
            return out;
          };
          plugin.register({rest:demoRest,socket:demoSocket,storage:{get:(k,d)=>demoStored[k]??d,set:(k,v)=>demoStored[k]=v},register:()=>{}});
        }''')
        page.get_by_role('tab',name='All providers',exact=True).click()
        page.get_by_role('tab',name='Requests',exact=True).click()
        page.get_by_role('button',name='Refresh',exact=True).click()
        for scope in ('profile:infra','scope:all'):
            page.get_by_role('combobox',name='Hermes profile',exact=True).select_option(scope)
            expect(page.locator('.au-quality-line')).to_contain_text('1 open (owner observed live) · 2 unresolved · 3 abandoned')
            expect(page.get_by_test_id('usage-totals')).not_to_contain_text('unresolved execution')
            expect(page.get_by_test_id('usage-totals')).not_to_contain_text('abandoned execution')
            expect(page.get_by_test_id('usage-totals')).to_contain_text('1 awaiting usage · subtotal')
            assert not page.get_by_test_id('usage-diagnostics').evaluate('e=>e.open')
            expect(page.get_by_test_id('request-list')).to_contain_text('unresolved (execution unknown)')
            expect(page.get_by_test_id('request-list')).to_contain_text('open (owner observed live)')
            expect(page.get_by_test_id('request-list')).to_contain_text('abandoned_without_usage')
        page.set_viewport_size({'width':390,'height':850})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors, errors
        assert not network, network
        browser.close()
    print('PASS lifecycle labels, missing reasons, request states, All profiles and narrow layout; no network')


if __name__=='__main__':run()
