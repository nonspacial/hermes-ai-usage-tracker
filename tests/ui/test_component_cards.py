"""test.17: session-derived writes occupy the main card; provider costs retained."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'
FIXTURE=r'''() => {
 events=[];compressions=[];
 const rate={provider:'openai-codex',model:'test-only-model',service_tier:'standard',input_tokens:'1',output_tokens:'2',cache_read_tokens:'.1',cache_write_tokens:'1.25',source:'SYNTHETIC TEST RATES'};
 function add(id,age,read){
 const input=20000-read,output=20;
 const usage={input_tokens:input,prompt_tokens:20000,output_tokens:output,cache_read_tokens:read,cache_write_tokens:0,total_tokens:20020,reasoning_tokens:0,request_count:1,usage_source:'SYNTHETIC TEST'};
 const components={input_tokens:input/1e6,output_tokens:output*2/1e6,cache_read_tokens:read*.1/1e6,cache_write_tokens:0};const cost=Object.values(components).reduce((a,b)=>a+b,0);
 events.push({id,started:now-age,ended:now-age+1,session_id:'chain',process:'demo',provider:'openai-codex',model:'test-only-model',response_model:'test-only-model',source:'main_hook',status:'completed',api_mode:'codex_responses',service_tier:'standard',usage,cost:{components,total_usd:cost,known_components_usd:cost,complete:true,rate,cache_read_savings_usd:read*.9/1e6,cache_write_premium_usd:0,cache_savings_usd:read*.9/1e6}});
 }
 add('cold',120,0);add('warm',90,12000);add('grown',60,15000);
 events.push({id:'pending',started:now-1,session_id:'other',status:'pending',source:'main_hook',process:'demo',provider:'openai-codex',model:'test-only-model'});
 window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'});
}'''


def run():
 ART.mkdir(exist_ok=True)
 with sync_playwright() as p:
  b=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=b.new_page(viewport={'width':1700,'height':1100},locale='en-GB',color_scheme='light')
  page.set_default_timeout(7000);errors=[];network=[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.on('request',lambda r:network.append(r.url))
  page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
  assert page.evaluate('dollars(12.5)') == '$12.50'
  assert page.evaluate('dollars(-12.5)') == '-$12.50'
  page.evaluate(FIXTURE)
  page.evaluate('''() => {
   const original=rest;
   rest=async(...args)=>{
    const result=await original(...args);
    if(result.summary)result.summary.missing_reasons=Object.fromEntries(Object.entries(result.summary.missing_fields).map(([key,n])=>[key,{awaiting_usage:n}]));
    return result;
   };
  }''')
  page.get_by_role('navigation',name='Providers',exact=True).get_by_role('tab',name='Codex',exact=True).click()
  page.get_by_role('navigation',name='Provider subpages',exact=True).get_by_role('tab',name='Cache & costs',exact=True).click()
  cache=page.get_by_test_id('cache-costs');cards=page.get_by_test_id('component-cost-cards');rates=page.get_by_test_id('published-rates')
  expect(cards.locator('article')).to_have_count(5)
  expect(page.get_by_test_id('cost-card-total').locator('dt').filter(has_text='Requests without this value')).to_have_attribute('title','1 awaiting usage (owner process observed live) · subtotal')
  assert 'records missing' not in page.locator('body').inner_text()
  expect(cache.locator('table')).to_have_count(1)
  expect(page.get_by_test_id('component-costs')).to_have_count(0)
  expected={'total':('60,060','$0.03582','1','1'),'input_tokens':('33,000','$0.0330','1','1'),'output_tokens':('60','$0.00012','1','1'),'cache_read_tokens':('27,000','$0.0027','1','1'),'cache_write_tokens':('15,000','15,000','1','1')}
  for key,(tokens,cost,missing,unpriced) in expected.items():
   card=page.get_by_test_id('cost-card-'+key)
   expect(card.locator('[data-field="tokens"]')).to_have_text(tokens)
   expect(card.get_by_test_id('component-amount')).to_contain_text(cost)
   expect(card.locator('[data-field="missing"]')).to_have_text(missing)
   expect(card.locator('[data-field="unpriced"]')).to_have_text(unpriced)
  expect(page.get_by_test_id('cache-read-growth')).to_have_count(0)
  write_card=page.get_by_test_id('cost-card-cache_write_tokens')
  expect(write_card).to_contain_text('Calculated from session reads')
  expect(write_card.locator('[data-field=reported_cost]')).to_contain_text('$0.0000')
  expect(write_card.locator('[data-field=comparisons]')).to_have_text('2')
  expect(rates.locator('tfoot')).to_contain_text('60,060')
  expect(rates.locator('tfoot')).to_contain_text('$0.03582')
  # Deltas live alongside usage, with reported zero and saved costs untouched.
  response=page.evaluate("demoRest('/ledger?profile=infra&start='+(now-3600)+'&end='+now+'&provider=openai-codex&limit=1')")
  assert response['summary']['known']['cache_write_tokens']==0
  assert response['summary']['session_cache_writes']['tokens']==15000
  assert response['cache_read_progression']['positive_read_growth_tokens']==15000
  assert response['cache_read_progression']['included_in_usage_or_cost'] is False
  for e in page.evaluate('events'):
   if e.get('usage'):assert e['usage']['cache_write_tokens']==0
  for node in page.get_by_test_id('cache-savings-summary').get_by_test_id('savings-coverage').all():
   expect(node).to_have_text('3 / 4 requests · subtotal')
  print('PASS all table columns mapped to 5 cards; exact total and unique missing counts; saved rate table retained; 15k calculated writes promoted to headline while raw counters and costs stay separate')
  # Place all summary cards before the remaining rates table.
  for width in (1700,860,390,320):
   page.set_viewport_size({'width':width,'height':1100})
   a,c,d=[x.bounding_box() for x in (cards,page.get_by_test_id('cache-savings-summary'),rates)]
   assert a['y']+a['height']<=c['y']+1 and c['y']+c['height']<=d['y']+1
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
   page.get_by_test_id('provider-subpage').screenshot(path=str(ART/f'component-cards-{width}.png'))
   for card in cards.locator('article').all():
    assert card.evaluate("e=>getComputedStyle(e).backgroundColor!=='rgba(0, 0, 0, 0)'")
  print('PASS 1700/860/390/320 layout, grey card fills, summary first, no horizontal page overflow')
  # Unknowns stay unknown; empty window is a separate zero state.
  page.set_viewport_size({'width':1700,'height':1100})
  page.evaluate("events=events.filter(r=>r.id==='pending');window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'})")
  for key in expected:
   expect(page.get_by_test_id('cost-card-'+key).get_by_test_id('component-amount')).to_have_text('—' if key=='cache_write_tokens' else '— *')
   expect(page.get_by_test_id('cost-card-'+key).locator('[data-field="tokens"]')).to_have_text('—')
  expect(write_card).to_contain_text('Awaiting read comparison')
  page.evaluate("events=[];window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'})")
  for key in expected:
   expect(page.get_by_test_id('cost-card-'+key).get_by_test_id('component-amount')).to_have_text('0' if key=='cache_write_tokens' else '$0.0000')
   expect(page.get_by_test_id('cost-card-'+key).locator('[data-field="tokens"]')).to_have_text('0')
  assert not errors,errors
  assert not network,network
  print('PASS unknown-vs-zero and empty windows; no JS errors or network calls')
  b.close()

if __name__=='__main__':run()
