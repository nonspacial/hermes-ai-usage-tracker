"""Offline chart tooltip placement and live theme/point coverage (synthetic only)."""
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT = Path(os.environ.get('HERMES_SCRATCH', '/home/nope/.hermes/profiles/infra/cache/scratch')) / 'chart-point-tooltip-synthetic.png'


def test_chart_tooltip():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = browser.new_page(viewport={'width': 1480, 'height': 900}, locale='en-GB')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.set_content((ROOT / 'preview.html').read_text(), wait_until='domcontentloaded')
            page.evaluate('window.demoPeakValue=2;window.demoPeakUnmatched=1')
            page.evaluate('''() => {const original=rest;rest=async (...args)=>{
                const result=await original(...args);
                if(args[0].startsWith('/ledger?'))window.tooltipData=result;
                return result;
            }}''')
            page.get_by_role('navigation', name='Providers', exact=True).get_by_role('tab', name='Codex', exact=True).click()
            page.wait_for_function('window.demoCalls.filter(p=>p.startsWith("/ledger?")).length >= 2')
            page.wait_for_function('window.tooltipData?.trend?.buckets?.at(-1)?.missing_usage === 1')
            chart = page.get_by_test_id('usage-chart')
            expect(page.get_by_test_id('subagent-summary')).to_contain_text('Peak observed subagents: 2 · partial')
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Model').click()
            expect(page.get_by_role('columnheader', name='Peak observed')).to_be_visible()
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Subagents').click()
            expect(page.get_by_role('columnheader', name='Peak observed')).to_have_count(0)
            page.get_by_role('group', name='Breakdown grouping').get_by_role('button', name='Hour').click()
            plot = chart.locator('.au-plot-hit')
            page.locator('.au-upper').evaluate('e=>e.scrollTop=150')
            # The final synthetic bucket has one request with unknown usage.
            # Read its actual DTO, rather than manufacturing a production date.
            def point(index):
                box = plot.bounding_box()
                assert box
                fraction = page.evaluate('''index => {
                    const rows=window.tooltipData.trend.buckets;
                    return ((rows[index].start+rows[index].end)/2-rows[0].start)/
                        (rows.at(-1).end-rows[0].start);
                }''', index)
                return box['x'] + box['width'] * fraction, box['y'] + box['height'] / 2

            rows = page.evaluate('window.tooltipData.trend.buckets')
            known = next(i for i, row in enumerate(rows) if row['attempts'] and not row['missing_usage'])
            missing = len(rows) - 1
            before = page.evaluate('window.demoCalls.filter(p=>p.startsWith("/ledger?")).length')
            page.mouse.move(*point(known))
            popup = page.locator('.au-tooltip[role="tooltip"]:visible')
            footer = page.get_by_test_id('chart-point-summary')
            expect(footer).to_be_visible()
            known_expected = page.evaluate('''index => {
                const b=window.tooltipData.trend.buckets[index];
                return new Date(b.start*1000).toLocaleString(undefined,{timeZone:'UTC'})+
                    ' UTC · '+b.known.total_tokens.toLocaleString(undefined)+
                    ' tokens · '+b.attempts.toLocaleString(undefined)+' requests · Peak observed subagents: 2 · partial';
            }''', known)
            expect(footer).to_have_text(known_expected)
            assert 'Click to interact with Left/Right arrow keys' in popup.inner_text()
            assert 'Click and drag at least 15 minutes' in popup.inner_text()
            assert popup.evaluate('e=>e.lastElementChild.dataset.testid') == 'chart-point-summary'
            assert chart.locator('.au-chart-tip').inner_text() == ''
            assert footer.inner_text() not in chart.inner_text()
            assert footer.evaluate('e=>getComputedStyle(e).color') == chart.locator('.au-line').first.evaluate('e=>getComputedStyle(e).stroke')
            assert plot.get_attribute('aria-describedby') == popup.get_attribute('id')

            page.mouse.move(*point(missing))
            expected = page.evaluate('''() => {
                const b=window.tooltipData.trend.buckets.at(-1);
                return new Date(b.start*1000).toLocaleString(undefined,{timeZone:'UTC'})+
                    ' UTC · — tokens · '+b.attempts.toLocaleString(undefined)+
                    ' requests · includes missing usage · Peak observed subagents: 2 · partial';
            }''')
            expect(footer).to_have_text(expected)
            assert chart.locator('.au-chart-tip').inner_text() == ''
            page.mouse.click(*point(missing))
            expect(chart).to_be_focused()
            expect(footer).to_have_text(expected)
            assert chart.get_attribute('aria-describedby') == popup.get_attribute('id')
            chart.press('ArrowLeft')
            expect(footer).not_to_have_text(expected)
            assert footer.inner_text() == chart.get_attribute('data-au-chart-summary')
            chart.press('ArrowRight')
            expect(footer).to_have_text(expected)
            page.evaluate("document.documentElement.style.setProperty('--ui-accent','#3b9f84')")
            assert footer.evaluate('e=>getComputedStyle(e).color') == 'rgb(59, 159, 132)'
            assert footer.evaluate('e=>getComputedStyle(e).color') == chart.locator('.au-line').first.evaluate('e=>getComputedStyle(e).stroke')
            assert 'Click and drag at least 15 minutes' in popup.inner_text()
            page.screenshot(path=str(SCREENSHOT))
            page.evaluate("document.documentElement.style.removeProperty('--ui-accent')")
            assert footer.evaluate('e=>getComputedStyle(e).color') == chart.locator('.au-line').first.evaluate('e=>getComputedStyle(e).stroke')
            chart.evaluate("e=>e.closest('.au-ledger').style.setProperty('--ui-accent','#9a622f')")
            assert footer.evaluate('e=>getComputedStyle(e).color') == 'rgb(154, 98, 47)'
            chart.evaluate("e=>e.closest('.au-ledger').style.removeProperty('--ui-accent')")
            # Pointer capture while dragging must keep the selected point and
            # instructions visible; cancellation must not submit a new range.
            page.mouse.move(*point(missing))
            page.mouse.down()
            page.mouse.move(*point(known), steps=4)
            expect(chart.locator('.au-plot-selection')).to_be_visible()
            expect(footer).to_have_text(known_expected)
            assert 'Click and drag at least 15 minutes' in popup.inner_text()
            plot.dispatch_event('pointercancel', {'pointerId': 1})
            page.mouse.up()
            expect(chart.locator('.au-plot-selection')).to_have_count(0)
            page.set_viewport_size({'width': 480, 'height': 700})
            chart.focus()
            chart.press('ArrowRight')
            expect(footer).to_be_visible()
            assert popup.evaluate('e=>{const b=e.getBoundingClientRect();return b.left>=0&&b.right<=innerWidth&&b.top>=0&&b.bottom<=innerHeight}')
            assert page.evaluate('window.demoCalls.filter(p=>p.startsWith("/ledger?")).length') == before
            page.set_viewport_size({'width': 1480, 'height': 900})
            page.get_by_role('group', name='Usage display').get_by_role('button', name='Cost', exact=True).click()
            page.locator('.au-upper').evaluate('e=>e.scrollTop=150')
            page.mouse.move(*point(missing))
            expect(footer).to_contain_text(' · partial price coverage')
            assert 'includes missing usage' not in footer.inner_text()
            assert chart.locator('.au-chart-tip').inner_text() == ''
            assert not errors, errors
            print('PASS synthetic chart tooltip: exact unknown summary, absent pane text, focus/hover/keyboard, instructions, live theme, no fetch')
            print('Screenshot:', SCREENSHOT)
        finally:
            browser.close()


if __name__ == '__main__':
    test_chart_tooltip()
