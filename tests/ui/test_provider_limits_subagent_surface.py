"""New Subscriptions/provider-first quotas and transparent clickable subagent card.
Offline synthetic data only; compare current hover/selected styling with test.14.
"""
from pathlib import Path
from datetime import datetime, timezone
import os, zipfile
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
ART=Path(__file__).parent/'artifacts'


def style(locator):
    return locator.evaluate('''e=>{let s=getComputedStyle(e);return{
      bg:s.backgroundColor,border:s.borderColor,borderWidth:s.borderWidth,
      shadow:s.boxShadow,color:s.color,cursor:s.cursor,outline:s.outline};}''')


def alpha(locator):
    return locator.evaluate('''e=>{const c=document.createElement('canvas');c.width=c.height=1;
    const x=c.getContext('2d');x.fillStyle=getComputedStyle(e).backgroundColor;x.fillRect(0,0,1,1);
    return x.getImageData(0,0,1,1).data[3]}''')


def card_first(page,provider):
    panel=page.get_by_test_id('provider-page');card=panel.get_by_test_id('provider-limits')
    expect(card).to_be_visible();expect(card).to_have_attribute('data-provider',provider)
    assert card.evaluate('e=>e.parentElement.classList.contains("au-upper")')
    a=card.bounding_box();b=panel.locator('.au-view-controls').bounding_box()
    assert a['y']+a['height']<=b['y']
    assert card.locator('details').count()==0
    return card


