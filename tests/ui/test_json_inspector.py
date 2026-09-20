"""Clipboard / selectable, stable details regressions. Offline synthetic data only.
Native clipboard bridge is mocked; actual OS clipboard and Hermes app not tested.
"""
import json, os, re
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'

def run():
 ART.mkdir(exist_ok=True)
 with sync_playwright() as pw:
  browser=pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=browser.new_page(viewport={'width':1700,'height':1100},locale='en-US',color_scheme='light')
  page.set_default_timeout(8000);errors=[];network=[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.on('request',lambda r:network.append(r.url))
  page.clock.install()
  page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
  # The installed Desktop host can disable selection at an ancestor.
  page.add_style_tag(content='body{user-select:none;-webkit-user-select:none}')
  main=page.get_by_role('navigation',name='Providers',exact=True)
  main.get_by_role('tab',name='Codex',exact=True).click()
  sub=page.get_by_role('navigation',name='Provider subpages',exact=True)
  sub.get_by_role('tab',name='Requests',exact=True).click()
  details=page.get_by_test_id('usage-details').nth(1)
  rid=details.get_attribute('data-record-id')
  details=page.locator('.au-json-details').filter(has=page.locator('summary')).nth(1)
  details.locator('summary').click()
  expect(details).to_have_attribute('open','')
  code=details.locator('.au-json-text');frozen=code.inner_text();original=json.loads(frozen)
  assert original['id']==rid and original.get('cost') and original.get('usage')
  btn=details.get_by_test_id('copy-json');btn.click()
  expect(details.get_by_role('status')).to_have_text('Copied')
  assert json.loads(page.evaluate('window.demoClipboard'))==original
  expect(details).to_have_attribute('open','')
  assert code.evaluate("e=>getComputedStyle(e).userSelect")=='text'
  assert code.locator('code').evaluate("e=>getComputedStyle(e).userSelect")=='text'
  print('PASS full-record copy via mocked native clipboard; icon, success state, explicit text selection under host user-select:none')
  # Freeze the JSON, not the background data or usage recorder. Keep a persistent
  # DOM reference to prove neither rolling query keys nor inserted rows remount it.
  code.evaluate("e=>{window.stablePre=e;e.scrollTop=120;const n=e.querySelector('code').firstChild;const r=document.createRange();r.setStart(n,4);r.setEnd(n,60);getSelection().removeAllRanges();getSelection().addRange(r);window.savedSelection=getSelection().toString();window.savedScroll=e.scrollTop;window.removals=0;window.watch=new MutationObserver(ms=>{for(const m of ms)for(const x of m.removedNodes)if(x===e||(x.nodeType===1&&x.contains(e)))window.removals++});window.watch.observe(document.querySelector('[data-testid=request-list]'),{childList:true,subtree:true});}")
  page.evaluate("""id=>{
   const rec=events.find(e=>e.id===id);rec.test_end_marker='only after close/reopen';
   events.unshift({...rec,id:'new-inserted-before-open',started:Date.now()/1000-1,ended:Date.now()/1000});
   queryClient.invalidateQueries({queryKey:['ai-usage-tracker','ledger']});
  }""",rid)
  page.wait_for_timeout(100)
  current=page.locator(f'.au-json-details[data-record-id="{rid}"]')
  expect(current).to_have_attribute('open','')
  assert current.locator('pre').inner_text()==frozen
  assert page.evaluate('window.stablePre===document.querySelector(\'.au-json-details[data-record-id="'+rid+'"] pre\')')
  assert page.evaluate('getSelection().toString()===window.savedSelection')
  assert page.evaluate('window.stablePre.scrollTop===window.savedScroll')
  # Add response latency, then cross the rolling 60s time boundary and polling.
  page.evaluate("window.originalRest=demoRest;demoRest=async(...a)=>{if(String(a[0]).startsWith('/ledger?'))await new Promise(r=>setTimeout(r,150));return originalRest(...a)};rest=demoRest")
  page.clock.fast_forward(66000);page.clock.run_for(600)
  expect(current).to_have_attribute('open','')
  assert current.locator('pre').inner_text()==frozen
  assert page.evaluate('window.removals')==0
  assert page.evaluate('getSelection().toString()===window.savedSelection')
  assert page.evaluate('window.stablePre.scrollTop===window.savedScroll')
  print('PASS open state, frozen full JSON, selection and scroll survive row insertion, latency, polling and rolling-window refresh without remount')
  # Manual header refresh should retain the same expanded details.
  page.locator('.au-original-header').get_by_role('button',name=re.compile('Refresh$')).click();page.clock.run_for(700)
  expect(current).to_have_attribute('open','');assert current.locator('pre').inner_text()==frozen
  assert page.evaluate('window.removals')==0
  current.locator('summary').click();expect(current).not_to_have_attribute('open','')
  current.locator('summary').click();expect(current).to_have_attribute('open','')
  expect(current.locator('pre')).to_contain_text('only after close/reopen')
  current.get_by_test_id('copy-json').focus();current.get_by_test_id('copy-json').press('Enter')
  assert json.loads(page.evaluate('window.demoClipboard'))['test_end_marker']=='only after close/reopen'
  print('PASS manual refresh and keyboard Copy; close/reopen loads the latest record')

  # A transient read failure must not blank the inspector or reset its scroll.
  stable=current.locator('pre').inner_text()
  page.evaluate('window.failLedger=true;queryClient.invalidateQueries()')
  page.clock.run_for(4500)
  expect(current).to_have_attribute('open','')
  assert current.locator('pre').inner_text()==stable
  page.evaluate('window.failLedger=false;queryClient.invalidateQueries()')
  page.clock.run_for(1500)
  expect(current).to_have_attribute('open','')
  assert current.locator('pre').inner_text()==stable
  print('PASS transient read failure and recovery do not blank the open inspector')

  # Clipboard fallback paths and failure do not falsely claim success.
  page.evaluate("window.demoClipboardFail=true;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{window.browserCopy=t}}})")
  current.get_by_test_id('copy-json').click();expect(current.get_by_role('status')).to_have_text('Copied')
  assert json.loads(page.evaluate('window.browserCopy'))['id']==rid
  page.evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('blocked')}}});document.execCommand=cmd=>{if(cmd!=='copy')return false;window.legacyCopy=document.activeElement.value;return true}")
  current.get_by_test_id('copy-json').click();expect(current.get_by_role('status')).to_have_text('Copied')
  assert json.loads(page.evaluate('window.legacyCopy'))['id']==rid
  page.evaluate("document.execCommand=()=>false")
  current.get_by_test_id('copy-json').click();expect(current.get_by_role('status')).to_have_text('Copy unavailable — select text to copy')
  expect(current).to_have_attribute('open','')
  page.evaluate('window.demoClipboardFail=false')
  current.get_by_test_id('copy-json').click();expect(current.get_by_role('status')).to_have_text('Copied')
  current.locator('.au-json-panel').screenshot(path=str(ART/'json-copy-panel.png'))
  page.get_by_test_id('request-list').screenshot(path=str(ART/'json-request-details-desktop.png'))
  print('PASS browser/legacy clipboard fallbacks, blocked-copy feedback, no accidental details collapse')
  # Returning from another tab preserves opened details and does not mix IDs.
  sub.get_by_role('tab',name='Cache & costs',exact=True).click()
  sub.get_by_role('tab',name='Requests',exact=True).click();expect(current).to_have_attribute('open','')
  main.get_by_role('tab',name='OpenRouter',exact=True).click();page.clock.run_for(700)
  assert page.locator(f'.au-json-details[data-record-id="{rid}"]').count()==0
  assert 'openai-codex' not in page.get_by_test_id('request-list').inner_text()
  main.get_by_role('tab',name='Codex',exact=True).click();page.clock.run_for(700)
  expect(current).to_have_attribute('open','')
  print('PASS tab/provider switching, restored inspector by ID, no stale provider rows shown')
  sub.get_by_role('tab',name='Compressions',exact=True).click()
  comp=page.get_by_test_id('usage-details').first;comp.locator('summary').click();expect(comp).to_have_attribute('open','')
  comp.get_by_test_id('copy-json').click();data=json.loads(page.evaluate('window.demoClipboard'))
  assert data['kind'] in ('compression','micro_compaction')
  # Narrow layout keeps the code scroll inside the table, not the entire page.
  page.set_viewport_size({'width':390,'height':840})
  comp.get_by_test_id('copy-json').click();expect(comp.get_by_role('status')).to_have_text('Copied')
  assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
  comp.locator('.au-json-panel').screenshot(path=str(ART/'json-copy-mobile.png'))
  page.evaluate('window.watch.disconnect()')
  assert not errors,errors
  assert not [x for x in network if x.startswith(('http:','https:'))],network
  print('PASS compression record Copy, mobile sizing, no JavaScript errors or external network requests')
  browser.close()
if __name__=='__main__':run()
