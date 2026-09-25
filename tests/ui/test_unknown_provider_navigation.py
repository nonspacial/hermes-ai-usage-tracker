"""Unknown recorded attribution is not a provider navigation destination."""
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
            page = browser.new_page(viewport={'width': 1150, 'height': 850})
            errors, network = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: network.append(request.url))
            page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
            nav = page.get_by_role('navigation', name='Providers', exact=True)
            expect(page.get_by_test_id('quota-home')).to_be_visible()
            assert nav.get_by_role('tab', name='Unknown', exact=True).count() == 0
            page.evaluate('''() => {
                const sample=events.find(r=>r.provider==='openai-codex'&&r.usage);
                for(const provider of ['unknown','a-legitimate-ledger-provider']) {
                    const row=structuredClone(sample);
                    row.id='synthetic-'+provider;row.provider=provider;
                    row.started=Date.now()/1000-5;row.ended=row.started+1;
                    row.session_id='synthetic-'+provider;
                    events.push(row);
                }
            }''')
            nav.get_by_role('tab', name='Codex', exact=True).click()
            expect(page.get_by_test_id('provider-page')).to_have_attribute('data-provider', 'openai-codex')
            expect(nav.get_by_role('tab', name='a-legitimate-ledger-provider', exact=True)).to_be_visible()
            assert nav.get_by_role('tab', name='unknown', exact=True).count() == 0
            picker = page.locator('select.au-provider-select')
            assert picker.locator('option[value="unknown"]').count() == 0
            assert picker.locator('option[value="a-legitimate-ledger-provider"]').count() == 1
            nav.get_by_role('tab', name='a-legitimate-ledger-provider', exact=True).click()
            expect(page.get_by_test_id('provider-page')).to_have_attribute('data-provider', 'a-legitimate-ledger-provider')
            # The unknown row stays in unfiltered accounting, not the navigation catalogue.
            nav.get_by_role('tab', name='All providers', exact=True).click()
            page.wait_for_function('''() => {
                const d=document.querySelector('[data-testid="recorded-summary"]');
                return d && d.getAttribute('aria-label') === 'All providers usage totals';
            }''')
            expect(page.get_by_test_id('recorded-summary').locator('.au-provider-row').filter(has_text='unknown')).to_be_visible()
            page.get_by_role('navigation', name='Provider subpages').get_by_role('tab', name='Requests').click()
            expect(page.get_by_text('synthetic-unknown', exact=False).first).to_be_visible()
            assert nav.get_by_role('tab', name='Codex', exact=True).count() == 1
            assert not network, network
            assert not errors, errors
            print('PASS unknown attribution hidden from provider navigation; usage and legitimate providers retained')
        finally:
            browser.close()


if __name__ == '__main__':
    run()
