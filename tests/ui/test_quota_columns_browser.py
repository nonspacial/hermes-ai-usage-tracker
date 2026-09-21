"""Synthetic provider quota layout; no accounts or network access."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1500, 'height': 1000})
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text())
        expect(page.get_by_test_id('quota-home')).to_be_visible()
        page.evaluate("""() => {
            const p=demoProviders.find(p=>p.id==='nous');
            p.quota.windows=[];
            p.quota.details=Array.from({length:8},(_,i)=>`Credit detail ${i+1}: $12.34`);
            queryClient.invalidateQueries({queryKey:['ai-usage-tracker','usage']});
        }""")
        card = page.locator('.au-quota-card').filter(has=page.get_by_text('Nous Portal', exact=True))
        rows = card.locator('.au-quota-rows')
        expect(rows).to_have_attribute('data-columns', 'multiple')
        values = rows.locator(':scope > *').all_text_contents()
        assert len(values) == 8
        def columns():
            return rows.evaluate("e=>getComputedStyle(e).gridTemplateColumns.split(' ').length")
        assert columns() == 2
        wide_box = rows.bounding_box()
        assert wide_box is not None
        wide_height = wide_box['height']
        card.evaluate("e=>e.style.width='400px'")
        assert columns() == 1
        narrow_box = rows.bounding_box()
        assert narrow_box is not None and narrow_box['height'] > wide_height
        assert rows.locator(':scope > *').all_text_contents() == values
        card.evaluate("e=>e.style.width=''")
        assert columns() == 2
        page.evaluate("""() => {
            const p=demoProviders.find(p=>p.id==='nous');p.quota.details=p.quota.details.slice(0,5);
            queryClient.invalidateQueries({queryKey:['ai-usage-tracker','usage']});
        }""")
        expect(rows).to_have_attribute('data-columns', 'single')
        assert columns() == 1
        page.evaluate("""() => {
            const p=demoProviders.find(p=>p.id==='nous');
            p.quota.windows=[{label:'Reported weekly window',remaining_percent:75,reset_at:'2099-01-01T00:00:00Z'}];
            queryClient.invalidateQueries({queryKey:['ai-usage-tracker','usage']});
        }""")
        expect(rows).to_have_attribute('data-columns', 'multiple')
        expect(rows.get_by_text('75% left', exact=True)).to_be_visible()
        assert columns() == 2
        art = ROOT / 'tests/ui/artifacts'
        art.mkdir(exist_ok=True)
        card.screenshot(path=str(art / 'quota-columns-wide.png'))
        page.set_viewport_size({'width': 390, 'height': 1000})
        assert columns() == 1
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        card.screenshot(path=str(art / 'quota-columns-narrow.png'))
        assert not errors, errors
        assert not network, network
        print('PASS quota rows: six-row threshold, two columns, pane-width fallback, unchanged contents, reported bars, no external requests')
        browser.close()


if __name__ == '__main__':
    run()
