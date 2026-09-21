"""Exercise the packaged record accordions with offline synthetic data."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                    args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 2200, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.abort())
        page.set_content((ROOT / 'preview.html').read_text())
        page.add_style_tag(content='#root{max-width:none}')
        page.get_by_role('tab', name='All providers', exact=True).click()
        nav = page.get_by_role('navigation', name='Provider subpages')
        for name in ['Requests', 'Cache & costs', 'Compressions', 'Models & tasks']:
            nav.get_by_role('tab', name=name, exact=True).click()
            pane = page.locator('.au-ledger')
            pane.evaluate('(e)=>e.style.width="390px"')
            table = page.locator('.au-table[data-accordions="true"]').first
            expect(table).to_have_attribute('data-layout', 'records')
            row = table.locator('tbody tr').first
            button = row.locator('.au-record-disclosure')
            expect(button).to_have_attribute('aria-expanded', 'false')
            expect(row.locator('td').nth(1)).to_be_hidden()
            assert '$' not in button.inner_text()
            assert 'estimated' not in button.inner_text().lower()
            button.focus()
            button.press('Enter')
            expect(button).to_have_attribute('aria-expanded', 'true')
            expect(row.locator('td').nth(1)).to_be_visible()
            row.evaluate('(e)=>window.savedRow=e')
            page.evaluate('queryClient.invalidateQueries()')
            page.wait_for_timeout(150)
            assert row.evaluate('(e)=>e===window.savedRow')
            expect(button).to_have_attribute('aria-expanded', 'true')
            button.focus()
            pane.evaluate('(e)=>e.style.width=""')
            expect(table).to_have_attribute('data-layout', 'table')
            expect(row).to_be_focused()
            expect(button).to_be_hidden()
            pane.evaluate('(e)=>e.style.width="390px"')
            expect(table).to_have_attribute('data-layout', 'records')
            expect(button).to_have_attribute('aria-expanded', 'true')
            button.click()
            expect(button).to_have_attribute('aria-expanded', 'false')
            expect(row.locator('td').nth(1)).to_be_hidden()
            assert table.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
            path = ROOT / 'tests/ui/artifacts' / ('accordion-' + name.split()[0].lower() + '.png')
            table.screenshot(path=str(path))
            pane.evaluate('(e)=>e.style.width=""')
        assert not errors, errors
        browser.close()
    print('PASS four accordion pages: keyboard, collapse/expand, refresh identity, wide-table focus, narrow containment')


if __name__ == '__main__':
    run()
