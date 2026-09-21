"""Synthetic scope integration, collisions, stale replies and polling; no live data."""
import csv
import io
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True,
                                   args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100})
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        picker = page.get_by_role('combobox', name='Hermes profile', exact=True)
        expect(picker).to_have_value('profile:infra')
        assert picker.locator('option').all_text_contents() == ['All profiles', 'default · default', 'infra', 'all']
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Requests', exact=True).click()
        expect(page.locator('tbody tr').first).to_be_visible()
        picker.select_option('profile:all')
        expect(page.get_by_role('button', name='Start test marker')).to_be_visible()
        assert page.evaluate("scopeCalls.some(c=>c.path.startsWith('/ledger?')&&new URL(c.path,'https://offline').searchParams.get('profile')==='all')")
        assert page.evaluate("demoStored['selected-profile-v1']") == 'all'
        # Old filters and inspectors must not enter any aggregate request.
        page.get_by_role('textbox', name='Session ID', exact=True).fill('demo-session-0')
        page.evaluate('window.scopeCalls=[];window.holdAggregate=[]')
        picker.select_option('scope:all')
        expect(page.get_by_role('textbox', name='Session ID', exact=True)).to_have_value('')
        expect(page.locator('tbody tr')).to_have_count(0)
        assert page.evaluate("demoStored['selected-profile-v1']") == 'all'
        assert page.evaluate("demoStored['selected-profile-scope-v1']") == 'all'
        assert page.evaluate("scopeCalls.filter(c=>c.path.startsWith('/ledger?')).every(c=>!new URL(c.path,'https://offline').searchParams.get('session'))")
        # A held aggregate response cannot reappear after selecting an individual.
        page.wait_for_function('holdAggregate.length>0')
        picker.select_option('profile:default')
        expect(page.locator('tbody tr').first).to_be_visible()
        page.evaluate('holdAggregate.splice(0).forEach(f=>f());window.holdAggregate=null')
        expect(picker).to_have_value('profile:default')
        assert not page.locator('[data-row-id^="ap1."]').count()
        # Enter aggregate with colliding local IDs from two physical profiles.
        picker.select_option('scope:all')
        expect(page.get_by_test_id('profile-coverage')).to_contain_text('complete')
        expect(page.locator('tbody tr').first).to_be_visible()
        keys = page.locator('tbody tr').evaluate_all('(rows)=>rows.map(r=>r.dataset.rowId)')
        assert len(set(keys)) == len(keys) and all(k.startswith('ap1.') for k in keys)
        assert ' · infra' in page.locator('tbody').inner_text() and ' · default' in page.locator('tbody').inner_text()
        expect(page.get_by_role('button', name='Start test marker')).to_have_count(0)
        expect(page.get_by_label('Refresh actions', exact=True)).to_have_count(0)
        # Fixed JSON snapshots remain keyed independently across refreshes.
        row = page.locator('tbody tr').first
        disclosure = row.locator('.au-record-disclosure')
        if disclosure.is_visible():
            disclosure.click()
        row.locator('summary').click()
        before = row.locator('pre').inner_text()
        assert 'original_ids' in before and 'profile_id' in before
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(row.locator('pre')).to_have_text(before)
        page.screenshot(path=str(ROOT / 'tests/ui/artifacts/all-profiles-wide.png'))
        # Session drill sends opaque identity, not its human-readable label.
        sid = row.locator('.au-drill').first.get_attribute('title')
        assert sid and sid.startswith('ap1.')
        row.locator('.au-drill').first.click()
        expect(page.get_by_role('textbox', name='Session ID', exact=True)).to_have_value(sid)
        expect(page.get_by_test_id('profile-coverage')).to_contain_text('1 / 1 readable')
        assert len(set(page.locator('tbody tr').evaluate_all('(rs)=>rs.map(r=>JSON.parse(atob(r.dataset.rowId.slice(4).replaceAll("-","+").replaceAll("_","/")))[0])'))) == 1
        page.get_by_role('button', name='Show all requests', exact=True).click()
        page.get_by_role('tab', name='Cache & costs', exact=True).click()
        expect(page.get_by_test_id('component-cost-cards')).to_be_visible()
        expect(page.get_by_role('button', name='Refresh provider prices')).to_have_count(0)
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        legend = page.locator('.au-skill-legend-row').first
        expect(legend).to_be_visible()
        assert ' · ' in legend.inner_text()
        legend.click()
        page.wait_for_function("scopeCalls.some(c=>c.path.startsWith('/ledger/skills?')&&new URL(c.path,'https://offline').searchParams.get('skill')?.startsWith('ap1.'))")
        expect(page.get_by_test_id('skill-drilldown')).to_be_visible()
        assert 'ap1.' not in page.get_by_test_id('skill-drilldown').locator('h3').inner_text()
        page.get_by_role('button', name='Clear skill selection').click()
        page.get_by_role('button', name='Context footprint', exact=True).click()
        snapshots = page.get_by_role('combobox', name='Context snapshot')
        values = snapshots.locator('option').evaluate_all('(xs)=>xs.map(x=>x.value).filter(Boolean)')
        assert len(values) == len(set(values)) and all(v.startswith('ap1.') for v in values)
        snapshots.select_option(values[-1])
        assert 'ap1.' not in page.get_by_test_id('skill-context').inner_text()
        # Partial and unavailable are explicit, never previous profile totals or zero.
        page.evaluate("demoAggregateCoverage='partial'")
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.get_by_test_id('profile-coverage').first).to_contain_text('partial')
        page.get_by_role('tab', name='Requests', exact=True).click()
        with page.expect_download() as download:
            page.get_by_role('button', name='Export request CSV', exact=True).click()
        rows = list(csv.DictReader(io.StringIO(Path(download.value.path()).read_text())))
        assert rows and all(r['export_atomic'] == 'false' and 'partial' in r['profile_coverage'] for r in rows)
        assert all(r['profile'] and r['profile_id'] and r['original_ids'] for r in rows)
        assert len({r['id'] for r in rows}) == len(rows)
        page.evaluate("demoAggregateCoverage='unavailable'")
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.get_by_test_id('recorded-summary')).to_contain_text('unavailable')
        expect(page.get_by_test_id('profile-coverage')).to_contain_text('unavailable')
        expect(page.locator('tbody tr')).to_have_count(0)
        expect(page.get_by_role('button', name='Export request CSV')).to_be_disabled()
        page.get_by_role('tab', name='Skills usage', exact=True).click()
        expect(page.get_by_test_id('skills-usage').locator('.au-number').first).to_have_text('—')
        page.get_by_role('tab', name='Subscriptions', exact=True).click()
        expect(page.get_by_test_id('quota-home')).to_contain_text('Combined subscription quota unavailable')
        assert '0 provider(s)' not in page.get_by_test_id('quota-home').inner_text()
        # Polling has no event socket, no writes and no less-than-60-second loop.
        page.evaluate("demoAggregateCoverage='complete'")
        page.get_by_role('tab', name='All providers', exact=True).click()
        page.get_by_role('tab', name='Requests', exact=True).click()
        page.get_by_role('button', name='Refresh', exact=True).click()
        expect(page.locator('tbody tr').first).to_be_visible()
        starts = page.evaluate('demoSocketStarts')
        page.clock.install()
        # Remount under the deterministic clock, then observe actual callbacks.
        picker.select_option('profile:infra')
        picker.select_option('scope:all')
        expect(page.locator('tbody tr').first).to_be_visible()
        starts = page.evaluate('demoSocketStarts')
        page.evaluate('window.scopeCalls=[]')
        page.clock.run_for(59000)
        assert not page.evaluate("scopeCalls.some(c=>c.path.startsWith('/ledger?'))")
        page.clock.run_for(1000)
        page.wait_for_function("scopeCalls.some(c=>c.path.startsWith('/ledger?'))")
        assert page.evaluate("scopeCalls.filter(c=>c.path.startsWith('/ledger?')).length") == 1
        page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});window.scopeCalls=[]")
        page.clock.run_for(120000)
        assert not page.evaluate("scopeCalls.some(c=>c.path.startsWith('/ledger?'))")
        page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'visible'});window.holdAggregate=[]")
        page.clock.run_for(60000)
        page.wait_for_function('holdAggregate.length===1')
        page.clock.run_for(120000)
        assert page.evaluate('holdAggregate.length') == 1
        page.evaluate('holdAggregate.splice(0).forEach(f=>f());window.holdAggregate=null')
        assert page.evaluate('demoSocketStarts') == starts
        assert page.evaluate("scopeCalls.every(c=>c.method==='GET'&&!c.path.includes('/ledger/events'))")
        # Stored aggregate restores as a tag, while the old individual key survives.
        assert page.evaluate("demoStored['selected-profile-v1']") == 'infra'
        page.evaluate("plugin.register({rest:demoRest,socket:demoSocket,storage:{get:(k,d)=>demoStored[k]??d,set:(k,v)=>demoStored[k]=v},register:()=>{}})")
        expect(picker).to_have_value('scope:all')
        # Aggregate records still use the bounded pane at narrow widths.
        page.set_viewport_size({'width': 390, 'height': 850})
        page.clock.run_for(100)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert page.locator('.au-reader').evaluate('e=>e.clientHeight>0&&e.closest(".au-pane").clientHeight<=innerHeight')
        page.screenshot(path=str(ROOT / 'tests/ui/artifacts/all-profiles-narrow.png'))
        assert not errors, errors
        assert not network, network
        browser.close()
    print('PASS tagged picker, real all profile, persistence, delayed switching, scoped IDs/drills/snapshots, partial CSV, unavailable values, no mutations/socket, visible 60s single-flight polling and narrow pane')


if __name__ == '__main__':
    run()
