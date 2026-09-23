"""Offline UI connection simulation; not an authenticated Hermes run."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'


def run():
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=browser.new_page(viewport={'width':1500,'height':1050},color_scheme='light');page.set_default_timeout(4000)
  errors=[];requests=[]
  page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append(r.url))
  page.clock.install()
  page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded');page.wait_for_timeout(80)
  badge=page.get_by_test_id('connection-status')
  assert badge.inner_text()=='Online' and badge.get_attribute('data-state')=='online'
  assert page.get_by_test_id('quota-home').is_visible()
  assert page.get_by_role('tab',name='Recorder',exact=True).count()==0
  main=page.get_by_role('navigation',name='Providers',exact=True)
  main.get_by_role('tab',name='Codex',exact=True).click();page.wait_for_timeout(100)
  assert page.get_by_test_id('recorded-summary').get_attribute('aria-label')=='Codex usage totals'
  assert 'Recorded usage' not in page.locator('body').inner_text()
  assert page.get_by_role('heading',name='Totals',exact=True).count()==0
  assert 'Hover or focus and use arrow keys' not in page.locator('body').inner_text()
  page.screenshot(path=str(ART/'clean-codex-overview.png'),full_page=True)
  sub=page.get_by_role('navigation',name='Provider subpages',exact=True)
  sub.get_by_role('tab',name='Cache & costs',exact=True).click();page.wait_for_timeout(60)
  cards=page.get_by_test_id('cache-savings-summary').bounding_box();rates=page.get_by_test_id('published-rates').bounding_box();cost=page.get_by_test_id('component-cost-cards').bounding_box()
  assert cost['y']+cost['height']<=cards['y'] and cards['y']+cards['height']<=rates['y']
  assert page.get_by_test_id('cache-costs').locator('details').count()==0
  assert page.get_by_test_id('published-rates').get_by_role('columnheader',name='Exact model').is_visible()
  page.screenshot(path=str(ART/'prices-under-cards.png'),full_page=True)
  # Clicking the badge replaces this plugin socket; no quota refresh or price fetch.
  before=page.evaluate('({start:demoSocketStarts,stop:demoSocketStops,calls:demoCalls.length})')
  badge.click();page.wait_for_timeout(100)
  after=page.evaluate('({start:demoSocketStarts,stop:demoSocketStops,calls:demoCalls.slice('+str(before['calls'])+')})')
  assert after['start']==before['start']+1 and after['stop']==before['stop']+1
  assert after['calls'] and all(v.startswith(('/ledger?','/ledger/status?','/ledger/change-token?')) for v in after['calls'])
  # Offline/unknown do not remain falsely green because old data is in memory.
  page.evaluate('failStatus=true;failLedger=true');badge.click();page.wait_for_timeout(80)
  assert badge.get_attribute('data-state')=='disconnected'
  assert badge.inner_text() in ('Disconnected','Reconnecting')
  page.screenshot(path=str(ART/'connection-retrying.png'),full_page=True)
  # Bounded backoff retries fail, then normal background health reads recover.
  for ms in [1000,2000,4000]:page.clock.fast_forward(ms);page.wait_for_timeout(30)
  assert badge.get_attribute('data-state')=='disconnected'
  page.evaluate('failStatus=false;failLedger=false')
  page.clock.fast_forward(16000);page.wait_for_timeout(100)
  assert badge.inner_text()=='Online'
  for state,label in [('not_recording','Not recording'),('limited','Limited'),('unverified','Unverified')]:
   page.evaluate('demoStatusState="'+state+'"');badge.click();page.wait_for_timeout(100)
   assert badge.inner_text()==label and badge.get_attribute('data-state')==state
  # Status failure cannot stop the original, independent quota page working.
  page.evaluate('failStatus=true;failLedger=true')
  main.get_by_role('tab',name='Subscriptions',exact=True).click();badge.click();page.wait_for_timeout(80)
  assert page.get_by_test_id('quota-home').get_by_text('Nous Portal',exact=True).is_visible()
  assert page.get_by_test_id('recorded-summary').count()==0
  assert badge.get_attribute('data-state')=='disconnected'
  page.evaluate('failStatus=false;failLedger=false;demoStatusState="online";window.dispatchEvent(new Event("online"))')
  page.clock.fast_forward(16000);page.wait_for_timeout(80)
  assert badge.inner_text()=='Online'
  # A profile switch rechecks the chosen profile rather than caching a previous badge.
  page.get_by_role('combobox',name='Hermes profile').select_option('profile:default');page.wait_for_timeout(80)
  assert page.evaluate('demoCalls.some(p=>p==="/ledger/status?profile=default")')
  page.set_viewport_size({'width':480,'height':1000});page.wait_for_timeout(80)
  assert badge.is_visible() and page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  page.get_by_label('Main page',exact=True).select_option('openai-codex');page.wait_for_timeout(80)
  assert page.get_by_role('tab',name='Recorder',exact=True).count()==0
  page.screenshot(path=str(ART/'mobile-online.png'),full_page=True)
  assert not errors,errors
  assert not requests,requests
  browser.close()
  print('PASS compact status badge on every page; no Recorder tab or redundant titles/hints')
  print('PASS component cards before the preserved provider rates table; no accordion or filler')
  print('PASS manual reconnect disposes/reopens plugin socket and refreshes data only')
  print('PASS automatic REST retry/backoff and periodic recovery; stale data never fakes Online')
  print('PASS explicit missing/limited/legacy states; quota survives failure; online event/profile recovery; dark mobile; no external calls')

if __name__=='__main__':run()
