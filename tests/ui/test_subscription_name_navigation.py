"""Offline, synthetic browser checks for name-only subscription navigation."""
from pathlib import Path
import os
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width': 1100, 'height': 850})
            errors, network = [], []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.on('request', lambda req: network.append(req.url))
            page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
            tabs = page.get_by_role('navigation', name='Providers', exact=True)
            home = page.get_by_test_id('quota-home')
            expect(home).to_be_visible()
            assert page.get_by_text('AI usage +', exact=True).count() >= 1
            for label, tab in [('Nous Portal', 'Nous Portal'),
                               ('ChatGPT or Codex Subscription', 'Codex'),
                               ('Ollama Cloud', 'Ollama Cloud')]:
                name = home.get_by_role('button', name=f'Open {label} provider page')
                expect(name).to_be_visible()
                before = name.evaluate('e => getComputedStyle(e).color')
                name.hover()
                assert name.evaluate('e => getComputedStyle(e).color') != before
                name.focus()
                name.press('Tab')
                page.keyboard.press('Shift+Tab')
                expect(name).to_be_focused()
                assert name.evaluate('e => getComputedStyle(e).outlineStyle') != 'none'
                name.press('Enter' if tab != 'Codex' else 'Space')
                expect(tabs.get_by_role('tab', name=tab, exact=True)).to_have_attribute('aria-selected', 'true')
                top = page.get_by_test_id('provider-limits')
                assert top.get_attribute('data-provider') == {'Codex': 'openai-codex', 'Nous Portal': 'nous', 'Ollama Cloud': 'ollama'}[tab]
                assert top.get_by_role('button', name=f'Open {label} provider page').count() == 0
                title = top.locator('.au-provider-name-static')
                expect(title).to_have_text(label)
                assert title.evaluate('e => getComputedStyle(e).cursor') != 'pointer'
                title.click()
                expect(tabs.get_by_role('tab', name=tab, exact=True)).to_have_attribute('aria-selected', 'true')
                tabs.get_by_role('tab', name='Subscriptions', exact=True).click()
            # Name click does not hide a provider; X still hides and its hidden card can navigate.
            home.get_by_role('button', name='Hide Ollama Cloud').click()
            assert home.get_by_role('button', name='Open Ollama Cloud provider page').count() == 0
            page.get_by_role('button', name='Hidden 1').click()
            hidden = home.get_by_role('button', name='Open Ollama Cloud provider page')
            expect(hidden).to_be_visible()
            hidden.click()
            expect(tabs.get_by_role('tab', name='Ollama Cloud', exact=True)).to_have_attribute('aria-selected', 'true')
            assert page.get_by_test_id('provider-limits').get_by_role('button', name='Unhide Ollama Cloud').count() == 1
            # Pane-responsive navigation uses the existing select and exact provider id.
            page.set_viewport_size({'width': 390, 'height': 850})
            page.get_by_role('combobox', name='Main page').select_option('__quota_home__')
            expect(page.get_by_test_id('quota-home').get_by_role('button', name='Open Nous Portal provider page')).to_be_visible()
            page.get_by_test_id('quota-home').get_by_role('button', name='Open Nous Portal provider page').click()
            assert page.get_by_role('combobox', name='Main page').input_value() == 'nous'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not network, network
            assert not errors, errors
            print('PASS name-only click/keyboard navigation, hover/focus, top plain text, hide and 390px scope')
        finally:
            browser.close()


if __name__ == '__main__':
    run()
