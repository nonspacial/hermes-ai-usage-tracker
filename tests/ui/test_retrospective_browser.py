"""Offline synthetic backend DTO: retrospective provenance remains visible in UI/CSV."""
import csv
import io
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'],headless=True,
                                  args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1400,'height':1000})
        errors=[];network=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:network.append(r.url))
        page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
        page.evaluate('''() => {
          window.downloads=[];downloadFile=(name,body)=>downloads.push({name,body});
          const old=rest;
          rest=async(path,options)=>{
            const d=await old(path,options);
            if(!path.startsWith('/ledger?')||!d.summary)return d;
            const rate=d.applied_rate_groups?.[0]?.rate;
            if(d.applied_rate_groups?.length){
              d.applied_rate_groups[0].rate={...rate,retrospective:true,observed_at:now-10};
              d.summary.supplemental_requests=1;
            }
            if(d.requests?.length){
              const r=d.requests[0];r.stored_accounting={usage:r.usage,cost:{complete:false,rate:null}};
              r.supplemental_valuation={basis:'current_published_rate_for_past_usage',observed_at:now-10,not_historical_charge:true};
              r.cost={...r.cost,rate:{...r.cost?.rate,retrospective:true}};
            }
            return d;
          }
        }''')
        page.get_by_role('tab',name='Codex',exact=True).click()
        page.get_by_role('group',name='Usage display').get_by_role('button',name='Cost').click()
        expect(page.get_by_test_id('retrospective-overview-note')).to_contain_text('not historical charges')
        page.get_by_role('tab',name='Cache & costs',exact=True).click()
        expect(page.get_by_test_id('retrospective-cost-note')).to_contain_text('not historical charges')
        expect(page.get_by_test_id('published-rates').locator('tbody tr [title*="Retrospective current-published-rate estimate"]')).not_to_have_count(0)
        page.get_by_role('tab',name='Requests',exact=True).click()
        expect(page.get_by_test_id('request-list').locator('tbody tr span[title*="Not a historical charge"]')).not_to_have_count(0)
        page.get_by_role('button',name='Export request CSV',exact=True).click()
        page.wait_for_function('downloads.length===1')
        rows=list(csv.DictReader(io.StringIO(page.evaluate('downloads[0].body'))))
        assert any('current_published_rate_for_past_usage' in r['supplemental_valuation'] and '"rate":null' in r['stored_accounting'] for r in rows)
        assert not errors,errors
        assert not network,network
        browser.close()
    print('PASS retrospective note, applied-rate provenance, request label, original accounting and CSV metadata')

if __name__=='__main__':run()
