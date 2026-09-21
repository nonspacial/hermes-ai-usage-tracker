"""Offline integration tests of the packaged Desktop component. No account calls."""
import os, re
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2];ART=Path(__file__).parent/'artifacts';ART.mkdir(exist_ok=True)

def input_card(page):
 return page.get_by_test_id('recorded-summary').locator('.au-metric').filter(has=page.locator('.au-muted',has_text='Uncached input'))

def order(page,mobile=False):
 primary=(page.get_by_label('Main page',exact=True) if mobile else page.get_by_role('navigation',name='Providers',exact=True)).bounding_box()
 filters=page.get_by_role('combobox',name='Agent scope',exact=True).bounding_box()
 summary=page.get_by_test_id('recorded-summary').bounding_box()
 nested=page.get_by_role('navigation',name='Provider subpages',exact=True).bounding_box()
 assert primary['y']+primary['height']<=filters['y'] and filters['y']+filters['height']<=summary['y']
 upper=page.locator('.au-upper').bounding_box()
 assert upper['y']+upper['height']<=nested['y'], 'Subpages belong below the bounded shared summary region'
 assert page.get_by_test_id('provider-page').get_by_role('navigation',name='Provider subpages',exact=True).count()==1
 assert page.get_by_test_id('main-navigation').get_by_role('tab',name='Overview',exact=True).count()==0
 totals=page.get_by_test_id('usage-totals').bounding_box();hero=page.get_by_test_id('usage-hero')
 if hero.count():assert totals['y']+totals['height']<=hero.bounding_box()['y']
 cache=page.get_by_test_id('cache-costs')
 if cache.count():
  savings=page.get_by_test_id('cache-savings-summary').bounding_box()
  rates=page.get_by_test_id('published-rates').bounding_box();costs=page.get_by_test_id('component-cost-cards').bounding_box()
  assert costs['y']+costs['height']<=savings['y'] and savings['y']+savings['height']<=rates['y']
  assert cache.locator('details').count()==0
  assert 'Prices refresh automatically.' not in cache.inner_text()

def assert_selected(group, name, attribute='aria-pressed'):
 buttons=group.locator('button')
 selected=group.locator('button['+attribute+'="true"]')
 assert selected.count()==1, (name, group.inner_text())
 assert selected.inner_text()==name, (name, selected.inner_text())
 def style(locator):
  return locator.evaluate("e=>{let s=getComputedStyle(e);return {bg:s.backgroundColor,color:s.color,border:s.borderColor,shadow:s.boxShadow,weight:s.fontWeight}}")
 on=style(selected)
 assert on['color']==on['border'], on
 assert on['color']=='rgb(167, 153, 239)', on
 assert 'inset' in on['shadow'], on
 assert on['bg'] not in ('transparent','rgba(0, 0, 0, 0)'), on
 assert int(on['weight'])>=600, on
 for i in range(buttons.count()):
  button=buttons.nth(i)
  if button.get_attribute(attribute)=='true':continue
  off=style(button)
  assert on['bg']!=off['bg'] and on['color']!=off['color'], (on,off)
 # Hover must not clear the selected fill and border.
 selected.hover()
 assert style(selected)==on
 selected.focus()
 selected.press('Tab')
 assert style(selected)==on, 'Selection should persist after focus leaves'
 selected.evaluate('() => document.activeElement?.blur()')

