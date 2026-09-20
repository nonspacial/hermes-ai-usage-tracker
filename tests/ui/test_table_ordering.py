"""Time breakdown reverses only table rows, never the shared chart data."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                   headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100}, locale='en-US', timezone_id='UTC')
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        page.evaluate('''() => {
            const original = rest;
            rest = async (...args) => {
                const result = await original(...args);
                if (args[0].startsWith('/ledger?')) window.orderingData = result;
                return result;
            };
        }''')
        page.get_by_role('tab', name='All providers', exact=True).click()
        for period, grouping in [('Past 24h', 'Hour'), ('7 days', 'Day')]:
            page.get_by_role('group', name='Time window', exact=True).get_by_role('button', name=period, exact=True).click()
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name=grouping, exact=True).click()
            page.wait_for_function("window.orderingData && orderingData.trend.unit === " + repr('hour' if grouping == 'Hour' else 'day'))
            before = page.evaluate('orderingData.trend.buckets.map(b => b.start)')
            expected = page.evaluate('''hour => [...orderingData.trend.buckets].sort((a,b)=>b.start-a.start).map(
                b => new Date(b.start*1000).toLocaleString(undefined, {
                    timeZone:'UTC',month:'short',day:'numeric',...(hour?{hour:'numeric'}:{})}))''', grouping == 'Hour')
            expect(page.locator('.au-breakdown tbody tr td:first-child .au-field-value')).to_have_text(expected)
            after = page.evaluate('orderingData.trend.buckets.map(b => b.start)')
            assert after == before == sorted(before)
            # Switching tables must not leave the chart's bucket array reversed.
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Model', exact=True).click()
            assert page.evaluate('orderingData.trend.buckets.map(b => b.start)') == before
        assert not errors, errors
        assert not network, network
        browser.close()
        print('PASS Hour/Day tables newest first; chart buckets remain chronological; no errors or external requests')


if __name__ == '__main__':
    run()
