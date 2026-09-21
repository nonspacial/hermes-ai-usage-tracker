"""Real promise/ack-driven UI state, using controlled offline SDK boundaries."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
ART = Path(__file__).parent / 'artifacts'


def run():
    ART.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                   headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1500, 'height': 1050})
        page.set_default_timeout(6000)
        page.clock.install()
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        page.get_by_role('tab', name='Codex', exact=True).click()
        badge = page.get_by_test_id('connection-status')
        expect(badge).to_have_text('Online')
        page.evaluate('''() => {
            window.originalRest=rest;window.gates={status:[],read:[],reload:[]};
            rest=async(path,options)=>{
                const kind=path.startsWith('/ledger/status?')?'status':path.startsWith('/ledger?')?'read':path.startsWith('/ledger/analytics/reload?')?'reload':null;
                if(kind==='reload'&&window.missingReload)throw new Error('404 Not Found');
                const result=await originalRest(path,options);
                if(kind&&window['hold_'+kind])await new Promise(resolve=>gates[kind].push(resolve));
                return result;
            };
            window.release=kind=>{window['hold_'+kind]=false;gates[kind].splice(0).forEach(resolve=>resolve())};
        }''')
        # Prior Online is invalidated immediately; only actual responses end busy.
        page.evaluate('hold_status=true;hold_read=true')
        badge.click()
        expect(badge).to_have_attribute('data-state', 'checking')
        expect(badge).to_have_attribute('aria-busy', 'true')
        expect(badge).to_be_disabled()
        assert badge.locator('.au-connection-dot').evaluate('el=>getComputedStyle(el).animationName') == 'au-connection-spin'
        page.clock.fast_forward(2000)
        expect(badge).to_have_text('Reconnecting')
        starts = page.evaluate('demoSocketStarts')
        badge.evaluate('el=>el.click()')
        assert page.evaluate('demoSocketStarts') == starts
        page.evaluate("release('status')")
        expect(badge).to_have_attribute('aria-busy', 'true')
        page.evaluate("release('read')")
        expect(badge).to_have_text('Online')
        expect(badge).to_have_attribute('aria-busy', 'false')
        print('PASS click invalidates cached green; spinner/disabled state follows pending checks, not elapsed time')

        # A reachable API cannot stand in for a missing subscription acknowledgement.
        page.evaluate('failSocket=true')
        badge.click()
        expect(badge).to_have_text('Reconnecting')
        page.clock.fast_forward(8001)
        expect(badge).to_have_text('Limited')
        expect(badge).to_have_attribute('aria-busy', 'false')
        assert 'acknowledgement' in badge.get_attribute('title')
        page.evaluate('failSocket=false')
        page.clock.fast_forward(20001)
        expect(badge).to_have_text('Online')
        # Stop fresh status delivery; receipt expiry is a failure, not green animation.
        page.evaluate('hold_status=true')
        page.clock.fast_forward(31000)
        expect(badge).to_have_attribute('data-state', 'disconnected')
        page.evaluate("release('status')")
        expect(badge).to_have_text('Online')
        print('PASS missing/silent subscription and stale status cannot show Online; actual acknowledgements recover')

        def menu(action):
            page.get_by_label('Refresh actions', exact=True).click()
            page.get_by_role('button', name=action, exact=True).click()

        before = page.evaluate('demoCalls.length')
        menu('Refresh data')
        expect(badge).to_have_text('Online')
        calls = page.evaluate(f'demoCalls.slice({before})')
        assert any(path.startswith('/usage?') for path in calls)
        assert not any('/analytics/reload' in path for path in calls)
        # Reload is an explicit separate action; it cannot make an absent recorder green.
        page.evaluate("demoStatusState='not_recording'")
        badge.click(); expect(badge).to_have_text('Not recording')
        page.evaluate('hold_reload=true')
        menu('Reload analytics backend')
        notice = page.get_by_test_id('analytics-reload-result')
        expect(notice).to_have_text('Validating analytics code…')
        expect(badge).to_be_disabled()
        page.evaluate("release('reload')")
        expect(notice).to_contain_text('loaded.')
        expect(badge).to_have_text('Not recording')
        paths = page.evaluate('demoCalls')
        assert any(path.startswith('/ledger/analytics?') for path in paths)
        page.evaluate('failReload=true')
        menu('Reload analytics backend')
        expect(notice).to_contain_text('previous code remains active')
        expect(badge).to_have_text('Not recording')
        page.evaluate('failReload=false;missingReload=true')
        menu('Reload analytics backend')
        expect(notice).to_contain_text('One backend restart is required')
        page.evaluate('missingReload=false')
        print('PASS explicit split-menu actions; active revision readback; failed/unsupported reload is never success')

        # Confirmed recorder absence takes precedence over an unrelated pending read.
        page.evaluate('hold_read=true')
        badge.click()
        expect(badge).to_have_text('Not recording')
        expect(badge).to_have_attribute('data-state', 'not_recording')
        expect(badge).to_have_attribute('aria-busy', 'true')
        page.evaluate("release('read')")
        expect(badge).to_have_attribute('aria-busy', 'false')

        # A delayed reload must not reconnect the old provider's view.
        page.evaluate("demoStatusState='online'")
        badge.click(); expect(badge).to_have_text('Online')
        page.evaluate('hold_reload=true')
        menu('Reload analytics backend')
        page.get_by_role('tab', name='All providers', exact=True).click()
        expect(badge).to_have_text('Online')
        before = page.evaluate('demoCalls.length')
        page.evaluate("release('reload')")
        expect(notice).to_contain_text('loaded.')
        calls = page.evaluate(f'demoCalls.slice({before})')
        assert not any(path.startswith('/ledger?') and 'provider=openai-codex' in path for path in calls), calls
        expect(badge).to_have_text('Online')
        print('PASS confirmed missing recorder is red during pending reads; delayed reload cannot revive an old view')

        # An old in-flight response from the previous profile must not turn the new one green.
        page.evaluate("demoStatusState='online';hold_status=true")
        badge.click()
        page.evaluate("hold_status=false;demoStatusState='not_recording'")
        page.get_by_role('combobox', name='Hermes profile').select_option('profile:default')
        expect(badge).to_have_text('Not recording')
        page.evaluate("release('status')")
        expect(badge).to_have_text('Not recording')
        page.set_viewport_size({'width': 390, 'height': 1000})
        for width in (1500, 1032, 847, 650, 500, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000})
            boxes = [page.locator(selector).bounding_box() for selector in (
                '.au-header-title', '.au-header-chip', '.au-header-profile', '.au-refresh-split', '.au-connection')]
            assert all(box is not None for box in boxes)
            boxes = [box for box in boxes if box is not None]
            centres = [box['y'] + box['height']/2 for box in boxes]
            assert max(centres)-min(centres)<2, (width, boxes)
            assert all(box['x']>=0 and box['x']+box['width']<=width for box in boxes), (width, boxes)
            assert all(a['x']+a['width']<=b['x']+1 for a,b in zip(boxes,boxes[1:])), (width,boxes)
            page.locator('.au-page-header').screenshot(path=str(ART / f'inline-header-{width}.png'))
        page.set_viewport_size({'width': 390, 'height': 1000})
        page.get_by_label('Refresh actions', exact=True).click()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        menu_box = page.locator('.au-refresh-options').bounding_box()
        assert menu_box['x'] >= 0 and menu_box['x'] + menu_box['width'] <= 390
        assert page.locator('.au-refresh-options').evaluate('el=>getComputedStyle(el).borderTopWidth') == '1px'
        assert page.locator('.au-refresh-options button').evaluate_all('items=>items.every(el=>getComputedStyle(el).borderTopWidth==="0px")')
        page.screenshot(path=str(ART / 'refresh-menu-traffic-lights.png'), full_page=True)
        page.get_by_role('button', name='Reload analytics backend', exact=True).click()
        expect(notice).to_contain_text('loaded.')
        page.evaluate('hold_reload=true')
        menu('Reload analytics backend')
        expect(notice).to_have_text('Validating analytics code…')
        page.evaluate("ReactDOM.unmountComponentAtNode(document.getElementById('root'))")
        before = page.evaluate('demoCalls.length')
        page.evaluate("release('reload')")
        page.wait_for_function("before=>demoCalls.slice(before).some(path=>path.startsWith('/ledger/analytics?'))", arg=before)
        calls = page.evaluate(f'demoCalls.slice({before})')
        assert not any(path.startswith(('/ledger?', '/ledger/status?')) for path in calls), calls
        assert not errors, errors
        assert not network, network
        browser.close()
        print('PASS profile-race protection, mobile dropdown, no browser errors or external requests')


if __name__ == '__main__':
    run()
