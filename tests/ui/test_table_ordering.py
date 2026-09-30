"""Time breakdown reverses only table rows, never the shared chart data.

The lower time list follows docs/RECORD_PAGING_CONTRACT.md: the first page holds
at most ten newest-first rows, Load more appends the next ten from the same
factual window, and View all records returns the complete list as one frozen
response. Ordering is asserted on every page against the full chart trend.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
PAGE = 10

LABELS = '''hour => [...orderingData.trend.buckets].sort((a,b)=>b.start-a.start).map(
    b => { const d=new Date(b.start*1000),offHour=d.getMinutes()!==0,time=hour||offHour||d.getHours()!==0;
        return d.toLocaleString(undefined,{month:'short',day:'numeric',...(time?{hour:'numeric',...(offHour?{minute:'2-digit'}:{})}:{})}); })'''


def settle(page, action, **params):
    """Run action and wait for the /ledger? response whose URL carries every given parameter.

    Unrelated /ledger? reads (a cache refresh, a follow-up page read) do not satisfy the wait.
    """
    seq = page.evaluate('orderingSeq')
    action()
    page.wait_for_function('''([prev, want]) => window.orderingUrls.slice(prev).some(url =>
        (q => Object.entries(want).every(([k, v]) => q.get(k) === v))(new URLSearchParams(url.split('?')[1])))''',
                           arg=[seq, params])


def check_chart(page, before=None):
    buckets = page.evaluate('orderingData.trend.buckets.map(b => b.start)')
    assert buckets == sorted(buckets), 'chart buckets must stay chronological'
    if before is not None:
        assert buckets == before, 'an appended page must not change the chart trend'
    return buckets


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
                                   headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        page = browser.new_page(viewport={'width': 1700, 'height': 1100}, locale='en-US', timezone_id='Europe/Paris')
        errors, network = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: network.append(r.url))
        page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
        page.evaluate('''() => {
            const original = rest;
            window.orderingSeq = 0;
            window.orderingUrls = [];
            rest = async (...args) => {
                const result = await original(...args);
                if (args[0].startsWith('/ledger?')) { window.orderingData = result; window.orderingUrls.push(args[0]); window.orderingSeq++; }
                return result;
            };
        }''')
        rows = page.locator('.au-breakdown tbody tr td:first-child .au-field-value')
        pager = page.get_by_test_id('record-pagination')
        load_more = page.get_by_role('button', name='Load more', exact=True)
        view_all = page.get_by_role('button', name='View all records', exact=True)
        paged, frozen = [], []
        page.get_by_role('tab', name='All providers', exact=True).click()
        for period, grouping in [('Past 24h', 'Hour'), ('7 days', 'Day')]:
            hour = grouping == 'Hour'
            page.get_by_role('group', name='Time window', exact=True).get_by_role('button', name=period, exact=True).click()
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name=grouping, exact=True).click()
            page.wait_for_function("window.orderingData && orderingData.trend.unit === " + repr('hour' if hour else 'day')
                                   + " && Array.isArray(orderingData.list_rows)")
            before = check_chart(page)
            expected = page.evaluate(LABELS, hour)
            total = len(expected)
            # The server counts the complete time list; the first page is the newest ten.
            assert page.evaluate('orderingData.list_count') == total, (period, total)
            expect(rows).to_have_text(expected[:PAGE])
            if total <= PAGE:
                expect(load_more).to_have_count(0)
                expect(view_all).to_have_count(0)
            else:
                paged.append(period)
                expect(pager).to_contain_text(f'Showing {PAGE} of {total} records')
                # Load more appends the next page from the same window; ordering holds across the join.
                settle(page, load_more.click, list_mode='page', offset=str(PAGE), group='time')
                shown = min(2 * PAGE, total)
                expect(pager).to_contain_text(f'Showing {shown} of {total} records')
                expect(rows).to_have_text(expected[:shown])
                check_chart(page, before)
                if shown < total:
                    # View all resolves the window anew; order against its own complete trend.
                    settle(page, view_all.click, list_mode='all', offset='0', group='time')
                    complete = page.evaluate(LABELS, hour)
                    expect(pager).to_contain_text(f'Showing {len(complete)} of {len(complete)} records')
                    expect(pager).to_contain_text('Frozen')
                    expect(load_more).to_have_count(0)
                    expect(view_all).to_have_count(0)
                    expect(rows).to_have_text(complete)
                    check_chart(page)
                    # Exit returns to the live ten-row page (cached or re-read); still newest first.
                    page.get_by_role('button', name='Exit frozen view', exact=True).click()
                    expect(pager).to_contain_text(f'Showing {PAGE} of ')
                    expect(rows).to_have_count(PAGE)
                    assert rows.all_text_contents() in (expected[:PAGE], complete[:PAGE]), rows.all_text_contents()
                    frozen.append(period)
                before = check_chart(page)
            # Switching tables must not leave the chart's bucket array reversed: the DTO the time
            # table rendered from keeps its chronological buckets, and the next read's chart is chronological.
            page.evaluate('window.orderingHeld = orderingData')
            settle(page, page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Model', exact=True).click,
                   list_mode='page', offset='0', group='model')
            assert page.evaluate('orderingHeld.trend.buckets.map(b => b.start)') == before
            check_chart(page)
        assert paged, 'fixture must provide at least one time list longer than one page'
        assert frozen, 'fixture must provide at least one time list longer than two pages (View all / Frozen / Exit)'
        assert not errors, errors
        assert not network, network
        browser.close()
        print('PASS Hour/Day tables newest first on the 10-row page and across Load more/View all ('
              + ', '.join(paged) + ' paged; View all/Frozen/Exit on ' + ', '.join(frozen) + '); chart buckets remain chronological; no errors or external requests')


if __name__ == '__main__':
    run()
