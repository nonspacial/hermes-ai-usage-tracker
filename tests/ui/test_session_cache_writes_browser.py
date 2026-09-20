"""Calculated-write definition throughout real plugin UI under an offline SDK."""
import csv,io,os,json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from test_component_cards import FIXTURE
ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'

def run():
 ART.mkdir(exist_ok=True)
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=browser.new_page(viewport={'width':1700,'height':1100},locale='en-US',color_scheme='light')
  page.set_default_timeout(8000);errors=[];network=[]
  page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:network.append(r.url))
  page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded');page.evaluate(FIXTURE)
  main=page.get_by_role('navigation',name='Providers',exact=True)
  main.get_by_role('tab',name='Codex',exact=True).click()
  summary=page.get_by_test_id('usage-totals')
  card=summary.locator('.au-metric').filter(has=page.locator('.au-muted',has_text='Cache writes'))
  expect(card.locator('.au-number')).to_have_text('15K')
  expect(card).to_contain_text('Calculated from session reads')
  assert page.get_by_test_id('cache-read-growth').count()==0
  sub=page.get_by_role('navigation',name='Provider subpages',exact=True)
  sub.get_by_role('tab',name='Requests',exact=True).click()
  table=page.get_by_test_id('request-list').locator('table')
  expect(table.get_by_role('columnheader',name='Writes · calc.')).to_be_visible()
  text=table.locator('tbody').inner_text();assert '12,000' in text and '3,000' in text
  with page.expect_download() as d:page.get_by_role('button',name='Export request CSV',exact=True).click()
  rows=list(csv.DictReader(io.StringIO(Path(d.value.path()).read_text())))
  written={r['id']:r for r in rows}
  assert written['warm']['cache_write_tokens']=='12000'
  assert written['warm']['provider_cache_write_tokens']=='0'
  assert written['grown']['cache_write_tokens']=='3000'
  assert written['grown']['cache_write_method']=='session_read_delta'
  assert written['grown']['cache_read_tokens']=='15000'
  assert written['grown']['output_tokens']=='20'
  sub.get_by_role('tab',name='Models & tasks',exact=True).click()
  expect(page.get_by_test_id('provider-subpage').locator('table')).to_contain_text('15K')
  sub.get_by_role('tab',name='Cache & costs',exact=True).click()
  costs=page.get_by_test_id('cost-card-cache_write_tokens')
  expect(costs.get_by_test_id('component-amount')).to_have_text('15,000')
  expect(costs.locator('[data-field=reported_cost]')).to_contain_text('$0.0000')
  # A newly completed observation updates the custom counter without a refresh restart.
  page.evaluate("""() => {const last=events.find(r=>r.id==='grown');events.push({...last,id:'grown-again',started:now-20,ended:now-19,process:'new-process',returned_service_tier:'fast',usage:{...last.usage,cache_read_tokens:18000,input_tokens:2000}});queryClient.invalidateQueries()}""")
  expect(card.locator('.au-number')).to_have_text('18K')
  expect(costs.get_by_test_id('component-amount')).to_have_text('18,000')
  # Independent child sequence doesn't borrow its parent's read counter.
  page.evaluate("""() => {const source=events.find(r=>r.id==='grown');for(const [id,age,reads] of [['child-cold',50,0],['child-warm',10,9000]])events.push({...source,id,started:now-age,ended:now-age+1,session_id:'child',subagent_id:'child',agent_kind:'subagent',usage:{...source.usage,cache_read_tokens:reads,input_tokens:20000-reads}});queryClient.invalidateQueries()}""")
  expect(card.locator('.au-number')).to_have_text('27K')
  response=page.evaluate("demoRest('/ledger?profile=infra&start='+(now-3600)+'&end='+now+'&provider=openai-codex&limit=1')")
  assert response['summary']['session_cache_writes']['tokens']==27000
  assert response['subagent_summary']['session_cache_writes']['tokens']==9000
  # Narrow window still finds its earlier predecessor outside the range.
  single=page.evaluate("demoRest('/ledger?profile=infra&start='+(now-22)+'&end='+(now-18)+'&provider=openai-codex')")
  assert single['summary']['session_cache_writes']['tokens']==3000
  assert single['requests'][0]['calculated_cache_writes']['previous_request_id']=='grown'
  page.get_by_test_id('provider-subpage').screenshot(path=str(ART/'cache-writes-calculated.png'))
  for width in (860,390):
   page.set_viewport_size({'width':width,'height':1000})
   expect(costs.get_by_test_id('component-amount')).to_have_text('27,000')
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  page.get_by_test_id('provider-subpage').screenshot(path=str(ART/'cache-writes-mobile.png'))
  assert not errors,errors
  assert not network,network
  print('PASS main writes/Requests/Models/cards/CSV match session deltas; raw usage retained; automatic update; independent child; predecessor before range; desktop/mobile and no network calls')
  browser.close()

if __name__=='__main__':run()
