"""Test.11: heading removed, controls right-aligned, original single gutter retained.

The native Tailwind p-4 rule is simulated for the gutter assertion; this is an
isolated browser test, not a claim of running inside authenticated Hermes.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'


def run():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1700,'height':1100},color_scheme='light')
        errors=[];network=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:network.append(r.url))
        page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
        page.set_default_timeout(5000)
        expect(page.get_by_test_id('quota-home')).to_be_visible()
        # Match the original native page wrapper's padding, absent from the preview shim.
        page.add_style_tag(content='.p-4{padding:16px !important;}')
        native_left=page.locator('.au-original-header').bounding_box()['x']
        page.get_by_role('navigation',name='Providers',exact=True).get_by_role('tab',name='All providers',exact=True).click()
        sub=page.get_by_role('navigation',name='Provider subpages',exact=True)
        group=page.get_by_role('group',name='Breakdown grouping',exact=True)
        for width in (1700,390,320):
            page.set_viewport_size({'width':width,'height':1100})
            sub.get_by_role('tab',name='Overview',exact=True).click()
            section=page.locator('.au-breakdown')
            expect(section).to_be_visible()
            assert section.locator('h1,h2,h3,h4,h5,h6').count()==0
            assert page.get_by_text('Breakdown',exact=True).count()==0
            a,b=section.bounding_box(),group.bounding_box()
            assert abs((a['x']+a['width'])-(b['x']+b['width']))<=1,(a,b)
            wrap=page.locator('.au-ledger')
            assert wrap.evaluate("el=>el.classList.contains('p-4')")
            assert wrap.locator('.p-4').count()==0,'No second padded page wrapper'
            assert wrap.evaluate("el=>getComputedStyle(el).paddingLeft")=='16px'
            lefts=[page.locator(s).bounding_box()['x'] for s in ('.au-original-header','[data-testid="main-navigation"]','[data-testid="provider-page"]','.au-breakdown')]
            assert max(lefts)-min(lefts)<=1,lefts
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            for label in ('Model','Hour','Project','Session','Subagents'):
                group.get_by_role('button',name=label,exact=True).click()
                expect(group.get_by_role('button',name=label,exact=True)).to_have_attribute('aria-pressed','true')
                expect(section.locator('table')).to_be_visible()
            group.get_by_role('button',name='Model',exact=True).click()
            if width in (1700,390):
                section.screenshot(path=str(ART/f'overview-clean-{width}.png'))
            print(f'PASS {width}px: no Breakdown heading; grouping right-aligned; controls functional; single original page gutter')
        page.set_viewport_size({'width':1700,'height':1100})
        page.get_by_role('tab',name='Subscriptions',exact=True).click()
        expect(page.get_by_test_id('quota-home')).to_be_visible()
        assert abs(page.locator('.au-original-header').bounding_box()['x']-native_left)<=1
        assert page.evaluate('getComputedStyle(document.body).backgroundColor')=='rgb(17, 17, 30)'
        assert not errors,errors
        assert not network,network
        print('PASS original quota alignment and dark background; no script errors or external requests')
        browser.close()

if __name__=='__main__':run()
