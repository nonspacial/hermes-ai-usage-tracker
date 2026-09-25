"""Offline, synthetic connection-badge freshness and shared tooltip regression."""
import os
import re
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT = Path(os.environ.get('HERMES_SCRATCH', '/home/nope/.hermes/profiles/infra/cache/scratch')) / 'usage-badge-freshness.png'


def footer(page):
    return page.get_by_test_id('ledger-freshness')


def check_tooltip(page, badge, state, prefix, tone):
    badge.hover()
    popup = page.locator('.au-tooltip[role="tooltip"]:visible').last
    expect(popup).to_contain_text('Running agents are not restarted.')
    expect(footer(page)).to_contain_text(prefix)
    expect(badge).to_have_attribute('data-state', state)
    expect(badge).to_have_attribute('aria-describedby', popup.get_attribute('id'))
    age = footer(page).locator('.au-tooltip-freshness-age')
    if tone:
        expect(age).to_have_attribute('data-tone', tone)
        token = '--ui-text-success' if tone == 'healthy' else '--ui-text-warning'
        colours = age.evaluate('''(el, token) => {
            const probe=document.createElement('span');probe.style.color='var('+token+','+(token==='--ui-text-success'?'#64bba8':'#d2b776')+')';
            el.parentElement.appendChild(probe);
            const expected=getComputedStyle(probe).color;
            const actual=getComputedStyle(el).color;
            const variable=getComputedStyle(el).getPropertyValue(token);
            probe.remove();return {actual,expected,variable};
        }''', token)
        assert colours['actual'] == colours['expected'], colours
    else:
        assert age.count() == 0
    return popup


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'), headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1100, 'height': 820})
        page.clock.install()
        errors, outbound = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: outbound.append(request.url))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        badge = page.get_by_test_id('connection-status')
        expect(badge).to_have_attribute('data-state', 'online')
        popup = check_tooltip(page, badge, 'online', 'Usage idle', None)
        assert 'Click to reconnect and refresh.' in popup.inner_text()
        page.get_by_role('tab', name='All providers', exact=True).click()
        expect(page.get_by_test_id('recorded-summary')).to_be_visible()
        expect(page.locator('.au-window-label')).to_have_count(0)
        assert 'since received' not in page.locator('.au-upper').inner_text()
        assert 'generated ' not in page.locator('.au-upper').inner_text()
        popup = check_tooltip(page, badge, 'online', 'Updated', 'healthy')
        first = footer(page).locator('.au-tooltip-freshness-age').inner_text()
        page.clock.fast_forward(5000)
        page.wait_for_function('first => document.querySelector(".au-tooltip-freshness-age")?.textContent !== first', arg=first)
        assert footer(page).inner_text().startswith('Updated ')
        assert 'generated' not in popup.inner_text()
        page.evaluate("document.querySelector('.au-ledger').style.setProperty('--ui-text-success','#17b6a3')")
        assert footer(page).locator('.au-tooltip-freshness-age').evaluate(
            'el => getComputedStyle(el).color') == 'rgb(23, 182, 163)'
        page.evaluate("document.querySelector('.au-ledger').style.removeProperty('--ui-text-success')")
        # Focus alone exposes the same styled, described footer and restores native title on departure.
        page.mouse.move(500, 600)
        badge.focus()
        expect(footer(page)).to_be_visible()
        expect(badge).to_have_attribute('aria-describedby', popup.get_attribute('id'))
        badge.blur()
        page.mouse.move(500, 600)
        expect(badge).to_have_attribute('title', re.compile('Running agents are not restarted'))
        # Keep the currently selected read pending; do not borrow the prior scope's age.
        page.evaluate('''()=>{window.held=[];const original=rest;rest=(path,opts)=>{
          if(new URL(path,'https://offline').pathname==='/ledger')
           return new Promise((resolve,reject)=>held.push({path,deliver:()=>original(path,opts).then(resolve,reject)}));
          return original(path,opts);
        }}''')
        page.get_by_role('tab', name='Requests', exact=True).click()
        page.wait_for_function('held.length>0')
        check_tooltip(page, badge, 'online', 'Updating…', None)
        assert 's' not in footer(page).inner_text()
        page.evaluate('held.shift().deliver()')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        check_tooltip(page, badge, 'online', 'Updated', 'healthy')
        # Offline evidence gives an amber age despite the previously healthy snapshot.
        page.evaluate('failStatus=true')
        badge.click()
        expect(badge).to_have_attribute('data-state', 'disconnected')
        check_tooltip(page, badge, 'disconnected', 'Updating…', 'caution')
        # A failed read retains its own dated snapshot without claiming an update.
        page.evaluate('failLedger=true;held.shift().deliver()')
        expect(footer(page)).to_contain_text('Update failed')
        check_tooltip(page, badge, 'disconnected', 'Update failed', 'caution')
        # The popup is body-mounted, but must follow pane-scoped colour overrides.
        page.evaluate("document.querySelector('.au-ledger').style.setProperty('--ui-text-warning','#e19532')")
        assert footer(page).locator('.au-tooltip-freshness-age').evaluate(
            'el => getComputedStyle(el).color') == 'rgb(225, 149, 50)'
        page.evaluate('failLedger=false;failStatus=false')
        page.get_by_role('tab', name='Overview', exact=True).click()
        page.wait_for_function('held.length>0')
        page.evaluate('held.shift().deliver()')
        expect(page.locator('.au-breakdown')).to_be_visible()
        page.get_by_role('tab', name='Requests', exact=True).click()
        page.wait_for_function('held.length>0')
        expect(page.get_by_test_id('request-list')).to_be_visible()
        badge.hover()
        expect(footer(page)).to_contain_text('Updating… · cached')
        expect(footer(page).locator('.au-tooltip-freshness-age')).to_be_visible()
        page.screenshot(path=str(SCREENSHOT))
        page.set_viewport_size({'width': 390, 'height': 500})
        badge.hover()
        assert footer(page).is_visible()
        assert page.locator('.au-tooltip[role="tooltip"]:visible').last.evaluate('''el => {
            const box=el.getBoundingClientRect();return box.left>=0 && box.right<=innerWidth &&
                box.top>=0 && box.bottom<=innerHeight && getComputedStyle(el).boxShadow!=='none';
        }''')
        page.screenshot(path=str(SCREENSHOT.with_name('usage-badge-freshness-narrow.png')))
        # Switching profiles cannot carry the previous profile's cached age.
        page.get_by_role('combobox', name='Hermes profile').select_option('profile:default')
        page.get_by_label('Main page', exact=True).select_option('')
        page.wait_for_function('''()=>document.querySelector('[data-testid="connection-status"]')
            ?.getAttribute('data-au-freshness')?.startsWith('Updating')''')
        badge.hover()
        expect(footer(page)).to_contain_text('Updating…')
        assert footer(page).locator('.au-tooltip-freshness-age').count() == 0
        assert 'cached' not in footer(page).inner_text()
        assert not errors and not outbound, (errors, outbound)
        browser.close()
        print('PASS badge tooltip: original text, concise scoped footer, cached/pending/failed/idle states, ticking age, theme colours, hover/focus and narrow fit')
        print('Screenshots:', SCREENSHOT, SCREENSHOT.with_name('usage-badge-freshness-narrow.png'))


if __name__ == '__main__':
    run()