def run():
 # Artifacts are shared with the other independent UI suites; do not delete them here.
 with sync_playwright() as p:
  b=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=b.new_page(viewport={'width':1500,'height':1150},color_scheme='light',locale='en-US');page.set_default_timeout(4500)
  errors=[];network=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:network.append(r.url))
  page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded');page.wait_for_timeout(300)
  assert '2.0.0-test.17' in page.locator('body').inner_text()
  main=page.get_by_role('navigation',name='Providers',exact=True)
  subnav=page.get_by_role('navigation',name='Provider subpages',exact=True)
  assert_selected(main,'Subscriptions','aria-selected')
  assert main.get_by_role('tab').all_text_contents()==['Subscriptions','All providers','Codex','Nous Portal','Ollama Cloud','OpenRouter']
  assert subnav.count()==0
  assert page.get_by_test_id('main-navigation').get_by_role('tab',name='Overview',exact=True).count()==0
  home=page.get_by_test_id('quota-home');assert home.is_visible()
  assert home.locator('details').count()==0 and page.get_by_test_id('recorded-summary').count()==0
  assert page.get_by_role('combobox',name='Agent scope').count()==0
  assert '2 reporting live quota' in home.inner_text()
  for text in ['Subscription credits: $0.10','Top-up credits: $0.00','Total usable: $0.10','Status: access depleted','No public subscription-quota API','No credentials for this provider']:
   assert text in home.inner_text(),text
  assert page.evaluate("demoCalls.every(p=>p.startsWith('/usage')||p.startsWith('/ledger/status')||p==='/ledger/profiles')")
  assert page.get_by_test_id('connection-status').inner_text()=='Online'
  page.screenshot(path=str(ART/'original-quota-home.png'),full_page=True)
  # Original hide/unhide, profile and status-chip controls remain functional.
  page.get_by_role('button',name='Hide Nous Portal',exact=True).click();page.wait_for_timeout(50)
  assert home.get_by_text('Nous Portal',exact=True).count()==0
  page.get_by_role('button',name=re.compile('Hidden 1')).click();page.wait_for_timeout(50)
  page.get_by_role('button',name='Unhide Nous Portal',exact=True).click();page.wait_for_timeout(50)
  page.get_by_role('combobox',name='Status bar chip provider').select_option('openai-codex')
  page.get_by_role('combobox',name='Hermes profile').select_option('profile:default');page.wait_for_timeout(50)
  page.get_by_role('combobox',name='Hermes profile').select_option('profile:infra');page.wait_for_timeout(50)
  print('PASS default original quota page, full details, hide/unhide, status pin, profiles; no ledger dependency')
  page.get_by_role('tab',name='All providers',exact=True).click();page.wait_for_timeout(150)
  assert_selected(subnav,'Overview','aria-selected')
  for name in ['All providers','Codex','Nous Portal','Ollama Cloud','OpenRouter']:
   page.get_by_role('tab',name=name,exact=True).click();page.wait_for_timeout(70);order(page)
   assert_selected(page.get_by_role('navigation',name='Providers'),name,'aria-selected')
  page.get_by_role('tab',name='All providers',exact=True).click();page.wait_for_timeout(70)
  assert 'Accounting details & coverage' not in page.locator('body').inner_text()
  assert 'Processed tokens = uncached' not in page.locator('body').inner_text()
  assert page.get_by_test_id('subagent-summary').locator('.au-number').inner_text() not in ('0','—')
  page.screenshot(path=str(ART/'overview-subagents.png'),full_page=True)
  page.get_by_test_id('subagent-summary').click();page.wait_for_timeout(80)
  assert page.get_by_role('combobox',name='Agent scope').input_value()=='subagent'
  assert '100.0% of known tokens' in page.get_by_test_id('subagent-summary').inner_text()
  assert page.get_by_test_id('subagent-summary').get_attribute('aria-pressed')=='true'
  page.get_by_role('tab',name='Requests',exact=True).click();page.wait_for_timeout(80)
  assert page.get_by_role('columnheader',name='Agent',exact=True).count()==1
  assert page.get_by_role('columnheader',name='Project',exact=True).count()==1
  assert 'Primary' not in page.locator('tbody').inner_text()
  page.get_by_role('button',name='Clear filters',exact=True).click();page.wait_for_timeout(80)
  page.get_by_role('tab',name='Overview',exact=True).click();page.wait_for_timeout(80)
  for name,header in [('Project','Project'),('Session','Session'),('Subagents','Subagents')]:
   page.get_by_role('group',name='Breakdown grouping').get_by_role('button',name=name,exact=True).click();page.wait_for_timeout(80)
   assert_selected(page.get_by_role('group',name='Breakdown grouping'),name)
   assert page.get_by_role('columnheader',name='Subagent tokens',exact=True).count()==1
   page.screenshot(path=str(ART/('breakdown-'+name.lower()+'.png')),full_page=True)
  page.locator('.au-breakdown tbody button').first.click();page.wait_for_timeout(80)
  assert_selected(subnav,'Requests','aria-selected')
  assert_selected(main,'All providers','aria-selected')
  assert page.get_by_role('button',name=re.compile('Subagent .* ×')).count()==1
  assert 'Primary' not in page.locator('tbody').inner_text()
  with page.expect_download() as d:page.get_by_role('button',name='Export request CSV',exact=True).click()
  csv=Path(d.value.path()).read_text();assert 'parent_session_id' in csv and 'agent_kind' in csv and 'project_id' in csv
  page.get_by_role('button',name='Clear filters',exact=True).click();page.wait_for_timeout(70)
  page.get_by_role('combobox',name='Project',exact=True).select_option('repo-hermes');page.wait_for_timeout(70)
  assert 'marine-controls' not in page.locator('tbody').inner_text()
  page.get_by_role('button',name='Clear filters',exact=True).click()
  page.get_by_role('textbox',name='Session ID',exact=True).fill('demo-session-0');page.wait_for_timeout(70)
  familyCount=page.locator('tbody tr').count();assert familyCount>1
  page.get_by_role('combobox',name='Session scope').select_option('exact');page.wait_for_timeout(70)
  assert page.locator('tbody tr').count()<familyCount
  page.get_by_role('button',name='Clear filters',exact=True).click();page.wait_for_timeout(70)
  print('PASS subagent card/filter, model/project/session/subagent tables, parent-family vs exact, project drill, CSV attribution')
  page.get_by_role('tab',name='Overview',exact=True).click()
  for name,unit in [('Past 24h','Hourly'),('7 days','Daily'),('30 days','Daily'),('90 days','Daily')]:
   page.get_by_role('button',name=name,exact=True).click();page.wait_for_timeout(70)
   assert_selected(page.get_by_role('group',name='Time window',exact=True),name)
   assert unit+' processed tokens' in page.get_by_test_id('usage-chart').inner_text()
  page.get_by_role('button',name='Cost',exact=True).click();page.wait_for_timeout(70)
  assert_selected(page.get_by_role('group',name='Usage display',exact=True),'Cost')
  assert 'Daily cost' in page.get_by_test_id('usage-chart').inner_text()
  page.get_by_test_id('usage-chart').focus();page.get_by_test_id('usage-chart').press('ArrowRight')
  assert page.get_by_role('status').count()>=1
  page.get_by_role('button',name='Day',exact=True).click();page.wait_for_timeout(70)
  assert_selected(page.get_by_role('group',name='Breakdown grouping'),'Day')
  for view in ['Cache & costs','Compressions','Models & tasks','Requests','Overview']:
   page.get_by_role('tab',name=view,exact=True).click();page.wait_for_timeout(70);order(page)
   assert_selected(subnav,view,'aria-selected')
   assert_selected(main,'All providers','aria-selected')
   if view=='Cache & costs':
    assert page.get_by_role('columnheader',name='Provider source',exact=True).count()==0
    page.get_by_role('button',name='Refresh provider prices',exact=True).click();page.wait_for_timeout(70)
    page.screenshot(path=str(ART/'cache-summary-first.png'),full_page=True)
   if view=='Compressions':assert page.get_by_role('columnheader',name='Next call input (reported)').count()==1
  print('PASS modes, periods, chart tooltip, all page active states, summary-first, pricing refresh, compressions')
  main.get_by_role('tab',name='Codex',exact=True).click();page.wait_for_timeout(80)
  for view in ['Overview','Requests','Cache & costs','Compressions','Models & tasks']:
   subnav.get_by_role('tab',name=view,exact=True).click();page.wait_for_timeout(80)
   assert_selected(main,'Codex','aria-selected');assert_selected(subnav,view,'aria-selected');order(page)
   assert page.get_by_test_id('recorded-summary').get_attribute('aria-label')=='Codex usage totals'
   assert 'Recorded usage' not in page.get_by_test_id('recorded-summary').inner_text()
  subnav.get_by_role('tab',name='Requests',exact=True).click();page.wait_for_timeout(80)
  for name,provider in [('Nous Portal','nous'),('Ollama Cloud','ollama'),('OpenRouter','openrouter'),('Codex','openai-codex')]:
   main.get_by_role('tab',name=name,exact=True).click();page.wait_for_timeout(80)
   assert_selected(main,name,'aria-selected');assert_selected(subnav,'Requests','aria-selected')
   assert page.get_by_test_id('provider-page').get_attribute('data-provider')==provider
   assert page.get_by_role('group',name='Time window').get_by_role('button',name='90 days',exact=True).get_attribute('aria-pressed')=='true'
  main.get_by_role('tab',name='Codex',exact=True).focus()
  main.get_by_role('tab',name='Codex',exact=True).press('ArrowRight');page.wait_for_timeout(60)
  assert_selected(main,'Nous Portal','aria-selected');assert_selected(subnav,'Requests','aria-selected')
  subnav.get_by_role('tab',name='Requests',exact=True).focus()
  subnav.get_by_role('tab',name='Requests',exact=True).press('ArrowRight');page.wait_for_timeout(60)
  assert_selected(main,'Nous Portal','aria-selected');assert_selected(subnav,'Cache & costs','aria-selected')
  main.get_by_role('tab',name='All providers',exact=True).click()
  subnav.get_by_role('tab',name='Overview',exact=True).click();page.wait_for_timeout(80)
  page.screenshot(path=str(ART/'navigation-hierarchy.png'),full_page=True)
  print('PASS independent provider/main navigation, all nested pages, provider switches retain subpage/timeframe, keyboard selection')
  # The original quota page must remain usable when the recorder endpoint fails.
  page.evaluate('failLedger=true');page.get_by_role('tab',name='Subscriptions',exact=True).click();page.wait_for_timeout(80)
  assert page.get_by_test_id('quota-home').is_visible() and 'Ledger unavailable' not in page.locator('body').inner_text()
  page.get_by_role('button',name=re.compile('Refresh$')).click();page.wait_for_timeout(80)
  assert page.get_by_test_id('quota-home').get_by_text('Nous Portal',exact=True).is_visible()
  page.evaluate('failLedger=false');page.get_by_role('tab',name='All providers',exact=True).click();page.wait_for_timeout(80)
  page.get_by_role('button',name='Tokens',exact=True).click();page.get_by_role('button',name='Past 24h',exact=True).click()
  # Unknown and zero stay distinguishable.
  page.evaluate("""() => {events.push({id:'unknown',started:Date.now()/1000-1,provider:'openai-codex',session_id:'unknown',usage:{prompt_tokens:1200,cache_read_tokens:100,total_tokens:1220}});events.push({id:'zero',started:Date.now()/1000-1,provider:'openai-codex',session_id:'zero',usage:{input_tokens:0,prompt_tokens:1200,cache_read_tokens:1000,cache_write_tokens:200,output_tokens:20,total_tokens:1220}})}""")
  for sid,expected in [('unknown','—'),('zero','0'),('empty','0')]:
   page.get_by_role('textbox',name='Session ID').fill(sid);page.wait_for_timeout(90)
   assert input_card(page).locator('.au-number').inner_text()==expected
  page.get_by_role('button',name='Clear filters',exact=True).click();page.wait_for_timeout(50)
  page.get_by_role('button',name='Start test marker',exact=True).click();page.wait_for_timeout(80)
  page.get_by_role('button',name='Add demo request',exact=True).click();page.wait_for_timeout(80)
  page.get_by_role('button',name='End test marker',exact=True).click();page.wait_for_timeout(80)
  assert_selected(page.get_by_role('group',name='Time window',exact=True),'Custom')
  page.get_by_role('button',name='Past 24h',exact=True).click();page.wait_for_timeout(80)
  for scheme in ['light','dark']:
   page.emulate_media(color_scheme=scheme)
   assert page.evaluate('getComputedStyle(document.body).backgroundColor')=='rgb(17, 17, 30)'
  page.set_viewport_size({'width':480,'height':1050});page.wait_for_timeout(100)
  order(page,True);assert page.get_by_label('Main page',exact=True).is_visible()
  page.get_by_role('combobox',name='Agent scope').select_option('subagent')
  page.get_by_role('combobox',name='Usage display',exact=True).select_option('Tokens')
  page.get_by_role('combobox',name='Time window',exact=True).select_option('30d');page.wait_for_timeout(100)
  page.screenshot(path=str(ART/'mobile-overview.png'),full_page=True)
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
  page.get_by_label('Main page',exact=True).select_option('__quota_home__');page.wait_for_timeout(100)
  assert page.get_by_test_id('recorded-summary').count()==0
  assert page.get_by_test_id('quota-home').get_by_text('Nous Portal',exact=True).is_visible()
  page.screenshot(path=str(ART/'mobile-quota-home.png'),full_page=True)
  assert subnav.count()==0
  assert page.get_by_label('Main page',exact=True).input_value()=='__quota_home__'
  page.get_by_label('Main page',exact=True).select_option('openai-codex');page.wait_for_timeout(80)
  assert page.get_by_test_id('provider-page').get_attribute('data-provider')=='openai-codex'
  subnav.get_by_role('tab',name='Cache & costs',exact=True).click();page.wait_for_timeout(80)
  assert page.get_by_label('Main page',exact=True).input_value()=='openai-codex'
  order(page,True)
  page.screenshot(path=str(ART/'mobile-codex-cache.png'),full_page=True)
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
  assert not errors,errors;assert not network,network
  print('PASS quota independent of broken ledger, unknown vs zero, test markers, mobile, dark theme, no JS errors or external requests')
  assert page.get_by_role('tab',name='Recorder',exact=True).count()==0
  assert 'Recorded usage ·' not in page.locator('body').inner_text()
  assert 'Hover or focus and use arrow keys' not in page.locator('body').inner_text()
  assert page.get_by_test_id('recorded-summary').get_by_role('heading',name='Totals',exact=True).count()==0
  assert page.get_by_test_id('connection-status').is_visible()
  b.close()
if __name__=='__main__':run()
