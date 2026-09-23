"""Offline synthetic Codex cards: no network, credentials or real redemption."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),
                                      headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width':1100,'height':850})
            errors=[];network=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:network.append(r.url))
            page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
            home=page.get_by_test_id('quota-home')
            controls=home.get_by_test_id('codex-resets')
            expect(controls.get_by_role('button',name='Resets: 0')).to_be_visible()
            expect(controls.get_by_role('checkbox',name='Auto use banked Codex reset')).not_to_be_checked()
            assert controls.get_by_role('button',name='Resets: 0').get_attribute('data-tone')=='zero'
            page.evaluate("""() => {window.demoResets.infra.count=null;queryClient.invalidateQueries({queryKey:['ai-usage-tracker','codex-resets','infra']})}""")
            expect(controls.get_by_role('button',name='Resets: unknown')).to_have_attribute('data-tone','zero')
            controls.get_by_role('button',name='Resets: unknown').click()
            expect(controls.get_by_role('button',name='Use one reset')).to_be_disabled()
            assert 'unavailable' in controls.inner_text().lower()
            controls.get_by_role('button',name='Resets: unknown').click()
            page.evaluate("""() => {window.demoResets.infra.count=0;queryClient.invalidateQueries({queryKey:['ai-usage-tracker','codex-resets','infra']})}""")
            expect(controls.get_by_role('button',name='Resets: 0')).to_be_visible()
            controls.get_by_role('button',name='Resets: 0').click()
            expect(controls.get_by_role('button',name='Use one reset')).to_be_disabled()
            assert 'No banked resets' in controls.inner_text()
            controls.get_by_role('button',name='Resets: 0').click()
            page.evaluate("""() => {Object.assign(window.demoResets.infra,{count:2,exhausted:false,redeemable:false});queryClient.invalidateQueries({queryKey:['ai-usage-tracker','codex-resets','infra']})}""")
            expect(controls.get_by_role('button',name='Resets: 2')).to_be_visible()
            assert controls.get_by_role('button',name='Resets: 2').get_attribute('data-tone')=='waiting'
            controls.get_by_role('button',name='Resets: 2').click()
            use=controls.get_by_role('button',name='Use one reset')
            expect(use).to_be_disabled()
            assert 'rounded 0%' in use.get_attribute('title')
            assert use.locator('xpath=..').get_attribute('title') == use.get_attribute('title')
            assert use.evaluate('e=>getComputedStyle(e).color') != controls.get_by_role('button',name='Resets: 2').evaluate('e=>getComputedStyle(e).color')
            controls.get_by_role('button',name='Resets: 2').click()
            page.evaluate("""() => {Object.assign(window.demoResets.infra,{episode:'fixture-episode',exhausted:true,redeemable:true});queryClient.invalidateQueries({queryKey:['ai-usage-tracker','codex-resets','infra']})}""")
            expect(controls.get_by_role('button',name='Resets: 2')).to_have_attribute('data-tone','ready')
            controls.get_by_role('checkbox',name='Auto use banked Codex reset').check()
            expect(controls.get_by_role('checkbox',name='Auto use banked Codex reset')).to_be_checked()
            tooltip=controls.get_by_role('tooltip')
            assert 'weekly' in tooltip.inner_text()
            controls.get_by_role('checkbox',name='Auto use banked Codex reset').hover()
            expect(tooltip).to_be_visible()
            assert tooltip.evaluate('e=>getComputedStyle(e).whiteSpace')=='normal'
            controls.get_by_role('button',name='Resets: 2').click()
            use.click()
            expect(controls.get_by_role('alertdialog',name='Confirm banked reset')).to_be_visible()
            assert page.evaluate("document.activeElement?.textContent") == 'Confirm use', page.evaluate("({active:document.activeElement?.outerHTML,dialog:document.querySelector('[role=alertdialog]')?.outerHTML})")
            page.keyboard.press('Shift+Tab')
            assert page.evaluate("document.activeElement?.textContent") == 'Cancel'
            page.keyboard.press('Tab')
            assert page.evaluate("document.activeElement?.textContent") == 'Confirm use'
            page.keyboard.press('Escape')
            expect(controls.get_by_role('alertdialog',name='Confirm banked reset')).to_have_count(0)
            assert page.evaluate("document.activeElement?.textContent") == 'Use one reset',page.evaluate("document.activeElement?.tagName")
            use.click()
            expect(controls.get_by_role('alertdialog',name='Confirm banked reset')).to_be_visible()
            assert not page.evaluate("window.demoResetCalls.some(x=>x.path==='/codex/resets/redeem')")
            controls.get_by_role('button',name='Cancel').click()
            assert page.evaluate("document.activeElement?.textContent") == 'Use one reset',page.evaluate("document.activeElement?.tagName")
            assert not page.evaluate("window.demoResetCalls.some(x=>x.path==='/codex/resets/redeem')")
            use.click()
            page.evaluate("""() => {window.demoResets.infra.episode='changed-episode';queryClient.invalidateQueries({queryKey:['ai-usage-tracker','codex-resets','infra']})}""")
            expect(controls.get_by_role('alertdialog',name='Confirm banked reset')).to_have_count(0)
            assert not page.evaluate("window.demoResetCalls.some(x=>x.path==='/codex/resets/redeem')")
            use.click();controls.get_by_role('button',name='Confirm use').click()
            expect(controls.get_by_role('button',name='Resets: 1')).to_have_attribute('data-tone','waiting')
            assert page.evaluate("window.demoResetCalls.filter(x=>x.path==='/codex/resets/redeem').length") == 1
            page.get_by_role('tab',name='Codex',exact=True).click()
            top=page.get_by_test_id('provider-limits').get_by_test_id('codex-resets')
            expect(top.get_by_role('button',name='Resets: 1')).to_be_visible()
            expect(top.get_by_role('checkbox',name='Auto use banked Codex reset')).to_be_checked()
            page.get_by_role('tab',name='Nous Portal',exact=True).click()
            expect(page.get_by_test_id('provider-limits').get_by_test_id('codex-resets')).to_have_count(0)
            page.get_by_role('tab',name='Codex',exact=True).click()
            page.get_by_role('combobox',name='Hermes profile').select_option('profile:default')
            expect(page.get_by_test_id('quota-home').get_by_test_id('codex-resets').get_by_role('button',name='Resets: 0')).to_be_visible()
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert not network and not errors,(network,errors)
            print('PASS account/profile-scoped resets in both cards; grey/amber/green, opt-in, confirmation, blocked state, no network')
        finally:
            browser.close()

if __name__ == '__main__':
    run()
