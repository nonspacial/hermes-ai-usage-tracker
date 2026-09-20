"""Real shared renderer: table-to-record reflow without dropped data or state."""
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
        page = browser.new_page(viewport={'width': 1900, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        # The demo shell caps itself at 1440px; Desktop panes can be wider.
        page.add_style_tag(content='#root{max-width:none}')
        page.get_by_role('tab', name='All providers', exact=True).click()
        sub = page.get_by_role('navigation', name='Provider subpages', exact=True)

        def settle():
            page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(r))))')

        def contained():
            assert page.evaluate('''() => [...document.querySelectorAll('.au-table')].every(e =>
                e.scrollWidth <= e.clientWidth + 1)''')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

        for name in ['Overview', 'Requests', 'Cache & costs', 'Compressions', 'Models & tasks']:
            sub.get_by_role('tab', name=name, exact=True).click()
            table = page.locator('.au-table').first
            settle()

            expect(table).to_have_attribute('data-layout', 'table')
            values = table.locator('tbody .au-field-value').all_text_contents()
            footer = table.locator('tfoot .au-field-value').all_text_contents()
            row_count = table.locator('tbody tr').count()
            assert row_count > 0
            for width in [850, 500, 390, 320]:
                # Keep the browser wide: only the plugin pane shrinks.
                page.locator('.au-ledger').evaluate('(e,w) => e.style.width=w+"px"', width)
                settle()
                contained()
                if width <= 390:
                    expect(table).to_have_attribute('data-layout', 'records')
                assert table.locator('tbody .au-field-value').all_text_contents() == values
                assert table.locator('tfoot .au-field-value').all_text_contents() == footer
                assert table.locator('tbody tr').count() == row_count
                if table.get_attribute('data-layout') == 'records':
                    assert table.locator('tbody tr').first.locator('.au-field-label').all_text_contents() == table.locator('thead th').all_text_contents()
                    expect(table.locator('tbody tr').first.locator('.au-field-label').first).to_be_visible()
            page.locator('.au-ledger').evaluate('(e) => e.style.width=""')
            expect(table).to_have_attribute('data-layout', 'table')
        print('PASS every shared table: pane-driven reflow, identical rows/values/totals/labels, no x overflow, restores wide table')

        sub.get_by_role('tab', name='Requests', exact=True).click()
        detail = page.locator('.au-json-details').first
        detail.locator('summary').click()
        expect(detail).to_have_attribute('open', '')
        detail.evaluate('(e) => window.originalInspector=e')
        snapshot = detail.locator('code').text_content()
        copy = detail.get_by_role('button', name='Copy JSON to clipboard')
        copy.focus()
        for width in [650, 390, 320, 1800, 390]:
            page.locator('.au-ledger').evaluate('(e,w) => e.style.width=w+"px"', width)
            settle()
            contained()
            assert detail.evaluate('(e) => e===window.originalInspector && e.open')
            assert detail.locator('code').text_content() == snapshot
            expect(copy).to_be_focused()
            if width < 700:
                box = detail.locator('.au-json-panel').bounding_box()
                assert box is not None and box['width'] <= width
        detail.locator('summary').click()
        page.locator('.au-table tbody tr').first.screenshot(path=str(ART / 'responsive-request-390.png'))
        print('PASS open JSON identity/snapshot/focus survives resize; JSON respects pane width')

        page.locator('.au-ledger').evaluate('(e) => e.style.width=""')
        page.evaluate('''() => {for(const e of events){
            e.model=e.response_model='organisation/'+('long-model-identifier-'.repeat(8));
        } queryClient.invalidateQueries()}''')
        for name in ['Overview', 'Requests', 'Cache & costs', 'Models & tasks']:
            sub.get_by_role('tab', name=name, exact=True).click()
            for width in [1500, 650, 390, 320]:
                page.locator('.au-ledger').evaluate('(e,w) => e.style.width=w+"px"', width)
                settle()
                contained()
        page.locator('.au-ledger').evaluate('(e) => e.style.width="650px"')
        settle()
        page.locator('.au-table tbody tr').first.screenshot(path=str(ART / 'responsive-model-650.png'))
        assert not errors, errors
        assert not network, network
        browser.close()
        print('PASS long identifiers wrap across table types; no browser errors or external requests')


if __name__ == '__main__':
    run()
