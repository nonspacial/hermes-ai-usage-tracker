"""Regression: filtered Requests always has a local route back to its full list.

Uses the packaged Desktop source in the same offline harness as the other UI
suites. No Hermes services, provider calls or credentials are used.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'

def run():
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=browser.new_page(viewport={'width':1500,'height':1100},color_scheme='light',locale='en-US')
  page.set_default_timeout(5000)
  errors=[];network=[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.on('request',lambda r:network.append(r.url))
  page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
  main=page.get_by_role('navigation',name='Providers',exact=True)
  subnav=page.get_by_role('navigation',name='Provider subpages',exact=True)
  nav=page.get_by_test_id('request-navigation')
  back=page.get_by_test_id('request-back')
  show_all=page.get_by_test_id('request-show-all')
  listing=page.get_by_test_id('request-list')
  def rows():return listing.locator('tbody tr')
  def session_link(name):
   link=listing.get_by_role('button',name=name,exact=True,include_hidden=True).first
   disclosure=link.locator('xpath=ancestor::tr').locator('.au-record-disclosure')
   if disclosure.is_visible() and disclosure.get_attribute('aria-expanded')=='false':disclosure.click()
   return link
  def last_query():
   return page.evaluate("() => Object.fromEntries(new URL(demoCalls.filter(p=>p.startsWith('/ledger?')).at(-1),'https://offline.test').searchParams)")
  def settled():page.wait_for_timeout(90)
  def scope_is(provider='openai-codex',period='Past 24h'):
   expect(page.get_by_test_id('provider-page')).to_have_attribute('data-provider',provider)
   expect(page.get_by_role('group',name='Time window',exact=True).get_by_role('button',name=period,exact=True)).to_have_attribute('aria-pressed','true')
  main.get_by_role('tab',name='Codex',exact=True).click()
  subnav.get_by_role('tab',name='Requests',exact=True).click()
  expect(rows()).to_have_count(7)
  assert nav.count()==0
  # Session drill reproduced from the reported single-row case.
  session_link('demo-session-2').click()
  expect(rows()).to_have_count(1)
  expect(back).to_be_visible();expect(show_all).to_be_visible()
  expect(nav.get_by_role('button',name='Remove session filter',exact=True)).to_be_visible()
  assert nav.bounding_box()['y']+nav.bounding_box()['height']<=listing.locator('table').bounding_box()['y']
  expect(page.get_by_role('textbox',name='Session ID',exact=True)).to_have_value('demo-session-2')
  # Inline record details remain available; reset must not leave an expanded
  # details row accidentally attached to a different request after list reorder.
  disclosure=rows().first.locator('.au-record-disclosure')
  if disclosure.is_visible() and disclosure.get_attribute('aria-expanded')=='false':disclosure.click()
  listing.locator('summary').first.click();assert listing.locator('details[open]').count()==1
  page.get_by_test_id('provider-subpage').screenshot(path=str(ART/'request-drill-return.png'))
  show_all.click();expect(rows()).to_have_count(7)
  expect(page.get_by_role('textbox',name='Session ID',exact=True)).to_have_value('')
  assert listing.locator('details[open]').count()==0 and nav.count()==0
  scope_is()
  session_link('demo-session-2').click();expect(rows()).to_have_count(1)
  back.focus();back.press('Enter');expect(rows()).to_have_count(7)
  scope_is()
  print('PASS exact reported session drill: visible Back/Show all beside table, keyboard Back, no stale row details')
  # Nested project -> session -> Back -> Back, restoring the original grouping.
  subnav.get_by_role('tab',name='Overview',exact=True).click()
  page.get_by_role('group',name='Breakdown grouping').get_by_role('button',name='Project',exact=True).click()
  page.locator('.au-breakdown tbody button.au-drill').filter(has_text='hermes-agent').click()
  expect(page.get_by_role('combobox',name='Project',exact=True)).to_have_value('repo-hermes')
  expect(subnav.get_by_role('tab',name='Overview',exact=True)).to_have_attribute('aria-selected','true')
  expect(page.locator('.au-breakdown tbody tr')).to_have_count(1)
  subnav.get_by_role('tab',name='Requests',exact=True).click()
  expect(rows()).to_have_count(5)
  session_link('demo-session-2').click();expect(rows()).to_have_count(1)
  back.click();expect(rows()).to_have_count(5)
  expect(page.get_by_role('combobox',name='Project',exact=True)).to_have_value('repo-hermes')
  subnav.get_by_role('tab',name='Overview',exact=True).click()
  back.click()
  expect(subnav.get_by_role('tab',name='Overview',exact=True)).to_have_attribute('aria-selected','true')
  expect(page.get_by_role('group',name='Breakdown grouping').get_by_role('button',name='Project',exact=True)).to_have_attribute('aria-pressed','true')
  expect(page.get_by_role('combobox',name='Project',exact=True)).to_have_value('')
  # Subagent entry path clears BOTH narrow subagent ID and agent scope.
  page.get_by_role('group',name='Breakdown grouping').get_by_role('button',name='Subagents',exact=True).click()
  page.locator('.au-breakdown tbody button.au-drill').filter(has_text='test-worker').click()
  expect(subnav.get_by_role('tab',name='Overview',exact=True)).to_have_attribute('aria-selected','true')
  subnav.get_by_role('tab',name='Requests',exact=True).click()
  expect(rows()).to_have_count(1)
  show_all.click();expect(rows()).to_have_count(7)
  expect(page.get_by_role('combobox',name='Agent scope',exact=True)).to_have_value('')
  assert not last_query()['subagent']
  print('PASS nested project/session Back stack, restores overview grouping; subagent Show all clears hidden scope')
  # Empty/manual scopes and individually removable local filter chips.
  page.get_by_role('combobox',name='Project',exact=True).select_option('repo-hermes')
  page.get_by_role('textbox',name='Session ID',exact=True).fill('not-a-session')
  expect(rows()).to_have_count(0);expect(show_all).to_be_visible()
  nav.get_by_role('button',name='Remove session filter',exact=True).click()
  expect(rows()).to_have_count(5)
  expect(page.get_by_role('combobox',name='Project',exact=True)).to_have_value('repo-hermes')
  show_all.click();expect(rows()).to_have_count(7)
  # A scoped saved test stays selected, including its exact start/end.
  page.evaluate("() => {savedTests.unshift({id:'navigation-test',label:'Navigation fixture',started:now-86400,ended:now});window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'})}")
  page.get_by_role('combobox',name='Saved tests',exact=True).select_option('navigation-test');settled()
  start=page.get_by_role('textbox',name='Window start',exact=True).input_value()
  end=page.get_by_role('textbox',name='Window end',exact=True).input_value()
  session_link('demo-session-2').click();expect(rows()).to_have_count(1)
  show_all.click();expect(rows()).to_have_count(7)
  scope_is(period='Custom')
  assert page.get_by_role('textbox',name='Window start',exact=True).input_value()==start
  assert page.get_by_role('textbox',name='Window end',exact=True).input_value()==end
  assert last_query()['test_id']=='navigation-test'
  print('PASS manual/empty filters remain escapable; chip removal preserves other filters; saved test boundaries remain unchanged')
  # Populate more than two batches. The scoped list starts with ten and appends ten.
  page.get_by_role('button',name='Past 24h',exact=True).click();settled()
  page.evaluate("""() => {const t=Date.now()/1000-60;for(let i=0;i<430;i++){
   const e=JSON.parse(JSON.stringify(events[0]));e.id='paging-'+i;e.started=t-i;e.ended=e.started+.1;
   e.session_id='demo-session-'+(i%5);Object.assign(e,demoContexts[e.session_id]);events.push(e);
  }window.demoChange++;for(const callback of window.demoSubscribers)callback({type:'changed',mode:'native-events'})}""")
  expect(rows()).to_have_count(10)
  page.get_by_role('button',name='Load more',exact=True).click();settled()
  expect(page.get_by_test_id('record-pagination')).to_contain_text('Showing 20 of 437 records')
  before=rows().first.inner_text()
  session_link('demo-session-2').click();settled()
  assert rows().count()<=10
  # Same narrowed scope is a no-op, not an extra Back history entry.
  session_link('demo-session-2').click();settled()
  back.click();settled()
  expect(page.get_by_test_id('record-pagination')).to_contain_text('Showing 10 of 437 records')
  assert rows().first.inner_text()==before and nav.count()==0
  page.get_by_test_id('provider-subpage').screenshot(path=str(ART/'request-list-restored.png'))
  # Scope stays reversible across background refreshes, but not stale providers.
  session_link('demo-session-2').click();settled()
  page.get_by_test_id('connection-status').click();settled();expect(back).to_be_visible()
  main.get_by_role('tab',name='Nous Portal',exact=True).click();settled()
  assert back.count()==0;expect(nav).to_have_count(0)
  expect(rows()).to_have_count(6)
  scope_is(provider='nous')
  main.get_by_role('tab',name='Codex',exact=True).click();settled()
  assert back.count()==0
  print('PASS paginated Back restores scope at first batch; duplicate drill is a no-op; refresh preserves history; provider switch cannot revive stale history')
  # Manual time changes also keep the selected timeframe rather than restoring
  # an old one through Back. The local Show all remains available.
  session_link('demo-session-2').click();settled()
  page.get_by_role('button',name='7 days',exact=True).click();settled()
  assert back.count()==0;show_all.click();settled();scope_is(period='7 days')
  # Mobile navigation remains local to the table and wraps without page overflow.
  page.get_by_role('button',name='Past 24h',exact=True).click();settled()
  session_link('demo-session-2').click();settled()
  page.set_viewport_size({'width':480,'height':1000});settled()
  expect(back).to_be_visible();expect(show_all).to_be_visible()
  assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  nav.scroll_into_view_if_needed();page.screenshot(path=str(ART/'request-mobile-back.png'))
  show_all.click();settled();expect(rows()).to_have_count(10)
  assert page.get_by_label('Main page',exact=True).input_value()=='openai-codex'
  assert page.get_by_role('combobox',name='Time window',exact=True).input_value()=='24h'
  assert not errors,errors;assert not network,network
  print('PASS chosen time window retained, mobile return controls, no overflow, no browser errors or external requests')
  browser.close()

if __name__=='__main__':run()
