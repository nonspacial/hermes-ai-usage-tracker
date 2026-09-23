"""Offline mounted liveness and rolling-window continuity, no real ledger."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]

def run():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,
                                  args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':390,'height':850})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.clock.install()
        page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
        page.get_by_label('Main page',exact=True).select_option('')
        page.get_by_role('tab',name='Requests',exact=True).click()
        expect(page.locator('tbody tr').first).to_be_visible()
        disclosure=page.locator('.au-record-disclosure').first
        if disclosure.is_visible():disclosure.click()
        page.locator('.au-reader').focus()
        page.clock.run_for(1000)
        initial=page.evaluate('''()=>({reads:demoCalls.filter(p=>p.startsWith('/ledger?')).length,
          focus:document.activeElement.className, open:document.querySelector('.au-record-disclosure')?.getAttribute('aria-expanded'),
          rows:document.querySelectorAll('tbody tr').length})''')
        page.clock.run_for(21000)
        assert page.evaluate('demoCalls.filter(p=>p.startsWith("/ledger?")).length')==initial['reads'], 'unchanged token caused full read'
        assert page.get_by_role('button',name='Refresh',exact=True).count()==1
        page.clock.run_for(39000)
        page.wait_for_function('n=>demoCalls.filter(p=>p.startsWith("/ledger?")).length>n',arg=initial['reads'])
        after=page.evaluate('''()=>({reads:demoCalls.filter(p=>p.startsWith('/ledger?')).length,
          focus:document.activeElement.className, open:document.querySelector('.au-record-disclosure')?.getAttribute('aria-expanded'),
          rows:document.querySelectorAll('tbody tr').length})''')
        assert after['reads']==initial['reads']+1,(initial,after)
        assert after['focus']==initial['focus'] and after['open']==initial['open'] and after['rows']==initial['rows'],(initial,after)
        assert not errors,errors
        browser.close()
        print('PASS quiet token checks do not read; visible rolling expiry reads once without Refresh busy or losing focus and disclosure')

if __name__=='__main__':run()
