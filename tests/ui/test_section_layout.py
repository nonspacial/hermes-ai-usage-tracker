"""Test.10: remove duplicate headings; move refresh and compression notes below tables.

Only the offline synthetic preview is exercised. No account or provider calls.
"""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
ART = Path(__file__).parent / 'artifacts'
NOTE = ('Before/after/threshold are Hermes context estimates. Next-call input is provider-reported '
        'when supplied and may include newly added tool or user content; it is not a pure measurement '
        'of compression savings. Auxiliary token/cost totals are already included in request totals.')


def run():
    ART.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100},
                                color_scheme='light', locale='en-US')
        page.set_default_timeout(5000)
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        page.get_by_role('navigation', name='Providers', exact=True).get_by_role(
            'tab', name='Codex', exact=True).click()
        sub = page.get_by_role('navigation', name='Provider subpages', exact=True)
        cache = page.get_by_test_id('cache-costs')
        refresh = page.get_by_role('button', name='Refresh provider prices', exact=True)
        comps = page.get_by_test_id('compression-events')

        def before(a, b):
            x, y = a.bounding_box(), b.bounding_box()
            assert x and y and x['y'] + x['height'] <= y['y'] + .5, (x, y)

        for width in (1700, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1100})
            sub.get_by_role('tab', name='Cache & costs', exact=True).click()
            expect(cache).to_be_visible()
            assert cache.locator('h3,h4').count() == 0
            assert 'Cache & token costs' not in cache.inner_text()
            assert 'Published rates · USD per 1M tokens' not in cache.inner_text()
            assert cache.locator('details').count() == 0
            assert cache.locator(':scope > *').evaluate_all(
                'els => els.map(e => e.dataset.testid)') == [
                'component-cost-cards', 'cache-savings-summary', 'published-rates', 'price-refresh-controls']
            cards = page.get_by_test_id('cache-savings-summary')
            rates = page.get_by_test_id('published-rates')
            costs = page.get_by_test_id('component-cost-cards')
            footer = page.get_by_test_id('price-refresh-controls')
            before(cards, rates)
            before(costs, cards)
            before(costs, footer)
            before(costs, refresh)
            assert 'USD per 1M tokens' in rates.get_attribute('title')
            assert 'selected window' in rates.get_attribute('aria-label')
            end, button = cache.bounding_box(), refresh.bounding_box()
            assert abs(end['x']+end['width']-button['x']-button['width']) <= 1, (end, button)
            refresh.focus()
            start = page.evaluate('demoCalls.length')
            refresh.press('Enter')
            expect(refresh).to_be_enabled()
            page.wait_for_timeout(80)
            calls = page.evaluate(f'demoCalls.slice({start})')
            assert any(c.startswith('/ledger/pricing/refresh?profile=infra') for c in calls), calls
            assert page.get_by_test_id('published-rates').locator('tbody tr').count() == 2
            assert page.get_by_test_id('component-cost-cards').locator('article').count() == 5
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if width != 320:
                page.get_by_test_id('provider-subpage').screenshot(path=str(ART / f'cache-clean-{width}.png'))
            print(f'PASS {width}px cache: no duplicate headings; component cards/savings/rates/refresh order; right-aligned keyboard refresh')

            sub.get_by_role('tab', name='Compressions', exact=True).click()
            expect(comps).to_be_visible()
            assert 'Compression and compaction events' not in comps.inner_text()
            assert comps.locator('h3,h4,strong').count() == 0
            controls = comps.locator(':scope > .au-toolbar')
            table = page.get_by_test_id('compression-table')
            note = page.get_by_test_id('compression-note')
            before(controls, table)
            before(table, note)
            expect(note).to_have_text(NOTE)
            kind = page.get_by_role('combobox', name='Compression type', exact=True)
            expect(table.locator('tbody tr')).to_have_count(2)
            for value in ('compression', 'micro_compaction'):
                kind.select_option(value)
                expect(table.locator('tbody tr')).to_have_count(1)
                before(table, note)
            kind.select_option('all')
            expect(table.locator('tbody tr')).to_have_count(2)
            with page.expect_download() as downloaded:
                page.get_by_role('button', name='Export compression JSON', exact=True).click()
            payload = json.loads(Path(downloaded.value.path()).read_text())
            assert len(payload) == 2 and all(r['provider'] == 'openai-codex' for r in payload)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if width != 320:
                page.get_by_test_id('provider-subpage').screenshot(path=str(ART / f'compressions-clean-{width}.png'))
            print(f'PASS {width}px compression: no repeated heading; note follows table; filtering/export unchanged')

        # No-data views still put explanatory text after the empty table/message.
        page.get_by_role('textbox', name='Session ID', exact=True).fill('no-matching-session')
        expect(page.get_by_test_id('compression-table').locator('tbody tr')).to_have_count(0)
        expect(comps.get_by_text('No compression events in this window.', exact=True)).to_be_visible()
        before(comps.get_by_text('No compression events in this window.', exact=True), page.get_by_test_id('compression-note'))
        page.get_by_role('button', name='Clear filters', exact=True).click()
        expect(page.get_by_test_id('compression-table').locator('tbody tr')).to_have_count(2)
        # Retain actual unavailable-price indication while removing normal prose/headers.
        page.evaluate('demoCatalogs[0].status="unavailable";demoCatalogs[0].stale=true')
        sub.get_by_role('tab', name='Cache & costs', exact=True).click()
        refresh.click()
        expect(cache.get_by_text('Some prices unavailable or stale', exact=True)).to_be_visible()
        before(page.get_by_test_id('component-cost-cards'), refresh)
        assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(17, 17, 30)'
        assert not errors, errors
        assert not network, network
        print('PASS empty results, retained unavailable-price state, original dark theme; no script errors or network calls')
        browser.close()


if __name__ == '__main__':
    run()
