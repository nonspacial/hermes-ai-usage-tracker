"""Test.12: local date filters, selected request rates, and complete-set totals.
Only synthetic offline data is used. No provider or user account calls.
"""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'

# Two Luna-shaped records match the reported token counts, but use explicitly
# synthetic unit-test prices. Large catalogues must not leak into the usage view.
FIXTURE=r'''() => {
 events=[];
 const add=(id,age,model,input,output,rate)=>{
  const usage={input_tokens:input,output_tokens:output,cache_read_tokens:0,cache_write_tokens:0,reasoning_tokens:0,prompt_tokens:input,total_tokens:input+output,usage_source:'SYNTHETIC TEST',request_count:1};
  const components={input_tokens:input*Number(rate.input_tokens)/1e6,output_tokens:output*Number(rate.output_tokens)/1e6,cache_read_tokens:0,cache_write_tokens:0};
  const total=Object.values(components).reduce((a,b)=>a+b,0);
  events.push({id,provider:'openai-codex',model,response_model:model,service_tier:'standard',started:now-age,ended:now-age+1,session_id:'luna-test-session',task:id==='title'?'title_generation':'main',agent_kind:'primary',project_id:'luna-project',project_label:'Projects',project_source:'working_directory',status:'completed',usage,cost:{components,total_usd:total,known_components_usd:total,complete:true,rate,cache_read_savings_usd:0,cache_write_premium_usd:0,cache_savings_usd:0}});
 };
 const luna={provider:'openai-codex',model:'gpt-5.6-luna',service_tier:'standard',input_tokens:'.4',output_tokens:'2',cache_read_tokens:'.04',cache_write_tokens:'.5',source:'SYNTHETIC TEST RATES'};
 add('main',60,'gpt-5.6-luna',17224,50,luna);add('title',55,'gpt-5.6-luna',235,11,luna);
 add('old',3*86400,'old-model',1000,50,{...luna,model:'old-model'});
 for(let i=0;i<205;i++)add('long-'+i,10*86400+i,'long-window-model',100,10,{...luna,model:'long-window-model'});
 for(let i=0;i<80;i++)demoCatalogs[0].rates.push({...luna,model:'unrelated-catalogue-'+i});
 queryClient.invalidateQueries();
}'''