def run():
    ART.mkdir(exist_ok=True)
    # Bundled text fixture contains ONLY the old UI CSS, not a second recorder.
    baseline_css=(ROOT/'tests/fixtures/test14-subagent-style.css').read_text()
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1700,'height':1120},locale='en-US',color_scheme='light')
        page.set_default_timeout(8000);page.clock.set_fixed_time(datetime(2026,9,20,16,0,tzinfo=timezone.utc))
        errors=[];network=[]
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:network.append(r.url))
        page.set_content((ROOT/'preview.html').read_text(),wait_until='domcontentloaded')
        main=page.get_by_role('navigation',name='Providers',exact=True)
        expect(main.get_by_role('tab',name='Subscriptions',exact=True)).to_have_attribute('aria-selected','true')
        assert main.get_by_role('tab').all_text_contents()==['Subscriptions','All providers','Codex','Nous Portal','Ollama Cloud','OpenRouter']
        expect(page.get_by_test_id('quota-home')).to_be_visible()
        assert page.get_by_test_id('provider-subnavigation').count()==0
        page.screenshot(path=str(ART/'subscriptions-default.png'),full_page=True)
        page.evaluate("""() => {demoProviders.find(p=>p.id==='openai-codex').quota.windows.unshift({label:'Five-hour',remaining_percent:74,reset_at:new Date(Date.now()+7200000).toISOString()});queryClient.invalidateQueries({queryKey:['ai-usage-tracker','usage']});}""")
        expect(page.get_by_test_id('quota-home').get_by_text('74% left',exact=True)).to_be_visible()
        main.get_by_role('tab',name='Codex',exact=True).click()
        card=card_first(page,'openai-codex');before=card.inner_text()
        expect(card.get_by_text('99% left',exact=True)).to_be_visible()
        expect(card.get_by_text('74% left',exact=True)).to_be_visible()
        main.get_by_role('tab',name='Subscriptions',exact=True).click()
        original=page.get_by_test_id('quota-home').locator('div.border').filter(has=page.get_by_text('ChatGPT or Codex Subscription',exact=True))
        assert original.inner_text()==before
        for label,provider in [('Codex','openai-codex'),('Nous Portal','nous'),('Ollama Cloud','ollama'),('OpenRouter','openrouter')]:
            main.get_by_role('tab',name=label,exact=True).click()
            for tab in ['Overview','Requests','Cache & costs','Compressions','Models & tasks']:
                page.get_by_role('navigation',name='Provider subpages',exact=True).get_by_role('tab',name=tab,exact=True).click()
                card=card_first(page,provider)
                assert card.locator('div.border').count()==1
            if provider=='nous':assert 'Total usable: $0.10' in card.inner_text()
            if provider=='ollama':assert 'No public subscription-quota API' in card.inner_text()
        main.get_by_role('tab',name='All providers',exact=True).click()
        for tab in ['Overview','Requests','Cache & costs','Compressions','Models & tasks']:
            page.get_by_role('navigation',name='Provider subpages',exact=True).get_by_role('tab',name=tab,exact=True).click()
            assert page.get_by_test_id('provider-limits').count()==0
        print('PASS Subscriptions default and original quota; all provider cards first on each subpage; no limits on All providers')
        main.get_by_role('tab',name='Codex',exact=True).click()
        page.get_by_role('navigation',name='Provider subpages',exact=True).get_by_role('tab',name='Overview',exact=True).click()
        before=card_first(page,'openai-codex').inner_text()
        page.get_by_role('group',name='Time window',exact=True).get_by_role('button',name='30 days',exact=True).click()
        assert card_first(page,'openai-codex').inner_text()==before
        page.get_by_role('combobox',name='Hermes profile',exact=True).select_option('profile:default')
        expect(page.get_by_test_id('quota-home')).to_be_visible()
        main.get_by_role('tab',name='Codex',exact=True).click()
        expect(page.get_by_test_id('provider-limits')).to_have_attribute('data-profile','default')
        expect(page.get_by_test_id('provider-limits').get_by_text('99% left',exact=True)).to_be_visible()
        page.get_by_role('combobox',name='Hermes profile',exact=True).select_option('profile:infra')
        expect(page.get_by_test_id('provider-limits')).to_have_attribute('data-profile','infra')
        print('PASS time filters do not change live quota; selected profile matches card')

        for width in (1700,860,390):
            page.set_viewport_size({'width':width,'height':1100})
            s=page.get_by_test_id('subagent-summary');page.mouse.move(width-1,0)
            if s.get_attribute('aria-pressed')=='true':s.click();page.mouse.move(width-1,0)
            normal=style(s)
            assert alpha(s)==0,normal
            other=page.get_by_test_id('usage-totals').locator('.au-metric').first
            assert alpha(other)==255
            other_style=style(other)
            s.hover();hover=style(s);assert alpha(s)>0
            s.click();page.mouse.move(width-1,0);selected=style(s);assert alpha(s)>0
            s.hover();selected_hover=style(s)
            # Apply the exact previous normal CSS to verify that only default fill changed.
            old=page.add_style_tag(content=baseline_css)
            assert style(s)==selected_hover
            page.mouse.move(width-1,0);assert style(s)==selected
            s.click();s.hover();assert style(s)==hover
            assert style(other)==other_style
            old.evaluate('e=>e.remove()');page.mouse.move(width-1,0)
            expect(s).to_have_attribute('aria-pressed','false');assert alpha(s)==0
            assert style(s)['border']==normal['border']
            assert page.get_by_role('combobox',name='Agent scope',exact=True).input_value()==''
            s.focus();assert style(s)['outline']!='none';s.press('Enter')
            expect(s).to_have_attribute('aria-pressed','true')
            assert page.get_by_role('combobox',name='Agent scope',exact=True).input_value()=='subagent'
            s.press('Enter');expect(s).to_have_attribute('aria-pressed','false')
            page.mouse.move(width-1,0);page.evaluate('document.activeElement?.blur()')
            assert alpha(s)==0
            page.get_by_test_id('usage-totals').screenshot(path=str(ART/f'subagent-transparent-{width}.png'))
            if width==1700:page.screenshot(path=str(ART/'provider-quota-and-transparent-subagent.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            print(f'PASS {width}px: transparent idle subagent; exact prior hover/selected styling; filled sibling cards; keyboard filter')
        # Mobile main navigation retains both the rename and the exclusions.
        page.get_by_role('combobox',name='Main page',exact=True).select_option('')
        assert page.get_by_test_id('provider-limits').count()==0
        page.get_by_role('combobox',name='Main page',exact=True).select_option('__quota_home__')
        expect(page.get_by_test_id('quota-home')).to_be_visible()
        assert page.get_by_test_id('provider-subnavigation').count()==0
        assert not errors,errors
        assert not network,network
        print('PASS dark mobile, no duplicate subnavigation, no external requests or JS errors')
        browser.close()

if __name__=='__main__':run()
