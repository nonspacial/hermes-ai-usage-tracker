"""Verify right-aligned, text-only request return actions and centered Back icon.

Exercises the packaged Desktop component in the offline synthetic harness.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
ART = Path(__file__).parent / 'artifacts'


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1500, 'height': 1100},
                                color_scheme='light', locale='en-US')
        page.set_default_timeout(5000)
        errors, requests = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: requests.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex', exact=True).click()
        page.get_by_role('navigation', name='Provider subpages', exact=True).get_by_role('tab', name='Requests', exact=True).click()
        listing = page.get_by_test_id('request-list')
        expect(listing.locator('tbody tr')).to_have_count(7)
        def session_link(name):
            link=listing.get_by_role('button',name=name,exact=True,include_hidden=True).first
            disclosure=link.locator('xpath=ancestor::tr').locator('.au-record-disclosure')
            if disclosure.is_visible() and disclosure.get_attribute('aria-expanded')=='false':disclosure.click()
            return link
        session_link('demo-session-0').click()
        expect(listing.locator('tbody tr')).to_have_count(5)
        nav = page.get_by_test_id('request-navigation')
        show_all = page.get_by_test_id('request-show-all')
        back = page.get_by_test_id('request-back')
        expect(show_all).to_be_visible()
        expect(back).to_be_visible()
        assert nav.locator('.au-return-actions > button').evaluate_all(
            "els => els.map(e => e.dataset.testid)") == ['request-show-all', 'request-back']
        assert nav.locator(':scope > *').evaluate_all(
            "els => els.map(e => e.className)") == ['au-active-scopes', 'au-return-actions']

        def check_style_and_geometry(width):
            page.set_viewport_size({'width': width, 'height': 1100})
            nav.scroll_into_view_if_needed()
            box, end = nav.bounding_box(), back.bounding_box()
            assert abs(box['x'] + box['width'] - end['x'] - end['width']) < 1, (box, end)
            start = show_all.bounding_box()
            assert start['x'] + start['width'] < end['x']
            assert abs(start['y'] - end['y']) < 1
            for button in (show_all, back):
                style = button.evaluate("""e => { const s = getComputedStyle(e); return {
                    bg:s.backgroundColor, borders:[s.borderTopWidth,s.borderRightWidth,s.borderBottomWidth,s.borderLeftWidth], shadow:s.boxShadow
                }}""")
                assert style == {'bg': 'rgba(0, 0, 0, 0)', 'borders': ['0px']*4, 'shadow': 'none'}, style
                button.hover()
                assert button.evaluate('e => getComputedStyle(e).backgroundColor') == 'rgba(0, 0, 0, 0)'
                assert button.evaluate('e => getComputedStyle(e).textDecorationLine') == 'underline'
            icon = back.locator('svg').bounding_box()
            text = back.locator('.au-return-label').bounding_box()
            assert abs(icon['y']+icon['height']/2-text['y']-text['height']/2) < .25, (icon, text)
            expect(back.locator('svg')).to_have_attribute('aria-hidden', 'true')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            if width >= 900:
                chips = nav.locator('.au-active-scopes').bounding_box()
                assert chips['x'] == box['x'] and chips['x']+chips['width'] < start['x']
            else:
                assert nav.locator('.au-active-scopes').bounding_box()['y'] <= start['y']
            print(f'PASS {width}px: right alignment; Show all then Back; transparent borderless default/hover; centered SVG')

        check_style_and_geometry(1500)
        page.mouse.move(0, 0)
        page.get_by_test_id('provider-subpage').screenshot(path=str(ART / 'request-actions-right.png'))
        check_style_and_geometry(390)
        page.mouse.move(0, 0)
        page.get_by_test_id('provider-subpage').screenshot(path=str(ART / 'request-actions-mobile.png'))
        check_style_and_geometry(320)
        show_all.focus()
        show_all.press('Tab')
        expect(back).to_be_focused()
        assert back.evaluate('e => getComputedStyle(e).outlineStyle') != 'none'
        back.press('Enter')
        expect(listing.locator('tbody tr')).to_have_count(7)
        session_link('demo-session-2').click()
        expect(listing.locator('tbody tr')).to_have_count(1)
        show_all.click()
        expect(listing.locator('tbody tr')).to_have_count(7)
        assert not errors, errors
        assert not requests, requests
        print('PASS keyboard focus/Back and Show all still restore the full scoped list; no script errors or network calls')
        browser.close()


if __name__ == '__main__':
    run()