def run():
 ART.mkdir(exist_ok=True)
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=browser.new_page(viewport={'width':1700,'height':1050},locale='en-US',color_scheme='light')
  page.set_default_timeout(8000)
  errors,network=[],[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.on('request',lambda r:network.append(r.url))
  page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
  page.evaluate(FIXTURE)
  main=page.get_by_role('navigation',name='Providers',exact=True)
  main.get_by_role('tab',name='Codex',exact=True).click()
  sub=page.get_by_role('navigation',name='Provider subpages',exact=True)
  sub.get_by_role('tab',name='Cache & costs',exact=True).click()
  filters=page.get_by_test_id('cache-window-filters');cache=page.get_by_test_id('cache-costs')
  rates=page.get_by_test_id('published-rates');costs=page.get_by_test_id('component-cost-cards')
  local=filters.get_by_role('group',name='Cache time window',exact=True)
  global_group=page.get_by_role('group',name='Time window',exact=True)
  expect(local.get_by_role('button',name='Past 24h',exact=True)).to_have_attribute('aria-pressed','true')
  expect(rates.locator('tbody tr')).to_have_count(1)
  expect(rates.locator('tbody')).to_contain_text('gpt-5.6-luna')
  assert 'unrelated-catalogue' not in cache.inner_text() and 'old-model' not in cache.inner_text()
  expect(rates.locator('tfoot')).to_contain_text('17,520')
  expect(costs.get_by_test_id('cost-card-total')).to_contain_text('17,520')
  expect(costs.get_by_test_id('cost-card-total')).to_contain_text('$0.007106')
  expect(costs.get_by_test_id('cost-card-input_tokens')).to_contain_text('17,459')
  expect(costs.get_by_test_id('cost-card-output_tokens')).to_contain_text('61')
  # Prices have units; totals never sum price-per-million columns.
  footer_cells=rates.locator('tfoot td').all_text_contents()
  assert footer_cells[4:8]==['','','',''],footer_cells
  boxes=[x.bounding_box() for x in (filters,costs,page.get_by_test_id('cache-savings-summary'),rates,page.get_by_test_id('price-refresh-controls'))]
  assert all(a['y']+a['height']<=b['y']+.5 for a,b in zip(boxes,boxes[1:]))
  page.get_by_test_id('provider-subpage').screenshot(path=str(ART/'cache-luna-selected-1700.png'))
  print('PASS Luna token/cost fixture, 80 unrelated catalogue entries hidden, visible unit labels, total footers, filters above existing cards')
  for label,count_rows,total in [('7 days',2,'18,570'),('30 days',3,'41,120'),('90 days',3,'41,120')]:
   local.get_by_role('button',name=label,exact=True).click()
   expect(rates.locator('tbody tr')).to_have_count(count_rows)
   expect(rates.locator('tfoot')).to_contain_text(total)
   expect(costs.get_by_test_id('cost-card-total')).to_contain_text(total)
   expect(global_group.get_by_role('button',name=label,exact=True)).to_have_attribute('aria-pressed','true')
   expect(local.get_by_role('button',name=label,exact=True)).to_have_attribute('aria-pressed','true')
  # 208 requests; rate rollups must not be truncated to first 200 requests.
  expect(rates.locator('tfoot')).to_contain_text('208')
  global_group.get_by_role('button',name='Past 24h',exact=True).click()
  expect(local.get_by_role('button',name='Past 24h',exact=True)).to_have_attribute('aria-pressed','true')
  expect(rates.locator('tbody tr')).to_have_count(1)
  print('PASS 24h/7d/30d/90d bidirectional filter sync, full 208-request totals, no pagination truncation')
  # Custom inputs are synchronized and can restore the window from this section.
  local.get_by_role('button',name='Custom',exact=True).click()
  a=page.evaluate('new Date((now-3600)*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,19)')
  b=page.evaluate('new Date(now*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,19)')
  # Chromium canonicalises zero seconds away; Playwright fill checks exact value.
  a=a[:-3] if a.endswith(':00') else a
  b=b[:-3] if b.endswith(':00') else b
  filters.get_by_label('Cache window start',exact=True).fill(a)
  filters.get_by_label('Cache window end',exact=True).fill(b)
  expect(page.get_by_label('Window start',exact=True)).to_have_value(a)
  expect(page.get_by_label('Window end',exact=True)).to_have_value(b)
  expect(rates.locator('tbody tr')).to_have_count(1)
  local.get_by_role('button',name='Past 24h',exact=True).click()
  # Scope from the same dashboard is retained, including empty results.
  page.get_by_role('textbox',name='Session ID',exact=True).fill('no-matching-session')
  expect(rates.locator('tbody tr')).to_have_count(0)
  expect(rates.locator('tfoot')).to_contain_text('0')
  expect(cache.get_by_text('No recorded requests in this window.',exact=True)).to_be_visible()
  local.get_by_role('button',name='7 days',exact=True).click()
  expect(page.get_by_role('textbox',name='Session ID',exact=True)).to_have_value('no-matching-session')
  expect(rates.locator('tbody tr')).to_have_count(0)
  page.get_by_role('button',name='Clear filters',exact=True).click()
  local.get_by_role('button',name='Past 24h',exact=True).click()
  before=rates.inner_text();page.get_by_role('button',name='Refresh provider prices',exact=True).click()
  expect(rates).to_have_text(before,use_inner_text=True)
  assert any('/ledger/pricing/refresh?' in s for s in page.evaluate('demoCalls'))
  # Incoming request updates appear under the unchanged window.
  page.evaluate('addDemoEvent()')
  expect(rates.locator('tfoot')).to_contain_text('34,794')
  print('PASS custom dates, empty scope, period changes preserve scope, refresh preserves saved rates, new events update totals')
  for width in (390,320):
   page.set_viewport_size({'width':width,'height':950})
   selector=filters.get_by_role('combobox',name='Cache time window',exact=True)
   expect(selector).to_be_visible()
   selector.select_option('30d')
   expect(page.get_by_role('combobox',name='Time window',exact=True)).to_have_value('30d')
   expect(rates.locator('tbody tr')).to_have_count(3)
   selector.select_option('24h')
   expect(rates.locator('tbody tr')).to_have_count(1)
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
   assert page.evaluate('getComputedStyle(document.body).backgroundColor')=='rgb(17, 17, 30)'
   if width==390:page.get_by_test_id('provider-subpage').screenshot(path=str(ART/'cache-luna-selected-390.png'))
  assert not errors,errors
  assert not network,network
  print('PASS mobile filters/active values at 390/320px, dark theme, no JavaScript errors or network calls')
  browser.close()

if __name__=='__main__':run()
