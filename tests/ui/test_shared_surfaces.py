"""Regression: real Hermes accent-fill tokens must not invert card/track surfaces.

The preview used to paint cards with preview-only CSS. This test removes that
stylesheet, injects Desktop-like translucent token values and verifies that the
packaged plugin alone still paints filled cards and quiet segmented tracks.
No account or network is used.
"""
from pathlib import Path
import os
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'
HOST='''
:root{--ui-base:#d4d1e4;--ui-accent:#a799ef;
 --ui-bg-chrome:#11111e;--dt-background:#11111e;
 --ui-bg-primary:color-mix(in srgb,#a799ef 16%,color-mix(in srgb,#d4d1e4 10%,transparent));
 --ui-bg-secondary:color-mix(in srgb,#a799ef 11%,color-mix(in srgb,#d4d1e4 7%,transparent));
 --ui-bg-quaternary:color-mix(in srgb,#a799ef 5%,color-mix(in srgb,#d4d1e4 4%,transparent));
 --ui-text-primary:#d4d1e4;--ui-text-secondary:#b8b3cd;--ui-text-tertiary:#9590aa;
 --ui-stroke-secondary:#343148;}
html,body,#root{color-scheme:dark;background:#11111e;color:#d4d1e4;}
#root{padding:0;max-width:1440px;}.p-4{padding:16px;}
'''

def rgba(locator):
    # Chromium returns color(srgb ...) for color-mix, so normalize through canvas.
    return locator.evaluate('''e=>{const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
      const ctx=canvas.getContext('2d');ctx.fillStyle=getComputedStyle(e).backgroundColor;ctx.fillRect(0,0,1,1);
      return Array.from(ctx.getImageData(0,0,1,1).data)}''')

def run():
    ART.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1700,'height':1200},color_scheme='light',locale='en-US')
        errors,network=[],[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:network.append(r.url))
        text=(ROOT/'preview.html').read_text()
        assert '#root .au-ledger .au-metric {' not in text
        assert '#root .au-ledger th {' not in text
        page.set_content(text,wait_until='domcontentloaded')
        page.evaluate("document.getElementById('preview-original-dark-theme').remove()")
        page.add_style_tag(content=HOST)
        main=page.get_by_role('navigation',name='Providers',exact=True)
        main.get_by_role('tab',name='Codex',exact=True).click()
        cards=page.get_by_test_id('usage-totals').locator('.au-metric')
        subagent=page.get_by_test_id('subagent-summary')
        card=rgba(cards.first);page_bg=page.evaluate('getComputedStyle(document.body).backgroundColor')
        assert card[3]==255 and card[:3]!=[17,17,30],(card,page_bg)
        for item in cards.all():assert rgba(item)==card
        assert rgba(subagent)[3]==0,rgba(subagent)
        for group in page.locator('.au-segment').all():
            if group.is_visible():assert rgba(group)[3]==0,rgba(group)
        for group in page.locator('.au-segment').all():
            if not group.is_visible():continue
            active=group.locator('button[aria-pressed="true"]');other=group.locator('button[aria-pressed="false"]').first
            if active.count():
                assert rgba(active)[3]==255 and rgba(active)!=card
                assert rgba(other)[3]==0
        subagent.click();expect(subagent).to_have_attribute('aria-pressed','true');assert rgba(subagent)!=card
        subagent.click();expect(subagent).to_have_attribute('aria-pressed','false');page.mouse.move(1695,1100);assert rgba(subagent)[3]==0
        # Back and Show all remain text-only and right-aligned after theme repair.
        page.get_by_role('group',name='Breakdown grouping').get_by_role('button',name='Subagents',exact=True).click()
        page.locator('.au-breakdown .au-drill').first.click()
        for control in page.locator('.au-return-button').all():assert rgba(control)[3]==0
        page.get_by_test_id('request-show-all').click()
        page.get_by_role('navigation',name='Provider subpages').get_by_role('tab',name='Overview',exact=True).click()
        page.get_by_test_id('provider-page').screenshot(path=str(ART/'desktop-theme-overview-1700.png'))
        page.get_by_role('navigation',name='Provider subpages').get_by_role('tab',name='Cache & costs',exact=True).click()
        savings=page.get_by_test_id('cache-savings-summary').locator('.au-metric')
        for item in savings.all():assert rgba(item)==card
        table=page.get_by_test_id('published-rates')
        header=rgba(table.locator('th').first);body=rgba(table.locator('.au-table'))
        assert header[3]==body[3]==255 and header!=body
        assert sum(header[:3])>sum(body[:3])>sum([17,17,30])
        page.get_by_test_id('provider-subpage').screenshot(path=str(ART/'desktop-theme-cache-1700.png'))
        print('PASS actual Desktop-like alpha tokens: opaque raised cards/tables, transparent control tracks, active tint, transparent normal subagent with retained selected tint')
        for width in (390,320):
            page.set_viewport_size({'width':width,'height':950})
            expect(page.get_by_test_id('cache-window-filters').get_by_role('combobox',name='Cache time window')).to_be_visible()
            for item in savings.all():assert rgba(item)==card
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if width==390:page.get_by_test_id('provider-subpage').screenshot(path=str(ART/'desktop-theme-cache-390.png'))
        print('PASS host page/gutters retained, theme changes shared by real component and preview, narrow layouts, plain return actions')
        assert not errors,errors
        assert not network,network
        browser.close()

if __name__=='__main__':run()
